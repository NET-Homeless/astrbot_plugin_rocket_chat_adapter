from __future__ import annotations

import asyncio
import base64
import os
import tempfile
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    At,
    AtAll,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.platform import AstrBotMessage, PlatformMetadata

if TYPE_CHECKING:
    from .rocketchat_adapter import RocketChatAdapter


class RocketChatMessageEvent(AstrMessageEvent):
    """
    Rocket.Chat 平台消息事件。

    负责将 AstrBot 框架生成的 MessageChain 回复发送回 Rocket.Chat 对应的房间。
    每一条收到的消息都会对应一个该事件实例，保存了目标 room_id 和适配器引用，
    以便在 send() 时知道发往哪个房间、以及调用适配器的发送方法。
    """

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        room_id: str,
        thread_id: str | None,
        adapter: "RocketChatAdapter",
        quote_original: bool = False,
    ) -> None:
        """
        Parameters
        ----------
        message_str:    纯文本消息字符串（供 LLM / 指令解析使用）
        message_obj:    完整的 AstrBotMessage 对象
        platform_meta:  平台元数据
        session_id:     会话 ID（等于 Rocket.Chat 的 roomId）
        room_id:        Rocket.Chat 房间 ID，发送回复时使用
        thread_id:      Rocket.Chat 线程 ID，回复线程消息时使用
        adapter:        RocketChatAdapter 实例，持有 HTTP session 和发送方法
        """
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.room_id: str = room_id
        self.thread_id: str | None = thread_id
        self.quote_original: bool = quote_original
        self.adapter: "RocketChatAdapter" = adapter
        self._typing_task: asyncio.Task | None = None
        self._typing_started: bool = False
        self._typing_keepalive_interval: float = 3.0

    # ------------------------------------------------------------------
    # typing 指示器
    # ------------------------------------------------------------------

    def start_typing_indicator(self) -> None:
        """
        启动一个延迟 typing 任务。

        设计目标：
        - 群聊 / 线程里 @bot 后，如果 LLM 需要较长时间生成回复，则显示 typing
        - 如果是系统命令等快速回复，在 delay 时间内完成，则不会显示 typing
        """
        if self._typing_task is not None and not self._typing_task.done():
            return
        logger.debug(f"[RocketChat][Event] start_typing_indicator room={self.room_id!r}")
        self._typing_task = asyncio.create_task(self._typing_indicator_worker())

    async def stop_typing_indicator(self) -> None:
        """结束 typing 状态并取消后台任务。"""
        task = self._typing_task
        self._typing_task = None

        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if self._typing_started:
            self._typing_started = False
            try:
                await self.adapter.send_typing(self.room_id, False)
            except Exception as exc:
                logger.warning(f"[RocketChat][Event] stop typing failed: {exc!r}")

    async def _typing_indicator_worker(self) -> None:
        """延迟后启动 typing，保持状态直到任务被取消。"""
        try:
            delay = float(getattr(self.adapter, "typing_indicator_delay", 0.8))
            logger.debug(f"[RocketChat][Event] typing worker started, delay={delay}s room={self.room_id!r}")
            if delay > 0:
                await asyncio.sleep(delay)

            # 发送 start typing
            await self.adapter.send_typing(self.room_id, True)
            self._typing_started = True
            logger.debug(f"[RocketChat][Event] typing started room={self.room_id!r}")

            # 等待任务被取消（即回复完成时）
            # Rocket.Chat 的 typing indicator 会自动过期，通常不需要 keepalive
            while True:
                await asyncio.sleep(1)  # 每秒检查一次是否被取消

        except asyncio.CancelledError:
            logger.debug(f"[RocketChat][Event] typing worker cancelled room={self.room_id!r}")
            raise
        except Exception as exc:
            logger.warning(f"[RocketChat][Event] typing worker failed: {exc!r}")

    # ------------------------------------------------------------------
    # 核心：将 MessageChain 发送到 Rocket.Chat
    # ------------------------------------------------------------------

    async def send(self, message: MessageChain) -> None:
        """
        将 AstrBot 生成的消息链发送回 Rocket.Chat。

        回复场景：
        - 频道 @mention（非线程）→ 引用原消息发到大厅（quote_original=True）
        - 线程 @mention          → 在同一线程里回复（thread_id 已设置）
        - 私信                   → 直接回复（无 tmid，无引用）

        注意：必须在末尾调用 await super().send(message)，
              框架依赖该调用更新内部发送状态与统计指标。
        """
        logger.debug(
            "[RocketChat][Event] send() quote_original=%s thread_id=%r chain=%r",
            self.quote_original,
            self.thread_id,
            [type(c).__name__ for c in message.chain],
        )
        await self.stop_typing_indicator()

        text_parts: list[str] = []

        try:
            for comp in message.chain:
                if isinstance(comp, Plain):
                    text_parts.append(comp.text)

                elif isinstance(comp, AtAll):
                    text_parts.append("@all ")

                elif isinstance(comp, At):
                    # Rocket.Chat 支持 @username 格式的提及
                    # AstrBot At 组件可能使用 name 或 qq 字段存储用户标识
                    mention_name = (
                        getattr(comp, "name", None) or getattr(comp, "qq", None) or ""
                    )
                    if mention_name:
                        text_parts.append(f"@{mention_name} ")

                elif isinstance(comp, (Image, File, Record, Video)):
                    combined = "".join(text_parts).strip()
                    if combined:
                        await self._flush_text(combined)
                        text_parts.clear()

                    if isinstance(comp, Image):
                        await self._send_image_component(comp)
                    elif isinstance(comp, File):
                        await self._send_file_component(comp)
                    elif isinstance(comp, Record):
                        await self._send_record_component(comp)
                    elif isinstance(comp, Video):
                        await self._send_video_component(comp)

                elif isinstance(comp, Reply):
                    # AstrBot 会自动在部分场景附加 Reply 组件。
                    # 由于 Rocket.Chat 适配器已经使用了原生的引用回复语法 (quote_original)
                    # 或者通过 thread (tmid) 回复，因此直接无视它，避免把内部对象发出去
                    pass

                else:
                    # 其他组件（Forward 等）暂时转文本兜底
                    fallback = str(comp)
                    if fallback:
                        text_parts.append(fallback)

            # 发送剩余文本
            remaining_text = "".join(text_parts).strip()
            if remaining_text:
                await self._flush_text(remaining_text)

            # ⚠️ 必须调用父类 send，框架在此更新发送状态 & 上报 Metric
            await super().send(message)
        finally:
            await self.stop_typing_indicator()

    async def _flush_text(self, text: str) -> None:
        """发送一段文本，自动选择引用回复或普通发送。"""
        logger.debug(
            "[RocketChat][Event] _flush_text() quote_original=%s text=%r",
            self.quote_original,
            text[:80],
        )
        if self.quote_original:
            await self.adapter.send_with_quote(
                self.room_id,
                text,
                self.message_obj.raw_message,
            )
            # 第一段文本已作为引用发出，后续内容走普通发送
            self.quote_original = False
        else:
            await self.adapter.send_text(self.room_id, text, tmid=self.thread_id)

    async def _send_file_component(self, file_comp: File) -> None:
        """发送普通文件组件。"""
        file_ref = file_comp.file or file_comp.url or ""
        if not file_ref:
            logger.warning("[RocketChat] 收到空文件组件，已跳过")
            return

        if file_ref.startswith("http://") or file_ref.startswith("https://"):
            text = (
                f"{file_comp.name}: {file_ref}"
                if getattr(file_comp, "name", None)
                else file_ref
            )
            await self.adapter.send_text(self.room_id, text, self.thread_id)
            return

        local_path = file_ref.replace("file:///", "").replace("file://", "")
        if local_path:
            await self.adapter.send_file(
                self.room_id,
                local_path,
                filename=getattr(file_comp, "name", None),
                tmid=self.thread_id,
            )
        else:
            logger.warning(f"[RocketChat] 无法识别的文件路径格式: {file_ref!r}，已跳过")

    async def _send_record_component(self, record: Record) -> None:
        """发送语音组件。"""
        file_ref = record.file or getattr(record, "url", None) or ""
        if not file_ref:
            logger.warning("[RocketChat] 收到空 file 字段的 Record 组件，已跳过")
            return

        local_path, cleanup = await self._resolve_uploadable_path(
            file_ref,
            default_suffix=".ogg",
        )
        if not local_path:
            logger.warning(f"[RocketChat] 无法识别的语音路径格式: {file_ref!r}，已跳过")
            return

        try:
            await self.adapter.send_file(
                self.room_id,
                local_path,
                filename=self._guess_filename(file_ref, local_path, "record.ogg"),
                tmid=self.thread_id,
            )
        finally:
            if cleanup:
                cleanup()

    async def _send_video_component(self, video: Video) -> None:
        """发送视频组件。"""
        file_ref = video.file or ""
        if not file_ref:
            logger.warning("[RocketChat] 收到空 file 字段的 Video 组件，已跳过")
            return

        local_path, cleanup = await self._resolve_uploadable_path(
            file_ref,
            default_suffix=".mp4",
        )
        if not local_path:
            logger.warning(f"[RocketChat] 无法识别的视频路径格式: {file_ref!r}，已跳过")
            return

        try:
            await self.adapter.send_file(
                self.room_id,
                local_path,
                filename=self._guess_filename(file_ref, local_path, "video.mp4"),
                tmid=self.thread_id,
            )
        finally:
            if cleanup:
                cleanup()

    async def _resolve_uploadable_path(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        """将组件引用解析为可上传的本地文件路径。"""
        if file_ref.startswith("http://") or file_ref.startswith("https://"):
            return await self.adapter._download_remote_media(file_ref, default_suffix)
        if file_ref.startswith("base64://"):
            return self.adapter._decode_base64_media(file_ref, default_suffix)

        local_path = file_ref.replace("file:///", "").replace("file://", "")
        return (local_path or None, None)

    def _guess_filename(self, file_ref: str, local_path: str, fallback: str) -> str:
        parsed = urlparse(file_ref)
        candidate = os.path.basename(parsed.path)
        if candidate:
            return candidate
        return os.path.basename(local_path) or fallback

    async def _send_image_component(self, img: Image) -> None:
        """发送图片组件（统一转换为本地上传以避免防盗链问题）。"""
        file_ref: str = img.file or ""

        if not file_ref:
            logger.warning("[RocketChat] 收到空 file 字段的 Image 组件，已跳过")
            return

        local_path, cleanup = await self._resolve_uploadable_path(
            file_ref,
            default_suffix=".png",
        )
        if not local_path:
            logger.warning(f"[RocketChat] 无法解析图片路径: {file_ref!r}，已跳过")
            return

        try:
            await self.adapter.send_image_file(
                self.room_id, local_path, tmid=self.thread_id
            )
        finally:
            if cleanup:
                cleanup()
