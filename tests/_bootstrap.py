from __future__ import annotations

import enum
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path


def install_astrbot_stubs() -> None:
    if "astrbot.api" in sys.modules:
        return

    repo_root = Path(__file__).resolve().parents[1]
    parent_dir = repo_root.parent
    if str(parent_dir) not in sys.path:
        sys.path.insert(0, str(parent_dir))

    logger = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )

    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = logger

    message_components_mod = types.ModuleType("astrbot.api.message_components")

    @dataclass
    class Plain:
        text: str = ""

    @dataclass
    class At:
        qq: str = ""
        name: str = ""

    @dataclass
    class AtAll(At):
        pass

    @dataclass
    class Image:
        file: str | None = None
        url: str | None = None

        @classmethod
        def fromFileSystem(cls, path: str) -> "Image":
            return cls(file=path)

        @classmethod
        def fromURL(cls, url: str) -> "Image":
            return cls(url=url)

        async def convert_to_file_path(self) -> str:
            return self.file or self.url or ""

    @dataclass
    class Record:
        file: str | None = None
        url: str | None = None

        @classmethod
        def fromFileSystem(cls, path: str) -> "Record":
            return cls(file=path)

        @classmethod
        def fromURL(cls, url: str) -> "Record":
            return cls(url=url)

        async def convert_to_file_path(self) -> str:
            return self.file or self.url or ""

    @dataclass
    class Video:
        file: str | None = None
        url: str | None = None

        @classmethod
        def fromFileSystem(cls, path: str) -> "Video":
            return cls(file=path)

        @classmethod
        def fromURL(cls, url: str) -> "Video":
            return cls(url=url)

        async def convert_to_file_path(self) -> str:
            return self.file or self.url or ""

    @dataclass
    class File:
        name: str = ""
        file: str | None = None
        url: str | None = None

        async def get_file(self) -> str:
            return self.file or self.url or ""

    @dataclass
    class Reply:
        id: str = ""
        chain: list = field(default_factory=list)
        sender_id: str = ""
        sender_nickname: str = ""
        time: int = 0
        message_str: str = ""

    for cls in (Plain, At, AtAll, Image, Record, Video, File, Reply):
        setattr(message_components_mod, cls.__name__, cls)

    event_mod = types.ModuleType("astrbot.api.event")

    class MessageChain:
        def __init__(self) -> None:
            self.chain: list = []

        def message(self, text: str) -> "MessageChain":
            self.chain.append(Plain(text=text))
            return self

    class AstrMessageEvent:
        def __init__(
            self,
            message_str: str,
            message_obj: object,
            platform_meta: object,
            session_id: str,
        ) -> None:
            self.message_str = message_str
            self.message_obj = message_obj
            self.platform_meta = platform_meta
            self.session_id = session_id
            self.is_at_or_wake_command = False
            self.is_wake = False
            self._sent_messages: list = []

        async def send(self, message: object) -> None:
            self._sent_messages.append(message)

        def get_messages(self) -> list:
            return list(getattr(self.message_obj, "message", []))

        def get_sender_id(self) -> str:
            sender = getattr(self.message_obj, "sender", None)
            return str(getattr(sender, "user_id", ""))

        def get_self_id(self) -> str:
            return str(getattr(self.message_obj, "self_id", ""))

        def is_private_chat(self) -> bool:
            return getattr(self.message_obj, "type", None) == MessageType.FRIEND_MESSAGE

        @property
        def unified_msg_origin(self) -> str:
            return "rocket_chat:group:test"

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain

    platform_mod = types.ModuleType("astrbot.api.platform")

    class MessageType(enum.Enum):
        FRIEND_MESSAGE = "friend"
        GROUP_MESSAGE = "group"

    @dataclass
    class MessageMember:
        user_id: str
        nickname: str = ""

    @dataclass
    class Group:
        group_id: str

    class AstrBotMessage:
        pass

    @dataclass
    class PlatformMetadata:
        name: str
        description: str
        id: str
        support_streaming_message: bool = False

    for cls in (AstrBotMessage, Group, MessageMember, MessageType, PlatformMetadata):
        setattr(platform_mod, cls.__name__, cls)

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.message_components"] = message_components_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.platform"] = platform_mod

