# AstrBot x Rocket.Chat 开发知识库

这份文档记录本适配器在接入、排障、补功能时已经验证过的实现结论，目标是避免后续每次都从零查 AstrBot 与 Rocket.Chat 的官方资料。

---

## 1. 适配器分层约定

当前仓库的职责划分已经比较清晰，后续扩展尽量沿用，不要再把逻辑揉回 `rocketchat_adapter.py` 单文件。

- [main.py](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/main.py)
  - AstrBot 插件入口，只负责注册平台
- [rocketchat_adapter.py](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/rocketchat_adapter.py)
  - 平台主适配器
  - 负责配置读取、缓存、生命周期、REST 辅助方法和各 bridge 编排
- [rocketchat_realtime.py](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/rocketchat_realtime.py)
  - DDP / WebSocket 桥接层
  - 负责 connect、resume login、房间订阅、动态订阅、DDP result 分发
- [rocketchat_inbound.py](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/rocketchat_inbound.py)
  - 入站消息桥接层
  - 负责消息解密后的归一化、引用递归、mention 唤醒判断、AstrBot 事件组装
- [rocketchat_sender.py](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/rocketchat_sender.py)
  - 出站发送桥接层
  - 负责文本、引用、typing、`send_by_session` 的消息链分发
- [rocketchat_event.py](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/rocketchat_event.py)
  - AstrBot 事件对象
  - 负责把 `MessageChain` 组件拆成文本、图片、文件、语音、视频，并调用 adapter/media 发出
- [rocketchat_e2ee.py](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/rocketchat_e2ee.py)
  - Rocket.Chat E2EE 协议实现
  - 负责客户端密钥、房间密钥、文本加解密、加密媒体消息体构造
- [rocketchat_media.py](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/rocketchat_media.py)
  - 普通房间与加密房间的媒体上传桥接层
  - 负责本地文件上传、远端媒体下载、本地临时文件管理、加密房间媒体 fallback

后续继续扩功能时的原则：

- 平台级协议和路由放 `adapter`
- DDP 握手、订阅、实时分发放 `realtime`
- 入站消息解析和 AstrBot 事件组装放 `inbound`
- 文本、引用、typing、消息链分发放 `sender`
- E2EE 协议细节放 `e2ee`
- 组件拆解与发送顺序放 `event`
- 上传、下载、媒体 fallback 放 `media`

---

## 2. AstrBot 侧已验证的约束

### 2.1 平台配置项类型

AstrBot 平台配置里的布尔值是支持开关控件的，`enable_e2ee` 应保持 `type: "bool"`。

结论：

- `enable_e2ee` 必须是布尔开关，不应做成文本输入框
- `password` / `e2ee_password` 虽然从产品角度更适合密码框，但当前 AstrBot 官方平台配置体系没有通用密码输入类型可直接复用
- 如果官方没支持，就不要在本仓库里私自做只对本地副本生效的 UI hack

### 2.2 AstrBot 发送模型

`RocketChatMessageEvent.send()` 是 AstrBot 回复链路的最后一跳，末尾必须执行 `await super().send(message)`，否则框架内部发送状态与统计可能不一致。

### 2.3 管理员标识

AstrBot 的管理员 ID 配置在 Rocket.Chat 侧应填写 `userId`，不是 `username`、不是显示名。

---

## 3. Rocket.Chat 接入总模型

当前适配器是典型的“两条链路并行”：

- REST API
  - 登录
  - 普通消息发送
  - 文件上传
  - E2EE 媒体上传确认
- DDP WebSocket
  - 实时接收消息
  - 房间订阅变更
  - typing 状态
  - E2EE `requestSubscriptionKeys`

实战结论：

- 只靠 REST 不能完成稳定的 Rocket.Chat Bot 对接
- 只靠 DDP 也不适合做文件上传与普通发消息
- 正确模型是 REST + DDP 混合

---

## 4. Rocket.Chat DDP 关键结论

### 4.1 最小握手流程

DDP 正常工作至少要走完这几步：

1. WebSocket `connect`
2. `login`，参数为 `{ "resume": authToken }`
3. 订阅 `stream-room-messages`
4. 订阅 `stream-notify-user`

如果是 Bot 账号：

- REST 登录先拿 `authToken`
- DDP 再用 `resume` 方式登录

### 4.2 动态订阅是必须做的

只在启动时拉一次 `subscriptions.get` 不够。机器人后续被拉进新房间时，必须监听 `stream-notify-user` 并增量订阅新房间，否则新房间里 bot 看起来“在线但没反应”。

### 4.3 DDP 调试要看 method result

像 typing 这种 `msg=method` 的调用，不能只看“我发出去了”，还要看服务端返回的 `result/error`。  
本适配器里 `send_typing()` 会通过统一 DDP call 等待 result，后续新增 DDP method 也建议沿用这个思路。

---

## 5. Typing 指示器知识

### 5.1 官方实现结论

官方参考不在 Electron 壳仓库，而在主仓库 Web 客户端。

关键文件：

- [Rocket.Chat `ComposerMessage.tsx`](https://github.com/RocketChat/Rocket.Chat/blob/master/apps/meteor/client/views/room/composer/ComposerMessage.tsx)
- [Rocket.Chat `UserAction.ts`](https://github.com/RocketChat/Rocket.Chat/blob/master/apps/meteor/app/ui/client/lib/UserAction.ts)

已验证结论：

- 官方不是“发一次 start typing 就不管了”
- 官方会在持续输入时不断触发 `start('typing')`
- 底层有节流和续期逻辑
- 官方 publish 的 payload 是四段，不是三段

### 5.2 当前适配器应对齐的行为

目前本仓库已经按官方行为对齐到这几个点：

- 使用 `stream-notify-room`
- 使用 `roomId/user-activity`
- 用户标识使用 bot 的 `username`
- 第 3 个参数为 `["user-typing"]` 或空数组
- 第 4 个参数为 `extras`；普通房间为 `{}`，线程 typing 必须带 `{"tmid": thread_id}`
- typing 开始后每 5 秒续期一次，直到真正回复前发送 stop
- 频道中除了显式 `@bot`，回复 bot 历史消息也会触发 AstrBot 回复，因此也应启动 typing

当前实现位置：

- [rocketchat_realtime.py](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/rocketchat_realtime.py)
- [rocketchat_sender.py](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/rocketchat_sender.py)
- [rocketchat_event.py](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/rocketchat_event.py)

### 5.3 Typing 不显示时先查什么

按优先级排：

1. Bot 是否已通过 DDP `resume` 登录成功
2. 房间 ID 是否正确
3. `send_typing()` 的 method result 是否有 `error`
4. payload 是否包含 `roomId/user-activity`、`username`、`["user-typing"]`、`extras`
5. 房间内其他用户客户端是否开启 typing indicator
6. Rocket.Chat 服务端是否对这类 method 做了限流或权限限制

---

## 6. E2EE 关键实现结论

### 6.1 支持边界

Rocket.Chat E2EE 实际只覆盖：

- 私信 `d`
- 私有群组 `p`

公开频道 `c` 不走这套端到端加密链路。

### 6.2 文本与媒体不是一条发送路径

E2EE 下文本和媒体必须分开看：

- 文本
  - 通过 `chat.sendMessage`
  - 消息里带 `t: "e2e"` 与加密后的 `content`
- 媒体
  - 先上传到 `rooms.media`
  - 再通过 `rooms.mediaConfirm` 确认消息

所以“文本能发”不等于“图片/语音/视频/文件也能发”。

### 6.3 房间密钥不能只赌第一次成功

`requestSubscriptionKeys` 首次超时在实战中确实会发生，不能把第一次失败当永久失败。

当前仓库已采用的恢复策略：

- `on_ws_ready()` 先请求一次
- 首次失败时再启动后台 `1s / 2s / 4s` 补偿重试
- `_ensure_room_key()` 会：
  1. 优先查缓存 key
  2. 强制刷新 subscription
  3. 优先导入 `E2ESuggestedKey`
  4. 其次处理 `E2EKey`
  5. 必要时做房间级轮询重试
- 如果最终仍失败，只跳过该条加密消息，不影响普通房间

### 6.4 E2EE 错误必须局部隔离

这是本适配器的重要约束：

- E2EE 初始化失败时，未加密房间必须继续可用
- 某个加密房间拿不到 key，不应拖垮其他房间
- 某条加密消息解不开，只跳过那一条

不要把 E2EE 错误冒泡成全局断线或平台不可用。

### 6.5 加密房间中的显式 @ 回复

在加密私有群组里，bot 回复时不能只依赖普通明文 `mentions` 语义，必要时要显式把 `@username` 拼进正文，并同步 `e2eMentions`，这样客户端展示效果才会和未加密房间一致。

---

## 7. 媒体处理知识

### 7.1 一律优先转本地文件再上传

无论普通房间还是加密房间，远端图片/语音/视频都优先下载到本地临时文件后再走统一上传链路。  
这样做的原因：

- 避免防盗链
- 避免 Rocket.Chat 直接拉外链失败
- 统一 MIME、文件名和附件结构

### 7.2 加密房间的远端媒体失败策略

加密房间里，远端图片/语音/视频如果下载失败，不再静默丢弃，而是降级为一条加密文本消息：

`远程媒体下载失败，原文件链接：<url>`

这样至少还能保住可点击链接，不会让用户误以为 bot 没回。

### 7.3 File 组件保持简单退化

`File` 的远端 URL 当前继续按文本链接发送，不强行做远端拉取上传。这个行为简单、稳、可预期，除非后面业务明确要求，不建议把它改成复杂分支。

### 7.4 媒体逻辑尽量收口到 `rocketchat_media.py`

如果后续再加：

- 新媒体类型
- 缩略图策略
- MIME 推断
- 文件名纠偏
- 下载大小限制

优先扩 `rocketchat_media.py`，不要在 `event.py` 里复制一份上传分支。

---

## 8. 回复与引用语义

当前适配器的回复语义分三种：

- 私聊
  - 直接回复
- 群聊 `@bot`
  - 使用引用回复格式
- 线程消息
  - 走 `tmid`

注意：

- AstrBot 的 `Reply` 组件不能原样透传给 Rocket.Chat
- Rocket.Chat 原生展示更适合通过链接引用或线程 `tmid` 来实现

---

## 9. 文档与实现要保持同步的点

以后每次改功能，至少同步检查这几处：

- [README.md](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/README.md) 的功能特性
- E2EE 支持边界
- 事件支持矩阵
- 已知限制
- `metadata.yaml` 的描述与版本

本仓库之前已经出现过“README 写 E2EE 只支持文本，但实现已经支持媒体”的情况，后续不要再让文档落后于代码。

---

## 10. 后续开发建议

优先级最高的补强方向仍然是测试，而不是再继续堆功能。

至少建议补这几类测试：

- E2EE 文本加解密单测
- 加密媒体 metadata/blob 加解密单测
- `subscriptions.get` 与房间 key 恢复流程单测
- typing method payload 单测
- 远端媒体下载失败 fallback 单测

如果没有测试，Rocket.Chat 这种“REST + DDP + E2EE + 媒体”四线并行的适配器很容易在小改动后出现回归。

---

## 11. 官方资料入口

后续再查资料，优先看这些官方入口：

- Rocket.Chat 主仓库
  - [https://github.com/RocketChat/Rocket.Chat](https://github.com/RocketChat/Rocket.Chat)
- Rocket.Chat 桌面壳仓库
  - [https://github.com/RocketChat/Rocket.Chat.Electron](https://github.com/RocketChat/Rocket.Chat.Electron)
- Rocket.Chat 开发者文档
  - [https://developer.rocket.chat](https://developer.rocket.chat)
- AstrBot 官方仓库
  - [https://github.com/AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)

查 Rocket.Chat 行为时的经验顺序：

1. 先看官方 Web 客户端源码怎么发
2. 再看服务端接口或 DDP method 约定
3. 最后才参考第三方帖子或模型回答

这样最省时间，也最不容易被过期资料带偏。
