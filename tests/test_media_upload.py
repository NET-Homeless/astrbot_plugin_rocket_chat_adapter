from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import dataclass
from typing import Any

from tests._bootstrap import install_astrbot_stubs

install_astrbot_stubs()

from astrbot_plugin_rocket_chat_adapter.rocketchat_media import (  # noqa: E402
    RocketChatMediaBridge,
)


@dataclass
class _DummyEncryptedUpload:
    encrypted_name: str = "encrypted-name"
    encrypted_bytes: bytes = b"encrypted-bytes"


class _DummyE2EE:
    def __init__(self) -> None:
        self.upload = _DummyEncryptedUpload()
        self.prepare_calls: list[dict[str, Any]] = []
        self.confirm_calls: list[dict[str, Any]] = []

    async def prepare_encrypted_upload(
        self,
        room_id: str,
        *,
        file_name: str,
        mime_type: str,
        file_bytes: bytes,
    ) -> _DummyEncryptedUpload:
        self.prepare_calls.append(
            {
                "room_id": room_id,
                "file_name": file_name,
                "mime_type": mime_type,
                "file_bytes": file_bytes,
            }
        )
        return self.upload

    async def build_upload_file_content(
        self,
        room_id: str,
        upload: _DummyEncryptedUpload,
    ) -> dict[str, Any]:
        return {"encrypted": {"algorithm": "rc.v2.aes-sha2", "ciphertext": "metadata"}}

    async def build_media_confirm_payload(
        self,
        room_id: str,
        *,
        upload_id: str,
        upload_url: str,
        upload: _DummyEncryptedUpload,
        text: str = "",
        tmid: str | None = None,
    ) -> dict[str, Any]:
        self.confirm_calls.append(
            {
                "room_id": room_id,
                "upload_id": upload_id,
                "upload_url": upload_url,
                "upload": upload,
                "text": text,
                "tmid": tmid,
            }
        )
        return {
            "t": "e2e",
            "content": {"algorithm": "rc.v2.aes-sha2", "ciphertext": "message"},
            "tmid": tmid,
        }


class _DummyAdapter:
    server_url = "https://chat.example.com"
    auth_token = "token"
    user_id = "user-id"

    def __init__(self) -> None:
        self._e2ee = _DummyE2EE()
        self.posted_json: list[tuple[str, dict[str, Any]]] = []

    def _get_auth_headers(self) -> dict[str, str]:
        return {
            "X-Auth-Token": self.auth_token,
            "X-User-Id": self.user_id,
        }

    def _is_unknown_room_info(self, room_info: dict[str, Any]) -> bool:
        return bool(room_info.get("_unknown"))

    def _is_e2ee_room_info(self, room_info: dict[str, Any]) -> bool:
        return bool(room_info.get("encrypted") and room_info.get("t") in {"d", "p"})

    async def _get_room_info(self, room_id: str) -> dict[str, Any]:
        return {"_id": room_id, "t": "c", "encrypted": False}

    async def _post_json_message(self, url: str, payload: dict[str, Any]) -> bool:
        self.posted_json.append((url, payload))
        return True


class _UnknownRoomAdapter(_DummyAdapter):
    async def _get_room_info(self, room_id: str) -> dict[str, Any]:
        return {"_id": room_id, "t": None, "encrypted": None, "_unknown": True}


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
        self.assertEqual(calls[1][2], {"msg": "desc", "tmid": "thread-1"})

    async def test_plain_upload_does_not_fallback_when_rooms_media_endpoint_is_missing(self) -> None:
        bridge = RocketChatMediaBridge(_DummyAdapter())
        calls = 0

        async def post_multipart_json_response(url: str, form: object) -> tuple[int, dict]:
            nonlocal calls
            calls += 1
            return 404, {"success": False, "errorType": "error-endpoint-not-found"}

        async def post_json_response(url: str, payload: dict[str, Any]) -> tuple[int, dict]:
            raise AssertionError("mediaConfirm should not be called after upload failure")

        bridge.post_multipart_json_response = post_multipart_json_response  # type: ignore[method-assign]
        bridge.post_json_response = post_json_response  # type: ignore[method-assign]

        uploaded = await bridge.upload_plain_file(
            "room-1",
            self.file_path,
            "test.txt",
            description="desc",
            tmid="thread-1",
        )

        self.assertFalse(uploaded)
        self.assertEqual(calls, 1)

    async def test_plain_upload_does_not_fallback_on_validation_error(self) -> None:
        bridge = RocketChatMediaBridge(_DummyAdapter())

        async def post_multipart_json_response(url: str, form: object) -> tuple[int, dict]:
            return 400, {"success": False, "errorType": "error-invalid-file-type"}

        bridge.post_multipart_json_response = post_multipart_json_response  # type: ignore[method-assign]

        uploaded = await bridge.upload_plain_file("room-1", self.file_path, "test.txt")

        self.assertFalse(uploaded)

    async def test_upload_local_file_refuses_unknown_room_info(self) -> None:
        bridge = RocketChatMediaBridge(_UnknownRoomAdapter())

        async def upload_plain_file(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("unknown room must not use plaintext upload")

        async def upload_encrypted_file(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("unknown room must not use encrypted upload")

        bridge.upload_plain_file = upload_plain_file  # type: ignore[method-assign]
        bridge.upload_encrypted_file = upload_encrypted_file  # type: ignore[method-assign]

        uploaded = await bridge.upload_local_file("room-1", self.file_path, "test.txt")

        self.assertFalse(uploaded)

    async def test_encrypted_upload_uses_rooms_media_then_confirm(self) -> None:
        adapter = _DummyAdapter()
        bridge = RocketChatMediaBridge(adapter)
        calls: list[tuple[str, str, Any]] = []

        async def post_multipart_json(url: str, form: object) -> dict[str, Any]:
            calls.append(("multipart", url, form))
            return {
                "success": True,
                "file": {"_id": "file-1", "url": "/file-upload/file-1/encrypted-name"},
            }

        bridge.post_multipart_json = post_multipart_json  # type: ignore[method-assign]

        uploaded = await bridge.upload_encrypted_file(
            "encrypted-room",
            self.file_path,
            "test.txt",
            description="caption",
            tmid="thread-1",
        )

        self.assertTrue(uploaded)
        self.assertEqual(adapter._e2ee.prepare_calls[0]["room_id"], "encrypted-room")
        self.assertEqual(adapter._e2ee.prepare_calls[0]["file_name"], "test.txt")
        self.assertEqual(adapter._e2ee.prepare_calls[0]["file_bytes"], b"hello")
        self.assertEqual(calls[0][0], "multipart")
        self.assertEqual(
            calls[0][1],
            "https://chat.example.com/api/v1/rooms.media/encrypted-room",
        )
        self.assertEqual(
            adapter._e2ee.confirm_calls,
            [
                {
                    "room_id": "encrypted-room",
                    "upload_id": "file-1",
                    "upload_url": "/file-upload/file-1/encrypted-name",
                    "upload": adapter._e2ee.upload,
                    "text": "caption",
                    "tmid": "thread-1",
                }
            ],
        )
        self.assertEqual(
            adapter.posted_json,
            [
                (
                    "https://chat.example.com/api/v1/rooms.mediaConfirm/encrypted-room/file-1",
                    {
                        "t": "e2e",
                        "content": {
                            "algorithm": "rc.v2.aes-sha2",
                            "ciphertext": "message",
                        },
                        "tmid": "thread-1",
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
