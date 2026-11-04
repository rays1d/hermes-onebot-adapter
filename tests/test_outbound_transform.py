"""test_outbound_transform.py — 出站文本转换层（outbound.py + send()/standalone 接入）。

覆盖：规则解析、`§N§` 行首剥离精度（裸数字/段中间不误伤）、字面量前缀、
默认关闭时逐字节等价、多段消息与空段跳过、配置经 env 生效。
"""

from __future__ import annotations

import pytest
from conftest import (
    GROUP_ID,
    OTHER_USER,
    api_requests,
    running_adapter,
)
from onebot_plugin.outbound import OutboundRules, parse_rules, strip_context_tags


@pytest.fixture(autouse=True)
def _clean_transform_env(monkeypatch):
    """这两个配置键必须由用例显式设置：清掉宿主环境可能存在的残留。"""
    monkeypatch.delenv("ONEBOT_STRIP_TAGS", raising=False)
    monkeypatch.delenv("ONEBOT_STRIP_PREFIXES", raising=False)


@pytest.fixture
def no_chunk_delay(monkeypatch):
    """分段间隔归零：多段用例不吃 0.3s/段的防风控等待。"""
    import onebot_plugin.adapter as adapter_mod
    import onebot_plugin.standalone as standalone_mod
    monkeypatch.setattr(standalone_mod, "CHUNK_INTERVAL_S", 0.0)
    monkeypatch.setattr(adapter_mod, "SEND_CHUNK_INTERVAL_S", 0.0)


def _texts(fake) -> list[str]:
    """所有 send_msg 帧里末段为 text 的正文（按发送顺序；媒体段被跳过）。"""
    out: list[str] = []
    for frame in fake.requests_for("send_msg"):
        segments = frame["params"]["message"]
        if segments and segments[-1].get("type") == "text":
            out.append(segments[-1]["data"]["text"])
    return out


# ---------------------------------------------------------------------------
# parse_rules / OutboundRules
# ---------------------------------------------------------------------------

class TestParseRules:
    def test_defaults_are_off(self):
        rules = parse_rules()
        assert rules == OutboundRules()
        assert rules.is_noop is True

    def test_true_like_values(self):
        for raw in (True, "true", "TRUE", "1", "yes", "on", " on "):
            assert parse_rules(raw).strip_context_tags is True

    def test_false_like_and_malformed_values(self):
        for raw in (False, "", "0", "false", "no", "off", None, 1, [], {"a": 1}):
            rules = parse_rules(raw)
            assert rules.strip_context_tags is False
            assert rules.is_noop is True

    def test_prefixes_from_csv_string(self):
        rules = parse_rules(None, " [BOT] , (草稿) ,, [BOT] ")
        assert rules.literal_prefixes == ("[BOT]", "(草稿)")
        assert rules.is_noop is False

    def test_prefixes_from_list(self):
        assert parse_rules(None, ["a", " b ", ""]).literal_prefixes == ("a", "b")

    def test_apply_is_identity_when_noop(self):
        text = "§3§ 你好\n12 个苹果&nbsp;   \n"
        assert OutboundRules().apply(text) == text

    def test_apply_empty_text(self):
        assert parse_rules("true", "[x]").apply("") == ""


# ---------------------------------------------------------------------------
# strip_context_tags（纯函数）
# ---------------------------------------------------------------------------

class TestStripContextTags:
    @pytest.mark.parametrize(("raw", "expected"), [
        ("§3§ 你好", "你好"),                    # 带分隔空格
        ("§3§你好", "你好"),                     # 无空格
        ("§12§ 多位数", "多位数"),
        ("§3§ §4§ 后一个不动", "§4§ 后一个不动"),  # 只剥一重
        ("§3§ ", ""),                            # 只剩 tag
        ("§3§", ""),
        ("前面正常", "前面正常"),
        ("§N§ 字面不合规", "§N§ 字面不合规"),      # N 必须数字
        ("§ 空格 3 §", "§ 空格 3 §"),
    ])
    def test_single_prefix(self, raw, expected):
        assert strip_context_tags(raw) == expected

    @pytest.mark.parametrize("raw", [
        "12 个苹果",          # 裸数字行首
        "2024 年总结",
        "1. 第一项",
        "42",
        "版本 12 说明",
        "§12 缺右定界",
        "12§ 缺左定界",
        "  §3§ 前导空格不剥",
        "看看 §12§ 这个",     # 段中间
        "Sction 12 无关",
    ])
    def test_never_touches_non_prefix_content(self, raw):
        assert strip_context_tags(raw) == raw

    def test_per_line_multiline(self):
        text = "§1§ 第一行\n正文中间 §2§ 不动\n§3§ 第三行"
        assert strip_context_tags(text) == "第一行\n正文中间 §2§ 不动\n第三行"

    def test_only_one_space_after_tag(self):
        assert strip_context_tags("§7§  两个空格") == " 两个空格"

    def test_tab_after_tag_not_eaten(self):
        assert strip_context_tags("§7§\t制表符") == "\t制表符"

    def test_empty(self):
        assert strip_context_tags("") == ""


# ---------------------------------------------------------------------------
# 字面量前缀（纯函数）
# ---------------------------------------------------------------------------

class TestLiteralPrefixes:
    def test_strip_with_space(self):
        rules = parse_rules(None, "[草稿]")
        assert rules.apply("[草稿] 正文") == "正文"

    def test_stacking_and_multi_line(self):
        rules = parse_rules(None, ">>")
        # 逐重剥 + 各吃一个空格：>>> → >（第三重不满足前缀）
        assert rules.apply(">> >>> 引用") == "> 引用"
        assert rules.apply("无前缀\n>> 第二行") == "无前缀\n第二行"
        assert rules.apply(">>>>>> 全剥") == "全剥"

    def test_regex_is_literal(self):
        rules = parse_rules(None, "a.c")
        assert rules.apply("a.c 匹配") == "匹配"
        assert rules.apply("abc 不匹配") == "abc 不匹配"

    def test_mid_line_untouched(self):
        rules = parse_rules(None, "(草稿)")
        assert rules.apply("标题 (草稿) 尾巴") == "标题 (草稿) 尾巴"

    def test_multiple_prefixes_apply_in_order(self):
        rules = parse_rules(None, "[A],[B]")
        assert rules.apply("[A] [B] 都有") == "都有"

    def test_tags_and_prefixes_together(self):
        rules = parse_rules("true", "[BOT]")
        assert rules.apply("[BOT] §9§ 正文") == "正文"


# ---------------------------------------------------------------------------
# adapter.send() 接入（真实 WS）
# ---------------------------------------------------------------------------

class TestSendTransform:
    @pytest.mark.asyncio
    async def test_disabled_is_byte_identical(self, no_chunk_delay):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            raw = "§3§ **加粗** 原样保留\n12 个苹果"
            result = await adapter.send(OTHER_USER, raw)
            assert result.success is True
            assert _texts(fake) == ["§3§ 加粗 原样保留\n12 个苹果"]

    @pytest.mark.asyncio
    async def test_strip_tags_enabled(self, no_chunk_delay):
        async with running_adapter(outbound_strip_tags=True) as (adapter, harness):
            fake = await harness.dial_bot()
            result = await adapter.send(OTHER_USER, "§3§ 你好呀")
            assert result.success is True
            assert _texts(fake) == ["你好呀"]

    @pytest.mark.asyncio
    async def test_strip_tags_via_env(self, no_chunk_delay, monkeypatch):
        monkeypatch.setenv("ONEBOT_STRIP_TAGS", "true")
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            await adapter.send(OTHER_USER, "§8§ 环境变量生效")
            assert _texts(fake) == ["环境变量生效"]

    @pytest.mark.asyncio
    async def test_bare_digits_and_mid_line_survive(self, no_chunk_delay):
        async with running_adapter(outbound_strip_tags=True) as (adapter, harness):
            fake = await harness.dial_bot()
            await adapter.send(OTHER_USER, "12 个苹果\n看看 §5§ 这个")
            assert _texts(fake) == ["12 个苹果\n看看 §5§ 这个"]

    @pytest.mark.asyncio
    async def test_literal_prefixes_via_config(self, no_chunk_delay):
        async with running_adapter(
                outbound_strip_prefixes="[草稿],(draft)") as (adapter, harness):
            fake = await harness.dial_bot()
            await adapter.send(OTHER_USER, "[草稿] 正文")
            assert _texts(fake) == ["正文"]

    @pytest.mark.asyncio
    async def test_only_prefix_does_not_break_group_target(self, no_chunk_delay):
        """非 ASCII/数字参数与群目标不受影响（回归：转换只在文本层）。"""
        async with running_adapter(outbound_strip_tags=True) as (adapter, harness):
            fake = await harness.dial_bot()
            await adapter.send(f"group:{GROUP_ID}", "§1§ 群里说")
            frame = fake.request_for("send_msg")
            assert frame["params"]["group_id"] == int(GROUP_ID)
            assert _texts(fake) == ["群里说"]

    @pytest.mark.asyncio
    async def test_chunk_starting_with_tag_after_split(self, no_chunk_delay):
        """多段消息：tag 落在第二段开头（分段后行首）也要剥，边界不变。"""
        async with running_adapter(max_msg_chars=10, outbound_strip_tags=True) as (adapter, harness):
            fake = await harness.dial_bot()
            text = "第一句话。\n§2§ 第二句话。"
            result = await adapter.send(OTHER_USER, text)
            assert result.success is True
            sent = _texts(fake)
            assert len(sent) == 2
            assert all("§" not in chunk for chunk in sent)
            assert "".join(sent).replace("\n", "") == text.replace("\n", "").replace("§2§ ", "")

    @pytest.mark.asyncio
    async def test_reply_to_moves_to_first_sent_chunk(self, no_chunk_delay):
        """首段转换后变空 → 跳过不发；reply 改挂第一个实际发出的段。"""
        async with running_adapter(max_msg_chars=5, outbound_strip_tags=True) as (adapter, harness):
            fake = await harness.dial_bot()
            await adapter.send(OTHER_USER, "§1§ \n正文", reply_to="42")
            frames = fake.requests_for("send_msg")
            assert len(frames) == 1
            # 唯一发出的一条：带 reply 段 + 正文
            assert frames[0]["params"]["message"][0] == {"type": "reply", "data": {"id": "42"}}
            assert _texts(fake) == ["正文"]

    @pytest.mark.asyncio
    async def test_all_chunks_empty_sends_nothing(self, no_chunk_delay):
        async with running_adapter(outbound_strip_tags=True) as (adapter, harness):
            fake = await harness.dial_bot()
            result = await adapter.send(OTHER_USER, "§1§ ")
            assert result.success is True
            assert api_requests(fake) == []

    @pytest.mark.asyncio
    async def test_media_caption_goes_through_transform(self, no_chunk_delay):
        """caption 走 send()，同一套规则生效（媒体本体不动）。"""
        async with running_adapter(outbound_strip_tags=True) as (adapter, harness):
            fake = await harness.dial_bot()
            fake.action_data["send_msg"] = {"message_id": 77}
            await adapter.send_image(
                OTHER_USER, "https://example.com/a.png", caption="§4§ 看这个")
            assert _texts(fake) == ["看这个"]
            # 第二条才是图片段
            segment_frames = [f for f in fake.requests_for("send_msg")
                              if f["params"]["message"][-1]["type"] == "image"]
            assert len(segment_frames) == 1

    @pytest.mark.asyncio
    async def test_empty_still_short_circuits_before_transform(self, no_chunk_delay):
        async with running_adapter(outbound_strip_tags=True) as (adapter, harness):
            fake = await harness.dial_bot()
            result = await adapter.send(OTHER_USER, "``` ```")
            assert result.success is True
            assert api_requests(fake) == []
