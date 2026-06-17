from __future__ import annotations

import asyncio
import os
from typing import Callable, TYPE_CHECKING
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

from .rocketchat_components import append_rendered_component

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
            logger.debug(f"[RocketChat][Event] typing worker cancelled room={self.room_id!r}")
            raise
        except Exception as exc:
            logger.warning(f"[RocketChat][Event] typing worker failed: {exc!r}")
        finally:
            if self._typing_started:
                self._typing_started = False
                try:
                    await self.adapter.send_typing(self.room_id, False, tmid=self.thread_id)
                except Exception as exc:
                    logger.warning(f"[RocketChat][Event] typing worker stop failed: {exc!r}")
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

        text_parts: list[str] = []
        pending_images: list[Image] = []
        # 标记当前段是否在图片之前就已经有文本（决定能否合并）
        text_before_image = False

        try:
            for comp in message.chain:
                if isinstance(comp, Plain):
                    if pending_images:
                        await self._flush_group(
                            text_parts, pending_images, text_before_image,
                        )
                        text_before_image = False
                    text_parts.append(comp.text)

                elif isinstance(comp, AtAll):
                    if pending_images:
                        await self._flush_group(
                            text_parts, pending_images, text_before_image,
                        )
                        text_before_image = False
                    text_parts.append("@all ")

                elif isinstance(comp, At):
                    mention_name = (
                        getattr(comp, "name", None) or getattr(comp, "qq", None) or ""
                    )
                    rendered = f"@{mention_name} " if mention_name else ""
                    if pending_images:
                        await self._flush_group(
                            text_parts, pending_images, text_before_image,
                        )
                        text_before_image = False
                    if rendered:
                        text_parts.append(rendered)

                elif isinstance(comp, Image):
                    if is_e2ee:
                        # E2EE: 先发文本，再单独发图片
                        await self._flush_group(
                            text_parts, pending_images, text_before_image,
                        )
                        text_before_image = False
                        await self._send_image_component(comp)
                    else:
                        # 首次遇到图片时，检查前面是否有文本
                        if not pending_images and not text_parts:
                            pass  # 空段，直接加入
                        elif not pending_images:
                            # 第一张图片，记录之前是否有文本
                            text_before_image = bool("".join(text_parts).strip())
                        pending_images.append(comp)

                elif isinstance(comp, (File, Record, Video)):
                    # 非图片媒体：先 flush 当前段，再单独发送
                    await self._flush_group(
                        text_parts, pending_images, text_before_image,
                    )
                    text_before_image = False

                    if isinstance(comp, File):
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
                    if pending_images:
                        await self._flush_group(
                            text_parts, pending_images, text_before_image,
                        )
                        text_before_image = False
                    append_rendered_component(text_parts, comp)

            # 发送剩余内容
            await self._flush_group(text_parts, pending_images, text_before_image)

            # ⚠️ 必须调用父类 send，框架在此更新发送状态 & 上报 Metric
            await super().send(message)
        finally:
            await self.stop_typing_indicator()

    async def _flush_group(
        self,
        text_parts: list[str],
        pending_images: list[Image],
        text_before_image: bool,
    ) -> None:
        """
        Flush 当前段的内容，保持原始顺序。

        - 文本在前 + 图片在后 → 首张图片携带文本合并为官方媒体消息
        - 图片在前（text_before_image=False）→ 图片单独发送以保持顺序，
          文本随后单独发送
        - 纯文本 / 纯图片 → 按类型发送
        """
        text = "".join(text_parts).strip()

        if pending_images:
            if text_before_image and text:
                # 文本出现在图片之前 → 安全合并
                await self._send_combined(text, list(pending_images))
            else:
                # 图片在前或没有文本 → 图片单独发送以保持顺序
                for img in pending_images:
                    await self._send_image_component(img)
                if text:
                    await self._flush_text(text)
        elif text:
            await self._flush_text(text)

        text_parts.clear()
        pending_images.clear()

    async def _send_combined(
        self, text: str, images: list[Image],
    ) -> None:
        """
        将文本与图片合并为一条 Rocket.Chat 消息发送。

        首张图片通过 rooms.media + rooms.mediaConfirm(msg=...) 生成带文本的媒体消息。
        多张图片时，后续图片单独确认生成消息，避免使用非官方的未 confirm 附件路径。
        如果引用回复（quote_original）激活，第一段文本先走引用回复，图片随后单独发送。
        """
        if not text and not images:
            return

        # 引用回复场景：文本先走引用，图片作为独立消息发送
        if self.quote_original and text:
            await self._flush_text(text)
            for img in images:
                await self._send_image_component(img)
            return

        # 消费 reply mention username（首次发送时附加 @username）
        mention_username = await self._consume_reply_mention_username()
        final_text = text
        if mention_username and final_text:
            final_text = f"@{mention_username} {final_text}"

        cleanups: list[Callable[[], None]] = []
        sent_text_with_image = False

        try:
            for img in images:
                file_ref: str = img.file or getattr(img, "url", None) or ""
                if not file_ref:
                    continue

                local_path: str | None = None
                cleanup: Callable[[], None] | None = None

                if file_ref.startswith("http://") or file_ref.startswith("https://"):
                    local_path, cleanup = await self.adapter._download_remote_media(
                        file_ref, ".png"
                    )
                else:
                    local_path, cleanup = await self._resolve_uploadable_path(
                        file_ref, default_suffix=".png"
                    )

                if cleanup:
                    cleanups.append(cleanup)
                if not local_path:
                    continue

                resolved_name = self._guess_filename(file_ref, local_path, "image.png")
                caption = final_text if not sent_text_with_image else ""
                uploaded = await self.adapter._media.upload_local_file(
                    self.room_id,
                    local_path,
                    resolved_name,
                    description=caption,
                    tmid=self.thread_id,
                )
                if uploaded:
                    sent_text_with_image = sent_text_with_image or bool(caption)
                    logger.debug(
                        "[RocketChat][Event] _send_combined: uploaded image %s room=%s caption=%s",
                        resolved_name, self.room_id, bool(caption),
                    )
                else:
                    logger.warning(
                        "[RocketChat][Event] _send_combined: upload failed for %s",
                        resolved_name,
                    )
        finally:
            for cleanup in cleanups:
                cleanup()

        if final_text and not sent_text_with_image:
            await self.adapter.send_text(
                self.room_id, final_text, tmid=self.thread_id,
            )

    async def _flush_text(self, text: str) -> None:
        """发送一段文本，自动选择引用回复或普通发送。"""
        logger.debug(
            "[RocketChat][Event] _flush_text() quote_original=%s text=%r",
            self.quote_original,
            text[:80],
        )
        mention_username = await self._consume_reply_mention_username()
        if self.quote_original:
            logger.debug(
                f"[RocketChat][Event] 触发引用回复 quote_original=True thread_id={self.thread_id!r}"
            )
            # 频道 @mention 场景：仅用 attachments 显示引用框，不创建线程
            # 如果是线程消息，thread_id 已在初始化时设置，后续普通发送会用到它
            await self.adapter.send_with_quote(
                self.room_id,
                text,
                self.message_obj.raw_message,
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
