"""tools.py — ``onebot_call`` 直通工具（action 白名单 + 管理员档）。

单个工具代理经白名单筛选的 OneBot 11 action，借道运行中的
:class:`~.adapter.OnebotAdapter` 反向 WS 连接（复用连接，不另开通道）。

与样板（``others/onebot/hermes-napcat-plugin``）的关键差异：

- **fail-closed 白名单**：读类/发送类默认放行；管理类（群管理等）要求
  调用会话归属用户在 ``ONEBOT_ADMIN_USERS``；未知 action 一律拒绝。
- **管理员归属**：adapter 记录最近一条通过门控的入站消息发送者
  （``OnebotAdapter.last_session_user``）；session 平台不是 onebot 时
  （CLI / cron 等本地会话）无法归属调用者，一律按白名单只读模式。
- **media_id 桥接**：入站媒体标记 ``[图片:m1]`` 里的短 id 可作为
  ``media_id`` 参数传给 get_image/get_record/get_file，由工具经
  ``OnebotAdapter.current_registry()`` 还原成实现端的 ``file`` 引用。
- ``_LIVE_ADAPTERS`` 弱引用集定义在本模块（避免 ``__init__`` ↔ ``tools``
  循环导入）：``__init__.py`` 的 adapter_factory 构建后经
  :func:`register_live_adapter` 把 adapter 加进来。

handler 返回值必须是 JSON 字符串（工具注册表只接受 str 结果，
``is_async=True`` 由分发器 await）。
"""

from __future__ import annotations

import json
import logging
import weakref
from typing import Any, Dict, Optional

from .adapter import NetworkError, NotConnectedError, OneBotApiError

logger = logging.getLogger(__name__)

__all__ = [
    "ADMIN_ACTIONS",
    "READ_SEND_ACTIONS",
    "ONEBOT_CALL_SCHEMA",
    "check_onebot_available",
    "register_live_adapter",
    "register_tools",
]

# ── live adapter 池（弱引用；定义在本模块避免循环导入）──────────────────────
# 弱：一个 hook/工具查询绝不能让已断开的 adapter 活着；网关对每个平台条目
# 本来也只构建一个 adapter 实例。
_LIVE_ADAPTERS: "weakref.WeakSet[Any]" = weakref.WeakSet()


def register_live_adapter(adapter) -> None:
    """adapter_factory 构建后调用：让工具层能找到 live adapter（``__init__.py`` 用）。"""
    try:
        _LIVE_ADAPTERS.add(adapter)
    except TypeError:  # pragma: no cover - 适配器不可弱引用时的防御
        logger.debug("onebot_call: adapter is not weak-referenceable", exc_info=True)


# ── action 白名单 ─────────────────────────────────────────────────────────────
# 读类/发送类：无风险，默认放行。
READ_SEND_ACTIONS = frozenset({
    # 账号 / 状态 / 关系
    "get_login_info", "get_status", "get_version_info",
    "get_friend_list", "get_group_list", "get_group_info",
    "get_group_member_info", "get_group_member_list", "get_group_honor_info",
    "get_stranger_info", "can_send_image", "can_send_record",
    # 消息读取
    "get_msg", "get_forward_msg", "get_group_msg_history",
    "get_friend_msg_history", "mark_msg_as_read", "delete_msg",
    # 媒体（get_image_by_id / ocr_image / download_file 为 NapCat 扩展）
    "get_image", "get_record", "get_file", "get_image_by_id",
    "ocr_image", "download_file",
    # 发送 / 回应
    "send_msg", "send_private_msg", "send_group_msg",
    "send_like", "set_msg_emoji_like",
})

# 管理类：需调用会话归属用户在 ONEBOT_ADMIN_USERS，否则拒绝（fail-closed）。
ADMIN_ACTIONS = frozenset({
    "set_group_card", "set_group_kick", "set_group_ban", "set_group_whole_ban",
    "set_group_admin", "set_group_name", "set_group_leave",
    "set_group_special_title", "set_group_add_request",
    "set_friend_add_request", "delete_friend",
    "upload_group_file", "upload_private_file", "set_qq_profile",
})

# 支持 ``media_id`` 桥接的媒体动作（实现端参数名统一为 ``file``）
_MEDIA_ACTIONS = frozenset({"get_image", "get_record", "get_file"})


# ── live adapter 解析 ─────────────────────────────────────────────────────────

def _get_onebot_adapter():
    """运行中网关的 OneBot 适配器；拿不到返回 None。

    首选 ``tools.send_message_senders._live_adapter``——多路复用下解析到
    **当前 profile** 的适配器（裸 ``runner.adapters`` 会命中默认 profile）。
    弱引用集只是 runner 引用不可得时（standalone / 测试宿主）的兜底。
    """
    try:
        from gateway.config import Platform
        from tools.send_message_senders import _live_adapter
        _, adapter = _live_adapter(Platform("onebot"))
        if adapter is not None:
            return adapter
    except Exception:
        pass
    candidates = [a for a in list(_LIVE_ADAPTERS) if a is not None]
    for candidate in candidates:
        if getattr(candidate, "is_connected", False):
            return candidate
    return candidates[0] if candidates else None


def _session_platform() -> str:
    """当前回合的消息平台名（非会话上下文返回空串）。"""
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_PLATFORM", "")
    except Exception:
        return ""


def check_onebot_available() -> bool:
    """工具可见性门：session 平台是 onebot，或网关进程里有已连接适配器。"""
    if _session_platform() == "onebot":
        return True
    adapter = _get_onebot_adapter()
    return adapter is not None and bool(getattr(adapter, "is_connected", False))


# ── 管理员判定 ────────────────────────────────────────────────────────────────

def _admin_verdict(adapter) -> Optional[str]:
    """管理类 action 的放行判定；拒绝时返回 error 文案，放行返回 None。

    fail-closed：session 平台不是 onebot（CLI/cron 等本地会话）时无法把
    调用归属到某个 QQ 用户，一律按只读模式拒绝管理类动作。
    """
    if _session_platform() != "onebot":
        return (
            "Admin actions are only available from a OneBot (QQ) chat session — the "
            "calling user cannot be attributed on this surface, so the tool stays "
            "read-only here."
        )
    if adapter is None:
        return "Admin actions require a connected OneBot adapter."
    admins = tuple(getattr(adapter, "admin_users", ()) or ())
    if not admins:
        return (
            "Admin actions require ONEBOT_ADMIN_USERS to list the caller's QQ "
            "number; the list is currently empty."
        )
    user_id = str(getattr(adapter, "last_session_user", "") or "").strip()
    if user_id and user_id in admins:
        return None
    return (
        "Admin actions are restricted to ONEBOT_ADMIN_USERS; the calling user "
        f"({user_id or 'unknown'}) is not on that list."
    )


# ── media_id 桥接 ─────────────────────────────────────────────────────────────

def _bridge_media_id(adapter, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 ``media_id`` 短标记还原成实现端的 ``file`` 引用；解析失败返回 None。

    入站媒体以 ``[图片:m1]`` 等内联标记呈现（segments.py 的 MediaRegistry，
    按消息轮换）；``current_registry()`` 返回最近一条入站消息的注册表。
    """
    media_id = str(params.get("media_id") or "").strip()
    if not media_id:
        return params
    registry_fn = getattr(adapter, "current_registry", None)
    registry = registry_fn() if callable(registry_fn) else None
    ref = registry.get(media_id) if registry is not None else None
    if ref is None:
        return None
    file_ref = ref.file or ref.url
    if not file_ref:
        return None
    bridged = {key: value for key, value in params.items() if key != "media_id"}
    bridged.setdefault("file", file_ref)
    if ref.file_size is not None:
        bridged.setdefault("file_size", ref.file_size)
    return bridged


# ── action 执行 ───────────────────────────────────────────────────────────────

async def _do_onebot_call(
    action: str, params: Dict[str, Any], *, adapter: Optional[Any] = None,
) -> Dict[str, Any]:
    """把白名单内的 action 经 live adapter 的反向 WS 连接发给实现端。

    ``adapter`` 已解析时直接复用（调用方 _onebot_call_handler 保证判定与
    执行同实例）；为 None 时自行解析（保持独立可用）。
    """
    if adapter is None:
        adapter = _get_onebot_adapter()
    if adapter is None:
        return {
            "error": (
                "OneBot call requires the gateway to be running with the OneBot "
                "adapter loaded. Start it via `hermes gateway run` and ensure the "
                "OneBot implementation (NapCat) is configured."
            )
        }
    if not getattr(adapter, "is_connected", False):
        return {
            "error": (
                "OneBot adapter exists but is not connected — let the OneBot "
                "implementation dial into the reverse WebSocket first."
            )
        }
    call_action = getattr(adapter, "call_action", None)
    if not callable(call_action):
        return {"error": "OneBot adapter does not expose call_action(); upgrade the plugin."}

    if action in _MEDIA_ACTIONS:
        params = _bridge_media_id(adapter, params)
        if params is None:
            return {
                "error": (
                    "media_id not found in the current message's media registry — "
                    "markers like [图片:m1] only resolve for the most recent inbound "
                    "message; ask the sender to resend or use get_group_msg_history."
                )
            }

    try:
        response = await call_action(action, params or {})
    except NotConnectedError as exc:
        return {"error": f"OneBot action '{action}' failed: {exc}", "retryable": True}
    except NetworkError as exc:
        return {"error": f"OneBot action '{action}' timed out or the link failed: {exc}"}
    except OneBotApiError as exc:
        return {"error": str(exc), "retcode": exc.retcode, "raw": exc.payload}
    except Exception as exc:  # pragma: no cover - 防御
        return {"error": f"OneBot action '{action}' raised: {exc}"}

    if not isinstance(response, dict):
        return {"error": f"OneBot returned non-dict response: {type(response).__name__}"}
    return {
        "success": True,
        "action": action,
        "status": response.get("status", "ok"),
        "retcode": response.get("retcode", 0),
        "data": response.get("data"),
    }


def _as_tool_result(payload: Dict[str, Any]) -> str:
    """JSON 编码工具结果——注册表只接受 ``str`` 结果。"""
    return json.dumps(payload, ensure_ascii=False, default=str)


async def _onebot_call_handler(args: Dict[str, Any], **kwargs) -> str:
    """注册表入口（``is_async=True``）：白名单/管理员判定 → 转发 → JSON 结果。"""
    del kwargs
    action = args.get("action")
    if not action or not isinstance(action, str):
        return _as_tool_result(
            {"error": "'action' is required and must be an OneBot 11 endpoint name (string)."})
    action = action.strip()
    params = args.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _as_tool_result({"error": "'params' must be an object/dict of action parameters."})

    # fail-closed 白名单：管理类先过管理员档，未知 action 直接拒绝。
    # adapter 只解析一次：管理员判定与实际执行用同一个实例（防两次解析间
    # live adapter 更换导致「判的是 A、发的是 B」的归属错位）。
    adapter = _get_onebot_adapter()
    if action in ADMIN_ACTIONS:
        error = _admin_verdict(adapter)
        if error:
            return _as_tool_result({"error": error, "action": action})
    elif action not in READ_SEND_ACTIONS:
        return _as_tool_result({
            "error": (
                f"Action '{action}' is not in the onebot_call whitelist — unknown or "
                "not-yet-allowlisted actions are rejected (fail-closed). The available "
                "actions are listed in the tool description and the onebot skill."
            ),
            "action": action,
        })

    return _as_tool_result(await _do_onebot_call(action, params, adapter=adapter))


# ── schema（description 内嵌 action 目录）─────────────────────────────────────

ONEBOT_CALL_SCHEMA = {
    "name": "onebot_call",
    "description": (
        "Invoke a whitelisted OneBot 11 action against the connected QQ account "
        "(NapCat or a compatible implementation). Use this for QQ operations beyond "
        "plain text replies — fetch message history, resolve inbound media, send "
        "messages/likes, manage the bot's QQ profile, and so on.\n\n"
        "IMPORTANT — output style for QQ replies: QQ does NOT render Markdown. "
        "Compose user-visible replies in plain text only — no **bold**, no #, no -, "
        "no `code`, no fenced blocks, no tables. Keep replies short; long texts are "
        "split automatically.\n\n"
        "Inbound media arrives as inline markers — [图片:m1] image, [语音:m1] voice, "
        "[视频:m1] video, [文件:<name>:m1] file — where m1 is a per-message media id. "
        "Resolve them on demand: get_image / get_record / get_file accept "
        "{\"media_id\": \"m1\"} and translate it to the implementation's file "
        "reference. Media ids only resolve for the most recent inbound message.\n\n"
        "Whitelisted actions (anything else is REJECTED — fail-closed):\n"
        "  Read/status: get_login_info, get_status, get_version_info, get_friend_list, "
        "get_group_list, get_group_info, get_group_member_info, get_group_member_list, "
        "get_group_honor_info, get_stranger_info, can_send_image, can_send_record.\n"
        "  Messages: get_msg, get_forward_msg, get_group_msg_history, "
        "get_friend_msg_history, mark_msg_as_read, delete_msg.\n"
        "  Media: get_image, get_record, get_file, get_image_by_id, ocr_image, "
        "download_file.\n"
        "  Send/reactions: send_msg, send_private_msg, send_group_msg, send_like, "
        "set_msg_emoji_like.\n"
        "  Admin tier (requires the calling QQ user to be listed in "
        "ONEBOT_ADMIN_USERS; refused everywhere else, e.g. local CLI sessions): "
        "set_group_card, set_group_kick, set_group_ban, set_group_whole_ban, "
        "set_group_admin, set_group_name, set_group_leave, set_group_special_title, "
        "set_group_add_request, set_friend_add_request, delete_friend, "
        "upload_group_file, upload_private_file, set_qq_profile.\n\n"
        "Examples: get_group_msg_history → {group_id:123, count:20}; "
        "get_image → {media_id:'m1'}; delete_msg → {message_id:'...'}; "
        "send_like → {user_id:10001, times:1}; "
        "set_group_card → {group_id:123, user_id:10001, card:'nickname'}.\n\n"
        "Returns JSON: on success {success:true, action, retcode:0, data:{...}}; on "
        "failure {error, retcode?, raw?}. Parameter shapes: see the onebot skill."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "OneBot 11 action endpoint name from the whitelist, e.g. "
                    "'get_group_msg_history', 'get_image', 'delete_msg'. Unknown "
                    "actions are rejected."
                ),
            },
            "params": {
                "type": "object",
                "description": (
                    "Action parameters as a JSON object. Shape depends on the action. "
                    "For get_image/get_record/get_file pass {media_id:'m1'} to resolve "
                    "an inbound media marker; other examples: get_group_msg_history → "
                    "{group_id:123, count:20}; delete_msg → {message_id:'...'}; "
                    "get_group_member_info → {group_id:123, user_id:10001}."
                ),
                "additionalProperties": True,
            },
        },
        "required": ["action"],
    },
}


def register_tools(ctx) -> None:
    """把 ``onebot_call`` 注册进 ``onebot`` 工具集。"""
    ctx.register_tool(
        name="onebot_call",
        toolset="onebot",
        schema=ONEBOT_CALL_SCHEMA,
        handler=_onebot_call_handler,
        is_async=True,
        description=ONEBOT_CALL_SCHEMA["description"],
        check_fn=check_onebot_available,
        emoji="🐧",
    )
