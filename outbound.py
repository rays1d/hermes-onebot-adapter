"""出站文本转换（纯函数，无 I/O）。

QQ 侧可见的文本在「分段完成、组装成 text 段之前」经过这里，按一组**显式**
规则做最小清洗。规则集默认为空——关闭时输出与历史行为逐字节一致。

首场景：Hermes Context Engine 把 ``§N§`` 回收编号当作回复前缀**原样持久化**
（Hermes 没有改写已存储消息的钩子），CLI/gateway 显示可以接受，但 QQ 聊天里
每条回复都顶着 ``§12§ `` 观感很差。这里只是把它剥掉，不参与入库，也不动
工具链路。

设计边界（钉死，避免长成通用改写网关）：

- **只支持行首**：规则一律锚定在行首，行中间出现同一串字面量绝不误伤；
- **只用字面量**：配置里写的永远是字面量/布尔，绝不编译成用户正则；
- **编号前缀必须带定界符**：``§N§`` 的 ``§`` 是硬要求，裸数字行首
  （``12 个苹果``、``2024 年``、``1. 第一项``）绝不能被吞；
- 不做入站转换、不做持久化改写。

新增清洗需求 = 在 :data:`_LITERAL_PREFIX` 之外的配置里加一条字面量前缀，
不需要改适配器代码。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = ["OutboundRules", "parse_rules", "strip_context_tags"]

# 行首 ``§N§`` 编号前缀，连其后至多**一个空格**一起剥。
# 两侧 ``§`` 是不可省的定界符：去掉它就会误吞「12 个苹果」这类正常行首数字。
_CONTEXT_TAG_RE = re.compile(r"^§\d+§ ?", re.MULTILINE)

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OutboundRules:
    """出站文本转换规则集（不可变；空规则集 = 原样返回文本）。"""

    strip_context_tags: bool = False
    literal_prefixes: tuple[str, ...] = ()

    @property
    def is_noop(self) -> bool:
        """无任何规则生效——调用方可据此跳过整个转换（含空段过滤）。"""
        return not self.strip_context_tags and not self.literal_prefixes

    def apply(self, text: str) -> str:
        """按序应用全部规则；空文本与空规则集直接原样返回。

        规则按**轮**应用至不动点（每步只会删字符，必然终止），因此各项规则可
        叠加：``[BOT] §9§ 正文`` 在开了前缀 + 编号两条时同样剥干净。
        """
        if not text or self.is_noop:
            return text
        while True:
            before = text
            if self.strip_context_tags:
                text = strip_context_tags(text)
            for prefix in self.literal_prefixes:
                text = _strip_literal_prefix(text, prefix)
            if text == before:
                return text


def parse_rules(raw_tags: Any = None, raw_prefixes: Any = None) -> OutboundRules:
    """把配置原值（布尔 / 逗号分隔字符串 / 列表）解析成规则集。

    容忍任意畸形输入：非布尔的 ``raw_tags`` 一律按 False（默认关闭），
    前缀里的空白项被丢弃。
    """
    return OutboundRules(
        strip_context_tags=_as_bool(raw_tags),
        literal_prefixes=tuple(_as_prefixes(raw_prefixes)),
    )


def strip_context_tags(text: str) -> str:
    """剥掉**每一行行首**的 ``§N§`` 编号前缀（含其后至多一个空格）。"""
    if not text:
        return text
    return _CONTEXT_TAG_RE.sub("", text)


# ── 内部辅助 ──────────────────────────────────────────────────

def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in _TRUTHY


def _as_prefixes(raw: Any) -> list[str]:
    """逗号分隔字符串 / 列表 → 去空白去空的字面量列表（保序）。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        items: Iterable[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        return []
    prefixes: list[str] = []
    for item in items:
        prefix = str(item).strip()
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def _strip_literal_prefix(text: str, prefix: str) -> str:
    """逐行剥掉行首字面量 ``prefix``（可叠加），并吃掉其后至多一个空格。

    纯字面量比较，不做正则解释——配置里写 ``(草稿)`` 就只匹配 ``(草稿)``。
    """
    if not prefix:
        return text
    lines: list[str] = []
    for line in text.split("\n"):
        while line.startswith(prefix):
            line = line[len(prefix):]
            line = line.removeprefix(" ")
        lines.append(line)
    return "\n".join(lines)
