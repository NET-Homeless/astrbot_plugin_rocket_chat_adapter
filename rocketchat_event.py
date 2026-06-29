from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    File,
    Image,
    Record,
    Video,
)

from .rocketchat_components import append_rendered_component
from .rocketchat_media import upload_combined_images
from .rocketchat_segments import (
    ImageGroup,
    MediaItem,
    dispatch_segments,
    iter_segments,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from astrbot.api.platform import AstrBotMessage, PlatformMetadata

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
        adapter: RocketChatAdapter,
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
        self.adapter: RocketChatAdapter = adapter
        self._typing_task: asyncio.Task | None = None
        self._typing_started: bool = False
        self._typing_keepalive_interval: float = 5.0
        self._typing_max_duration: float = 120.0
        self._reply_mention_sent: bool = False

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
        logger.debug(
            f"[RocketChat][Event] start_typing_indicator room={self.room_id!r}"
        )
        self._typing_task = asyncio.create_task(self._typing_indicator_worker())

    async def stop_typing_indicator(self) -> None:
        """结束 typing 状态并取消后台任务。"""
        task = self._typing_task
        self._typing_task = None

        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if self._typing_started:
            self._typing_started = False
            try:
                await self.adapter.send_typing(self.room_id, False, tmid=self.thread_id)
            except Exception as exc:
                logger.warning(f"[RocketChat][Event] stop typing failed: {exc!r}")

    async def _typing_indicator_worker(self) -> None:
        """延迟后启动 typing，并按官方续期窗口持续保活。"""
        try:
            delay = float(getattr(self.adapter, "typing_indicator_delay", 0.8))
            logger.debug(
                "[RocketChat][Event] typing worker started, delay=%ss room=%r max_duration=%ss",
                delay,
                self.room_id,
                self._typing_max_duration,
            )
            if delay > 0:
                await asyncio.sleep(delay)

            # 发送 start typing
            await self.adapter.send_typing(self.room_id, True, tmid=self.thread_id)
            self._typing_started = True
            logger.debug(f"[RocketChat][Event] typing started room={self.room_id!r}")

            # 对齐官方客户端续期模型：在 typing 超时窗口内定期 renew。
            started_at = asyncio.get_running_loop().time()
            while True:
                elapsed = asyncio.get_running_loop().time() - started_at
                remaining = self._typing_max_duration - elapsed
                if remaining <= 0:
                    logger.warning(
                        "[RocketChat][Event] typing auto-stopped after max duration room=%r max_duration=%ss",
                        self.room_id,
                        self._typing_max_duration,
                    )
                    break

                await asyncio.sleep(min(self._typing_keepalive_interval, remaining))
                elapsed = asyncio.get_running_loop().time() - started_at
                if elapsed >= self._typing_max_duration:
                    logger.warning(
                        "[RocketChat][Event] typing auto-stopped after max duration room=%r max_duration=%ss",
                        self.room_id,
                        self._typing_max_duration,
                    )
                    break

                await self.adapter.send_typing(self.room_id, True, tmid=self.thread_id)
                logger.debug(
                    "[RocketChat][Event] typing renewed room=%r interval=%ss",
                    self.room_id,
                    self._typing_keepalive_interval,
                )

        except asyncio.CancelledError:
            logger.debug(
                f"[RocketChat][Event] typing worker cancelled room={self.room_id!r}"
            )
            raise
        except Exception as exc:
            logger.warning(f"[RocketChat][Event] typing worker failed: {exc!r}")
        finally:
            if self._typing_started:
                self._typing_started = False
                try:
                    await self.adapter.send_typing(
                        self.room_id, False, tmid=self.thread_id
                    )
                except Exception as exc:
                    logger.warning(
                        f"[RocketChat][Event] typing worker stop failed: {exc!r}"
                    )
            if self._typing_task is asyncio.current_task():
                self._typing_task = None

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

        图文合并（保序）：
        - 普通房间中，仅当文本紧邻出现在 Image 之前时才会尽量合并为单条媒体消息
          （文本进入 rooms.mediaConfirm 的 msg，图片由官方媒体确认生成）。
        - 如果 Image 出现在文本之前，或 Image 后又出现文本，则拆开发送以保持原始顺序。
        - E2EE 房间保持逐条发送行为（加密附件格式不同）。
        - File / Record / Video 组件始终独立发送。

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

        # E2EE 房间保持现有逐条发送行为
        room_info = await self.adapter._get_room_info(self.room_id)
        is_e2ee = bool(room_info.get("encrypted") and room_info.get("t") in {"d", "p"})

        try:
            # 使用统一的保序切分 + 分发
            sender_adapter = _EventSegmentSender(self)
            segments = iter_segments(
                message.chain,
                is_e2ee=is_e2ee,
                render_at=True,
                render_other=lambda parts, comp: append_rendered_component(parts, comp),
            )
            await dispatch_segments(segments, sender_adapter)

            # ⚠️ 必须调用父类 send，框架在此更新发送状态 & 上报 Metric
            await super().send(message)
        finally:
            await self.stop_typing_indicator()

    async def _flush_text(self, text: str) -> None:
        """发送一段文本，自动选择引用回复或普通发送。"""
        logger.debug(
            "[RocketChat][Event] _flush_text() quote_original=%s text_len=%d",
            self.quote_original,
            len(text),
        )
        mention_username = await self._consume_reply_mention_username()
        if self.quote_original:
            logger.debug(
                f"[RocketChat][Event] 触发引用回复 quote_original=True thread_id={self.thread_id!r}"
            )
            # 频道 @mention 场景：仅用 attachments 显示引用框，不创建线程
            # 如果是线程消息，thread_id 已在初始化时设置，后续普通发送会用到它
            raw_message: dict = (
                self.message_obj.raw_message
                if isinstance(self.message_obj.raw_message, dict)
                else {}
            )
            await self.adapter.send_with_quote(
                self.room_id,
                text,
                raw_message,
                tmid=self.thread_id,  # 仅当原消息本身是线程消息时才传 thread_id
                mention_username=mention_username,
            )
            # 第一段文本已作为引用发出，后续内容走普通发送
            self.quote_original = False
        else:
            logger.debug(
                f"[RocketChat][Event] 普通发送 quote_original=False thread_id={self.thread_id!r}"
            )
            await self.adapter.send_text(
                self.room_id,
                text,
                tmid=self.thread_id,
                mention_username=mention_username,
            )

    async def _consume_reply_mention_username(self) -> str | None:
        if self._reply_mention_sent:
            return None

        if not await self.adapter._should_explicit_reply_mention(self.room_id):
            self._reply_mention_sent = True
            return None

        raw_message = getattr(self.message_obj, "raw_message", None) or {}
        sender = raw_message.get("u", {}) if isinstance(raw_message, dict) else {}
        username = str(sender.get("username") or "").strip()
        if not username or username == getattr(self.adapter, "bot_username", None):
            self._reply_mention_sent = True
            return None

        self._reply_mention_sent = True
        return username

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
            if await self._handle_remote_media_fallback(file_ref, "语音"):
                return
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
            if await self._handle_remote_media_fallback(file_ref, "视频"):
                return
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

    async def _handle_remote_media_fallback(
        self,
        file_ref: str,
        media_kind: str,
    ) -> bool:
        if not (file_ref.startswith("http://") or file_ref.startswith("https://")):
            return False
        return await self.adapter.send_remote_media_fallback(
            self.room_id,
            file_ref,
            media_kind=media_kind,
            tmid=self.thread_id,
        )

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
        # Base64 引用不含有意义的文件名，直接使用 fallback
        if file_ref.startswith("base64://"):
            return fallback
        parsed = urlparse(file_ref)
        candidate = os.path.basename(parsed.path)
        if candidate:
            return candidate
        return os.path.basename(local_path) or fallback

    async def _send_image_group(self, group: ImageGroup) -> None:
        """发送图片组（Segment 路径）。

        仅当 caption 非空时才尝试合并发送（text_before_image 场景）。
        否则逐个独立发送以保持顺序。
        """
        if not group.images:
            return

        images = list(group.images)
        text = group.caption

        # 无 caption：逐个发送（图片在前，或纯图片）
        if not text:
            for img in images:
                await self._send_image_component(img)
            return

        # 有 caption：引用回复场景特殊处理
        if self.quote_original:
            await self._flush_text(text)
            for img in images:
                await self._send_image_component(img)
            return

        # 有 caption：尝试合并发送
        mention_username = await self._consume_reply_mention_username()
        final_text = text
        if mention_username and final_text:
            final_text = f"@{mention_username} {final_text}"

        sent_text_with_image = await upload_combined_images(
            self.adapter,
            self.room_id,
            final_text,
            images,
            tmid=self.thread_id,
        )

        if final_text and not sent_text_with_image:
            await self.adapter.send_text(
                self.room_id,
                final_text,
                tmid=self.thread_id,
            )

    async def _send_image_component(self, img: Image) -> None:
        """发送图片组件（统一转换为本地上传以避免防盗链问题）。"""
        file_ref: str = img.file or getattr(img, "url", None) or ""

        if not file_ref:
            logger.warning("[RocketChat] 收到空 file 字段的 Image 组件，已跳过")
            return

        if file_ref.startswith("http://") or file_ref.startswith("https://"):
            await self.adapter.send_image_url(
                self.room_id,
                file_ref,
                tmid=self.thread_id,
            )
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


# ============================================================================
# 保序发送：SegmentSender 适配器（Event 路径）
# ============================================================================


class _EventSegmentSender:
    """RocketChatMessageEvent 的 SegmentSender 适配器。

    封装 quote_original、mention、线程等 Event 特有的发送语义。
    """

    def __init__(self, event: RocketChatMessageEvent) -> None:
        self.event = event

    async def send_text(self, text: str) -> None:
        await self.event._flush_text(text)

    async def send_image_group(self, group: ImageGroup) -> None:
        await self.event._send_image_group(group)

    async def send_media(self, item: MediaItem) -> None:
        comp = item.component
        if isinstance(comp, File):
            await self.event._send_file_component(comp)
        elif isinstance(comp, Record):
            await self.event._send_record_component(comp)
        elif isinstance(comp, Video):
            await self.event._send_video_component(comp)
