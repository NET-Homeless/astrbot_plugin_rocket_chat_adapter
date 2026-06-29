from __future__ import annotations

from typing import Any
import unittest

from tests._bootstrap import install_astrbot_stubs

install_astrbot_stubs()

from astrbot_plugin_rocket_chat_adapter.rocketchat_e2ee import (  # noqa: E402
    RocketChatE2EEManager,
)


class _DummyAdapter:
    user_id = "bot-id"

    async def _get_room_info(self, room_id: str) -> dict[str, Any]:
        return {
            "_id": room_id,
            "t": "p",
            "encrypted": True,
            "e2eKeyId": "key-id",
        }


class RocketChatE2EETests(unittest.IsolatedAsyncioTestCase):
    async def test_room_key_refresh_failure_skips_e2ee_message_without_raising(
        self,
    ) -> None:
        manager = RocketChatE2EEManager(
            adapter=_DummyAdapter(),
            enabled=True,
            password="secret",
        )
        manager.ready = True

        async def get_subscription(
            room_id: str, *, refresh: bool = False
        ) -> dict[str, Any]:
            raise RuntimeError("temporary subscription failure")

        manager._get_subscription = get_subscription  # type: ignore[method-assign]

        decrypted = await manager.maybe_decrypt_message(
            {"_id": "msg-1", "rid": "room-1", "t": "e2e", "content": {}}
        )

        self.assertIsNone(decrypted)


if __name__ == "__main__":
    unittest.main()
