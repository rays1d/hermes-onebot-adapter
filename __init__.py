"""hermes-onebot-adapter：Hermes Agent 的 OneBot v11 平台插件（注册入口）。

独立插件目录：把本目录放到 ``~/.hermes/plugins/onebot/``（目录名 = 平台
注册名）——零 pip 安装、零核心改动。Hermes 发现有 ``plugin.yaml`` 且
``__init__.py`` 暴露 ``register(ctx)`` 的子目录即加载。

模块布局：
    __init__.py     注册入口（本文件）+ 配置探针
    adapter.py      OnebotAdapter — 反向 WS 传输 / 入站分派 / 出站全家
    segments.py     OneBot 消息段 ↔ 内部表示 ↔ 媒体注册表（纯函数）
    formatting.py   markdown → QQ 纯文本 + 句边界分段（纯函数）
    tools.py        onebot_call — 白名单 + 管理员档的直通工具
    standalone.py   进程外 cron 投递（经实现端 OneBot HTTP API）
    skills/onebot/  agent 操作说明（输出风格 + 动作目录 + 媒体解析）

平台插件铁律：绝不直接读 ``os.environ``、绝不写 ``os.environ``——所有
env/凭据读取走 ``gateway.platforms._shared`` 的 scoped secret（多路复用
下进程 env 是默认 profile 的值，裸读会串台）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from gateway.platforms._shared import (
    apply_yaml_bridge as _apply_yaml_bridge,
    get_scoped_secret as _get_scoped_secret,
    seed_extra_from_env as _seed_extra_from_env,
)

from . import tools as _tools
from .adapter import OnebotAdapter, check_onebot_requirements
from .formatting import QQ_TEXT_LIMIT
from .standalone import _standalone_send

logger = logging.getLogger(__name__)

__all__ = ["register"]

# ── config.yaml 桥接表（apply_yaml_config_fn 表驱动）─────────────────────────
# (yaml_key, ENV_VAR, kind)。apply_yaml_bridge 的 kind 只认 lower/str/csv/json；
# port / max_msg_chars 用 "str"——YAML 原值进 extra（int 保留 int），env 落字符串，
# adapter 侧 int() 再转换。env > YAML（env 已设时跳过写 env）。
YAML_TABLE = (
    ("token", "ONEBOT_TOKEN", "str"),
    ("host", "ONEBOT_HOST", "str"),
    ("port", "ONEBOT_PORT", "str"),
    ("path", "ONEBOT_PATH", "str"),
    ("http_url", "ONEBOT_HTTP_URL", "str"),
    ("dm_policy", "ONEBOT_DM_POLICY", "str"),
    ("group_policy", "ONEBOT_GROUP_POLICY", "str"),
    ("allowed_users", "ONEBOT_ALLOWED_USERS", "str"),
    ("group_allowed_users", "ONEBOT_GROUP_ALLOWED_USERS", "str"),
    ("allowed_groups", "ONEBOT_ALLOWED_GROUPS", "str"),
    ("admin_users", "ONEBOT_ADMIN_USERS", "str"),
    ("allow_all", "ONEBOT_ALLOW_ALL_USERS", "str"),
    ("max_msg_chars", "ONEBOT_MAX_MSG_CHARS", "str"),
)


# ── 被动探针 / 配置助手（插件注册表回调）──────────────────────────────────────

def _configured_token(config) -> str:
    """token 的三处拼写归一（YAML extra / YAML typed key / scoped env）。"""
    extra = getattr(config, "extra", None) or {}
    return str(
        extra.get("token")
        or getattr(config, "token", None)
        or _get_scoped_secret("ONEBOT_TOKEN")
        or ""
    ).strip()


def validate_config(config) -> bool:
    """平台是否配置到可启动的程度。

    token 是唯一硬要求（plugin.yaml ``requires_env``）：实现端反向 WS 拨入
    的鉴权凭证，可来自 ``.env``（``ONEBOT_TOKEN``）或 config.yaml
    （``platforms.onebot.token`` 或 ``platforms.onebot.extra.token``）。
    host/port/path 都有默认值。刻意不做"无 token 强启"路径：enabled 但无
    token 的 adapter 只会永远重试连接失败。
    """
    return bool(_configured_token(config))


def is_connected(config) -> bool:
    """配置层面"已接好"的判定（setup 状态展示用）——与 validate_config 同义。"""
    return validate_config(config)


def _env_enablement() -> dict | None:
    """config 加载期从本 profile 的 env 种 ``PlatformConfig.extra``。

    纯 seeder，无副作用。token 是启用开关（required_env 唯一项）：无 token
    返回 None 即不启用（fail-closed）。行为类配置（白名单/策略）不在此处
    种——adapter 构造时按 scoped secret 读取，也可写在 config.yaml extra。
    """
    token = str(_get_scoped_secret("ONEBOT_TOKEN") or "").strip()
    if not token:
        return None
    seed = _seed_extra_from_env((
        ("ONEBOT_HOST", "host", None),
        ("ONEBOT_PORT", "port", int),
        ("ONEBOT_PATH", "path", None),
        ("ONEBOT_HTTP_URL", "http_url", None),
    ), home_env="ONEBOT_HOME_CHANNEL")
    seed["token"] = token
    return seed


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict | None:
    """config.yaml ``platforms.onebot.<key>`` → ``ONEBOT_*`` env（env 优先）+ extra 种子。"""
    del yaml_cfg  # 表键都直接挂在平台段下，无需整份 YAML
    return _apply_yaml_bridge(platform_cfg, YAML_TABLE)


# ── adapter 工厂 ──────────────────────────────────────────────────────────────

def _build_adapter(config) -> OnebotAdapter:
    """``adapter_factory``：构建 adapter 并让它可被 hooks/tools 找到。"""
    adapter = OnebotAdapter(config)
    _tools.register_live_adapter(adapter)
    return adapter


# ── 插件入口 ──────────────────────────────────────────────────────────────────

def register(ctx) -> None:
    """Hermes 插件系统发现本插件时调用。"""
    ctx.register_platform(
        name="onebot",
        label="OneBot (QQ)",
        adapter_factory=_build_adapter,
        check_fn=check_onebot_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ONEBOT_TOKEN"],
        install_hint=(
            "Place this plugin directory at ~/.hermes/plugins/onebot/ (aiohttp ships "
            "with hermes — no extra deps), set ONEBOT_TOKEN to the access token of "
            "the WebSocket-client (reverse WS) item in your OneBot implementation "
            "(NapCat), and point that client at ws://<hermes-host>:8643/onebot/ws."
        ),
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="ONEBOT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="ONEBOT_ALLOWED_USERS",
        allow_all_env="ONEBOT_ALLOW_ALL_USERS",
        max_message_length=QQ_TEXT_LIMIT * 2,
        emoji="🐧",
        pii_safe=False,
        platform_hint=(
            "You are chatting via QQ (OneBot 11 / NapCat). QQ does not render "
            "Markdown — reply in plain text only, keep it short and conversational. "
            "In groups you are woken only when explicitly @-mentioned or replied to; "
            "inbound group messages carry a sender-name prefix. Images/voice/files "
            "arrive as [图片:m1] / [语音:m1] / [文件:<name>:m1] markers — resolve them "
            "on demand with the onebot_call tool (get_image / get_record / get_file "
            "accept {media_id: 'm1'})."
        ),
    )

    _tools.register_tools(ctx)

    # 技能注册：存在才挂，缺失只 WARN 不炸（老宿主/打包差异下降级为无技能）
    skill_path = Path(__file__).parent / "skills" / "onebot" / "SKILL.md"
    if skill_path.exists():
        try:
            ctx.register_skill("onebot", skill_path)
        except Exception:
            logger.warning("OneBot: skill registration failed", exc_info=True)
    else:
        logger.warning("OneBot: skill file missing at %s", skill_path)
