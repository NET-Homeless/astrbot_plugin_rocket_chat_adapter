from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from astrbot.api import logger
from cryptography.hazmat.primitives import hashes, padding as sym_padding
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def _b64_encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64_decode(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + ("=" * (-len(data) % 4)))


def _binary_encode(data: bytes) -> str:
    return data.decode("latin-1")


def _binary_decode(data: str) -> bytes:
    return data.encode("latin-1")


def _json_dumps(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _uint_to_b64url(value: int) -> str:
    length = max(1, (value.bit_length() + 7) // 8)
    return _b64url_encode(value.to_bytes(length, "big"))


def _b64url_to_uint(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), "big")


def _derive_key(password: str, salt: str, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_binary_decode(salt),
        iterations=iterations,
    )
    return kdf.derive(_binary_decode(password))


def _decrypt_aes_cbc(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _encrypt_aes_cbc(key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    iv = os.urandom(16)
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return iv, encryptor.update(padded) + encryptor.finalize()


def _decode_prefixed_base64(data: str) -> tuple[str, bytes]:
    encoded_length = 344
    if len(data) < encoded_length:
        raise ValueError("invalid prefixed base64 payload")
    prefix = data[:-encoded_length]
    decoded = _b64_decode(data[-encoded_length:])
    if len(decoded) != 256:
        raise ValueError("invalid RSA payload length")
    return prefix, decoded


def _encode_prefixed_base64(prefix: str, data: bytes) -> str:
    if len(data) != 256:
        raise ValueError("unexpected RSA payload length")
    return prefix + _b64_encode(data)


def _export_public_jwk(key: rsa.RSAPublicKey) -> dict[str, Any]:
    numbers = key.public_numbers()
    return {
        "kty": "RSA",
        "alg": "RSA-OAEP-256",
        "e": _uint_to_b64url(numbers.e),
        "ext": True,
        "key_ops": ["encrypt"],
        "n": _uint_to_b64url(numbers.n),
    }


def _export_private_jwk(key: rsa.RSAPrivateKey) -> dict[str, Any]:
    numbers = key.private_numbers()
    public_numbers = numbers.public_numbers
    return {
        "kty": "RSA",
        "alg": "RSA-OAEP-256",
        "e": _uint_to_b64url(public_numbers.e),
        "ext": True,
        "key_ops": ["decrypt"],
        "n": _uint_to_b64url(public_numbers.n),
        "d": _uint_to_b64url(numbers.d),
        "p": _uint_to_b64url(numbers.p),
        "q": _uint_to_b64url(numbers.q),
        "dp": _uint_to_b64url(numbers.dmp1),
        "dq": _uint_to_b64url(numbers.dmq1),
        "qi": _uint_to_b64url(numbers.iqmp),
    }


def _import_public_jwk(data: str | dict[str, Any]) -> rsa.RSAPublicKey:
    jwk = json.loads(data) if isinstance(data, str) else data
    numbers = rsa.RSAPublicNumbers(
        e=_b64url_to_uint(jwk["e"]),
        n=_b64url_to_uint(jwk["n"]),
    )
    return numbers.public_key()


def _import_private_jwk(data: str | dict[str, Any]) -> rsa.RSAPrivateKey:
    jwk = json.loads(data) if isinstance(data, str) else data
    public_numbers = rsa.RSAPublicNumbers(
        e=_b64url_to_uint(jwk["e"]),
        n=_b64url_to_uint(jwk["n"]),
    )
    numbers = rsa.RSAPrivateNumbers(
        p=_b64url_to_uint(jwk["p"]),
        q=_b64url_to_uint(jwk["q"]),
        d=_b64url_to_uint(jwk["d"]),
        dmp1=_b64url_to_uint(jwk["dp"]),
        dmq1=_b64url_to_uint(jwk["dq"]),
        iqmp=_b64url_to_uint(jwk["qi"]),
        public_numbers=public_numbers,
    )
    return numbers.private_key()


def _encrypt_private_key_for_server(
    user_id: str,
    password: str,
    private_key_json: str,
) -> str:
    salt = f"v2:{user_id}:{uuid.uuid4()}"
    iterations = 100_000
    key = _derive_key(password, salt, iterations)
    iv = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(iv, _binary_decode(private_key_json), None)
    return _json_dumps(
        {
            "iv": _b64_encode(iv),
            "ciphertext": _b64_encode(ciphertext),
            "salt": salt,
            "iterations": iterations,
        }
    )


def _decrypt_private_key_from_server(
    user_id: str,
    password: str,
    stored_private_key: str,
) -> str:
    parsed = json.loads(stored_private_key)
    if "$binary" in parsed:
        raw = _b64_decode(parsed["$binary"])
        iv = raw[:16]
        ciphertext = raw[16:]
        key = _derive_key(password, user_id, 1000)
        return _binary_encode(_decrypt_aes_cbc(key, iv, ciphertext))

    iv = _b64_decode(parsed["iv"])
    ciphertext = _b64_decode(parsed["ciphertext"])
    salt = parsed["salt"]
    iterations = int(parsed["iterations"])
    key = _derive_key(password, salt, iterations)

    if len(iv) == 12:
        plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
    else:
        plaintext = _decrypt_aes_cbc(key, iv, ciphertext)
    return _binary_encode(plaintext)


@dataclass
class SessionKey:
    key_id: str
    alg: str
    key_bytes: bytes
    raw_jwk: dict[str, Any]

    @classmethod
    def generate(cls, key_id: str) -> "SessionKey":
        key_bytes = os.urandom(32)
        jwk = {
            "kty": "oct",
            "k": _b64url_encode(key_bytes),
            "key_ops": ["encrypt", "decrypt"],
            "ext": True,
            "alg": "A256GCM",
        }
        return cls(key_id=key_id, alg="A256GCM", key_bytes=key_bytes, raw_jwk=jwk)

    @classmethod
    def from_jwk_json(cls, key_id: str, jwk_json: str) -> "SessionKey":
        jwk = json.loads(jwk_json)
        return cls(
            key_id=key_id,
            alg=jwk["alg"],
            key_bytes=_b64url_decode(jwk["k"]),
            raw_jwk=jwk,
        )

    def export_jwk_json(self) -> str:
        return _json_dumps(self.raw_jwk)

    def encrypt_payload(self, plaintext: bytes) -> dict[str, str]:
        if self.alg == "A256GCM":
            iv = os.urandom(12)
            ciphertext = AESGCM(self.key_bytes).encrypt(iv, plaintext, None)
        elif self.alg == "A128CBC":
            iv, ciphertext = _encrypt_aes_cbc(self.key_bytes, plaintext)
        else:
            raise ValueError(f"unsupported session key algorithm: {self.alg}")

        return {
            "kid": self.key_id,
            "iv": _b64_encode(iv),
            "ciphertext": _b64_encode(ciphertext),
        }

    def decrypt_payload(self, iv: bytes, ciphertext: bytes) -> bytes:
        if self.alg == "A256GCM":
            return AESGCM(self.key_bytes).decrypt(iv, ciphertext, None)
        if self.alg == "A128CBC":
            return _decrypt_aes_cbc(self.key_bytes, iv, ciphertext)
        raise ValueError(f"unsupported session key algorithm: {self.alg}")


@dataclass
class RoomKeyStore:
    current: Optional[SessionKey] = None
    old_keys: dict[str, SessionKey] = field(default_factory=dict)

    def find(self, key_id: str) -> Optional[SessionKey]:
        return self.old_keys.get(key_id) or self.current


class RocketChatE2EEManager:
    """
    Python implementation aligned with Rocket.Chat official E2EE client/server source.

    Scope in this adapter:
    - text / quote / attachment-metadata encryption for encrypted DM/private-group rooms
    - inbound encrypted text decryption
    - non-encrypted rooms always stay on the old plain-text path
    """

    def __init__(self, adapter: Any, enabled: bool, password: str) -> None:
        self.adapter = adapter
        self.enabled = enabled
        self.password = password or ""
        self.ready = False
        self.public_key_json: Optional[str] = None
        self.private_key: Optional[rsa.RSAPrivateKey] = None
        self._room_keys: dict[str, RoomKeyStore] = {}
        self._room_locks: dict[str, asyncio.Lock] = {}
        self._subscriptions_by_room: dict[str, dict[str, Any]] = {}
        self._subscriptions_cache_ts: float = 0.0

    async def initialize(self) -> None:
        if not self.enabled:
            return
        if not self.password:
            logger.warning("[RocketChat][E2EE] 已启用 E2EE，但未配置 e2ee_password，已跳过加密支持")
            self.enabled = False
            return

        try:
            data = await self._rest_get("/api/v1/e2e.fetchMyKeys")
            public_key = data.get("public_key")
            private_key = data.get("private_key")

            if public_key and private_key:
                private_key_json = _decrypt_private_key_from_server(
                    self.adapter.user_id,
                    self.password,
                    private_key,
                )
                self.public_key_json = public_key
                self.private_key = _import_private_jwk(private_key_json)
            else:
                private_key_obj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                public_key_obj = private_key_obj.public_key()
                public_key_json = _json_dumps(_export_public_jwk(public_key_obj))
                private_key_json = _json_dumps(_export_private_jwk(private_key_obj))
                encrypted_private_key = _encrypt_private_key_for_server(
                    self.adapter.user_id,
                    self.password,
                    private_key_json,
                )
                await self._rest_post(
                    "/api/v1/e2e.setUserPublicAndPrivateKeys",
                    {
                        "public_key": public_key_json,
                        "private_key": encrypted_private_key,
                        "force": False,
                    },
                )
                self.public_key_json = public_key_json
                self.private_key = private_key_obj

            self.ready = True
            logger.info("[RocketChat][E2EE] 客户端密钥已就绪")
        except Exception as exc:
            self.ready = False
            logger.warning(f"[RocketChat][E2EE] 初始化失败，将保持普通房间链路不受影响: {exc!r}")

    async def on_ws_ready(self) -> None:
        if not self.ready:
            return
        try:
            await self.adapter._ddp_call("e2e.requestSubscriptionKeys", [])
        except Exception as exc:
            logger.debug(f"[RocketChat][E2EE] requestSubscriptionKeys 失败: {exc!r}")

    async def should_encrypt_room(self, room_info: dict) -> bool:
        return bool(
            self.enabled
            and self.ready
            and room_info.get("encrypted")
            and room_info.get("t") in {"d", "p"}
        )

    async def maybe_decrypt_message(self, raw_msg: dict) -> Optional[dict]:
        if raw_msg.get("t") != "e2e":
            return raw_msg
        if not self.ready:
            return None

        room_id = raw_msg.get("rid")
        if not room_id:
            return None

        room_info = await self.adapter._get_room_info(room_id)
        session_key = await self._ensure_room_key(room_id, room_info=room_info)
        if not session_key:
            return None

        key_store = self._room_keys.get(room_id) or RoomKeyStore(current=session_key)
        decrypted = self._decrypt_message_payload(raw_msg, key_store)
        if decrypted is None:
            return None

        merged = dict(raw_msg)
        merged.update(decrypted)
        merged["e2e"] = "done"
        return merged

    async def build_send_message(
        self,
        room_id: str,
        text: str = "",
        attachments: Optional[list[dict[str, Any]]] = None,
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        room_info = await self.adapter._get_room_info(room_id)
        if not await self.should_encrypt_room(room_info):
            return None

        session_key = await self._ensure_room_key(room_id, room_info=room_info)
        if not session_key:
            return None

        content_to_encrypt: dict[str, Any] = {}
        if text:
            content_to_encrypt["msg"] = text
        if attachments:
            content_to_encrypt["attachments"] = attachments

        encrypted = session_key.encrypt_payload(
            _json_dumps(content_to_encrypt).encode("utf-8")
        )

        message: dict[str, Any] = {
            "rid": room_id,
            "t": "e2e",
            "e2e": "pending",
            "content": {
                "algorithm": "rc.v2.aes-sha2",
                **encrypted,
            },
        }
        if tmid:
            message["tmid"] = tmid
        return {"message": message}

    def encrypted_upload_supported(self, room_info: dict) -> bool:
        return not (
            self.enabled
            and room_info.get("encrypted")
            and room_info.get("t") in {"d", "p"}
        )

    async def _ensure_room_key(
        self,
        room_id: str,
        *,
        room_info: Optional[dict] = None,
    ) -> Optional[SessionKey]:
        room_info = room_info or await self.adapter._get_room_info(room_id)
        if not await self.should_encrypt_room(room_info):
            return None

        lock = self._room_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            key_store = self._room_keys.setdefault(room_id, RoomKeyStore())
            if key_store.current and key_store.current.key_id == room_info.get("e2eKeyId"):
                await self._maybe_share_room_key(room_id, key_store.current)
                return key_store.current

            subscription = await self._get_subscription(room_id, refresh=True)

            if subscription:
                key_store.old_keys = await self._load_old_keys(subscription)

                suggested_key = subscription.get("E2ESuggestedKey")
                if suggested_key:
                    imported = self._import_group_key(suggested_key)
                    if imported:
                        key_store.current = imported
                        try:
                            await self._rest_post(
                                "/api/v1/e2e.acceptSuggestedGroupKey",
                                {"rid": room_id},
                            )
                        except Exception as exc:
                            logger.debug(f"[RocketChat][E2EE] acceptSuggestedGroupKey 失败: {exc!r}")
                        await self._maybe_share_room_key(room_id, imported)
                        return imported

                existing_key = subscription.get("E2EKey")
                if existing_key:
                    imported = self._import_group_key(existing_key)
                    if imported:
                        key_store.current = imported
                        await self._maybe_share_room_key(room_id, imported)
                        return imported

            if not room_info.get("e2eKeyId"):
                created = await self._create_room_key(room_id)
                key_store.current = created
                return created

            await self.on_ws_ready()
            for _ in range(3):
                await asyncio.sleep(1)
                subscription = await self._get_subscription(room_id, refresh=True)
                if not subscription:
                    continue
                key_store.old_keys = await self._load_old_keys(subscription)
                for field in ("E2ESuggestedKey", "E2EKey"):
                    encrypted_key = subscription.get(field)
                    if not encrypted_key:
                        continue
                    imported = self._import_group_key(encrypted_key)
                    if not imported:
                        continue
                    key_store.current = imported
                    if field == "E2ESuggestedKey":
                        try:
                            await self._rest_post(
                                "/api/v1/e2e.acceptSuggestedGroupKey",
                                {"rid": room_id},
                            )
                        except Exception as exc:
                            logger.debug(f"[RocketChat][E2EE] acceptSuggestedGroupKey 失败: {exc!r}")
                    await self._maybe_share_room_key(room_id, imported)
                    return imported

            logger.warning(f"[RocketChat][E2EE] 未能及时拿到房间密钥 room_id={room_id!r}")
            return None

    async def _create_room_key(self, room_id: str) -> SessionKey:
        if not self.public_key_json:
            raise RuntimeError("public key not ready")

        key_id = str(uuid.uuid4())
        session_key = SessionKey.generate(key_id)
        encrypted_self_key = self._encrypt_group_key_for_participant(
            session_key,
            self.public_key_json,
        )

        await self._rest_post(
            "/api/v1/e2e.setRoomKeyID",
            {"rid": room_id, "keyID": key_id},
        )
        await self._rest_post(
            "/api/v1/e2e.updateGroupKey",
            {"rid": room_id, "uid": self.adapter.user_id, "key": encrypted_self_key},
        )
        await self._maybe_share_room_key(room_id, session_key)
        room_info = await self.adapter._get_room_info(room_id, refresh=True)
        self.adapter._cache_room_info(room_info)
        logger.info(f"[RocketChat][E2EE] 已创建房间密钥 room_id={room_id!r} key_id={key_id}")
        return session_key

    async def _maybe_share_room_key(self, room_id: str, session_key: SessionKey) -> None:
        try:
            users = (
                await self._rest_get(
                    "/api/v1/e2e.getUsersOfRoomWithoutKey",
                    params={"rid": room_id},
                )
            ).get("users", [])
            if not users:
                return

            encrypted_users = []
            for user in users:
                public_key = user.get("e2e", {}).get("public_key")
                user_id = user.get("_id")
                if not public_key or not user_id or user_id == self.adapter.user_id:
                    continue
                encrypted_users.append(
                    {
                        "_id": user_id,
                        "key": self._encrypt_group_key_for_participant(session_key, public_key),
                    }
                )

            if not encrypted_users:
                return

            await self._rest_post(
                "/api/v1/e2e.provideUsersSuggestedGroupKeys",
                {"usersSuggestedGroupKeys": {room_id: encrypted_users}},
            )
        except Exception as exc:
            logger.debug(f"[RocketChat][E2EE] 分发房间密钥失败 room_id={room_id!r}: {exc!r}")

    async def _load_old_keys(self, subscription: dict[str, Any]) -> dict[str, SessionKey]:
        old_keys: dict[str, SessionKey] = {}
        for field in ("oldRoomKeys", "suggestedOldRoomKeys"):
            for key_payload in subscription.get(field, []) or []:
                encrypted_key = key_payload.get("E2EKey")
                key_id = key_payload.get("e2eKeyId")
                if not encrypted_key or not key_id:
                    continue
                session_key = self._import_group_key(encrypted_key)
                if session_key:
                    old_keys[key_id] = session_key
        return old_keys

    def _import_group_key(self, encrypted_key: str) -> Optional[SessionKey]:
        if not self.private_key:
            return None

        try:
            key_id, encrypted = _decode_prefixed_base64(encrypted_key)
            decrypted = self.private_key.decrypt(
                encrypted,
                asym_padding.OAEP(
                    mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
            return SessionKey.from_jwk_json(key_id, _binary_encode(decrypted))
        except Exception as exc:
            logger.debug(f"[RocketChat][E2EE] 导入房间密钥失败: {exc!r}")
            return None

    def _encrypt_group_key_for_participant(
        self,
        session_key: SessionKey,
        public_key_json: str,
    ) -> str:
        public_key = _import_public_jwk(public_key_json)
        encrypted = public_key.encrypt(
            _binary_decode(session_key.export_jwk_json()),
            asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return _encode_prefixed_base64(session_key.key_id, encrypted)

    def _decrypt_message_payload(
        self,
        raw_msg: dict[str, Any],
        key_store: RoomKeyStore,
    ) -> Optional[dict[str, Any]]:
        try:
            payload = raw_msg.get("content")
            if payload:
                key_id = payload["kid"]
                iv = _b64_decode(payload["iv"])
                ciphertext = _b64_decode(payload["ciphertext"])
            else:
                key_id = raw_msg["msg"][:12]
                decoded = _b64_decode(raw_msg["msg"][12:])
                iv, ciphertext = decoded[:16], decoded[16:]

            session_key = key_store.find(key_id)
            if not session_key:
                logger.warning(
                    f"[RocketChat][E2EE] 找不到解密密钥 room_id={raw_msg.get('rid')!r} key_id={key_id!r}"
                )
                return None

            plaintext = session_key.decrypt_payload(iv, ciphertext)
            decoded = json.loads(plaintext.decode("utf-8"))
            if not isinstance(decoded, dict):
                return None

            if "text" in decoded and "msg" not in decoded and isinstance(decoded["text"], str):
                decoded["msg"] = decoded.pop("text")
            return decoded
        except Exception as exc:
            logger.warning(
                f"[RocketChat][E2EE] 解密消息失败 room_id={raw_msg.get('rid')!r} msg_id={raw_msg.get('_id')!r}: {exc!r}"
            )
            return None

    async def _get_subscription(
        self,
        room_id: str,
        *,
        refresh: bool = False,
    ) -> Optional[dict[str, Any]]:
        await self._refresh_subscriptions(force=refresh)
        return self._subscriptions_by_room.get(room_id)

    async def _refresh_subscriptions(self, *, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if not force and (now - self._subscriptions_cache_ts) < 1.0:
            return
        subscriptions = (
            await self._rest_get("/api/v1/subscriptions.get")
        ).get("update", [])
        self._subscriptions_by_room = {
            sub["rid"]: sub for sub in subscriptions if isinstance(sub, dict) and sub.get("rid")
        }
        self._subscriptions_cache_ts = now

    async def _rest_get(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        url = f"{self.adapter.server_url}{path}"
        async with self.adapter._http_session.get(
            url,
            params=params,
            headers=self.adapter._auth_headers(),
        ) as resp:
            data = await resp.json()
        if not data.get("success"):
            raise RuntimeError(f"GET {path} failed: {data}")
        return data

    async def _rest_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.adapter.server_url}{path}"
        async with self.adapter._http_session.post(
            url,
            json=payload,
            headers=self.adapter._auth_headers(),
        ) as resp:
            data = await resp.json()
        if not data.get("success"):
            raise RuntimeError(f"POST {path} failed: {data}")
        return data
