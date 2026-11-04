# hermes-onebot-adapter

[![tests](https://github.com/rays1d/hermes-onebot-adapter/actions/workflows/tests.yml/badge.svg)](https://github.com/rays1d/hermes-onebot-adapter/actions/workflows/tests.yml)

[Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 OneBot v11 (QQ) 平台插件。

通过 OneBot v11 反向 WebSocket 对接你自有的 NapCat 实例（或其他 OneBot 11 实现端：
LLOneBot / Lagrange / go-cqhttp），让 Hermes 在 QQ 私聊/群聊中对话、收发媒体、执行 QQ 动作——
无需腾讯开放平台 bot 资质，用个人 QQ 号即可获得完整个人号能力。

官方自带的 `qqbot` 平台走腾讯开放平台 API v2（需审核、能力受限）；本插件走 OneBot 路线，
适合已有 NapCat（或愿意装一个）的个人用户。

## 架构

```
QQ 服务器 ⇄ NapCat（你的机器，可与 Hermes 跨机，经 EasyTier 等组网互联）
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
│  onebot_call 工具     直通 OneBot action（白名单）   │
│  standalone 投递      cron 经 NapCat HTTP API 投递  │
└─────────────────────────────────────────────────┘
```

设计为跨机部署友好：NapCat 与 Hermes 不必同机，EasyTier 虚拟局域网即可互通；
入站媒体走「内联标记 → 按需拉取」，出站大文件优先 `upload_file_stream` 流式上传
（NapCat ≥ 4.8.115），失败自动回退 base64，避免 base64 膨胀占满内网带宽。

零第三方依赖：aiohttp 用 hermes 自带的一份，插件无 pip 需求。

## 安装

从 GitHub 安装（推荐；升级一条 `hermes plugins update` 即可）：

```bash
hermes plugins install rays1d/hermes-onebot-adapter --enable
```

装完**重启 gateway**——平台适配器不会热物化（插件在 gateway 启动后才 enable 的话，
平台不会出现在运行中的进程里）：

```bash
systemctl --user restart hermes-gateway   # 按你的部署方式重启
```

> **首次安装会有安全扫描提示（caution）**：Hermes 的安装扫描会把本仓库 README /
> `plugin.yaml` / 测试夹具里的**说明性文字**——如 `0.0.0.0`、`http://…`、`uv run
> --project`——当成网络/供应链风险（实测 14 条均来自文本，`.py` 代码零命中）。
> 交互式终端确认继续即可；无 tty 的自动化安装（SSH / CI）加 `--force` 接受该判定：
> `hermes plugins install rays1d/hermes-onebot-adapter --enable --force`

<details>
<summary>手动安装（离线 / 开发调试）</summary>

把本仓库内容放进 Hermes 的插件目录即可，**目录名任意**——平台注册名 `onebot`
来自代码，与目录名、插件名无关：

```bash
# 二选一：拷贝或 symlink
cp -r hermes-onebot-adapter ~/.hermes/plugins/onebot-adapter
# 或
ln -s /path/to/hermes-onebot-adapter ~/.hermes/plugins/onebot-adapter

hermes plugins enable onebot-adapter
```

校验插件能被发现并加载（`validate` 接的是**目录路径**，不是插件名）：

```bash
hermes plugins validate ~/.hermes/plugins/onebot-adapter
```

</details>

## NapCat 侧配置

在 NapCat 的「网络配置」里新增一项 **WebSocket 客户端**（ws-reverse）：

| 配置项 | 值 |
|---|---|
| URL | `ws://<hermes 主机>:8643/onebot/ws` |
| Token | 与 Hermes 侧 `ONEBOT_TOKEN` 完全一致 |
| 消息格式 | 数组（segment array，本插件优先解析段数组；CQ 码字符串也可）|
| 报文过滤 / 开启事件 | 默认即可 |

同机部署：`<hermes 主机>` 填 `127.0.0.1`。跨机部署（EasyTier / Tailscale 等）：
填 Hermes 机器的虚拟网卡 IP（如 `100.x.x.x`），Hermes 侧保持 `ONEBOT_HOST=0.0.0.0`
默认值即可（不要硬绑定某个虚拟 IP——组网地址可能变化）。

**cron / 定时投递需要额外一步**：cron 调度器跑在独立进程里，无法复用反向 WS 连接，
需要 NapCat 同时开启 **HTTP 服务端**（如 `http://0.0.0.0:3000`，token 一致），
并在 Hermes 侧配置 `ONEBOT_HTTP_URL` 指向它。不使用 cron 交付的话可以不开。

## 环境变量

| env | 必填 | 默认 | 说明 |
|---|---|---|---|
| `ONEBOT_TOKEN` | ✅ | — | NapCat 反向 WS 共享密钥（`Authorization: Bearer` / `?access_token=`）|
| `ONEBOT_HOST` | | `0.0.0.0` | WS 绑定地址（可收紧到 EasyTier 网卡 IP）|
| `ONEBOT_PORT` | | `8643` | WS 监听端口 |
| `ONEBOT_PATH` | | `/onebot/ws` | WS 路径（与 NapCat URL 一致）|
| `ONEBOT_HTTP_URL` | | — | NapCat HTTP API base URL（cron/standalone 投递必需）|
| `ONEBOT_HOME_CHANNEL` | | — | cron 目标：QQ 号（私聊）或 `group:<群号>` |
| `ONEBOT_DM_POLICY` | | `allowlist` | 私聊策略：`allowlist` / `open` / `disabled` |
| `ONEBOT_GROUP_POLICY` | | `allowlist` | 群聊策略：`allowlist` / `open` / `disabled` |
| `ONEBOT_ALLOWED_USERS` | | — | 私聊白名单（逗号分隔 QQ 号）|
| `ONEBOT_GROUP_ALLOWED_USERS` | | — | 群内白名单（空 = 群全员）|
| `ONEBOT_ALLOWED_GROUPS` | | — | 群白名单（`*` = 任意群；allowlist 策略下必填）|
| `ONEBOT_ADMIN_USERS` | | — | 管理员 QQ 号（解锁群管理等危险 action）|
| `ONEBOT_ALLOW_ALL_USERS` | | `false` | 一键放开（fail-open，仅限可信环境）|
| `ONEBOT_MAX_MSG_CHARS` | | `2000` | 出站单条消息分段阈值 |
| `ONEBOT_STRIP_TAGS` | | `false` | 剥行首 `§N§` 编号前缀（见下「出站文本转换」）|
| `ONEBOT_STRIP_PREFIXES` | | — | 逗号分隔的行首字面量前缀（纯字面量，不解释为正则）|

也可以不用 env，写在 `~/.hermes/config.yaml` 的 `platforms.onebot.*` 下（env 优先）：

```yaml
platforms:
  onebot:
    token: "与 NapCat 一致的 token"
    host: 0.0.0.0          # 默认 0.0.0.0
    port: 8643
    path: /onebot/ws
    http_url: "http://100.x.x.x:3000"   # cron 投递用
    dm_policy: allowlist
    group_policy: allowlist
    allowed_users: "10001,10002"
    allowed_groups: "123456,234567"
    admin_users: "10001"
    max_msg_chars: 2000
    outbound_strip_tags: true        # 等价 ONEBOT_STRIP_TAGS=true
    outbound_strip_prefixes: ""      # 等价 ONEBOT_STRIP_PREFIXES
```

`hermes gateway status` 显示平台已连接（等 NapCat 拨入后）、`hermes gateway run`
启动即可。

## 权限模型（分面策略，fail-closed）

- **私聊**：`ONEBOT_DM_POLICY`
  - `allowlist`（默认）：只有 `ONEBOT_ALLOWED_USERS` 里的 QQ 号能用；**名单为空 = 全部拒绝**
  - `open`：任何人可私聊
  - `disabled`：忽略所有私聊
- **群聊**：`ONEBOT_GROUP_POLICY`
  - `allowlist`（默认）：只有 `ONEBOT_ALLOWED_GROUPS` 里的群生效；空 = 全部拒绝，`*` = 任意群
  - 群内可再用 `ONEBOT_GROUP_ALLOWED_USERS` 收紧到人（空 = 群全员）
  - `open` / `disabled` 同上
- **兜底**：`ONEBOT_ALLOW_ALL_USERS=true` 跳过以上所有门（同时跳过网关级用户门）——
  仅建议完全可信的个人部署使用
- **管理员**：`ONEBOT_ADMIN_USERS` 单独一档，解锁 `onebot_call` 的管理类动作
  （踢人/禁言/改群名片等）；管理员判定基于「最近一条通过门控的入站消息发送者」，
  CLI/cron 等非 QQ 会话一律只读

**首次使用怎么拿到自己的 QQ 号**：门控拒绝时会在 gateway 日志里以 INFO 级记录
完整 id 与配置提示，例如：

```
[onebot] 私聊被拒 user_id=123456789（dm_policy=allowlist 且未在 ONEBOT_ALLOWED_USERS；
将其加入白名单或设 ONEBOT_DM_POLICY=open 放行）
```

把日志里的号码复制进对应白名单 env 即可。

**群聊触发条件**：只有 **@机器人** 或 **回复机器人的消息** 才触发回复；普通群聊内容
不会唤醒 agent。入站群消息会带发送者昵称前缀（`张三：\n内容`），方便区分说话人。

## onebot_call 工具

单一工具 `onebot_call(action, params)` 代理所有**白名单内**的 OneBot 11 action，
复用当前反向 WS 连接。白名单外一律拒绝（fail-closed）。

| 分组 | 动作 |
|---|---|
| 读类/状态 | get_login_info, get_status, get_version_info, get_friend_list, get_group_list, get_group_info, get_group_member_info, get_group_member_list, get_group_honor_info, get_stranger_info, can_send_image, can_send_record |
| 消息 | get_msg, get_forward_msg, get_group_msg_history, get_friend_msg_history, mark_msg_as_read, delete_msg |
| 媒体 | get_image, get_record, get_file, get_image_by_id, ocr_image, download_file |
| 发送/回应 | send_msg, send_private_msg, send_group_msg, send_like, set_msg_emoji_like |
| **管理类（需管理员）** | set_group_card, set_group_kick, set_group_ban, set_group_whole_ban, set_group_admin, set_group_name, set_group_leave, set_group_special_title, set_group_add_request, set_friend_add_request, delete_friend, upload_group_file, upload_private_file, set_qq_profile |

入站媒体以 `[图片:m1]` / `[语音:m1]` / `[视频:m1]` / `[文件:<名字>:m1]` 内联标记呈现，
agent 需要时用 `onebot_call` 把 `media_id` 传给 get_image / get_record / get_file
按需拉取（跨机链路不浪费带宽）；媒体 id 只对最近一条入站消息有效。

agent 还通过平台的媒体通道直接发图/语音/视频/文件：本地大文件优先走
`upload_file_stream` 流式上传（NapCat ≥ 4.8.115），探测失败自动回退 base64
（媒体段）或 `file://` 直传（文件动作，需与 NapCat 同机可见）。

## 出站文本转换

QQ 侧可见的文本在「分段完成、组装成 text 段之前」会过一层最小清洗，规则全部
可配置、**默认全关**（关闭时输出与历史行为逐字节一致）：

| 配置（env）| 默认 | 作用 |
|---|---|---|
| `outbound_strip_tags`（`ONEBOT_STRIP_TAGS`）| `false` | 剥掉每行**行首**的 `§N§` 编号前缀（连其后至多一个空格）|
| `outbound_strip_prefixes`（`ONEBOT_STRIP_PREFIXES`）| 空 | 逗号分隔的**字面量**前缀，逐行行首剥（可叠加），同样吃掉其后至多一个空格 |

边界钉死，不做通用改写网关：

- **只锚定行首**——行中间出现同一串字面量绝不误伤（`看看 §12§ 这个` 原样保留）；
- **`§N§` 必须带定界符**——裸数字行首（`12 个苹果`、`2024 年`、`1. 第一项`）不会被吞；
- **配置只写字面量**——永不编译成正则，写 `(草稿)` 就只匹配 `(草稿)`；
- 只作用于 `send` / standalone（cron）投递的显示文本，不动入库、工具链路与
  分段边界；`onebot_call` 直通动作（`send_msg` 等）参数原样转发，不经转换。

```yaml
platforms:
  onebot:
    outbound_strip_tags: true                 # 打开 §N§ 剥离
    outbound_strip_prefixes: "[BOT],(草稿)"   # 额外的行首字面量前缀
```

典型场景：Hermes Context Engine 会把 `§N§` 回收编号当回复前缀原样持久化，
CLI/gateway 显示可以接受，QQ 聊天则需要打开 `outbound_strip_tags` 保持观感。

## 故障排查

**连不上（NapCat 一直重试拨入）**
- `hermes gateway status` 里平台显示未连接 → 看三方：端口、token、路径
- 端口不通：`ONEBOT_HOST` 绑定的是 `127.0.0.1` 而 NapCat 在别的机器（跨机时用 `0.0.0.0`）；
  防火墙/组网 ACL 拦了 8643
- token 不符：NapCat 侧 token 与 `ONEBOT_TOKEN` 必须逐字符一致；Hermes 日志会出现
  「拒绝未鉴权的 WS 拨入」
- 路径不符：NapCat URL 路径必须与 `ONEBOT_PATH`（默认 `/onebot/ws`）一致
- `hermes gateway status` 显示连接但 NapCat 侧报错：确认 NapCat 网络项类型是
  **WebSocket 客户端**（ws-reverse），不是服务端

**收到消息不回复**
- 私聊不回：查门控——默认 `allowlist` 且名单为空 = 全拒；gateway 日志里有带完整
  QQ 号的拒绝提示，照提示加白名单
- 群聊不回：确认被 @ 或回复的是机器人自己的消息；群必须在 `ONEBOT_ALLOWED_GROUPS` 里；
  群内还被 `ONEBOT_GROUP_ALLOWED_USERS` 收紧时确认发送者在名单里
- 只回一次/不重复回：同一 message_id 5 分钟内去重；机器人自己的消息回显一律忽略

**媒体解析失败**
- `get_image` 返回失败：OneBot 实现端依赖本地缓存/下载，跨机部署下确认 NapCat 侧
  能访问该资源的 url 或缓存文件仍在；换更高清重发即可
- 媒体标记 `[图片:m1]` 报 "media_id not found"：媒体 id 只对最近一条入站消息有效，
  跨消息引用请让发送者重发，或用 `get_group_msg_history` 找回
- 出站文件对方收不到（`file://` 兜底）：`upload_file_stream` 不可用（NapCat <
  4.8.115）且文件不在 NapCat 本机路径上；升级 NapCat 或改用 URL/base64 途径

**其他**
- 长回复被拆成多条：默认按句边界在 2000 字符处分段（`ONEBOT_MAX_MSG_CHARS` 可调），
  段间 300ms 间隔缓解个人号风控
- 个人号发送频率限制：QQ 风控由腾讯侧控制，避免高频刷屏；本插件不做自动限速（v2）

## 测试与开发

```bash
# 前置：把 hermes-agent 与本仓库 clone 到同级目录
#   hermes-projects/
#   ├── hermes-agent/            ← git clone https://github.com/NousResearch/hermes-agent
#   └── hermes-onebot-adapter/   ← 本仓库

# 单测（hermes 环境 + 插件目录；--no-sync 避免 hermes 依赖被重装）
cd hermes-onebot-adapter
PYTHONPATH=../hermes-agent uv run --project ../hermes-agent --no-sync \
    python -m pytest tests/ -q

# 静态检查
uv run --project ../hermes-agent ruff check .
```

测试用 fake NapCat（真实 aiohttp WS client）拨入插件起的真实 server 驱动全链路，
覆盖协议（echo/超时/鉴权/挤占）、段解析、入站触发、门控矩阵、出站媒体回退链、
standalone HTTP 投递与注册契约。

## License

MIT
