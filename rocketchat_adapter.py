"""
Rocket.Chat 平台适配器（Platform Adapter）

架构：
  - REST API  → 认证（POST /api/v1/login）、发送消息（POST /api/v1/chat.postMessage）
  - WebSocket → DDP 协议实时接收消息（wss://server/websocket）

依赖：aiohttp
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import tempfile
import time
from asyncio import Queue
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import File, Image, Plain, Record, Reply, Video
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)

from .rocketchat_event import RocketChatMessageEvent


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
    },
    support_streaming_message=False,
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
        # 已订阅房间集合，防止重复订阅导致消息被多次处理
        self._subscribed_rooms: set = set()
        # DDP method 调用 ID 计数器，确保每次调用 ID 唯一
        self._ddp_call_id: int = 0
        # 后台任务强引用集合，防止 Python 3.12+ GC 回收未完成的 task
        self._background_tasks: set[asyncio.Task] = set()
        # 并发处理控制，防止瞬间过多消息导致处理积压
        self._message_semaphore = asyncio.Semaphore(100)

        self._meta = PlatformMetadata(
            name="rocket_chat",
            description="Rocket.Chat 消息平台适配器",
            id=platform_config.get("id", "rocket_chat"),
            support_streaming_message=False,
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

    def _auth_headers(self) -> dict:
        """构造认证请求头。"""
        return {
            "X-Auth-Token": self.auth_token,
            "X-User-Id": self.user_id,
            "Content-Type": "application/json",
        }

    async def _get_subscriptions(self) -> List[dict]:
        """获取机器人所有订阅的房间列表。"""
        url = f"{self.server_url}/api/v1/subscriptions.get"
        async with self._http_session.get(url, headers=self._auth_headers()) as resp:
            data = await resp.json()
        return data.get("update", []) if data.get("success") else []

    async def _get_room_type(self, room_id: str) -> str:
        """
        获取房间类型，带本地缓存。

        返回值：
          "c"  → 公开频道（channel）
          "p"  → 私有群组（private group）
          "d"  → 私信（direct message）
        """
        if room_id in self._room_type_cache:
            logger.debug(
                f"[RocketChat][room] cache hit room_id={room_id!r} type={self._room_type_cache[room_id]!r}"
            )
            return self._room_type_cache[room_id]

        url = f"{self.server_url}/api/v1/rooms.info?roomId={room_id}"
        logger.debug(
            f"[RocketChat][room] fetching room info room_id={room_id!r} url={url}"
        )
        try:
            async with self._http_session.get(
                url, headers=self._auth_headers()
            ) as resp:
                data = await resp.json()
            logger.debug(
                f"[RocketChat][room] room info response room_id={room_id!r} data={data}"
            )
            if data.get("success"):
                room = data.get("room", {})
                room_type = room.get("t", "c")
                self._room_type_cache[room_id] = room_type
                room_name = room.get("name") or room.get("fname")
                if room_name:
                    self._room_name_cache[room_id] = room_name
                logger.debug(
                    f"[RocketChat][room] resolved room_id={room_id!r} type={room_type!r}"
                )
                return room_type
        except Exception as e:
            logger.warning(f"[RocketChat] 获取房间类型失败 room_id={room_id}: {e}")

        logger.debug(f"[RocketChat][room] fallback room_id={room_id!r} type='c'")
        return "c"

    def _build_message_link(self, room_id: str, message_id: str) -> str:
        """构造指向原始消息的 Rocket.Chat 深链接（用于引用附件）。"""
        room_type = self._room_type_cache.get(room_id, "c")
        room_name = self._room_name_cache.get(room_id, "")
        if not room_name:
            return ""
        if room_type == "c":
            path = f"channel/{room_name}"
        elif room_type == "p":
            path = f"group/{room_name}"
        else:
            return ""
        return f"{self.server_url}/{path}?msg={message_id}"

    async def _fetch_message_by_id(self, msg_id: str) -> Optional[dict]:
        """通过 API 获取指定消息详情。"""
        url = f"{self.server_url}/api/v1/chat.getMessage?msgId={msg_id}"
        try:
            async with self._http_session.get(url, headers=self._auth_headers()) as resp:
                data = await resp.json()
                if data.get("success"):
                    return data.get("message")
        except Exception as exc:
            logger.warning(f"[RocketChat] 无法拉取被引用的消息明细 msgId={msg_id}: {exc!r}")
        return None

    # ------------------------------------------------------------------ #
    #  WebSocket / DDP 协议                                                #
    # ------------------------------------------------------------------ #

    async def _ws_connect_and_listen(self) -> None:
        """建立 WebSocket 连接，完成 DDP 握手、认证、订阅，然后进入消息监听循环。"""
        # 将 http(s) 替换为 ws(s)
        ws_url = (
            self.server_url.replace("https://", "wss://", 1).replace(
                "http://", "ws://", 1
            )
        ) + "/websocket"

        # 重连时重置订阅集合，让本次连接重新订阅所有房间
        self._subscribed_rooms.clear()

        async with self._http_session.ws_connect(
            ws_url,
            heartbeat=30.0,  # aiohttp 层面 TCP 心跳
            max_msg_size=8 * 1024 * 1024,  # 限制单条消息最大 8MB 防止超大帧导致内存激增 (DoS风险)
        ) as ws:
            self._ws = ws
            try:
                # DDP 三步握手：connect → login → subscribe
                await self._ddp_connect(ws)
                await self._ddp_login(ws)

                subscriptions = await self._get_subscriptions()
                await self._ddp_subscribe_rooms(ws, subscriptions)
                await self._ddp_subscribe_user_events(ws)

                logger.info(
                    f"[RocketChat] WebSocket 就绪，共订阅 {len(subscriptions)} 个房间"
                )

                # 进入主监听循环
                await self._ws_listen_loop(ws)
            finally:
                self._ws = None

    async def _ddp_connect(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """发送 DDP connect 握手报文并等待 connected 确认。"""
        await ws.send_json(
            {
                "msg": "connect",
                "version": "1",
                "support": ["1"],
            }
        )

        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(raw.data)
            if data.get("msg") == "ping":
                await ws.send_json({"msg": "pong"})
            elif data.get("msg") == "connected":
                logger.debug("[RocketChat] DDP connect 握手成功")
                return

        raise RuntimeError("[RocketChat] DDP connect 未收到 connected 响应")

    async def _ddp_login(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """使用 REST authToken 进行 DDP 认证。"""
        await ws.send_json(
            {
                "msg": "method",
                "method": "login",
                "id": "ddp-login",
                "params": [{"resume": self.auth_token}],
            }
        )

        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(raw.data)
            if data.get("msg") == "ping":
                await ws.send_json({"msg": "pong"})
            elif data.get("msg") == "result" and data.get("id") == "ddp-login":
                if "error" in data:
                    raise RuntimeError(f"[RocketChat] DDP 登录失败: {data['error']}")
                logger.debug("[RocketChat] DDP 登录成功")
                return

        raise RuntimeError("[RocketChat] DDP login 未收到 result 响应")

    async def _ddp_subscribe_rooms(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        subscriptions: List[dict],
    ) -> None:
        """为每个已订阅的房间建立 stream-room-messages 订阅。"""
        for sub in subscriptions:
            room_id = sub.get("rid")
            if not room_id:
                continue
            # 从订阅数据顺带缓存房间类型和名称，减少后续 API 调用
            room_type = sub.get("t")
            if room_type:
                self._room_type_cache[room_id] = room_type
            room_name = sub.get("name") or sub.get("fname")
            if room_name:
                self._room_name_cache[room_id] = room_name
            await ws.send_json(
                {
                    "msg": "sub",
                    "id": f"room-{room_id}",
                    "name": "stream-room-messages",
                    "params": [room_id, False],
                }
            )
            self._subscribed_rooms.add(room_id)

    async def _ddp_subscribe_user_events(
        self, ws: aiohttp.ClientWebSocketResponse
    ) -> None:
        """
        订阅用户级别事件流（stream-notify-user），
        用于感知机器人被加入新房间，从而动态补充订阅。
        """
        await ws.send_json(
            {
                "msg": "sub",
                "id": f"user-notif-{self.user_id}",
                "name": "stream-notify-user",
                "params": [f"{self.user_id}/rooms-changed", False],
            }
        )

    async def _ws_listen_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """持续读取 WebSocket 帧，分发给各处理器。"""
        async for raw in ws:
            if not self._running:
                break

            if raw.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(raw.data)
                    await self._dispatch_ddp(data, ws)
                except json.JSONDecodeError:
                    logger.warning(f"[RocketChat] 收到非 JSON 帧: {raw.data[:200]}")
                except Exception as exc:
                    logger.error(
                        f"[RocketChat] 处理 DDP 消息时出错: {exc!r}",
                        exc_info=True,
                    )

            elif raw.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                logger.debug(f"[RocketChat] WebSocket 帧类型: {raw.type}")
                break

    async def _dispatch_ddp(
        self,
        data: dict,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        """根据 DDP msg 字段将消息路由到对应处理器。"""
        msg_type = data.get("msg")
        collection = data.get("collection", "")

        if msg_type == "ping":
            # 应用层心跳：服务器发 ping，客户端必须回 pong
            await ws.send_json({"msg": "pong"})

        elif msg_type == "changed":
            if collection == "stream-room-messages":
                # 房间新消息推送
                args: List[dict] = data.get("fields", {}).get("args", [])
                for raw_msg in args:
                    # 并发并使用信号量控制上限，避免突发高流量导致 OOM 或过载
                    async def process(msg: dict):
                        async with self._message_semaphore:
                            await self._process_incoming_message(msg)

                    task = asyncio.create_task(process(raw_msg))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)

            elif collection == "stream-notify-user":
                # 用户级别通知（如：被加入新房间）
                await self._handle_user_notification(data, ws)

        elif msg_type == "result":
            # DDP method 调用结果
            result_id = data.get("id", "")
            if result_id.startswith("typing-"):
                error = data.get("error")
                if error:
                    logger.warning(
                        f"[RocketChat] typing method 调用被服务端拒绝: id={result_id} error={error}"
                    )
                else:
                    logger.debug(
                        f"[RocketChat] typing method 调用成功: id={result_id} result={data.get('result')}"
                    )

        elif msg_type == "added":
            # DDP 初始化数据，暂不处理
            pass

        elif msg_type == "ready":
            # 订阅就绪确认，暂不处理
            pass

    async def _handle_user_notification(
        self,
        data: dict,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        """处理 stream-notify-user 事件，动态订阅新加入的房间。"""
        fields = data.get("fields", {})
        event_name = fields.get("eventName", "")
        args: list = fields.get("args", [])
        if not args:
            return

        event_type = args[0] if len(args) > 0 else ""
        room_payload = args[1] if len(args) > 1 and isinstance(args[1], dict) else None
        room_id = ""
        if room_payload:
            room_id = room_payload.get("_id") or room_payload.get("rid") or ""

        if room_id and event_name.endswith("/rooms-changed"):
            room_type = room_payload.get("t")
            if isinstance(room_type, str) and room_type:
                self._room_type_cache[room_id] = room_type
                logger.debug(
                    f"[RocketChat][room] cached from notify room_id={room_id!r} type={room_type!r} event={event_type!r}"
                )

        # 只在真正新加入房间时订阅，updated 仅表示房间有活动，不需要重新订阅
        if (
            event_type == "inserted"
            and room_id
            and room_id not in self._subscribed_rooms
        ):
            await ws.send_json(
                {
                    "msg": "sub",
                    "id": f"room-{room_id}",
                    "name": "stream-room-messages",
                    "params": [room_id, False],
                }
            )
            self._subscribed_rooms.add(room_id)
            logger.info(f"[RocketChat] 动态订阅新房间: {room_id}")

    async def _normalize_media_url(self, media_url: str) -> str:
        """将 Rocket.Chat 返回的相对媒体地址补全为绝对 URL，并加上认证参数。"""
        url = media_url
        if not (url.startswith("http://") or url.startswith("https://")):
            if url.startswith("/"):
                url = f"{self.server_url}{url}"
            else:
                url = f"{self.server_url}/{url}"

        # 仅对指向自身服务器的链接附加认证 query 参数
        if url.startswith(self.server_url) and self.user_id and self.auth_token:
            # 避免重复附加
            if "rc_uid=" not in url and "rc_token=" not in url:
                delimiter = "&" if "?" in url else "?"
                url = f"{url}{delimiter}rc_uid={self.user_id}&rc_token={self.auth_token}"

        return url

    def _classify_file_kind(self, file_obj: dict) -> str:
        """基于 MIME / 文件名 / URL 推断文件类别。"""
        candidates: List[str] = []

        for key in ("type", "mimeType", "contentType"):
            value = file_obj.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)

        for key in ("name", "title", "url", "path", "title_link", "titleLink", "link"):
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

    async def _extract_image_urls(self, raw_msg: dict) -> List[str]:
        """从 Rocket.Chat 多种附件/文件结构中提取图片 URL。"""
        image_urls: List[str] = []

        async def add_image_candidate(candidate: Any, force: bool = False) -> Optional[str]:
            if not candidate:
                return None

            if isinstance(candidate, dict):
                for key in (
                    "url",
                    "path",
                    "image_url",
                    "imageUrl",
                    "title_link",
                    "titleLink",
                    "link",
                ):
                    res = await add_image_candidate(candidate.get(key), force=force)
                    if res:
                        return res
                return None

            if not isinstance(candidate, str):
                return None

            if force:
                return await self._normalize_media_url(candidate)

            guessed, _ = mimetypes.guess_type(candidate.split("?", 1)[0])
            if guessed and guessed.startswith("image/"):
                return await self._normalize_media_url(candidate)
            return None

        for key in (
            "image_url",
            "imageUrl",
            "image",
            "thumb_url",
            "thumbUrl",
            "image_preview",
            "imagePreview",
        ):
            res = await add_image_candidate(raw_msg.get(key), force=True)
            if res and res not in image_urls:
                image_urls.append(res)
                break

        def _get_all_attachments(payload: dict) -> List[dict]:
            res = []
            att_raw = payload.get("attachments", [])
            atts = [att_raw] if isinstance(att_raw, dict) else [a for a in att_raw if isinstance(a, dict)]
            for att in atts:
                if att.get("message_link"):
                    continue
                res.append(att)
                res.extend(_get_all_attachments(att))
            return res

        all_attachments = _get_all_attachments(raw_msg)

        for att in all_attachments:
            mime_type = (
                att.get("image_type") or att.get("type") or att.get("mimeType") or ""
            )
            is_image_attachment = bool(
                att.get("image_dimensions")
            ) or mime_type.startswith("image/")

            for key in (
                "image_url",
                "imageUrl",
                "image",
                "url",
                "path",
                "title_link",
                "titleLink",
                "image_preview",
                "imagePreview",
                "thumb_url",
                "thumbUrl",
            ):
                force = is_image_attachment or key in {
                    "image_url",
                    "imageUrl",
                    "image",
                    "image_preview",
                    "imagePreview",
                    "thumb_url",
                    "thumbUrl",
                }
                res = await add_image_candidate(att.get(key), force=force)
                if res and res not in image_urls:
                    image_urls.append(res)
                    break

        files_raw = raw_msg.get("files", [])
        if isinstance(files_raw, dict):
            files = [files_raw]
        else:
            files = [f for f in files_raw if isinstance(f, dict)]

        for file_obj in files:
            is_image_file = self._classify_file_kind(file_obj) == "image"

            for key in ("url", "path", "title_link", "titleLink", "link"):
                res = await add_image_candidate(file_obj.get(key), force=is_image_file)
                if res and res not in image_urls:
                    image_urls.append(res)
                    break

        for file_key in ("file", "fileUpload"):
            single_file = raw_msg.get(file_key)
            if not isinstance(single_file, dict):
                continue

            is_image_file = self._classify_file_kind(single_file) == "image"

            for key in ("url", "path", "title_link", "titleLink", "link"):
                res = await add_image_candidate(single_file.get(key), force=is_image_file)
                if res and res not in image_urls:
                    image_urls.append(res)
                    break

        for url_obj in raw_msg.get("urls", []):
            if not isinstance(url_obj, dict):
                continue
            meta = url_obj.get("meta") if isinstance(url_obj.get("meta"), dict) else {}
            headers = (
                url_obj.get("headers")
                if isinstance(url_obj.get("headers"), dict)
                else {}
            )
            content_type = (
                meta.get("contentType")
                or headers.get("contentType")
                or headers.get("content-type")
                or ""
            )
            if not str(content_type).startswith("image/"):
                continue
            candidate = url_obj.get("url") or url_obj.get("parsedUrl")
            if candidate:
                image_urls.append(await self._normalize_media_url(candidate))

        deduped_urls: List[str] = []
        for image_url in image_urls:
            if image_url not in deduped_urls:
                deduped_urls.append(image_url)
        return deduped_urls

    async def _extract_media_components(self, raw_msg: dict, target_kind: str) -> List[tuple[str, dict]]:
        """提取特定的媒体组件并验证其 URL"""
        results = []

        async def add_candidate(file_obj: dict) -> None:
            if self._classify_file_kind(file_obj) != target_kind:
                return
            for key in ("url", "path", "title_link", "titleLink", "link"):
                value = file_obj.get(key)
                if value:
                    url = await self._normalize_media_url(value)
                    results.append((url, file_obj))
                    break

        def _get_all_attachments(payload: dict) -> List[dict]:
            res = []
            att_raw = payload.get("attachments", [])
            atts = [att_raw] if isinstance(att_raw, dict) else [a for a in att_raw if isinstance(a, dict)]
            for att in atts:
                if att.get("message_link"):
                    continue
                res.append(att)
                res.extend(_get_all_attachments(att))
            return res

        for context in [raw_msg] + _get_all_attachments(raw_msg):
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
            
            # For attachments acting as media directly (like audio_url/video_url via title_link)
            if context != raw_msg:
                await add_candidate(context)

        return results

    async def _extract_file_components(self, raw_msg: dict) -> List[File]:
        """从 Rocket.Chat 文件结构中提取严格意义上的普通文件组件。"""
        candidates = await self._extract_media_components(raw_msg, "file")
        deduped: List[File] = []
        seen: set[tuple[str, str]] = set()
        for url, file_obj in candidates:
            name = file_obj.get("name") or file_obj.get("title") or "attachment"
            key = (name, url)
            if key not in seen:
                seen.add(key)
                deduped.append(File(name=name, url=url))
        return deduped

    async def _extract_record_components(self, raw_msg: dict) -> List[Record]:
        """从 Rocket.Chat 文件结构中提取语音组件。"""
        candidates = await self._extract_media_components(raw_msg, "audio")
        deduped: List[Record] = []
        seen: set[str] = set()
        for url, _ in candidates:
            if url not in seen:
                seen.add(url)
                deduped.append(Record.fromURL(url))
        return deduped

    async def _extract_video_components(self, raw_msg: dict) -> List[Video]:
        """从 Rocket.Chat 文件结构中提取视频组件。"""
        candidates = await self._extract_media_components(raw_msg, "video")
        deduped: List[Video] = []
        seen: set[str] = set()
        for url, _ in candidates:
            if url not in seen:
                seen.add(url)
                deduped.append(Video.fromURL(url))
        return deduped

    async def _process_incoming_message(self, raw_msg: dict) -> None:
        """
        将 Rocket.Chat 原始消息转换为 AstrBotMessage，构造事件并提交队列。
        """
        try:
            # ---- 过滤规则 ----
            # 1. 系统消息（有 t 字段，如用户加入/离开通知）
            if raw_msg.get("t"):
                return

            # 2. 机器人自身发出的消息
            if raw_msg.get("u", {}).get("_id") == self.user_id:
                logger.debug("[RocketChat][IN] skip self message")
                return

            # 3. 空消息（无文本且无任何可处理媒体）
            # --- 构建消息组件流 (按时序排列文本和媒体，完美兼容多模态大模型的视觉理解) ---
            import re
            
            def _get_all_attachments_tmp(payload: dict) -> List[dict]:
                res = []
                att_raw = payload.get("attachments", [])
                atts = [att_raw] if isinstance(att_raw, dict) else [a for a in att_raw if isinstance(a, dict)]
                for att in atts:
                    res.append(att)
                    res.extend(_get_all_attachments_tmp(att))
                return res

            seen_quote_ids = set()

            async def _build_components_recursively(current_payload: dict, current_depth: int = 0, max_depth: int = 3) -> list:
                """将原始附表及正文转化为 Component 流，使用 Reply 组件标准化引用"""
                chain = []
                if current_depth >= max_depth:
                    return chain

                msg_text = current_payload.get("msg", "").strip()
                
                # --- 提取自身的全部附件 ---
                local_images = await self._extract_image_urls(current_payload)
                local_recs = await self._extract_record_components(current_payload)
                local_vids = await self._extract_video_components(current_payload)
                local_files = await self._extract_file_components(current_payload)

                # ---- 只在 depth=0 时处理引用，转为 Reply 组件 ----
                if current_depth == 0:
                    # 兼容多种引用格式：
                    # 1. [ ](https://.../?msg=123)
                    # 2. [引用文本](https://.../?msg=123)
                    # 3. https://.../?msg=123
                    pattern = re.compile(r"(?:\[.*?\]\()?https?://[^\s)]+\?msg=([a-zA-Z0-9]+)[^\s)]*(?:\))?")
                    
                    explicit_quote_ids = []
                    implicit_quote_ids = []
                    
                    # 从正文链接提取显式引用
                    for match in pattern.finditer(msg_text):
                        q_id = match.group(1)
                        if q_id not in explicit_quote_ids:
                            explicit_quote_ids.append(q_id)
                    
                    # 从 attachment message_link 提取隐式引用
                    for att in _get_all_attachments_tmp(current_payload):
                        link = att.get("message_link") or ""
                        if "msg=" in link:
                            msg_id = link.rsplit("msg=", 1)[-1].split("&")[0]
                            if msg_id and msg_id not in implicit_quote_ids:
                                implicit_quote_ids.append(msg_id)

                    # --- 先处理显式引用（正文中出现的）---
                    for q_id in explicit_quote_ids:
                        if q_id not in seen_quote_ids:
                            seen_quote_ids.add(q_id)
                            q_msg = await self._fetch_message_by_id(q_id)
                            if q_msg:
                                # 递归构建被引用消息的组件（仅 depth>0，不包含引用本身）
                                q_components = await _build_components_recursively(q_msg, current_depth + 1, max_depth)
                                if q_components:
                                    # 获取引用消息的元数据
                                    q_sender_id = q_msg.get("u", {}).get("_id", "")
                                    q_sender_name = q_msg.get("u", {}).get("name") or q_msg.get("u", {}).get("username", "")
                                    q_ts_raw = q_msg.get("ts")
                                    if isinstance(q_ts_raw, dict):
                                        q_timestamp = int(q_ts_raw.get("$date", time.time() * 1000) / 1000)
                                    else:
                                        q_timestamp = int(time.time())
                                    
                                    # 提取引用消息的纯文本
                                    q_msg_text = "".join([c.text for c in q_components if isinstance(c, Plain)]).strip()
                                    
                                    # 创建 Reply 组件
                                    reply_comp = Reply(
                                        id=q_id,
                                        chain=q_components,
                                        sender_id=q_sender_id,
                                        sender_nickname=q_sender_name,
                                        time=q_timestamp,
                                        message_str=q_msg_text,
                                    )
                                    chain.append(reply_comp)
                    
                    # --- 再处理隐式引用（仅在 attachment 中的）---
                    for q_id in implicit_quote_ids:
                        if q_id not in seen_quote_ids:
                            seen_quote_ids.add(q_id)
                            q_msg = await self._fetch_message_by_id(q_id)
                            if q_msg:
                                q_components = await _build_components_recursively(q_msg, current_depth + 1, max_depth)
                                if q_components:
                                    q_sender_id = q_msg.get("u", {}).get("_id", "")
                                    q_sender_name = q_msg.get("u", {}).get("name") or q_msg.get("u", {}).get("username", "")
                                    q_ts_raw = q_msg.get("ts")
                                    if isinstance(q_ts_raw, dict):
                                        q_timestamp = int(q_ts_raw.get("$date", time.time() * 1000) / 1000)
                                    else:
                                        q_timestamp = int(time.time())
                                    
                                    q_msg_text = "".join([c.text for c in q_components if isinstance(c, Plain)]).strip()
                                    
                                    reply_comp = Reply(
                                        id=q_id,
                                        chain=q_components,
                                        sender_id=q_sender_id,
                                        sender_nickname=q_sender_name,
                                        time=q_timestamp,
                                        message_str=q_msg_text,
                                    )
                                    chain.insert(0, reply_comp)  # 隐式引用一般出现在头部
                    
                    # --- 清理正文中的引用链接，保留纯文本 ---
                    cleaned_msg_text = pattern.sub("", msg_text).strip()
                    if cleaned_msg_text:
                        chain.append(Plain(text=cleaned_msg_text))
                else:
                    # ---- depth > 0：仅返回消息自身内容，不处理引用 ----
                    if msg_text:
                        chain.append(Plain(text=msg_text))

                # --- 最后铺设自身的媒体内容 ---
                if local_images or local_recs or local_vids or local_files:
                    for i in local_images:
                        chain.append(Image.fromURL(i))
                    chain.extend(local_recs)
                    chain.extend(local_vids)
                    chain.extend(local_files)

                return chain

            components = await _build_components_recursively(raw_msg)
            
            # 用于快速获取最终平摊出来的纯文本，用于指令判断和日志
            msg_text = "".join([c.text for c in components if isinstance(c, Plain)]).strip()

            if not msg_text and not [c for c in components if not isinstance(c, Plain)]:
                logger.debug("[RocketChat][IN] skip empty/unsupported message")
                return

            # ---- 基本字段提取 ----
            room_id: str = raw_msg.get("rid", "")
            sender_id: str = raw_msg.get("u", {}).get("_id", "")
            sender_username: str = raw_msg.get("u", {}).get("username", "")
            sender_name: str = raw_msg.get("u", {}).get("name") or sender_username
            thread_id: Optional[str] = raw_msg.get("tmid")
            
            ts_raw = raw_msg.get("ts")
            if isinstance(ts_raw, dict):
                timestamp = int(ts_raw.get("$date", time.time() * 1000) / 1000)
            else:
                timestamp = int(time.time())

            room_type = await self._get_room_type(room_id)
            msg_type = (
                MessageType.FRIEND_MESSAGE
                if room_type == "d"
                else MessageType.GROUP_MESSAGE
            )

            # ---- 构造 AstrBotMessage ----
            abm = AstrBotMessage()
            abm.type = msg_type
            abm.self_id = self.user_id
            abm.session_id = room_id
            abm.message_id = raw_msg.get("_id", "")
            abm.sender = MessageMember(user_id=sender_id, nickname=sender_name)
            abm.message = components
            abm.message_str = msg_text
            abm.raw_message = raw_msg
            abm.timestamp = timestamp
            abm.group = None

            if msg_type == MessageType.GROUP_MESSAGE:
                abm.group = Group(group_id=room_id)

            # ---- 检测 @mention，决定是否唤醒 AstrBot ----
            bot_mentioned = False
            mentions = raw_msg.get("mentions", [])
            if isinstance(mentions, list):
                for m in mentions:
                    if isinstance(m, dict) and (
                        m.get("_id") == self.user_id or m.get("username") == self.bot_username
                    ):
                        bot_mentioned = True
                        break
            
            if bot_mentioned:
                # 遍地清理 @botusername，绝对不可以把全局替换后的纯文本重新塞进单一组件，这样会破坏多模态组件流时序！
                bot_mentions = [f"@{self.bot_username} ", f"@{self.bot_username}"]
                new_msg_text = ""
                for comp in abm.message:
                    if isinstance(comp, Plain):
                        for bm in bot_mentions:
                            comp.text = comp.text.replace(bm, "")
                        new_msg_text += comp.text
                
                abm.message_str = new_msg_text.strip()
                logger.debug(
                    f"[RocketChat][IN] bot mentioned, clean_text={abm.message_str!r}"
                )

            # ---- 判断回复场景 ----
            # is_thread_msg: 收到的消息本身就在线程里（有 tmid）
            is_thread_msg = bool(raw_msg.get("tmid"))
            # 频道 @mention 且不在线程里 → 引用原消息回复到大厅
            should_quote = (
                msg_type == MessageType.GROUP_MESSAGE
                and bot_mentioned
                and not is_thread_msg
            )

            # ---- 构造平台事件 ----
            event = RocketChatMessageEvent(
                message_str=abm.message_str,
                message_obj=abm,
                platform_meta=self.meta(),
                session_id=abm.session_id,
                room_id=room_id,
                thread_id=thread_id,  # 线程消息时为父消息 ID，否则 None
                quote_original=should_quote,
                adapter=self,
            )
            # 群聊中 @mention 触发唤醒；私信始终处理
            if bot_mentioned or msg_type == MessageType.FRIEND_MESSAGE:
                event.is_at_or_wake_command = True

            # 群聊 / 线程中仅在 @bot 时启用延迟 typing；
            # 私聊则收到消息后直接启用延迟 typing。
            if (
                msg_type == MessageType.GROUP_MESSAGE and bot_mentioned
            ) or msg_type == MessageType.FRIEND_MESSAGE:
                event.start_typing_indicator()

            logger.debug(
                "[RocketChat][IN] → commit type=%s room=%r msg=%r wake=%s"
                % (
                    "DM" if msg_type == MessageType.FRIEND_MESSAGE else "Group",
                    room_id,
                    (abm.message_str[:60] + "…")
                    if len(abm.message_str) > 60
                    else abm.message_str,
                    event.is_at_or_wake_command,
                )
            )
            self.commit_event(event)
        except Exception as exc:
            logger.error(
                f"[RocketChat][IN] unhandled processing error: {exc!r}", exc_info=True
            )
            raise

    # ------------------------------------------------------------------ #
    #  消息发送方法（供 RocketChatMessageEvent 调用）                       #
    # ------------------------------------------------------------------ #

    async def send_text(
        self,
        room_id: str,
        text: str,
        tmid: Optional[str] = None,
    ) -> None:
        """
        发送纯文本消息。

        :param room_id: 目标房间 ID
        :param text:    消息正文
        :param tmid:    可选，回复的线程消息 ID（创建/追加线程）
        """
        payload: dict = {"roomId": room_id, "text": text}
        if tmid:
            payload["tmid"] = tmid

        url = f"{self.server_url}/api/v1/chat.postMessage"
        try:
            async with self._http_session.post(
                url, json=payload, headers=self._auth_headers()
            ) as resp:
                data = await resp.json()
                if not data.get("success"):
                    logger.error(f"[RocketChat] 发送文本失败: {data}")
        except Exception as exc:
            logger.error(f"[RocketChat] 发送文本异常: {exc!r}")

    async def send_typing(self, room_id: str, flag: bool) -> None:
        """
        通过 Rocket.Chat Realtime API 发送 typing 状态。

        :param room_id: 目标房间 ID
        :param flag:    True 表示正在输入，False 表示结束输入
        """
        if not self._ws or self._ws.closed or not self.bot_username:
            logger.warning(
                f"[RocketChat] typing 跳过: ws={self._ws is not None and not getattr(self._ws, 'closed', True)} bot_username={self.bot_username!r}"
            )
            return

        try:
            logger.debug(
                f"[RocketChat] send typing room_id={room_id!r} user={self.bot_username!r} flag={flag}"
            )

            # 使用现代 user-activity API，确保兼容性
            # 每次 method 调用使用唯一的 DDP ID
            self._ddp_call_id += 1

            await self._ws.send_json(
                {
                    "msg": "method",
                    "method": "stream-notify-room",
                    "id": f"typing-{self._ddp_call_id}",
                    "params": [f"{room_id}/user-activity", self.bot_username, ["user-typing"] if flag else []],
                }
            )
        except Exception as exc:
            logger.warning(
                f"[RocketChat] 发送 typing 状态失败 room_id={room_id!r} flag={flag}: {exc!r}"
            )

    async def send_with_quote(
        self,
        room_id: str,
        text: str,
        original_msg: dict,
        tmid: Optional[str] = None,
    ) -> None:
        """
        发送带引用原始消息的回复（频道 @mention 场景）。

        :param room_id:      目标房间 ID
        :param text:         机器人回复正文
        :param original_msg: 被引用的原始消息 raw_msg dict
        :param tmid:         可选线程 ID
        """
        msg_id = original_msg.get("_id", "")
        link = self._build_message_link(room_id, msg_id)

        # 使用 Rocket.Chat 原生引用格式：[ ](链接) 后接回复内容
        if link:
            quote_text = f"[ ]({link}) {text}"
        else:
            quote_text = text

        payload: dict = {
            "roomId": room_id,
            "text": quote_text,
        }
        if tmid:
            payload["tmid"] = tmid

        url = f"{self.server_url}/api/v1/chat.postMessage"
        try:
            async with self._http_session.post(
                url, json=payload, headers=self._auth_headers()
            ) as resp:
                data = await resp.json()
                if not data.get("success"):
                    logger.error(f"[RocketChat] 引用回复失败: {data}")
        except Exception as exc:
            logger.error(f"[RocketChat] 引用回复异常: {exc!r}")

    async def send_image_url(
        self,
        room_id: str,
        image_url: str,
        text: str = "",
        tmid: Optional[str] = None,
    ) -> None:
        """
        通过 URL 发送图片消息（使用 attachments 形式）。

        :param room_id:   目标房间 ID
        :param image_url: 图片公开 URL
        :param text:      可选的消息正文
        """
        payload = {
            "roomId": room_id,
            "text": text,
            "attachments": [{"image_url": image_url}],
        }
        if tmid:
            payload["tmid"] = tmid
        url = f"{self.server_url}/api/v1/chat.postMessage"
        try:
            async with self._http_session.post(
                url, json=payload, headers=self._auth_headers()
            ) as resp:
                data = await resp.json()
                if not data.get("success"):
                    logger.error(f"[RocketChat] 发送图片 URL 失败: {data}")
        except Exception as exc:
            logger.error(f"[RocketChat] 发送图片 URL 异常: {exc!r}")

    def _infer_upload_content_type(self, file_path: str, filename: str) -> str:
        """推断上传文件 MIME 类型。"""
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

    async def send_image_file(
        self,
        room_id: str,
        file_path: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> None:
        """
        上传本地图片文件到指定房间。

        :param room_id:     目标房间 ID
        :param file_path:   本地文件路径
        :param description: 可选描述文字
        """
        url = f"{self.server_url}/api/v1/rooms.upload/{room_id}"
        # 上传不能带 Content-Type: application/json，只需认证头
        headers = {
            "X-Auth-Token": self.auth_token,
            "X-User-Id": self.user_id,
        }
        try:
            with open(file_path, "rb") as fp:
                filename = os.path.basename(file_path) or "image.png"
                form = aiohttp.FormData()
                content_type = self._infer_upload_content_type(file_path, filename)
                form.add_field(
                    "file",
                    fp,
                    filename=filename,
                    content_type=content_type,
                )
                if description:
                    form.add_field("description", description)
                if tmid:
                    form.add_field("tmid", tmid)

                async with self._http_session.post(
                    url, data=form, headers=headers
                ) as resp:
                    data = await resp.json()
                    if not data.get("success"):
                        logger.error(f"[RocketChat] 上传图片失败: {data}")
        except FileNotFoundError:
            logger.error(f"[RocketChat] 图片文件不存在: {file_path}")
        except Exception as exc:
            logger.error(f"[RocketChat] 上传图片异常: {exc!r}")

    async def send_file(
        self,
        room_id: str,
        file_path: str,
        filename: str | None = None,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> None:
        """上传任意本地文件到指定房间。"""
        url = f"{self.server_url}/api/v1/rooms.upload/{room_id}"
        headers = {
            "X-Auth-Token": self.auth_token,
            "X-User-Id": self.user_id,
        }
        try:
            with open(file_path, "rb") as fp:
                resolved_name = filename or os.path.basename(file_path) or "attachment"
                form = aiohttp.FormData()
                content_type = self._infer_upload_content_type(file_path, resolved_name)
                form.add_field(
                    "file",
                    fp,
                    filename=resolved_name,
                    content_type=content_type,
                )
                if description:
                    form.add_field("description", description)
                if tmid:
                    form.add_field("tmid", tmid)

                async with self._http_session.post(
                    url, data=form, headers=headers
                ) as resp:
                    data = await resp.json()
                    if not data.get("success"):
                        logger.error(f"[RocketChat] 上传文件失败: {data}")
        except FileNotFoundError:
            logger.error(f"[RocketChat] 文件不存在: {file_path}")
        except Exception as exc:
            logger.error(f"[RocketChat] 上传文件异常: {exc!r}")

    async def _resolve_outbound_media_path(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        """将远端/本地/Base64 媒体引用解析为可上传的本地路径。"""
        if file_ref.startswith("http://") or file_ref.startswith("https://"):
            return await self._download_remote_media(file_ref, default_suffix)
        if file_ref.startswith("base64://"):
            return self._decode_base64_media(file_ref, default_suffix)

        local_path = file_ref.replace("file:///", "").replace("file://", "")
        return (local_path or None, None)

    async def _download_remote_media(
        self,
        url: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            logger.warning(f"[RocketChat] 拒绝下载不支持的媒体协议: {url}")
            return None, None

        filename = os.path.basename(parsed.path)
        _, ext = os.path.splitext(filename)
        suffix = ext if ext else default_suffix
        try:
            async with self._http_session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                allow_redirects=True,
                max_redirects=3,
            ) as resp:
                if resp.status >= 400:
                    logger.error(f"[RocketChat] 下载媒体失败 {resp.status}: {url}")
                    return None, None

                content_length = resp.content_length
                if (
                    content_length is not None
                    and content_length > self.remote_media_max_size
                ):
                    logger.error(
                        f"[RocketChat] 下载媒体失败，文件过大: {content_length} > {self.remote_media_max_size} ({url})"
                    )
                    return None, None

                raw = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > self.remote_media_max_size:
                        logger.error(
                            f"[RocketChat] 下载媒体失败，文件超过限制: {len(raw)} > {self.remote_media_max_size} ({url})"
                        )
                        return None, None
        except Exception as exc:
            logger.error(f"[RocketChat] 下载媒体异常: {exc!r}")
            return None, None

        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            tmp.write(raw)
            tmp.close()
            return tmp.name, lambda: os.unlink(tmp.name)
        except Exception:
            tmp.close()
            os.unlink(tmp.name)
            raise

    def _decode_base64_media(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        try:
            raw = base64.b64decode(file_ref[len("base64://") :])
        except Exception as exc:
            logger.error(f"[RocketChat] Base64 媒体处理失败: {exc!r}")
            return None, None

        tmp = tempfile.NamedTemporaryFile(suffix=default_suffix, delete=False)
        try:
            tmp.write(raw)
            tmp.close()
            return tmp.name, lambda: os.unlink(tmp.name)
        except Exception:
            tmp.close()
            os.unlink(tmp.name)
            raise

    async def _send_message_chain(
        self, room_id: str, message_chain: MessageChain, tmid: Optional[str] = None
    ) -> None:
        """
        将 MessageChain 发送到指定房间（内部复用方法）。
        """
        text_parts: List[str] = []

        for comp in message_chain.chain:
            if isinstance(comp, Plain):
                text_parts.append(comp.text)
            elif isinstance(comp, (Image, File, Record, Video)):
                if text_parts:
                    await self.send_text(room_id, "".join(text_parts), tmid)
                    text_parts.clear()

                if isinstance(comp, Image):
                    file_ref: str = comp.file or ""
                    if file_ref.startswith("http"):
                        await self.send_image_url(room_id, file_ref, tmid=tmid)
                    else:
                        local_path = file_ref.replace("file:///", "").replace("file://", "")
                        if local_path:
                            await self.send_image_file(room_id, local_path, tmid=tmid)

                elif isinstance(comp, File):
                    file_ref = comp.file or getattr(comp, "url", None) or ""
                    if file_ref.startswith("http://") or file_ref.startswith("https://"):
                        await self.send_text(
                            room_id,
                            f"{comp.name}: {file_ref}" if getattr(comp, "name", None) else file_ref,
                            tmid,
                        )
                    else:
                        local_path = file_ref.replace("file:///", "").replace("file://", "")
                        if local_path:
                            await self.send_file(room_id, local_path, filename=getattr(comp, "name", None), tmid=tmid)

                elif isinstance(comp, (Record, Video)):
                    file_ref = comp.file or getattr(comp, "url", None) or ""
                    suffix = ".mp4" if isinstance(comp, Video) else ".ogg"
                    media_path, cleanup = await self._resolve_outbound_media_path(file_ref, suffix)
                    if media_path:
                        try:
                            await self.send_file(room_id, media_path, tmid=tmid)
                        finally:
                            if cleanup:
                                cleanup()
            else:
                fallback = str(comp)
                if fallback:
                    text_parts.append(fallback)

        if text_parts:
            await self.send_text(room_id, "".join(text_parts), tmid)
