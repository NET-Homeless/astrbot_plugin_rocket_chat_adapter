from __future__ import annotations

import unittest

from tests._bootstrap import install_astrbot_stubs

install_astrbot_stubs()

from astrbot.api.message_components import At, Image, Reply  # noqa: E402
from astrbot.api.platform import MessageType, PlatformMetadata  # noqa: E402
from astrbot_plugin_rocket_chat_adapter import rocketchat_event  # noqa: E402
from astrbot_plugin_rocket_chat_adapter.rocketchat_inbound import (  # noqa: E402
    RocketChatInboundBridge,
)


class _DummyMedia:
    async def extract_image_components(self, raw_msg: dict) -> list:
        if raw_msg.get("_id") == "bot-image":
            return [Image.fromFileSystem("/tmp/bot-image.jpg")]
        return []

    async def extract_record_components(self, raw_msg: dict) -> list:
        return []

    async def extract_video_components(self, raw_msg: dict) -> list:
        return []

    async def extract_file_components(self, raw_msg: dict) -> list:
        return []


class _DummyAdapter:
    def __init__(self) -> None:
        self.user_id = "bot-id"
        self.bot_username = "bot"
        self._media = _DummyMedia()
        self._committed_events: list = []
        self._fetched_messages: dict[str, dict] = {}
        self._processed_message_ids: dict[str, float] = {}

    async def _maybe_decrypt_incoming_message(self, raw_msg: dict) -> dict:
        decrypted = raw_msg.get("_decrypted")
        if raw_msg.get("t") == "e2e" and isinstance(decrypted, dict):
            merged = dict(raw_msg)
            merged.update(decrypted)
            merged["e2e"] = "done"
            return merged
        return raw_msg

    async def _get_room_type(self, room_id: str) -> str:
        return "p"

    async def _fetch_message_by_id(self, msg_id: str) -> dict | None:
        return self._fetched_messages.get(msg_id)

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="rocket_chat",
            description="Rocket.Chat",
            id="rocket_chat",
            support_streaming_message=False,
        )

    def commit_event(self, event: object) -> None:
        self._committed_events.append(event)


def _would_astrbot_reply_wake(event: object) -> bool:
    return any(
        isinstance(comp, Reply)
        and str(comp.sender_id) == str(event.message_obj.self_id)
        for comp in event.message_obj.message
    )


class RocketChatMentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        original_start_typing_indicator = (
            rocketchat_event.RocketChatMessageEvent.start_typing_indicator
        )
        self.addCleanup(
            setattr,
            rocketchat_event.RocketChatMessageEvent,
            "start_typing_indicator",
            original_start_typing_indicator,
        )
        self.started_typing_events: list = []

        def record_typing_start(event: object) -> None:
            self.started_typing_events.append(event)

        rocketchat_event.RocketChatMessageEvent.start_typing_indicator = record_typing_start
        self.adapter = _DummyAdapter()
        self.bridge = RocketChatInboundBridge(self.adapter)

    async def test_multi_mention_only_message_does_not_wake_bot(self) -> None:
        raw_msg = {
            "_id": "msg-1",
            "rid": "room-1",
            "msg": "@alice @bot",
            "u": {"_id": "user-1", "username": "carol", "name": "Carol"},
            "mentions": [
                {"_id": "user-a", "username": "alice", "name": "Alice"},
                {"_id": "bot-id", "username": "bot", "name": "Bot"},
            ],
            "attachments": [],
            "urls": [],
        }

        await self.bridge.process_incoming_message(raw_msg)

        self.assertEqual(len(self.adapter._committed_events), 1)
        event = self.adapter._committed_events[0]
        self.assertFalse(event.is_at_or_wake_command)
        self.assertEqual(event.message_str, "")
        self.assertTrue(all(isinstance(comp, At) for comp in event.message_obj.message))
        self.assertEqual(self.started_typing_events, [])

    async def test_multi_mention_with_real_text_keeps_wake_and_strips_mentions(self) -> None:
        raw_msg = {
            "_id": "msg-2",
            "rid": "room-1",
            "msg": "@alice @bot 帮我看看",
            "u": {"_id": "user-2", "username": "dave", "name": "Dave"},
            "mentions": [
                {"_id": "user-a", "username": "alice", "name": "Alice"},
                {"_id": "bot-id", "username": "bot", "name": "Bot"},
            ],
            "attachments": [],
            "urls": [],
        }

        await self.bridge.process_incoming_message(raw_msg)

        self.assertEqual(len(self.adapter._committed_events), 1)
        event = self.adapter._committed_events[0]
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.message_str, "帮我看看")
        self.assertEqual(event.message_obj.type, MessageType.GROUP_MESSAGE)
        self.assertEqual(self.started_typing_events, [event])

    async def test_quote_bot_image_and_mention_other_user_preserves_reply_wake(self) -> None:
        self.adapter._fetched_messages["bot-image"] = {
            "_id": "bot-image",
            "rid": "room-1",
            "msg": "",
            "u": {"_id": "bot-id", "username": "bot", "name": "Bot"},
            "mentions": [],
            "attachments": [],
            "urls": [],
        }
        raw_msg = {
            "_id": "msg-3",
            "rid": "room-1",
            "msg": "@alice",
            "u": {"_id": "user-3", "username": "erin", "name": "Erin"},
            "mentions": [
                {"_id": "user-a", "username": "alice", "name": "Alice"},
            ],
            "attachments": [
                {
                    "message_link": (
                        "https://chat.example.com/group/general?msg=bot-image"
                    ),
                },
            ],
            "urls": [],
        }

        await self.bridge.process_incoming_message(raw_msg)

        self.assertEqual(len(self.adapter._committed_events), 1)
        event = self.adapter._committed_events[0]
        self.assertFalse(event.is_at_or_wake_command)
        self.assertTrue(_would_astrbot_reply_wake(event))
        self.assertEqual(event.message_str, "")
        self.assertTrue(any(isinstance(comp, At) for comp in event.message_obj.message))
        self.assertTrue(
            any(isinstance(comp, Reply) for comp in event.message_obj.message)
        )
        self.assertEqual(self.started_typing_events, [event])

    async def test_quote_bot_image_and_mention_other_user_with_text_preserves_reply_wake(self) -> None:
        self.adapter._fetched_messages["bot-image"] = {
            "_id": "bot-image",
            "rid": "room-1",
            "msg": "",
            "u": {"_id": "bot-id", "username": "bot", "name": "Bot"},
            "mentions": [],
            "attachments": [],
            "urls": [],
        }
        raw_msg = {
            "_id": "msg-4",
            "rid": "room-1",
            "msg": "@alice 这张图怎么样",
            "u": {"_id": "user-4", "username": "frank", "name": "Frank"},
            "mentions": [
                {"_id": "user-a", "username": "alice", "name": "Alice"},
            ],
            "attachments": [
                {
                    "message_link": (
                        "https://chat.example.com/group/general?msg=bot-image"
                    ),
                },
            ],
            "urls": [],
        }

        await self.bridge.process_incoming_message(raw_msg)

        self.assertEqual(len(self.adapter._committed_events), 1)
        event = self.adapter._committed_events[0]
        self.assertFalse(event.is_at_or_wake_command)
        self.assertTrue(_would_astrbot_reply_wake(event))
        self.assertEqual(event.message_str, "这张图怎么样")
        self.assertTrue(
            any(isinstance(comp, Reply) for comp in event.message_obj.message)
        )
        self.assertEqual(self.started_typing_events, [event])

    async def test_quote_bot_image_without_bot_mention_preserves_reply_wake(self) -> None:
        self.adapter._fetched_messages["bot-image"] = {
            "_id": "bot-image",
            "rid": "room-1",
            "msg": "",
            "u": {"_id": "bot-id", "username": "bot", "name": "Bot"},
            "mentions": [],
            "attachments": [],
            "urls": [],
        }
        raw_msg = {
            "_id": "msg-5",
            "rid": "room-1",
            "msg": "这张图不错",
            "u": {"_id": "user-5", "username": "grace", "name": "Grace"},
            "mentions": [],
            "attachments": [
                {
                    "message_link": (
                        "https://chat.example.com/group/general?msg=bot-image"
                    ),
                },
            ],
            "urls": [],
        }

        await self.bridge.process_incoming_message(raw_msg)

        self.assertEqual(len(self.adapter._committed_events), 1)
        event = self.adapter._committed_events[0]
        self.assertFalse(event.is_at_or_wake_command)
        self.assertTrue(_would_astrbot_reply_wake(event))
        self.assertEqual(event.message_str, "这张图不错")
        self.assertTrue(
            any(isinstance(comp, Reply) for comp in event.message_obj.message)
        )
        self.assertEqual(self.started_typing_events, [event])

    async def test_duplicate_quote_bot_image_delivery_is_processed_once(self) -> None:
        self.adapter._fetched_messages["bot-image"] = {
            "_id": "bot-image",
            "rid": "room-1",
            "msg": "",
            "u": {"_id": "bot-id", "username": "bot", "name": "Bot"},
            "mentions": [],
            "attachments": [],
            "urls": [],
        }
        raw_msg = {
            "_id": "msg-6",
            "rid": "room-1",
            "msg": "@alice 这张图怎么样",
            "u": {"_id": "user-6", "username": "heidi", "name": "Heidi"},
            "mentions": [
                {"_id": "user-a", "username": "alice", "name": "Alice"},
            ],
            "attachments": [
                {
                    "message_link": (
                        "https://chat.example.com/group/general?msg=bot-image"
                    ),
                },
            ],
            "urls": [],
        }

        await self.bridge.process_incoming_message(raw_msg)
        await self.bridge.process_incoming_message(
            {**raw_msg, "_updatedAt": {"$date": 1776599831000}}
        )
        await self.bridge.process_incoming_message(
            {**raw_msg, "urls": [{"url": "https://example.com/preview"}]}
        )
        await self.bridge.process_incoming_message(dict(raw_msg))

        self.assertEqual(len(self.adapter._committed_events), 1)
        event = self.adapter._committed_events[0]
        self.assertFalse(event.is_at_or_wake_command)
        self.assertTrue(_would_astrbot_reply_wake(event))
        self.assertEqual(event.message_str, "这张图怎么样")
        self.assertTrue(
            any(isinstance(comp, Reply) for comp in event.message_obj.message)
        )
        self.assertEqual(self.started_typing_events, [event])

    async def test_empty_delivery_does_not_block_later_content_update(self) -> None:
        raw_msg = {
            "_id": "msg-empty-then-update",
            "rid": "room-1",
            "msg": "",
            "u": {"_id": "user-7", "username": "ivan", "name": "Ivan"},
            "mentions": [],
            "attachments": [],
            "urls": [],
        }

        await self.bridge.process_incoming_message(raw_msg)
        await self.bridge.process_incoming_message(
            {
                **raw_msg,
                "msg": "@bot ping",
                "mentions": [
                    {"_id": "bot-id", "username": "bot", "name": "Bot"},
                ],
            }
        )

        self.assertEqual(len(self.adapter._committed_events), 1)
        event = self.adapter._committed_events[0]
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.message_str, "ping")
        self.assertEqual(self.started_typing_events, [event])

    async def test_duplicate_ddp_delivery_is_processed_once(self) -> None:
        raw_msg = {
            "_id": "msg-7",
            "rid": "room-1",
            "msg": "@bot 帮我看看",
            "u": {"_id": "user-7", "username": "ivan", "name": "Ivan"},
            "mentions": [
                {"_id": "bot-id", "username": "bot", "name": "Bot"},
            ],
            "attachments": [],
            "urls": [],
        }

        await self.bridge.process_incoming_message(raw_msg)
        await self.bridge.process_incoming_message(dict(raw_msg))

        self.assertEqual(len(self.adapter._committed_events), 1)
        event = self.adapter._committed_events[0]
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.message_str, "帮我看看")
        self.assertEqual(self.started_typing_events, [event])

    async def test_duplicate_e2ee_delivery_is_processed_once(self) -> None:
        raw_msg = {
            "_id": "msg-e2ee-1",
            "rid": "room-1",
            "msg": "",
            "t": "e2e",
            "u": {"_id": "user-8", "username": "judy", "name": "Judy"},
            "mentions": [],
            "attachments": [],
            "urls": [],
            "_decrypted": {
                "msg": "@bot 加密消息",
                "mentions": [
                    {"_id": "bot-id", "username": "bot", "name": "Bot"},
                ],
            },
        }

        await self.bridge.process_incoming_message(raw_msg)
        await self.bridge.process_incoming_message(
            {**raw_msg, "_updatedAt": {"$date": 1776599831000}}
        )
        await self.bridge.process_incoming_message(dict(raw_msg))

        self.assertEqual(len(self.adapter._committed_events), 1)
        event = self.adapter._committed_events[0]
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.message_str, "加密消息")
        self.assertEqual(self.started_typing_events, [event])


if __name__ == "__main__":
    unittest.main()
