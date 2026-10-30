# Mac 微信候选方案源码评审（2026-10-30）

结论：短期可见片段优先研究“指定窗口截图 + 本机 Vision OCR + 用户核对片段”；长期数据库读取另将 [GreenBubbles 列为重点候选](GREENBUBBLES_VALIDATION_2026-10-30.md)，已完成隔离虚构数据测试，真实取钥/读取未验收。这是一条待验证读取路径，不是现有 Mac 接入已成功。此次只审源码，未运行候选脚本或操作微信。

| 候选与审阅版本 | 可借鉴部分 | 不能直接接入的原因 |
| --- | --- | --- |
| [S7arFish/wxauto-mcp-mac](https://github.com/S7arFish/wxauto-mcp-mac/tree/9538325b3929a6c19d08889df920e9ba48352129) | 小型 MCP / AppleScript 边界 | 读取依赖 AX static text / group text，仍受当前缺失控件限制；主动激活、搜索切换；将每条消息 sender 都设为目标名，不能作为双方对话身份依据 |
| [dingliang/wx-gui](https://github.com/dingliang/wx-gui/tree/4c6d218509a72af1acbe459a036298d5d26c042c) | Vision OCR、标题识别、多行聚合和群消息分块 | 截图使用屏幕坐标 screencapture -R，遮挡可能读到其他应用；布局比例和阈值需实机验证；搜索和批量快照路径会激活/点击，不纳入首轮 |
| [BearyRadish/wechat-macos-automation](https://github.com/BearyRadish/wechat-macos-automation/tree/e9230e872df90b24bb8f006101a0e1f889c25c64) | wincap.swift 的 ScreenCaptureKit 指定窗口捕获思路 | 主要是发送自动化，不是结构化消息读取 Provider；窗口采用宽松子串匹配并选最大窗口，需严格身份校验；所审树未见 LICENSE，不直接复制源码 |

## 关键源码

- [AX 读取脚本](https://github.com/S7arFish/wxauto-mcp-mac/blob/9538325b3929a6c19d08889df920e9ba48352129/src/wxauto_mcp/scripts/wechat_read_last.scpt) 与 [MCP 调用](https://github.com/S7arFish/wxauto-mcp-mac/blob/9538325b3929a6c19d08889df920e9ba48352129/src/wxauto_mcp/server.py)。窗口存在不证明消息可读；搜索首项不等于身份校验。
- [wx-gui 驱动](https://github.com/dingliang/wx-gui/blob/4c6d218509a72af1acbe459a036298d5d26c042c/app/automation/drivers/accessibility_driver.py) 和 [Vision OCR](https://github.com/dingliang/wx-gui/blob/4c6d218509a72af1acbe459a036298d5d26c042c/app/automation/ocr/vision_ocr.py)。直接当前会话读取先检查运行/登录；搜索、切换和批量快照另有主动操作。不能将整个项目当成被动读取器。
- [窗口捕获](https://github.com/BearyRadish/wechat-macos-automation/blob/e9230e872df90b24bb8f006101a0e1f889c25c64/scripts/wincap.swift)。helper 本身没有激活/重启微信，但捕获权限、空白图、窗口选取和当前版本兼容仍需验证；作者使用报告不等于本机验收。

## 最小接入顺序

1. 独立编写/复用许可明确的小型截图组件：精确 bundle ID、PID、window ID；多窗口不猜选，不自动请求/重置权限，不激活窗口。
2. 用户单独同意后只做一次当前可见窗口的被动捕获；不切换、不滚动、不发送。空白图、标题不明确、登录/权限异常即停止，不改用另一库重复试。遵循 [登录保护](MACOS_LOGIN_SAFETY_2026-10-28.md)。
3. 本机 OCR 生成可见片段，标记来源、采集时间、置信度、范围不完整；发言人不确定就标未知，不推断稳定 ID。截图默认不长期保存，不写日志。
4. 先让用户预览并勾选片段，明确触发后才交给 Agent。OCR backend 通过 Provider 小型适配器实现，Bridge 不持有微信自动化逻辑。
5. Windows 并行保留 pyweixin 适配器，在线交互桌面可用后优先验收读取。发送单独验收，禁止自动重发。

两端都支持是目标；先用真实设备证明一端读取可用，不能用 MCP 成功或候选 README 替代。
