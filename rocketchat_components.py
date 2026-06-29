from __future__ import annotations

from collections.abc import Iterable
import json
from typing import Any

_BLOCK_COMPONENT_TYPES = {"forward", "node", "nodes"}


def append_rendered_component(text_parts: list[str], comp: Any) -> bool:
    """Append a Rocket.Chat-readable fallback for non-core AstrBot components."""
    rendered = render_component_for_rocket_chat(comp)
    if not rendered:
        return False

    if _component_kind(comp) in _BLOCK_COMPONENT_TYPES:
        if text_parts and not text_parts[-1].endswith(("\n", " ")):
            text_parts.append("\n")
        text_parts.append(rendered)
        if not rendered.endswith("\n"):
            text_parts.append("\n")
    else:
        text_parts.append(rendered)
    return True


def render_message_chain_text(
    message_chain: Any,
    *,
    plain_type: type | tuple[type, ...] | None = None,
    reply_type: type | tuple[type, ...] | None = None,
) -> str:
    """Render the text-like part of a MessageChain without leaking object reprs."""
    text_parts: list[str] = []
    for comp in getattr(message_chain, "chain", []):
        if reply_type is not None and isinstance(comp, reply_type):
            continue
        if plain_type is not None and isinstance(comp, plain_type):
            text_parts.append(getattr(comp, "text", ""))
            continue
        append_rendered_component(text_parts, comp)
    return "".join(text_parts).strip()


def render_component_for_rocket_chat(comp: Any) -> str:
    """Render AstrBot components that Rocket.Chat has no native type for."""
    return _render_inline_component(comp)


def _render_forward(comp: Any) -> str:
    forward_id = _string_attr(comp, "id")
    if forward_id:
        return f"【合并转发消息】\n转发消息 ID：{forward_id}"
    return "【合并转发消息】"


def _render_nodes(comp: Any) -> str:
    nodes = list(_iter_components(_safe_getattr(comp, "nodes")))
    if not nodes:
        return "【合并转发消息】\n（没有可展示的转发节点）"

    lines = [f"【合并转发消息，共 {len(nodes)} 条】"]
    for index, node in enumerate(nodes, start=1):
        lines.extend(_render_node_lines(node, index=index))
    return "\n".join(lines)


def _render_node(comp: Any) -> str:
    return "\n".join(["【转发消息节点】", *_render_node_lines(comp, index=None)])


def _render_node_lines(comp: Any, *, index: int | None) -> list[str]:
    sender = _render_node_sender(comp)
    content = _render_component_chain(_safe_getattr(comp, "content"))
    if not content:
        content = "（空消息）"

    prefix = f"{index}. " if index is not None else "- "
    content_lines = content.splitlines() or [content]
    lines = [f"{prefix}{sender}: {content_lines[0]}"]
    for line in content_lines[1:]:
        lines.append(f"   {line}")
    return lines


def _render_node_sender(comp: Any) -> str:
    name = _string_attr(comp, "name")
    uin = _string_attr(comp, "uin")
    if name and uin and uin != "0":
        return f"{name}({uin})"
    if name:
        return name
    if uin and uin != "0":
        return uin
    return "未知用户"


def _render_component_chain(value: Any) -> str:
    parts: list[str] = []
    for item in _iter_components(value):
        rendered = _render_inline_component(item)
        if not rendered:
            continue
        if parts and _needs_space(parts[-1], rendered):
            parts.append(" ")
        parts.append(rendered)
    return "".join(parts).strip()


def _render_inline_component(comp: Any) -> str:
    kind = _component_kind(comp)
    if kind in {"plain", "text"}:
        return _string_attr(comp, "text")
    if kind == "atall":
        return "@all"
    if kind == "at":
        target = _string_attr(comp, "name") or _string_attr(comp, "qq")
        return f"@{target}" if target else "@"
    if kind == "image":
        ref = _media_ref(comp)
        return f"[图片: {ref}]" if ref else "[图片]"
    if kind == "file":
        name = _string_attr(comp, "name")
        ref = _media_ref(comp)
        if name and ref:
            return f"[文件: {name} {ref}]"
        if name:
            return f"[文件: {name}]"
        return f"[文件: {ref}]" if ref else "[文件]"
    if kind == "record":
        ref = _media_ref(comp)
        return f"[语音: {ref}]" if ref else "[语音]"
    if kind == "video":
        ref = _media_ref(comp)
        return f"[视频: {ref}]" if ref else "[视频]"
    if kind == "node":
        return _render_node(comp)
    if kind == "nodes":
        return _render_nodes(comp)
    if kind == "forward":
        return _render_forward(comp)
    if kind == "reply":
        message = _string_attr(comp, "message_str")
        return f"[回复: {message}]" if message else "[回复]"
    if kind == "face":
        face_id = _string_attr(comp, "id")
        return f"[表情: {face_id}]" if face_id else "[表情]"
    if kind == "poke":
        target = _string_attr(comp, "id") or _string_attr(comp, "qq")
        return f"[戳一戳: {target}]" if target else "[戳一戳]"
    if kind == "share":
        title = _string_attr(comp, "title")
        url = _string_attr(comp, "url")
        if title and url:
            return f"[分享: {title} {url}]"
        return f"[分享: {title or url}]" if (title or url) else "[分享]"
    if kind == "location":
        title = _string_attr(comp, "title") or _string_attr(comp, "content")
        lat = _string_attr(comp, "lat")
        lon = _string_attr(comp, "lon")
        coords = ", ".join(part for part in (lat, lon) if part)
        detail = " ".join(part for part in (title, coords) if part)
        return f"[位置: {detail}]" if detail else "[位置]"
    if kind == "music":
        title = _string_attr(comp, "title")
        url = _string_attr(comp, "url") or _string_attr(comp, "audio")
        detail = " ".join(part for part in (title, url) if part)
        return f"[音乐: {detail}]" if detail else "[音乐]"
    if kind == "json":
        data = _safe_getattr(comp, "data")
        title = _extract_json_title(data)
        return f"[JSON 消息: {title}]" if title else "[JSON 消息]"
    if kind == "unknown":
        text = _string_attr(comp, "text")
        return text or "[未知消息]"
    return _render_generic_component(comp, kind)


def _render_generic_component(comp: Any, kind: str) -> str:
    label = kind or type(comp).__name__
    text = _string_attr(comp, "text") or _string_attr(comp, "content")
    if text:
        return text
    return f"[暂不支持的消息类型: {label}]"


def _component_kind(comp: Any) -> str:
    raw_type = _safe_getattr(comp, "type")
    value = _safe_getattr(raw_type, "value") if raw_type is not None else None
    if value is None:
        value = raw_type
    if value:
        text = str(value)
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        return text.strip().lower()
    return type(comp).__name__.strip().lower()


def _iter_components(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)):
        return []
    if isinstance(value, Iterable):
        return value
    return [value]


def _media_ref(comp: Any) -> str:
    for name in ("url", "file_", "file", "path"):
        value = _safe_getattr(comp, name)
        if value:
            return str(value).strip()
    return ""


def _string_attr(comp: Any, name: str) -> str:
    value = _safe_getattr(comp, name)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _safe_getattr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        return None


def _needs_space(previous: str, current: str) -> bool:
    if not previous or not current:
        return False
    if previous.endswith(("\n", " ", "\t")) or current.startswith(("\n", " ", "\t")):
        return False
    return previous[-1].isascii() and current[0].isascii()


def _extract_json_title(data: Any) -> str:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return data[:80].strip()
    if not isinstance(data, dict):
        return ""
    for key in ("title", "desc", "description", "prompt", "summary"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    meta = data.get("meta")
    if isinstance(meta, dict):
        for value in meta.values():
            if isinstance(value, str) and value.strip():
                return value.strip()[:80]
            if isinstance(value, dict):
                nested = value.get("title") or value.get("desc")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()[:80]
    return ""
