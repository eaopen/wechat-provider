# macOS 独立 Provider / Issue #9：无证书本机模式

状态（2026-11-03）：个人本机使用**不依赖 Apple Developer ID、Apple Development 或自签证书**。
A 路径（本机索引 / 已授权导出 / 原生分享导入）不需要 Helper、FDA 或证书；B 路径（可选直读已授权微信原库）使用固定安装的 ad-hoc 签名 Helper（`codesign -s -`，不是证书）。离线 IPC 与合成数据测试通过；真实 TCC 权限归属（C）、Bridge/Python 更新后权限保持（D）、Helper 更新后是否需要重授（E）均**未验收**，需要用户单独批准。Issue #9 保持打开。

## 1. 为什么不需要证书

- **A 路径（默认）**：`wechat-provider serve`（MCP stdio）或 `daemon` + `proxy` 只读 Provider 自有索引、用户已授权的规范化导出和微信合并转发 ZIP。不访问受保护的微信容器，因此不需要 FDA，也不需要任何代码签名证书。
- **B 路径（可选直读原库）**：TCC 授权对象是一个**固定安装一次**的最小 Swift Helper。默认以 ad-hoc 方式签名（`codesign --force --sign -`），不使用 Apple 证书、不需要 keychain 中的签名身份；这不是“自签证书”，只是本地代码身份。
- **证书模式仅作为未来分发可选项**：`--sign 'Developer ID Application: ...'` 保留给未来正式分发，不是本机开发或普通安装的前提。
- **不承诺永久授权**：Apple TN3127 明确 ad-hoc 代码身份随二进制版本变化。更新 Helper 后 macOS 可能要求重新授权（见 §6 E），文档不声称“永久 FDA”。
- 无论哪种模式，TCC 是否真的把权限归给 Provider 都需要真机验收（C），**编译成功和合成测试通过不等于 TCC 验收通过**。

## 2. 运行边界

```text
用户 LaunchAgent 或用户手动打开
  → 固定安装的 WeChat Provider.app（ad-hoc 或证书）
  → 固定 Python daemon（wechat_provider.cli daemon --socket ...）
  → 用户已授权的 Reader

Bridge（无需 FDA）
  → wechat-provider proxy --socket /absolute/private/provider.sock
  → 同一 daemon（只连接，不读取、不启动服务）

A 路径 MCP 客户端
  → wechat-provider serve（无 daemon、无 Helper、无 FDA）
```

- `proxy` 只连接已存在的同用户服务，不执行读取、不启动 daemon/Reader，失败时不回退为 Bridge 直接读库。
- Socket 父目录必须归当前用户所有且仅属主可访问（不满足则拒绝；daemon 新建时为 `0700`），socket 为 `0600`，拒绝符号链接祖先；双向用操作系统 peer UID 验证。同一 OS 用户是授权边界，不对同用户恶意进程提供额外隔离。
- IPC 只接受固定的 MCP 方法白名单（当前 10 个既有工具方法），参数绑定既有方法签名；**不接受命令、环境变量、数据库路径等覆盖参数**。每帧最多 1 MiB、60 秒超时，串行服务，不新增发送队列、不自动重试发送；进程崩溃后由独占锁保护清理旧 socket。
- Helper 只从外部 runtime 配置读取固定的 `python`、`socket` 和环境变量白名单，启动命令固定为 `python -m wechat_provider.cli daemon --socket ...`；IPC 请求无法改变它。

## 3. A 路径：无 FDA、无证书（默认，本机可用）

```sh
# 安装（Python 3.10+）
python3 -m venv .venv
. .venv/bin/activate
pip install -e .

# 本机诊断与 MCP stdio
wechat-provider status
wechat-provider serve

# 可选：同用户 daemon + 代理（Bridge 或其它 MCP 客户端）
wechat-provider daemon --socket "$HOME/Library/Application Support/wechat-provider/ipc/provider.sock"
wechat-provider proxy --socket "$HOME/Library/Application Support/wechat-provider/ipc/provider.sock"

# 导入用户已授权的规范化 JSONL 导出
wechat-provider import-jsonl /absolute/path/authorized_messages.jsonl
```

- `daemon` 的 socket 父目录需由当前用户创建并保持 `0700`；daemon 在缺失时会创建，但已有目录权限不符会拒绝启动。`serve` 是 MCP stdio，不创建 socket。
- 已有授权导出、明文一致快照、微信合并转发 ZIP（经一次性授权文件）和用户主动保存的片段都只写 Provider 自有 SQLite，不写微信数据库、不解密、不取钥。
- 未配置直读 Reader 时，`wechat_status` 中 `readers.local_index.available=true`，`readers.structured_db.available=false`；`capabilities.structured_db_read` 只表示“已配置且可执行”，`verified`/`tcc_verified` 始终为 `false`。

## 4. B 路径：可选的无证书 Helper 直读已授权原库

仅在用户确实需要直读当前微信数据库、且已自行授权 Wechat-MCP 等 Reader 时使用。**先构建到最终安装目录，再安装一次**；不要放进 Bridge 开发构建目录，也不要每次调试 Bridge 就重建 Provider。

### 4.1 固定安装一次

```sh
python3 macos/build_provider.py \
  --runtime /absolute/private/helper-runtime.json \
  --output "$HOME/Applications"
```

- 默认模式对 `WeChat Provider.app` 做 ad-hoc 签名（`codesign --force --sign -`），**不需要任何证书**。只有未来分发才需要 `--sign 'Developer ID Application: ...'`。
- 运行时配置**外置**：默认 `~/Library/Application Support/wechat-provider/helper/runtime.json`（目录 `0700`、文件 `0600`）。构建时把外部路径写入 Info.plist 的 `WCProviderRuntimePath`，Helper 启动时读取该路径（仅当 Info.plist 缺少该键时才回退默认路径；键存在但文件缺失/不安全时直接失败退出）。`--runtime-path /absolute/outside-bundle/runtime.json` 可覆盖外部位置，必须是绝对路径且位于 app bundle 之外。
- runtime JSON 示例（**不含密钥本身**；`python`、`socket` 必须是绝对路径，环境变量仅限白名单）：

```json
{
  "python": "/absolute/provider-venv/bin/python",
  "socket": "/Users/USER/Library/Application Support/wechat-provider/ipc/provider.sock",
  "environment": {
    "WECHAT_PROVIDER_READER": "wechat_mcp",
    "WECHAT_PROVIDER_WM_PYTHON": "/absolute/sdk-venv/bin/python",
    "WECHAT_PROVIDER_WM_STATE_DIR": "/absolute/private-state",
    "WECHAT_PROVIDER_WM_SOURCE_ID": "sha256:BOUND_SOURCE"
  }
}
```

- 构建脚本校验 runtime 内容：`python`/`socket` 必须是绝对路径、环境变量只允许固定白名单；外部配置目录归当前用户且收紧为 `0700`，配置文件以原子替换写入并设为 `0600`，拒绝符号链接。Helper 启动时再次校验该文件是常规文件、归当前用户所有、无 group/world 权限位。socket 绝对路径还需 ≤103 字节（macOS `sockaddr_un.sun_path` 限制）。
- 脚本只输出可审阅的 LaunchAgent plist（`com.eaopen.wechat-provider.plist`），打印 JSON summary，并明确**不安装、不启用、不申请权限、不修改 TCC**。ad-hoc 模式**不包含 `AssociatedBundleIdentifiers`**（ad-hoc 没有 Team ID；Apple ServiceManagement 要求 Team Identifier 匹配）。证书模式才可包含该键。在真实验收（C）完成前，不能假定 launchd 启动的 Helper 已被 macOS 关联为 TCC responsible process；可先由用户显式打开固定安装的 App 做小范围实测，自动启动仅在验证后接入。
- 安装/卸载 LaunchAgent 由用户审阅 plist 后手动执行（`launchctl bootstrap gui/$(id -u) /absolute/plist`；卸载用对应 `launchctl bootout`）。Helper 收到 SIGTERM/SIGINT 会终止其 daemon，不会操作微信进程或删除聊天数据。

### 4.2 已安装 App 的覆盖保护

若 `$HOME/Applications/WeChat Provider.app` 已存在，脚本**默认拒绝覆盖或原地重签**，必须显式 `--force`。`--force` 会先在输出目录的 staging 目录完成编译、签名和 runtime 校验，成功后才把旧 App 移到 `.previous` 备份并换入新 App；编译、签名或配置写入失败时自动恢复旧 App，旧 LaunchAgent/配置不会被指向一个缺失的 Helper。脚本会警告：新 Helper 的代码身份/CDHash 会变化，macOS 可能要求重新授权 TCC，**不承诺原有 FDA 保留**。因此：

- 只更新 Python/SDK/Reader 配置时，**不要重建 Helper**，改用 §4.3 的 `--configure-only`。
- 只有 Helper 本身需要升级时才使用 `--force`，并按 E 项重新验收权限：

  ```sh
  python3 macos/build_provider.py --runtime /absolute/private/helper-runtime.json \
    --output "$HOME/Applications" --force
  ```

- 由旧版脚本构建、仍把 `runtime.json` 内嵌在 bundle 里的 App，Info.plist 中没有 `WCProviderRuntimePath`；此时 `--configure-only` 会拒绝并提示需要重建一次。一次性迁移用 `--force` 重建后，后续配置更新即可只用 `--configure-only`。

### 4.3 更新 Python/SDK/Reader：只改外部 runtime，不重编 Helper

```sh
python3 macos/build_provider.py --configure-only \
  --runtime /absolute/private/helper-runtime.json \
  --output "$HOME/Applications"
```

- `--configure-only` 只校验并**原子替换外部 runtime.json**（`0600`，目录 `0700`），不编译、不签名、不修改 app bundle 内任何字节；若已安装 App 不存在则拒绝执行。它从已安装 App 的 Info.plist 读取 `WCProviderRuntimePath`，因此**不能借 `--runtime-path` 更换配置位置**（更换位置需要重建 Helper）。
- 输出 summary 中 `helper_rebuilt=false`、`helper_resigned=false`、`tcc_changed=false`，可用于确认没有重建/重签或改动 TCC。
- 因此更换 Python 虚拟环境路径、升级 Wechat-MCP SDK 或修改 Reader 环境变量时，**Helper 二进制和签名保持不变**，不需要重新授权 TCC。
- **Bridge 更新也不会重建 Helper**：Bridge 只注册固定代理命令 `wechat-provider proxy --socket /absolute/private/provider.sock`，通过同用户 socket 访问 Provider；Bridge 自身不需要、也不应申请微信数据库 FDA。
- 注意：这只保证“配置更新不改变 Helper 身份”。若 Helper 二进制本身被替换，仍按 E 项处理。

## 5. 状态语义

`wechat_status` 区分“已安装/已配置/实际读取成功”，但这些都不等于 TCC 归属验收：

- `readers.structured_db.available`：Reader 已配置且可执行；Reader status 的 `verified` 与 IPC status 的 `tcc_verified` 始终为 `false`。
- IPC 状态包含 `read_access=not_checked / verified / access_denied`：`verified` 仅表示该进程成功读取过结构化来源，**不等于 TCC 验收**；状态查询本身不打开微信数据库。
- 权限错误只返回固定代码（如 `source_access_denied`、`reader_unavailable`、`invalid_argument`、`provider_ipc_failed`），不回显路径、密钥、SQL 或聊天正文。
- 缺权限时不会自动改用其它来源、不会复制/解密微信数据库、不会提升权限。

## 6. 验收划分（A–E，不夸大）

| 项 | 内容 | 当前状态 |
| --- | --- | --- |
| A | 本机索引 / 已授权 JSONL 导出 / 合并转发 ZIP / 手动片段，经 `serve` 或 `daemon`+`proxy` 读取；零 FDA、零证书 | 离线合成数据测试通过；不声称真实数据全量或实时 |
| B | 固定 Helper + 同用户 Unix socket + MCP 代理的合成读取（虚构数据） | 离线单元测试通过；**不是** TCC 验收 |
| C | 真实 Mac 上独立 Helper/Provider 的 TCC 权限归属：撤销 Bridge FDA 后，Provider 只读授权范围成功 | **未进行**；需要用户单独批准 |
| D | 重编 Bridge / 升级 Python Provider 后仍可读、无需重复授权 | **未验收**；需在 C 之后实测 |
| E | 更新 Helper 二进制后是否保留权限 | **未验收**；按 TN3127，ad-hoc 身份变化可能要求重授，不承诺保留 |

C 项执行前必须获得用户单独批准，并保持微信登录不变。建议步骤（全部在用户批准下进行）：固定安装 App → 用户只授予 Provider 所需的最小范围（确需直读原库才考虑 FDA，且授权对象是 Provider 而不是 Bridge）→ 撤销 Bridge 的 FDA → 经 `proxy` 读取少量已授权消息 → 重编 Bridge 再读（D）→ 记录操作系统版本、`codesign -dr -` requirement、权限所有者与聚合结果。任何一步失败都停止，不扩大权限范围。

A/B 的离线复核（合成数据，不访问真实微信、不改 TCC）：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

## 7. 登录安全与禁止动作（P0）

仓库记录过两轮本地验证后微信要求重新登录，直接原因尚未查明。**未经针对性用户批准，禁止**：退出/重启/复制微信、`codesign --force` 重签微信、关闭 SIP、Hook/注入/调试微信、`tccutil reset`、编辑 Keychain 或登录数据、强制复制正在写入的数据库/WAL、重复权限开关试验。不要为跑测试或修复权限而重签微信或重新登录。

真实验收只做 Provider/Helper 自身的安装与授权，不操作微信进程和登录态；出现登录/会话异常立即终止该尝试。

## 8. Apple 参考

- [TN3127: Inside Code Signing: Requirements](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements)：ad-hoc 代码身份与 designated requirement 随版本变化。
- [TN3179: Understanding local network privacy](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy)：responsible code / LaunchAgent 关联原则；主题为本地网络隐私，不能当作 FDA 验收证据。
- [ServiceManagement: Updating helper executables](https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos)：`AssociatedBundleIdentifiers` 的 Team Identifier 前提。
- [Accessing files from the macOS App Sandbox](https://developer.apple.com/documentation/security/accessing-files-from-the-macos-app-sandbox)：优先评估用户选择文件的授权方式；本轮尚未实现 security-scoped bookmark。
