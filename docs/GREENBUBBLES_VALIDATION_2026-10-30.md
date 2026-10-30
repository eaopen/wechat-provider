# GreenBubbles 读取后端验证（2026-10-30）

## 结论

GreenBubbles 比单纯 GUI 自动化更接近长期历史读取需求，提升为 **Mac 数据库读取的重点候选**。它已有查询实现，不需要重新编写解密器；但不是当前日常账号可直接启用的默认后端。短期仍保留手动输入/已有索引，有条件的 OCR 只负责当前可见片段。

本轮完成源码审阅与虚构数据执行验证。没有运行 acquire、doctor/discover、真实数据查询或初始化；没有提权、附加调试器、复制/修改/重签名/重启微信，也未检查或输出真实密钥。

## 固定版本及测试

审阅 [bojieli/greenbubbles @ 69f19c7](https://github.com/bojieli/greenbubbles/tree/69f19c7089d7ef011e9ba43d44c6fb4e6e5be98b)，Cargo 包 0.10.0 / MIT。Rust 查询组件引用 wx-cli 的固定提交 2abe708f55bfe135539a385df856fdc58f97fc74，不能把整个工程理解为无外部依赖的单文件 SDK。

在临时源码目录用 Rust 1.90、上游 Cargo.lock 编译；仅运行 Rust 查询/附件测试，测试进程 HOME 指向临时目录并移除 query-profile 环境覆盖。测试数据库和测试密钥全部人工生成。

| 验证 | 结果 | 证明范围 |
| --- | --- | --- |
| live_query_cli | 14 通过、1 忽略，80.95 秒 | 明文/加密库、联系人、JSON envelope、分页/游标绑定、身份绑定、错误密钥、查询范围限制、profile 和快照凭据 |
| live_attachment_cli | 7 通过，1.46 秒 | 附件关联、检查无落盘、按需生成单个私有文件、拒绝路径/身份不匹配与超限 |
| 本轮独立补充检查 | 1 通过，27.97 秒 | 加密虚构库每页 1 条完整翻页，ID 不重复，crossDatabaseAtomic=false；读取前后递归比较文件集合及每个文件全部字节一致 |

合计 **22 项通过，1 项性能基准未运行**。时间是 debug 测试用例总时长，含造库/密钥派生/多次进程启动，不能当成真实单次查询延迟。字节不变证明仅限此次非并发虚构库，不代表生产 WAL/SHM 行为或登录安全已验收。

编译入口：

```sh
cargo +1.90.0 test --manifest-path Native/GreenBubbles/Cargo.toml --test live_query_cli --no-run --locked
cargo +1.90.0 test --manifest-path Native/GreenBubbles/Cargo.toml --test live_attachment_cli --no-run --locked
```

随后在隔离 HOME 下执行编译后的对应测试二进制。补充检查只加在临时检出源码，未修改上游或安装全局工具。本轮未运行任何个人记忆、AI 总结、发送或全量导出路径。

## 源码确认的能力与边界

- [live_query.rs](https://github.com/bojieli/greenbubbles/blob/69f19c7089d7ef011e9ba43d44c6fb4e6e5be98b/Native/GreenBubbles/src/live_query.rs)：打开只读连接并验证 PRAGMA query_only=ON；快照连接使用 SQLITE_OPEN_READ_ONLY。查询有条数、响应大小和语句超时限制。
- 分页返回 schema/formatVersion、source identity、page.hasMore/nextCursor、consistency 和 warnings。消息 ID 与来源/会话绑定；联系人 ID 和显示名分开，不依赖 GUI 搜索首项。
- 跨库、跨页不是一个原子时点；本机未同步的数据不可见。coverageComplete 不等于手机/云端完整历史。使用原生 FTS 失败后的受限回退也不能冒充全库搜索。
- 附件 inspect 与 materialize 要分开：后者会解密/转换并产生本地文件。Provider 首版只取文本和必要元数据，不自动物化附件。
- 默认配置可能自动发现最近活跃账号；Provider 集成必须显式绑定本地 profile/账号与 source identity，不能让 Agent 提供任意路径，不能根据最近修改时间猜账号。
- 当前仍是研究 alpha，仅发布 Apple silicon / macOS 版本；不是 Windows 后端，也不能解决真实发送。

## 初始化核对：不是无副作用的只读安装

[官方取钥文档](https://github.com/bojieli/greenbubbles/blob/69f19c7089d7ef011e9ba43d44c6fb4e6e5be98b/docs/PASSPHRASE_ACQUISITION.md) 明确要求管理员权限、对安装的 WeChat.app 重签名、重启、退出账号再登录；Swift 取钥组件生成 LLDB attach/breakpoint/memory-read 脚本。查询组件可独立使用已授权密钥/明文库/快照，不需要每次查询再取钥。

文档的真实取钥证据仅覆盖 4.1.12 / 4.1.13；工具不拒绝其他版本，不代表 4.1.15 已验证。当前设备的既往记录是 4.1.15 build 270135，不能外推兼容。

[Wechat-MCP](https://github.com/xerlxzx/Wechat-MCP) 当前 README 声明覆盖 macOS 27 / WeChat 4.1.15 builds 270102、270135；其流程是签名临时客户端副本、要求退出原客户端、在调试器下运行副本后恢复，可能手机确认登录。它不修改安装的客户端，但仍不是无登录干扰；版本匹配是作者声明，本轮未复现。不可与 BiboyQG 同名 AX 项目混淆。

[wechat-exporter](https://github.com/robbin/wechat-exporter) 当前是技能和实现规范，不附带现成解密器或 MCP；不优先选择从零重建其链路。

## Provider 接入差距与顺序

1. 保持取钥/初始化独立于 Provider、Bridge 和 MCP 工具；任何查询都不能自动取钥、提权、重签名或重试登录。缺凭据明确返回未就绪。
2. 新增可选 GreenBubbles 查询适配器，只白名单固定的会话/消息查询命令，用显式本地 profile、--json 和有界 limit。密钥由已有私有凭据文件管理，不进入 Bridge、参数或日志。
3. 契约需要新增独立数据库来源，保留 source identity、opaque message ID、稳定会话 ID、显示名、采集时间、hasMore/nextCursor、warnings。当前只支持 desktop_ui/local_index 且标 display_name，不能把 GreenBubbles 结果伪装为 desktop_ui。
4. 当前 SQLite 依据导入顺序返回消息，而 GreenBubbles 默认最新在前；需要显式统一时间顺序、账号与 source ID 去重规则。分页游标不能当永久消息 ID；只导入用户选定片段，不自动全量复制。
5. 先用明文虚构库/授权快照完成 Provider→Bridge 适配验收，再在隔离测试设备或已有有效密钥的明确账号验证本机数据；核对双方发言、时间、会话与分页，不把 mock 或上游测试当实机成功。

本轮没有新增可用的数据库适配器，产品仍不能宣称已接通 GreenBubbles。真实初始化受 [登录保护](MACOS_LOGIN_SAFETY_2026-10-28.md) 约束，其明确规定“未经针对性、明确的用户批准”不得重签名、重启或调试附加。当前泛指“再做验证”不覆盖这些会影响日常微信的步骤。
