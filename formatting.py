"""Markdown → QQ 纯文本格式化与句边界分段（纯函数，无 I/O）。

QQ 不渲染 Markdown：出站前先把 agent 回复剥成纯文本，再按句边界切成
适合刷屏体验的短消息。剥离去掉标记但**保留信息**——链接保留为
``文字 (URL)``、代码块保留内容、引用/列表降级为 ``> `` / ``• `` 前缀。
"""

from __future__ import annotations

import re

__all__ = ["QQ_TEXT_LIMIT", "split_for_qq", "strip_markdown"]

QQ_TEXT_LIMIT = 2000

# 优先在句末标点/换行后切；其次空格；都匹配不上才硬切
_SPLIT_RE = re.compile(r"(?<=[。！？；!?;\n])")

# ── Markdown 剥离 ─────────────────────────────────────────────

# 代码块围栏（含语言标记），DOTALL 让 ``` 跨行
_FENCE_RE = re.compile(r"```[^\S\n]*[^\n`]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDER_RE = re.compile(r"(?<!_)__(.+?)__(?!_)", re.DOTALL)  # __x__ 也是粗体
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_ITALIC_UNDER_RE = re.compile(r"(?<!_)_([^_\n]+)_(?!_)")
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)
_ORDERED_RE = re.compile(r"^[ \t]*\d+[.)][ \t]+", re.MULTILINE)
_HRULE_RE = re.compile(r"^[ \t]*([-*_])[ \t]*(?:\1[ \t]*)+$", re.MULTILINE)


def strip_markdown(text: str) -> str:
    """Markdown → QQ 纯文本：去标记留内容，保留换行结构。"""
    if not text:
        return text

    # 1) 代码块：去掉围栏行，保留内容
    text = _FENCE_RE.sub(lambda m: m.group(1).rstrip("\n"), text)

    # 2) 图片/链接：图片降级为 [图片: alt]，链接保留文字+URL
    text = _IMAGE_RE.sub(lambda m: f"[图片: {m.group(1).strip()}]".rstrip(), text)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)

    # 3) 强调/删除线/行内代码：去标记留内容（__x__ 是粗体，先于单下划线斜体处理）
    text = _BOLD_RE.sub(r"\1", text)
    text = _BOLD_UNDER_RE.sub(r"\1", text)
    text = _STRIKE_RE.sub(r"\1", text)
    text = _ITALIC_STAR_RE.sub(r"\1", text)
    text = _ITALIC_UNDER_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)

    # 4) 结构元素：标题去 #、引用/列表降级前缀、水平线折叠为空行
    text = _HRULE_RE.sub("", text)
    text = _HEADING_RE.sub("", text)
    text = _BLOCKQUOTE_RE.sub("> ", text)
    text = _BULLET_RE.sub("• ", text)
    text = _ORDERED_RE.sub("", text)

    return text.strip("\n")


# ── 分段 ────────────────────────────────────────────────────

def split_for_qq(text: str, max_chars: int = QQ_TEXT_LIMIT) -> list[str]:
    """把长文本按句边界切成各段 ≤ max_chars 的消息列表。

    切点优先级：句末标点/换行（。！？；!?;\n 之后）→ 空格 → 硬切。
    每段必然 ≤ max_chars；空输入返回空列表；整体不超限时原样单段返回。
    """
    if not text:
        return []
    if max_chars <= 0:
        max_chars = 1
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        # 切点：窗口内最后一个句边界；退化找最后一个空格；再退化硬切。
        # 三种取法都保证 cut ≥ 1，循环必然前进
        cut = 0
        for m in _SPLIT_RE.finditer(window):
            cut = m.end()
        if cut == 0:
            space = window.rfind(" ")
            cut = space + 1 if space > 0 else max_chars
        piece, remaining = remaining[:cut], remaining[cut:]
        if piece.strip():
            chunks.append(piece.rstrip("\n"))
    if remaining.strip():
        chunks.append(remaining.strip("\n"))
    return [c for c in chunks if c.strip()]
