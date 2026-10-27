# WeChat Provider

**跨平台、本地优先的微信 MCP 能力提供者（macOS / Windows）。**

独立运行，无需安装 [Local Bridge](https://github.com/eaopen/local-bridge)；未来可作为普通 MCP Provider 由 Local Bridge 注册和调用。微信聊天数据、实际发送和 UI 操作均由本项目负责，Bridge 不内置微信业务逻辑。

## 当前交付（v0.1.0）

| 功能 | 当前状态 |
| --- | --- |
| MCP stdio 6 个工具 | ✅ 已实现 |
| 本地 SQLite / FTS5 文本索引 | ✅ 已实现；仅检索已读/导入的数据 |
| 发送任务记账 / 幂等键 / 单机 GUI 锁 | ✅ 已实现；RPA 调用在隔离子进程中，不污染 MCP stdio；不声称成功送达 |
| Windows 4.x GUI 适配 | 🔶 对接 [pyweixin](https://github.com/Hello-Mr-Crab/pywechat)；需实机验证 |
| macOS GUI 适配 | 🔶 对接 [wxauto-mac](https://github.com/PatrickHua/wxauto-mac)；需实机验证当前微信版本 |
| 微信原生 SQLCipher 数据库解密与增量导入 | ❌ 尚未实现，放在独立后续任务 |
| Local Bridge 直接自动注册 | ❌ 尚未实现；MCP Server 可单独启动 |

**重要：仓库提供可运行的软件骨架、SQLite 和 MCP 实现，但还没有完成真实微信账号的端到端发送验收。** 微信 4.x 的可访问性树可随账号/版本变化，不能假设任意机器可用。

## 安装

在 **Python 3.10+** 的 Windows 或 macOS 上运行：

```sh
git clone https://github.com/eaopen/wechat-provider.git
cd wechat-provider
python -m venv .venv
# 激活 .venv 后
pip install -e .
```

Windows 额外安装 **pyweixin**（上游包 pywechat127）：

```sh
pip install -e ".[windows]"
```

macOS 额外安装 **wxauto-mac**，且在系统设置中授权启动终端或 Agent 的辅助功能：

```sh
pip install -e ".[macos]"
```

以上两种后端均为**可选运行时依赖**，不复制/魔改上游项目。它们可能在部分微信 4.x 版本上失效，届时优先修复对应小型适配器，而非重写整个 Provider。

## 本地诊断及 MCP

```sh
wechat-provider status
wechat-provider serve
```

`serve` 使用 MCP **stdio** 传输，适用于 Codex、Claude Code 等支持外部本地 MCP 的客户端。请把 MCP 客户端配置为调用当前虚拟环境中的 `wechat-provider serve`，不是 HTTP 接口。没有后台驻留/开放监听端口的要求。

提供六个工具：

- `wechat_status`
- `wechat_list_chats(limit)`
- `wechat_get_messages(chat_id, limit)`
- `wechat_search(query, chat_id?, limit)`
- `wechat_send_message(chat_id, text, idempotency_key?)`
- `wechat_get_send_status(task_id)`

## 使用边界

- 当前的 `chat_id` 对应**界面显示名称**，不是微信不可变用户 ID。名称重复、模糊或群聊标题变化时可能选错对象；发给重要对象前要在界面核对。MVP 优先从文件传输助手/自己控制的测试对话进行人工验收。
- RPA 的返回 `submitted` 仅表示底层 UI 自动化方法无异常返回，**不表示验证了正确接收方或对方已收到**。异常但可能已经点击发送的任务标为 `uncertain`，不会盲目重发。
- 消息读取得自桌面 UI 可见范围，**不是全量历史**。获取 UI 消息后会索引文字；搜索只覆盖已读/导入的内容，结果带 `complete=false`。
- 当前不做自动群发、自动回复、批量好友操作、朋友圈及密钥内存扫描。未经明确授权不要把聊天数据发送给远程模型。
- 运行微信 UI RPA 需要本机登录、可交互图形桌面，以及当前微信版本的无障碍可访问性/屏幕状态。

## 离线 SQLite 导入（无密钥/无解密）

如果有通过其他**已获授权**的工具导出的规范化 JSONL，可以只导入自己需要的记录：

```sh
wechat-provider import-jsonl /path/to/my_messages.jsonl
```

每行示例（仅虚构数据；`source_id` 应尽量来自稳定原始消息 ID）：

```json
{"chat_id":"测试群","source_id":"example-001","text":"下周一起验收","sender":"甲","sent_at":"2026-10-27T10:00:00+08:00"}
```

本项目只写自己的 SQLite，不写微信自有数据库。默认位置：macOS 在用户 `~/Library/Application Support/wechat-provider/wechat.db`；Windows 在 `%LOCALAPPDATA%/wechat-provider/wechat.db`。可用 `WECHAT_PROVIDER_DB` 覆盖；`WECHAT_PROVIDER_ACCOUNT` 用于本机指定账号隔离（默认 `default`）。

## 技术选择与再利用

- Windows：直接调用上游维护的 `pyweixin.Messages` 会话列表、历史读取、发送方法。
- Mac：直接调用 `wxauto-mac.WeChat` 的 `GetSessionList`、`GetChatHistory`、`SendToFriend`。另参考 [wechat-macos-automation](https://github.com/BearyRadish/wechat-macos-automation) 的发送校验经验；不把设备专属的硬编码坐标当默认逻辑。
- 数据库读取的下一阶段参考 [robbin/wechat-exporter](https://github.com/robbin/wechat-exporter)、[Wechat-MCP](https://github.com/xerlxzx/Wechat-MCP) 及 Windows 的 [wechat-local-analytics](https://github.com/davids1896/wechat-local-analytics)；先验证不同微信版本的兼容性，再接入 **只读离线快照**，不直接嵌入未经确认的解密脚本。
- **不使用 OpenRPA** 作为默认引擎；复杂 Windows 通用业务工作流有实际需求时再评估独立适配。

见 [架构和完成条件](docs/ARCHITECTURE.md)、[Local Bridge 对接边界](docs/LOCAL_BRIDGE.md) 及 [开发要求](AGENTS.md)。

## 开发验证

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src
```

CI 只对 SQLite / MCP 无关核心 / 模拟 RPA 做跨平台验证，**不会声称真实微信测试通过**。

License: Apache-2.0. 第三方依赖遵循各自许可，不直接复制其源码。
