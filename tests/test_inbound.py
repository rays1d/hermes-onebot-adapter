"""test_inbound.py — 入站消息链路（fake NapCat 拨入真实 server 驱动）。

覆盖：私聊直答、群 @ 触发（at 剥离 + 昵称前缀）、回复 bot 触发（get_msg 取原文）、
非触发场景、message_id 去重、self 回显过滤、引用原文失败降级、媒体内联标记。
"""

from __future__ import annotations

import pytest

from conftest import (
    GROUP_ID,
    OTHER_USER,
    OTHER_USER2,
    SELF_ID,
    group_event,
    install_message_handler,
    private_event,
    running_adapter,
    settle,
    wait_until,
)

# 群聊触发测试的公共配置：群白名单 + 私聊白名单（allowlist 默认策略）
GATE_EXTRA = {
    "allowed_groups": GROUP_ID,
    "allowed_users": f"{OTHER_USER},{OTHER_USER2}",
}


def quoted_message_data(*, sender_id: str, nickname: str, message) -> dict:
    """get_msg 回包的 data：被引用消息。"""
    return {
        "message_id": 999,
        "sender": {"user_id": int(sender_id), "nickname": nickname},
        "message": message,
    }


# ---------------------------------------------------------------------------
# 私聊
# ---------------------------------------------------------------------------

class TestPrivateInbound:
    @pytest.mark.asyncio
    async def test_private_message_reaches_agent(self, monkeypatch):
        async with running_adapter(allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(private_event("你好"))
            await wait_until(lambda: len(events) == 1)
            event = events[0]
            assert event.text == "你好"
            assert event.user_id == str(int(OTHER_USER))
            assert event.user_name == f"u{OTHER_USER}"
            assert event.message_id == "1001"
            assert event.source.chat_id == str(int(OTHER_USER))
            assert event.source.chat_type == "dm"
            assert event.source.platform.value == "onebot"
            assert event.media_urls == []  # 按需拉取：不主动下载

    @pytest.mark.asyncio
    async def test_dm_allowlist_denied_user_never_reaches_agent(self, monkeypatch):
        async with running_adapter(allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(private_event("放我进去", user_id=OTHER_USER2))
            await settle()
            assert events == []
            # 实现端没有被发过任何 API 请求（门控先行，连 get_msg 都不碰）
            assert fake.requests == []

    @pytest.mark.asyncio
    async def test_self_message_filtered(self, monkeypatch):
        async with running_adapter(allowed_users=SELF_ID) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(private_event("自言自语", user_id=SELF_ID))
            await settle()
            assert events == []

    @pytest.mark.asyncio
    async def test_duplicate_message_id_deduplicated(self, monkeypatch):
        async with running_adapter(allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            payload = private_event("只处理一次", message_id=555)
            await fake.push_event(payload)
            await fake.push_event(payload)  # 同 id 重投
            await wait_until(lambda: len(events) == 1)
            await settle()
            assert len(events) == 1

    @pytest.mark.asyncio
    async def test_reply_fetch_failure_degrades_to_placeholder(self, monkeypatch):
        async with running_adapter(allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            fake.fail_actions.add("get_msg")
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(private_event(
                [{"type": "reply", "data": {"id": "999"}},
                 {"type": "text", "data": {"text": "你说得对"}}]))
            await wait_until(lambda: len(events) == 1)
            event = events[0]
            assert event.reply_to_message_id == "999"
            assert event.reply_to_text == "[引用消息]"
            assert event.reply_to_author_id is None
            assert event.reply_to_is_own_message is False

    @pytest.mark.asyncio
    async def test_media_markers_registered_for_on_demand_fetch(self, monkeypatch):
        async with running_adapter(allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(private_event([
                {"type": "image", "data": {"url": "https://e.com/cat.jpg",
                                            "file": "cat.jpg", "file_size": 2048}},
                {"type": "record", "data": {"url": "https://e.com/voice.mp3"}},
            ]))
            await wait_until(lambda: len(events) == 1)
            event = events[0]
            assert event.text == "[图片:m1][语音:m2]"
            registry = adapter.current_registry()
            assert registry is not None
            assert registry.get("m1").url == "https://e.com/cat.jpg"
            assert registry.get("m1").file == "cat.jpg"
            assert registry.get("m1").file_size == 2048
            assert registry.get("m2").type == "record"


# ---------------------------------------------------------------------------
# 群聊触发
# ---------------------------------------------------------------------------

class TestGroupTrigger:
    @pytest.mark.asyncio
    async def test_at_mention_triggers_and_at_stripped(self, monkeypatch):
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(group_event([
                {"type": "at", "data": {"qq": SELF_ID}},
                {"type": "text", "data": {"text": " 在吗"}},
            ]))
            await wait_until(lambda: len(events) == 1)
            event = events[0]
            assert event.text == f"u{OTHER_USER}：\n在吗"  # @bot 剥离 + 昵称前缀
            assert event.source.chat_id == f"group:{int(GROUP_ID)}"
            assert event.source.chat_type == "group"

    @pytest.mark.asyncio
    async def test_sender_card_preferred_over_nickname(self, monkeypatch):
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            fake.action_data["get_msg"] = quoted_message_data(
                sender_id=SELF_ID, nickname="bot", message="之前的消息")
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(group_event(
                [{"type": "reply", "data": {"id": "999"}},
                 {"type": "text", "data": {"text": "早"}}],
                card="群名片哥", message_id=2003))
            await wait_until(lambda: len(events) == 1)
            assert events[0].text.startswith("群名片哥：\n早")

    @pytest.mark.asyncio
    async def test_plain_group_message_not_triggered(self, monkeypatch):
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(group_event("普通聊天内容，没有 @"))
            await settle()
            assert events == []

    @pytest.mark.asyncio
    async def test_at_other_user_not_triggered(self, monkeypatch):
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(group_event([
                {"type": "at", "data": {"qq": OTHER_USER2}},
                {"type": "text", "data": {"text": " 你看这个"}},
            ]))
            await settle()
            assert events == []

    @pytest.mark.asyncio
    async def test_reply_to_bot_triggers_with_quoted_text(self, monkeypatch):
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            fake.action_data["get_msg"] = quoted_message_data(
                sender_id=SELF_ID, nickname="bot", message="bot 之前说的话")
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(group_event([
                {"type": "reply", "data": {"id": "999"}},
                {"type": "text", "data": {"text": "哈哈是的"}},
            ]))
            await wait_until(lambda: len(events) == 1)
            event = events[0]
            assert event.text == f"u{OTHER_USER}：\n哈哈是的"
            assert event.reply_to_message_id == "999"
            assert event.reply_to_text == "[bot] bot 之前说的话"
            assert event.reply_to_author_id == SELF_ID
            assert event.reply_to_author_name == "bot"
            assert event.reply_to_is_own_message is True
            # get_msg 参数：reply_id 已转 int
            frame = fake.request_for("get_msg")
            assert frame["params"] == {"message_id": 999}

    @pytest.mark.asyncio
    async def test_reply_to_other_user_not_triggered(self, monkeypatch):
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            fake.action_data["get_msg"] = quoted_message_data(
                sender_id=OTHER_USER2, nickname="路人", message="别人的话")
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(group_event([
                {"type": "reply", "data": {"id": "999"}},
                {"type": "text", "data": {"text": "跟你说"}},
            ]))
            await wait_until(lambda: fake.request_for("get_msg") is not None)
            await settle()
            assert events == []

    @pytest.mark.asyncio
    async def test_at_mention_with_no_text_ignored(self, monkeypatch):
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(group_event(
                [{"type": "at", "data": {"qq": SELF_ID}}]))
            await settle()
            assert events == []  # 触发但没有可处理文本

    @pytest.mark.asyncio
    async def test_group_outside_allowlist_rejected_before_any_api(self, monkeypatch):
        async with running_adapter(allowed_groups="999") as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(group_event([
                {"type": "at", "data": {"qq": SELF_ID}},
                {"type": "text", "data": {"text": "在吗"}},
            ]))
            await settle()
            assert events == []
            assert fake.requests == []  # 门控先行：白名单外连 get_msg 都不发


# ---------------------------------------------------------------------------
# 引用原文成功路径（私聊 + 群）
# ---------------------------------------------------------------------------

class TestQuotedMessage:
    @pytest.mark.asyncio
    async def test_quoted_text_in_context_dm(self, monkeypatch):
        async with running_adapter(allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            fake.action_data["get_msg"] = quoted_message_data(
                sender_id=OTHER_USER2, nickname="小明", message="明天去爬山吗")
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(private_event([
                {"type": "reply", "data": {"id": "999"}},
                {"type": "text", "data": {"text": "去！"}},
            ]))
            await wait_until(lambda: len(events) == 1)
            event = events[0]
            assert event.reply_to_text == "[小明] 明天去爬山吗"
            assert event.reply_to_author_id == str(int(OTHER_USER2))
            assert event.reply_to_author_name == "小明"
            assert event.reply_to_is_own_message is False
