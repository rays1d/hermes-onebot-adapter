"""test_outbound.py — 出站链路：markdown 剥离、句边界分段、send 全家、媒体回退链。

media 回退链用真实 WS 驱动：upload_file_stream 探测成功/失败、base64 兜底、
探测失败后不再重试。standalone（cron 投递）走真实 aiohttp HTTP server。
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from conftest import (
    GROUP_ID,
    OTHER_USER,
    api_requests,
    free_port,
    running_adapter,
)
from gateway.config import PlatformConfig
from onebot_plugin import standalone as standalone_mod
from onebot_plugin.formatting import QQ_TEXT_LIMIT, split_for_qq, strip_markdown


def _fast_timeout(adapter, monkeypatch, seconds: float = 0.2):
    """把指定 adapter 的 _call_api 等待上限压到 ``seconds``（仅 send_msg）。

    ``_send_message_segments`` 不透传 timeout 参数；这里包装实例方法，避免
    「已提交后超时」用例真等 30s。
    """
    original = adapter._call_api

    async def _wrapped(action, params=None, timeout=30.0):
        return await original(
            action, params, timeout=seconds if action == "send_msg" else timeout)

    monkeypatch.setattr(adapter, "_call_api", _wrapped)


# ---------------------------------------------------------------------------
# strip_markdown
# ---------------------------------------------------------------------------

class TestStripMarkdown:
    @pytest.mark.parametrize(("raw", "expected"), [
        ("## 标题\n正文", "标题\n正文"),
        ("### 深一级标题", "深一级标题"),
        ("**加粗** 与 *斜体* 与 __下划线斜体__", "加粗 与 斜体 与 下划线斜体"),
        ("[点这里](https://example.com)", "点这里 (https://example.com)"),
        ("![截图](https://example.com/a.png)", "[图片: 截图]"),
        ("```python\nprint('hi')\n```", "print('hi')"),
        ("行内 `code()` 保留", "行内 code() 保留"),
        ("~~划掉~~", "划掉"),
        ("- 项目一\n- 项目二", "• 项目一\n• 项目二"),
        ("1. 第一\n2. 第二", "第一\n第二"),
        ("> 引用内容", "> 引用内容"),
        ("---", ""),
        ("普通一句话", "普通一句话"),
    ])
    def test_cases(self, raw, expected):
        assert strip_markdown(raw) == expected

    def test_empty(self):
        assert strip_markdown("") == ""
        assert strip_markdown(None) is None  # 防御：原样返回

    def test_multiline_structure_preserved(self):
        text = "# 命令\n\n- foo\n- bar\n\n说明文字"
        assert strip_markdown(text) == "命令\n\n• foo\n• bar\n\n说明文字"

    def test_hrule_only_drops_its_own_line(self):
        """回归（真机 AC5）：水平线只删自身行，其后内容不得被吞。

        hermes cron 投递格式内嵌 `-------------` 分隔线；旧实现用
        `[\s\S]*$` 把 hr 之后到文末的全部内容吞掉，导致正文丢失。
        """
        cron_text = (
            "Cronjob Response: AC5-插件验收\n"
            "(job_id: 0d135509ec6b)\n"
            "-------------\n\n"
            "你好！这是一条通过 cron 定时投递的测试消息。👋\n\n"
            "To stop or manage this job, send me a new message."
        )
        out = strip_markdown(cron_text)
        assert "Cronjob Response" in out
        assert "你好" in out
        assert "stop or manage" in out
        assert "-" * 3 not in out  # hr 线本身被移除
        # 各类 hr 形态都只影响自身行
        assert strip_markdown("a\n***\nb") == "a\n\nb"
        assert strip_markdown("a\n___\nb") == "a\n\nb"
        assert strip_markdown("a\n- - -\nb") == "a\n\nb"
        # 非整行的重复字符不是 hr，不动
        assert strip_markdown("a--b") == "a--b"


# ---------------------------------------------------------------------------
# split_for_qq
# ---------------------------------------------------------------------------

class TestSplitForQQ:
    def test_short_text_single_chunk(self):
        assert split_for_qq("短句", 100) == ["短句"]

    def test_empty(self):
        assert split_for_qq("", 100) == []

    def test_sentence_boundary_preferred(self):
        text = "第一句。" * 8  # 40 字
        chunks = split_for_qq(text, 20)
        assert len(chunks) == 2
        assert all(len(c) <= 20 for c in chunks)
        assert "".join(chunks) == text  # 不丢字

    def test_newline_is_a_boundary(self):
        text = "甲乙丙丁\n" * 10
        chunks = split_for_qq(text, 12)
        assert chunks
        assert all(len(c) <= 12 for c in chunks)
        assert all(c.strip() for c in chunks)
        # 块尾换行被剥掉，但内容不丢字
        assert "".join(c.replace("\n", "") for c in chunks) == "甲乙丙丁" * 10

    def test_space_fallback(self):
        text = "word " * 10  # 无句末标点
        chunks = split_for_qq(text.rstrip(), 12)
        assert all(len(c) <= 12 for c in chunks)
        assert all(c.strip() for c in chunks)

    def test_hard_cut_without_boundaries(self):
        text = "无标点无空格" * 10  # 60 字连续文本
        chunks = split_for_qq(text, 25)
        assert len(chunks) >= 3
        assert all(len(c) <= 25 for c in chunks)
        assert "".join(chunks) == text

    def test_max_chars_floor(self):
        chunks = split_for_qq("abcdef", 0)
        assert chunks == ["a", "b", "c", "d", "e", "f"]

    def test_default_limit_constant(self):
        assert QQ_TEXT_LIMIT == 2000
        assert split_for_qq("短", QQ_TEXT_LIMIT) == ["短"]


# ---------------------------------------------------------------------------
# 文本出站（真实 WS）
# ---------------------------------------------------------------------------

def _counter_ids(fake, prefix=1000):
    """让每个 send_msg 回包带递增 message_id（多段 continuation 断言用）。"""
    counter = {"n": 0}

    def handler(action, params):
        if action == "send_msg":
            counter["n"] += 1
            return {"message_id": prefix + counter["n"]}
        return {}

    fake.action_handler = handler


@pytest.fixture
def no_chunk_delay(monkeypatch):
    """分段间隔归零：多段用例不吃 0.3s/段的防风控等待。"""
    monkeypatch.setattr(standalone_mod, "CHUNK_INTERVAL_S", 0.0)
    import onebot_plugin.adapter as adapter_mod
    monkeypatch.setattr(adapter_mod, "SEND_CHUNK_INTERVAL_S", 0.0)


class TestSendText:
    @pytest.mark.asyncio
    async def test_send_private_text(self, no_chunk_delay):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.action_data["send_msg"] = {"message_id": 77}
            result = await adapter.send(OTHER_USER, "你好呀")
            assert result.success is True
            assert result.message_id == "77"
            frame = fake.request_for("send_msg")
            assert frame["params"]["message_type"] == "private"
            assert frame["params"]["user_id"] == int(OTHER_USER)
            assert frame["params"]["message"] == [
                {"type": "text", "data": {"text": "你好呀"}}]

    @pytest.mark.asyncio
    async def test_send_group_target(self, no_chunk_delay):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            result = await adapter.send(f"group:{GROUP_ID}", "群里说")
            assert result.success is True
            frame = fake.request_for("send_msg")
            assert frame["params"]["group_id"] == int(GROUP_ID)
            assert frame["params"]["message_type"] == "group"

    @pytest.mark.asyncio
    async def test_markdown_stripped_before_send(self, no_chunk_delay):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            await adapter.send(OTHER_USER, "**重点** 看[文档](https://e.com/d)")
            frame = fake.request_for("send_msg")
            assert frame["params"]["message"] == [
                {"type": "text", "data": {"text": "重点 看文档 (https://e.com/d)"}}]

    @pytest.mark.asyncio
    async def test_empty_after_strip_sends_nothing(self, no_chunk_delay):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            result = await adapter.send(OTHER_USER, "``` ```")
            assert result.success is True
            assert api_requests(fake) == []

    @pytest.mark.asyncio
    async def test_long_text_split_with_continuation_ids(self, no_chunk_delay):
        async with running_adapter(max_msg_chars=20) as (adapter, harness):
            fake = await harness.dial_bot()
            _counter_ids(fake)
            text = "第一句话。" * 6  # 30 字 > 20
            result = await adapter.send(OTHER_USER, text)
            assert result.success is True
            frames = fake.requests_for("send_msg")
            assert len(frames) == 2
            sent = "".join(f["params"]["message"][0]["data"]["text"] for f in frames)
            assert sent == text  # 分段不丢字
            # message_id 取末段；前段进 continuation_message_ids
            assert result.message_id == "1002"
            assert result.continuation_message_ids == ("1001",)

    @pytest.mark.asyncio
    async def test_reply_to_only_on_first_chunk(self, no_chunk_delay):
        async with running_adapter(max_msg_chars=20) as (adapter, harness):
            fake = await harness.dial_bot()
            _counter_ids(fake)
            await adapter.send(OTHER_USER, "第一句话。" * 6, reply_to="42")
            frames = fake.requests_for("send_msg")
            assert frames[0]["params"]["message"][0] == {
                "type": "reply", "data": {"id": "42"}}
            assert frames[1]["params"]["message"][0]["type"] == "text"

    @pytest.mark.asyncio
    async def test_send_failure_propagates(self, no_chunk_delay):
        """明确 API 失败：不可重试，错误透传。"""
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.fail_actions.add("send_msg")
            result = await adapter.send(OTHER_USER, "会失败")
            assert result.success is False
            assert result.error is not None
            assert "fake failure" in result.error
            assert result.retryable is False

    @pytest.mark.asyncio
    async def test_send_without_connection_retryable(self, no_chunk_delay):
        """未连接（未提交）→ 可重试，基类可安全自动重试。"""
        async with running_adapter() as (adapter, harness):
            result = await adapter.send(OTHER_USER, "没人在线")
            assert result.success is False
            assert result.retryable is True

    @pytest.mark.asyncio
    async def test_submitted_timeout_not_retryable(self, no_chunk_delay, monkeypatch):
        """已提交后超时 → retryable=False + error_kind=unknown + submitted 标记。

        防重复消息核心：此时消息可能已送达，基类绝不自动重发。
        """
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.hold_actions.add("send_msg")
            _fast_timeout(adapter, monkeypatch)
            result = await adapter.send(OTHER_USER, "可能已送达")
            assert result.success is False
            assert result.retryable is False
            assert result.error_kind == "unknown"
            assert result.raw_response == {"submitted": True}
            assert "未知" in result.error

    @pytest.mark.asyncio
    async def test_connection_drop_after_submission_not_retryable(self, no_chunk_delay):
        """已提交后连接断开 → 同样按「结局未知」处理，不可重试。"""
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.drop_on_call = {"send_msg": {1}}
            result = await adapter.send(OTHER_USER, "断线前发出的")
            assert result.success is False
            assert result.retryable is False
            assert result.error_kind == "unknown"
            assert result.raw_response == {"submitted": True}

    @pytest.mark.asyncio
    async def test_later_chunk_failure_forces_not_retryable(self, no_chunk_delay):
        """第 3 段失败：前 2 段已发出——retryable 强制 False，
        message_id=末段成功 id，更早的已发段进 continuation。"""
        async with running_adapter(max_msg_chars=12) as (adapter, harness):
            fake = await harness.dial_bot()
            _counter_ids(fake)
            fake.fail_on_call = {"send_msg": {3}}
            text = "第一句话。" * 6  # 30 字 → 12 字 × 3 段
            result = await adapter.send(OTHER_USER, text)
            assert result.success is False
            assert result.retryable is False  # 重试会重复前 2 段
            assert result.message_id == "1002"  # 最后成功段
            assert result.continuation_message_ids == ("1001",)
            frames = fake.requests_for("send_msg")
            assert len(frames) == 3

    @pytest.mark.asyncio
    async def test_second_chunk_failure_reports_first_id(self, no_chunk_delay):
        """第 2 段失败：仅第 1 段已发出——message_id=第 1 段 id，continuation 空。"""
        async with running_adapter(max_msg_chars=12) as (adapter, harness):
            fake = await harness.dial_bot()
            _counter_ids(fake)
            fake.fail_on_call = {"send_msg": {2}}
            result = await adapter.send(OTHER_USER, "第一句话。" * 6)
            assert result.success is False
            assert result.retryable is False
            assert result.message_id == "1001"
            assert result.continuation_message_ids == ()

    @pytest.mark.asyncio
    async def test_first_chunk_failure_keeps_retryable_passthrough(self, no_chunk_delay):
        """首段即失败：未发出任何段，retryable 原样透传（API 失败 → False）。"""
        async with running_adapter(max_msg_chars=12) as (adapter, harness):
            fake = await harness.dial_bot()
            _counter_ids(fake)
            fake.fail_on_call = {"send_msg": {1}}
            result = await adapter.send(OTHER_USER, "第一句话。" * 6)
            assert result.success is False
            assert result.retryable is False  # OneBotApiError 的透传值
            assert result.message_id is None
            assert result.continuation_message_ids == ()
            assert len(fake.requests_for("send_msg")) == 1  # 未继续发后续段


# ---------------------------------------------------------------------------
# 媒体出站
# ---------------------------------------------------------------------------

class TestSendMedia:
    @pytest.mark.asyncio
    async def test_http_url_passthrough(self, no_chunk_delay):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            result = await adapter.send_image(OTHER_USER, "https://e.com/a.png",
                                              caption="看图")
            assert result.success is True
            frames = fake.requests_for("send_msg")
            assert len(frames) == 2  # caption 先行，图片随后
            assert frames[0]["params"]["message"] == [
                {"type": "text", "data": {"text": "看图"}}]
            assert frames[1]["params"]["message"] == [
                {"type": "image", "data": {"file": "https://e.com/a.png"}}]
            assert fake.requests_for("upload_file_stream") == []  # URL 不走上传

    @pytest.mark.asyncio
    async def test_stream_upload_success_uses_napcat_path(self, no_chunk_delay, tmp_path):
        local = tmp_path / "pic.png"
        local.write_bytes(b"\x89PNG-fake-image-bytes")
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()

            def handler(action, params):
                if action == "upload_file_stream":
                    if params.get("is_complete"):
                        return {"file_path": "C:/NapCat/Temp/pic.png"}
                    return {"received_chunks": params.get("chunk_index", 0) + 1}
                if action == "send_msg":
                    return {"message_id": 321}
                return {}

            fake.action_handler = handler
            result = await adapter.send_image(OTHER_USER, str(local))
            assert result.success is True
            assert result.message_id == "321"
            upload_frames = fake.requests_for("upload_file_stream")
            # 小文件：一个分块帧 + 一个 is_complete 帧
            assert len(upload_frames) == 2
            chunk_frame = upload_frames[0]["params"]
            assert chunk_frame["total_chunks"] == 1
            assert base64.b64decode(chunk_frame["chunk_data"]) == local.read_bytes()
            assert upload_frames[1]["params"]["is_complete"] is True
            send_frame = fake.request_for("send_msg")
            assert send_frame["params"]["message"] == [
                {"type": "image", "data": {"file": "C:/NapCat/Temp/pic.png"}}]

    @pytest.mark.asyncio
    async def test_stream_failure_falls_back_to_base64_and_never_retries(
            self, no_chunk_delay, tmp_path):
        local = tmp_path / "pic.png"
        local.write_bytes(b"hello-image")
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.fail_actions.add("upload_file_stream")

            result = await adapter.send_image(OTHER_USER, str(local))
            assert result.success is True
            expected = "base64://" + base64.b64encode(b"hello-image").decode()
            frame = fake.request_for("send_msg")
            assert frame["params"]["message"] == [
                {"type": "image", "data": {"file": expected}}]
            assert adapter._upload_unsupported is True

            # 第二次不再探测 upload_file_stream（永久回退）
            await adapter.send_image(OTHER_USER, str(local))
            assert len(fake.requests_for("upload_file_stream")) == 1

    @pytest.mark.asyncio
    async def test_voice_and_video_segments(self, no_chunk_delay, tmp_path):
        voice = tmp_path / "a.mp3"
        voice.write_bytes(b"voice-bytes")
        video = tmp_path / "v.mp4"
        video.write_bytes(b"video-bytes")
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.fail_actions.add("upload_file_stream")  # 直接走 base64 兜底

            await adapter.send_voice(OTHER_USER, str(voice))
            await adapter.send_video(OTHER_USER, str(video))
            frames = fake.requests_for("send_msg")
            assert frames[0]["params"]["message"] == [{
                "type": "record",
                "data": {"file": "base64://" + base64.b64encode(b"voice-bytes").decode()}}]
            assert frames[1]["params"]["message"] == [{
                "type": "video",
                "data": {"file": "base64://" + base64.b64encode(b"video-bytes").decode()}}]

    @pytest.mark.asyncio
    async def test_document_uses_upload_file_actions_with_file_uri_fallback(
            self, no_chunk_delay, tmp_path):
        doc = tmp_path / "报告.pdf"
        doc.write_bytes(b"%PDF-fake")
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.fail_actions.add("upload_file_stream")

            result = await adapter.send_document(OTHER_USER, str(doc), caption="附件")
            assert result.success is True
            frame = fake.request_for("upload_private_file")
            assert frame["params"]["user_id"] == int(OTHER_USER)
            assert frame["params"]["name"] == "报告.pdf"
            assert frame["params"]["file"].startswith("file://")
            assert frame["params"]["file"].endswith("报告.pdf")
            # caption 紧随其后补发
            caption_frame = fake.request_for("send_msg")
            assert caption_frame["params"]["message"] == [
                {"type": "text", "data": {"text": "附件"}}]

    @pytest.mark.asyncio
    async def test_document_group_target_uses_upload_group_file(
            self, no_chunk_delay, tmp_path):
        doc = tmp_path / "data.csv"
        doc.write_bytes(b"a,b\n1,2\n")
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.fail_actions.add("upload_file_stream")

            await adapter.send_document(f"group:{GROUP_ID}", str(doc))
            frame = fake.request_for("upload_group_file")
            assert frame["params"]["group_id"] == int(GROUP_ID)
            assert frame["params"]["name"] == "data.csv"

    @pytest.mark.asyncio
    async def test_stream_success_document_uses_napcat_path(
            self, no_chunk_delay, tmp_path):
        doc = tmp_path / "data.csv"
        doc.write_bytes(b"a,b\n1,2\n")
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()

            def handler(action, params):
                if action == "upload_file_stream":
                    if params.get("is_complete"):
                        return {"file_path": "C:/NapCat/Temp/data.csv"}
                    return {"received_chunks": 1}
                return {}

            fake.action_handler = handler
            await adapter.send_document(OTHER_USER, str(doc))
            frame = fake.request_for("upload_private_file")
            assert frame["params"]["file"] == "C:/NapCat/Temp/data.csv"


# ---------------------------------------------------------------------------
# get_chat_info
# ---------------------------------------------------------------------------

class TestGetChatInfo:
    @pytest.mark.asyncio
    async def test_group_info(self, no_chunk_delay):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.action_data["get_group_info"] = {"group_name": "测试群"}
            info = await adapter.get_chat_info(f"group:{GROUP_ID}")
            assert info == {"name": "测试群", "type": "group",
                            "chat_id": f"group:{GROUP_ID}"}

    @pytest.mark.asyncio
    async def test_dm_info(self, no_chunk_delay):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.action_data["get_stranger_info"] = {"nickname": "路人甲"}
            info = await adapter.get_chat_info(OTHER_USER)
            assert info == {"name": "路人甲", "type": "dm", "chat_id": OTHER_USER}

    @pytest.mark.asyncio
    async def test_api_failure_degrades_to_raw_chat_id(self, no_chunk_delay):
        async with running_adapter() as (adapter, harness):
            fake = await harness.dial_bot()
            fake.fail_actions.add("get_group_info")
            info = await adapter.get_chat_info(f"group:{GROUP_ID}")
            assert info["name"] == f"group:{GROUP_ID}"
            assert info["type"] == "group"


# ---------------------------------------------------------------------------
# standalone（cron 投递，真实 aiohttp HTTP server）
# ---------------------------------------------------------------------------

class FakeHTTPAPI:
    """NapCat OneBot HTTP API 测试替身。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.response: dict = {"status": "ok", "retcode": 0, "data": {"message_id": 77}}
        self.status = 200
        self.runner = None

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append({
            "path": request.path,
            "authorization": request.headers.get("Authorization", ""),
            "body": await request.json(),
        })
        return web.json_response(self.response, status=self.status)

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/{action}", self.handle)
        self.port = free_port()
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", self.port)
        await site.start()
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    def request_for(self, action: str) -> dict | None:
        for item in reversed(self.requests):
            if item["path"] == f"/{action}":
                return item
        return None


@asynccontextmanager
async def running_http_api():
    api = FakeHTTPAPI()
    base_url = await api.start()
    try:
        yield api, base_url
    finally:
        await api.stop()


def _pconfig(**extra) -> PlatformConfig:
    return PlatformConfig(enabled=True, extra={"token": "tok", **extra})


class TestStandaloneSend:
    @pytest.mark.asyncio
    async def test_private_delivery(self, monkeypatch, no_chunk_delay):
        async with running_http_api() as (api, base_url):
            monkeypatch.setenv("ONEBOT_HTTP_URL", base_url)
            result = await standalone_mod._standalone_send(
                _pconfig(), OTHER_USER, "**晚上** 吃火锅")
            assert result == {"success": True, "message_id": "77"}
            req = api.request_for("send_private_msg")
            assert req is not None
            assert req["authorization"] == "Bearer tok"
            assert req["body"]["user_id"] == int(OTHER_USER)
            assert req["body"]["message"] == [
                {"type": "text", "data": {"text": "晚上 吃火锅"}}]

    @pytest.mark.asyncio
    async def test_group_delivery(self, monkeypatch, no_chunk_delay):
        async with running_http_api() as (api, base_url):
            monkeypatch.setenv("ONEBOT_HTTP_URL", base_url)
            result = await standalone_mod._standalone_send(
                _pconfig(), f"group:{GROUP_ID}", "群里通知")
            assert result["success"] is True
            req = api.request_for("send_group_msg")
            assert req["body"]["group_id"] == int(GROUP_ID)
            assert "user_id" not in req["body"]

    @pytest.mark.asyncio
    async def test_multi_chunk_delivery(self, monkeypatch, no_chunk_delay):
        # standalone 固定按 QQ_TEXT_LIMIT 分段；monkeypatch 模块常量避免 2000+ 字正文
        monkeypatch.setattr(standalone_mod, "QQ_TEXT_LIMIT", 20)
        async with running_http_api() as (api, base_url):
            monkeypatch.setenv("ONEBOT_HTTP_URL", base_url)
            text = "第一句话。" * 6  # 30 字 → 2 段
            result = await standalone_mod._standalone_send(_pconfig(), OTHER_USER, text)
            assert result["success"] is True
            assert result["message_id"] == "77"  # 末段 id
            frames = [r for r in api.requests if r["path"] == "/send_private_msg"]
            assert len(frames) == 2
            joined = "".join(f["body"]["message"][0]["data"]["text"] for f in frames)
            assert joined == text

    @pytest.mark.asyncio
    async def test_missing_http_url_reports_error(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        result = await standalone_mod._standalone_send(_pconfig(), OTHER_USER, "hi")
        assert "error" in result
        assert "ONEBOT_HTTP_URL" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_chat_id_reports_error(self, monkeypatch):
        async with running_http_api() as (api, base_url):
            monkeypatch.setenv("ONEBOT_HTTP_URL", base_url)
            result = await standalone_mod._standalone_send(_pconfig(), "not-a-qq", "hi")
            assert "error" in result
            assert api.requests == []

    @pytest.mark.asyncio
    async def test_http_error_status_reports_error(self, monkeypatch):
        async with running_http_api() as (api, base_url):
            monkeypatch.setenv("ONEBOT_HTTP_URL", base_url)
            api.status = 500
            result = await standalone_mod._standalone_send(_pconfig(), OTHER_USER, "hi")
            assert "error" in result
            assert "500" in result["error"]

    @pytest.mark.asyncio
    async def test_failed_body_reports_error(self, monkeypatch):
        async with running_http_api() as (api, base_url):
            monkeypatch.setenv("ONEBOT_HTTP_URL", base_url)
            api.response = {"status": "failed", "retcode": 1200, "wording": "被风控"}
            result = await standalone_mod._standalone_send(_pconfig(), OTHER_USER, "hi")
            assert "error" in result
            assert "被风控" in result["error"]
