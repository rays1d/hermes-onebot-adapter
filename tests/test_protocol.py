"""test_protocol.py — OneBot action 通道与连接生命周期（真实 WS 传输）。

覆盖：echo 匹配、超时、status=failed / retcode≠0、未连接、token 鉴权
（401 / query token）、同端点新连接挤旧（旧 pending 失败）、回包串台防御、
meta 心跳、坏帧容错。
"""

from __future__ import annotations

import asyncio
import re

import aiohttp
import pytest

from conftest import (
    SELF_ID,
    TEST_TOKEN,
    FakeNapCat,
    running_adapter,
)
from onebot_plugin.adapter import (
    NetworkError,
    NotConnectedError,
    OneBotApiError,
)

ECHO_RE = re.compile(r"^\d+:\d+$")


# ---------------------------------------------------------------------------
# echo / future 机制
# ---------------------------------------------------------------------------

class TestEchoMatching:
    @pytest.mark.asyncio
    async def test_call_api_roundtrip_and_echo_format(self):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            payload = await adapter._call_api("get_version_info", {})
            assert payload["status"] == "ok"
            assert payload["retcode"] == 0
            frame = fake.request_for("get_version_info")
            assert frame is not None
            assert frame["params"] == {}
            assert ECHO_RE.match(frame["echo"])  # "{generation}:{seq}"

    @pytest.mark.asyncio
    async def test_concurrent_calls_get_distinct_echos(self):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            results = await asyncio.gather(
                adapter._call_api("get_login_info", {}),
                adapter._call_api("get_status", {}),
                adapter._call_api("get_version_info", {}),
            )
            assert all(r["status"] == "ok" for r in results)
            echoes = [f["echo"] for f in fake.requests]
            assert len(set(echoes)) == 3
            assert [int(e.split(":")[1]) for e in echoes] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_timeout_raises_network_error_and_cleans_pending(self):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.hold_actions.add("get_status")
            with pytest.raises(NetworkError):
                await adapter._call_api("get_status", {}, timeout=0.2)
            assert adapter._pending == {}  # 超时后 pending 不泄漏

    @pytest.mark.asyncio
    async def test_failed_status_raises_api_error(self):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.fail_actions.add("get_group_info")
            with pytest.raises(OneBotApiError) as exc_info:
                await adapter._call_api("get_group_info", {"group_id": 1})
            assert exc_info.value.retcode == 1200
            assert "fake failure for get_group_info" in exc_info.value.wording
            assert exc_info.value.action == "get_group_info"

    @pytest.mark.asyncio
    async def test_ok_status_with_nonzero_retcode_also_fails(self):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.custom_responses["get_friend_list"] = {
                "status": "ok", "retcode": 500, "wording": "boom", "data": {}}
            with pytest.raises(OneBotApiError) as exc_info:
                await adapter._call_api("get_friend_list", {})
            assert exc_info.value.retcode == 500
            assert "boom" in exc_info.value.wording

    @pytest.mark.asyncio
    async def test_not_connected_raises_retryable(self):
        async with running_adapter() as (adapter, harness):
            # server 已起但实现端未拨入
            with pytest.raises(NotConnectedError) as exc_info:
                await adapter._call_api("get_login_info", {})
            assert exc_info.value.retryable is True


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------

class TestAuth:
    @pytest.mark.asyncio
    async def test_wrong_token_rejected_with_401(self):
        async with running_adapter() as (adapter, harness):
            fake = FakeNapCat(harness.url, "wrong-token")
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc_info:
                await fake.connect()
            assert exc_info.value.status == 401
            await fake.close()
            assert adapter._ws is None  # 未建立任何连接

    @pytest.mark.asyncio
    async def test_missing_token_rejected_with_401(self):
        async with running_adapter() as (adapter, harness):
            fake = FakeNapCat(harness.url, None)
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc_info:
                await fake.connect()
            assert exc_info.value.status == 401
            await fake.close()

    @pytest.mark.asyncio
    async def test_query_token_accepted(self):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial(token=TEST_TOKEN, token_in_query=True)
            payload = await adapter._call_api("get_login_info", {})
            assert payload["status"] == "ok"
            assert fake.request_for("get_login_info") is not None


# ---------------------------------------------------------------------------
# 连接生命周期
# ---------------------------------------------------------------------------

class TestConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_new_connection_supersedes_old(self):
        async with running_adapter() as (adapter, harness):
            fake1 = await harness.dial_bot()
            ws1 = adapter._ws
            fake1.hold_actions.add("get_status")

            task = asyncio.create_task(adapter._call_api("get_status", {}, timeout=5))
            await fake1.wait_action("get_status")

            # 同端点新连接拨入：挤掉旧连接
            fake2 = await harness.dial_bot()
            with pytest.raises(NotConnectedError):
                await asyncio.wait_for(task, 2)  # 旧连接 pending 被明确失败

            assert adapter._generation == 2
            ws2 = adapter._ws
            assert ws2 is not None and ws2 is not ws1 and ws1.closed
            assert adapter.self_id == SELF_ID  # 新连接的 lifecycle 已上报
            # 新连接可正常调用
            payload = await adapter._call_api("get_version_info", {})
            assert payload["status"] == "ok"
            assert fake2.request_for("get_version_info") is not None

    @pytest.mark.asyncio
    async def test_disconnect_fails_pending_and_closes_client(self):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.hold_actions.add("get_status")
            task = asyncio.create_task(adapter._call_api("get_status", {}, timeout=5))
            await fake.wait_action("get_status")
            await adapter.disconnect()
            with pytest.raises(NotConnectedError):
                await task
            await asyncio.wait_for(fake.closed_event.wait(), 2)

    @pytest.mark.asyncio
    async def test_lifecycle_and_heartbeat_recorded(self):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            assert adapter.self_id == SELF_ID  # lifecycle.connect 记录 self_id
            assert adapter._last_heartbeat == 0.0
            await fake.push_event({
                "post_type": "meta_event", "meta_event_type": "heartbeat",
                "self_id": SELF_ID})
            await asyncio.sleep(0.05)
            assert adapter._last_heartbeat > 0


# ---------------------------------------------------------------------------
# 回包匹配防御
# ---------------------------------------------------------------------------

class TestResponseGuard:
    @pytest.mark.asyncio
    async def test_response_from_foreign_connection_dropped(self):
        """非发起连接递来的回包不得 resolve pending（串台防御，丢弃留超时）。"""
        async with running_adapter() as (adapter, harness):
            await harness.dial_bot()
            loop = asyncio.get_running_loop()
            stale_ws = object()  # 伪装旧连接的 owner
            fut = loop.create_future()
            adapter._pending["1:1"] = (stale_ws, fut)
            adapter._resolve_response("1:1", adapter._ws, {"status": "ok", "retcode": 0})
            assert not fut.done()
            assert "1:1" in adapter._pending

    @pytest.mark.asyncio
    async def test_stale_generation_echo_without_pending_ignored(self):
        """旧连接代号的 echo 已无 pending（被清理过）：丢弃并告警，不炸不误配。"""
        async with running_adapter() as (adapter, harness):
            await harness.dial_bot()
            stale_echo = f"{adapter._generation + 50}:1"
            adapter._resolve_response(stale_echo, adapter._ws, {"status": "ok"})
            assert adapter._pending == {}

    @pytest.mark.asyncio
    async def test_non_json_frame_ignored(self):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            await fake.ws.send_str("not-json{{{")
            await fake.push_event({
                "post_type": "meta_event", "meta_event_type": "heartbeat",
                "self_id": SELF_ID})
            await asyncio.sleep(0.05)
            assert adapter._last_heartbeat > 0  # 连接未被坏帧破坏

    @pytest.mark.asyncio
    async def test_message_sent_echo_self_ignored(self, monkeypatch):
        """go-cqhttp 系 ``message_sent`` 回显等同 self 消息：不进 agent。"""
        from conftest import install_message_handler, settle
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event({
                "post_type": "message_sent", "message_type": "private",
                "message_id": 777, "user_id": 999,
                "sender": {"user_id": 999, "nickname": "x"}, "message": "echo"})
            await settle()
            assert events == []
