from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

from tests._bootstrap import install_astrbot_stubs

install_astrbot_stubs()

from astrbot_plugin_rocket_chat_adapter.rocketchat_media import (  # noqa: E402
    RocketChatMediaBridge,
)


class _DummyAdapter:
    server_url = "https://chat.example.com"
    auth_token = "token"
    user_id = "user-id"

    def _get_auth_headers(self) -> dict[str, str]:
        return {
            "X-Auth-Token": self.auth_token,
            "X-User-Id": self.user_id,
        }


class RocketChatPlainUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        fd, self.file_path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "wb") as fp:
            fp.write(b"hello")
        self.addCleanup(lambda: os.path.exists(self.file_path) and os.unlink(self.file_path))

    async def test_plain_upload_uses_rooms_media_then_confirm(self) -> None:
        bridge = RocketChatMediaBridge(_DummyAdapter())
        calls: list[tuple[str, str, Any]] = []

        async def post_multipart_json_response(url: str, form: object) -> tuple[int, dict]:
            calls.append(("multipart", url, form))
            return 200, {"success": True, "file": {"_id": "file-1", "url": "/file-upload/file-1/test.txt"}}

        async def post_json_response(url: str, payload: dict[str, Any]) -> tuple[int, dict]:
            calls.append(("json", url, payload))
            return 200, {"success": True, "message": {"_id": "msg-1"}}

        bridge.post_multipart_json_response = post_multipart_json_response  # type: ignore[method-assign]
        bridge.post_json_response = post_json_response  # type: ignore[method-assign]

        uploaded = await bridge.upload_plain_file(
            "room-1",
            self.file_path,
            "test.txt",
            description="desc",
            tmid="thread-1",
        )

        self.assertTrue(uploaded)
        self.assertEqual(calls[0][0], "multipart")
        self.assertEqual(calls[0][1], "https://chat.example.com/api/v1/rooms.media/room-1")
        self.assertEqual(calls[1][0], "json")
        self.assertEqual(
            calls[1][1],
            "https://chat.example.com/api/v1/rooms.mediaConfirm/room-1/file-1",
        )
        self.assertEqual(calls[1][2], {"description": "desc", "tmid": "thread-1"})

    async def test_plain_upload_falls_back_when_rooms_media_endpoint_is_missing(self) -> None:
        bridge = RocketChatMediaBridge(_DummyAdapter())
        fallback_calls: list[tuple[str, str, str, str, str | None]] = []
        new_endpoint_calls = 0

        async def post_multipart_json_response(url: str, form: object) -> tuple[int, dict]:
            nonlocal new_endpoint_calls
            new_endpoint_calls += 1
            return 404, {"success": False, "errorType": "error-endpoint-not-found"}

        async def upload_legacy_plain_file(
            room_id: str,
            file_path: str,
            resolved_name: str,
            description: str = "",
            tmid: str | None = None,
        ) -> bool:
            fallback_calls.append((room_id, file_path, resolved_name, description, tmid))
            return True

        bridge.post_multipart_json_response = post_multipart_json_response  # type: ignore[method-assign]
        bridge.upload_legacy_plain_file = upload_legacy_plain_file  # type: ignore[method-assign]

        uploaded = await bridge.upload_plain_file(
            "room-1",
            self.file_path,
            "test.txt",
            description="desc",
            tmid="thread-1",
        )

        self.assertTrue(uploaded)
        self.assertEqual(
            fallback_calls,
            [("room-1", self.file_path, "test.txt", "desc", "thread-1")],
        )
        self.assertEqual(new_endpoint_calls, 1)

        uploaded = await bridge.upload_plain_file("room-1", self.file_path, "test.txt")

        self.assertTrue(uploaded)
        self.assertEqual(new_endpoint_calls, 1)

    async def test_plain_upload_does_not_fallback_on_validation_error(self) -> None:
        bridge = RocketChatMediaBridge(_DummyAdapter())

        async def post_multipart_json_response(url: str, form: object) -> tuple[int, dict]:
            return 400, {"success": False, "errorType": "error-invalid-file-type"}

        async def upload_legacy_plain_file(*args: object, **kwargs: object) -> bool:
            raise AssertionError("legacy upload should not be called")

        bridge.post_multipart_json_response = post_multipart_json_response  # type: ignore[method-assign]
        bridge.upload_legacy_plain_file = upload_legacy_plain_file  # type: ignore[method-assign]

        uploaded = await bridge.upload_plain_file("room-1", self.file_path, "test.txt")

        self.assertFalse(uploaded)


if __name__ == "__main__":
    unittest.main()
