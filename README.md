# AstrBot Rocket.Chat 平台适配器

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.0-blue)](https://github.com/AstrBotDevs/AstrBot)

将 [Rocket.Chat](https://rocket.chat) 接入 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的消息平台适配器插件。

开发与排障知识沉淀见：[docs/DEVELOPMENT_KNOWLEDGE_BASE.md](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/docs/DEVELOPMENT_KNOWLEDGE_BASE.md)

---

## 功能特性

- ✅ **实时消息接收** — 基于 WebSocket（DDP 协议）订阅频道、私有群组、私信消息
- ✅ **消息发送** — 通过 REST API 发送文本、图片、语音、视频和普通文件
- ✅ **E2EE 消息闭环** — 按 Rocket.Chat 官方 E2EE 协议收发加密私聊/私有群组的文本与媒体消息
- ✅ **输入中提示** — 群聊/线程中 `@bot` 或回复 bot 消息后支持延迟 typing；私聊也支持延迟 typing
- ✅ **自动重连** — WebSocket 断线后自动重连，无需人工干预
- ✅ **动态订阅** — 机器人被加入新房间后自动订阅，无需重启
- ✅ **全局管理员** — 与 AstrBot 核心权限系统无缝集成
- ✅ **远程媒体转存** — 图片、语音、视频优先下载到本地后再统一上传，尽量避免外链防盗链或直链失效
- ✅ **Base64 媒体发送** — 支持将 `base64://` 媒体引用落地为临时文件后发送
- ✅ **E2EE 媒体闭环** — 加密私聊/私有群组中的图片、语音、视频和普通文件支持按官方协议上传、确认、接收和解密

---

## 环境要求

| 依赖 | 版本要求 |
|------|----------|
| Python | >= 3.10 |
| AstrBot | >= 4.0 |
| aiohttp | >= 3.9 |
| Rocket.Chat Server | >= 5.0（推荐 6.x）|

---

## 安装

### 方式一：通过 AstrBot WebUI 安装（推荐）

1. 打开 AstrBot WebUI，进入 **插件市场**
2. 搜索 `astrbot_plugin_rocket_chat_adapter`
3. 点击安装

### 方式二：手动安装

将本仓库克隆到 AstrBot 的插件目录（`data/plugins/`）：

```bash
cd data/plugins
git clone https://github.com/NET-Homeless/astrbot_plugin_rocket_chat_adapter
```

---

## 配置

### 第一步：准备 Rocket.Chat 机器人账号

1. 在 Rocket.Chat 管理界面创建一个专用的机器人账号
2. 为该账号分配 `bot` 角色（可选，用于自定义显示名和头像）
3. 确保机器人账号已加入需要监听的频道/群组

### 第二步：在 AstrBot WebUI 中添加平台实例

1. 打开 AstrBot WebUI，进入 **平台** 页面
2. 点击 **添加平台**，选择 `rocket_chat`
3. 填写配置项（见下表）

### 配置项说明

| 配置项 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `id` | string | 否 | `rocket_chat` | 适配器实例唯一标识，多实例时需区分 |
| `server_url` | string | ✅ | `http://localhost:3000` | Rocket.Chat 服务器地址（含协议，不含末尾 `/`）|
| `username` | string | ✅ | — | 机器人账号用户名 |
| `password` | string | ✅ | — | 机器人账号密码 |
| `reconnect_delay` | float | 否 | `5.0` | WebSocket 断线后重连等待秒数 |
| `typing_indicator_delay` | float | 否 | `0.5` | 输入中提示延迟秒数；若在该时间内已回复，则不会显示 typing |
| `remote_media_max_size` | int | 否 | `20971520` | 远端媒体下载大小上限（字节） |
| `enable_e2ee` | bool | 否 | `false` | 是否启用 Rocket.Chat 官方 E2EE 支持 |
| `e2ee_password` | string | 否 | — | Rocket.Chat E2EE 私钥密码；仅在 `enable_e2ee=true` 时使用 |

### 配置示例（JSON）

```json
{
  "id": "rocket_chat",
  "server_url": "https://chat.example.com",
  "username": "astrbot",
  "password": "your_bot_password",
  "reconnect_delay": 5.0,
  "typing_indicator_delay": 0.5,
  "remote_media_max_size": 20971520,
  "enable_e2ee": true,
  "e2ee_password": "your_e2ee_password"
}
```

### E2EE 支持边界

- 当前实现按 Rocket.Chat 官方源码兼容 **私信 (`d`)** 和 **私有群组 (`p`)** 的 E2EE 文本、图片、音频、视频和普通文件收发
- 普通未加密频道/私聊继续沿用原有明文链路，不受 E2EE 初始化失败影响
- 加密房间中的 **文本 / 引用回复 / 图片 / 音频 / 视频 / 文件上传** 走 E2EE（`/api/v1/rooms.media` + `/api/v1/rooms.mediaConfirm`）
- 加密房间中的媒体接收会自动按附件内 `encryption` 信息解密，再以 AstrBot 的普通 `Image/Record/Video/File` 组件形式进入事件流
- 如果 `requestSubscriptionKeys` 首次超时或房间密钥暂未同步到订阅，适配器会自动重试；只有重试耗尽后才会跳过该条加密消息
- 加密房间中的远程图片 / 语音 / 视频如果下载失败，会降级为一条加密文本消息，并附可点击的原文件链接
- 如果 E2EE 初始化失败或房间密钥不可用，加密房间消息会被安全跳过，不会影响未加密房间的正常收发

---

## 使用说明

### 触发 AstrBot 指令

默认情况下，AstrBot 处理以 `/` 开头的指令，或在唤醒词触发后的对话消息。
在 Rocket.Chat 中，直接发送对应消息即可：

```
/help          # 查看帮助
/ask 今天天气   # 使用 AI 对话
```

私信机器人账号时，无需唤醒词，所有消息均会被处理。

### 输入中提示（Typing）

- **群聊 / 线程**：在 `@bot` 或回复 bot 消息后启动延迟 typing
- **私聊**：只要用户给 bot 账号发消息，就会启动延迟 typing
- **线程支持**：线程内 typing 会通过 `extras.tmid` 归属到对应线程
- **延迟控制**：默认延迟 `0.5s`，如果 bot 在延迟时间内已经完成回复，则不会显示 typing
- **停止时机**：真正发送消息前会自动发送 `typing=false`

### 管理员权限

Rocket.Chat 适配器已接入 AstrBot 的全局权限管理系统。
请在 AstrBot 的全局配置（`config.json` 或 WebUI）的 **管理员 ID** 选项中，填入对应用户的 Rocket.Chat `User ID`（可在后台接收消息日志中的 `sender_id` 处找到该值），即可拥有管理员专属指令权限（如 `/plugin`、`/config` 等）。

---

## 技术架构

```
AstrBot 框架
    │
    ├── RocketChatAdapterPlugin (Star)          ← main.py，插件入口，触发注册
    │
    └── RocketChatAdapter (Platform)            ← rocketchat_adapter.py，主编排层
            ├── RocketChatRealtimeBridge        ← rocketchat_realtime.py
            │     ├── DDP connect / login
            │     ├── stream-room-messages
            │     ├── stream-notify-user
            │     └── DDP method result 分发
            │
            ├── RocketChatInboundBridge         ← rocketchat_inbound.py
            │     ├── E2EE 消息解密后的入站归一化
            │     ├── 引用消息递归解析
            │     └── AstrBotMessage / Event 组装
            │
            ├── RocketChatSenderBridge          ← rocketchat_sender.py
            │     ├── 文本 / 引用 / typing 发送
            │     └── 主动 send_by_session 消息链分发
            │
            ├── RocketChatMediaBridge           ← rocketchat_media.py
            │     ├── 普通房间 rooms.upload
            │     ├── E2EE 房间 rooms.media + mediaConfirm
            │     ├── 媒体下载 / 临时文件 / Base64 解码
            │     └── 加密房间远程媒体 fallback
            │
            └── RocketChatE2EEManager           ← rocketchat_e2ee.py
                  ├── 客户端密钥与房间密钥
                  ├── 文本消息加解密
                  └── 媒体元数据 / 文件内容加解密

RocketChatMessageEvent (AstrMessageEvent)      ← rocketchat_event.py
    └── send(MessageChain)
          ├── Plain / At / AtAll → 文本发送 / 引用回复 / 线程回复
          ├── Image / Record / Video → 统一转为可上传文件路径
          └── File → 本地上传或文本链接退化
```

### 消息流转图

```
Rocket.Chat 用户发消息
    │
    ▼
RocketChatRealtimeBridge.ws_listen_loop()
    │
    ▼
RocketChatInboundBridge.process_incoming_message()
    │  构造 AstrBotMessage + RocketChatMessageEvent
    ▼
commit_event(event)  →  AstrBot 事件队列
    │
    ▼
AstrBot 框架处理（指令匹配 / LLM 推理）
    │
    ▼
RocketChatMessageEvent.send()
    │
    ├── RocketChatSenderBridge.send_text / send_with_quote
    └── RocketChatMediaBridge.upload_plain_file / upload_encrypted_file
    ▼
Rocket.Chat 房间收到回复
```

---

## 严格支持目标（实现范围基线）

为避免“支持所有事件”的歧义，当前版本的严格支持目标限定为：

1. **消息闭环能力**：私信/群聊的文本、图片、普通文件（收/发）
2. **会话发送能力**：按 `room_id` 的 `send_by_session` 主动发送
3. **订阅连续性能力**：机器人加入新房间后的自动订阅

除上述基线外的事件类型（系统/审计/状态/交互类事件）默认视为非目标范围，除非后续版本单独声明支持。

## 事件支持矩阵（当前实现）

| 类别 | Rocket.Chat 能力 | AstrBot 映射 | 状态 | 说明 |
|------|------------------|--------------|------|------|
| 入站文本消息 | `stream-room-messages`（DM / Channel） | `FRIEND_MESSAGE` / `GROUP_MESSAGE` | ✅ 已实现 | 核心对话链路 |
| 入站图片消息 | `attachments` / `files` / `file` / `urls` 中可归一化图片 | `Image` 组件 | ✅ 已实现 | 已覆盖多种字段变体 |
| 入站普通文件 | `files` / `file` 中非图片/非音频/非视频附件 | `File` 组件 | ✅ 已实现 | 严格排除图片、语音、视频 |
| 入站语音消息 | `files` / `file` 中音频附件 | `Record` 组件 | ✅ 已实现 | 基于 MIME / 文件名 / URL 严格识别 |
| 入站视频消息 | `files` / `file` 中视频附件 | `Video` 组件 | ✅ 已实现 | 基于 MIME / 文件名 / URL 严格识别 |
| 房间订阅变更 | `stream-notify-user`（被加入新房间） | 动态订阅房间消息流 | ✅ 已实现 | 无需重启插件 |
| 出站文本回复 | 普通:`chat.postMessage` / E2EE:`chat.sendMessage` | `event.send` / `send_by_session` | ✅ 已实现 | 支持线程 `tmid` 和 Markdown 原生引用 |
| 出站图片回复 | 普通:`rooms.upload` / E2EE:`rooms.media + mediaConfirm` | `Image` 组件发送 | ✅ 已实现 | 统一转本地上传避免防盗链；加密房间远程下载失败时降级为加密文本链接 |
| 出站普通文件 | 普通:`rooms.upload` / E2EE:`rooms.media + mediaConfirm` | `File` 组件发送 | ✅ 已实现 | 本地文件上传；远端 URL 退化为文本链接 |
| 出站语音回复 | 普通:`rooms.upload` / E2EE:`rooms.media + mediaConfirm` | `Record` 组件发送 | ✅ 已实现 | 本地文件、HTTP(S)、Base64 均可上传；加密房间远程下载失败时降级为加密文本链接 |
| 出站视频回复 | 普通:`rooms.upload` / E2EE:`rooms.media + mediaConfirm` | `Video` 组件发送 | ✅ 已实现 | 本地文件、HTTP(S) 均可上传；加密房间远程下载失败时降级为加密文本链接 |
| 出站输入中状态 | `stream-notify-room` | typing 指示器 | ✅ 已实现 | 群聊/线程在 `@bot` 或回复 bot 消息时启用；私聊也支持；受 `typing_indicator_delay` 控制 |
| 系统/审计事件 | 加入/退出/改名/权限变化等 | 无统一映射 | ❌ 不支持 | 当前版本不建模为 AstrBot 事件 |
| 交互状态事件 | 编辑、撤回、反应、已读、在线状态 | 无统一映射 | ❌ 不支持 | 输入中已实现，其余状态仍不在当前适配器范围 |
| 其他富媒体语义 | 转发等 | 文本兜底或忽略 | ⚠️ 降级处理 | 非消息闭环基线的一部分 |

### 严格边界判定

- **严格支持（✅）**
  - 入站：文本消息、图片消息、普通文件消息、语音消息、视频消息
  - 出站：文本回复、图片回复、普通文件回复、语音回复、视频回复、输入中状态
  - 运行：房间订阅变更（动态订阅）
- **降级支持（⚠️）**
  - 转发等其他富媒体语义：仅文本兜底或忽略，不保证语义等价
- **不支持（❌）**
  - 系统/审计事件
  - 交互状态事件（编辑、撤回、反应、已读、在线状态等）

以上判定以当前实现为准：只有 ✅ 条目属于“严格支持目标”，⚠️/❌ 均不纳入严格支持承诺。

## 已知限制

| 限制 | 类型 | 说明 |
|------|------|------|
| Realtime API 已废弃 | 外部约束 | Rocket.Chat 官方将 DDP WebSocket 标注为 Deprecated；当前接收链路仍依赖该能力。 |
| 流式消息输出不可用 | 外部约束 | Rocket.Chat REST 发送形态限制，`support_streaming_message=False`。 |
| 其他富媒体语义仍为降级支持 | 功能边界 | 当前严格支持覆盖文本、图片、普通文件、语音、视频和输入中状态；转发等其余富媒体语义仍为兜底或未实现。 |
| 加密远程媒体依赖源地址可下载 | 行为约束 | 加密房间中的远程图片/语音/视频会先下载再上传；如果源地址不可下载，会降级为加密文本链接。 |
| 非消息型事件未覆盖 | 范围约束 | 编辑、撤回、反应、已读、在线状态等事件不在当前版本范围。 |

以上“支持范围”定义的是 AstrBot 与 Rocket.Chat 的稳定交集，不等同于两端全部事件能力。

---

## 常见问题

**Q: 机器人登录失败，提示 `REST 登录失败`？**

A: 请检查：
- `server_url` 是否正确，末尾不要加 `/`
- `username` 和 `password` 是否正确
- 如果服务器启用了双因素认证（2FA），需要先在账号设置中关闭 2FA

**Q: 机器人无法收到某个频道的消息？**

A: 请确保机器人账号已加入该频道（成为成员）。如果是运行中才被拉进新房间，适配器会通过 `stream-notify-user` 自动增量订阅，通常不需要重启。

**Q: 发送图片失败？**

A: 请确认机器人账号有上传文件的权限，管理员可在 Rocket.Chat 管理界面 → 权限 中确认。

**Q: WebSocket 频繁断线重连？**

A: 可能原因：
- 服务器网络不稳定
- `reconnect_delay` 设置过短，可适当增大（如改为 `15.0`）
- 服务器证书问题（HTTPS/WSS），检查 SSL 配置

**Q: 如何同时接入多个 Rocket.Chat 服务器？**

A: 在 AstrBot 平台配置中添加多个 `rocket_chat` 实例，每个实例设置不同的 `id` 即可。

---

## 开发与贡献

欢迎提交 Issue 和 Pull Request！

### 项目结构

```
astrbot_plugin_rocket_chat_adapter/
├── main.py                      # 插件入口，导入即注册
├── rocketchat_adapter.py        # 平台主适配器，负责配置、缓存、生命周期和编排
├── rocketchat_realtime.py       # DDP / WebSocket 握手、订阅、分发、动态订阅
├── rocketchat_inbound.py        # 入站消息解析、引用递归、唤醒判断、事件组装
├── rocketchat_sender.py         # 文本/引用/typing/消息链发送编排
├── rocketchat_media.py          # 普通与 E2EE 媒体收发、下载与 fallback
├── rocketchat_e2ee.py           # Rocket.Chat E2EE 协议与密钥管理
├── rocketchat_event.py          # AstrMessageEvent 实现（回复链路）
├── metadata.yaml                # 插件元数据
├── requirements.txt             # Python 依赖
├── docs/
│   └── DEVELOPMENT_KNOWLEDGE_BASE.md
└── README.md                    # 本文档
```

---

## 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 强大的 AI 聊天机器人框架
- [Rocket.Chat](https://rocket.chat) — 开源团队协作平台
- [aiohttp](https://github.com/aio-libs/aiohttp) — Python 异步 HTTP 客户端
