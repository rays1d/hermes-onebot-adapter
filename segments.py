"""OneBot v11 消息段解析模块（纯函数/简单类，无 I/O）。

职责：
- 把入站消息统一成段列表：优先解析 NapCat 常见的段数组形态
  ``[{"type": "text", "data": {...}}, ...]``，回退解析 CQ 码字符串
  ``[CQ:type,key=value,...]``（含实体转义还原）。
- 渲染成面向 agent 的纯文本：媒体段注册进 :class:`MediaRegistry` 后以
  内联标记呈现（如 ``[图片:m1]``），agent 需要真图时再用
  ``napcat_call("get_image", {...})`` 按需拉取——不主动下载，省跨机带宽。
- 提供 reply / at 的快速提取器。

防御式解析约定：未知段类型一律保留（type/data 原样，即
("unknown", type, data) 的语义），渲染时回退 ``[{type}]`` 占位，绝不抛异常。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "MediaRef",
    "MediaRegistry",
    "Segment",
    "at_targets",
    "extract_reply_id",
    "parse_segments",
    "render_segments",
    "render_text",
]

# 有专门渲染处理的段类型；不在表内的视为 unknown，渲染回退占位
_KNOWN_TYPES = frozenset({"text", "at", "face", "reply", "image", "record", "video", "file"})

# CQ 码：[CQ:type,k=v,...]。值/参数中不允许出现未转义的 [ ]，
# 转义后为 &#91; &#93;，因此用 [^\[\]] 安全匹配
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_.\-]+)((?:,[^\[\]]*)*)\]")

# ── 转义还原（OneBot 11 规范）───────────────────────────────────
# 纯文本里 & [ ] 被转义（& → &amp;）；CQ 码参数额外转义逗号（, → &#44;）。
# 还原时 & 必须最后处理，避免 "&#91;" 被二次还原成 "["
# （它应还原成字面 "&#91;"）。

_UNESCAPE_TEXT_ORDER = (("&#91;", "["), ("&#93;", "]"), ("&amp;", "&"))
_UNESCAPE_PARAM_ORDER = (("&#44;", ","),) + _UNESCAPE_TEXT_ORDER


def _unescape(s: str, table) -> str:
    for old, new in table:
        s = s.replace(old, new)
    return s


def _unescape_text(s: str) -> str:
    return _unescape(s, _UNESCAPE_TEXT_ORDER)


def _unescape_param(s: str) -> str:
    return _unescape(s, _UNESCAPE_PARAM_ORDER)


@dataclass
class Segment:
    """单个消息段。``raw_text`` 仅 text 段有值：已还原转义的原始文本。"""

    type: str
    data: dict = field(default_factory=dict)
    raw_text: str | None = None

    @property
    def is_known(self) -> bool:
        return self.type in _KNOWN_TYPES


@dataclass
class MediaRef:
    """一条媒体引用：media_id 是本条消息内的短标记（m1/m2/...）。

    url/file 二者至少有一个可用来拉取；file 侧存 OneBot ``file``/``file_id``
    字段原值（get_image/get_record/get_file 均认它）。
    """

    media_id: str
    type: str  # image / record / video / file
    url: str | None = None
    file: str | None = None
    file_size: int | None = None
    name: str | None = None  # 仅 file 段：文件名


class MediaRegistry:
    """媒体引用注册表：每条消息新建一个实例即完成计数重置（m1、m2、…）。"""

    def __init__(self) -> None:
        self._refs: dict[str, MediaRef] = {}
        self._counter = 0

    def register(
        self,
        seg_type: str,
        url: str | None = None,
        file: str | None = None,
        file_size: int | None = None,
        name: str | None = None,
    ) -> str:
        """登记一条媒体引用，返回其 media_id（m1、m2、…）。"""
        self._counter += 1
        media_id = f"m{self._counter}"
        self._refs[media_id] = MediaRef(
            media_id=media_id, type=seg_type, url=url, file=file,
            file_size=file_size, name=name,
        )
        return media_id

    def get(self, media_id: str) -> MediaRef | None:
        return self._refs.get(media_id)

    def all(self) -> list[MediaRef]:
        return list(self._refs.values())

    def __len__(self) -> int:
        return len(self._refs)

    def __contains__(self, media_id: object) -> bool:
        return media_id in self._refs


# ── 解析 ──────────────────────────────────────────────────────

def parse_segments(raw: Any) -> list[Segment]:
    """把段数组（list）或 CQ 码字符串（str）统一解析成段列表。

    - list[dict]：标准段数组形态，逐项取 type/data；data 非字典时按空处理。
    - str：CQ 码混合文本，``[CQ:...]`` 之外的部分是 text 段。
    - 其他类型（None、数字等）返回空列表；任何畸形输入不抛异常。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return _from_cq_string(raw)
    if isinstance(raw, list):
        return _from_array(raw)
    return []


def _from_array(items: list) -> list[Segment]:
    segments: list[Segment] = []
    for item in items:
        if isinstance(item, str):
            # 容忍实现端把纯文本直接塞进段数组
            if item:
                segments.append(Segment("text", {"text": item}, item))
            continue
        if not isinstance(item, dict):
            continue  # 其他畸形项丢弃，不抛异常
        seg_type = str(item.get("type", "")).strip()
        if not seg_type:
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            data = {}
        raw_text = str(data.get("text", "")) if seg_type == "text" else None
        segments.append(Segment(seg_type, data, raw_text))
    return segments


def _from_cq_string(text: str) -> list[Segment]:
    segments: list[Segment] = []
    pos = 0
    for m in _CQ_RE.finditer(text):
        if m.start() > pos:
            segments.append(_text_segment(text[pos:m.start()]))
        params = _parse_cq_params(m.group(2) or "")
        segments.append(Segment(m.group(1), params))
        pos = m.end()
    if pos < len(text):
        segments.append(_text_segment(text[pos:]))
    return segments


def _text_segment(raw: str) -> Segment:
    decoded = _unescape_text(raw)
    return Segment("text", {"text": decoded}, decoded)


def _parse_cq_params(params: str) -> dict:
    """解析 CQ 码参数串 ``",k=v,k2=v2"``。原始逗号是分隔符，字面逗号已转义。"""
    out: dict = {}
    if not params:
        return out
    for part in params.split(","):
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            out[_unescape_param(key)] = _unescape_param(value)
        else:
            out[_unescape_param(part)] = ""  # 无值参数
    return out


# ── 渲染 ──────────────────────────────────────────────────────

def render_text(segment: Segment, media_registry: MediaRegistry) -> str:
    """把单个段渲染为面向 agent 的文本；媒体段注册后以内联标记呈现。

    - text → 原文
    - at → "@{qq}"（有 name 时 "@{name}"）
    - face → "[表情:{id}]"
    - reply → 空串（引用由适配器单独取原文处理）
    - image/record/video → 注册后 "[图片:m1]" / "[语音:m1]" / "[视频:m1]"
    - file → "[文件:{name}:m1]"（无名时 "[文件:m1]"）
    - 未知段（forward/json 等）→ "[{type}]" 占位
    """
    seg_type = segment.type
    data = segment.data
    if seg_type == "text":
        if segment.raw_text is not None:
            return segment.raw_text
        return str(data.get("text", ""))
    if seg_type == "at":
        name = str(data.get("name") or "").strip()
        if name:
            return f"@{name}"
        qq = str(data.get("qq") or "").strip()
        return f"@{qq}" if qq else "@"
    if seg_type == "face":
        return f"[表情:{data.get('id', '')}]"
    if seg_type == "reply":
        return ""
    if seg_type == "image":
        media_id = media_registry.register("image", url=_media_url(data), file=_media_file(data),
                                           file_size=_media_size(data))
        return f"[图片:{media_id}]"
    if seg_type == "record":
        media_id = media_registry.register("record", url=_media_url(data), file=_media_file(data),
                                           file_size=_media_size(data))
        return f"[语音:{media_id}]"
    if seg_type == "video":
        media_id = media_registry.register("video", url=_media_url(data), file=_media_file(data),
                                           file_size=_media_size(data))
        return f"[视频:{media_id}]"
    if seg_type == "file":
        name = _file_name(data)
        media_id = media_registry.register("file", url=_media_url(data),
                                           file=_media_file(data) or data.get("file_id"),
                                           file_size=_media_size(data), name=name)
        return f"[文件:{name}:{media_id}]" if name else f"[文件:{media_id}]"
    # 未知段类型：原样保留（见模块 docstring），渲染回退占位
    return f"[{seg_type}]"


def render_segments(segments: list[Segment], media_registry: MediaRegistry) -> str:
    """整条消息渲染为纯文本（直接拼接，媒体标记已内联）。"""
    return "".join(render_text(seg, media_registry) for seg in segments)


def _media_url(data: dict) -> str | None:
    url = data.get("url")
    return str(url) if url else None


def _media_file(data: dict) -> str | None:
    value = data.get("file") or data.get("file_id")
    return str(value) if value else None


def _media_size(data: dict) -> int | None:
    value = data.get("file_size", data.get("size"))
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _file_name(data: dict) -> str:
    name = data.get("name") or data.get("file_name") or data.get("filename")
    return str(name).strip() if name else ""


# ── 提取器 ────────────────────────────────────────────────────

def extract_reply_id(segments: list[Segment]) -> str | None:
    """第一个 reply 段的 data.id（引用的消息 ID），无则 None。"""
    for seg in segments:
        if seg.type == "reply":
            reply_id = seg.data.get("id")
            if reply_id is not None and str(reply_id).strip():
                return str(reply_id).strip()
    return None


def at_targets(segments: list[Segment]) -> list[str]:
    """所有 at 段的目标 QQ 号（字符串，含 ``all``），去重保序。"""
    targets: list[str] = []
    for seg in segments:
        if seg.type != "at":
            continue
        qq = str(seg.data.get("qq") or "").strip()
        if qq and qq not in targets:
            targets.append(qq)
    return targets
