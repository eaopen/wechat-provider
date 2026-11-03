# 微信合并转发 ZIP 原生导入（P0，合约 v1）

面向 Companion 的「微信合并转发导出 ZIP」导入能力。用户在 Companion 界面里**明确选择**一个
`.zip` 文件后，Companion 通过一次性授权文件把路径交给本 Provider；Provider 校验、解析并返回有界
预览，用户勾选范围后交给 Agent（仍由用户触发），只有用户再次点击「保存整份聊天到本机索引」时才会
写入 Provider 自有 SQLite。

**边界（必须如实理解）**：本实现只验证 WeChat **macOS 合并转发导出的 TXT 文本格式**；不读取微信
数据库、不接触登录状态/权限、不解密客户端数据、不发送任何消息。附件在 P0 只有元数据，不解包、
不落盘。合成 ZIP 只能证明管线可用，**不等于**真实微信分享已被验证。

合约原文：[跨仓契约 v1](NATIVE_SHARE_CONTRACT_V1.md)（与 Companion 仓库同一份）。

## 用户能力

| 能力 | 说明 |
| --- | --- |
| 预览 | 返回参与人、消息数、附件数、本地时间范围、覆盖度；消息分批（默认 200 条）有界返回 |
| 时区 | 只有显式传入 `timezone_offset_minutes` 时才产生带偏移的 `sent_at`（预览与保存均支持）；否则只保留本地时间文本并给出 warning，绝不假设系统时区 |
| 范围选择 | Companion 用 `sender：text` 勾选 ≤ 7800 UTF-8 字节，由 Companion 侧拦截，Provider 不改限制 |
| 保存（可选） | 用户显式点击后，整份导出（≤ 20000 条）幂等写入 Provider 既有 SQLite；可显式传时区以保留带偏移 `sent_at`；重复保存只计 duplicates |
| 附件 | 仅元数据：`{name, kind, bytes}`，最多返回 50 条；正文行与附件名/文件名完全相等时才记录 `attachment_refs` |
| 归档（保留） | `Service.archive_native_export` 为 P1.5 预留，P0 抛 `native_export_unsupported`，不暴露 MCP 工具 |

## 安全设计：只接受授权文件，不接受路径或字节

MCP 工具**从不接受**文件路径或文件字节，因此网页/Agent 无法借 Provider 读取任意本地文件。

```
Companion（用户明确选择 ZIP，前台动作）
  → 在自己的 app-data 目录暂存 ZIP（0600），路径只在内存
  → 向 Provider 数据目录写入一次性授权文件 <32hex>.json（0600，TTL 600s）
  → 调用 wechat_native_export_preview( grant_token, timezone_offset_minutes, offset, limit )
  → Provider 校验授权 → 删除授权（一次性，失败也删）→ 打开 ZIP → 解析
  → 返回有界预览 JSON（不持久化聊天内容）
用户点击保存：
  → 第二个授权 → wechat_native_export_save( grant_token, expected_fingerprint, timezone_offset_minutes )
  → Provider 重新解析并核对指纹（TOCTOU），再写入本机索引
```

- 授权目录：`default_db_path().parent()/native-export-grants/`，权限 `0700`，不接受符号链接；
  由 `wechat_status.native_export.grant_dir` 报告，Companion 校验其为 `$HOME` 下真实目录。
- 授权文件：文件名必须是 32 位小写十六进制 + `.json`；`version=1`、`token` 与文件名一致、
  `purpose ∈ preview|save` 与被调工具一致；文件必须是同 uid 的普通文件、无 group/other 权限、
  mtime 在 600s 内、≤ 2048 字节。
- `path` 必须是绝对路径、无 NUL、以 `.zip` 结尾、`lstat` 为同 uid 普通文件（非符号链接），
  大小 ≤ 24 MiB。
- 任何失败都删除授权（安全前提下）并抛 `native_export_grant_invalid`；Companion 重试会重新授权。
- Provider 不移动、不改名、不删除用户的原始 ZIP；拒绝时原文件保持原样。

## ZIP 校验（stdlib `zipfile`，先校验后解析，fail closed）

限制（任一超出即失败）：

| 限制 | 值 |
| --- | --- |
| ZIP 文件大小 | 25 165 824 字节（24 MiB） |
| 条目数 | 2 000 |
| 解压后总量 | 268 435 456 字节（256 MiB） |
| 单条目解压后 | 33 554 432 字节（32 MiB） |
| 压缩比（总解压/总压缩） | 200 |
| 路径长度 | 512 字节 |

同时拒绝：加密条目、非 0/8 压缩方法、ZIP64、多分卷、NFC+小写归一化后的重名、绝对路径、
`..`/`.`/空路径段、反斜杠、任一组件中的 `:`、控制字符、符号链接/硬链接/其他非普通文件类型、
非零大小的目录条目。所有条目都以有界读取读完，由 `zipfile` 校验 CRC（CRC 不符即拒绝）。

TXT 发现：优先 basename 精确等于 `聊天记录.txt` 的条目；否则在所有 ≤ 16 MiB 的 `.txt` 条目中
选择解析记录数最多者。找不到可解析 TXT → `native_export_parse_failed`（原文件保留）。

记录格式（微信 macOS 合并转发导出）：

```
·发送人显示名
2026年9月5日 08:05
正文第 1 行
正文第 2 行
```

- 可有 UTF-8 BOM；CRLF 归一为 LF；第一条记录必须位于 BOM/前导空白之后的行首，否则解析失败；
- 多行正文按原样保留（内部空行不折叠），仅在记录边界去除尾部空白；
- 发送人是显示名，**不是**微信稳定身份；相同显示名的不同群不会合并；
- 解析失败/格式异常不会被猜测补全。

## 身份与覆盖度（不伪造）

- `fingerprint = sha256:` + 64 hex，覆盖归一化后的记录（发送人 + 本地时间文本 + 正文，`\x1e` 分隔）；
  同一 ZIP 重复导入得到同一指纹 → 幂等。
- `session_id = native_share:` + 指纹前 32 hex，是**分享维度** id，不是微信稳定会话 id；
  同一聊天的两次不同导出是两个不同 session（绝不静默融合）。
- `source_id = native:` + 32 hex，包含「相同三元组的出现序号」，重复内容各得独立 id；
  重复导入记为 duplicates 而不是重复行。
- 预览带 `coverage`（`transcript_found` / `format` / `parsed_records` / `unparsed_tail_bytes` /
  `complete`），不虚构未解析内容的消息数。
- 保存响应固定 `live_read=false`、`complete=false`：本机索引不是完整历史、也不是实时微信读取。

## 时区

TXT 时间戳没有时区。预览与保存都通过显式 `timezone_offset_minutes` 决定是否产生绝对时间。

`wechat_native_export_preview(..., timezone_offset_minutes)`：

- 显式整数 `-840..840`：`sent_at` 为带显式偏移的 ISO-8601（如 `2026-09-05T08:05:00+08:00`），
  `sent_at_local` 为 `2026-09-05T08:05`，`export.timezone = {"applied": true, "offset_minutes": N}`。
- `null`/缺省：`sent_at = null`，保留 `sent_at_local`，`timezone.applied = false` 且带 warning；
  Provider 内部**绝不**假设系统时区。

P0 的 `wechat_native_export_save` 新增可选 `timezone_offset_minutes`，与预览使用同一校验
（整数 `-840..840`，否则 `invalid_argument`）：

- 显式传入：保存的行 `sent_at` 为带显式偏移的 ISO-8601（如 `2026-09-05T08:05:00+08:00`），
  `native_exports.timezone_offset_minutes = N`，保存回执 `"timezone": {"applied": true,
  "offset_minutes": N}`。
- `null`/缺省：保存的行 `sent_at = NULL`、`timezone_offset_minutes = NULL`，回执
  `"timezone": {"applied": false}`：不臆造绝对时间。

幂等规则：同一指纹已记录时，再次保存（无论是否传时区、传不同时区）一律返回
`imported = 0`、`duplicates = <该导出已记录的消息数>`、`already_imported = true`，**不重新插入**
任何行，也**不重写**已存 `sent_at`。因此第二次用不同时区保存既不会触发身份冲突，也不会改变已有
时间戳；此时回执的 `timezone` 报告该导出首次保存时记录的偏移（而非本次请求值）。

Companion 在预览与保存时传 `-new Date().getTimezoneOffset()`（两次使用同一偏移时，保存后的
`sent_at` 与预览中的绝对时间一致）。

## Provider 接口

MCP 工具（名称固定）：

- `wechat_native_export_preview(grant_token, timezone_offset_minutes=null, offset=0, limit=200)`
- `wechat_native_export_save(grant_token, expected_fingerprint, timezone_offset_minutes=null)`

IPC 白名单（`local_ipc.METHODS`）新增 `native_export_preview`、`native_export_save`；
既有 `save_excerpt`、`forget_saved_excerpts` 保持不变。

开发者/验收 CLI（与 MCP 同一套校验，产品路径仍由 Companion 授权）：

```sh
wechat-provider grant-native-export /path/to/chat.zip --purpose preview --grant-dir <dir>
wechat-provider preview-native-export <grant_token> [--timezone-offset-minutes 480] [--offset 0] [--limit 200]
wechat-provider save-native-export <grant_token> --fingerprint sha256:<64hex> [--timezone-offset-minutes 480]
```

`wechat_status` 新增：

```json
"capabilities": {"native_export_import": true, "native_export_contract_version": 1, "...": "..."},
"native_export": {
  "contract_version": 1,
  "enabled": true,
  "grant_dir": "/Users/<user>/Library/Application Support/wechat-provider/native-export-grants",
  "grant_ttl_seconds": 600,
  "max_archive_bytes": 25165824,
  "max_messages": 20000,
  "max_preview_messages": 200,
  "max_preview_bytes": 262144,
  "timezone_offset_required_for_absolute_time": true
}
```

`list_chats(source="local_index")` 对原生分享会话返回 `title = native_exports.label`、
`source_kind = "native_share"`、`native_share_only = true`；它绝不会被报告为
`structured_db` 或实时历史。混合来源显示 `source_kind = "mixed"`。

## 本机存储

只使用 Provider 既有的 SQLite（不新增第二个数据库）：

- `messages`：`chat_id = session_id`、`source_id`、`sender`、
  `sent_at = NULL`（保存未传时区）或带偏移 ISO-8601（保存传了时区）、`text`、
  `source = "native_share"`，沿用 `Store.import_messages` 单事务与幂等语义；
- `native_exports`（新表，同一文件）：`session_id` 主键、`fingerprint` 唯一、`label`、
  `message_count`、`participant_count`、`attachment_count`、`timezone_offset_minutes`、
  `source_zip_name`、`imported_at`；
- 附件在 P0 **不复制**进数据库（仅元数据）。

## 错误码

| 错误码 | 含义 / Companion 提示方向 |
| --- | --- |
| `native_export_grant_invalid` | 授权失效/被拒绝（过期、复用、权限、内容不符）；请重新选择 ZIP |
| `native_export_archive_invalid` | ZIP 校验失败（损坏、路径穿越、符号链接、加密、方法不支持、重名、CRC） |
| `native_export_limit_exceeded` | 体积/条目/压缩比/消息数/单条消息/路径等超限；请缩小导出范围 |
| `native_export_parse_failed` | 未找到可解析的合并转发文字；请确认是微信「合并转发」导出 |
| `native_export_changed` | 预览后 ZIP 发生变化（指纹不符）；重新预览后再保存 |
| `native_export_unsupported` | 不支持的格式（ZIP64 等）或保留操作（归档） |
| `invalid_argument` | 参数越界（limit/offset/时区/fingerprint 形状） |

错误信息只包含稳定错误码，不回显私有路径、聊天正文或后端细节。

## 明确限制

- 只验证 macOS 合并转发 TXT 格式；其他导出器/版本、Windows 分享入口不在 P0。
- 附件仅元数据（不提取、不建立笔记链接）；P0 不解压任何内容到磁盘。
- 不接触客户端 XML/加密数据；任何代码路径都不读取微信登录或数据库。
- 合成 ZIP 证明的是 Provider 管线与 MCP 传输，**不证明**真实微信分享被抓取/验证过。
- 保存不做密码学擦除保证；删除语义沿用 Provider SQLite 的普通删除。

## 本机验证

```sh
cd wechat-provider-native-share
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
```

覆盖：授权一次性/过期/权限/篡改/目的、ZIP 安全矩阵（穿越、符号链接、炸弹/压缩比、加密、
坏 CRC、方法、重名、ZIP64、多分卷、体积/条目/路径限制）、解析器（单聊/群聊/中英 emoji/多行/
重复消息/同名不同群/非法日期/BOM/CRLF）、时区显式与缺省、保存时区写入与重复保存不回写、
附件元数据与引用、预览窗口与字节上限、
保存幂等与 TOCTOU 指纹、`list_chats` 来源、IPC 白名单、CLI 三个命令，以及真实 MCP stdio
集成（合成 ZIP、grant→preview→save→local_index）。全部为合成数据，不访问真实微信。
