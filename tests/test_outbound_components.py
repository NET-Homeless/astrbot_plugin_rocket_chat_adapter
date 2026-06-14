from __future__ import annotations

import unittest

from tests._bootstrap import install_astrbot_stubs

install_astrbot_stubs()

from astrbot.api.event import MessageChain  # noqa: E402
from astrbot.api.message_components import Forward, Image, Node, Nodes, Plain, Reply  # noqa: E402
from astrbot.api.platform import AstrBotMessage, MessageType, PlatformMetadata  # noqa: E402
from astrbot_plugin_rocket_chat_adapter.rocketchat_event import (  # noqa: E402
    RocketChatMessageEvent,
)
from astrbot_plugin_rocket_chat_adapter.rocketchat_sender import (  # noqa: E402
    RocketChatSenderBridge,
)


class _DummyMessage(AstrBotMessage):
    def __init__(self) -> None:
        self.raw_message = {"u": {"username": "alice"}}
        self.self_id = "bot-id"
        self.type = MessageType.GROUP_MESSAGE
        self.message = []


class _DummyEventAdapter:
    def __init__(self) -> None:
        self.bot_username = "bot"
        self.sent_texts: list[tuple[str, str, str | None, str | None]] = []
        self.sent_quotes: list[tuple[str, str, dict, str | None, str | None]] = []
        self.typing_calls: list[tuple[str, bool, str | None]] = []

    async def send_typing(self, room_id: str, flag: bool, tmid: str | None = None) -> None:
        self.typing_calls.append((room_id, flag, tmid))

    async def _should_explicit_reply_mention(self, room_id: str) -> bool:
        return False

    async def send_text(
        self,
        room_id: str,
        text: str,
        tmid: str | None = None,
        mention_username: str | None = None,
    ) -> None:
        self.sent_texts.append((room_id, text, tmid, mention_username))

    async def send_with_quote(
        self,
        room_id: str,
        text: str,
        original_msg: dict,
        tmid: str | None = None,
        mention_username: str | None = None,
    ) -> None:
        self.sent_quotes.append((room_id, text, original_msg, tmid, mention_username))


class _DummySenderAdapter:
    def __init__(self) -> None:
        self.sent_texts: list[tuple[str, str, str | None]] = []
        self.sent_quotes: list[tuple[str, str, dict, str | None]] = []
        self.messages_by_id: dict[str, dict] = {}

    async def _fetch_message_by_id(self, msg_id: str) -> dict | None:
        return self.messages_by_id.get(msg_id)

    def _build_message_link(self, room_id: str, msg_id: str) -> str:
        return ""


class _DummyE2EE:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def build_send_message(
        self,
        room_id: str,
        text: str = "",
        attachments: list[dict] | None = None,
        tmid: str | None = None,
        e2e_mentions: dict | None = None,
    ) -> dict:
        self.calls.append(
            {
                "room_id": room_id,
                "text": text,
                "attachments": attachments,
                "tmid": tmid,
                "e2e_mentions": e2e_mentions,
            }
        )
        return {
            "message": {
                "rid": room_id,
                "t": "e2e",
                "e2e": "pending",
                "content": {"stub": "encrypted"},
            }
        }


class _DummyEncryptedAdapter(_DummySenderAdapter):
    server_url = "https://chat.example.com"

    def __init__(self) -> None:
        super().__init__()
        self._e2ee = _DummyE2EE()
        self.posted_json: list[tuple[str, dict]] = []

    async def _get_room_info(self, room_id: str) -> dict:
        return {"_id": room_id, "t": "p", "encrypted": True}


class _DummyEncryptedEventAdapter(_DummyEncryptedAdapter):
    bot_username = "bot"

    def __init__(self) -> None:
        super().__init__()
        self._sender = RocketChatSenderBridge(self)

        async def post_json_message(url: str, payload: dict) -> bool:
            self.posted_json.append((url, payload))
            return True

        self._sender.post_json_message = post_json_message  # type: ignore[method-assign]

    async def send_typing(self, room_id: str, flag: bool, tmid: str | None = None) -> None:
        pass

    async def _should_explicit_reply_mention(self, room_id: str) -> bool:
        return await self._sender.should_explicit_reply_mention(room_id)

    async def send_text(
        self,
        room_id: str,
        text: str,
        tmid: str | None = None,
        mention_username: str | None = None,
    ) -> None:
        await self._sender.send_text(
            room_id,
            text,
            tmid=tmid,
            mention_username=mention_username,
        )

    async def send_with_quote(
        self,
        room_id: str,
        text: str,
        original_msg: dict,
        tmid: str | None = None,
        mention_username: str | None = None,
    ) -> None:
        await self._sender.send_with_quote(
            room_id,
            text,
            original_msg,
            tmid=tmid,
            mention_username=mention_username,
        )


class RocketChatOutboundComponentTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_send_renders_nodes_as_readable_markdown(self) -> None:
        adapter = _DummyEventAdapter()
        event = RocketChatMessageEvent(
            message_str="",
            message_obj=_DummyMessage(),
            platform_meta=PlatformMetadata(
                name="rocket_chat",
                description="Rocket.Chat",
                id="rocket_chat",
            ),
            session_id="room-1",
            room_id="room-1",
            thread_id=None,
            adapter=adapter,
        )
        chain = MessageChain()
        chain.chain.extend(
            [
                Plain(text="收到："),
                Nodes(
                    nodes=[
                        Node(name="Alice", uin="10001", content=[Plain(text="第一条")]),
                        Node(
                            name="Bob",
                            uin="10002",
                            content=[
                                Plain(text="看图"),
                                Image.fromURL("https://example.com/a.png"),
                            ],
                        ),
                    ]
                ),
            ]
        )

        await event.send(chain)

        self.assertEqual(len(adapter.sent_texts), 1)
        sent_text = adapter.sent_texts[0][1]
        self.assertIn("收到：", sent_text)
        self.assertIn("【合并转发消息，共 2 条】", sent_text)
        self.assertIn("1. Alice(10001): 第一条", sent_text)
        self.assertIn("2. Bob(10002): 看图[图片: https://example.com/a.png]", sent_text)
        self.assertNotIn("type=", sent_text)
        self.assertNotIn("Nodes(", sent_text)

    async def test_sender_bridge_renders_forward_id_without_object_string(self) -> None:
        adapter = _DummySenderAdapter()
        sender = RocketChatSenderBridge(adapter)

        async def send_text(room_id: str, text: str, tmid: str | None = None) -> None:
            adapter.sent_texts.append((room_id, text, tmid))

        sender.send_text = send_text  # type: ignore[method-assign]

        chain = MessageChain()
        chain.chain.extend([Plain(text="这里有转发\n"), Forward(id="forward-message-id")])

        await sender.send_message_chain("room-1", chain)

        self.assertEqual(len(adapter.sent_texts), 1)
        sent_text = adapter.sent_texts[0][1]
        self.assertIn("这里有转发", sent_text)
        self.assertIn("【合并转发消息】", sent_text)
        self.assertIn("转发消息 ID：forward-message-id", sent_text)
        self.assertNotIn("type=", sent_text)
        self.assertNotIn("Forward(", sent_text)

    async def test_sender_bridge_preserves_nodes_in_reply_text(self) -> None:
        adapter = _DummySenderAdapter()
        adapter.messages_by_id["reply-1"] = {"_id": "reply-1", "rid": "room-1", "msg": "old"}
        sender = RocketChatSenderBridge(adapter)

        async def send_with_quote(
            room_id: str,
            text: str,
            original_msg: dict,
            tmid: str | None = None,
            mention_username: str | None = None,
        ) -> None:
            adapter.sent_quotes.append((room_id, text, original_msg, tmid))

        sender.send_with_quote = send_with_quote  # type: ignore[method-assign]

        chain = MessageChain()
        chain.chain.extend(
            [
                Reply(id="reply-1"),
                Plain(text="引用里的转发："),
                Nodes(nodes=[Node(name="Alice", uin="10001", content=[Plain(text="正文")])]),
            ]
        )

        await sender.send_message_chain("room-1", chain)

        self.assertEqual(len(adapter.sent_quotes), 1)
        sent_text = adapter.sent_quotes[0][1]
        self.assertIn("引用里的转发：", sent_text)
        self.assertIn("【合并转发消息，共 1 条】", sent_text)
        self.assertIn("Alice(10001): 正文", sent_text)
        self.assertNotIn("type=", sent_text)

    async def test_sender_bridge_encrypts_rendered_nodes_in_e2ee_room(self) -> None:
        adapter = _DummyEncryptedAdapter()
        sender = RocketChatSenderBridge(adapter)

        async def post_json_message(url: str, payload: dict) -> bool:
            adapter.posted_json.append((url, payload))
            return True

        sender.post_json_message = post_json_message  # type: ignore[method-assign]

        chain = MessageChain()
        chain.chain.extend(
            [
                Plain(text="加密房间转发："),
                Nodes(nodes=[Node(name="Alice", uin="10001", content=[Plain(text="正文")])]),
            ]
        )

        await sender.send_message_chain("encrypted-room", chain, tmid="thread-1")

        self.assertEqual(len(adapter._e2ee.calls), 1)
        e2ee_call = adapter._e2ee.calls[0]
        self.assertEqual(e2ee_call["room_id"], "encrypted-room")
        self.assertEqual(e2ee_call["tmid"], "thread-1")
        self.assertIn("加密房间转发：", e2ee_call["text"])
        self.assertIn("【合并转发消息，共 1 条】", e2ee_call["text"])
        self.assertIn("Alice(10001): 正文", e2ee_call["text"])
        self.assertNotIn("type=", e2ee_call["text"])

        self.assertEqual(len(adapter.posted_json), 1)
        self.assertEqual(
            adapter.posted_json[0][0],
            "https://chat.example.com/api/v1/chat.sendMessage",
        )
        self.assertEqual(adapter.posted_json[0][1]["message"]["t"], "e2e")

    async def test_event_send_encrypts_rendered_nodes_in_e2ee_room(self) -> None:
        adapter = _DummyEncryptedEventAdapter()
        event = RocketChatMessageEvent(
            message_str="",
            message_obj=_DummyMessage(),
            platform_meta=PlatformMetadata(
                name="rocket_chat",
                description="Rocket.Chat",
                id="rocket_chat",
            ),
            session_id="encrypted-room",
            room_id="encrypted-room",
            thread_id=None,
            adapter=adapter,
        )
        chain = MessageChain()
        chain.chain.extend(
            [
                Plain(text="事件回复转发："),
                Nodes(nodes=[Node(name="Alice", uin="10001", content=[Plain(text="正文")])]),
            ]
        )

        await event.send(chain)

        self.assertEqual(len(adapter._e2ee.calls), 1)
        text = adapter._e2ee.calls[0]["text"]
        self.assertIn("事件回复转发：", text)
        self.assertIn("【合并转发消息，共 1 条】", text)
        self.assertIn("Alice(10001): 正文", text)
        self.assertNotIn("type=", text)
        self.assertEqual(adapter.posted_json[0][0], "https://chat.example.com/api/v1/chat.sendMessage")
        self.assertEqual(adapter.posted_json[0][1]["message"]["t"], "e2e")


if __name__ == "__main__":
    unittest.main()
