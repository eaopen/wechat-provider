# Wechat-MCP 已授权数据库 Reader

此可选 Reader 使用用户已有的 Wechat-MCP SDK、配置和逐数据库密钥，适用于不能直接使用 GreenBubbles 账户口令的来源。不执行取钥、重签名、登录、归档刷新、GUI 操作或发送。沿用现有六个 MCP 工具及 `source="structured_db"`。

## 配置

安装并自行授权 Wechat-MCP 后，在 Provider 进程环境中设置：

```sh
WECHAT_PROVIDER_READER=wechat_mcp
WECHAT_PROVIDER_WM_PYTHON=/absolute/path/to/Wechat-MCP/.venv/bin/python
WECHAT_PROVIDER_WM_STATE_DIR=/absolute/path/to/private/state
WECHAT_PROVIDER_WM_SOURCE_ID=sha256:YOUR_BOUND_SOURCE_ID
```

State 目录必须已有 `config.json`、`keys.json`、`identity.json`。密钥、配置和绑定信息仅保存在用户受限目录，不提交仓库。来源身份为 SDK 的 `inventory.account_id` 与 `config.state_dir.resolve()` 字符串连接后的 UTF-8 SHA-256，前缀为 `sha256:`；由本地 SDK 计算并明确绑定，响应身份不一致时拒绝读取。

当前适配器固定支持 `ADAPTER_VERSION=wechat-4.1.15-270102-270135-v2`，使用 SDK 内部读取接口；升级 SDK 后必须重新验证。未配置、版本不符、超时或失败均不自动改用其他读取来源。状态检查不打开微信数据库，`verified=false` 不因安装或配置完成而变为实机验收。

## 数据边界

- SDK 以只读方式读取加密数据库及 WAL；可能更新自己的 schema 元数据文件，不写微信数据库，不创建第二份聊天索引。
- 消息按时间、分库 ID、local_id 做跨分库 keyset 分页；游标绑定来源、操作、会话和搜索词。会话显示名称不作为稳定 ID。
- 搜索必须指定会话，每次最多检查合并后的 100 条候选记录；空结果也可能携带下一页游标。搜索词通过 stdin 传递。
- 多库读取不是原子快照，不支持的消息、无有效时间记录和辅助存储可能缺失；始终报告覆盖不完整，不承诺全量历史或实时订阅。
- 仅复用文本解析，不物化附件、不展开引用；发送仍由独立后端处理，本 Reader 不证明发送可用。

## 验证

单元测试使用虚构 SQLite 分库验证相同时间戳下的跨库分页、游标身份绑定、缺失配置及无副作用状态检查。真实本地验收应分别验证 Provider MCP 和 Bridge 读取入口，只记录条数、时间和状态，不在报告中输出聊天正文、联系人或密钥。读取验收不等同于 Agent 建议或真实发送验收。

## macOS 桌面应用权限

终端验证成功不代表桌面进程具有相同权限。桌面应用读取微信容器可能被 macOS 拒绝（EPERM）；SDK 的递归枚举原先会把它掩盖成零个数据库。本 Reader 先直接检查目录访问，返回 `source_access_denied`，由 Bridge 显示系统授权指引。用户需在系统设置的隐私与安全性中为实际运行的 Bridge 应用授权，再重启 Bridge；不要为此重新取钥、重签名微信或重新登录。
