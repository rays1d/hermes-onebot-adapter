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
    api_requests,
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
            assert api_requests(fake) == []

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
            assert api_requests(fake) == []  # 门控先行：白名单外连 get_msg 都不发


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


# ---------------------------------------------------------------------------
# 群内待答穿透（clarify pending → 未 @ 的回答也触发）
# ---------------------------------------------------------------------------

class TestGroupPendingFollowup:
    """agent 在群里发起澄清后，用户的裸回答（不 @ 机器人）也必须到达网关。

    穿透只发生在已过门控的消息上（权限不放松）；查询失败按「无提示」
    fail-closed。session key 用与 ``_group_pending_prompt`` 同款的 provisional
    派生（build_source 同参数 + ``_event_session_key``）。
    """

    @staticmethod
    def _pending_key(adapter, user_id: str) -> str:
        from gateway.platforms.event import MessageEvent
        source = adapter.build_source(
            chat_id=f"group:{GROUP_ID}", chat_type="group", user_id=str(user_id),
            user_name=f"u{user_id}", message_id="pending")
        return adapter._event_session_key(
            MessageEvent(text="", source=source, message_id="pending"))

    @pytest.fixture(autouse=True)
    def _clear_clarify_state(self):
        from tools import clarify_gateway
        yield
        clarify_gateway._entries.clear()
        clarify_gateway._session_index.clear()

    @pytest.mark.asyncio
    async def test_unmentioned_group_answer_reaches_agent_when_pending(
            self, monkeypatch):
        """门控放行 + 群内 + 未 @ + 有待答澄清 → 视为触发进网关。"""
        from tools import clarify_gateway
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            key = self._pending_key(adapter, OTHER_USER)
            clarify_gateway.register("cl-test", key, "选 A 还是 B？", None)
            await fake.push_event(group_event("A"))  # 未 @、未回复 bot
            await wait_until(lambda: len(events) == 1)
            event = events[0]
            assert event.text == f"u{OTHER_USER}：\nA"  # 昵称前缀照加
            # provisional 派生与真实事件一致——这是穿透能命中的前提
            assert adapter._event_session_key(event) == key

    @pytest.mark.asyncio
    async def test_no_pending_plain_group_message_still_ignored(self, monkeypatch):
        """无待答提示时维持原判：未 @ 的群消息仍是闲聊，不误放。"""
        from tools import clarify_gateway
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            assert clarify_gateway.get_pending_for_session(
                self._pending_key(adapter, OTHER_USER)) is None
            await fake.push_event(group_event("今天天气不错"))
            await settle()
            assert events == []

    @pytest.mark.asyncio
    async def test_resolved_pending_no_longer_passes(self, monkeypatch):
        """pending 已被回答并收割后穿透关闭：后续裸消息回到原判。

        生产里 ``wait_for_response`` 的等待线程被唤醒后随即把条目从索引
        收割（见 clarify_gateway.wait_for_response）；测试里没有阻塞的
        agent 线程，按同一逻辑手动收割。
        """
        from tools import clarify_gateway
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            key = self._pending_key(adapter, OTHER_USER)
            clarify_gateway.register("cl-test", key, "选 A 还是 B？", None)
            assert clarify_gateway.resolve_gateway_clarify("cl-test", "A")
            with clarify_gateway._lock:  # wait_for_response 收割路径的等价操作
                clarify_gateway._entries.pop("cl-test", None)
                ids = clarify_gateway._session_index.get(key) or []
                if "cl-test" in ids:
                    ids.remove("cl-test")
                if not ids:
                    clarify_gateway._session_index.pop(key, None)
            await fake.push_event(group_event("今天天气不错"))
            await settle()
            assert events == []

    @pytest.mark.asyncio
    async def test_other_user_session_pending_does_not_pass(self, monkeypatch):
        """群会话按成员隔离（group_sessions_per_user 默认开）：别人的待答不放行。"""
        from tools import clarify_gateway
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            clarify_gateway.register(
                "cl-test", self._pending_key(adapter, OTHER_USER), "选 A 还是 B？", None)
            await fake.push_event(group_event("我也答一个", user_id=OTHER_USER2))
            await settle()
            assert events == []

    @pytest.mark.asyncio
    async def test_gate_denied_user_pending_never_consulted(self, monkeypatch):
        """权限不放松：门控拒绝的消息直接返回，pending 查询不执行、零 API 请求。"""
        from tools import clarify_gateway
        original = clarify_gateway.get_pending_for_session
        consulted: list[str] = []

        def spy(session_key, *, include_choice_prompts=False):
            consulted.append(session_key)
            return original(session_key, include_choice_prompts=include_choice_prompts)

        monkeypatch.setattr(clarify_gateway, "get_pending_for_session", spy)
        async with running_adapter(allowed_groups=GROUP_ID,
                                   group_allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            clarify_gateway.register(
                "cl-test", self._pending_key(adapter, OTHER_USER2), "选 A 还是 B？", None)
            await fake.push_event(group_event("放我答", user_id=OTHER_USER2))
            await settle()
            assert events == []
            assert consulted == []  # 门控先行：pending 查询根本不发生
            assert api_requests(fake) == []

    @pytest.mark.asyncio
    async def test_clarify_import_failure_fails_closed(self, monkeypatch):
        """clarify 模块导入失败 → 静默维持原判（fail-closed，绝不放行）。"""
        import sys
        monkeypatch.setitem(sys.modules, "tools.clarify_gateway", None)
        async with running_adapter(**GATE_EXTRA) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(group_event("回答澄清"))
            await settle()
            assert events == []


# ---------------------------------------------------------------------------
# 媒体注册表环 + get_msg 兜底（真机表情包取图失败的修复）
# ---------------------------------------------------------------------------

class TestMediaRegistryRing:
    @pytest.mark.asyncio
    async def test_media_id_resolves_across_messages(self, monkeypatch):
        """图消息之后来了纯文本消息，旧 media_id 仍可解析（环保留 10 条）。"""
        async with running_adapter(allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(private_event(
                [{"type": "image", "data": {"url": "https://e.com/cat.jpg",
                                            "file": "cat.jpg"}}], message_id=1001))
            await wait_until(lambda: len(events) == 1)
            await fake.push_event(private_event("第二条纯文本", message_id=1002))
            await wait_until(lambda: len(events) == 2)
            ref = await adapter.resolve_media_ref("m1")
            assert ref is not None
            assert ref.url == "https://e.com/cat.jpg"

    @pytest.mark.asyncio
    async def test_ring_eviction_after_ten_messages(self, monkeypatch):
        async with running_adapter(allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(private_event(
                [{"type": "image", "data": {"url": "https://e.com/cat.jpg"}}],
                message_id=1001))
            await wait_until(lambda: len(events) == 1)
            for i in range(2, 13):  # 再来 11 条纯文本 → 最老的注册表被挤出
                await fake.push_event(private_event(f"msg{i}", message_id=1000 + i))
            await wait_until(lambda: len(events) == 12)
            assert await adapter.resolve_media_ref("m1") is None

    @pytest.mark.asyncio
    async def test_empty_ref_backfilled_via_get_msg(self, monkeypatch, caplog):
        """事件段缺 url/file（真机表情包场景）→ get_msg 兜底回写 url/file。"""
        async with running_adapter(allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(private_event(
                [{"type": "image", "data": {}}],  # 实现端没带 url/file
                message_id=1001))
            await wait_until(lambda: len(events) == 1)
            assert adapter.current_registry().get("m1").url is None
            fake.action_data["get_msg"] = {
                "message_id": 1001,
                "message": [{"type": "image", "data": {
                    "url": "https://e.com/sticker.gif", "file": "stk.gif"}}],
            }
            ref = await adapter.resolve_media_ref("m1")
            assert ref is not None
            assert ref.url == "https://e.com/sticker.gif"
            assert ref.file == "stk.gif"

    @pytest.mark.asyncio
    async def test_get_msg_failure_returns_original_ref(self, monkeypatch):
        async with running_adapter(allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(private_event([{"type": "image", "data": {}}],
                                                message_id=1001))
            await wait_until(lambda: len(events) == 1)
            fake.fail_actions.add("get_msg")  # 兜底失败不抛异常
            ref = await adapter.resolve_media_ref("m1")
            assert ref is not None
            assert ref.url is None and ref.file is None

    @pytest.mark.asyncio
    async def test_unknown_media_id_returns_none(self, monkeypatch):
        async with running_adapter(allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event(private_event("你好"))
            await wait_until(lambda: len(events) == 1)
            assert await adapter.resolve_media_ref("m9") is None
