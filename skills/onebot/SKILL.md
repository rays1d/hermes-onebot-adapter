---
name: onebot
description: QQ（OneBot 11 / NapCat）平台的输出风格、onebot_call 动作目录、媒体标记解析、群聊礼仪与 home channel 投递。在 QQ 会话中工作，或需要操作 QQ（查消息/取媒体/群管理）时读本技能。
---

# QQ / OneBot 平台操作说明

## 平台能力概述

- 你正通过 **QQ**（OneBot 11 协议，NapCat 等实现端）与人对话：私聊与群聊都支持。
- QQ **不渲染 Markdown**：所有面向用户的回复一律纯文本——不用 `**加粗**`、`#`、`-`、反引号代码、表格、围栏块；链接写成 `文字 (URL)`。
- 回复简短、口语化；长回复会被自动按句边界分段发送，无需自己分页。
- 群聊里只有被 **@** 或被**回复**时才会触发你；入站群消息带发送者昵称前缀（`张三：\n内容`），注意区分说话人。
- 每条发出的消息都是真实 QQ 消息，请珍惜个人号的风控额度：不连续高频发送，不主动打扰。

## onebot_call：QQ 动作直通工具

单一工具 `onebot_call(action, params)` 代理所有**白名单内**的 OneBot 11 action，复用当前反向 WS 连接。

- 返回 JSON：成功 `{"success": true, "action": "...", "retcode": 0, "data": {...}}`；失败 `{"error": "..."} `（可能附 `retcode`/`raw`）。
- **白名单外的 action 一律被拒绝（fail-closed）**——不要猜测端点名，先查下面的目录。
- **管理类动作**仅当发起会话的 QQ 用户在 `ONEBOT_ADMIN_USERS` 名单内才放行；其他来源（CLI、cron 等非 QQ 会话）一律只读模式。

### 动作目录

| 分组 | 动作 |
|---|---|
| 读类/状态 | get_login_info, get_status, get_version_info, get_friend_list, get_group_list, get_group_info, get_group_member_info, get_group_member_list, get_group_honor_info, get_stranger_info, can_send_image, can_send_record |
| 消息 | get_msg, get_forward_msg, get_group_msg_history, get_friend_msg_history, mark_msg_as_read, delete_msg |
| 媒体 | get_image, get_record, get_file, get_image_by_id, ocr_image, download_file |
| 发送/回应 | send_msg, send_private_msg, send_group_msg, send_like（点赞名片）, set_msg_emoji_like（消息表情回应） |
| **管理类（需管理员）** | set_group_card（群名片）, set_group_kick（踢人）, set_group_ban（禁言）, set_group_whole_ban（全群禁言）, set_group_admin（设管理）, set_group_name（改群名）, set_group_leave（退群）, set_group_special_title（专属头衔）, set_group_add_request（处理入群申请）, set_friend_add_request（处理好友申请）, delete_friend, upload_group_file / upload_private_file（传文件）, set_qq_profile（改资料） |

### 常用示例

- 群历史：`{"action": "get_group_msg_history", "params": {"group_id": 123, "count": 20}}`
- 取图片：`{"action": "get_image", "params": {"media_id": "m1"}}`
- 发私聊文本：`{"action": "send_private_msg", "params": {"user_id": 10001, "message": "你好"}}`
- 点赞：`{"action": "send_like", "params": {"user_id": 10001, "times": 1}}`
- 撤回：`{"action": "delete_msg", "params": {"message_id": 12345}}`

## 入站媒体标记与解析

- 收到的图片/语音/视频/文件以内联标记呈现：`[图片:m1]`、`[语音:m1]`、`[视频:m1]`、`[文件:报告.pdf:m1]`。`m1` 是**本条消息内**的媒体编号。
- 需要看图/听语音/取文件时，用 `onebot_call` 把 `media_id` 传给对应 get 动作，工具会自动还原成实现端的 file 引用（内含 get_msg 兑底：实现端事件段没带 url/file 时会自动按消息号重取）：
  - 图片：`{"action": "get_image", "params": {"media_id": "m1"}}` → 成功后 `data` 里**直接带 `local_file`（本地缓存路径）与 `mime`**，可直接交给视觉工具识图，无需自己下载。
  - 语音：`{"action": "get_record", "params": {"media_id": "m1"}}`（默认服务端转 wav；可覆盖 `"out_format": "mp3"`）。
  - 文件：`{"action": "get_file", "params": {"media_id": "m1"}}`。
- 媒体 id 对**最近 10 条入站消息**有效（注册表环）；更早的标记会得到明确报错——此时用 get_msg / get_group_msg_history 按消息号重取。
- 历史类接口（get_group_msg_history 等）的回包里内嵌 base64 已被替换为省略标记，不要试图直接用它们；需要媒体时按消息号拿 url 或用 get 动作。
- 不需要处理媒体时**不要主动拉取**（跨机链路带宽有限，按需取用）。

## 出站发送

- **发媒体的首选方式**：在回复文本里写 `MEDIA:/绝对路径/文件名`（每行一个）。网关会拦截并路由为 QQ 的图片/语音/视频/文件消息；`get_image/get_record/get_file` 返回的 `local_file` 路径可以直接这样回发。
- 普通文本直接回复即可；长回复会被自动按句边界分段发送。
- 也可用 `onebot_call` 显式发送：`send_msg` / `send_private_msg` / `send_group_msg`，`message` 支持纯文本字符串或段数组，例如 `[{"type": "image", "data": {"file": "https://example.com/a.png"}}]`。
- 跨机部署（EasyTier 等内网）下，本地大文件出站会优先走 NapCat 的流式上传（≥ 4.8.115），失败自动回退 base64——无需关心细节。

## 群聊礼仪

- 被 @ 或被回复才发言；不主动刷屏、不拆很多条短消息刷屏。
- 一次回答把话说完；确需多条时保持连贯。
- **敏感操作先确认**：踢人、禁言、撤回他人消息、改群名等管理动作，即使你是管理员身份，也应先向用户确认再执行；执行后如实报告结果，失败不得谎报成功。
- 不主动私聊群成员、不加好友、不代发广告或拉票。

## home channel / cron 投递

- `ONEBOT_HOME_CHANNEL` 指定通知投递目标：**纯数字** = 私聊 QQ 号；`group:<群号>` = 群聊。
- 定时任务 `deliver=onebot` 经实现端的 OneBot HTTP API（`ONEBOT_HTTP_URL`）投递，与 WS 主链路独立；standalone 路径只发文本，媒体附件请放在 gateway 会话内发送。
