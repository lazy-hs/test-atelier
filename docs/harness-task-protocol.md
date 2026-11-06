# Harness 任务协议

本文是浏览器、Test Atelier FastAPI、API Bridge 和 Harness Node Host 之间的第一版任务协议。协议覆盖需求分析、用例编写和受限项目技能，使用版本化任务类型和结果 Schema，不把 UI 文案或 Provider 私有字段暴露给模型协议。

## 1. 端点

所有端点都位于同源 `/api/v1`，需要登录、项目访问权限和 CSRF（读取事件流除外）。

部署设置 `TEST_ATELIER_HARNESS_ONLY=1` 后，需求分析和用例编写的旧
`/analyses`、`/analyses/{run_id}/generate` 和旧 `/ai/jobs/{job_id}/retry` 路由会拒绝新执行请求；页面只能创建本协议中的
Harness 任务。该开关不会删除历史 `analysis_runs`、`case_drafts` 或旧任务，便于在真实模型
和平台验收前回滚到兼容模式。API 和旧 AI worker 必须使用相同的开关配置：
开启后，worker 会把尚未开始或租约恢复的旧分析/用例任务标为失败并释放队列容量，
不会调用旧模型。切换前仍需停止接收旧任务并等待已经开始模型调用的任务结束；
该开关无法跨进程取消切换前已在运行的旧 worker 或正在进行的模型调用。
`/api/v1/health/worker` 对 API/worker 模式不一致返回 503；Harness-only API 遇到
未报告模式的旧版 worker 心跳也返回 503。发布流程必须核对此探针，不得仅以
心跳文件存在或 API 自身 `/health/ready` 作为切换完成的证据。
发布时运行只读 `production_smoke.py --require-worker --expected-harness-mode on`；
普通兼容部署使用 `off`，不能把缺少模式标记的旧 worker 作为通过。

```http
POST /projects/{project_id}/harness/tasks
GET  /projects/{project_id}/harness/tasks/{task_id}
GET  /projects/{project_id}/harness/tasks/{task_id}/events?after={sequence}
GET  /projects/{project_id}/harness/tasks/{task_id}/session
GET  /projects/{project_id}/harness/tasks/{task_id}/result
POST /projects/{project_id}/harness/tasks/{task_id}/cancel
POST /projects/{project_id}/harness/tasks/{task_id}/retry
POST /projects/{project_id}/harness/tasks/{task_id}/confirm
DELETE /projects/{project_id}
```

项目删除由业务 API 协调：先拒绝未结束任务并提交项目的
`deletion_pending_at`，再用任务原所有者的 `task.delete` 凭据逐条调用 Host，
收到所有持久删除回执后才清理业务数据库。Host 不可达、旧 Session 布局或
子进程尚未退出时保留项目与任务行，原 DELETE 可重试；项目列表暴露待处理
时间供管理员定位和重试，不向浏览器提供直接删除 Host 目录的接口。

`GET /projects/{project_id}/harness/config` 返回显式的 `skills` capability 列表。
当前声明 `requirement-analysis.v1`、`test-cases.v1` 和受限的 `skill-execution.v1`。
通用执行仅允许 `api-parsing`、`dev-doc-analysis`、`figma-to-prd` 三个已审查 Skill；
结果只进入任务和 Session 回放，不自动写入需求、用例或发布业务表。新增技能必须同时
定义任务类型、结果 Schema、持久化投影、敏感输出策略和跨平台测试，不能只把 `skill_id`
加入前端下拉框。

### 创建任务

请求体：

```json
{
  "task_type": "requirement-analysis",
  "task_version": "v1",
  "source_document_ids": ["src_01"],
  "skill_id": "requirement-analysis",
  "session_id": null,
  "input": {
    "language": "zh-CN",
    "output_mode": "structured"
  },
  "idempotency_key": "client-generated-opaque-key"
}
```

创建任务的 `session_id` 只能为 `null` 或省略。现阶段没有跨任务或跨 Host 重启的
Session resume；传入既有 Session ID 会被 API 和独立 Host 拒绝，重试任务会新建
Session 并保留旧任务的受权回放。
上游 ACP 的 `session/resume` 可恢复同一持久化根与工作区内的非活跃 Session，但不回放
历史通知，也不恢复已退出的工具进程；恢复一个 Session 不能证明业务任务完成。Host 不会对
重启中断任务自动恢复或重发原提示词，以免重复工具副作用或误提交分窗的部分结果。

用例编写任务必须引用已确认的 `analysis_run_id`，不能只传页面上未落库的分析文本：

```json
{
  "task_type": "test-cases",
  "task_version": "v1",
  "analysis_run_id": "analysis_01",
  "skill_id": "test-cases",
  "input": {"output_mode": "structured"},
  "idempotency_key": "client-generated-opaque-key"
}
```

创建用例任务时，API 以已保存的 Harness 需求和验收条件构造一次性的分析上下文，
将人工确认的歧义决策填入该上下文的 `resolution`。不发送重复的兼容报告正文或与
人工决策冲突的原始 `resolution=null`，也不修改数据库里供审计的 Harness 原始结果。

通用项目技能任务必须提供项目内已授权的来源文档：

```json
{
  "task_type": "skill-execution",
  "task_version": "v1",
  "source_document_ids": ["src_01"],
  "skill_id": "api-parsing",
  "input": {"language": "zh-CN"},
  "idempotency_key": "client-generated-opaque-key"
}
```

`GET /result` 只在项目权限校验、有界事件 catch-up 和 `completed` 终态后返回已校验的 Harness envelope；
`task.result` 已落盘但尚未收到 `task.completed`，或任务随后失败/取消时，返回空结果和真实任务状态，
不能当作可用结果；任务事件仍由项目 ACL 保护，用于审计和恢复。
响应同时返回从数据库任务行读取的 `project_id`、`source_document_ids` 和
`analysis_run_id`（项目技能可为 `null`），供离线质量评审核对分析与用例是否确属同一轮，
这些字段不是由模型结果反推；结果内部的来源和用例分析 ID 仍须与任务行一致。
通用技能页面以只读方式展示该结果，不把任意 `data` 自动转换为业务需求、用例或发布记录。
任务的 `input` 只允许语言标签和固定的 `output_mode=structured`；其他键、自由文本指令
或不合法语言标签在 API 和独立 Host 两层拒绝，不进入业务任务或模型输入。

服务端按 `(project_id, user_id, idempotency_key)` 去重。重复请求返回同一个 `task_id`，不能创建两个业务任务或两个可发布草稿。

Bridge 为每份授权材料发送 `source_document_ids` 与 `source_documents`。独立 Host
在运行模型前核对两组文档 ID 恰好一致、无重复且正文为文本；缺失/错绑材料不能仅靠
提示词或最终结果 Schema 纠正。旧版直接调用 Host、未携带 ID 列表的测试客户端保持
原有协议；Test Atelier 始终携带列表。用例编写只发送文档 ID/文件名元数据及
已确认的结构化需求分析，不再重复传送整个原始需求正文；这样来源追溯保持不变，
也避免大材料与原始未确认指令重新挤占用例模型上下文。

失败或已取消的任务可以通过 `POST .../retry` 创建新的 Attempt。重试只读取旧任务中
已持久化且再次通过项目 ACL 校验的 `source_document_ids`，不会从浏览器重新提交原文；
用例编写重试继续引用原来的已确认 `analysis_run_id`。新 Attempt 使用新的
`task_id`、幂等键和 Harness Session，旧任务和旧 Session 保留用于审计与回放。非终态、
已完成任务以及明确标记 `retryable=false` 的错误不能重试。

Bridge 在发送前按 UTF-8 字节数校验任务请求体，默认上限为 8 MiB（可通过
`TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES` 调整，最大 32 MiB）。需求分析或受限项目技能的正文使
单次请求超过此上限，或需求分析/项目技能的材料即使低于 HTTP 上限但可能超过普通
Host 分窗上限时，API 改为先发送不含正文的任务与材料 SHA-256 清单，再以
`POST /v1/tasks/{task_id}/input/chunks` 分批上传 UTF-8 正文，最后调用
`POST .../input/start` 校验完整长度和哈希后才启动模型。两步上传均使用只针对
当前任务、项目和用户的 `task.input` 作用域；缺块、重传冲突、内容损坏或
任务已取消时失败关闭，不调用模型。每个窗口最多 1 MiB，单份文本最多 100 MiB，
单任务文本合计最多 128 MiB；普通请求的 8 MiB 限制保持不变。API 根据与 Host
一致配置的普通窗口尺寸、重叠和总数计算保守容量，把换行造成的最短推进量及每份
材料至少一窗计入预算；窗口策略低于请求体上限时也会使用分段传输。跨服务部署须
保持三项普通分窗参数一致，否则只凭 API 判断不能保证 Host 的运行预算。
普通需求分析和项目技能创建请求现附带用于判断的 `window_policy`（窗口字符数、
重叠字符数、最大窗口数）；独立 Host 在授权后、落盘和启动运行器前对实际生效的
普通分窗配置逐项核对，不一致返回固定的 409 `window_policy_mismatch`。
直接调用 Host 的旧客户端可不带该字段；已决定采用分段模式的任务使用单独的
分段窗口预算，普通窗口策略不限制其创建。配置不一致需要运维修复，不能把
409 当作已运行的模型结果或暗中放宽 Host 的窗口数。
传输窗口不是模型
分析窗口；Host 对分段接收的需求分析和受限项目技能另以最多 2000 个 64000 字符窗口
执行 map/reduce，默认总执行时限 2 小时（上限 24 小时）。文档上传上限仍按
**原文件字节数**计算，因此一个高压缩率文件的提取正文可能超出上述文本预算。
用例编写的已确认分析上下文本身超过请求上限时，API 不再重复上传原始需求正文，
而是将已确认的结构化上下文序列化为单独的 `analysis:<analysis_run_id>` 输入，
以 `staged_kind=analysis`、`input_manifest` 和相同的 `task.input` 分段通道传输。
Host 检查哈希与分析 ID、Schema 后，按完整需求项及其关联歧义分窗调用 `test-cases`
Skill，逐窗检查需求覆盖并通过有界分层 reduce 聚合。单条完整需求超出窗口时，
按 `requirement-json.v1` 序列化原文切成带起止位置的片段；不拆开 Unicode 代理对，
每片保留明确的需求 ID 和完整的相关歧义决策。每片都要生成用例，最终结果必须保留
这些片段用例 ID，覆盖全部已确认需求且用例 ID 不重复，再由 API 验证追溯并投影草稿。
如果全局元数据/决策本身放不进窗口、超过 2000 窗口或聚合结果超预算，则失败关闭，
不截断或静默跳过；分片检查不能证明真实模型准确理解了跨片语义，仍需人工质量验收。
项目技能的超限来源正文可以分段，但其余非来源字段超限、
单文档/任务文本预算超限或清单仍超限时，同样返回 `413`。

Host 在私有任务 Session 目录中暂存分块，模型执行结束及已取消上传会清理暂存正文；
重启时中断的上传标记失败并清理，正常停机后未重启的暂存仍可能存在。
运行器停止状态无法确认时暂存正文与 Session 一并隔离，不在旧进程可能写入时清理；
完成前不得推断为已执行。停止服务做备份时应将
私有 Session 目录视为可能包含原文的敏感数据，按照
`docs/harness-backup-recovery.md` 统一保护、保留及删除。

Host 返回的创建回执、确认/取消/删除回执和 Session 回放也通过 Bridge 的流式
响应上限（默认 16 MiB）检查，成功和错误响应均在解析 JSON 前限额读取原始字节；
不先缓冲完整正文，也不接受压缩正文绕过限额。SSE 流按同一响应上限累计原始字节，
在拼接或解码无换行的事件行之前拒绝超限数据。

需求分析或受限项目技能任务超过单窗口字符预算时，Host 自动执行有界 map/reduce：
每个 map 子任务只接收一个带源文档 ID、文件名和字符偏移的窗口，随后由独立 reduce
轮次合并部分结果。
任务仍只有一个 Test Atelier `task_id`，中间轮次通过 `task.progress` 报告
`chunk_started`、`chunk_completed` 和 `reduce_started`；所有窗口共用总任务超时和最大
窗口数，任何一个窗口失败都会使业务任务失败，不产生部分可发布结果。项目技能的
reduce 合并所选技能的 `output` 和 `items`，保留来源并在 `warnings` 标示冲突；结果仍
仅供查看，不能因为合并成功就自动成为业务需求或用例。每个 reduce 轮次
还受 `HARNESS_REDUCE_BATCH_CHARS`（默认 48000）和 `HARNESS_REDUCE_BATCH_SIZE`（默认
16）限制；多个轮次以树形方式继续聚合，只有最后一轮产生最终 reduce 结果。事件会额外
携带 `round`、`batch_index`、`batch_count`、`input_count` 和 `final`，便于页面展示进度。
单项结果超过预算，或任意两个结果都无法合批时，任务直接失败，不能无限循环或以单项
中间结果冒充最终结果；批次间也不重置总任务截止时间。

任务开始时的 `session_id` 可能是 Host 预分配的临时 ID。真实 dsh/ACP Session
创建后，Host 先发送 `task.progress`（`kind=session_bound`，携带真实 `session_id`），
再发送该 Session 的结果。map/reduce 可绑定多个 Session；API 只接受先前任务事件
已绑定、且与 `task.result` 事件一致的结果 Session ID，不允许结果自行声明新 ID。
尚无任何绑定记录的任务也必须拒绝 `task.result`，不能让第一条结果事件自我授权。
`ta-<task_id>` 只是分配占位符，不返回为可回放 Session，也不允许它作为最终结果；
`chunk_started` 和 `reduce_started` 的合成窗口 ID 仅用于显示进度，不能据此授权回放。
只有真实 `session_bound` 事件中的 ID 进入 map/reduce 的 Session 列表。
Host 的绑定事件还记录自己预分配的 `allocated_session_id`。若崩溃发生在事件回放落盘、
索引替换之前，Host 启动时只从索引序号之后的有效 `session_bound` 恢复真实绑定，
精确移除对应临时 ID；旧事件没有该字段时只可移除已知的 `ta-<task_id>` 占位符，
不得按目录名猜测 map/reduce Session 归属。无效绑定失败关闭，不能授权跨任务回放。
两个 Profile 的 JSONL 持久化根目录必须与 Host 的绝对路径 `HARNESS_SESSION_ROOT`
一致；否则业务结果可能完成，但 Session 回放和停机备份会遗漏真实 dsh 文件。

## 2. 状态机

```text
queued -> starting -> running -> completed
                         |          |
                         v          v
                 waiting_confirmation  failed
                         |          ^
                         v          |
                    running       retry

queued/starting/running/waiting_confirmation -> cancelled
```

状态语义：

- `queued`：已写入 Test Atelier 持久队列，尚未分配 Harness Session。
- `starting`：已创建作用域凭据和 Session，正在加载 Profile/Skill。
- `running`：Agent 正在执行；可产生增量事件。
- `waiting_confirmation`：Harness 要求用户确认工具或业务动作，不能自动完成。
- `completed`：结果通过 Schema 校验并可供 Test Atelier 确认或保存。
- `failed`：发生可诊断失败；`error.code` 必须稳定，允许新 Attempt 重试。
- `cancelled`：取消已传播到 Agent、Provider、Tool 和脚本；未知中止不得标记为成功。

只允许服务端执行状态迁移。终态不能被普通客户端改回运行态；重试创建新的 `attempt` 和 Agent turn，并保留旧任务事件。

新任务的 `task.result` 先严格校验并保存到 Harness 任务投影，但不立即创建业务分析或用例草稿。
只有随后收到 `task.completed`，才在同一事务中写入 `analysis_runs` 或 `case_drafts` 并标记任务完成。
缺失有效结果的完成事件进入 `schema_invalid` 失败路径；结果之后发生失败或取消也不能产生
可用的业务结果。升级前已经投影过的 `task.result` 事件不带新的待提交标记，完成时不会重复
生成草稿或覆盖人工编辑。

## 3. 任务响应

```json
{
  "task_id": "task_01",
  "project_id": "project_01",
  "task_type": "requirement-analysis",
  "task_version": "v1",
  "status": "running",
  "session_id": "session_01",
  "session_ids": ["session_01"],
  "source_document_ids": ["src_01"],
  "retry_of": null,
  "result_schema": "requirement-analysis.v1",
  "sequence": 12,
  "created_at": "2026-10-23T10:00:00Z",
  "updated_at": "2026-10-23T10:01:00Z"
}
```

任务详情可以返回摘要和最终结构化结果，但不默认返回完整 Session。完整回放由独立受 ACL 保护的 Session 查询接口提供。

对于仍处于非终态的任务，任务详情请求会执行一次有界的远端事件 catch-up：它从本地已保存
的 Harness 远端序号继续读取事件，并将结果和业务投影写入 Test Atelier。该 tick 使用短暂的
空闲超时，不会把详情请求变成长连接；上游暂时不可用时保留当前任务状态，下一次详情请求或
SSE 重连继续从最后一个远端序号尝试。这样浏览器在 SSE 中途断开后，刷新任务详情仍可恢复
结果，不要求浏览器必须持续持有唯一的投影连接。生产部署还运行独立的
`api.harness_projector` worker；它按数据库租约持续消费 Host 的持久事件，因此无客户端请求时
也能完成业务投影。浏览器详情 catch-up 只是低延迟补偿，不是唯一消费者。

`GET .../tasks/{task_id}/session?session_id=...` 只允许任务所属项目的已认证成员访问。FastAPI
先检查项目 ACL，再向 Host 请求与该任务绑定的 Session；Host 使用独立的
`session.read` 作用域凭据再次校验任务和 Session 的绑定。一个有界 map/reduce 任务可能拥有
多个 Session，服务端只允许查询任务 `session_ids` 中的值；省略参数时读取最终主 Session。
旧任务中即使残留 `ta-<task_id>` 占位符，详情和回放接口也会过滤或拒绝它。
响应是有大小和事件数量上限的只读回放，不提供写入、删除或任意 Session ID 查询。超出上限时
响应保留前缀并将 `truncated=true`，调用方不能将其当作完整记录。

## 4. SSE 事件

`GET /events` 使用 `text/event-stream`。客户端通过 `after` 或 `Last-Event-ID` 续传；服务端必须保持 Test Atelier 投影事件的 `sequence` 单调递增且不复用。Harness Host 自身也有独立的远端序号，Bridge 以内部远端游标拉取，再映射为本地序号，不能把两个序号空间直接比较。

Bridge 创建任务时把自身响应预算减去 4096 字节作为 Host 单条事件上限；读取 Host
`/v1/tasks/{task_id}/events?after=...&max_bytes=...` 时也使用该有界字节预算。
Host 只在完整事件之间分页，达到页预算便结束本次连接，不推进尚未发送的序号；
Projector、详情补偿和页面在下一次连接从最后持久化的远端/本地序号继续。
结果事件若单独超过创建时约定的预算，Host 在发出 `task.result` 之前以
`output_too_large` 终止任务，不留下已完成却无法投影的业务结果。过大的增量
`task.progress` 文本只以 `output_truncated` 标记代替，原始 Session 仍按受保护的
Session 回放规则管理。若消费方后来把响应预算调低到低于旧任务的单条事件，
Host 返回 `event_frame_exceeds_transport_budget`（413）；应恢复匹配的预算后续传，
不能把配置不匹配误判为任务成功。

Host 的 `HARNESS_TASK_ROOT` 按任务分开存储元数据索引 `<task_id>.json` 与敏感回放 `<task_id>.events.json`。后者包含进度文本及完整结构化结果，用于 Host 重启后按远端游标继续向 Projector 交付事件；不可视为无敏感信息的任务索引。两者应一起实施目录 ACL、备份、保留期和清理，Windows 不能仅依赖 POSIX 文件 mode。历史内嵌事件索引由 Host 启动时迁移；回放丢失/损坏时不得将任务恢复为成功。

后台 Projector、详情补偿和浏览器 SSE 采用同一远端协议校验：事件类型必须在白名单内，`task_id` 必须匹配，`sequence` 必须是正的 JSON 安全整数。旧序号只去重；缺失、字符串、布尔值或越界序号不能推测为下一个值。协议违规投影为安全的本地 `schema_invalid` 终态，不转发异常事件内容，也不把未经验证的 Session ID 加入任务访问范围。

浏览器订阅到 Host 的事件流若因短暂网络故障中断，Test Atelier 不把该故障投影为
`task.failed`，也不推进本地 SSE 游标；响应只发送不带 `id` 的瞬态
`task.transport_error`，随后关闭连接。页面按上一个已持久化的本地事件 ID 重连，
并在重连前读取任务详情触发一次有界 catch-up；独立 Projector Worker 仍负责无浏览器时
的持续投影。只有 Host 明确发送 `task.failed`、结果校验失败或取消完成，任务才进入终态。

创建任务时的 `X-Request-ID` 由 Host 按 128 字符安全字符集限制后写入任务事件，
用于跨 API、Bridge、Host 和 Session 的故障关联；请求 ID 不承载用户输入或凭据。

```text
id: 13
event: task.progress
data: {"task_id":"task_01","session_id":"session_01","sequence":13,"kind":"assistant_delta","text":"正在整理需求"}
```

事件类型：

| 事件 | 必填字段 | 说明 |
| --- | --- | --- |
| `task.created` | `task_id`, `sequence` | 任务进入队列 |
| `task.started` | `task_id`, `session_id`, `sequence` | 任务启动；此时 Session ID 可能只是不可回放的占位符 |
| `task.progress` | `task_id`, `sequence`, `kind` | 增量文本、工具状态或阶段进度；不得写入密钥 |
| `task.confirmation_required` | `task_id`, `sequence`, `confirmation` | 等待用户确认；`confirmation` 可包含 `confirmation_id`/`id`、`prompt`/`question`，客户端不得假设固定字段名 |
| `task.result` | `task_id`, `sequence`, `schema`, `result` | Schema 校验后的完整结果 |
| `task.failed` | `task_id`, `sequence`, `error` | 稳定错误码和可显示消息 |
| `task.cancelled` | `task_id`, `sequence`, `reason` | 取消完成 |
| `task.completed` | `task_id`, `sequence` | 业务结果已持久化 |
| `task.keepalive` | `task_id`, `sequence` | 无输出时保持连接 |
| `task.transport_error` | `task_id`, `error` | 仅当前订阅的瞬态故障；不带 `id`、不持久化、不改变任务状态 |

Host 不主动把上传文件原文、Prompt、API Key、Cookie、数据库连接串或模型内部思考作为进度事件发送；但模型生成的进度文本和最终结果可能回显敏感需求内容，因此事件回放和 SSE 仍必须按项目 ACL 保护。需要追溯时使用受保护的 Session 和结果存储。

Host 重启后，已落盘的终态任务和事件仍可查询；无法恢复的活动子进程会被标记为 `host_restarted` 失败，不能伪装为成功。重复的远端事件按远端序号去重，重复结果不会再次写入业务表。
异常退出前处于运行或等待确认的任务，其旧运行器可能仍在写入；Host 将它标记为
`termination_unconfirmed`，健康检查计数，并拒绝取消确认和删除任务私有 Session 目录，
直到运维停写、核实剩余进程与文件后处置。Host 在启动运行器前先持久化启动意图；
`starting` 阶段若意图为真或旧索引没有该字段，也保守隔离。意图明确为假的分段上传
尚未启动模型，可清理临时原文并按失败任务处理。
只读存储对账把本项目的隔离任务计为 `unconfirmed_runner_stops`，不能把数据文件齐全
误判为可删除；解除隔离只能走停写、备份验证和人工进程树核实后的逐任务工具，
具体步骤见 `docs/harness-backup-recovery.md`，业务 API 不提供直接清除标记的入口。

结构化结果校验失败时，Test Atelier 会将任务置为 `failed`，保留稳定的
`schema_invalid` 错误、结果哈希/大小/字段名摘要和任务已验证的 Session 引用；模型生成的标识和未知 Schema 仅保存哈希，不会把
模型返回正文或 Pydantic `input_value` 写入普通事件、错误字段或日志。原始内容只能
通过受 ACL 保护的 Harness Session 回放获取。

终态任务的 `GET /events?after=0` 直接从 Test Atelier 的本地事件投影回放，
不会再次调用 Harness，也不会重复写入 `analysis_runs` 或 `case_drafts`；这条路径用于
浏览器刷新和只读恢复。完整 Harness Session 原始事件通过上述受 ACL 保护的 Session
查询接口提供，不能把业务投影误认为完整 Session。该接口只读取 Profile 配置的
line-readable JSONL Session 目录，Host 不导入 Harness 私有持久化实现。

## 5. 取消和确认

取消接口必须幂等。服务端收到取消后先写入持久状态，再向 Harness 发送取消信号，最后等待 Agent、Provider、Tool 和脚本结束；超出宽限期时标记为 `cancelled` 或 `unknown`，不能标记为 `completed`。

确认接口：

```json
{
  "confirmation_id": "confirm_01",
  "decision": "approve",
  "comment": "允许读取已授权的需求材料"
}
```

API Bridge 对确认仍只接受 `approve`、`reject`、`cancel` 三种决策，并要求
`confirmation_id`（兼容滚动升级时也接受 `id`）。未知字段、缺少确认 ID 或其他决策值
会在转发到 Host 前返回 `422`；服务端不会把任意前端 JSON 原样传给 Harness。
Phase 2.6 的业务歧义选择另用受限 `ambiguity_choice` 字段传输，只能配合 `approve`：
`accept_suggestion`、`provide_explanation`、`skip`、`mark_pending`；提供解释时
`comment` 不得为空。Host 校验确认种类，歧义决定不能冒充 ACP 工具权限。

独立 Host 可在官方 SDK ACP 下显式设置 `HARNESS_ANALYSIS_CHECKPOINTS=1`，让需求分析
先由模型返回 Phase 2.5 解析摘要；有警告或适用端未决时，Host 以
`kind=skill_checkpoint` 发出受项目 ACL 保护的确认事件，不提前发布最终结果。
确认可附 2000 字符内的 `comment`，由同一 Harness Session 的下一轮接收；拒绝则取消任务。
无警告且标签明确按快速模式自动继续。headless/legacy ACP 不支持此模式，预检会拒绝。
Phase 2.5 后由同一 Session 每轮只返回一条关键歧义，按原文、类型、建议理解逐项向用户
展示；无更多关键歧义时返回结束标记，随后模型须先提交包含 14 项检查、来源数量、
追溯覆盖率和未映射来源数的 Phase 2.8 自检。Host 核对声明的门槛，失败只允许在
同一 Session 返回 Phase 2 修正一次；最终报告还要与自检证据标记、需求/来源 ID、
逐项歧义、每条需求的 EP/BVA/ST/EG 预覆盖表行、与自检一致的追溯率、尼尔森 H1～H10
逐条结论、子项核对统计及“未核对子项：0”一致；仅列方法名称或 H 编号不能通过。多文档报告的来源覆盖
节还须逐个列出输入文件名或原始来源 ID，否则不发布结果。
决定写入任务事件并送回同一 Session；跳过和待确认不等于已解决，阻断歧义也不自动改写为
结构化已确认。这些可机械核对的条件与模型自评仍不能证明语义正确、尼尔森子项确已
逐条评审或跨文档确无遗漏；正式验收仍须人工对照真实材料及质量 Gold。
人工澄清仍另存为 Test Atelier 业务决定，不改写 Harness 原始报告。

用例编写的 ACP Phase 3 在收到第一份通过 Phase 2.8 对账的模型用例后，
以 `kind=case_tag_selection` 暂停。用户可不增加例外直接继续，或在 `comment`
中提交一个已有 `case_id` 及不同的 `applicable_platforms`；Host 校验用例 ID、
适用端及字段边界，再要求同一 Session 的模型重发完整 `test-cases.v1`。
Host 检查其他用例及来源均未改变，随后仍以 `case_tag_exception` 要求逐条批准；
拒绝、模型改写其他用例或预审指标失配均不会发布结果。业务层不会自行修改
Harness 原始用例。`Pc`、`Web`、`Android`、`IOS` 是被测产品标签，
不扩展工作台的 macOS/Windows/Linux 运行范围。

只允许创建任务的同一用户或具有项目写权限的成员确认。确认过期、任务状态不匹配或 `confirmation_id` 已消费时返回 `409`。

## 6. 错误格式

```json
{
  "error": {
    "code": "project_skill_disabled",
    "message": "当前项目未启用此技能",
    "request_id": "req_01",
    "retryable": false,
    "details": {"skill_id": "requirement-analysis"}
  }
}
```

首批稳定错误码：`unauthenticated`、`forbidden`、`project_not_found`、`project_archived`、`project_skill_disabled`、`skill_not_allowed`、`task_not_found`、`task_conflict`、`queue_full`、`provider_unavailable`、`request_too_large`、`output_too_large`、`schema_invalid`、`quality_gate_failed`、`report_missing`、`confirmation_unavailable`、`cancelled`、`internal_error`。`report_missing` 表示新需求分析结果缺少合规的原始 Markdown 报告，`quality_gate_failed` 表示预审修正仍未通过或最终报告与声明证据矛盾；两者均不发布结果。错误消息可以本地化，`code` 和 `retryable` 不能依赖文案解析。

当前默认 Test Atelier Profile 通过一次性 `headless` 运行器执行任务。该运行器不提供进程内的
双向确认通道；如果 Harness 产生确认事件，Host 必须失败关闭并返回
`confirmation_unavailable`，而不是向已经结束的 stdin 写入数据。需要真实工具审批时，使用
独立的 `test-atelier-acp` Profile 和显式 `HARNESS_DSH_PROTOCOL=acp` transport；Host 在单个
任务生命周期内保持 ACP 子进程和 Session，通过 `session/request_permission` 等待独立的
`task.confirm` 请求。确认映射只允许 `allow_once`、`reject_once` 或 `cancelled`，不接受前端
提交任意 `optionId`。Host 重启后仍失败关闭；若以后支持显式 Session 续写，应另行设计
授权、原工作区/持久化根校验、人工确认过期与工具副作用的去重，不能直接重放旧任务。

## 7. 安全和审计要求

- API Bridge 必须校验作用域凭据的用户、项目、任务、操作和过期时间。
- Node Host 不能直接接收浏览器提交的任意 Skill、脚本路径、Provider 命令或数据库查询。
- 所有创建、确认、取消、重试、结果提交和权限拒绝都写入 Test Atelier 审计事件。
- 日志只保留低敏字段：`request_id`、`task_id`、`session_id`、状态、耗时、错误码、Provider 名称和版本。
- 任务结果提交采用乐观锁；页面刷新或重复 SSE 事件不能重复写入 `analysis_runs` 或 `case_drafts`。
