# 微信 4.2.0 本机真实只读连接

日期：2026-11-08，Asia/Shanghai。

## 已通过的实际链路

既有私有启动器 `~/.local/share/wechat-history-prep/wechat-provider-bound` → Provider MCP stdio → 独立 Reader 子进程 → 已保存密钥 → 当前微信加密数据库（包含 WAL）。没有使用 WeFlow，也没有用历史导入索引替代当前数据。

本机安装版本 **4.2.0，build 270350**。最初原版 SDK 实际调用返回 `unsupported_build`，发生在数据读取之前。

使用保存的密钥，以现有底层只读 SQLCipher Reader 独立核对全部 6 个必需数据库：联系人、会话、附件索引、消息资源索引、2 个消息分库均可打开，必需字段和行身份映射检查差异为 0。没有绕过原适配器版本检查冒充旧版本。

核对完成后增加精确版本/构建号兼容，保留密钥、表结构、账户身份检查及其他版本拒绝规则。SDK 只修改本机已有源码，未更新上游发布。

## 实际读取证据

- 两个消息分库共 **610960** 条；当前分库 **231818** 条、405 个消息表。
- 2026-11-08 当天截至检查时为 **177** 条；全库最新消息时间 **2026-11-08 11:29:03 +08:00**。
- 历史分库的最新记录仍为 2025-11-19，单独识别为历史数据，没有以此证明当前性。
- 真实 MCP `wechat_status` 识别 `wechat_mcp`；`wechat_list_chats(source="structured_db", limit=2)` 返回 2 个会话。
- 对实际存在近期消息的会话，`wechat_get_messages` 连续读取两页各 2 条；稳定 ID 无重复，样本最新时间 **2026-11-08 11:29:03 +08:00**。
- 两页结果为 `source_namespace=wechat_mcp`、`source_mode=liveEncrypted`，`coverage_complete=false`。MCP 使用临时空索引，读取后本地索引仍为空，确认没有从缓存补出样本或自动复制历史。
- 输出只包含聚合计数、时间及状态，没有记录聊天正文、联系人名称、会话 ID 或密钥。

## 修改与复现

本机 SDK：`~/.local/share/wechat-history-prep/Wechat-MCP-v0.1.2`。

SDK 本地分支 `fix/wechat-4.2.0-270350`，提交 `eb15bfd`，基于 `f6c99a6`；未推送上游。

1. SDK `adapters/wechat_4_1.py` 增加已核对的 `(4.2.0, 270350)`；其他版本/构建组合继续拒绝。新标识为 `wechat-4.1.15-4.2.0-270102-270135-270350-v3`。
2. Provider `wechat_mcp_worker.py` 精确接受原 v2 和本机 v3 标识。
3. 新增 SDK 测试覆盖当前组合、相邻 build、错误版本/build 配对及必需字段变化。

SDK 可审阅补丁保存在 [patches/wechat-mcp-4.2.0-270350.patch](patches/wechat-mcp-4.2.0-270350.patch)，只包含本轮兼容改动和新增测试，不包含已有用户测试改动。更换 SDK 安装目录前须审阅并应用补丁；不能只改 Provider 的版本字符串。

SDK 聚焦回归命令：

```sh
.venv/bin/python -m pytest -q tests/unit/test_schema.py \
  tests/unit/test_schema_420_local.py tests/integration/test_message_shards.py \
  tests/integration/test_contacts_groups.py
```

结果 **42 passed**；Provider 加入 SDK 版本白名单回归后 **51 项通过**，覆盖旧 v2、本机 v3 和未知版本在读取前拒绝。SDK 测试使用已有的本机 SQLCipher 路径配置，该既有配置改动不纳入本轮补丁。真实数据验证不在自动化测试中默认执行。

## 保留的边界

没有重启、重签名或修改 WeChat.app，没有重取密钥、修改 TCC/FDA/SIP、发送消息或全量导出。SDK 只更新自身的 inventory/schema 元数据，不写微信数据库。

此验收限于既有私有启动器的 Provider MCP 读取入口；当前聊天的工具集合没有因此自动注册新 MCP 工具。独立签名 Provider Helper 尚未运行，桌面端 TCC 归属、升级后权限保持、Companion 用户流程和发送均未验收，`tcc_verified=false`。

SDK 的归档、媒体、proof/取钥流程未扩展为 4.2.0 支持。读取保持不完整覆盖标记；其他新版本需另行核对，不能继续扩大白名单。

本轮代码和 SDK 补丁仅本地提交，未推送或发布。
