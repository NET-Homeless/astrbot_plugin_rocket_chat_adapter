from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.platform import AstrBotMessage, Group, MessageMember, MessageType

from .rocketchat_event import RocketChatMessageEvent


class RocketChatInboundBridge:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self._processed_message_ttl_seconds = 10 * 60
        self._processed_message_cache_limit = 4096
        # 消息去重锁，防止并发处理导致重复消息
        self._dedup_lock: Optional[asyncio.Lock] = None

    def extract_mentions_for_wake(self, raw_msg: dict) -> list[Any]:
        mentions = raw_msg.get("mentions")
        if isinstance(mentions, list) and mentions:
            return mentions

        e2e_mentions = raw_msg.get("e2eMentions")
        if isinstance(e2e_mentions, dict):
            merged: list[Any] = []
            merged.extend(e2e_mentions.get("e2eUserMentions", []) or [])
            merged.extend(e2e_mentions.get("e2eChannelMentions", []) or [])
            return merged

        if isinstance(e2e_mentions, list):
            return e2e_mentions

        return []

    def _iter_textual_mentions(self, raw_msg: dict) -> list[tuple[str, str, str]]:
        seen: set[str] = set()
        resolved: list[tuple[str, str, str]] = []

        for mention in self.extract_mentions_for_wake(raw_msg):
            if isinstance(mention, dict):
                username = str(mention.get("username") or "").strip()
                if not username or username in seen:
                    continue
                mention_id = str(
                    mention.get("_id") or mention.get("id") or username
                ).strip()
                display_name = str(mention.get("name") or username).strip() or username
                resolved.append((username, mention_id, display_name))
                seen.add(username)
                continue

            if isinstance(mention, str):
                username = mention.lstrip("@").strip()
                if not username or username in seen:
                    continue
                resolved.append((username, username, username))
                seen.add(username)

        return resolved

    def _build_text_components(self, raw_msg: dict, text: str) -> list[Any]:
        if not text:
            return []

        mentions = self._iter_textual_mentions(raw_msg)
        if not mentions:
            return [Plain(text=text)]

        token_map = {
            f"@{username}": (mention_id, display_name)
            for username, mention_id, display_name in mentions
        }
        pattern = re.compile(
            "("
            + "|".join(
                re.escape(token)
                for token in sorted(token_map.keys(), key=len, reverse=True)
            )
            + ")"
        )

        chain: list[Any] = []
        for part in pattern.split(text):
            if not part or not part.strip():
                continue
            mention_meta = token_map.get(part)
            if mention_meta:
                mention_id, display_name = mention_meta
                chain.append(At(qq=mention_id, name=display_name))
            else:
                chain.append(Plain(text=part))
        return chain

    def _build_plain_text(self, components: list[Any]) -> str:
        plain_text = "".join(
            comp.text for comp in components if isinstance(comp, Plain) and comp.text
        )
        return re.sub(r"\s+", " ", plain_text).strip()

    def _classify_mentions(self, raw_msg: dict) -> tuple[bool, bool]:
        bot_mentioned = False
        has_other_user_mentions = False

        for mention in self.extract_mentions_for_wake(raw_msg):
            if isinstance(mention, str):
                normalized = mention.lstrip("@").strip()
                if normalized in {self.adapter.user_id, self.adapter.bot_username}:
                    bot_mentioned = True
                elif normalized:
                    has_other_user_mentions = True
                continue

            if isinstance(mention, dict):
                is_bot = (
                    mention.get("_id") == self.adapter.user_id
                    or mention.get("username") == self.adapter.bot_username
                )
                if is_bot:
                    bot_mentioned = True
                elif mention.get("_id") or mention.get("username") or mention.get("name"):
                    has_other_user_mentions = True

        return bot_mentioned, has_other_user_mentions

    async def _claim_incoming_message(self, raw_msg: dict) -> bool:
        """
        检查并标记消息为已处理，防止重复处理。

        使用 asyncio.Lock 保护临界区，避免并发竞态条件导致同一消息被处理多次。

        Args:
            raw_msg: 原始消息字典

        Returns:
            True 表示这是新消息，应该处理；False 表示重复消息，应该跳过
        """
        message_id = str(raw_msg.get("_id") or "").strip()
        if not message_id:
            return True

        seen = getattr(self.adapter, "_processed_message_ids", None)
        if not isinstance(seen, dict):
            return True

        # 延迟初始化锁（在事件循环中）
        if self._dedup_lock is None:
            self._dedup_lock = asyncio.Lock()

        # 使用锁保护整个 check-then-set 操作，确保原子性
        async with self._dedup_lock:
            now = time.time()
            expires_before = now - self._processed_message_ttl_seconds

            # 清理过期条目（仅在超过限制时）
            if len(seen) > self._processed_message_cache_limit:
                for cached_id, timestamp in list(seen.items()):
                    if timestamp < expires_before:
                        seen.pop(cached_id, None)
                while len(seen) > self._processed_message_cache_limit:
                    seen.pop(next(iter(seen)), None)

            # 检查是否已处理
            if message_id in seen:
                logger.debug(
                    f"[RocketChat][IN] skip duplicate message msg_id={message_id!r}"
                )
                return False

            # 标记为已处理
            seen[message_id] = now
            return True

    def _has_reply_to_self(self, components: list) -> bool:
        return any(
            isinstance(comp, Reply)
            and str(getattr(comp, "sender_id", "")) == str(self.adapter.user_id)
            for comp in components
        )

    async def _build_components_recursively(
        self,
        current_payload: dict,
        *,
        seen_quote_ids: set[str],
        current_depth: int = 0,
        max_depth: int = 3,
    ) -> list:
        chain = []
        if current_depth >= max_depth:
            return chain

        msg_text = current_payload.get("msg", "").strip()

        local_images = await self.adapter._media.extract_image_components(current_payload)
        local_recs = await self.adapter._media.extract_record_components(current_payload)
        local_vids = await self.adapter._media.extract_video_components(current_payload)
        local_files = await self.adapter._media.extract_file_components(current_payload)

        quote_ids = []

        for url_obj in current_payload.get("urls", []):
            u_str = url_obj.get("url") or ""
            p_url = url_obj.get("parsedUrl", {})
            if p_url and "query" in p_url and "msg" in p_url["query"]:
                q_id = p_url["query"]["msg"]
                if q_id and q_id not in quote_ids:
                    quote_ids.append(q_id)
                    logger.debug(f"[RocketChat][IN] 从urls.parsedUrl识别引用: msg_id={q_id}")
            elif "msg=" in u_str:
                parsed = urlparse(u_str)
                qs = parse_qs(parsed.query)
                if "msg" in qs:
                    q_id = qs["msg"][0]
                    if q_id and q_id not in quote_ids:
                        quote_ids.append(q_id)
                        logger.debug(
                            f"[RocketChat][IN] 从urls手动parse识别引用: msg_id={q_id} url={u_str[:80]}"
                        )

        att_raw = current_payload.get("attachments", [])
        direct_atts = [att_raw] if isinstance(att_raw, dict) else [a for a in att_raw if isinstance(a, dict)]
        for att in direct_atts:
            link = att.get("message_link") or ""
            if "msg=" in link:
                parsed = urlparse(link)
                qs = parse_qs(parsed.query)
                if "msg" in qs:
                    q_id = qs["msg"][0]
                    if q_id and q_id not in quote_ids:
                        quote_ids.append(q_id)
                        logger.debug(f"[RocketChat][IN] 从直接attachment.message_link识别引用: msg_id={q_id}")

        link_pattern = re.compile(
            r"\[([^\]]*)\]\(([^)]*msg=[^)]*)\)|"
            r"((?:https?|http)://[^\s)]+msg=[^\s)]*)",
            re.IGNORECASE,
        )

        if not quote_ids:
            for match in link_pattern.finditer(msg_text):
                markdown_url = match.group(2)
                direct_url = match.group(3)
                u_str = markdown_url or direct_url or ""
                if u_str and "msg=" in u_str:
                    u_clean = u_str.strip("[]() ")
                    parsed = urlparse(u_clean)
                    qs = parse_qs(parsed.query)
                    if "msg" in qs:
                        q_id = qs["msg"][0]
                        if q_id and q_id not in quote_ids:
                            quote_ids.append(q_id)

        found_reply_comps = []
        for q_id in quote_ids:
            if q_id in seen_quote_ids:
                continue
            seen_quote_ids.add(q_id)
            try:
                q_msg = await self.adapter._fetch_message_by_id(q_id)
                if not q_msg:
                    logger.debug(f"[RocketChat][IN] 被引用消息不存在或无权限访问: msgId={q_id}")
                    continue

                q_components = await self._build_components_recursively(
                    q_msg,
                    seen_quote_ids=seen_quote_ids,
                    current_depth=current_depth + 1,
                    max_depth=max_depth,
                )
                if not q_components:
                    continue

                q_sender_id = q_msg.get("u", {}).get("_id", "")
                q_sender_name = q_msg.get("u", {}).get("name") or q_msg.get("u", {}).get("username", "")
                q_ts_raw = q_msg.get("ts")
                if isinstance(q_ts_raw, dict):
                    q_timestamp = int(q_ts_raw.get("$date", time.time() * 1000) / 1000)
                else:
                    q_timestamp = int(time.time())

                q_msg_text = "".join([c.text for c in q_components if isinstance(c, Plain)]).strip()
                found_reply_comps.append(
                    Reply(
                        id=q_id,
                        chain=q_components,
                        sender_id=q_sender_id,
                        sender_nickname=q_sender_name,
                        time=q_timestamp,
                        message_str=q_msg_text,
                    )
                )
            except Exception as exc:
                logger.warning(f"[RocketChat][IN] 递归处理被引用消息出错: msgId={q_id} error={exc!r}")

        chain.extend(found_reply_comps)

        cleaned_msg_text = link_pattern.sub("", msg_text).strip()
        if cleaned_msg_text:
            chain.extend(self._build_text_components(current_payload, cleaned_msg_text))
            if current_depth == 0:
                logger.debug(f"[RocketChat][IN] 清理后文本: clean_text={cleaned_msg_text!r}")

        if local_images or local_recs or local_vids or local_files:
            logger.debug(
                f"[RocketChat][IN] 深度{current_depth}提取媒体: "
                f"images={len(local_images)} records={len(local_recs)} "
                f"videos={len(local_vids)} files={len(local_files)}"
            )
            chain.extend(local_images)
            chain.extend(local_recs)
            chain.extend(local_vids)
            chain.extend(local_files)

        return chain

    async def process_incoming_message(self, raw_msg: dict) -> None:
        try:
            logger.debug(
                f"[RocketChat][IN-RAW] msg_id={raw_msg.get('_id')} "
                f"sender={raw_msg.get('u', {}).get('username')} "
                f"room={raw_msg.get('rid')} "
                f"text_len={len(raw_msg.get('msg', ''))} "
                f"attachments={len(raw_msg.get('attachments', []))} "
                f"mentions={len(raw_msg.get('mentions', []))} "
                f"has_files={bool(raw_msg.get('files') or raw_msg.get('file'))} "
                f"has_urls={bool(raw_msg.get('urls'))} "
                f"is_thread={bool(raw_msg.get('tmid'))} "
                f"is_system={bool(raw_msg.get('t') and raw_msg.get('t') != 'e2e')}"
            )
            logger.debug(f"[RocketChat][IN-FULL] {json.dumps(raw_msg, ensure_ascii=False, default=str)}")

            raw_msg = await self.adapter._maybe_decrypt_incoming_message(raw_msg)
            if not raw_msg:
                logger.debug("[RocketChat][IN] skip undecipherable e2e message")
                return

            if raw_msg.get("t") and raw_msg.get("t") != "e2e":
                return

            if raw_msg.get("u", {}).get("_id") == self.adapter.user_id:
                logger.debug("[RocketChat][IN] skip self message")
                return

            components = await self._build_components_recursively(raw_msg, seen_quote_ids=set())
            msg_text = self._build_plain_text(components)

            if not msg_text and not [c for c in components if not isinstance(c, Plain)]:
                logger.debug("[RocketChat][IN] skip empty/unsupported message")
                return

            room_id: str = raw_msg.get("rid", "")
            sender_id: str = raw_msg.get("u", {}).get("_id", "")
            sender_username: str = raw_msg.get("u", {}).get("username", "")
            sender_name: str = raw_msg.get("u", {}).get("name") or sender_username
            thread_id: Optional[str] = raw_msg.get("tmid")

            ts_raw = raw_msg.get("ts")
            if isinstance(ts_raw, dict):
                timestamp = int(ts_raw.get("$date", time.time() * 1000) / 1000)
            else:
                timestamp = int(time.time())

            room_type = await self.adapter._get_room_type(room_id)
            msg_type = MessageType.FRIEND_MESSAGE if room_type == "d" else MessageType.GROUP_MESSAGE

            abm = AstrBotMessage()
            abm.type = msg_type
            abm.self_id = self.adapter.user_id
            abm.session_id = room_id
            abm.message_id = raw_msg.get("_id", "")
            abm.sender = MessageMember(user_id=sender_id, nickname=sender_name)
            abm.message = components
            abm.message_str = msg_text
            abm.raw_message = raw_msg
            abm.timestamp = timestamp
            abm.group = Group(group_id=room_id) if msg_type == MessageType.GROUP_MESSAGE else None

            bot_mentioned, has_other_user_mentions = self._classify_mentions(raw_msg)
            reply_to_self = self._has_reply_to_self(components)

            if not bot_mentioned and self.adapter.bot_username:
                bot_mentioned = f"@{self.adapter.bot_username}" in (abm.message_str or "")
                has_other_user_mentions = bool(has_other_user_mentions or "@" in (abm.message_str or ""))

            if bot_mentioned:
                logger.debug(f"[RocketChat][IN] bot mentioned, clean_text={abm.message_str!r}")

            has_current_non_text_payload = any(
                not isinstance(comp, (Plain, At, Reply)) for comp in abm.message
            )
            suppress_multi_mention_only_wake = (
                msg_type == MessageType.GROUP_MESSAGE
                and bot_mentioned
                and has_other_user_mentions
                and not abm.message_str
                and not has_current_non_text_payload
            )
            suppress_wake = suppress_multi_mention_only_wake
            if suppress_wake:
                logger.debug(
                    "[RocketChat][IN] suppress wake for multi-mention-only message "
                    f"room={room_id!r} msg_id={abm.message_id!r}"
                )

            is_thread_msg = bool(raw_msg.get("tmid"))
            should_quote = (
                msg_type == MessageType.GROUP_MESSAGE
                and bot_mentioned
                and not suppress_wake
                and not is_thread_msg
            )

            logger.debug(
                f"[RocketChat][IN] 回复场景判定: msg_type={msg_type} bot_mentioned={bot_mentioned} "
                f"is_thread={is_thread_msg} -> should_quote={should_quote}"
            )

            event = RocketChatMessageEvent(
                message_str=abm.message_str,
                message_obj=abm,
                platform_meta=self.adapter.meta(),
                session_id=abm.session_id,
                room_id=room_id,
                thread_id=thread_id,
                quote_original=should_quote,
                adapter=self.adapter,
            )

            if not await self._claim_incoming_message(raw_msg):
                return

            explicit_wake = (
                (bot_mentioned and not suppress_wake)
                or msg_type == MessageType.FRIEND_MESSAGE
            )
            reply_wake = msg_type == MessageType.GROUP_MESSAGE and reply_to_self
            should_wake_astrbot = explicit_wake or reply_wake
            if explicit_wake:
                event.is_at_or_wake_command = True

            if should_wake_astrbot:
                event.start_typing_indicator()

            logger.debug(
                "[RocketChat][IN] → commit type=%s room=%r msg=%r wake=%s"
                % (
                    "DM" if msg_type == MessageType.FRIEND_MESSAGE else "Group",
                    room_id,
                    (abm.message_str[:60] + "…") if len(abm.message_str) > 60 else abm.message_str,
                    event.is_at_or_wake_command,
                )
            )
            self.adapter.commit_event(event)
        except Exception as exc:
            logger.error(f"[RocketChat][IN] unhandled processing error: {exc!r}", exc_info=True)
            raise
