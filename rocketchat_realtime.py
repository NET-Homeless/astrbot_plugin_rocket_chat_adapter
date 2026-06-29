from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
from astrbot.api import logger

# 导入常量
try:
    from .rocketchat_adapter import (
        DDP_CALL_DEFAULT_TIMEOUT,
        WEBSOCKET_HEARTBEAT_INTERVAL,
        WEBSOCKET_MAX_MSG_SIZE,
    )
except ImportError:
    WEBSOCKET_HEARTBEAT_INTERVAL = 30.0
    WEBSOCKET_MAX_MSG_SIZE = 8 * 1024 * 1024
    DDP_CALL_DEFAULT_TIMEOUT = 10.0


class RocketChatRealtimeBridge:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    async def ws_connect_and_listen(self) -> None:
        ws_url = (
            self.adapter.server_url.replace("https://", "wss://", 1).replace(
                "http://", "ws://", 1
            )
        ) + "/websocket"

        self.adapter._subscribed_rooms.clear()

        # 重连时清理房间缓存，防止长期运行导致缓存无限增长。
        # 断线期间房间状态可能已变化（改名/删除/权限变更），
        # 缓存会在下方 _get_subscriptions() 和 _cache_room_info() 中重建。
        self.adapter._room_info_cache.clear()
        self.adapter._room_type_cache.clear()
        self.adapter._room_name_cache.clear()
        self.adapter._room_cache_locks.clear()
        self.adapter._pending_room_subscriptions.clear()

        async with self.adapter._http_session.ws_connect(
            ws_url,
            heartbeat=WEBSOCKET_HEARTBEAT_INTERVAL,
            max_msg_size=WEBSOCKET_MAX_MSG_SIZE,
        ) as ws:
            self.adapter._ws = ws
            try:
                await self.ddp_connect(ws)
                await self.ddp_login(ws)

                subscriptions = await self.adapter._get_subscriptions()
                await self.ddp_subscribe_rooms(ws, subscriptions)
                await self.ddp_subscribe_user_events(ws)

                logger.info(
                    f"[RocketChat] WebSocket 就绪，共订阅 {len(subscriptions)} 个房间"
                )

                e2ee_task = asyncio.create_task(self.adapter._e2ee.on_ws_ready())
                self.adapter._background_tasks.add(e2ee_task)
                e2ee_task.add_done_callback(self.adapter._background_tasks.discard)

                await self.ws_listen_loop(ws)
            finally:
                self.adapter._ws = None

    async def ddp_connect(self, ws: aiohttp.ClientWebSocketResponse) -> None:
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

    async def ddp_login(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.send_json(
            {
                "msg": "method",
                "method": "login",
                "id": "ddp-login",
                "params": [{"resume": self.adapter.auth_token}],
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

    async def ddp_subscribe_rooms(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        subscriptions: list[dict],
    ) -> None:
        # 房间信息已由 _get_subscriptions() 缓存（run() 在本方法之前调用），
        # 这里只负责发起 stream-room-messages 订阅，不重复写缓存。
        for sub in subscriptions:
            room_id = sub.get("rid")
            if not room_id:
                continue
            await self._subscribe_room_messages(ws, room_id)

    async def ddp_subscribe_user_events(
        self,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        await ws.send_json(
            {
                "msg": "sub",
                "id": f"user-notif-{self.adapter.user_id}",
                "name": "stream-notify-user",
                "params": [f"{self.adapter.user_id}/rooms-changed", False],
            }
        )

    async def ws_listen_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for raw in ws:
            if not self.adapter._running:
                break

            if raw.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(raw.data)
                    await self.dispatch_ddp(data, ws)
                except json.JSONDecodeError:
                    logger.warning(f"[RocketChat] 收到非 JSON 帧: {raw.data[:200]}")
                except Exception as exc:
                    logger.error(
                        f"[RocketChat] 处理 DDP 消息时出错: {exc!r}", exc_info=True
                    )

            elif raw.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                logger.debug(f"[RocketChat] WebSocket 帧类型: {raw.type}")
                break

    async def dispatch_ddp(
        self,
        data: dict,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        msg_type = data.get("msg")
        collection = data.get("collection", "")

        if msg_type == "ping":
            await ws.send_json({"msg": "pong"})

        elif msg_type == "changed":
            if collection == "stream-room-messages":
                args: list[dict] = data.get("fields", {}).get("args", [])
                for raw_msg in args:

                    async def process(msg: dict) -> None:
                        async with self.adapter._message_semaphore:
                            await self.adapter._process_incoming_message(msg)

                    task = asyncio.create_task(process(raw_msg))
                    self.adapter._background_tasks.add(task)
                    task.add_done_callback(self.adapter._background_tasks.discard)

            elif collection == "stream-notify-user":
                await self.handle_user_notification(data, ws)

        elif msg_type == "result":
            result_id = data.get("id", "")
            future = self.adapter._pending_ddp_results.pop(result_id, None)
            method = self.adapter._pending_ddp_methods.pop(result_id, "")
            if future and not future.done():
                future.set_result(data)
            if method:
                error = data.get("error")
                if error:
                    logger.warning(
                        f"[RocketChat] DDP method 调用被服务端拒绝: method={method} id={result_id} error={error}"
                    )
                else:
                    # 成功调用仅在 trace 级别记录，避免日志噪音
                    pass

        elif msg_type == "added":
            pass

        elif msg_type == "ready":
            self._handle_ready(data)

        elif msg_type == "nosub":
            self._handle_nosub(data)

    async def _subscribe_room_messages(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        room_id: str,
    ) -> None:
        sub_id = f"room-{room_id}"
        if room_id in self.adapter._subscribed_rooms:
            return
        if sub_id in self.adapter._pending_room_subscriptions:
            return
        await ws.send_json(
            {
                "msg": "sub",
                "id": sub_id,
                "name": "stream-room-messages",
                "params": [room_id, False],
            }
        )
        self.adapter._pending_room_subscriptions[sub_id] = room_id

    def _handle_ready(self, data: dict[str, Any]) -> None:
        for sub_id in data.get("subs", []) or []:
            room_id = self.adapter._pending_room_subscriptions.pop(sub_id, None)
            if not room_id:
                continue
            self.adapter._subscribed_rooms.add(room_id)
            logger.debug(
                f"[RocketChat] 房间订阅已确认: room_id={room_id!r} sub_id={sub_id!r}"
            )

    def _handle_nosub(self, data: dict[str, Any]) -> None:
        sub_id = data.get("id")
        if not isinstance(sub_id, str):
            return
        room_id = self.adapter._pending_room_subscriptions.pop(sub_id, None)
        if room_id:
            self.adapter._subscribed_rooms.discard(room_id)
            logger.warning(
                f"[RocketChat] 房间订阅失败: room_id={room_id!r} sub_id={sub_id!r} error={data.get('error')}"
            )

    async def handle_user_notification(
        self,
        data: dict,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
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

        if room_id and event_name.endswith("/rooms-changed") and room_payload:
            room_type = room_payload.get("t")
            if isinstance(room_type, str) and room_type:
                await self.adapter._cache_room_info(
                    {
                        "_id": room_id,
                        "t": room_type,
                        "name": room_payload.get("name"),
                        "fname": room_payload.get("fname"),
                        "encrypted": bool(room_payload.get("encrypted", False)),
                        "e2eKeyId": room_payload.get("e2eKeyId"),
                    }
                )
                logger.debug(
                    f"[RocketChat][room] cached from notify room_id={room_id!r} type={room_type!r} event={event_type!r}"
                )

        if (
            event_type == "inserted"
            and room_id
            and room_id not in self.adapter._subscribed_rooms
        ):
            await self._subscribe_room_messages(ws, room_id)
            logger.info(f"[RocketChat] 动态订阅新房间: {room_id}")

    async def ddp_call(
        self,
        method: str,
        params: list[Any] | None = None,
        timeout: float = DDP_CALL_DEFAULT_TIMEOUT,
    ) -> Any:
        if not self.adapter._ws or self.adapter._ws.closed:
            raise RuntimeError("ddp websocket not ready")

        self.adapter._ddp_call_id += 1
        call_id = f"ddp-{self.adapter._ddp_call_id}"
        future = asyncio.get_running_loop().create_future()
        self.adapter._pending_ddp_results[call_id] = future
        self.adapter._pending_ddp_methods[call_id] = method
        try:
            await self.adapter._ws.send_json(
                {
                    "msg": "method",
                    "method": method,
                    "id": call_id,
                    "params": params or [],
                }
            )
            data = await asyncio.wait_for(future, timeout=timeout)
            if data.get("error"):
                raise RuntimeError(data["error"])
            return data.get("result")
        finally:
            self.adapter._pending_ddp_results.pop(call_id, None)
            self.adapter._pending_ddp_methods.pop(call_id, None)
