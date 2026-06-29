from __future__ import annotations

import asyncio
import unittest

from tests._bootstrap import install_astrbot_stubs

install_astrbot_stubs()

from astrbot_plugin_rocket_chat_adapter.rocketchat_adapter import (  # noqa: E402
    RocketChatAdapter,
)


def _base_config() -> dict:
    return {
        "id": "rocket_chat",
        "server_url": "https://chat.example.com",
        "username": "bot",
        "password": "secret",
        "reconnect_delay": 5.0,
        "typing_indicator_delay": 0.5,
        "remote_media_max_size": 1024,
        "enable_e2ee": False,
        "e2ee_password": "",
    }


class RocketChatConfigValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_e2ee_without_password_does_not_block_adapter_initialization(
        self,
    ) -> None:
        config = _base_config()
        config["enable_e2ee"] = True

        adapter = RocketChatAdapter(config, {}, asyncio.Queue())

        self.assertTrue(adapter.enable_e2ee)
        self.assertEqual(adapter.e2ee_password, "")
        self.assertFalse(adapter._e2ee.ready)

    async def test_missing_login_password_still_fails_fast(self) -> None:
        config = _base_config()
        config["password"] = ""

        with self.assertRaises(ValueError):
            RocketChatAdapter(config, {}, asyncio.Queue())

    async def test_server_url_without_http_scheme_fails_fast(self) -> None:
        config = _base_config()
        config["server_url"] = "chat.example.com"

        with self.assertRaises(ValueError):
            RocketChatAdapter(config, {}, asyncio.Queue())

    async def test_server_url_with_unsupported_scheme_fails_fast(self) -> None:
        config = _base_config()
        config["server_url"] = "ftp://chat.example.com"

        with self.assertRaises(ValueError):
            RocketChatAdapter(config, {}, asyncio.Queue())


if __name__ == "__main__":
    unittest.main()
