# Chat Copilot 读取契约与验收（2026-10-30）

## 已实现

- 六个 MCP 工具不变，list/get 增加 source 参数；默认 desktop_ui 保持兼容，local_index 明确只读自有索引，不调用 GUI。
- status 提供 read_contract_version=1、local_index_read、live_read_available、live_read_verified=false、send_verified=false。可尝试不等于已经验收。
- get 返回 chat_id、id_kind=display_name、source、complete=false、live_read；索引结果保留入库时间。实时失败降级有 cached_fallback 标记。
- macOS 当前在创建上游客户端之前阻断，blocked_reason=macos_login_safety_hold。不得以安装依赖或 MCP 可连接绕过。
- Bridge 仅按用户指定会话读取最多 20 条（桌面默认 10 条），展示来源和片段选择。选入输入框不会自动调用 Agent，也没有发送入口。

## 验证证据

16 项离线 unittest 与真实 stdio MCP smoke 通过；覆盖账号隔离、索引不碰适配器、Mac 初始化阻断。隔离临时索引中虚构一条消息，经真实 MCP → Bridge → WorkBuddy glm-5.3-flash/low，于 13.446 秒返回三档候选；live_wechat=false，不能视为实机读取成功。没有读私人聊天、启动或重启微信。

## 实机剩余步骤

Windows：需要在线且已登录微信的交互桌面。先调用 status 核对依赖，再指定本人控制测试会话执行一次 desktop_ui 读取；人工核对会话、双方发言、顺序和可见范围，验证返回来源确为 desktop_ui。标题/身份无法确认即停止。当前远程设备盘点无在线 Windows 设备，尚未实机验收。

macOS：当前版本 4.1.15，既往 AX 缺失并发生两次重登录。保持安全阻断；候选方案见 [源码评审](MAC_CANDIDATES_2026-10-30.md)。被动截图试验仍须单独同意，发送不在本次验收内。

实机读取通过后，再走“读取 → 用户勾选 → 生成建议 → 编辑复制”。发送需要另行指定收件人和精确内容并确认，不将 submitted 当 delivered，不自动重试。
