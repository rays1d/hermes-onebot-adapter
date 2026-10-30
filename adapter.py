"""OnebotAdapter：反向 WebSocket 适配器（OneBot 11 / NapCat 拨入，Universal 角色）。

继承 :class:`gateway.platforms.base.BasePlatformAdapter`，承担：

- **连接生命周期**：aiohttp WS server（默认 ``0.0.0.0:8643``，路径 ``/onebot/ws``），
  NapCat 作为 client 拨入；token 鉴权（``Authorization: Bearer`` 或 ``?access_token=``）；
  同一端点的新连接替换旧连接（一个 QQ 账号对应一个网关端点）。
- **API 通道**：``_call_api`` 走 Universal 单连接——``{action, params, echo}`` 出、
  按 echo 匹配回包，echo 前缀带连接代号（generation），跨连接回包丢弃并告警。
- **入站**：meta（lifecycle/heartbeat）维护状态；message 事件经去重 → self 过滤 →
  段解析（segments.py）→ 门控（分面策略）→ 群触发判断（@bot / 回复 bot）→
  引用取原文（get_msg）→ ``build_source`` → ``handle_message`` 交给网关。
- **出站**：markdown 剥离 → 句边界分段（formatting.py）→ ``send_msg``；
  媒体经 ``upload_file_stream``（NapCat v4.8.115+）流式上传，探测失败永久回退
  base64；文件走 ``upload_private_file``/``upload_group_file``。

多路复用铁律：所有 env/凭据读取一律走
``gateway.platforms._shared.get_scoped_secret`` / ``extra_or_secret``，
**禁止** ``os.getenv`` 读取、**禁止**写 ``os.environ``。

架构参考：``others/onebot/hermes-napcat-plugin/plugin/adapter.py``（最干净样板）。
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

try:
    from aiohttp import WSCloseCode, WSMsgType, web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - 防御性导入守卫
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]
    WSCloseCode = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import (
    extra_or_secret as _extra_or_secret,
    get_scoped_secret as _get_scoped_secret,
)
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator

from .formatting import QQ_TEXT_LIMIT, split_for_qq, strip_markdown
from .segments import MediaRegistry, at_targets, extract_reply_id, parse_segments, render_text

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8643
DEFAULT_PATH = "/onebot/ws"
DEFAULT_MAX_MSG_CHARS = QQ_TEXT_LIMIT
DEFAULT_API_TIMEOUT = 30.0        # 单个 action 的默认等待秒数（可 per-call 覆盖）
REPLY_FETCH_TIMEOUT = 5.0         # 取引用原文的等待秒数（快速降级，不拖慢主链路）
SEND_CHUNK_INTERVAL_S = 0.3       # 分段间隔：缓解个人号高频发送风控
WS_MAX_MSG_SIZE = 4 * 1024 * 1024  # 入站帧上限——绝不用 0（无限制）
STREAM_UPLOAD_CHUNK_SIZE = 64 * 1024        # upload_file_stream 分块大小
STREAM_UPLOAD_RETENTION_MS = 30 * 1000      # NapCat 侧流式文件保留时长

MAX_MESSAGE_LENGTH = QQ_TEXT_LIMIT * 4  # QQ 无硬限制；给网关一个宽松值

_TRUTHY = {"1", "true", "yes", "on"}
_POLICIES = {"open", "allowlist", "disabled"}


def check_onebot_requirements() -> bool:
    """被动探测：aiohttp 当前是否可导入。绝不安装。"""
    return AIOHTTP_AVAILABLE


class NotConnectedError(Exception):
    """无可用 OneBot 连接（NapCat 尚未拨入或已断开）。发送侧视为可重试。"""

    def __init__(self, message: str = "OneBot 连接未就绪") -> None:
        super().__init__(message)
        self.retryable = True


class NetworkError(Exception):
    """动作超时或链路级失败（帧可能已写出，发送侧不得自动重试）。"""


class OneBotApiError(Exception):
    """OneBot action 执行失败（status=failed 或 retcode≠0）。"""

    def __init__(self, retcode: Any, wording: str, action: str, payload: Any = None) -> None:
        self.retcode = retcode
        self.wording = wording
        self.action = action
        self.payload = payload
        super().__init__(f"OneBot 动作 {action!r} 失败 (retcode={retcode}): {wording}")


def _truthy(value: Any) -> bool:
    """配置布尔值 → bool（``""``/``"0"``/``"false"`` 均为 False）。"""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in _TRUTHY


def _csv_list(raw: Any) -> list[str]:
    """逗号分隔字符串 / 列表 → 去空白去空的字符串列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return [str(item).strip() for item in raw if str(item).strip()]


def _coerce_int(value: Any) -> Any:
    """尽力转 int；转不动原样返回（OneBot 端自行解释）。"""
    if isinstance(value, bool):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


def _echo_generation(echo: Any) -> Optional[int]:
    """从 ``"{generation}:{seq}"`` 形态的 echo 里解析连接代号；解析不出返回 None。"""
    try:
        return int(str(echo).split(":", 1)[0])
    except (ValueError, IndexError, AttributeError):
        return None


def _local_path(path_or_url: str) -> Optional[Path]:
    """把入参解释为适配器侧本地文件路径；不是本地文件返回 None。

    ``http(s)://``/``base64://``/``data:`` 天然跨机安全，直接放行；
    ``file://`` 只在指向 NapCat 本机文件时安全——若指向 Hermes 侧真实存在的
    文件，则按本地路径处理（走流式上传/base64），否则原样交给实现端解释。
    """
    text = str(path_or_url or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith(("http://", "https://", "base64://", "data:")):
        return None
    candidate = text
    if lowered.startswith("file://"):
        parsed = urlparse(text)
        candidate = unquote(parsed.path or "")
        if os.name == "nt" and len(candidate) > 2 and candidate[0] == "/" and candidate[2] == ":":
            candidate = candidate[1:]  # file:///C:/... → C:/...
    try:
        path = Path(os.path.abspath(os.path.expanduser(candidate)))
        if path.is_file():
            return path
    except OSError:
        return None
    return None


def _file_uri(path: Path) -> str:
    """本地路径 → OneBot ``file://`` URI（NapCat 同机部署时的兜底形态）。"""
    normalized = str(path).replace("\\", "/")
    if not normalized.startswith("/"):
        normalized = "/" + normalized  # Windows 盘符路径 → file:///C:/...
    return f"file://{normalized}"


class OnebotAdapter(BasePlatformAdapter):
    """OneBot 11 反向 WebSocket 适配器（NapCat 等实现端作为 client 拨入）。

    config.yaml ``platforms.onebot.extra`` 键（括号内为对应 env，env 优先）：

        token (ONEBOT_TOKEN)                  共享鉴权 token——必填
        host (ONEBOT_HOST)                    WS 绑定地址，默认 0.0.0.0
        port (ONEBOT_PORT)                    WS 监听端口，默认 8643
        path (ONEBOT_PATH)                    WS 路径，默认 /onebot/ws
        http_url (ONEBOT_HTTP_URL)            实现端 HTTP API（cron 投递用）
        dm_policy (ONEBOT_DM_POLICY)          私聊策略 open/allowlist/disabled
        group_policy (ONEBOT_GROUP_POLICY)    群聊策略 open/allowlist/disabled
        allowed_users (ONEBOT_ALLOWED_USERS)  私聊白名单
        group_allowed_users (ONEBOT_GROUP_ALLOWED_USERS)   群内成员白名单
        allowed_groups (ONEBOT_ALLOWED_GROUPS) 群白名单（"*" = 任意群）
        admin_users (ONEBOT_ADMIN_USERS)      管理员档（危险动作放行）
        allow_all (ONEBOT_ALLOW_ALL_USERS)    一键放开兜底
        max_msg_chars (ONEBOT_MAX_MSG_CHARS)  出站分段阈值，默认 2000
    """

    SUPPORTS_MESSAGE_EDITING = False  # OneBot 11 无编辑消息动作
    splits_long_messages = True       # 出站长文由本适配器自行分段
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("onebot"))

        extra = getattr(config, "extra", None) or {}

        # ── 凭据（token：YAML extra → typed key → scoped env，参照样板写法）──
        self._token: str = str(
            extra.get("token")
            or getattr(config, "token", None)
            or _get_scoped_secret("ONEBOT_TOKEN")
            or ""
        ).strip()

        # ── 监听配置 ──
        self._host: str = str(_extra_or_secret(extra, "host", "ONEBOT_HOST", DEFAULT_HOST))
        try:
            self._port: int = int(_extra_or_secret(extra, "port", "ONEBOT_PORT", DEFAULT_PORT))
        except (TypeError, ValueError):
            self._port = DEFAULT_PORT
        self._path: str = str(_extra_or_secret(extra, "path", "ONEBOT_PATH", DEFAULT_PATH))
        if not self._path.startswith("/"):
            self._path = "/" + self._path
        self._http_url: str = str(
            _extra_or_secret(extra, "http_url", "ONEBOT_HTTP_URL", "") or ""
        ).strip()

        # ── 门控（分面策略，默认 allowlist，fail-closed）──
        self._dm_policy: str = self._normalize_policy(
            _extra_or_secret(extra, "dm_policy", "ONEBOT_DM_POLICY", "allowlist"), "ONEBOT_DM_POLICY")
        self._group_policy: str = self._normalize_policy(
            _extra_or_secret(extra, "group_policy", "ONEBOT_GROUP_POLICY", "allowlist"),
            "ONEBOT_GROUP_POLICY")
        self._allow_all: bool = _truthy(
            _extra_or_secret(extra, "allow_all", "ONEBOT_ALLOW_ALL_USERS", ""))
        self._allowed_users: list[str] = _csv_list(
            _extra_or_secret(extra, "allowed_users", "ONEBOT_ALLOWED_USERS", ""))
        self._group_allowed_users: list[str] = _csv_list(
            _extra_or_secret(extra, "group_allowed_users", "ONEBOT_GROUP_ALLOWED_USERS", ""))
        self._allowed_groups: list[str] = _csv_list(
            _extra_or_secret(extra, "allowed_groups", "ONEBOT_ALLOWED_GROUPS", ""))
        self._admin_users: list[str] = _csv_list(
            _extra_or_secret(extra, "admin_users", "ONEBOT_ADMIN_USERS", ""))

        # ── 出站分段 ──
        try:
            self._max_msg_chars: int = int(
                _extra_or_secret(extra, "max_msg_chars", "ONEBOT_MAX_MSG_CHARS",
                                 DEFAULT_MAX_MSG_CHARS))
        except (TypeError, ValueError):
            self._max_msg_chars = DEFAULT_MAX_MSG_CHARS
        if self._max_msg_chars <= 0:
            self._max_msg_chars = DEFAULT_MAX_MSG_CHARS

        # ── 运行时状态 ──
        self._runner: Optional["web.AppRunner"] = None  # type: ignore[name-defined]
        self._site: Optional["web.TCPSite"] = None  # type: ignore[name-defined]
        self._ws: Optional[Any] = None                    # 当前连接（最新者）
        self._ws_lock = asyncio.Lock()                    # send_json 串行化
        self._generation: int = 0                         # 连接代号：echo 前缀，防回包串台
        self._seq: int = 0                                # echo 序号
        # echo → (发起连接, Future)
        self._pending: dict[str, tuple[Any, asyncio.Future]] = {}
        self._self_id: Optional[str] = None
        self._last_heartbeat: float = 0.0
        self._registry: Optional[MediaRegistry] = None    # 最近一条消息的媒体注册表
        self._upload_unsupported: bool = False            # upload_file_stream 探测失败后永久回退
        # 最近一条通过门控的入站消息归属（tools.py 管理员档判定用）
        self._last_session_user: Optional[str] = None
        self._last_session_chat_id: Optional[str] = None

        self._dedup = MessageDeduplicator()

    # ------------------------------------------------------------------
    # 配置辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_policy(raw: Any, env_name: str) -> str:
        """策略值归一化：open / allowlist / disabled；非法值回退 allowlist。"""
        value = str(raw or "").strip().lower()
        if value in _POLICIES:
            return value
        if value:
            logger.warning("[onebot] %s=%r 不是合法策略（open/allowlist/disabled），按 allowlist 处理",
                           env_name, raw)
        return "allowlist"

    @property
    def name(self) -> str:
        return "OneBot"

    @property
    def self_id(self) -> Optional[str]:
        """当前连接的机器人 QQ 号（实现端上报）。"""
        return self._self_id

    def current_registry(self) -> Optional[MediaRegistry]:
        """最近一条入站消息的媒体注册表（``onebot_call`` 工具按 media_id 查引用用）。"""
        return self._registry

    @property
    def last_session_user(self) -> Optional[str]:
        """最近一条通过门控的入站消息发送者 QQ 号。

        ``onebot_call`` 的管理员档判定用：工具执行期间归属当前回合的调用
        者。并发多会话下以最后过门控者为准（单用户部署无歧义）。
        """
        return self._last_session_user

    @property
    def last_session_chat_id(self) -> Optional[str]:
        """最近一条通过门控的入站消息所属会话（``group:<gid>`` 或私聊 QQ 号）。"""
        return self._last_session_chat_id

    @property
    def admin_users(self) -> tuple[str, ...]:
        """管理员 QQ 号集合（ONEBOT_ADMIN_USERS）。"""
        return tuple(self._admin_users)

    # ------------------------------------------------------------------
    # 门控（分面策略，fail-closed）
    # ------------------------------------------------------------------

    def _authorize(self, chat_type: str, user_id: str, group_id: str = "") -> bool:
        """入站消息放行判断。拒绝时 INFO 级记录完整 id（方便复制进白名单）。"""
        if self._allow_all:
            return True

        if chat_type == "dm":
            if self._dm_policy == "disabled":
                return False
            if self._dm_policy == "open":
                return True
            # allowlist：空名单 = 全拒（fail-closed），日志给配置提示
            if user_id in self._allowed_users:
                return True
            logger.info(
                "[onebot] 私聊被拒 user_id=%s（dm_policy=allowlist 且未在 ONEBOT_ALLOWED_USERS；"
                "将其加入白名单或设 ONEBOT_DM_POLICY=open 放行）", user_id)
            return False

        # 群聊
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist":
            # 空名单 = 全拒；"*" = 放行任意群
            if "*" not in self._allowed_groups and group_id not in self._allowed_groups:
                logger.info(
                    "[onebot] 群聊被拒 group_id=%s user_id=%s（group_policy=allowlist 且未在 "
                    "ONEBOT_ALLOWED_GROUPS；将其加入白名单或设 ONEBOT_GROUP_POLICY=open）",
                    group_id, user_id)
                return False
        # 群内成员收紧：空 = 群内不限
        if self._group_allowed_users and user_id not in self._group_allowed_users:
            logger.info(
                "[onebot] 群成员被拒 group_id=%s user_id=%s（未在 ONEBOT_GROUP_ALLOWED_USERS）",
                group_id, user_id)
            return False
        return True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """启动反向 WS server，等待 OneBot 实现端拨入。"""
        del is_reconnect  # 被动 server：断线后由实现端重拨，无需区分
        if not AIOHTTP_AVAILABLE:
            self._set_fatal_error(
                "onebot_missing_dependency", "aiohttp is required for the OneBot adapter",
                retryable=True)
            return False
        if not self._token:
            self._set_fatal_error(
                "onebot_missing_token",
                "ONEBOT_TOKEN is required — set a shared secret to authenticate the dial-in",
                retryable=True)
            return False
        if self._site is not None:
            logger.info("[%s] WS server 已在监听，无需重复 connect", self.name)
            return True

        if not self._acquire_platform_lock(
            "onebot-bind", f"{self._host}:{self._port}{self._path}", "OneBot bind address"
        ):
            return False

        try:
            app = web.Application()
            app.router.add_get("/health", self._handle_health)
            app.router.add_get(self._path, self._handle_ws)
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
        except Exception as exc:
            self._set_fatal_error(
                "onebot_bind_error", f"OneBot startup failed: {exc}", retryable=True)
            logger.error("[%s] 启动失败: %s", self.name, exc, exc_info=True)
            await self._teardown_server()
            self._release_platform_lock()
            return False

        try:
            from gateway.platforms.shared_ingress import listener_base_url
            listener_base = listener_base_url(self._host, self._port)
        except Exception:
            listener_base = f"http://{self._host}:{self._port}"
        self._mark_connected(listener_base=listener_base)
        self._wire_plugin_handlers(None)
        logger.info("[%s] 反向 WebSocket 监听 ws://%s:%d%s（等待 OneBot 实现端拨入）",
                    self.name, self._host, self._port, self._path)
        return True

    async def disconnect(self) -> None:
        """优雅关停：断开连接、关 server、清 pending、释放锁。"""
        self._mark_disconnected()
        if self._ws is not None:
            try:
                await self._ws.close(code=WSCloseCode.GOING_AWAY, message=b"gateway shutdown")
            except Exception:
                pass
            self._ws = None
        await self._teardown_server()
        self._fail_all_pending("连接已关闭")
        self._release_platform_lock()
        logger.info("[%s] 已断开", self.name)

    async def _teardown_server(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None

    def _fail_pending_for(self, ws: Any, reason: str) -> None:
        """只清掉「写在给定连接上」的 pending——新连接的请求不受旧连接 teardown 影响。"""
        for echo, (owner, fut) in list(self._pending.items()):
            if owner is ws:
                self._pending.pop(echo, None)
                if not fut.done():
                    fut.set_exception(NotConnectedError(f"OneBot {reason}"))

    def _fail_all_pending(self, reason: str) -> None:
        for _, fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(NotConnectedError(f"OneBot {reason}"))
        self._pending.clear()

    # ------------------------------------------------------------------
    # 传输层：WS server 与鉴权
    # ------------------------------------------------------------------

    def _live_ws(self) -> Optional[Any]:
        ws = self._ws
        if ws is None or getattr(ws, "closed", True):
            return None
        return ws

    async def _handle_health(self, request):  # pragma: no cover - trivial
        del request
        return web.json_response({"status": "ok", "platform": self.platform.value})

    def _is_authorized_request(self, request) -> bool:
        """握手鉴权：``Authorization: Bearer <token>`` 或 ``?access_token=``。"""
        if not self._token:
            return False
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            if header[7:].strip() == self._token:
                return True
        elif header.strip() == self._token:
            return True
        query_token = request.rel_url.query.get("access_token") or request.query.get("access_token")
        if query_token and str(query_token).strip() == self._token:
            return True
        return False

    async def _handle_ws(self, request):
        """``/{path}`` 端点：鉴权 → 建连（挤掉旧连接）→ 逐帧分派。"""
        if not self._is_authorized_request(request):
            logger.warning("[%s] 拒绝未鉴权的 WS 拨入 remote=%s", self.name, request.remote)
            return web.Response(status=401, text="unauthorized")

        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=WS_MAX_MSG_SIZE)
        await ws.prepare(request)

        # NapCat 在升级请求里带 X-Self-ID；lifecycle.connect 帧会再补一次
        header_self_id = request.headers.get("X-Self-ID")
        if header_self_id and str(header_self_id).strip():
            self._self_id = str(header_self_id).strip()

        previous = self._ws
        self._generation += 1  # 新连接新代号：旧连接的回包必然对不上，串台即被丢弃
        self._ws = ws
        if previous is not None and not previous.closed:
            logger.info("[%s] 新连接挤掉旧连接（remote=%s）", self.name, request.remote)
            try:
                await previous.close(code=WSCloseCode.GOING_AWAY, message=b"superseded")
            except Exception:
                pass
        self._fail_pending_for(previous, "连接已被新连接替换")

        logger.info("[%s] OneBot 实现已连接 remote=%s self_id=%s generation=%d",
                    self.name, request.remote, self._self_id, self._generation)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    payload = self._safe_parse_json(msg.data)
                    if payload is not None:
                        self._dispatch(payload, ws)
                    else:
                        logger.debug("[%s] 非 JSON 帧已丢弃", self.name)
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("[%s] WebSocket 错误: %s", self.name, ws.exception())
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] WebSocket 读取循环异常", self.name)
        finally:
            if self._ws is ws:
                self._ws = None
            self._fail_pending_for(ws, "连接已关闭")
            logger.info("[%s] OneBot 实现已断开", self.name)
        return ws

    @staticmethod
    def _safe_parse_json(raw: Any) -> Optional[dict]:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # 入站分派
    # ------------------------------------------------------------------

    def _dispatch(self, payload: dict, ws: Any) -> None:
        """按帧内容分派：API 回包 / meta / message；notice & request 仅记日志。"""
        echo = payload.get("echo")
        if echo is not None and "post_type" not in payload:
            self._resolve_response(str(echo), ws, payload)
            return

        post_type = payload.get("post_type")
        if post_type in ("meta_event", "meta"):  # v11 叫 meta_event，v12 叫 meta
            self._handle_meta(payload)
            return
        if post_type == "message":
            self._spawn(self._handle_message_event(payload))
            return
        if post_type == "message_sent":  # go-cqhttp 系回显，等同 self 消息
            logger.debug("[%s] 忽略 message_sent 回显", self.name)
            return
        logger.debug("[%s] 忽略 post_type=%r", self.name, post_type)

    def _spawn(self, coro) -> None:
        """后台任务执行（不阻塞 WS 读循环）：get_msg 回包与请求同连接，内联等待会死锁。"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _resolve_response(self, echo: str, ws: Any, payload: dict) -> None:
        """API 回包按 echo 匹配 pending；连接代号不符的跨连接回包丢弃并 WARN。"""
        entry = self._pending.get(echo)
        if entry is None:
            gen = _echo_generation(echo)
            if gen is not None and gen != self._generation:
                logger.warning("[%s] 丢弃跨连接回包 echo=%s（当前连接代号 %d）",
                               self.name, echo, self._generation)
            else:
                logger.debug("[%s] 回包 echo=%s 无匹配 pending（已超时或被清理）", self.name, echo)
            return
        owner, fut = entry
        if owner is not ws:
            # 理论上单连接承载请求与回包；出现即串台，丢弃留 pending 走超时路径
            logger.warning("[%s] 回包 echo=%s 来自非发起连接，丢弃", self.name, echo)
            return
        self._pending.pop(echo, None)
        if not fut.done():
            fut.set_result(payload)

    def _handle_meta(self, payload: dict) -> None:
        """meta 事件：lifecycle 记 self_id，heartbeat 刷新心跳时间。"""
        self_id = payload.get("self_id")
        if self_id is not None and str(self_id).strip():
            new_id = str(self_id).strip()
            if new_id != self._self_id:
                logger.info("[%s] self_id=%s", self.name, new_id)
            self._self_id = new_id
        meta_type = payload.get("meta_event_type") or payload.get("sub_type")
        if meta_type == "heartbeat":
            self._last_heartbeat = time.time()
        elif meta_type == "lifecycle":
            logger.info("[%s] lifecycle sub_type=%s self_id=%s",
                        self.name, payload.get("sub_type"), self._self_id)

    # ------------------------------------------------------------------
    # 入站消息处理（后台任务）
    # ------------------------------------------------------------------

    async def _handle_message_event(self, payload: dict) -> None:
        """单条消息事件的完整处理（异常只记日志，不杀后台任务）。"""
        try:
            await self._process_message_event(payload)
        except Exception:
            logger.exception("[%s] 处理消息事件失败", self.name)

    async def _process_message_event(self, payload: dict) -> None:
        message_id = payload.get("message_id")
        if message_id is None:
            logger.debug("[%s] 消息缺 message_id，忽略", self.name)
            return
        msg_key = str(message_id)
        if self._dedup.is_duplicate(msg_key):
            logger.debug("[%s] 重复消息 message_id=%s 丢弃", self.name, msg_key)
            return

        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        user_id = str(sender.get("user_id") or payload.get("user_id") or "").strip()
        if not user_id:
            logger.debug("[%s] 消息缺发送者 user_id，忽略", self.name)
            return
        if self._self_id and user_id == self._self_id:
            return  # 自己的消息回显，防循环

        message_type = str(payload.get("message_type") or "")
        if message_type == "private":
            chat_type = "dm"
        elif message_type == "group":
            chat_type = "group"
        else:
            logger.debug("[%s] 忽略 message_type=%r", self.name, message_type)
            return

        group_id = str(payload.get("group_id") or "").strip()
        if chat_type == "group" and not group_id:
            logger.debug("[%s] 群消息缺 group_id，忽略", self.name)
            return

        # 门控先行：未授权者不发任何 API 请求（连 get_msg 都不碰）
        if not self._authorize(chat_type, user_id, group_id):
            return

        # 每条消息一个新 MediaRegistry：media_id 计数重置（m1、m2、…）
        registry = MediaRegistry()
        self._registry = registry

        raw = payload.get("message")
        if raw is None or raw == "":
            raw = payload.get("raw_message")
        segments = parse_segments(raw)

        mentioned = bool(self._self_id) and self._self_id in at_targets(segments)
        text_parts: list[str] = []
        for seg in segments:
            # @自己的段剥离（群聊触发用的 at 不进 agent 文本）
            if (seg.type == "at" and self._self_id
                    and str(seg.data.get("qq") or "").strip() == self._self_id):
                continue
            text_parts.append(render_text(seg, registry))
        text = "".join(text_parts).strip()

        # 引用消息：一次 get_msg 同时服务「触发判断」与「原文入上下文」
        reply_id = extract_reply_id(segments)
        quoted = await self._fetch_quoted_message(reply_id, registry) if reply_id else None

        if chat_type == "group":
            triggered = mentioned
            if not triggered and quoted is not None and self._self_id:
                triggered = quoted.get("sender_id") == self._self_id
            if not triggered:
                logger.debug("[%s] 群消息未触发（未 @ 机器人且非回复机器人）group_id=%s user_id=%s",
                             self.name, group_id, user_id)
                return
            if not text:
                logger.debug("[%s] 群消息已触发但无文本内容，忽略", self.name)
                return
            sender_name = (str(sender.get("card") or "").strip()
                           or str(sender.get("nickname") or "").strip())
            if sender_name:
                text = f"{sender_name}：\n{text}"  # 群共享会话：带发送者前缀便于区分
        else:
            if not text:
                logger.debug("[%s] 私聊消息无文本内容，忽略", self.name)
                return

        user_name = (str(sender.get("card") or "").strip()
                     or str(sender.get("nickname") or "").strip() or None)
        chat_id = f"group:{group_id}" if chat_type == "group" else user_id
        # 工具层管理员归属：记录最近一次通过门控的调用者与所属会话
        self._last_session_user = user_id
        self._last_session_chat_id = chat_id
        source = self.build_source(
            chat_id=chat_id, chat_type=chat_type, user_id=user_id,
            user_name=user_name, message_id=msg_key,
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            user_id=user_id,
            user_name=user_name,
            source=source,
            raw_message=payload,
            message_id=msg_key,
            media_urls=[],   # 按需拉取模式：媒体不下载，agent 用工具经 media_id 取
            media_types=[],
        )
        if reply_id:
            event.reply_to_message_id = reply_id
            if quoted is not None:
                event.reply_to_text = quoted["text"]
                event.reply_to_author_id = quoted["sender_id"] or None
                event.reply_to_author_name = quoted["sender_name"]
                event.reply_to_is_own_message = (
                    bool(self._self_id) and quoted["sender_id"] == self._self_id)
            else:
                event.reply_to_text = "[引用消息]"

        await self.handle_message(event)

    async def _fetch_quoted_message(
        self, reply_id: str, registry: MediaRegistry,
    ) -> Optional[dict]:
        """get_msg 取被引用消息原文；任何失败返回 None（调用方降级 ``[引用消息]``）。

        被引用消息里的媒体也注册进当前 registry——agent 需要时可按 media_id 取。
        """
        try:
            payload = await self._call_api(
                "get_msg", {"message_id": _coerce_int(reply_id)},
                timeout=REPLY_FETCH_TIMEOUT)
        except Exception as exc:
            logger.info("[%s] 获取引用消息原文失败 id=%s: %s", self.name, reply_id, exc)
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not data:
            return None
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        sender_id = str(sender.get("user_id") or data.get("user_id") or "").strip()
        sender_name = (str(sender.get("card") or "").strip()
                       or str(sender.get("nickname") or "").strip()
                       or (sender_id or ""))
        raw = data.get("message")
        if raw is None or raw == "":
            raw = data.get("raw_message")
        quoted_text = "".join(render_text(seg, registry) for seg in parse_segments(raw)).strip()
        text = f"[{sender_name}] {quoted_text}" if sender_name else quoted_text
        return {"sender_id": sender_id, "sender_name": sender_name or None, "text": text}

    # ------------------------------------------------------------------
    # OneBot action 通道（echo / future）
    # ------------------------------------------------------------------

    async def _call_api(
        self, action: str, params: Optional[dict], timeout: float = DEFAULT_API_TIMEOUT,
    ) -> dict:
        """向实现端发起 action 并等待同 echo 回包，返回完整响应 dict。

        - 无连接 → :class:`NotConnectedError`（可重试）
        - 超时 / 发送失败 → :class:`NetworkError`（发送侧不得自动重试）
        - status=failed 或 retcode≠0 → :class:`OneBotApiError`
        """
        ws = self._live_ws()
        if ws is None:
            raise NotConnectedError("OneBot 连接未就绪（实现端尚未拨入）")
        self._seq += 1
        echo = f"{self._generation}:{self._seq}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[echo] = (ws, fut)
        try:
            async with self._ws_lock:
                await ws.send_json({"action": action, "params": params or {}, "echo": echo})
        except Exception as exc:
            self._pending.pop(echo, None)
            raise NetworkError(f"发送 {action} 请求失败: {exc}") from exc
        try:
            payload = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            raise NetworkError(f"{action} 超时（等待 {timeout}s）") from None

        status = str(payload.get("status") or "")
        retcode = payload.get("retcode")
        int_retcode = (
            retcode if isinstance(retcode, int) and not isinstance(retcode, bool) else None)
        # retcode=1 / status=async：请求已提交异步处理（限速或 *_async 调用），
        # 结果未知但**不算失败**——按成功返回（data/message_id 为空，OneBot 11
        # communication/http.md 的响应语义）。谎报失败会让发送侧重发、造成重复消息。
        if status == "async" or int_retcode == 1:
            return payload
        failed = status == "failed" or (int_retcode is not None and int_retcode != 0)
        if failed:
            wording = str(payload.get("wording") or payload.get("message") or "未知错误")
            raise OneBotApiError(retcode=retcode, wording=wording, action=action, payload=payload)
        return payload

    # tools.py（步骤 7）复用 live 连接时的公开别名
    call_action = _call_api

    async def _call_api_data(
        self, action: str, params: Optional[dict], timeout: float = DEFAULT_API_TIMEOUT,
    ) -> dict:
        """``_call_api`` 的便捷封装：直接返回 ``data`` dict（非 dict 时为空）。"""
        payload = await self._call_api(action, params, timeout)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # 出站：文本
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_chat_target(chat_id: str) -> tuple[str, str]:
        """Hermes ``chat_id`` → ``(chat_type, 裸 id)``。

        ``group:<gid>`` → 群；``private:<uid>`` → 私聊；纯数字默认私聊
        （QQ 号即私聊 id，群 id 入站时统一记成 ``group:`` 前缀形态）。
        """
        text = str(chat_id or "").strip()
        lowered = text.lower()
        if lowered.startswith("group:"):
            return "group", text.split(":", 1)[1].strip()
        if lowered.startswith("private:"):
            return "private", text.split(":", 1)[1].strip()
        return "private", text

    def _error_result(self, exc: Exception) -> SendResult:
        """异常 → SendResult：连接缺失可重试，其余（超时/API 失败）不重试。"""
        if isinstance(exc, NotConnectedError):
            return SendResult(success=False, error=str(exc), retryable=True)
        return SendResult(success=False, error=str(exc))

    async def _send_message_segments(
        self, chat_type: str, target: str, segments: list[dict],
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """用 ``send_msg`` 发送预构造段数组（reply 段只挂在首条上）。"""
        if reply_to:
            segments = [{"type": "reply", "data": {"id": str(reply_to)}}, *segments]
        params: dict[str, Any] = {"message": segments}
        if chat_type == "group":
            params["group_id"] = _coerce_int(target)
            params["message_type"] = "group"
        else:
            params["user_id"] = _coerce_int(target)
            params["message_type"] = "private"
        try:
            payload = await self._call_api("send_msg", params)
        except Exception as exc:  # NotConnected / Network / ApiError 统一转结果
            return self._error_result(exc)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        message_id = data.get("message_id")
        return SendResult(
            success=True,
            message_id=str(message_id) if message_id is not None else None,
            raw_response=payload,
        )

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """发文本：markdown 剥离 → 句边界分段 → 逐段发送（段间 0.3s 防风控）。"""
        del metadata
        chat_type, target = self._resolve_chat_target(chat_id)
        text = strip_markdown(content or "")
        if not text.strip():
            return SendResult(success=True)

        chunks = split_for_qq(text, self._max_msg_chars)
        if not chunks:
            return SendResult(success=True)

        message_ids: list[str] = []
        last: Optional[SendResult] = None
        for idx, chunk in enumerate(chunks):
            last = await self._send_message_segments(
                chat_type, target, [{"type": "text", "data": {"text": chunk}}],
                reply_to=reply_to if idx == 0 else None)
            if not last.success:
                return last
            if last.message_id:
                message_ids.append(last.message_id)
            if idx < len(chunks) - 1:
                await asyncio.sleep(SEND_CHUNK_INTERVAL_S)
        assert last is not None
        if len(message_ids) > 1:
            # 多段：message_id 取末段（后续编辑/引用对准最新一条），其余进 continuation
            return dataclasses.replace(
                last, continuation_message_ids=tuple(message_ids[:-1]))
        return last

    # ------------------------------------------------------------------
    # 出站：媒体
    # ------------------------------------------------------------------

    def _mark_upload_unsupported(self, reason: str) -> None:
        """探测失败 → 本次进程内永久回退 base64，不再反复尝试。"""
        if not self._upload_unsupported:
            self._upload_unsupported = True
            logger.warning("[%s] upload_file_stream 不可用（%s），后续媒体改用 base64 直传",
                           self.name, reason)

    async def _upload_file_stream(self, path: Path) -> tuple[Optional[str], Optional[str]]:
        """NapCat 扩展 ``upload_file_stream``：把本地文件跨机流式推到 NapCat 侧。

        成功返回 ``(NapCat 本地文件路径, None)``；失败 ``(None, 原因)``。
        需 NapCat v4.8.115+；失败由调用方决定回退策略。
        """
        try:
            file_size = path.stat().st_size
            chunk_size = STREAM_UPLOAD_CHUNK_SIZE
            total_chunks = max(1, math.ceil(file_size / chunk_size)) if file_size else 1
            stream_id = uuid.uuid4().hex
            sha256 = hashlib.sha256()
            base_params: dict[str, Any] = {
                "stream_id": stream_id,
                "filename": path.name,
                "file_size": file_size,
                "total_chunks": total_chunks,
                "file_retention": STREAM_UPLOAD_RETENTION_MS,
            }
            with path.open("rb") as fh:
                for chunk_index in range(total_chunks):
                    chunk = fh.read(chunk_size)
                    sha256.update(chunk)
                    params = dict(base_params)
                    params["chunk_index"] = chunk_index
                    params["chunk_data"] = base64.b64encode(chunk).decode("ascii")
                    if chunk_index == total_chunks - 1:
                        params["expected_sha256"] = sha256.hexdigest()
                    await self._call_api("upload_file_stream", params)
            final = await self._call_api(
                "upload_file_stream", {"stream_id": stream_id, "is_complete": True})
        except (NotConnectedError, NetworkError, OneBotApiError) as exc:
            return None, str(exc)
        except OSError as exc:
            return None, f"读取本地文件失败: {exc}"

        data = final.get("data") if isinstance(final.get("data"), dict) else {}
        candidates = [data]
        file_info = data.get("file_info")
        if isinstance(file_info, dict):
            candidates.append(file_info)
        for source_dict in candidates:
            for key in ("file_path", "path", "file", "url"):
                value = source_dict.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip(), None
        return None, "upload_file_stream 响应缺少 file_path"

    async def _media_segment_ref(self, path_or_url: str) -> str:
        """image/record/video 段的 ``file`` 值：URL 透传 → 流式上传 → base64 兜底。"""
        text = str(path_or_url or "").strip()
        lowered = text.lower()
        if lowered.startswith(("http://", "https://", "base64://", "data:")):
            return text
        local = _local_path(text)
        if local is None:
            return text  # 可能是实现端本地路径，交给实现端解释
        if not self._upload_unsupported:
            uploaded, error = await self._upload_file_stream(local)
            if uploaded:
                return uploaded
            self._mark_upload_unsupported(error or "未知原因")
        try:
            data = local.read_bytes()
        except OSError as exc:
            logger.warning("[%s] 读取本地文件失败 %s: %s（原样透传）", self.name, local, exc)
            return text
        return "base64://" + base64.b64encode(data).decode("ascii")

    async def _document_ref(self, path_or_url: str) -> str:
        """upload_*_file 的 ``file`` 值：URL 透传 → 流式上传 → file:// 兜底。

        与媒体段不同：upload_private_file/upload_group_file 对 ``base64://``
        支持不可靠，故兜底用原路径（仅当 NapCat 与文件同机时可见）。
        """
        text = str(path_or_url or "").strip()
        lowered = text.lower()
        if lowered.startswith(("http://", "https://", "base64://", "data:")):
            return text
        local = _local_path(text)
        if local is None:
            return text
        if not self._upload_unsupported:
            uploaded, error = await self._upload_file_stream(local)
            if uploaded:
                return uploaded
            self._mark_upload_unsupported(error or "未知原因")
        logger.warning(
            "[%s] upload_file_stream 不可用，文件 %s 以 file:// 直传"
            "（仅当该文件在 NapCat 侧同路径可见时才能送达）", self.name, local)
        return _file_uri(local)

    async def _send_media_segment(
        self, chat_id: str, seg_type: str, path_or_url: str,
        caption: Optional[str] = None, reply_to: Optional[str] = None,
    ) -> SendResult:
        """发单条媒体段；caption 先按文本发出（QQ 段数组无法图文同条带说明）。"""
        if caption and caption.strip():
            caption_result = await self.send(chat_id, caption, reply_to=reply_to)
            if not caption_result.success:
                return caption_result
        chat_type, target = self._resolve_chat_target(chat_id)
        try:
            media_ref = await self._media_segment_ref(path_or_url)
        except (NotConnectedError, NetworkError, OneBotApiError) as exc:
            return self._error_result(exc)
        segment = [{"type": seg_type, "data": {"file": media_ref}}]
        return await self._send_message_segments(chat_type, target, segment)

    async def send_image(
        self, chat_id: str, image_url: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """发图片：http(s) URL 直接透传，本地路径流式上传/base64。"""
        del metadata
        return await self._send_media_segment(
            chat_id, "image", image_url, caption, reply_to)

    async def send_image_file(
        self, chat_id: str, image_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """本地图片文件（或 URL）——与 :meth:`send_image` 同路径。"""
        del kwargs, metadata
        return await self._send_media_segment(
            chat_id, "image", image_path, caption, reply_to)

    async def send_voice(
        self, chat_id: str, audio_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """发语音：OneBot ``record`` 段（NapCat 接受流式路径 / base64://）。"""
        del kwargs, metadata
        return await self._send_media_segment(
            chat_id, "record", audio_path, caption, reply_to)

    async def send_video(
        self, chat_id: str, video_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """发视频：OneBot ``video`` 段。"""
        del kwargs, metadata
        return await self._send_media_segment(
            chat_id, "video", video_path, caption, reply_to)

    async def send_document(
        self, chat_id: str, file_path: str, caption: Optional[str] = None,
        file_name: Optional[str] = None, reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        """发文件：``upload_private_file`` / ``upload_group_file`` 扩展动作。"""
        del kwargs, metadata
        chat_type, target = self._resolve_chat_target(chat_id)
        try:
            file_ref = await self._document_ref(str(file_path))
        except (NotConnectedError, NetworkError, OneBotApiError) as exc:
            return self._error_result(exc)
        display_name = str(file_name or os.path.basename(str(file_path)) or "file").strip() or "file"
        if chat_type == "group":
            action, id_key = "upload_group_file", "group_id"
        else:
            action, id_key = "upload_private_file", "user_id"
        params: dict[str, Any] = {
            id_key: _coerce_int(target), "file": file_ref, "name": display_name,
        }
        try:
            payload = await self._call_api(action, params)
        except Exception as exc:
            return self._error_result(exc)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        message_id = data.get("message_id")
        result = SendResult(
            success=True,
            message_id=str(message_id) if message_id is not None else None,
            raw_response=payload,
        )
        # upload_*_file 不携带文本，caption 紧随其后补发（尽力而为）
        if caption and caption.strip():
            caption_result = await self.send(chat_id, caption, reply_to=reply_to)
            if not caption_result.success:
                result.error = result.error or caption_result.error
        return result

    # ------------------------------------------------------------------
    # 其他基类钩子
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: str, metadata: Optional[dict] = None) -> None:
        """OneBot v11 无 typing 指示，空实现（留调试日志）。"""
        del metadata
        logger.debug("[%s] send_typing no-op（chat=%s）", self.name, chat_id)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """尽力解析会话名：群走 ``get_group_info``，私聊走 ``get_stranger_info``。"""
        chat_type, target = self._resolve_chat_target(chat_id)
        try:
            if chat_type == "group":
                data = await self._call_api_data(
                    "get_group_info", {"group_id": _coerce_int(target)})
                name = str(data.get("group_name") or target)
                return {"name": name, "type": "group", "chat_id": chat_id}
            data = await self._call_api_data(
                "get_stranger_info", {"user_id": _coerce_int(target)})
            name = str(data.get("nickname") or target)
            return {"name": name, "type": "dm", "chat_id": chat_id}
        except Exception:
            # 查询失败降级：会话仍可用，只是没有友好名称
            return {"name": chat_id, "type": chat_type if chat_type in ("dm", "group") else "dm",
                    "chat_id": chat_id}

    def format_message(self, content: str) -> str:
        """QQ 不渲染 Markdown：出站统一剥成纯文本。"""
        return strip_markdown(content)
