# WeFlow / ChatLab 本地开发验证

验证日期：2026-11-08。仅修改 WeChat Provider；Local Bridge、Companion 无需修改。

## 已实现范围

- ChatLab 0.0.2 JSON 导入；JSONL 每行必须是一个完整 ChatLab 页面文档，不是单条消息。
- 可选 `weflow` Reader，通过现有 MCP `source="structured_db"` 列会话、分页读取文本。
- 显式 CLI `sync-weflow`，每页消息、会话身份与续传位置在同一个 SQLite 事务提交。
- 来源身份 + 原始会话 ID 生成独立索引 ID；显示名只作标题。同名会话、不同来源及 Provider 账号互不混淆。
- 文本消息采用原始稳定消息 ID；正文或身份冲突整页回滚。没有稳定 ID 的文本拒绝整批，不用正文/时间哈希冒充身份。
- 导入后可用现有 `local_index` 读取和全文检索；结果仍为 `complete=false`。导入的稳定会话 ID 不可作为 GUI 发送目标。

本轮没有接入 SSE、媒体下载/附件归档、联系人归档、关系记忆或后台自动同步。非文本返回 `skipped_non_text`；不能把导入成功理解为完整聊天备份。成员显示名暂不入库，文本保留稳定 sender ID，避免昵称变更造成消息冲突。

## 离线导入

先将 `WECHAT_PROVIDER_DB` 指向自己的测试数据库，避免把试验样本混入日常索引。

```sh
export WECHAT_PROVIDER_DB=/absolute/path/to/test-index.db
export WECHAT_PROVIDER_ACCOUNT=synthetic-validation

wechat-provider import-chatlab /absolute/path/to/authorized-chatlab.json \
  --source-id own-export-test \
  --session-id fixture-group@chatroom \
  --owner-id fixture-owner
```

`--source-id` 是用户明确绑定的来源身份。同一个账号的数据在离线导入和 API 同步时使用同一来源身份，即可按原始消息 ID 去重；不同导出来源应使用不同身份。私聊格式没有可靠的对方会话 ID，所以 `--session-id` 必须显式提供；群聊还校验 `meta.groupId`。文档须包含 `meta.ownerId`；传 `--owner-id` 时必须匹配，后续同一来源/会话不得换号。

每批最多 8 MiB / 10000 条，全部校验后单事务提交。时间戳按秒处理，统一为 UTC。只支持 `type=0` 文本；缺字段、版本不支持、时间错误和身份冲突均不回显正文。

## 可选本机 API

仅在用户已经独立配置好 WeFlow 数据访问和 API 时使用。适配器不负责安装、启动 WeFlow，不获取数据库密钥，不触发微信 GUI 或权限操作。

以下均为虚构占位值；真实 Token 通过本地环境注入，不写入 Git、命令参数、URL 或浏览器：

```sh
export WECHAT_PROVIDER_READER=weflow
export WECHAT_PROVIDER_WEFLOW_URL=http://127.0.0.1:5031
export WECHAT_PROVIDER_WEFLOW_SOURCE_ID=own-export-test
export WECHAT_PROVIDER_WEFLOW_OWNER_ID=fixture-owner
# 在本地私密环境中配置 WECHAT_PROVIDER_WEFLOW_TOKEN，勿把真实值放入分享的脚本。

wechat-provider status
wechat-provider sync-weflow --session-id fixture-group@chatroom --page-size 100 --max-pages 10
wechat-provider serve
```

`status` 只检查配置，`available=true` 不证明 API 可达或账号已验收，始终 `verified=false`。

限制：仅允许显式端口的 `http://127.0.0.1` 或 `http://[::1]` 根地址；不解析 `localhost`，不接受远端、用户信息、查询参数或自定义路径。Token 只放 Authorization Header；不使用环境 HTTP 代理，不跟随重定向。每响应最多 8 MiB，HTTP socket 超时 15 秒。仅构造以下两个固定 GET 路由，没有任意 API 代理：

- `/api/v1/sessions?format=chatlab&limit=...`
- `/api/v1/sessions/{encoded-id}/messages?since=...&end=...&offset=...&limit=...`

读取和同步均最多每页 100 条；每次显式同步 1–100 页。读取不会自动入库。API 原生会话列表没有分页游标，因此会话结果不承诺全量；直接 `structured_db` 搜索暂不支持，需显式同步后使用 `local_index`。

### 分页与续传

源码核对：[官方 HTTP API 文档](https://github.com/hicccc77/WeFlow/blob/main/docs/HTTP-API.md)、[HTTP 服务实现](https://github.com/hicccc77/WeFlow/blob/main/electron/services/httpService.ts)。本实现独立编写，不复制其源码或引入其原生组件。

当前上游 `handlePullMessages` 将 `since/end` 作为过滤条件、`offset` 作为偏移，同时返回最后一条消息的时间作为 `nextSince`，累计偏移作为 `nextOffset`。改变时间过滤条件后继续使用累计偏移有跳项风险，因此适配器固定本轮 `since/end`，只推进累计 `offset`，不同时套用两个建议字段。它校验页条数、时间范围、`hasMore` 和偏移进度；空页却声称仍有下一页时拒绝提交。

每页提交后可停止进程，下次同一命令从 SQLite 中的 checkpoint 继续。请求失败时保留已提交页，失败页不推进；并发任务的旧 checkpoint 会被拒绝。重放相同消息计入 `duplicates`。

一轮时间窗口取尽后，下次从上一窗口上界前一秒重新拉取，覆盖边界同秒消息并幂等去重。`window_exhausted=true` 只指这轮取尽，不指整个历史完整。更早时间的迟到/回填消息、上游删除或排序变化、拉取时源数据变化仍可能影响覆盖；需要另行离线补导入或对账，**本轮模拟验证不证明真实活动数据库绝无漏项**。

## 本地验证结果

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/smoke_mcp.py
.venv/bin/python -m compileall -q src
.venv/bin/ruff check src/wechat_provider/chatlab_import.py \
  src/wechat_provider/weflow_reader.py tests/test_chatlab_import.py tests/test_weflow_reader.py
```

- 原有 36 项用例继续通过；新增 14 项，共 50 项通过。包含真实环回 HTTP 模拟服务，未请求真实 WeFlow。
- 真实 CLI `import-chatlab` 首次导入 2 条、重放新增 0 条；CLI API 同步新增 3 条、重复 2 条；随后经真实 MCP stdio 读取结构化会话、游标翻页、读取本地 5 条消息和检索均通过。全部为临时目录中的虚构数据。
- 同一时间戳超过单页、页数限额停止、重建 Store 后续传、503 中断恢复、窗口边界增量和重复重放通过；身份错误、重定向、401、业务失败、无进度游标、内容冲突和旧 checkpoint 均拒绝。
- Token 不出现在状态/错误中，不进入 URL；代理环境不改变环回连接；适配器没有调用 GUI mock。已有六工具 CLI/MCP smoke 和 Python 编译通过；新增模块与新增测试 Ruff 检查通过。
- 全仓 Ruff 在修改前已有 24 个问题，本轮未作无关清理。代码仅本地提交，未推送或部署；保留已有用户文档变更。

仍待真实验收：用户已安装 WeFlow 的实际版本、已获授权账号、API 返回格式与稳定消息 ID 覆盖、活动历史分页覆盖。没有读取私人聊天、启动微信、取钥或更改 TCC/FDA/SIP。测试通过不代表这些真实链路已经接通。
