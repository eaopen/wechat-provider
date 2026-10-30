# macOS 独立 Provider / Issue #9

状态：IPC 与原生 Helper 已实现、合成索引 MCP 端到端通过；稳定签名安装及真实 TCC 迁移待验收。当前开发机没有有效签名身份，未修改 FDA 设置，也未关闭 Issue #9。

## 运行边界

用户 LaunchAgent → 独立签名 WeChat Provider.app → 固定 Python daemon → 已授权 Reader。
Bridge → `wechat-provider proxy --socket /absolute/private/path/provider.sock` → daemon。

代理只连接已存在服务，不执行读取、不启动服务、失败不回退到直接 Reader。原有 `serve` 独立 stdio 模式继续兼容，但不解决由 Bridge 启动时的权限归属。

Socket 父目录必须归当前用户所有、0700，socket 为0600，拒绝符号链接；双向使用操作系统 peer UID 验证。同一 OS 用户是授权边界，不对同用户恶意进程提供额外隔离。请求只有六个固定服务方法，参数绑定现有方法签名；不提供命令、环境、数据库路径参数。每帧最多1MiB、60秒超时；串行服务不新增发送队列、不自动重试发送。进程崩溃后由独占锁保护清理旧 socket。

## 构建与审阅

固定安装目录建议为用户 Applications 下的 `WeChat Provider.app`，不要放入 Bridge 开发构建目录。先在该最终目录构建，生成的 LaunchAgent 引用绝对路径。不要每次调试 Bridge 时重建 Provider。

建立私有 runtime JSON（不含密钥本身）：

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

```sh
python3 macos/build_provider.py --runtime /absolute/private/runtime.json \
  --output /Users/USER/Applications --sign 'Developer ID Application: YOUR IDENTITY'
```

脚本编译原生入口并用指定证书签整个 bundle，输出带 `AssociatedBundleIdentifiers` 的 LaunchAgent；拒绝 ad-hoc 签名，不安装、不启动、不申请权限。Python/SDK 应安装在固定、受用户控制的环境中。更改 runtime 需要重新签名；升级必须使用同一 Bundle ID、证书团队及兼容的 designated requirement。应对新旧版本分别执行 `codesign -dr -` 和 `codesign --verify --strict` 并比较，不以 Bundle ID 相同推断权限一定保留。

用户审阅后将生成的 plist 放入 `~/Library/LaunchAgents` 并使用 `launchctl bootstrap gui/UID /absolute/plist` 启用。卸载使用对应 `launchctl bootout`，不会操作微信进程或删除聊天数据。Helper 收到终止信号会终止其 daemon。

Bridge 仅注册固定代理可执行文件，参数 `proxy --socket /absolute/private/path/provider.sock`。若当前 Bridge 设置界面固定使用 `serve` 参数，可注册一个用户拥有的固定 launcher：仅 exec 安装好的 `wechat-provider proxy --socket ...`，忽略传入参数，不能启动 daemon。此 launcher 不需微信目录访问权限。

## 状态和验收

`wechat_status` 保留 Reader 的 installed/configured 字段。IPC 状态增加 `read_access=not_checked/verified/access_denied`；verified 仅表示该进程成功读取结构化来源，**不等于 TCC 归属验收**，`tcc_verified` 始终为 false。状态查询不主动打开数据库。权限错误只返回固定代码，不暴露路径、密钥和聊天正文。已有索引/快照需用户明确选择，不自动切换来源。

离线测试覆盖真实 Unix socket、peer UID 拒绝、方法/参数白名单、目录权限、代理 MCP 初始化与 local_index 查询；原生 Swift 入口编译通过。这些不证明 TCC responsible application 已独立。

真实验收需要用户单独批准：安装稳定签名 App，用户授权 Provider 所需最小范围；如确需 FDA 再授权 Provider。撤销 Bridge FDA 后，通过代理读少量已授权消息；重编 Bridge 再读；以相同签名身份升级 Provider 再读。记录操作系统版本、签名 requirement、权限所有者与聚合结果，保持微信登录不变。禁止重置 TCC、关闭 SIP 或重签微信。未通过前保持 PR 草稿及 Issue 打开。

Apple 参考：
- https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy （responsible code / LaunchAgent 关联原则；主题为本地网络隐私，不能直接当作 FDA 验收证据）
- https://developer.apple.com/documentation/security/accessing-files-from-the-macos-app-sandbox （优先评估用户选择文件的授权方式；本轮尚未实现 security-scoped bookmark）
