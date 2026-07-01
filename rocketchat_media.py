from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import tempfile
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger
from astrbot.api.message_components import File, Image, Record, Video

if TYPE_CHECKING:
    from collections.abc import Callable

# ============================================================================
# 媒体下载配置常量
# ============================================================================

MEDIA_DOWNLOAD_TOTAL_TIMEOUT = 30  # 秒
MEDIA_DOWNLOAD_CONNECT_TIMEOUT = 10  # 秒


def _is_http_url(file_ref: str) -> bool:
    return file_ref.startswith("http://") or file_ref.startswith("https://")


def _normalize_local_file_path(file_ref: str) -> str | None:
    """将本地文件引用归一化为绝对路径。

    处理 file:// 前缀，并将相对路径解析为相对当前工作目录的绝对路径。
    部分插件（如生图插件）只产出裸相对路径（无 file://、非绝对），
    直接交给 open()/upload 会触发 FileNotFoundError，统一在此收口。
    """
    local_path = file_ref.replace("file:///", "").replace("file://", "")
    if not local_path:
        return None
    if not os.path.isabs(local_path):
        local_path = os.path.abspath(local_path)
    return local_path


async def _resolve_image_upload_path(
    adapter: Any,
    file_ref: str,
) -> tuple[str | None, Callable[[], None] | None]:
    """将图片引用解析为可上传的本地路径，统一处理 http / base64 / file 三类。"""
    if _is_http_url(file_ref):
        return await adapter._download_remote_media(file_ref, ".png")
    if file_ref.startswith("base64://"):
        return adapter._decode_base64_media(file_ref, ".png")
    return (_normalize_local_file_path(file_ref), None)


def _guess_upload_filename(
    file_ref: str,
    local_path: str,
    fallback: str = "image.png",
) -> str:
    if file_ref.startswith("base64://"):
        return fallback
    candidate = os.path.basename(urlparse(file_ref).path)
    if candidate:
        return candidate
    return os.path.basename(local_path) or fallback


async def upload_combined_images(
    adapter: Any,
    room_id: str,
    text: str,
    images: list[Image],
    tmid: str | None = None,
) -> bool:
    """
    将文本与一组图片合并为 Rocket.Chat 媒体消息发送。

    首张图片通过 rooms.media + rooms.mediaConfirm(msg=...) 携带 text 作为 caption，
    后续图片独立确认生成消息。远端图片优先下载到本地再上传（规避防盗链）；下载失败时
    降级为 URL 引用发送，避免丢图。base64:// / file:// 引用均解析为本地文件后上传。

    返回 text 是否已随某张图片一起发出；调用方据此决定是否再单独补发文本。
    """
    sent_text_with_image = False
    cleanups: list[Callable[[], None]] = []
    try:
        for img in images:
            file_ref: str = img.file or getattr(img, "url", None) or ""
            if not file_ref:
                continue

            local_path, cleanup = await _resolve_image_upload_path(adapter, file_ref)
            if cleanup:
                cleanups.append(cleanup)

            if not local_path:
                if _is_http_url(file_ref):
                    caption = text if not sent_text_with_image else ""
                    if await adapter._media.send_image_url(
                        room_id,
                        file_ref,
                        text=caption,
                        tmid=tmid,
                    ):
                        sent_text_with_image = sent_text_with_image or bool(caption)
                continue

            caption = text if not sent_text_with_image else ""
            uploaded = await adapter._media.upload_local_file(
                room_id,
                local_path,
                _guess_upload_filename(file_ref, local_path),
                description=caption,
                tmid=tmid,
            )
            if uploaded:
                sent_text_with_image = sent_text_with_image or bool(caption)
    finally:
        for cleanup in cleanups:
            cleanup()
    return sent_text_with_image


class RocketChatMediaBridge:
    """Rocket.Chat media send/receive helpers, including E2EE file flows."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    @staticmethod
    def normalize_local_file_path(file_ref: str) -> str | None:
        """桥接公开入口：将本地文件引用归一化为绝对路径。

        供 sender/event 等模块统一调用，避免各自重复 strip file:// 的散点逻辑。
        """
        return _normalize_local_file_path(file_ref)

    def classify_file_kind(self, file_obj: dict) -> str:
        candidates: list[str] = []

        for key in (
            "type",
            "mimeType",
            "contentType",
            "image_type",
            "audio_type",
            "video_type",
        ):
            value = file_obj.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)

        for key in (
            "name",
            "title",
            "url",
            "path",
            "title_link",
            "titleLink",
            "link",
            "image_url",
            "imageUrl",
            "audio_url",
            "audioUrl",
            "video_url",
            "videoUrl",
        ):
            value = file_obj.get(key)
            if not isinstance(value, str) or not value:
                continue
            guessed, _ = mimetypes.guess_type(value.split("?", 1)[0])
            if guessed:
                candidates.append(guessed)

        for candidate in candidates:
            if candidate.startswith("image/"):
                return "image"
            if candidate.startswith("audio/"):
                return "audio"
            if candidate.startswith("video/"):
                return "video"

        return "file"

    def get_all_attachments_recursive(
        self,
        payload: dict,
        *,
        skip_quote_attachments: bool = False,
    ) -> list[dict]:
        res = []
        att_raw = payload.get("attachments", [])
        atts = (
            [att_raw]
            if isinstance(att_raw, dict)
            else [a for a in att_raw if isinstance(a, dict)]
        )
        for att in atts:
            if skip_quote_attachments and att.get("message_link"):
                continue
            res.append(att)
            res.extend(
                self.get_all_attachments_recursive(
                    att,
                    skip_quote_attachments=skip_quote_attachments,
                )
            )
        return res

    def _is_encrypted_media_attachment(self, file_obj: dict) -> bool:
        encryption = file_obj.get("encryption")
        return (
            isinstance(encryption, dict)
            and isinstance(encryption.get("key"), dict)
            and isinstance(encryption.get("iv"), str)
            and bool(encryption.get("iv"))
        )

    async def download_remote_bytes(self, url: str) -> bytes | None:
        """
        下载远程媒体字节数据。

        对于 Rocket.Chat 服务器的 URL，自动添加认证 Header。

        Args:
            url: 媒体 URL

        Returns:
            下载的字节数据，失败返回 None
        """
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            logger.warning(f"[RocketChat] 拒绝下载不支持的媒体协议: {url}")
            return None

        # 指向本服务器的媒体需要带上认证 Header；外部链接不带
        headers = (
            self.adapter._get_auth_headers()
            if self.adapter._is_own_server_url(url)
            else {}
        )

        try:
            async with self.adapter._get_http_session().get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=MEDIA_DOWNLOAD_TOTAL_TIMEOUT,
                    connect=MEDIA_DOWNLOAD_CONNECT_TIMEOUT,
                ),
                allow_redirects=True,
                max_redirects=3,
            ) as resp:
                if resp.status >= 400:
                    logger.error(f"[RocketChat] 下载媒体失败 {resp.status}: {url}")
                    return None

                content_length = resp.content_length
                if (
                    content_length is not None
                    and content_length > self.adapter.remote_media_max_size
                ):
                    logger.error(
                        f"[RocketChat] 下载媒体失败，文件过大: {content_length} > {self.adapter.remote_media_max_size} ({url})"
                    )
                    return None

                raw = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > self.adapter.remote_media_max_size:
                        logger.error(
                            f"[RocketChat] 下载媒体失败，文件超过限制: {len(raw)} > {self.adapter.remote_media_max_size} ({url})"
                        )
                        return None
                return bytes(raw)
        except Exception as exc:
            logger.error(f"[RocketChat] 下载媒体异常: {exc!r}")
            return None

    def _guess_media_suffix(
        self,
        file_obj: dict,
        media_url: str,
        default_suffix: str,
    ) -> str:
        for candidate in (
            file_obj.get("name"),
            file_obj.get("title"),
            media_url,
        ):
            if not isinstance(candidate, str) or not candidate:
                continue
            parsed = urlparse(candidate)
            _, ext = os.path.splitext(parsed.path or candidate)
            if ext:
                return ext

        for key in (
            "type",
            "mimeType",
            "contentType",
            "image_type",
            "audio_type",
            "video_type",
        ):
            mime_value = file_obj.get(key)
            if not isinstance(mime_value, str) or not mime_value:
                continue
            ext = mimetypes.guess_extension(mime_value.split(";", 1)[0].strip())
            if ext:
                return ext

        return default_suffix

    def _write_temp_media_file(self, raw: bytes, suffix: str) -> str:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            return tmp.name

    async def _select_media_url(
        self,
        file_obj: dict,
        target_kind: str,
    ) -> str | None:
        key_candidates: dict[str, tuple[str, ...]] = {
            "image": (
                "image_url",
                "imageUrl",
                "image",
                "thumb_url",
                "thumbUrl",
                "image_preview",
                "imagePreview",
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
            "audio": (
                "audio_url",
                "audioUrl",
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
            "video": (
                "video_url",
                "videoUrl",
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
            "file": (
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
        }

        for key in key_candidates.get(target_kind, ()):
            value = file_obj.get(key)
            if isinstance(value, str) and value:
                return await self.adapter._normalize_media_url(value)
        return None

    async def _materialize_media_reference(
        self,
        file_obj: dict,
        target_kind: str,
    ) -> dict[str, str] | None:
        media_url = await self._select_media_url(file_obj, target_kind)
        if not media_url:
            return None

        name = (
            file_obj.get("name")
            or file_obj.get("title")
            or os.path.basename(urlparse(media_url).path)
            or "attachment"
        )

        if not self._is_encrypted_media_attachment(file_obj):
            return {"name": name, "url": media_url}

        raw = await self.download_remote_bytes(media_url)
        if raw is None:
            return None

        try:
            encryption = file_obj["encryption"]
            decrypted = self.adapter._e2ee.decrypt_uploaded_media(
                raw,
                key_data=encryption["key"],
                iv_b64=encryption["iv"],
            )
        except Exception as exc:
            logger.warning(f"[RocketChat][E2EE] 媒体解密失败: {exc!r}")
            return None

        expected_hash = (
            file_obj.get("hashes", {}).get("sha256")
            if isinstance(file_obj.get("hashes"), dict)
            else None
        )
        if expected_hash:
            actual_hash = hashlib.sha256(decrypted).hexdigest()
            if actual_hash.lower() != str(expected_hash).lower():
                logger.warning(
                    f"[RocketChat][E2EE] 媒体哈希校验失败: expected={expected_hash} actual={actual_hash}"
                )
                return None

        suffix = self._guess_media_suffix(file_obj, media_url, ".bin")
        local_path = self._write_temp_media_file(decrypted, suffix)
        return {"name": name, "path": local_path}

    async def _extract_media_payloads(
        self,
        raw_msg: dict,
        target_kind: str,
    ) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []

        async def add_candidate(file_obj: dict) -> None:
            if self.classify_file_kind(file_obj) != target_kind:
                return
            materialized = await self._materialize_media_reference(
                file_obj, target_kind
            )
            if materialized:
                results.append(materialized)

        # 引用消息的预览附件会内嵌原消息的媒体结构。
        # 这里如果继续递归，会把引用里的图片/文件误当成当前消息自己的媒体，
        # 随后引用消息又会被单独拉取并构建一次，最终在 AstrBot 历史里重复出现。
        all_attachments = self.get_all_attachments_recursive(
            raw_msg,
            skip_quote_attachments=True,
        )

        for context in [raw_msg, *all_attachments]:
            files_raw = context.get("files", [])
            if isinstance(files_raw, dict):
                iterable = [files_raw]
            else:
                iterable = [f for f in files_raw if isinstance(f, dict)]
            for file_obj in iterable:
                await add_candidate(file_obj)

            for file_key in ("file", "fileUpload"):
                single_file = context.get(file_key)
                if isinstance(single_file, dict):
                    await add_candidate(single_file)

            if context != raw_msg:
                await add_candidate(context)

        return results

    async def extract_image_components(self, raw_msg: dict) -> list[Image]:
        components: list[Image] = []
        seen: set[str] = set()

        for media in await self._extract_media_payloads(raw_msg, "image"):
            ref = media.get("path") or media.get("url") or ""
            if not ref or ref in seen:
                continue
            seen.add(ref)
            if media.get("path"):
                components.append(Image.fromFileSystem(media["path"]))
            elif media.get("url"):
                components.append(Image.fromURL(media["url"]))

        for url_obj in raw_msg.get("urls", []):
            if not isinstance(url_obj, dict):
                continue
            raw_meta = url_obj.get("meta")
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            raw_headers = url_obj.get("headers")
            headers = raw_headers if isinstance(raw_headers, dict) else {}
            content_type = (
                meta.get("contentType")
                or headers.get("contentType")
                or headers.get("content-type")
                or ""
            )
            if not str(content_type).startswith("image/"):
                continue
            candidate = url_obj.get("url") or url_obj.get("parsedUrl")
            if not candidate:
                continue
            normalized = await self.adapter._normalize_media_url(candidate)
            if normalized in seen:
                continue
            seen.add(normalized)
            components.append(Image.fromURL(normalized))

        return components

    async def extract_file_components(self, raw_msg: dict) -> list[File]:
        candidates = await self._extract_media_payloads(raw_msg, "file")
        deduped: list[File] = []
        seen: set[tuple[str, str]] = set()
        for media in candidates:
            name = media.get("name") or "attachment"
            ref = media.get("path") or media.get("url") or ""
            key = (name, ref)
            if key in seen or not ref:
                continue
            seen.add(key)
            if media.get("path"):
                deduped.append(File(name=name, file=media["path"]))
            elif media.get("url"):
                deduped.append(File(name=name, url=media["url"]))
        return deduped

    async def extract_record_components(self, raw_msg: dict) -> list[Record]:
        candidates = await self._extract_media_payloads(raw_msg, "audio")
        deduped: list[Record] = []
        seen: set[str] = set()
        for media in candidates:
            ref = media.get("path") or media.get("url") or ""
            if ref in seen or not ref:
                continue
            seen.add(ref)
            if media.get("path"):
                deduped.append(Record.fromFileSystem(media["path"]))
            elif media.get("url"):
                deduped.append(Record.fromURL(media["url"]))
        return deduped

    async def extract_video_components(self, raw_msg: dict) -> list[Video]:
        candidates = await self._extract_media_payloads(raw_msg, "video")
        deduped: list[Video] = []
        seen: set[str] = set()
        for media in candidates:
            ref = media.get("path") or media.get("url") or ""
            if ref in seen or not ref:
                continue
            seen.add(ref)
            if media.get("path"):
                deduped.append(Video.fromFileSystem(media["path"]))
            elif media.get("url"):
                deduped.append(Video.fromURL(media["url"]))
        return deduped

    def infer_upload_content_type(self, file_path: str, filename: str) -> str:
        guessed_type, _ = mimetypes.guess_type(filename)
        if guessed_type:
            return guessed_type

        guessed_type, _ = mimetypes.guess_type(file_path)
        if guessed_type:
            return guessed_type

        try:
            with open(file_path, "rb") as fp:
                header = fp.read(16)
        except Exception:
            return "application/octet-stream"

        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if header.startswith(b"BM"):
            return "image/bmp"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return "image/webp"

        return "application/octet-stream"

    async def post_multipart_json(
        self,
        url: str,
        form: aiohttp.FormData,
    ) -> dict[str, Any] | None:
        status, data = await self.post_multipart_json_response(url, form)
        if data and data.get("success", bool(status is not None and status < 400)):
            return data
        return None

    async def post_multipart_json_response(
        self,
        url: str,
        form: aiohttp.FormData,
    ) -> tuple[int | None, dict[str, Any] | None]:
        headers = self.adapter._get_auth_headers()
        try:
            async with self.adapter._get_http_session().post(
                url, data=form, headers=headers
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    text = await resp.text()
                    data = {"success": False, "error": text}
                if resp.status >= 400 or not data.get("success", resp.status < 400):
                    logger.error(
                        f"[RocketChat] 上传请求失败: status={resp.status} data={data}"
                    )
                    return resp.status, None if data is None else data
                return resp.status, data
        except Exception as exc:
            logger.error(f"[RocketChat] 上传请求异常: {exc!r}")
            return None, None

    async def post_json_response(
        self,
        url: str,
        payload: dict[str, Any],
    ) -> tuple[int | None, dict[str, Any] | None]:
        try:
            async with self.adapter._get_http_session().post(
                url,
                json=payload,
                headers=self.adapter._get_auth_headers(),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    text = await resp.text()
                    data = {"success": False, "error": text}
                if resp.status >= 400 or not data.get("success", resp.status < 400):
                    logger.error(
                        f"[RocketChat] JSON 请求失败: status={resp.status} data={data}"
                    )
                    return resp.status, data
                return resp.status, data
        except Exception as exc:
            logger.error(f"[RocketChat] JSON 请求异常: {exc!r}")
            return None, None

    async def upload_plain_file(
        self,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: str | None = None,
    ) -> bool:
        media_url = f"{self.adapter.server_url}/api/v1/rooms.media/{room_id}"
        with open(file_path, "rb") as fp:
            form = aiohttp.FormData()
            content_type = self.infer_upload_content_type(file_path, resolved_name)
            form.add_field(
                "file", fp, filename=resolved_name, content_type=content_type
            )
            upload_status, upload_resp = await self.post_multipart_json_response(
                media_url, form
            )

        upload_ok = bool(
            upload_resp
            and upload_resp.get(
                "success", bool(upload_status is not None and upload_status < 400)
            )
        )
        if not upload_ok or not upload_resp:
            logger.error(
                "[RocketChat] rooms.media 上传失败: status=%s data=%s",
                upload_status,
                upload_resp,
            )
            return False

        uploaded_file = upload_resp.get("file") or {}
        file_id = uploaded_file.get("_id")
        if not file_id:
            logger.error(f"[RocketChat] rooms.media 响应缺少文件 ID: {upload_resp}")
            return False

        confirm_payload: dict[str, Any] = {}
        if description:
            confirm_payload["msg"] = description
        if tmid:
            confirm_payload["tmid"] = tmid
        confirm_status, confirm_resp = await self.post_json_response(
            f"{self.adapter.server_url}/api/v1/rooms.mediaConfirm/{room_id}/{file_id}",
            confirm_payload,
        )
        confirm_ok = bool(
            confirm_resp
            and confirm_resp.get(
                "success", bool(confirm_status is not None and confirm_status < 400)
            )
        )
        if confirm_ok:
            return True

        logger.error(
            "[RocketChat] rooms.mediaConfirm 失败: status=%s data=%s",
            confirm_status,
            confirm_resp,
        )
        return False

    async def upload_encrypted_file(
        self,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: str | None = None,
    ) -> bool:
        try:
            with open(file_path, "rb") as fp:
                file_bytes = fp.read()
        except FileNotFoundError:
            logger.error(f"[RocketChat] 文件不存在: {file_path}")
            return False

        mime_type = self.infer_upload_content_type(file_path, resolved_name)
        upload = await self.adapter._e2ee.prepare_encrypted_upload(
            room_id,
            file_name=resolved_name,
            mime_type=mime_type,
            file_bytes=file_bytes,
        )
        if not upload:
            logger.warning(
                f"[RocketChat][E2EE] 未能准备加密上传数据，已跳过 room_id={room_id!r}"
            )
            return False

        file_content = await self.adapter._e2ee.build_upload_file_content(
            room_id, upload
        )
        if not file_content:
            logger.warning(
                f"[RocketChat][E2EE] 未能生成加密文件元数据，已跳过 room_id={room_id!r}"
            )
            return False

        form = aiohttp.FormData()
        form.add_field(
            "file",
            upload.encrypted_bytes,
            filename=upload.encrypted_name,
            content_type="application/octet-stream",
        )
        form.add_field(
            "content", json.dumps(file_content["encrypted"], ensure_ascii=False)
        )

        upload_resp = await self.post_multipart_json(
            f"{self.adapter.server_url}/api/v1/rooms.media/{room_id}",
            form,
        )
        if not upload_resp:
            return False

        uploaded_file = upload_resp.get("file") or {}
        file_id = uploaded_file.get("_id")
        file_url = uploaded_file.get("url")
        if not file_id or not file_url:
            logger.error(
                f"[RocketChat][E2EE] rooms.media 响应缺少文件信息: {upload_resp}"
            )
            return False

        confirm_payload = await self.adapter._e2ee.build_media_confirm_payload(
            room_id,
            upload_id=file_id,
            upload_url=file_url,
            upload=upload,
            text=description,
            tmid=tmid,
        )
        if not confirm_payload:
            logger.warning(
                f"[RocketChat][E2EE] 未能生成 mediaConfirm 负载，已跳过 room_id={room_id!r}"
            )
            return False

        return await self.adapter._post_json_message(
            f"{self.adapter.server_url}/api/v1/rooms.mediaConfirm/{room_id}/{file_id}",
            confirm_payload,
        )

    async def upload_local_file(
        self,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: str | None = None,
    ) -> bool:
        room_info = await self.adapter._get_room_info(room_id)
        if self.adapter._is_unknown_room_info(room_info):
            logger.warning(
                f"[RocketChat][room] 房间信息未知，拒绝上传媒体以避免误走明文 room_id={room_id!r}"
            )
            return False
        if self._is_e2ee_room_info(room_info):
            return await self.upload_encrypted_file(
                room_id,
                file_path,
                resolved_name,
                description=description,
                tmid=tmid,
            )
        return await self.upload_plain_file(
            room_id,
            file_path,
            resolved_name,
            description=description,
            tmid=tmid,
        )

    def _is_e2ee_room_info(self, room_info: dict[str, Any]) -> bool:
        return self.adapter._is_e2ee_room_info(room_info)

    async def is_encrypted_room(self, room_id: str) -> bool:
        room_info = await self.adapter._get_room_info(room_id)
        if self.adapter._is_unknown_room_info(room_info):
            return False
        return self._is_e2ee_room_info(room_info)

    async def send_remote_media_fallback(
        self,
        room_id: str,
        media_url: str,
        *,
        media_kind: str,
        text: str = "",
        tmid: str | None = None,
    ) -> bool:
        if not await self.is_encrypted_room(room_id):
            return False

        logger.warning(
            f"[RocketChat][E2EE] 加密房间{media_kind}下载失败，降级为加密文本链接发送 room_id={room_id!r}"
        )
        fallback_text = f"远程媒体下载失败，原文件链接：{media_url}"
        if text:
            fallback_text = f"{text}\n{fallback_text}".strip()
        await self.adapter.send_text(room_id, fallback_text, tmid=tmid)
        return True

    async def send_image_url(
        self,
        room_id: str,
        image_url: str,
        text: str = "",
        tmid: str | None = None,
    ) -> bool:
        # 统一：先下载远程图片到本地，再通过文件上传发送
        # 避免外部 URL 防盗链（Referer 检查）导致图片在 Rocket.Chat 中无法显示
        local_path, cleanup = await self.download_remote_media(image_url, ".png")
        if not local_path:
            handled = await self.send_remote_media_fallback(
                room_id,
                image_url,
                media_kind="图片",
                text=text,
                tmid=tmid,
            )
            if not handled:
                logger.warning(
                    f"[RocketChat] 远程图片下载失败，降级为 URL 引用发送 url={image_url[:80]!r}"
                )
                return await self.adapter._send_structured_message(
                    room_id,
                    text,
                    attachments=[{"image_url": image_url}],
                    tmid=tmid,
                )
            return True

        try:
            return await self.send_image_file(
                room_id,
                local_path,
                description=text,
                tmid=tmid,
            )
        finally:
            if cleanup:
                cleanup()

    async def send_image_file(
        self,
        room_id: str,
        file_path: str,
        description: str = "",
        tmid: str | None = None,
    ) -> bool:
        try:
            filename = os.path.basename(file_path) or "image.png"
            return await self.upload_local_file(
                room_id,
                file_path,
                filename,
                description=description,
                tmid=tmid,
            )
        except FileNotFoundError:
            logger.error(f"[RocketChat] 图片文件不存在: {file_path}")
        except Exception as exc:
            logger.error(f"[RocketChat] 上传图片异常: {exc!r}")
        return False

    async def send_file(
        self,
        room_id: str,
        file_path: str,
        filename: str | None = None,
        description: str = "",
        tmid: str | None = None,
    ) -> None:
        try:
            resolved_name = filename or os.path.basename(file_path) or "attachment"
            await self.upload_local_file(
                room_id,
                file_path,
                resolved_name,
                description=description,
                tmid=tmid,
            )
        except FileNotFoundError:
            logger.error(f"[RocketChat] 文件不存在: {file_path}")
        except Exception as exc:
            logger.error(f"[RocketChat] 上传文件异常: {exc!r}")

    async def download_remote_media(
        self,
        url: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path)
        _, ext = os.path.splitext(filename)
        suffix = ext if ext else default_suffix
        raw = await self.download_remote_bytes(url)
        if raw is None:
            return None, None

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            name = tmp.name
        return name, lambda: os.unlink(name)

    def decode_base64_media(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        try:
            raw = base64.b64decode(file_ref[len("base64://") :])
        except Exception as exc:
            logger.error(f"[RocketChat] Base64 媒体处理失败: {exc!r}")
            return None, None

        with tempfile.NamedTemporaryFile(suffix=default_suffix, delete=False) as tmp:
            tmp.write(raw)
            name = tmp.name
        return name, lambda: os.unlink(name)
