"""
Rocket.Chat 平台适配器（Platform Adapter）

架构：
  - REST API  → 认证（POST /api/v1/login）、发送消息（POST /api/v1/chat.postMessage）
  - WebSocket → DDP 协议实时接收消息（wss://server/websocket）

依赖：aiohttp
"""

from __future__ import annotations

import asyncio
from asyncio import Queue
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.platform import (
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)

from .rocketchat_e2ee import RocketChatE2EEManager
from .rocketchat_inbound import RocketChatInboundBridge
from .rocketchat_media import RocketChatMediaBridge
from .rocketchat_realtime import RocketChatRealtimeBridge
from .rocketchat_sender import RocketChatSenderBridge


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


ROCKETCHAT_CONFIG_METADATA = {
    "server_url": {
        "description": "Rocket.Chat 服务器地址",
        "type": "string",
        "hint": "Rocket.Chat 服务地址，包含 http:// 或 https://，不要带末尾斜杠。",
    },
    "username": {
        "description": "机器人用户名",
        "type": "string",
        "hint": "用于登录 Rocket.Chat 的机器人用户名。",
    },
    "password": {
        "description": "机器人密码",
        "type": "string",
        "hint": "用于登录 Rocket.Chat 的机器人密码。",
    },
    "reconnect_delay": {
        "description": "重连延迟",
        "type": "float",
        "hint": "WebSocket 断开后自动重连的等待秒数。",
    },
    "typing_indicator_delay": {
        "description": "输入中延迟",
        "type": "float",
        "hint": "回复较慢时显示 typing 的延迟秒数。",
    },
    "remote_media_max_size": {
        "description": "远程媒体大小上限",
        "type": "int",
        "hint": "下载远程媒体时允许的最大字节数。",
    },
    "enable_e2ee": {
        "description": "启用 E2EE",
        "type": "bool",
        "hint": "启用 Rocket.Chat 端到端加密支持。私信和私有群组会按官方 E2EE 协议处理。",
    },
    "e2ee_password": {
        "description": "E2EE 密钥密码",
        "type": "string",
        "hint": "Rocket.Chat E2EE 私钥密码。仅在启用 E2EE 时使用。",
    },
}


@register_platform_adapter(
    "rocket_chat",
    "Rocket.Chat 消息平台适配器",
    default_config_tmpl={
        "id": "rocket_chat",
        "server_url": "http://localhost:3000",
        "username": "",
        "password": "",
        "reconnect_delay": 5.0,
        "typing_indicator_delay": 0.5,
        "remote_media_max_size": 20971520,
        "enable_e2ee": False,
        "e2ee_password": "",
    },
    support_streaming_message=False,
    config_metadata=ROCKETCHAT_CONFIG_METADATA,
)
class RocketChatAdapter(Platform):
    """
    Rocket.Chat 平台适配器。

    配置项（default_config_tmpl）：
      id                     : 适配器实例唯一标识，默认 "rocket_chat"
      server_url             : Rocket.Chat 服务器地址，如 http://localhost:3000
      username               : 机器人账号用户名
      password               : 机器人账号密码
      reconnect_delay        : WebSocket 断线后重连等待秒数，默认 5.0
      typing_indicator_delay : 输入中提示的延迟秒数；小于该时间的快速系统回复不显示 typing
      remote_media_max_size  : 远端媒体下载大小上限（字节），默认 20MB
      enable_e2ee           : 是否启用 Rocket.Chat E2EE 支持，默认 False
      e2ee_password         : Rocket.Chat E2EE 私钥密码；仅在 enable_e2ee=True 时使用
    """

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings: dict = platform_settings

        # 配置读取
        self.server_url: str = platform_config.get(
            "server_url", "http://localhost:3000"
        ).rstrip("/")
        self.username: str = platform_config.get("username", "")
        self.password: str = platform_config.get("password", "")
        self.reconnect_delay: float = float(platform_config.get("reconnect_delay", 5.0))
        self.typing_indicator_delay: float = float(
            platform_config.get("typing_indicator_delay", 0.5)
        )
        self.remote_media_max_size: int = int(
            platform_config.get("remote_media_max_size", 20 * 1024 * 1024)
        )
        self.enable_e2ee: bool = _coerce_bool(platform_config.get("enable_e2ee", False))
        self.e2ee_password: str = str(platform_config.get("e2ee_password", ""))

        # 配置验证
        self._validate_config()

        # 运行时状态
        self.auth_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.bot_username: Optional[str] = None

        self._http_session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running: bool = False
        # 停止信号：terminate() 调用时 set，用于立即打断重连 sleep
        self._stop_event: Optional[asyncio.Event] = None

        # 房间类型缓存（避免重复 API 请求）
        # key: room_id, value: "c"（频道）| "p"（私有群组）| "d"（私信）
        self._room_type_cache: Dict[str, str] = {}
        # 房间名称缓存，用于构造 message_link
        self._room_name_cache: Dict[str, str] = {}
        # 房间完整信息缓存（类型/名称/加密状态/e2eKeyId）
        self._room_info_cache: Dict[str, dict] = {}
        # 房间信息缓存锁，防止并发更新导致数据覆盖丢失
        self._room_cache_locks: Dict[str, asyncio.Lock] = {}
        # 已订阅房间集合，防止重复订阅导致消息被多次处理
        self._subscribed_rooms: set = set()
        # DDP method 调用 ID 计数器，确保每次调用 ID 唯一
        self._ddp_call_id: int = 0
        # 等待 DDP result 的 Future 映射
        self._pending_ddp_results: Dict[str, asyncio.Future] = {}
        # 后台任务强引用集合，防止 Python 3.12+ GC 回收未完成的 task
        self._background_tasks: set[asyncio.Task] = set()
        # 并发处理控制，防止瞬间过多消息导致处理积压
        self._message_semaphore = asyncio.Semaphore(100)
        # 已处理入站消息 ID 缓存，防止 Rocket.Chat message updated / link preview
        # 等重复 DDP 推送导致同一条消息触发多次回复。
        self._processed_message_ids: Dict[str, float] = {}

        self._meta = PlatformMetadata(
            name="rocket_chat",
            description="Rocket.Chat 消息平台适配器",
            id=platform_config.get("id", "rocket_chat"),
            support_streaming_message=False,
        )
        self._e2ee = RocketChatE2EEManager(
            adapter=self,
            enabled=self.enable_e2ee,
            password=self.e2ee_password,
        )
        self._inbound = RocketChatInboundBridge(self)
        self._media = RocketChatMediaBridge(self)
        self._realtime = RocketChatRealtimeBridge(self)
        self._sender = RocketChatSenderBridge(self)

    def _validate_config(self) -> None:
        """
        验证配置项的有效性，在初始化时立即报错而非运行时失败。

        Raises:
            ValueError: 配置项无效时抛出异常
        """
        if not self.server_url:
            raise ValueError(
                "[RocketChat] 配置项 'server_url' 不能为空。"
                "请在 AstrBot 配置中设置 Rocket.Chat 服务器地址（如 http://localhost:3000）。"
            )

        if not self.username:
            raise ValueError(
                "[RocketChat] 配置项 'username' 不能为空。"
                "请在 AstrBot 配置中设置 Rocket.Chat 用户名。"
            )

        if not self.password:
            raise ValueError(
                "[RocketChat] 配置项 'password' 不能为空。"
                "请在 AstrBot 配置中设置 Rocket.Chat 密码。"
            )

        if self.enable_e2ee and not self.e2ee_password:
            raise ValueError(
                "[RocketChat] 启用 E2EE (enable_e2ee=true) 时必须提供 'e2ee_password'。"
                "E2EE 密码用于加密/解密私钥，请在 Rocket.Chat 网页端设置后填入配置。"
            )

        if self.reconnect_delay < 0:
            raise ValueError(
                f"[RocketChat] 配置项 'reconnect_delay' 必须为非负数，当前值: {self.reconnect_delay}"
            )

        if self.typing_indicator_delay < 0:
            raise ValueError(
                f"[RocketChat] 配置项 'typing_indicator_delay' 必须为非负数，当前值: {self.typing_indicator_delay}"
            )

        if self.remote_media_max_size <= 0:
            raise ValueError(
                f"[RocketChat] 配置项 'remote_media_max_size' 必须为正数，当前值: {self.remote_media_max_size}"
            )

    # ------------------------------------------------------------------ #
    #  Platform 抽象方法实现                                                #
    # ------------------------------------------------------------------ #

    def meta(self) -> PlatformMetadata:
        return self._meta

    async def run(self) -> None:
        """适配器主入口，持续运行并自动重连。"""
        self._running = True
        self._stop_event = asyncio.Event()
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=45.0)
        )

        try:
            # 第一步：REST API 登录，获取 authToken / userId
            await self._rest_login()
            await self._e2ee.initialize()

            # 第二步：外层重连循环
            while self._running:
                try:
                    await self._ws_connect_and_listen()
                except asyncio.CancelledError:
                    # CancelledError 不能吞掉，必须重新抛出
                    raise
                except Exception as exc:
                    if not self._running:
                        break
                    logger.warning(
                        f"[RocketChat] WebSocket 连接断开: {exc!r}，"
                        f"{self.reconnect_delay:.1f}s 后重连..."
                    )
                    # 用 Event 等待，terminate() 可立即打断而不必等满 reconnect_delay
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self.reconnect_delay
                        )
                    except asyncio.TimeoutError:
                        pass
        finally:
            await self._cleanup()

    async def terminate(self) -> None:
        """停止适配器，由 AstrBot 在关闭或禁用时调用。"""
        self._running = False
        # 立即唤醒正在等待重连 sleep 的协程
        if self._stop_event is not None:
            self._stop_event.set()
        await self._cleanup()
        await super().terminate()

    async def send_by_session(
        self,
        session: Any,
        message_chain: MessageChain,
    ) -> None:
        """
        由框架调用，主动向指定会话发送消息（非响应用户消息触发）。

        session.session_id 对应 Rocket.Chat 的 room_id。
        """
        room_id = session.session_id
        await self._send_message_chain(
            room_id, message_chain, getattr(session, "message_id", None)
        )
        # 必须调用：父类上报统计指标
        await super().send_by_session(session, message_chain)

    # ------------------------------------------------------------------ #
    #  内部辅助：清理资源                                                   #
    # ------------------------------------------------------------------ #

    async def _cleanup(self) -> None:
        """关闭 WebSocket 和 HTTP Session。"""
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._http_session and not self._http_session.closed:
            try:
                await self._http_session.close()
            except Exception:
                pass

        for future in list(self._pending_ddp_results.values()):
            if not future.done():
                future.cancel()
        self._pending_ddp_results.clear()

        # 统一取消并等待后台任务完成，避免生命周期外泄漏
        if self._background_tasks:
            for task in list(self._background_tasks):
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            except Exception:
                pass
            self._background_tasks.clear()

    # ------------------------------------------------------------------ #
    #  REST API                                                            #
    # ------------------------------------------------------------------ #

    async def _rest_login(self) -> None:
        """通过 REST API 登录，获取 authToken 和 userId。"""
        url = f"{self.server_url}/api/v1/login"
        async with self._http_session.post(
            url,
            json={"user": self.username, "password": self.password},
        ) as resp:
            data = await resp.json()

        if data.get("status") != "success":
            raise RuntimeError(f"[RocketChat] REST 登录失败: {data}")

        d = data["data"]
        self.auth_token = d["authToken"]
        self.user_id = d["userId"]
        self.bot_username = d["me"]["username"]

        logger.info(
            f"[RocketChat] 登录成功 | 用户: {self.bot_username} | userId: {self.user_id}"
        )

    async def _get_subscriptions(self) -> List[dict]:
        """获取机器人所有订阅的房间列表。"""
        url = f"{self.server_url}/api/v1/subscriptions.get"
        async with self._http_session.get(url, headers=self._get_auth_headers()) as resp:
            data = await resp.json()
        subscriptions = data.get("update", []) if data.get("success") else []
        for sub in subscriptions:
            if not isinstance(sub, dict):
                continue
            room_id = sub.get("rid")
            if not room_id:
                continue
            await self._cache_room_info(
                {
                    "_id": room_id,
                    "t": sub.get("t", self._room_type_cache.get(room_id, "c")),
                    "name": sub.get("name"),
                    "fname": sub.get("fname"),
                    "encrypted": bool(sub.get("encrypted", False)),
                    "e2eKeyId": sub.get("e2eKeyId"),
                }
            )
        return subscriptions

    async def _cache_room_info(self, room: dict) -> None:
        """
        缓存房间信息，使用房间级别的锁防止并发更新导致数据丢失。

        使用独立的锁保护每个房间的缓存更新，避免不同房间之间互相阻塞。

        Args:
            room: 房间信息字典
        """
        room_id = room.get("_id")
        if not room_id:
            return

        # 获取或创建房间级别的锁（setdefault 保证原子性）
        lock = self._room_cache_locks.setdefault(room_id, asyncio.Lock())

        # 使用锁保护整个读-合并-写操作
        async with lock:
            cached = dict(self._room_info_cache.get(room_id, {}))
            cached.update(room)
            self._room_info_cache[room_id] = cached

            room_type = cached.get("t")
            if room_type:
                self._room_type_cache[room_id] = room_type
            room_name = cached.get("name") or cached.get("fname")
            if room_name:
                self._room_name_cache[room_id] = room_name

    async def _get_room_info(self, room_id: str, refresh: bool = False) -> dict:
        if not refresh and room_id in self._room_info_cache:
            return self._room_info_cache[room_id]

        url = f"{self.server_url}/api/v1/rooms.info?roomId={room_id}"
        logger.debug(
            f"[RocketChat][room] fetching room info room_id={room_id!r} url={url}"
        )
        try:
            async with self._http_session.get(
                url, headers=self._get_auth_headers()
            ) as resp:
                data = await resp.json()
            logger.debug(
                f"[RocketChat][room] room info response room_id={room_id!r} data={data}"
            )
            if data.get("success"):
                room = data.get("room", {}) or {}
                await self._cache_room_info(room)
                return self._room_info_cache.get(room_id, room)
        except Exception as exc:
            logger.warning(f"[RocketChat] 获取房间信息失败 room_id={room_id}: {exc}")

        fallback = self._room_info_cache.get(room_id, {"_id": room_id, "t": "c", "encrypted": False})
        await self._cache_room_info(fallback)
        return fallback

    async def _get_room_type(self, room_id: str) -> str:
        """
        获取房间类型，带本地缓存。

        返回值：
          "c"  → 公开频道（channel）
          "p"  → 私有群组（private group）
          "d"  → 私信（direct message）
        """
        room = await self._get_room_info(room_id)
        room_type = room.get("t", "c")
        logger.debug(
            f"[RocketChat][room] resolved room_id={room_id!r} type={room_type!r}"
        )
        return room_type

    def _build_message_link(self, room_id: str, message_id: str) -> str:
        """构造指向原始消息的 Rocket.Chat 深链接（用于引用附件）。"""
        room_type = self._room_type_cache.get(room_id, "c")
        room_name = self._room_name_cache.get(room_id, "")
        
        # 补全链接路径
        if room_type == "c":
            path = f"channel/{room_name}" if room_name else f"channel/{room_id}"
        elif room_type == "p":
            path = f"group/{room_name}" if room_name else f"group/{room_id}"
        elif room_type == "d":
            path = f"direct/{room_id}"
        else:
            path = f"channel/{room_id}"
            
        return f"{self.server_url}/{path}?msg={message_id}"

    async def _fetch_message_by_id(self, msg_id: str) -> Optional[dict]:
        """通过 API 获取指定消息详情。"""
        url = f"{self.server_url}/api/v1/chat.getMessage?msgId={msg_id}"
        try:
            async with self._http_session.get(url, headers=self._get_auth_headers()) as resp:
                data = await resp.json()
                if data.get("success"):
                    message = data.get("message")
                    return await self._maybe_decrypt_incoming_message(message)
                else:
                    logger.debug(f"[RocketChat] 获取消息详情失败: msgId={msg_id} response={data}")
        except asyncio.TimeoutError:
            logger.debug(f"[RocketChat] 获取消息详情超时: msgId={msg_id}")
        except Exception as exc:
            logger.debug(f"[RocketChat] 无法拉取被引用的消息明细 msgId={msg_id}: {exc!r}")
        return None

    async def _maybe_decrypt_incoming_message(self, raw_msg: Optional[dict]) -> Optional[dict]:
        if not isinstance(raw_msg, dict):
            return raw_msg
        if raw_msg.get("t") != "e2e":
            return raw_msg
        return await self._e2ee.maybe_decrypt_message(raw_msg)

    # ------------------------------------------------------------------ #
    #  WebSocket / DDP 协议                                                #
    # ------------------------------------------------------------------ #

    async def _ws_connect_and_listen(self) -> None:
        await self._realtime.ws_connect_and_listen()

    async def _ddp_connect(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await self._realtime.ddp_connect(ws)

    async def _ddp_login(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await self._realtime.ddp_login(ws)

    async def _ddp_subscribe_rooms(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        subscriptions: List[dict],
    ) -> None:
        await self._realtime.ddp_subscribe_rooms(ws, subscriptions)

    async def _ddp_subscribe_user_events(
        self, ws: aiohttp.ClientWebSocketResponse
    ) -> None:
        await self._realtime.ddp_subscribe_user_events(ws)

    async def _ws_listen_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await self._realtime.ws_listen_loop(ws)

    async def _dispatch_ddp(
        self,
        data: dict,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        await self._realtime.dispatch_ddp(data, ws)

    async def _handle_user_notification(
        self,
        data: dict,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        await self._realtime.handle_user_notification(data, ws)

    async def _ddp_call(
        self,
        method: str,
        params: Optional[list[Any]] = None,
        timeout: float = 10.0,
    ) -> Any:
        return await self._realtime.ddp_call(method, params=params, timeout=timeout)

    async def _normalize_media_url(self, media_url: str) -> str:
        """
        将 Rocket.Chat 返回的相对媒体地址补全为绝对 URL，并加上认证参数。

        注意:
        - 媒体 URL 中保留认证参数是为了支持浏览器直接访问和客户端展示
        - 程序下载时应同时使用 _get_auth_headers() 获取 Header（更安全）
        - 这是双重认证策略：Header 用于程序，URL 参数用于浏览器
        """
        url = media_url
        if not (url.startswith("http://") or url.startswith("https://")):
            if url.startswith("/"):
                url = f"{self.server_url}{url}"
            else:
                url = f"{self.server_url}/{url}"

        # 仅对指向自身服务器的链接附加认证 query 参数
        if self.user_id and self.auth_token and self._is_own_server_url(url):
            # 避免重复附加
            if "rc_uid=" not in url and "rc_token=" not in url:
                delimiter = "&" if "?" in url else "?"
                url = f"{url}{delimiter}rc_uid={self.user_id}&rc_token={self.auth_token}"

        return url

    def _is_own_server_url(self, url: str) -> bool:
        """
        判断给定 URL 是否指向当前配置的 Rocket.Chat 服务器。

        通过比较 scheme + netloc（而非裸字符串前缀）判定，避免
        ``http://host`` 与 ``http://hostname`` 这类前缀重叠导致的误判，
        也防止外部链接碰巧以 server_url 开头而被错误地附加认证信息。

        Args:
            url: 待检测的绝对 URL

        Returns:
            True 表示该 URL 指向本服务器
        """
        own = urlparse(self.server_url)
        target = urlparse(url)
        return (
            own.scheme == target.scheme
            and own.netloc == target.netloc
        )

    def _get_auth_headers(self) -> dict:
        """
        获取 Rocket.Chat REST API 认证 Header。

        仅在已登录（持有 userId 与 authToken）时返回有效头；未登录时返回
        空字典，避免向请求里塞入 ``X-Auth-Token: None`` 这类无效值。

        参考: https://developer.rocket.chat/apidocs/authentication-api
        """
        if self.user_id and self.auth_token:
            return {
                "X-Auth-Token": self.auth_token,
                "X-User-Id": self.user_id,
            }
        return {}

    def _classify_file_kind(self, file_obj: dict) -> str:
        return self._media.classify_file_kind(file_obj)

    def _get_all_attachments_recursive(self, payload: dict) -> List[dict]:
        return self._media.get_all_attachments_recursive(payload)

    async def _extract_image_components(self, raw_msg: dict) -> List[Image]:
        return await self._media.extract_image_components(raw_msg)

    async def _extract_file_components(self, raw_msg: dict) -> List[File]:
        return await self._media.extract_file_components(raw_msg)

    async def _extract_record_components(self, raw_msg: dict) -> List[Record]:
        return await self._media.extract_record_components(raw_msg)

    async def _extract_video_components(self, raw_msg: dict) -> List[Video]:
        return await self._media.extract_video_components(raw_msg)

    def _extract_mentions_for_wake(self, raw_msg: dict) -> list[Any]:
        return self._inbound.extract_mentions_for_wake(raw_msg)

    async def _process_incoming_message(self, raw_msg: dict) -> None:
        await self._inbound.process_incoming_message(raw_msg)

    # ------------------------------------------------------------------ #
    #  消息发送方法（供 RocketChatMessageEvent 调用）                       #
    # ------------------------------------------------------------------ #

    async def _post_json_message(self, url: str, payload: dict) -> bool:
        return await self._sender.post_json_message(url, payload)

    async def _send_structured_message(
        self,
        room_id: str,
        text: str = "",
        *,
        attachments: Optional[list[dict[str, Any]]] = None,
        tmid: Optional[str] = None,
        e2e_mentions: Optional[dict[str, Any]] = None,
    ) -> bool:
        return await self._sender.send_structured_message(
            room_id,
            text,
            attachments=attachments,
            tmid=tmid,
            e2e_mentions=e2e_mentions,
        )

    async def _build_explicit_reply_mention(
        self,
        room_id: str,
        mention_username: Optional[str],
    ) -> tuple[str | None, dict[str, Any] | None]:
        return await self._sender.build_explicit_reply_mention(room_id, mention_username)

    async def _should_explicit_reply_mention(self, room_id: str) -> bool:
        return await self._sender.should_explicit_reply_mention(room_id)

    async def send_text(
        self,
        room_id: str,
        text: str,
        tmid: Optional[str] = None,
        mention_username: Optional[str] = None,
    ) -> None:
        await self._sender.send_text(room_id, text, tmid=tmid, mention_username=mention_username)

    async def send_typing(
        self,
        room_id: str,
        flag: bool,
        tmid: Optional[str] = None,
    ) -> None:
        await self._sender.send_typing(room_id, flag, tmid=tmid)

    async def send_with_quote(
        self,
        room_id: str,
        text: str,
        original_msg: dict,
        tmid: Optional[str] = None,
        mention_username: Optional[str] = None,
    ) -> None:
        await self._sender.send_with_quote(
            room_id,
            text,
            original_msg,
            tmid=tmid,
            mention_username=mention_username,
        )

    async def send_image_url(
        self,
        room_id: str,
        image_url: str,
        text: str = "",
        tmid: Optional[str] = None,
    ) -> None:
        await self._sender.send_image_url(room_id, image_url, text=text, tmid=tmid)

    async def send_image_file(
        self,
        room_id: str,
        file_path: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> None:
        await self._sender.send_image_file(room_id, file_path, description=description, tmid=tmid)

    async def send_file(
        self,
        room_id: str,
        file_path: str,
        filename: str | None = None,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> None:
        await self._sender.send_file(
            room_id,
            file_path,
            filename=filename,
            description=description,
            tmid=tmid,
        )

    async def send_remote_media_fallback(
        self,
        room_id: str,
        media_url: str,
        *,
        media_kind: str,
        text: str = "",
        tmid: Optional[str] = None,
    ) -> bool:
        return await self._sender.send_remote_media_fallback(
            room_id,
            media_url,
            media_kind=media_kind,
            text=text,
            tmid=tmid,
        )

    async def _resolve_outbound_media_path(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        return await self._sender.resolve_outbound_media_path(file_ref, default_suffix)

    async def _download_remote_media(
        self,
        url: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        return await self._sender.download_remote_media(url, default_suffix)

    def _decode_base64_media(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        return self._sender.decode_base64_media(file_ref, default_suffix)

    async def _send_message_chain(
        self, room_id: str, message_chain: MessageChain, tmid: Optional[str] = None
    ) -> None:
        await self._sender.send_message_chain(room_id, message_chain, tmid=tmid)
