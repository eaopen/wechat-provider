# Integration with Local Bridge — 当前可用合同

WeChat Provider 是独立安装、独立运行的本机微信 MCP 服务。微信登录、桌面 GUI/RPA、SQLite/FTS5、本机发送任务和结果状态始终由本仓库管理；Local Bridge 只作为**可选 MCP Client** 连接已安装的 Provider。

## 当前已实现（2026-10-28）

- WeChat Provider 提供 `wechat-provider serve` stdio MCP 和六个工具，不依赖 Local Bridge。
- [Local Bridge PR #13](https://github.com/eaopen/local-bridge/pull/13) 已合并：用户可以在本机主动注册固定可执行程序及参数，再通过 MCP `initialize → tools/list → tools/call` 调用已公布的工具。
- [Local Bridge PR #14](https://github.com/eaopen/local-bridge/pull/14) 已合并：Ubuntu CI **实际安装并启动本仓库的 Python MCP Server**，由 Rust Local Bridge 完成状态、空索引搜索及拒绝未知工具的跨进程验证。
- **尚未实现**：自动扫描微信聊天、`messages.subscribe` 事件订阅、Bridge 自动代理全部工具给浏览器/AI、实际 Windows/macOS 微信 4.x GUI 读写验收或发送送达核验。

## 最小本机接入

先按照 [README](../README.md) 安装微信 Provider 和目标操作系统对应的可选依赖；`wechat-provider serve` 是由本仓提供的**独立 MCP Server**。

在安装 Local Bridge 的用户本机，找到真正的 `wechat-provider` 可执行程序绝对路径，手动注册：

```sh
local-bridge provider add wechat /absolute/path/to/wechat-provider serve
local-bridge provider list
local-bridge provider tools wechat
local-bridge provider call wechat wechat_status '{}'
local-bridge provider call wechat wechat_search '{"query":"待办","limit":10}'
```

如果已登录的微信 UI 可以提供相应读取，显式指定想读取的聊天：

```sh
local-bridge provider call wechat wechat_list_chats '{"limit":5}'
local-bridge provider call wechat wechat_get_messages '{"chat_id":"文件传输助手","limit":10}'
```

发送操作仅限用户明确从本机 CLI 发起，**不会因为注册而默认允许网页或 Agent 自动发送**：

```sh
local-bridge provider call wechat wechat_send_message '{"chat_id":"文件传输助手","text":"受控测试","idempotency_key":"test-local-20261028-01"}'
local-bridge provider call wechat wechat_get_send_status '{"task_id":"<task-id>"}'
```

当前 `chat_id` 是 UI **显示名称**，不是微信唯一且稳定的用户 ID。用受控的测试会话验收，发送前核对聊天对象；返回 `submitted` 只表示 UI 调用没有抛错，**绝不等于已送达**。遇到 `uncertain` 时查询原任务，不自动重复发送。该限制也适用于经 Local Bridge 代理的结果。

取消注册：`local-bridge provider remove wechat`。这不会删除原 Provider 的 SQLite、账号资料或安装目录。

## 所有权与非目标

| 所属模块 | 职责 |
|---|---|
| WeChat Provider | 登录中的微信桌面适配、聊天 UI、消息索引、发送状态/幂等、实际响应 |
| Local Bridge | 当前 OS 用户主动安装的 MCP 连接器注册、工具发现、调用转发和结果展示 |
| Local Tool Bridge Chrome 扩展 | 独立网页上下文采集、用户主动交接；**尚未**开放浏览器直接调用微信读取或发送 |
| CloudFile/Etech | 原企业登录、业务对象/文件权限和文件版本，不因微信 Provider 接入而改变 |

默认可以在同一电脑同时使用个人微信和企业文件工具，不建立新的个人/企业隔离墙；每个原模块仍执行已有认证、权限与操作结果判断。消息文本是数据，不能被当作命令自动执行。

不硬编码 Etech、CloudFile 或 Local Bridge 的私有程序/路径到 WeChat Provider 核心，不构建新的微信协议解密/RPA，不因为出现 Bridge 就要求用户迁移原有 MCP 客户端。

## 待验收的真实场景

- [Windows 微信 UI 验收 #1](https://github.com/eaopen/wechat-provider/issues/1)
- [macOS 微信 UI 验收 #2](https://github.com/eaopen/wechat-provider/issues/2)
- [只读离线快照导入 #3](https://github.com/eaopen/wechat-provider/issues/3)

MCP 传输验证成功 ≠ GUI 兼容 ≠ 微信发送核验成功。
