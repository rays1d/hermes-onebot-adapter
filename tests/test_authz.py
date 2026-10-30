"""test_authz.py — 分面策略门控（dm/group × open/allowlist/disabled × allow_all）。

单元矩阵直接打 ``_authorize``；另配集成测试验证拒绝路径（日志含完整 id、
零 API 请求）与 ``ONEBOT_GROUP_ALLOWED_USERS`` 收紧。
"""

from __future__ import annotations

import logging

import pytest

from conftest import (
    GROUP_ID,
    OTHER_USER,
    OTHER_USER2,
    SELF_ID,
    group_event,
    install_message_handler,
    make_adapter,
    private_event,
    running_adapter,
    settle,
)

# ---------------------------------------------------------------------------
# 私聊分面
# ---------------------------------------------------------------------------


class TestDmPolicy:
    def test_allowlist_empty_rejects_everyone(self):
        adapter = make_adapter()  # 默认 dm_policy=allowlist，无白名单
        assert adapter._authorize("dm", OTHER_USER) is False

    def test_allowlist_member_passes(self):
        adapter = make_adapter(allowed_users=OTHER_USER)
        assert adapter._authorize("dm", OTHER_USER) is True

    def test_open_passes_anyone(self):
        adapter = make_adapter(dm_policy="open")
        assert adapter._authorize("dm", "99999") is True

    def test_disabled_rejects_even_members(self):
        adapter = make_adapter(dm_policy="disabled", allowed_users=OTHER_USER)
        assert adapter._authorize("dm", OTHER_USER) is False

    def test_invalid_policy_falls_back_to_allowlist(self, caplog):
        with caplog.at_level(logging.WARNING):
            adapter = make_adapter(dm_policy="banana")
        assert adapter._dm_policy == "allowlist"
        assert adapter._authorize("dm", OTHER_USER) is False
        assert any("ONEBOT_DM_POLICY" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 群聊分面
# ---------------------------------------------------------------------------


class TestGroupPolicy:
    def test_allowlist_empty_rejects_every_group(self):
        adapter = make_adapter()
        assert adapter._authorize("group", OTHER_USER, GROUP_ID) is False

    def test_allowlist_member_group_passes(self):
        adapter = make_adapter(allowed_groups=GROUP_ID)
        assert adapter._authorize("group", OTHER_USER, GROUP_ID) is True

    def test_allowlist_other_group_rejected(self):
        adapter = make_adapter(allowed_groups=GROUP_ID)
        assert adapter._authorize("group", OTHER_USER, "888888") is False

    def test_star_passes_any_group(self):
        adapter = make_adapter(allowed_groups="*")
        assert adapter._authorize("group", OTHER_USER, "任意群") is True

    def test_open_passes_any_group(self):
        adapter = make_adapter(group_policy="open")
        assert adapter._authorize("group", OTHER_USER, "888888") is True

    def test_disabled_rejects_even_allowed_groups(self):
        adapter = make_adapter(group_policy="disabled", allowed_groups=GROUP_ID)
        assert adapter._authorize("group", OTHER_USER, GROUP_ID) is False

    def test_group_allowed_users_tightens(self):
        adapter = make_adapter(allowed_groups=GROUP_ID,
                               group_allowed_users=OTHER_USER)
        assert adapter._authorize("group", OTHER_USER, GROUP_ID) is True
        assert adapter._authorize("group", OTHER_USER2, GROUP_ID) is False

    def test_empty_group_allowed_users_means_all_members(self):
        adapter = make_adapter(allowed_groups=GROUP_ID)
        assert adapter._authorize("group", OTHER_USER2, GROUP_ID) is True

    def test_invalid_group_policy_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            adapter = make_adapter(group_policy="whatever")
        assert adapter._group_policy == "allowlist"
        assert adapter._authorize("group", OTHER_USER, GROUP_ID) is False


# ---------------------------------------------------------------------------
# allow_all 兑底 与 拒绝日志
# ---------------------------------------------------------------------------


class TestAllowAllAndLogging:
    def test_allow_all_overrides_everything(self):
        adapter = make_adapter(allow_all="true", dm_policy="disabled",
                               group_policy="disabled")
        assert adapter._authorize("dm", "99999") is True
        assert adapter._authorize("group", "99999", "888888") is True

    def test_allow_all_false_is_not_truthy(self):
        adapter = make_adapter(allow_all="false")
        assert adapter._allow_all is False
        assert adapter._authorize("dm", "99999") is False

    def test_dm_rejection_logs_full_user_id(self, caplog):
        adapter = make_adapter()  # allowlist 空名单
        with caplog.at_level(logging.INFO, logger="onebot_plugin.adapter"):
            assert adapter._authorize("dm", "123456789") is False
        assert any("123456789" in r.message for r in caplog.records)
        assert any("ONEBOT_ALLOWED_USERS" in r.message for r in caplog.records)

    def test_group_rejection_logs_full_ids(self, caplog):
        adapter = make_adapter()
        with caplog.at_level(logging.INFO, logger="onebot_plugin.adapter"):
            assert adapter._authorize("group", "123456789", "987654321") is False
        messages = " | ".join(r.message for r in caplog.records)
        assert "987654321" in messages and "123456789" in messages
        assert "ONEBOT_ALLOWED_GROUPS" in messages

    def test_group_member_rejection_logs_reason(self, caplog):
        adapter = make_adapter(allowed_groups=GROUP_ID, group_allowed_users=OTHER_USER)
        with caplog.at_level(logging.INFO, logger="onebot_plugin.adapter"):
            assert adapter._authorize("group", OTHER_USER2, GROUP_ID) is False
        assert any("ONEBOT_GROUP_ALLOWED_USERS" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 集成：拒绝路径端到端（真实 server + fake NapCat）
# ---------------------------------------------------------------------------


class TestAuthzIntegration:
    @pytest.mark.asyncio
    async def test_denied_dm_logs_and_never_calls_api(self, monkeypatch, caplog):
        async with running_adapter() as (adapter, harness):  # 空白名单
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            with caplog.at_level(logging.INFO, logger="onebot_plugin.adapter"):
                await fake.push_event(private_event("让我进", user_id=OTHER_USER))
                await settle()
            assert events == []
            assert fake.requests == []
            # 日志给出可复制的完整 QQ 号与配置提示
            messages = " | ".join(r.message for r in caplog.records)
            assert str(int(OTHER_USER)) in messages
            assert "ONEBOT_DM_POLICY" in messages

    @pytest.mark.asyncio
    async def test_denied_group_member_logs_group_and_user(self, monkeypatch, caplog):
        async with running_adapter(allowed_groups=GROUP_ID,
                                   group_allowed_users=OTHER_USER) as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            with caplog.at_level(logging.INFO, logger="onebot_plugin.adapter"):
                await fake.push_event(group_event(
                    [{"type": "at", "data": {"qq": SELF_ID}},
                     {"type": "text", "data": {"text": "在吗"}}],
                    user_id=OTHER_USER2))
                await settle()
            assert events == []
            messages = " | ".join(r.message for r in caplog.records)
            assert str(int(OTHER_USER2)) in messages
            assert "ONEBOT_GROUP_ALLOWED_USERS" in messages
