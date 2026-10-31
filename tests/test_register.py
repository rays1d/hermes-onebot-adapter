"""test_register.py — 插件注册契约与工具层（不依赖 WS 的单元断言）。

覆盖：validate_config/is_connected（token 三处拼写）、env_enablement_fn 种子、
apply_yaml_bridge（YAML→env，env 优先不改写）、register() 对 mock ctx 的
全参数断言、onebot_call 白名单/管理员档/media_id 桥接。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock


import onebot_plugin as plugin
from conftest import fake_live_adapter
from gateway.config import PlatformConfig
from onebot_plugin import tools
from onebot_plugin.adapter import (
    NetworkError,
    OneBotApiError,
    OnebotAdapter,
    UnknownOutcome,
    check_onebot_requirements,
)
from onebot_plugin.formatting import QQ_TEXT_LIMIT
from onebot_plugin.segments import MediaRegistry
from onebot_plugin.standalone import _standalone_send


def asyncio_run(coro):
    return asyncio.run(coro)


def _handler_result(payload: str) -> dict:
    return json.loads(payload)


# ---------------------------------------------------------------------------
# validate_config / is_connected（token 三处拼写）
# ---------------------------------------------------------------------------

class TestValidateConfig:
    def test_token_in_extra(self):
        assert plugin.validate_config(
            PlatformConfig(enabled=True, extra={"token": "t"})) is True

    def test_token_typed_key(self):
        assert plugin.validate_config(PlatformConfig(enabled=True, token="t")) is True

    def test_token_in_scoped_env(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_TOKEN", "env-tok")
        assert plugin.validate_config(PlatformConfig(enabled=True)) is True

    def test_missing_token_fails(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_TOKEN", raising=False)
        assert plugin.validate_config(PlatformConfig(enabled=True)) is False

    def test_blank_token_fails(self):
        assert plugin.validate_config(
            PlatformConfig(enabled=True, extra={"token": "  "})) is False

    def test_is_connected_mirrors_validate(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_TOKEN", raising=False)
        assert plugin.is_connected(PlatformConfig(enabled=True)) is False
        monkeypatch.setenv("ONEBOT_TOKEN", "env-tok")
        assert plugin.is_connected(PlatformConfig(enabled=True)) is True


# ---------------------------------------------------------------------------
# env_enablement_fn
# ---------------------------------------------------------------------------

class TestEnvEnablement:
    def test_no_token_returns_none(self, monkeypatch):
        monkeypatch.delenv("ONEBOT_TOKEN", raising=False)
        assert plugin._env_enablement() is None

    def test_token_enables_and_seeds_host_port_path(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_TOKEN", "tok")
        monkeypatch.setenv("ONEBOT_HOST", "10.0.0.5")
        monkeypatch.setenv("ONEBOT_PORT", "9000")
        monkeypatch.setenv("ONEBOT_PATH", "ob/ws")  # 无前导斜杠原样种子
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://10.0.0.5:3000")
        seed = plugin._env_enablement()
        assert seed == {
            "token": "tok", "host": "10.0.0.5", "port": 9000,
            "path": "ob/ws", "http_url": "http://10.0.0.5:3000",
        }
        assert isinstance(seed["port"], int)  # conv=int

    def test_home_channel_seeded(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_TOKEN", "tok")
        monkeypatch.setenv("ONEBOT_HOME_CHANNEL", "10086")
        seed = plugin._env_enablement()
        assert seed["home_channel"] == {"chat_id": "10086", "name": "Home"}

    def test_home_channel_name_override(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_TOKEN", "tok")
        monkeypatch.setenv("ONEBOT_HOME_CHANNEL", "group:123")
        monkeypatch.setenv("ONEBOT_HOME_CHANNEL_NAME", "小助手")
        seed = plugin._env_enablement()
        assert seed["home_channel"] == {"chat_id": "group:123", "name": "小助手"}

    def test_bad_port_skipped(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_TOKEN", "tok")
        monkeypatch.setenv("ONEBOT_PORT", "not-a-number")
        seed = plugin._env_enablement()
        assert "port" not in seed


# ---------------------------------------------------------------------------
# apply_yaml_config_fn（YAML→env 桥接，env 优先不改写）
# ---------------------------------------------------------------------------

class TestApplyYamlBridge:
    def test_yaml_values_bridged_to_env_and_extra(self, monkeypatch):
        for name in ("ONEBOT_TOKEN", "ONEBOT_PORT", "ONEBOT_DM_POLICY",
                     "ONEBOT_ALLOWED_GROUPS"):
            monkeypatch.delenv(name, raising=False)
        platform_cfg = {"token": "yaml-tok", "port": 9001, "dm_policy": "open",
                        "allowed_groups": "123,456"}
        seeded = plugin._apply_yaml_config({}, platform_cfg)
        assert seeded == platform_cfg  # 原值进 extra
        import os
        assert os.environ["ONEBOT_TOKEN"] == "yaml-tok"
        assert os.environ["ONEBOT_PORT"] == "9001"
        assert os.environ["ONEBOT_DM_POLICY"] == "open"
        assert os.environ["ONEBOT_ALLOWED_GROUPS"] == "123,456"

    def test_env_wins_not_overwritten(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_DM_POLICY", "allowlist")
        monkeypatch.setenv("ONEBOT_PORT", "8643")
        seeded = plugin._apply_yaml_config({}, {"dm_policy": "open", "port": 9001})
        import os
        assert os.environ["ONEBOT_DM_POLICY"] == "allowlist"  # env 未被改写
        assert os.environ["ONEBOT_PORT"] == "8643"
        assert seeded["dm_policy"] == "open"  # 种子仍携带 YAML 值

    def test_empty_platform_cfg_returns_none(self):
        assert plugin._apply_yaml_config({}, {}) is None

    def test_yaml_table_covers_all_documented_keys(self):
        documented = {
            "token", "host", "port", "path", "http_url", "dm_policy",
            "group_policy", "allowed_users", "group_allowed_users",
            "allowed_groups", "admin_users", "allow_all", "max_msg_chars",
        }
        assert {row[0] for row in plugin.YAML_TABLE} == documented


# ---------------------------------------------------------------------------
# register(ctx) 全参数契约
# ---------------------------------------------------------------------------

class TestRegister:
    def test_register_platform_full_contract(self):
        ctx = MagicMock()
        plugin.register(ctx)
        ctx.register_platform.assert_called_once()
        kwargs = ctx.register_platform.call_args.kwargs
        assert kwargs["name"] == "onebot"
        assert kwargs["label"] == "OneBot (QQ)"
        assert callable(kwargs["adapter_factory"])
        assert kwargs["check_fn"] is check_onebot_requirements
        assert kwargs["validate_config"] is plugin.validate_config
        assert kwargs["required_env"] == ["ONEBOT_TOKEN"]
        assert kwargs["cron_deliver_env_var"] == "ONEBOT_HOME_CHANNEL"
        assert kwargs["standalone_sender_fn"] is _standalone_send
        assert kwargs["allowed_users_env"] == "ONEBOT_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "ONEBOT_ALLOW_ALL_USERS"
        assert kwargs["parse_target_ref_fn"] is plugin.parse_target_ref
        assert kwargs["dashboard_description"] == (
            "通过 OneBot v11（NapCat 等）连接 QQ，支持私聊/群聊/媒体/定时投递")
        # allow_update_command 默认即 True（platform_registry.PlatformEntry），无需注册
        assert "allow_update_command" not in kwargs
        assert kwargs["max_message_length"] == QQ_TEXT_LIMIT * 2
        assert kwargs["emoji"] == "🐧"
        assert kwargs["pii_safe"] is False
        hint = kwargs["platform_hint"]
        assert "QQ" in hint and "Markdown" in hint and "[图片:m1]" in hint
        assert "8643" in kwargs["install_hint"]

    def test_registers_tool_and_skill(self):
        ctx = MagicMock()
        plugin.register(ctx)
        tool_kwargs = ctx.register_tool.call_args.kwargs
        assert tool_kwargs["name"] == "onebot_call"
        assert tool_kwargs["toolset"] == "onebot"
        assert tool_kwargs["is_async"] is True
        assert callable(tool_kwargs["handler"])
        assert callable(tool_kwargs["check_fn"])
        skill_args = ctx.register_skill.call_args
        assert skill_args.args[0] == "onebot"
        assert skill_args.args[1].name == "SKILL.md"

    def test_manifest_declares_provides_tools(self):
        """平台插件默认延迟加载：CLI/TUI 进程不物化平台，不声明 provides_tools 则
        register() 不运行，onebot_call 在非 gateway 会话完全缺失（loader #78050
        语义）。声明后 loader 从 tools.py 预注册客户端工具，要求 register_tools(ctx)。"""
        from agent.skill_utils import yaml_load
        manifest_path = Path(plugin.__file__).parent / "plugin.yaml"
        manifest = yaml_load(manifest_path.read_text(encoding="utf-8"))
        assert manifest.get("provides_tools") == ["onebot_call"]
        # loader 预注册入口：tools.py 必须暴露 register_tools(ctx)（plugins_loader.py 校验）
        assert callable(getattr(tools, "register_tools", None))
        # 预注册走 ctx.register_tool，与 register() 同一入口 → 幂等（同 toolset 重注册放行）
        ctx = MagicMock()
        tools.register_tools(ctx)
        ctx.register_tool.assert_called_once()
        assert ctx.register_tool.call_args.kwargs["name"] == "onebot_call"

    def test_adapter_factory_registers_live_adapter(self):
        ctx = MagicMock()
        plugin.register(ctx)
        factory = ctx.register_platform.call_args.kwargs["adapter_factory"]
        config = PlatformConfig(enabled=True, extra={"token": "t"})
        adapter = factory(config)
        assert isinstance(adapter, OnebotAdapter)
        assert adapter in tools._LIVE_ADAPTERS  # 工具层能找到它（弱引用）

    def test_check_fn_passive_probe(self):
        # 被动探测：aiohttp 可导入即 True；绝不安装东西
        assert check_onebot_requirements() is True


# ---------------------------------------------------------------------------
# parse_target_ref（send_message 目标解析，parse_target_ref_fn 注册键）
# ---------------------------------------------------------------------------

class TestParseTargetRef:
    def test_platform_qualified_group(self):
        assert plugin.parse_target_ref("onebot:group:123") == ("group:123", None)

    def test_platform_qualified_digits_are_private(self):
        assert plugin.parse_target_ref("onebot:123456") == ("123456", None)

    def test_bare_group_ref(self):
        assert plugin.parse_target_ref("group:123") == ("group:123", None)

    def test_bare_digits_are_private(self):
        assert plugin.parse_target_ref("123456") == ("123456", None)

    def test_private_user_dm_prefixes_normalize_to_plain_id(self):
        # 私聊规范化与 build_source 一致：纯 QQ 号，不带前缀
        for ref in ("private:999", "user:999", "dm:999", "onebot:private:999"):
            assert plugin.parse_target_ref(ref) == ("999", None), ref

    def test_whitespace_tolerated(self):
        assert plugin.parse_target_ref("  onebot: group:123 ") == ("group:123", None)
        assert plugin.parse_target_ref(" 123456 ") == ("123456", None)

    def test_group_ref_keeps_non_numeric_target(self):
        # 带前缀 = 显式意图：group: 后的非数字 id 也接受（发送时由实现端判错）
        assert plugin.parse_target_ref("group:abc") == ("group:abc", None)

    def test_unparseable_returns_none(self):
        assert plugin.parse_target_ref("") is None
        assert plugin.parse_target_ref(None) is None
        assert plugin.parse_target_ref("张三的群") is None  # 交还通道目录兜底
        assert plugin.parse_target_ref("group:") is None
        assert plugin.parse_target_ref("onebot:") is None
        assert plugin.parse_target_ref("onebot:group:") is None
        assert plugin.parse_target_ref("onebot:private:") is None
        assert plugin.parse_target_ref("onebot:张三") is None

    def test_thread_id_always_none(self):
        # OneBot v11 无 thread 概念
        assert plugin.parse_target_ref("onebot:group:123")[1] is None


# ---------------------------------------------------------------------------
# onebot_call 工具（白名单 / 管理员档 / media_id 桥接）
# ---------------------------------------------------------------------------

class TestOnebotCallWhitelist:
    def test_unknown_action_rejected(self, monkeypatch):
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: None)
        result = _handler_result(
            asyncio_run(tools._onebot_call_handler({"action": "set_restart"})))
        assert "not in the onebot_call whitelist" in result["error"]
        assert result["action"] == "set_restart"

    def test_action_required(self):
        result = _handler_result(asyncio_run(tools._onebot_call_handler({})))
        assert "action" in result["error"]

    def test_params_must_be_dict(self):
        result = _handler_result(
            asyncio_run(tools._onebot_call_handler({"action": "get_msg", "params": "x"})))
        assert "params" in result["error"]

    def test_read_action_success_shape(self, monkeypatch):
        fake = fake_live_adapter(responses={"get_login_info": {"user_id": 1}})
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_login_info", "params": {}})))
        assert result == {"success": True, "action": "get_login_info",
                          "status": "ok", "retcode": 0, "data": {"user_id": 1}}
        assert fake.calls == [("get_login_info", {}, 30.0)]

    def test_no_adapter_reports_error(self, monkeypatch):
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: None)
        result = _handler_result(
            asyncio_run(tools._onebot_call_handler({"action": "get_status"})))
        assert "gateway" in result["error"].lower()

    def test_disconnected_adapter_reports_dial_in_hint(self, monkeypatch):
        fake = fake_live_adapter(connected=False)
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(
            asyncio_run(tools._onebot_call_handler({"action": "get_status"})))
        assert "not connected" in result["error"]
        assert "retryable" not in result  # 未连接分支不承诺 retryable

    def test_network_error_maps_as_retryable_before_submission(self, monkeypatch):
        """未提交失败（ws.send 层）：工具结果标记 retryable。"""
        fake = fake_live_adapter(responses={"get_msg": NetworkError("链路失败")})
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(
            asyncio_run(tools._onebot_call_handler(
                {"action": "get_msg", "params": {"message_id": 1}})))
        assert "before submission" in result["error"]
        assert result["retryable"] is True

    def test_unknown_outcome_flagged_for_agent(self, monkeypatch):
        """已提交但结果未知：明确告知 agent 不要盲目重试。"""
        fake = fake_live_adapter(
            responses={"get_status": UnknownOutcome("get_status", "超时")})
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(
            asyncio_run(tools._onebot_call_handler({"action": "get_status"})))
        assert "outcome unknown" in result["error"]
        assert result["outcome_unknown"] is True

    def test_api_error_carries_retcode_and_raw(self, monkeypatch):
        err = OneBotApiError(retcode=1200, wording="denied", action="get_msg",
                             payload={"status": "failed"})
        fake = fake_live_adapter(responses={"get_msg": err})
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(
            asyncio_run(tools._onebot_call_handler(
                {"action": "get_msg", "params": {"message_id": 1}})))
        assert result["error"] != ""
        assert result["retcode"] == 1200
        assert result["raw"] == {"status": "failed"}


class TestOnebotCallAdminTier:
    def test_admin_action_refused_on_local_session(self, monkeypatch):
        monkeypatch.setattr(tools, "_session_platform", lambda: "")
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "set_group_ban", "params": {}})))
        assert "read-only" in result["error"]

    def test_admin_action_requires_admin_list(self, monkeypatch):
        monkeypatch.setattr(tools, "_session_platform", lambda: "onebot")
        fake = fake_live_adapter(admins=())
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "set_group_ban", "params": {}})))
        assert "ONEBOT_ADMIN_USERS" in result["error"]

    def test_admin_action_requires_connected_adapter(self, monkeypatch):
        monkeypatch.setattr(tools, "_session_platform", lambda: "onebot")
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: None)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "set_group_ban", "params": {}})))
        assert "connected OneBot adapter" in result["error"]

    def test_admin_action_passes_for_listed_caller(self, monkeypatch):
        monkeypatch.setattr(tools, "_session_platform", lambda: "onebot")
        fake = fake_live_adapter(admins=("10001",), session_user="10001")
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "set_group_ban",
             "params": {"group_id": 1, "user_id": 2, "duration": 60}})))
        assert result["success"] is True
        assert fake.calls[0][0] == "set_group_ban"
        assert fake.calls[0][1] == {"group_id": 1, "user_id": 2, "duration": 60}

    def test_admin_action_refused_for_unlisted_caller(self, monkeypatch):
        monkeypatch.setattr(tools, "_session_platform", lambda: "onebot")
        fake = fake_live_adapter(admins=("10001",), session_user="99999")
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "set_group_kick", "params": {}})))
        assert "not on that list" in result["error"]

    def test_admin_and_read_sets_are_disjoint(self):
        assert not (tools.ADMIN_ACTIONS & tools.READ_SEND_ACTIONS)
        assert "set_group_kick" in tools.ADMIN_ACTIONS
        assert "get_msg" in tools.READ_SEND_ACTIONS
        assert "send_msg" in tools.READ_SEND_ACTIONS


class TestMediaIdBridge:
    def _registry_with_image(self) -> MediaRegistry:
        registry = MediaRegistry()
        registry.register("image", url="https://e.com/a.jpg",
                          file="abc.jpg", file_size=1024)
        return registry

    def test_media_id_resolved_to_file_ref(self, monkeypatch):
        fake = fake_live_adapter(registry=self._registry_with_image())
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_image", "params": {"media_id": "m1"}})))
        assert result["success"] is True
        action, params, _ = fake.calls[0]
        assert action == "get_image"
        assert params == {"file": "abc.jpg", "file_size": 1024}  # media_id 已还原

    def test_media_id_falls_back_to_url(self, monkeypatch):
        registry = MediaRegistry()
        registry.register("image", url="https://e.com/b.jpg")  # 无 file 字段
        fake = fake_live_adapter(registry=registry)
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_image", "params": {"media_id": "m1"}})))
        assert result["success"] is True
        assert fake.calls[0][1] == {"file": "https://e.com/b.jpg"}

    def test_unknown_media_id_reports_error_without_api_call(self, monkeypatch):
        fake = fake_live_adapter(registry=self._registry_with_image())
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_image", "params": {"media_id": "m9"}})))
        assert "not found in any recent message's media registry" in result["error"]
        assert fake.calls == []  # 未发出任何 API 请求

    def test_no_registry_also_reports_error(self, monkeypatch):
        fake = fake_live_adapter()  # current_registry 为 None
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_record", "params": {"media_id": "m1"}})))
        assert "not found in any recent message's media registry" in result["error"]

    def test_media_action_without_media_id_passes_through(self, monkeypatch):
        fake = fake_live_adapter()
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_file", "params": {"file": "xyz"}})))
        assert result["success"] is True
        assert fake.calls[0][1] == {"file": "xyz"}

    def test_extra_params_preserved_beside_file(self, monkeypatch):
        fake = fake_live_adapter(registry=self._registry_with_image())
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_record",
             "params": {"media_id": "m1", "out_format": "mp3"}})))
        assert result["success"] is True
        assert fake.calls[0][1] == {"out_format": "mp3", "file": "abc.jpg",
                                    "file_size": 1024}

    # ── 新链路：resolve_media_ref（注册表环）+ 兑底 + 落地缓存 + 瘦身 ──

    def _recent_fake(self, monkeypatch, ref, responses=None):
        registry = MediaRegistry()
        registry._refs[ref.media_id] = ref
        registry._counter = 1
        fake = fake_live_adapter(recent_media=[("111", registry)],
                                 responses=responses or {})
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        return fake

    def test_ref_without_url_file_reports_missing_ref(self, monkeypatch):
        from onebot_plugin.segments import MediaRef
        ref = MediaRef(media_id="m1", type="image", url=None, file=None)
        fake = self._recent_fake(monkeypatch, ref)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_image", "params": {"media_id": "m1"}})))
        assert "without url/file" in result["error"]
        assert fake.calls == []  # 缺引用时不发请求

    def test_get_record_defaults_to_wav(self, monkeypatch):
        from onebot_plugin.segments import MediaRef
        ref = MediaRef(media_id="m1", type="record", url="https://e.com/a",
                       file="abc", file_size=10)
        fake = self._recent_fake(monkeypatch, ref)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_record", "params": {"media_id": "m1"}})))
        assert result["success"] is True
        assert fake.calls[0][1]["out_format"] == "wav"

    def test_get_image_base64_becomes_local_file(self, monkeypatch, tmp_path):
        from onebot_plugin.segments import MediaRef
        png = bytes.fromhex("89504e470d0a1a0a") + b"rest"
        import base64 as b64mod
        ref = MediaRef(media_id="m1", type="image", url=None, file="abc")
        fake = self._recent_fake(
            monkeypatch, ref,
            responses={"get_image": {"file": "abc", "base64": b64mod.b64encode(png).decode()}})
        cached = tmp_path / "img.png"

        async def fake_cache(data, ext=".jpg"):
            cached.write_bytes(data)
            return str(cached)

        monkeypatch.setattr(tools, "_cache_image_fn", lambda: fake_cache)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_image", "params": {"media_id": "m1"}})))
        assert result["success"] is True
        assert result["data"]["local_file"] == str(cached)
        assert result["data"]["mime"] == "image/png"
        assert cached.read_bytes() == png

    def test_get_image_url_download_localized(self, monkeypatch, tmp_path):
        from onebot_plugin.segments import MediaRef
        png = bytes.fromhex("89504e470d0a1a0a") + b"x"
        ref = MediaRef(media_id="m1", type="image", url="https://e.com/a.png", file=None)
        fake = self._recent_fake(
            monkeypatch, ref, responses={"get_image": {"url": "https://e.com/a.png"}})
        cached = tmp_path / "dl.png"

        async def fake_fetch(adapter_, url):
            return png

        async def fake_cache(data, ext=".jpg"):
            cached.write_bytes(data)
            return str(cached)

        monkeypatch.setattr(tools, "_fetch_media_bytes", fake_fetch)
        monkeypatch.setattr(tools, "_cache_image_fn", lambda: fake_cache)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_image", "params": {"media_id": "m1"}})))
        assert result["data"]["local_file"] == str(cached)
        assert result["data"]["mime"] == "image/png"

    def test_get_image_download_failure_keeps_success(self, monkeypatch):
        from onebot_plugin.segments import MediaRef
        ref = MediaRef(media_id="m1", type="image", url="https://e.com/a.png", file=None)
        fake = self._recent_fake(
            monkeypatch, ref, responses={"get_image": {"url": "https://e.com/a.png"}})

        async def fail_fetch(adapter_, url):
            return None

        monkeypatch.setattr(tools, "_fetch_media_bytes", fail_fetch)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_image", "params": {"media_id": "m1"}})))
        # 不谎报失败：整体 success 保留，local_file 置空并附 fetch_error
        assert result["success"] is True
        assert result["data"]["local_file"] is None
        assert result["data"]["fetch_error"]
        assert result["data"]["url"] == "https://e.com/a.png"

    def test_history_base64_stripped(self, monkeypatch):
        fake = fake_live_adapter(responses={"get_friend_msg_history": {
            "messages": [{"message": [{"type": "image",
                                       "data": {"base64": "B" * 9000,
                                                "url": "https://e.com/a.jpg"}}]}],
        }})
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: fake)
        result = _handler_result(asyncio_run(tools._onebot_call_handler(
            {"action": "get_friend_msg_history",
             "params": {"user_id": 1, "count": 5}})))
        seg = result["data"]["messages"][0]["message"][0]["data"]
        assert "omitted" in seg["base64"]
        assert seg["url"] == "https://e.com/a.jpg"  # 小字段不动


class TestToolVisibility:
    def test_available_when_session_platform_is_onebot(self, monkeypatch):
        monkeypatch.setattr(tools, "_session_platform", lambda: "onebot")
        assert tools.check_onebot_available() is True

    def test_unavailable_without_adapter_or_session(self, monkeypatch):
        monkeypatch.setattr(tools, "_session_platform", lambda: "")
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: None)
        assert tools.check_onebot_available() is False

    def test_schema_declares_action_catalog(self):
        schema = tools.ONEBOT_CALL_SCHEMA
        assert schema["name"] == "onebot_call"
        assert schema["parameters"]["required"] == ["action"]
        description = schema["description"]
        for marker in ("ONEBOT_ADMIN_USERS", "[图片:m1]", "get_group_msg_history",
                       "REJECTED"):
            assert marker in description

    def test_tool_result_is_json_string(self, monkeypatch):
        monkeypatch.setattr(tools, "_get_onebot_adapter", lambda: None)
        raw = asyncio_run(tools._onebot_call_handler({"action": "get_status"}))
        assert isinstance(raw, str)
        json.loads(raw)  # 必须是合法 JSON（工具注册表只收 str）
