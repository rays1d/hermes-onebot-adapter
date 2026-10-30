"""standalone.py — 进程外 cron/通知投递（纯 HTTP，经实现端 OneBot HTTP API）。

cron 调度器在独立于 gateway 的进程里跑（``hermes cron run``），而本插件与
实现端的链路是**反向 WebSocket**——实现端拨入 gateway 持有的监听端口，cron
子进程无法复用它。所以 standalone 路径走实现端的 **OneBot 11 HTTP API**：
NapCat 侧启用 HTTP server，插件经 ``ONEBOT_HTTP_URL``（或 config.yaml
``platforms.onebot.http_url``）指向其 base URL；access token 与 WS 的
``ONEBOT_TOKEN`` 相同（``Authorization: Bearer``）。

``thread_id`` / ``media_files`` / ``force_document`` 仅为签名平价接受：
standalone HTTP 路径只投递纯文本（媒体走 live adapter 的出站全家）。
分段复用 :func:`formatting.split_for_qq`（句边界，默认 2000 字符），
段间 0.3s 间隔与 adapter 一致，缓解个人号高频发送风控。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from gateway.platforms._shared import (
    get_scoped_secret as _get_scoped_secret,
    send_error,
)

from .formatting import QQ_TEXT_LIMIT, split_for_qq, strip_markdown

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 30        # 单段投递的 HTTP 总超时
CHUNK_INTERVAL_S = 0.3     # 与 adapter.SEND_CHUNK_INTERVAL_S 一致（防风控）


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list[str]] = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """经实现端 HTTP API 把文本投递到 ``chat_id``（cron 端到端投递的落点）。"""
    import asyncio

    import aiohttp

    del thread_id, media_files, force_document  # standalone 只发文本（签名平价）

    extra = getattr(pconfig, "extra", None) or {}
    base_url = str(
        _get_scoped_secret("ONEBOT_HTTP_URL") or extra.get("http_url") or ""
    ).rstrip("/")
    if not base_url:
        return {
            "error": (
                "OneBot standalone send: set ONEBOT_HTTP_URL (or http_url under "
                "platforms.onebot.extra) to your OneBot implementation's HTTP API "
                "base URL to enable out-of-process delivery"
            )
        }
    token = str(
        extra.get("token")
        or getattr(pconfig, "token", None)
        or _get_scoped_secret("ONEBOT_TOKEN")
        or ""
    ).strip()

    # home channel 解析：纯数字=私聊 QQ 号；group:<gid>=群；private:<uid> 亦收
    text = str(chat_id or "").strip()
    lowered = text.lower()
    if lowered.startswith("group:"):
        is_group, raw_id = True, text.split(":", 1)[1].strip()
    elif lowered.startswith("private:"):
        is_group, raw_id = False, text.split(":", 1)[1].strip()
    else:
        is_group, raw_id = False, text
    try:
        target_id = int(raw_id)
    except (TypeError, ValueError):
        return send_error(f"OneBot standalone send: invalid chat_id {chat_id!r}")

    action = "send_group_msg" if is_group else "send_private_msg"
    id_key = "group_id" if is_group else "user_id"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    chunks = split_for_qq(strip_markdown(message or ""), QQ_TEXT_LIMIT)
    if not chunks:
        return send_error("OneBot standalone send: empty message after markdown stripping")

    last_id: Optional[str] = None
    try:
        async with aiohttp.ClientSession() as session:
            for index, chunk in enumerate(chunks):
                payload = {
                    id_key: target_id,
                    "message": [{"type": "text", "data": {"text": chunk}}],
                }
                try:
                    async with session.post(
                        f"{base_url}/{action}",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S),
                    ) as resp:
                        try:
                            body = await resp.json(content_type=None)
                        except ValueError:
                            body = {}
                        if not resp.ok:
                            return send_error(
                                f"OneBot standalone send: HTTP {resp.status}: {body}")
                except aiohttp.ClientError as exc:
                    return send_error(f"OneBot standalone send: connection failed: {exc}")
                status = body.get("status")
                retcode = body.get("retcode")
                # 严格判定：OneBot HTTP 成功必答 {status:"ok", retcode:0}；
                # 非 ok、非零 retcode、或两者皆缺一律视为失败。
                failed = (
                    (status is not None and status != "ok")
                    or (retcode is not None and retcode != 0)
                    or (status is None and retcode is None)
                )
                if failed:
                    msg = (body.get("message") or body.get("wording") or body.get("msg")
                           or f"status={status!r} retcode={retcode!r}")
                    return send_error(f"OneBot standalone send ({action}): {msg}")
                data = body.get("data") or {}
                if isinstance(data, dict) and data.get("message_id"):
                    last_id = str(data["message_id"])
                if index < len(chunks) - 1:
                    await asyncio.sleep(CHUNK_INTERVAL_S)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return send_error(f"OneBot standalone send failed: {exc}")

    return {"success": True, "message_id": last_id}
