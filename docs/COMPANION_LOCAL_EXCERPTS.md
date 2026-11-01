# Companion 显式本机片段（v0.1.1）

第一次使用时 Provider 索引为空，新增两个用户主动调用的 MCP 工具，让用户建立私有常聊会话，而不是破解微信数据库。

| 工具 | 作用 | 副作用 |
| --- | --- | --- |
| wechat_save_excerpt(chat_title, text) | 用户明确选定文字保存到本机 SQLite 索引 | 本机写入；不能当作实时微信历史 |
| wechat_forget_saved_excerpts(chat_title) | 移除此标签下所有手动保存的片段 | 只删 manual_excerpt，不动其他导入 |

- chat_title 是用户自定义显示标签，非微信稳定 ID；空值/NUL/超过 200 UTF-8 字节拒绝。
- text 是用户选中的片段，空值/NUL/超过 7800 UTF-8 字节拒绝。文本可能有多条发言；Provider 不猜测真实发言人和时间。
- 同一标签+相同正文以 SHA-256 标识重复，重复点击不会产生多份。响应只含导入/重复条数等回执。
- wechat_status.capabilities.manual_excerpt_save=true 是可选能力，不影响旧 6 工具。
- local_index 读取带 source=manual_excerpt、complete=false、live_read=false，不能冒充完整实时历史。
- 不点击保存时，原 Companion 粘贴/Agent 流程不会写入 Provider 索引。
- 显式删除只做 SQLite 普通删除，不保证底层存储块的密码学安全擦除。
- 完全不打开微信窗口、访问微信私有数据库、重启微信或请求系统读取权限。

## 责任划分

Companion 负责显式点击与隐私告知；Local Bridge 仅通用转发 MCP 工具；WeChat Provider 验证参数、维护本机索引。任何自主调用 MCP 的其他客户端仍须由自身获取用户许可。

## 本机验证

独立 Provider 单元测试覆盖参数限制、账号隔离、重复写入、删除保护，均使用临时 SQLite。
Companion / Local Bridge / Provider 使用隔离目录与虚构消息完成真实 MCP 流程：状态、保存、重复、列会话、读取、清理。没有访问真实微信索引、正文、权限或登录状态。

## 2026-11-02 修复（本地索引契约）

- **NUL 校验此前实际未生效**：`save_excerpt` / `forget_saved_excerpts` 比较的是两字符转义 `"\\0"`，真实 NUL（`\x00`）会通过校验。现已改为真实 NUL 并补充回归测试；Companion 仍会在调用前拦截，但直接调用 MCP 的客户端不再有缺口。
- **桌面读取不可用时不再返回空会话列表**：`list_chats(source="desktop_ui")` 遇到 `AdapterUnavailable` 现在抛错，而不是 `chats: []` 加 error 字段。避免调用方把“读不到”误读成“没有会话”，符合“不伪造覆盖范围”的原则。
- **本机索引列表增加来源信息**：`list_chats(source="local_index")` 每行新增 `manual_excerpt_only` 与 `message_count`，便于 Companion 把“仅用户手动保存的片段”与混合导入的历史区分显示；该字段只是标注，不改变读取范围。
- 未新增任何微信读取能力：结构化 Reader 未配置、macOS GUI 读取仍保持安全暂停；不触碰登录状态、不重启/重签微信。

## 2026-11-02 补充修复（IPC 边界与错误码）

- **IPC 帧上限修正 off-by-one**：`receive()` 现在拒绝超过 `MAX_FRAME` 的帧（恰好 1 MiB 仍可接收），不再接受 1 MiB + 1 字节。
- **稳定的非反光错误码**：IPC 只回传 `source_access_denied`、`invalid_argument`、`reader_unavailable`、`provider_ipc_failed` 四个码，不回传路径、SQL、正文或异常细节；Companion 据此给出可操作中文提示（参数限制 / 来源暂不可用 / 权限拒绝）。
- 本轮仍不新增任何微信读取能力，也不改变 Provider 的隐私边界。
