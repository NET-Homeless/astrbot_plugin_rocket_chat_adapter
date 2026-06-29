"""MessageChain 保序发送的段抽象与分发逻辑。

设计目标：
- 提取 Event 和 Sender 两条路径中重复的"保序切分 + flush"逻辑
- 保持各自的发送语义差异（quote_original、At 渲染、Reply 处理等）
- 纯函数切分 + 协议化发送，易于测试和扩展
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol

from astrbot.api.message_components import File, Image, Record, Video

# ============================================================================
# Segment 类型定义
# ============================================================================


@dataclass(frozen=True, slots=True)
class TextSegment:
    """纯文本段（可能包含 @ 提及）。"""

    text: str


@dataclass(frozen=True, slots=True)
class ImageGroup:
    """一组连续的图片，可携带前置文本作为 caption。

    caption 仅在 text_before_image=True 时非空。
    """

    images: tuple[Image, ...]
    caption: str = ""


@dataclass(frozen=True, slots=True)
class MediaItem:
    """非图片媒体（File / Record / Video），始终独立发送。"""

    component: File | Record | Video


Segment = TextSegment | ImageGroup | MediaItem


# ============================================================================
# 渲染回调类型
# ============================================================================


RenderComponentFn = Callable[[list[str], object], bool]
"""渲染非核心组件到 text_parts 的回调。

签名：render_fn(text_parts: list[str], component) -> bool
返回 True 表示成功渲染（追加了文本）。
"""


# ============================================================================
# Segment 发送协议
# ============================================================================


class SegmentSender(Protocol):
    """发送单个 Segment 的协议。

    实现方负责：
    - 文本发送的具体方式（普通 / 引用回复 / 带 mention）
    - 图片组的发送（合并 / 独立）
    - 媒体项的发送（含下载/解码/回退）
    """

    async def send_text(self, text: str) -> None:
        """发送一段纯文本。"""
        ...

    async def send_image_group(self, group: ImageGroup) -> None:
        """发送图片组（可能携带 caption）。"""
        ...

    async def send_media(self, item: MediaItem) -> None:
        """发送单个非图片媒体。"""
        ...


# ============================================================================
# 纯函数：MessageChain → Segments
# ============================================================================


def iter_segments(
    components: list,
    *,
    is_e2ee: bool,
    render_at: bool = False,
    render_other: RenderComponentFn | None = None,
) -> Iterator[Segment]:
    """将 MessageChain 组件列表切分为保序段。

    保序规则：
    - 文本/At 连续累积，直到遇到 Image / 非图片媒体
    - Image 连续累积为 ImageGroup，caption 仅当文本紧邻在图片之前时携带
    - 非图片媒体（File/Record/Video）始终作为独立 MediaItem
    - E2EE 房间遇到 Image 时立即 flush，确保逐条加密发送
    - Reply 组件被忽略（由调用方在外部处理）

    其他组件（非 Plain/At/AtAll/Image/File/Record/Video/Reply）：
    - 如果提供 render_other 回调，会调用它尝试渲染到当前文本缓冲
    - 如果未提供或渲染失败，会 flush 当前段（确保顺序），然后跳过该组件

    Args:
        components: MessageChain.chain 列表
        is_e2ee: 是否为 E2EE 房间，影响 Image 的 flush 策略
        render_at: 是否在切分时渲染 At/AtAll（Event 路径需要，Sender 路径不需要）
        render_other: 可选回调，用于将其他组件渲染为文本追加

    Yields:
        Segment: 按原始顺序切分出的段
    """
    text_parts: list[str] = []
    pending_images: list[Image] = []
    text_before_image = False

    def flush_text() -> TextSegment | None:
        """将累积的文本 flush 为 TextSegment。"""
        text = "".join(text_parts).strip()
        text_parts.clear()
        return TextSegment(text) if text else None

    def flush_images() -> ImageGroup | None:
        """将累积的图片 flush 为 ImageGroup。"""
        nonlocal text_before_image
        if not pending_images:
            return None
        caption = "".join(text_parts).strip() if text_before_image else ""
        images = tuple(pending_images)
        pending_images.clear()
        text_before_image = False
        text_parts.clear()
        return ImageGroup(images=images, caption=caption)

    for comp in components:
        # Plain 文本
        if _is_plain(comp):
            text = getattr(comp, "text", "")
            if not text:
                continue
            if pending_images:
                if img_group := flush_images():
                    yield img_group
                text_before_image = False
            text_parts.append(text)
            continue

        # At / AtAll（仅在 render_at=True 时处理）
        if render_at:
            from astrbot.api.message_components import At, AtAll

            if isinstance(comp, AtAll):
                if pending_images:
                    if img_group := flush_images():
                        yield img_group
                    text_before_image = False
                text_parts.append("@all ")
                continue

            if isinstance(comp, At):
                mention_name = (
                    getattr(comp, "name", None) or getattr(comp, "qq", None) or ""
                )
                rendered = f"@{mention_name} " if mention_name else ""
                if rendered:
                    if pending_images:
                        if img_group := flush_images():
                            yield img_group
                        text_before_image = False
                    text_parts.append(rendered)
                continue

        # 图片组件
        if isinstance(comp, Image):
            if is_e2ee:
                # E2EE：先 flush 文本，再单独发图片
                if txt := flush_text():
                    yield txt
                text_before_image = False
                yield ImageGroup(images=(comp,), caption="")
            else:
                if not pending_images and not text_parts:
                    pass  # 空段，直接加入
                elif not pending_images:
                    text_before_image = bool("".join(text_parts).strip())
                pending_images.append(comp)
            continue

        # 非图片媒体：先 flush 当前段，再独立发送
        if isinstance(comp, (File, Record, Video)):
            if txt := flush_text():
                yield txt
            if img_group := flush_images():
                yield img_group
            text_before_image = False
            yield MediaItem(component=comp)
            continue

        # Reply：忽略，由调用方外部处理
        from astrbot.api.message_components import Reply

        if isinstance(comp, Reply):
            continue

        # 其他组件：尝试渲染，或 flush 并跳过
        if render_other is not None:
            # 尝试渲染到当前文本缓冲
            rendered = render_other(text_parts, comp)
            if rendered:
                continue
        # 无法渲染：flush 当前段以保持顺序，然后跳过该组件
        if txt := flush_text():
            yield txt
        if img_group := flush_images():
            yield img_group
        text_before_image = False
        # 跳过该组件，不 yield 任何内容
        # 调用方如果需要处理，应在外部预处理或使用其他机制

    # 循环结束：必须 flush 剩余内容！
    # 顺序很重要：先 flush_images（可能消费 text_parts 作为 caption），再 flush_text
    if img_group := flush_images():
        yield img_group
    if txt := flush_text():
        yield txt


def _is_plain(comp: object) -> bool:
    """判断是否为 Plain 组件（通过 duck typing 兼容不同导入）。"""
    # 优先用类型检查
    try:
        from astrbot.api.message_components import Plain

        if isinstance(comp, Plain):
            return True
    except Exception:
        pass
    # 回退：检查是否有 text 属性且类型名包含 plain/text
    if hasattr(comp, "text"):
        kind = type(comp).__name__.lower()
        return "plain" in kind or "text" in kind
    return False


# ============================================================================
# 分发器：通用保序发送循环
# ============================================================================


async def dispatch_segments(
    segments: Iterator[Segment],
    sender: SegmentSender,
) -> None:
    """按顺序将 Segments 分发给发送器。

    这是保序发送的核心循环，Event 和 Sender 路径共享此逻辑。
    """
    for seg in segments:
        if isinstance(seg, TextSegment):
            await sender.send_text(seg.text)
        elif isinstance(seg, ImageGroup):
            await sender.send_image_group(seg)
        elif isinstance(seg, MediaItem):
            await sender.send_media(seg)


# ============================================================================
# 便捷：从 MessageChain 直接分发（供简单场景使用）
# ============================================================================


async def send_message_chain_ordered(
    components: list,
    sender: SegmentSender,
    *,
    is_e2ee: bool,
    render_at: bool = False,
    render_other: RenderComponentFn | None = None,
) -> None:
    """便捷函数：直接从组件列表保序发送。

    内部使用 iter_segments + dispatch_segments。
    """
    segments = iter_segments(
        components, is_e2ee=is_e2ee, render_at=render_at, render_other=render_other
    )
    await dispatch_segments(segments, sender)
