from __future__ import annotations

import unittest

from tests._bootstrap import install_astrbot_stubs

install_astrbot_stubs()

from astrbot_plugin_rocket_chat_adapter.rocketchat_sender import (  # noqa: E402
    RocketChatSenderBridge,
)


class _DummyWs:
    closed = False


class _DummyRealtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list, float]] = []

    async def ddp_call(self, method: str, params: list, timeout: float = 10.0) -> object:
        self.calls.append((method, params, timeout))
        return None


class _DummyAdapter:
    def __init__(self) -> None:
        self._ws = _DummyWs()
        self.bot_username = "rocketbot"
        self._realtime = _DummyRealtime()


class RocketChatTypingTests(unittest.IsolatedAsyncioTestCase):
    async def test_room_typing_payload_uses_user_activity_extras(self) -> None:
        adapter = _DummyAdapter()
        sender = RocketChatSenderBridge(adapter)

        await sender.send_typing("room-1", True)

        self.assertEqual(
            adapter._realtime.calls,
            [
                (
                    "stream-notify-room",
                    ["room-1/user-activity", "rocketbot", ["user-typing"], {}],
                    10.0,
                )
            ],
        )

    async def test_thread_typing_payload_includes_tmid_extra(self) -> None:
        adapter = _DummyAdapter()
        sender = RocketChatSenderBridge(adapter)

        await sender.send_typing("room-1", True, tmid="thread-1")
        await sender.send_typing("room-1", False, tmid="thread-1")

        self.assertEqual(
            adapter._realtime.calls,
            [
                (
                    "stream-notify-room",
                    [
                        "room-1/user-activity",
                        "rocketbot",
                        ["user-typing"],
                        {"tmid": "thread-1"},
                    ],
                    10.0,
                ),
                (
                    "stream-notify-room",
                    ["room-1/user-activity", "rocketbot", [], {"tmid": "thread-1"}],
                    10.0,
                ),
            ],
        )
