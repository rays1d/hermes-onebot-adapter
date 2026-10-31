"""Test bootstrap + fake NapCat harness（真实 aiohttp 传输，不 mock WS 层）。

启动方式（见 implement.md）::

    PYTHONPATH=../hermes-agent uv run --project ../hermes-agent --no-sync \
        python -m pytest tests/ -q

conftest 负责：
- 把 hermes-agent 仓库加入 ``sys.path``（或 ``HERMES_AGENT_ROOT`` 指定）
- 把插件仓库根加载成包 ``onebot_plugin``（子模块的相对导入依赖包上下文）
- 预创建 ``Platform("onebot")`` 伪成员（插件未注册进 hermes 的 platform_registry）
- 每个测试隔离 ``HERMES_HOME`` / ``HERMES_GATEWAY_LOCK_DIR``（adapter.connect()
  会写 runtime status 与 scoped lock，不能碰真实 ``~/.hermes``）
- :class:`FakeNapCat`：aiohttp WS client 拨入 adapter 起的**真实 server**，
  记录 action 请求并按 echo 自动回 ok（可按 action 配置 hold/fail/自定义 data），
  并能注入事件帧（``push_event``）
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent  # hermes-onebot-adapter/
DEFAULT_HERMES_ROOT = REPO_ROOT.parent / "hermes-agent"

TEST_TOKEN = "test-token"
SELF_ID = "10086"
OTHER_USER = "20001"
OTHER_USER2 = "20002"
GROUP_ID = "123456"


def _ensure_hermes_importable() -> None:
    try:
        import gateway.config  # noqa: F401
        return
    except ImportError:
        pass
    candidate = os.environ.get("HERMES_AGENT_ROOT") or str(DEFAULT_HERMES_ROOT)
    if (Path(candidate) / "gateway" / "config.py").is_file():
        sys.path.insert(0, candidate)
        return
    raise ImportError(
        "hermes-agent not importable — run tests via "
        "`uv run --project ../hermes-agent --no-sync python -m pytest tests/` "
        "or point HERMES_AGENT_ROOT at a hermes-agent checkout"
    )


_ensure_hermes_importable()


def _load_plugin_package() -> None:
    """把插件仓库根注册成包 ``onebot_plugin``（幂等）。

    仓库根的 ``__init__.py`` 暴露 register(ctx)，adapter/tools 等子模块用相对
    导入——必须有包上下文。目录名含连字符不能直接 import，故用显式 spec 加载。

    额外把该模块注册为 ``sys.modules["__init__"]``：pytest 9 的 Session 收集会把
    含 ``__init__.py`` 的 rootdir 也建成 Package 节点并以顶层名 ``__init__`` 导入
    它（importlib 模式 + ``module_name_from_path``），相对导入会炸。预注册后该
    导入命中缓存直接返回插件包模块，不再重复执行。
    """
    if "onebot_plugin" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "onebot_plugin", REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["onebot_plugin"] = module
    sys.modules.setdefault("__init__", module)
    spec.loader.exec_module(module)


_load_plugin_package()

from gateway.config import Platform, PlatformConfig  # noqa: E402

if "onebot" not in Platform._value2member_map_:
    Platform._add_pseudo_member("onebot")


# ---------------------------------------------------------------------------
# 隔离：HERMES_HOME（runtime status 文件）与 gateway lock 目录
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_hermes_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "gateway-locks"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# 等待助手
# ---------------------------------------------------------------------------

async def wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> None:
    """轮询直到 predicate 为真；超时抛 asyncio.TimeoutError（测试即失败）。"""

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout)


async def settle(seconds: float = 0.15) -> None:
    """给「不该发生的事」一个机会发生（否定断言前的宽限）。"""
    await asyncio.sleep(seconds)


def api_requests(fake: "FakeNapCat") -> list[dict]:
    """排除连接期身份探针后的 action 帧。

    连接建立后 adapter 会主动发一次 ``get_login_info``（身份确认，属连接
    生命周期而非消息处理）；「门控拒绝 ⇒ 零 API 请求」类断言应排除它。
    """
    return [f for f in fake.requests if f.get("action") != "get_login_info"]


# ---------------------------------------------------------------------------
# FakeNapCat：真实 WS client，拨入 adapter 的 server
# ---------------------------------------------------------------------------

class FakeNapCat:
    """OneBot 实现端的测试替身。

    - ``requests``：收到的所有 action 帧（``{action, params, echo}``）
    - 默认按 echo 回 ``{"status": "ok", "retcode": 0, "data": ...}``
    - ``hold_actions``：记录但不回（测超时 / 旧连接 pending 清理）
    - ``fail_actions``：回 ``status=failed``（测 OneBotApiError）
    - ``fail_on_call`` / ``drop_on_call``：按「第 N 次调用」精确控场——前者回 failed，
      后者收到即断开不回包（驱动「已提交后连接断开」路径）
    - ``action_data`` / ``action_handler``：自定义回包 data
    - ``push_event``：注入事件帧（无 echo，不触发自动回包）
    """

    def __init__(self, url: str, token: str | None = TEST_TOKEN, *,
                 token_in_query: bool = False):
        self.url = url
        self.token = token
        self.token_in_query = token_in_query
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.requests: list[dict] = []
        self.events_sent: list[dict] = []
        self.responses_sent: list[dict] = []
        self.hold_actions: set[str] = set()
        self.fail_actions: set[str] = set()
        self.fail_on_call: dict[str, set[int]] = {}  # action → 第 N 次调用回 failed（1 起）
        self.drop_on_call: dict[str, set[int]] = {}  # action → 第 N 次调用后断开不回包
        self._call_counts: dict[str, int] = {}
        self.custom_responses: dict[str, dict] = {}  # action → 完整响应体（echo 自动补）
        self.action_data: dict[str, dict] = {}
        self.action_handler = None  # callable(action, params) -> data dict
        self.closed_event = asyncio.Event()
        self._serve_task: asyncio.Task | None = None

    async def connect(self) -> "FakeNapCat":
        self.session = aiohttp.ClientSession()
        headers = {}
        url = self.url
        if self.token:
            if self.token_in_query:
                url = f"{url}?access_token={self.token}"
            else:
                headers["Authorization"] = f"Bearer {self.token}"
        self.ws = await self.session.ws_connect(url, headers=headers)
        self._serve_task = asyncio.create_task(self._serve())
        return self

    async def _serve(self) -> None:
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                frame = json.loads(msg.data)
                echo = frame.get("echo")
                action = str(frame.get("action") or "")
                self.requests.append(frame)
                if echo is None:
                    continue  # 事件帧：不回包
                call_no = self._call_counts[action] = self._call_counts.get(action, 0) + 1
                if call_no in self.drop_on_call.get(action, ()):
                    # 收到即断开、不回包：服务端 pending 走「已提交后断开」清理路径
                    with contextlib.suppress(Exception):
                        await self.ws.close()
                    return
                if call_no in self.fail_on_call.get(action, ()):
                    await self.send_response({
                        "status": "failed", "retcode": 1200,
                        "wording": f"fake failure #{call_no} for {action}", "echo": echo,
                    })
                    continue
                if action in self.hold_actions:
                    continue
                if action in self.custom_responses:
                    await self.send_response(
                        {**self.custom_responses[action], "echo": echo})
                    continue
                if action in self.fail_actions:
                    await self.send_response({
                        "status": "failed", "retcode": 1200,
                        "wording": f"fake failure for {action}", "echo": echo,
                    })
                    continue
                if self.action_handler is not None:
                    data = self.action_handler(action, frame.get("params") or {})
                else:
                    data = dict(self.action_data.get(action) or {})
                await self.send_response(
                    {"status": "ok", "retcode": 0, "data": data, "echo": echo})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # 连接被服务端关闭（挤占/断开）：静默退出
        finally:
            self.closed_event.set()

    async def send_response(self, payload: dict) -> None:
        self.responses_sent.append(payload)
        await self.ws.send_json(payload)

    async def push_event(self, payload: dict) -> None:
        self.events_sent.append(payload)
        await self.ws.send_json(payload)

    def request_for(self, action: str) -> dict | None:
        for frame in reversed(self.requests):
            if frame.get("action") == action:
                return frame
        return None

    def requests_for(self, action: str) -> list[dict]:
        return [frame for frame in self.requests if frame.get("action") == action]

    async def wait_action(self, action: str, timeout: float = 2.0) -> dict:
        await wait_until(lambda: self.request_for(action) is not None, timeout)
        return self.request_for(action)

    async def close(self) -> None:
        if self._serve_task is not None:
            self._serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._serve_task
            self._serve_task = None
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
            self.ws = None
        if self.session is not None:
            with contextlib.suppress(Exception):
                await self.session.close()
            self.session = None


# ---------------------------------------------------------------------------
# Adapter harness：真实 connect()（server 监听随机空闲端口）+ 拨入助手
# ---------------------------------------------------------------------------

class AdapterHarness:
    """起真实 OnebotAdapter（connect() 全链路）并让 FakeNapCat 拨入。"""

    def __init__(self, **extra):
        self.port = free_port()
        self.extra = {"token": TEST_TOKEN, "host": "127.0.0.1", "port": self.port, **extra}
        self.adapter = None

    async def start(self):
        from onebot_plugin.adapter import OnebotAdapter
        self.adapter = OnebotAdapter(PlatformConfig(enabled=True, extra=self.extra))
        assert await self.adapter.connect() is True
        return self.adapter

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{self.adapter._path}"

    async def dial(self, token: str | None = TEST_TOKEN,
                   **kwargs) -> FakeNapCat:
        fake = FakeNapCat(self.url, token, **kwargs)
        try:
            await fake.connect()
        except Exception:
            await fake.close()
            raise
        # 等服务端把连接登记为 live（ws_connect 返回与服务端 _ws 赋值之间有窗口期）
        await wait_until(lambda: self.adapter._live_ws() is not None)
        return fake

    async def dial_bot(self, *, self_id: str = SELF_ID, **kwargs) -> FakeNapCat:
        """拨入并上报 lifecycle.connect，adapter 记住 self_id（群触发判断依赖它）。"""
        fake = await self.dial(**kwargs)
        await fake.push_event({
            "post_type": "meta_event", "meta_event_type": "lifecycle",
            "sub_type": "connect", "self_id": self_id,
        })
        await wait_until(lambda: self.adapter.self_id == self_id)
        return fake

    async def stop(self) -> None:
        if self.adapter is not None:
            await self.adapter.disconnect()
            self.adapter = None


@asynccontextmanager
async def running_adapter(**extra):
    harness = AdapterHarness(**extra)
    adapter = await harness.start()
    try:
        yield adapter, harness
    finally:
        await harness.stop()


# ---------------------------------------------------------------------------
# 事件构造助手
# ---------------------------------------------------------------------------

def private_event(message, *, message_id=1001, user_id: str = OTHER_USER) -> dict:
    """私聊消息事件；``message`` 可为纯文本字符串或段数组。"""
    return {
        "post_type": "message", "message_type": "private",
        "message_id": message_id, "user_id": int(user_id),
        "sender": {"user_id": int(user_id), "nickname": f"u{user_id}"},
        "message": message, "raw_message": message if isinstance(message, str) else "",
    }


def group_event(message, *, message_id=2001, user_id: str = OTHER_USER,
                group_id: str = GROUP_ID, card: str | None = None) -> dict:
    """群消息事件；``card`` 模拟群名片（优先于 nickname）。"""
    sender = {"user_id": int(user_id), "nickname": f"u{user_id}"}
    if card:
        sender["card"] = card
    return {
        "post_type": "message", "message_type": "group",
        "message_id": message_id, "group_id": int(group_id),
        "user_id": int(user_id), "sender": sender,
        "message": message, "raw_message": message if isinstance(message, str) else "",
    }


def install_message_handler(adapter, monkeypatch) -> list:
    """把 adapter.handle_message 换成记录器，返回 events 列表。"""
    events: list = []

    async def _record(event):
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", _record)
    return events


def make_adapter(**extra):
    """同步构造 OnebotAdapter（门控/工具等单元测试用，不起 server）。"""
    from onebot_plugin.adapter import OnebotAdapter
    extra.setdefault("token", TEST_TOKEN)
    return OnebotAdapter(PlatformConfig(enabled=True, extra=extra))


def fake_live_adapter(*, connected: bool = True, admins=("10001",),
                      session_user: str = "", registry=None, responses=None,
                      recent_media=None):
    """tools.py 单元测试用的假 adapter（duck-typed，不依赖真实 WS）。

    ``recent_media``：[(message_id, registry), ...]——模拟 adapter 的注册表环，
    提供时走 resolve_media_ref 新链路；未提供时仅有 current_registry（旧链路）。
    """
    responses = responses if responses is not None else {}
    calls: list[tuple] = []

    async def call_action(action, params=None, timeout=30.0):
        calls.append((action, params, timeout))
        if isinstance(responses.get(action), Exception):
            raise responses[action]
        return {"status": "ok", "retcode": 0,
                "data": responses.get(action) or {}, "echo": "9:1"}

    async def resolve_media_ref(media_id):
        for _mid, reg in reversed(list(recent_media or [])):
            ref = reg.get(media_id)
            if ref is not None:
                return ref
        return None

    ns_kwargs = dict(
        is_connected=connected, call_action=call_action, calls=calls,
        admin_users=tuple(admins), last_session_user=session_user,
        last_session_chat_id=None,
        current_registry=(lambda: registry) if registry is not None else None,
    )
    if recent_media is not None:
        ns_kwargs["resolve_media_ref"] = resolve_media_ref
    return SimpleNamespace(**ns_kwargs)


# importlib 导入模式下 conftest 以 "tests.conftest" 名字加载；给测试文件里
# 的 ``from conftest import ...`` 提供稳定的顶层别名。
sys.modules.setdefault("conftest", sys.modules[__name__])
