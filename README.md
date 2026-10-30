# hermes-onebot-adapter

[Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 OneBot v11 (QQ) 平台插件。

通过 OneBot v11 反向 WebSocket 对接你自有的 NapCat 实例（或其他 OneBot 11 实现端：
LLOneBot / Lagrange / go-cqhttp），让 Hermes 在 QQ 私聊/群聊中对话、收发媒体、执行 QQ 动作——
无需腾讯开放平台 bot 资质，用个人 QQ 号即可获得完整个人号能力。

## 架构

```
QQ 服务器 ⇄ NapCat（你的机器，可与 Hermes 跨机，经 EasyTier 组网）
               │ 反向 WebSocket（NapCat 作 client 拨入，Universal 角色：
               │   事件 + API 同一条全双工连接）
               ▼
┌─────────────────────────────────────────────────┐
│ hermes-onebot-adapter（hermes gateway 进程内插件）  │
│                                                 │
│  OnebotAdapter        反向 WS server（0.0.0.0:8643）│
│   ├─ 入站：token 鉴权 → 去重 → 段解析 → 权限门控      │
│   │        → 群 @/回复触发 → 交 hermes agent        │
│   ├─ 出站：markdown 剥离 → 句边界分段 → send_msg     │
│   └─ 媒体：入站内联标记按需拉取；出站流式上传优先        │
│                                                 │
│  napcat_call 工具     直通 OneBot action（白名单）   │
│  standalone 投递      cron 经 NapCat HTTP API 投递  │
└─────────────────────────────────────────────────┘
```

设计为跨机部署友好：NapCat 与 Hermes 不必同机，EasyTier 虚拟局域网即可互通；
入站媒体走「URL → 按需拉取」，出站大文件优先 `upload_file_stream` 流式上传，
避免 base64 膨胀占满内网带宽。

## 快速开始

> 本节为初稿占位，完整安装步骤随实现进度补全（见 implement.md 步骤 12）。

1. 安装插件到 `~/.hermes/plugins/onebot/`
2. 配置 `ONEBOT_TOKEN`（与 NapCat 反向 WS 的 access token 一致）
3. NapCat 侧添加 WebSocket 客户端（ws-reverse）：
   URL `ws://<hermes主机>:8643/onebot/ws`，token 与上面一致
4. `hermes gateway status` 确认平台已连接，私聊发消息即闭环

## 环境变量

| env | 必填 | 默认 | 说明 |
|---|---|---|---|
| `ONEBOT_TOKEN` | ✅ | — | NapCat 反向 WS 共享密钥 |
| `ONEBOT_HOST` | | `0.0.0.0` | WS 绑定地址（可收紧到 EasyTier 网卡 IP）|
| `ONEBOT_PORT` | | `8643` | WS 监听端口 |
| `ONEBOT_PATH` | | `/onebot/ws` | WS 路径 |
| `ONEBOT_HTTP_URL` | | — | NapCat HTTP API（cron/standalone 投递必需）|
| `ONEBOT_HOME_CHANNEL` | | — | cron 目标（QQ 号或 `group:<gid>`）|
| `ONEBOT_DM_POLICY` | | `allowlist` | 私聊策略：`allowlist` / `open` / `disabled` |
| `ONEBOT_GROUP_POLICY` | | `allowlist` | 群聊策略：`allowlist` / `open` / `disabled` |
| `ONEBOT_ALLOWED_USERS` | | — | 私聊白名单（逗号分隔 QQ 号）|
| `ONEBOT_GROUP_ALLOWED_USERS` | | — | 群内白名单（空 = 群全员）|
| `ONEBOT_ALLOWED_GROUPS` | | — | 群白名单（allowlist 策略下必填）|
| `ONEBOT_ADMIN_USERS` | | — | 管理员（解锁群管理等危险 action）|
| `ONEBOT_ALLOW_ALL_USERS` | | `false` | 一键放开（fail-open，仅限可信环境）|
| `ONEBOT_MAX_MSG_CHARS` | | `2000` | 出站单条消息分段阈值 |

## 故障排查

> 初稿占位，随 README 完整化补全（token 不匹配、端口不通、EasyTier 路由等）。

## License

MIT
