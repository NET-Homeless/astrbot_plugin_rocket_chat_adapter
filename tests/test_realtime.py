from __future__ import annotations

import unittest
from typing import Any

from tests._bootstrap import install_astrbot_stubs

install_astrbot_stubs()

from astrbot_plugin_rocket_chat_adapter.rocketchat_realtime import (  # noqa: E402
    RocketChatRealtimeBridge,
)


class _DummyWs:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


class _DummyAdapter:
    def __init__(self) -> None:
        self.user_id = "bot-id"
        self._subscribed_rooms: set[str] = set()
        self._pending_room_subscriptions: dict[str, str] = {}
        self.cached_rooms: list[dict[str, Any]] = []

    async def _cache_room_info(self, room: dict[str, Any]) -> None:
        self.cached_rooms.append(room)


class RocketChatRealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_room_subscription_waits_for_ready_and_retries_after_nosub(self) -> None:
        adapter = _DummyAdapter()
        bridge = RocketChatRealtimeBridge(adapter)
        ws = _DummyWs()
        notification = {
            "fields": {
                "eventName": "bot-id/rooms-changed",
                "args": [
                    "inserted",
                    {"_id": "room-1", "t": "c", "name": "general"},
                ],
            }
        }

        await bridge.handle_user_notification(notification, ws)  # type: ignore[arg-type]

        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(adapter._pending_room_subscriptions, {"room-room-1": "room-1"})
        self.assertNotIn("room-1", adapter._subscribed_rooms)

        bridge._handle_nosub({"msg": "nosub", "id": "room-room-1", "error": {"reason": "denied"}})

        self.assertEqual(adapter._pending_room_subscriptions, {})
        self.assertNotIn("room-1", adapter._subscribed_rooms)

        await bridge.handle_user_notification(notification, ws)  # type: ignore[arg-type]
        bridge._handle_ready({"msg": "ready", "subs": ["room-room-1"]})

        self.assertEqual(len(ws.sent), 2)
        self.assertEqual(adapter._pending_room_subscriptions, {})
        self.assertIn("room-1", adapter._subscribed_rooms)


if __name__ == "__main__":
    unittest.main()
