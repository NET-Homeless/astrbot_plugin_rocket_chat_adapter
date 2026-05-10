from __future__ import annotations

import unittest

from tests._bootstrap import install_astrbot_stubs

install_astrbot_stubs()

from astrbot.api.message_components import At  # noqa: E402
from astrbot.api.platform import MessageType, PlatformMetadata  # noqa: E402
from astrbot_plugin_rocket_chat_adapter import rocketchat_event  # noqa: E402
from astrbot_plugin_rocket_chat_adapter.rocketchat_inbound import (  # noqa: E402
    RocketChatInboundBridge,
)


class _DummyMedia:
    async def extract_image_components(self, raw_msg: dict) -> list:
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

    async def _maybe_decrypt_incoming_message(self, raw_msg: dict) -> dict:
        return raw_msg

    async def _get_room_type(self, room_id: str) -> str:
        return "p"

    async def _fetch_message_by_id(self, msg_id: str) -> None:
        return None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="rocket_chat",
            description="Rocket.Chat",
            id="rocket_chat",
            support_streaming_message=False,
        )

    def commit_event(self, event: object) -> None:
        self._committed_events.append(event)


class RocketChatMentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        rocketchat_event.RocketChatMessageEvent.start_typing_indicator = lambda self: None
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


if __name__ == "__main__":
    unittest.main()
