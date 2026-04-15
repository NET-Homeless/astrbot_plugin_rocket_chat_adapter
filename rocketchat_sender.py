from __future__ import annotations

from typing import Any, Callable, Optional

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import File, Image, Plain, Record, Reply, Video


class RocketChatSenderBridge:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    async def post_json_message(self, url: str, payload: dict) -> bool:
        try:
            async with self.adapter._http_session.post(
                url,
                json=payload,
                headers=self.adapter._auth_headers(),
            ) as resp:
                data = await resp.json()
                if not data.get("success"):
                    logger.error(f"[RocketChat] 发送消息失败: {data}")
                    return False
                return True
        except Exception as exc:
            logger.error(f"[RocketChat] 发送消息异常: {exc!r}")
            return False

    async def send_structured_message(
        self,
        room_id: str,
        text: str = "",
        *,
        attachments: Optional[list[dict[str, Any]]] = None,
        tmid: Optional[str] = None,
        e2e_mentions: Optional[dict[str, Any]] = None,
    ) -> bool:
        room_info = await self.adapter._get_room_info(room_id)
        is_e2ee_room = room_info.get("encrypted") and room_info.get("t") in {"d", "p"}

        if is_e2ee_room:
            encrypted_payload = await self.adapter._e2ee.build_send_message(
                room_id,
                text=text,
                attachments=attachments,
                tmid=tmid,
                e2e_mentions=e2e_mentions,
            )
            if not encrypted_payload:
                logger.warning(
                    f"[RocketChat][E2EE] 房间为加密房间，当前无法安全发送，已跳过 room_id={room_id!r}"
                )
                return False
            return await self.post_json_message(
                f"{self.adapter.server_url}/api/v1/chat.sendMessage",
                encrypted_payload,
            )

        payload: dict[str, Any] = {"roomId": room_id, "text": text}
        if attachments:
            payload["attachments"] = attachments
        if tmid:
            payload["tmid"] = tmid
        return await self.post_json_message(
            f"{self.adapter.server_url}/api/v1/chat.postMessage",
            payload,
        )

    async def build_explicit_reply_mention(
        self,
        room_id: str,
        mention_username: Optional[str],
    ) -> tuple[str | None, dict[str, Any] | None]:
        room_info = await self.adapter._get_room_info(room_id)
        if not (mention_username and room_info.get("encrypted") and room_info.get("t") == "p"):
            return None, None

        normalized = mention_username.lstrip("@").strip()
        if not normalized:
            return None, None

        return (
            f"@{normalized}",
            {
                "e2eUserMentions": [f"@{normalized}"],
                "e2eChannelMentions": [],
            },
        )

    async def should_explicit_reply_mention(self, room_id: str) -> bool:
        room_info = await self.adapter._get_room_info(room_id)
        return bool(room_info.get("encrypted") and room_info.get("t") == "p")

    async def send_text(
        self,
        room_id: str,
        text: str,
        tmid: Optional[str] = None,
        mention_username: Optional[str] = None,
    ) -> None:
        mention_text, e2e_mentions = await self.build_explicit_reply_mention(
            room_id,
            mention_username,
        )
        final_text = f"{mention_text} {text}".strip() if mention_text else text
        await self.send_structured_message(
            room_id,
            final_text,
            tmid=tmid,
            e2e_mentions=e2e_mentions,
        )

    async def send_typing(self, room_id: str, flag: bool) -> None:
        if not self.adapter._ws or self.adapter._ws.closed or not self.adapter.bot_username:
            logger.debug(
                f"[RocketChat] typing 跳过: ws={self.adapter._ws is not None and not getattr(self.adapter._ws, 'closed', True)} "
                f"bot_username={self.adapter.bot_username!r}"
            )
            return

        try:
            logger.debug(
                f"[RocketChat] send typing room_id={room_id!r} user={self.adapter.bot_username!r} flag={flag}"
            )
            self.adapter._ddp_call_id += 1
            await self.adapter._ws.send_json(
                {
                    "msg": "method",
                    "method": "stream-notify-room",
                    "id": f"typing-{self.adapter._ddp_call_id}",
                    "params": [
                        f"{room_id}/user-activity",
                        self.adapter.bot_username,
                        ["user-typing"] if flag else [],
                        {},
                    ],
                }
            )
        except Exception as exc:
            logger.warning(
                f"[RocketChat] 发送 typing 状态失败 room_id={room_id!r} flag={flag}: {exc!r}"
            )

    async def send_with_quote(
        self,
        room_id: str,
        text: str,
        original_msg: dict,
        tmid: Optional[str] = None,
        mention_username: Optional[str] = None,
    ) -> None:
        msg_id = original_msg.get("_id", "")
        link = self.adapter._build_message_link(room_id, msg_id)
        mention_text, e2e_mentions = await self.build_explicit_reply_mention(
            room_id,
            mention_username,
        )

        reply_line = f"{mention_text} {text}".strip() if mention_text else text
        final_text = f"[ ]({link})\n{reply_line}" if link else reply_line

        logger.info(
            f"[RocketChat] send_with_quote() 发送: quote_msg_id={msg_id!r} tmid={tmid!r}"
        )
        await self.send_structured_message(
            room_id,
            final_text,
            tmid=tmid,
            e2e_mentions=e2e_mentions,
        )

    async def send_image_url(
        self,
        room_id: str,
        image_url: str,
        text: str = "",
        tmid: Optional[str] = None,
    ) -> None:
        await self.adapter._media.send_image_url(room_id, image_url, text=text, tmid=tmid)

    async def send_image_file(
        self,
        room_id: str,
        file_path: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> None:
        await self.adapter._media.send_image_file(
            room_id,
            file_path,
            description=description,
            tmid=tmid,
        )

    async def send_file(
        self,
        room_id: str,
        file_path: str,
        filename: str | None = None,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> None:
        await self.adapter._media.send_file(
            room_id,
            file_path,
            filename=filename,
            description=description,
            tmid=tmid,
        )

    async def send_remote_media_fallback(
        self,
        room_id: str,
        media_url: str,
        *,
        media_kind: str,
        text: str = "",
        tmid: Optional[str] = None,
    ) -> bool:
        return await self.adapter._media.send_remote_media_fallback(
            room_id,
            media_url,
            media_kind=media_kind,
            text=text,
            tmid=tmid,
        )

    async def resolve_outbound_media_path(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        if file_ref.startswith("http://") or file_ref.startswith("https://"):
            return await self.adapter._media.download_remote_media(file_ref, default_suffix)
        if file_ref.startswith("base64://"):
            return self.adapter._media.decode_base64_media(file_ref, default_suffix)

        local_path = file_ref.replace("file:///", "").replace("file://", "")
        return (local_path or None, None)

    async def download_remote_media(
        self,
        url: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        return await self.adapter._media.download_remote_media(url, default_suffix)

    def decode_base64_media(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        return self.adapter._media.decode_base64_media(file_ref, default_suffix)

    async def send_message_chain(
        self,
        room_id: str,
        message_chain: MessageChain,
        tmid: Optional[str] = None,
    ) -> None:
        reply_comp = next((c for c in message_chain.chain if isinstance(c, Reply)), None)
        full_text = "".join([c.text for c in message_chain.chain if isinstance(c, Plain)]).strip()

        reply_sent = False
        if reply_comp:
            q_msg = await self.adapter._fetch_message_by_id(reply_comp.id)
            if q_msg:
                await self.send_with_quote(room_id, full_text, q_msg, tmid)
                reply_sent = True
            else:
                link = self.adapter._build_message_link(room_id, reply_comp.id)
                if link:
                    await self.send_text(room_id, f"[ ]({link}) {full_text}", tmid)
                    reply_sent = True

        if not reply_sent:
            text_parts: list[str] = []
            for comp in message_chain.chain:
                if isinstance(comp, Plain):
                    text_parts.append(comp.text)
                elif isinstance(comp, (Image, File, Record, Video)):
                    if text_parts:
                        await self.send_text(room_id, "".join(text_parts), tmid)
                        text_parts.clear()

                if isinstance(comp, Image):
                    file_ref: str = comp.file or ""
                    if file_ref.startswith("http"):
                        await self.send_image_url(room_id, file_ref, tmid=tmid)
                    else:
                        local_path = file_ref.replace("file:///", "").replace("file://", "")
                        if local_path:
                            await self.send_image_file(room_id, local_path, tmid=tmid)

                elif isinstance(comp, File):
                    file_ref = comp.file or getattr(comp, "url", None) or ""
                    if file_ref.startswith("http://") or file_ref.startswith("https://"):
                        await self.send_text(
                            room_id,
                            f"{comp.name}: {file_ref}" if getattr(comp, "name", None) else file_ref,
                            tmid,
                        )
                    else:
                        local_path = file_ref.replace("file:///", "").replace("file://", "")
                        if local_path:
                            await self.send_file(
                                room_id,
                                local_path,
                                filename=getattr(comp, "name", None),
                                tmid=tmid,
                            )

                elif isinstance(comp, (Record, Video)):
                    file_ref = comp.file or getattr(comp, "url", None) or ""
                    suffix = ".mp4" if isinstance(comp, Video) else ".ogg"
                    media_path, cleanup = await self.resolve_outbound_media_path(file_ref, suffix)
                    if media_path:
                        try:
                            await self.send_file(room_id, media_path, tmid=tmid)
                        finally:
                            if cleanup:
                                cleanup()
                else:
                    fallback = str(comp)
                    if fallback:
                        text_parts.append(fallback)

            if text_parts:
                await self.send_text(room_id, "".join(text_parts), tmid)
