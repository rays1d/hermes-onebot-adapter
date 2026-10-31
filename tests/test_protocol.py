"""test_protocol.py — OneBot action 通道与连接生命周期（真实 WS 传输）。

覆盖：echo 匹配、超时、status=failed / retcode≠0、未连接、token 鉴权
（401 / query token）、同端点新连接挤旧（旧 pending 失败）、回包串台防御、
meta 心跳、坏帧容错。
"""

from __future__ import annotations

import asyncio
import json
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
    UnknownOutcome,
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
    async def test_submitted_timeout_raises_unknown_outcome_and_cleans_pending(self):
        """已提交后回包超时 → UnknownOutcome（结果未知，绝不回落为可重试失败）。"""
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.hold_actions.add("get_status")
            with pytest.raises(UnknownOutcome) as exc_info:
                await adapter._call_api("get_status", {}, timeout=0.2)
            assert exc_info.value.action == "get_status"
            assert adapter._pending == {}  # 超时后 pending 不泄漏

    @pytest.mark.asyncio
    async def test_unknown_outcome_when_connection_drops_after_submission(self):
        """已提交后连接断开 → UnknownOutcome（不得回落为可重试的 NotConnectedError）。"""
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.drop_on_call = {"get_status": {1}}
            task = asyncio.create_task(adapter._call_api("get_status", {}, timeout=5))
            await fake.wait_action("get_status")
            with pytest.raises(UnknownOutcome) as exc_info:
                await asyncio.wait_for(task, 2)
            assert exc_info.value.action == "get_status"

    @pytest.mark.asyncio
    async def test_send_failure_before_submission_is_retryable(self):
        """ws.send 本身失败（未提交）→ NetworkError，retryable=True。"""
        async with running_adapter() as (adapter, harness):
            await harness.dial_bot()

            class ExplodingWs:
                closed = False

                async def send_json(self, payload):
                    raise ConnectionError("boom")

            adapter._ws = ExplodingWs()
            with pytest.raises(NetworkError) as exc_info:
                await adapter._call_api("get_status", {}, timeout=1)
            assert exc_info.value.retryable is True
            assert adapter._pending == {}  # 提交失败不泄漏 pending

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
    async def test_async_status_is_not_a_failure(self):
        """status=async（限速/已提交异步处理）：不算失败，按成功返回。"""
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.custom_responses["send_msg"] = {
                "status": "async", "retcode": 1, "data": None}
            payload = await adapter._call_api("send_msg", {"message": []})
            assert payload["status"] == "async"
            assert payload["retcode"] == 1

    @pytest.mark.asyncio
    async def test_retcode_1_with_unrecognized_status_is_not_a_failure(self):
        """retcode=1 = 已提交处理（communication/http.md），不得谎报失败。"""
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.custom_responses["send_msg"] = {
                "status": "", "retcode": 1, "data": None}
            payload = await adapter._call_api("send_msg", {"message": []})
            assert payload["retcode"] == 1

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
            with pytest.raises(UnknownOutcome):
                await asyncio.wait_for(task, 2)  # 请求已提交但连接被替换：结局未知

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
            with pytest.raises(UnknownOutcome):
                await task  # 请求已提交但连接关闭：结局未知
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
    async def test_echo_frame_never_enters_message_flow(self, monkeypatch):
        """带非空 echo 的帧一律按回包处理：即使携带 post_type 也绝不进消息流。"""
        from conftest import install_message_handler, settle
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            events = install_message_handler(adapter, monkeypatch)
            await fake.push_event({
                "echo": "7:1", "post_type": "message", "message_type": "private",
                "message_id": 555, "user_id": 999,
                "sender": {"user_id": 999, "nickname": "x"}, "message": "hi"})
            await settle()
            assert events == []
            # 连接未被破坏：后续正常事件照常处理
            await fake.push_event({
                "post_type": "meta_event", "meta_event_type": "heartbeat",
                "self_id": SELF_ID})
            await asyncio.sleep(0.05)
            assert adapter._last_heartbeat > 0

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


# ---------------------------------------------------------------------------
# 帧大小防御
# ---------------------------------------------------------------------------

class TestOversizedFrameGuard:
    @pytest.mark.asyncio
    async def test_oversized_frame_skipped_and_connection_survives(self):
        """超过处理阈值的单帧整体丢弃（连接保持）；协议层 50MB 硬上限兑底。"""
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            big = "x" * (4 * 1024 * 1024 + 128)  # > 4MB 处理阈值，< 50MB 协议上限
            await fake.ws.send_str(json.dumps(
                {"echo": "1:1", "post_type": "meta_event",
                 "meta_event_type": "heartbeat", "self_id": SELF_ID, "blob": big}))
            await asyncio.sleep(0.3)  # 大帧传输 + 跳过判定
            assert adapter._last_heartbeat == 0.0  # 大帧未进入分派
            # 连接存活：后续正常帧照常处理
            await fake.push_event({
                "post_type": "meta_event", "meta_event_type": "heartbeat",
                "self_id": SELF_ID})
            await asyncio.sleep(0.05)
            assert adapter._last_heartbeat > 0
