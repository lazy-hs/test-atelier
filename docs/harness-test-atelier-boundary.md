# DeepSeek Harness 与 Test Atelier 边界

## 1. 目标架构

```text
Browser
  -> Harness Web Shell / Test Atelier extension UI
       -> same-origin API Bridge
            -> Test Atelier FastAPI
                 -> Cookie session / CSRF / project ACL
                 -> SQLAlchemy / Alembic / business tables
                 -> durable AI job queue and worker

Harness Node Host
  -> Cordis runtime
  -> Agent / LLM / Tool / Skill / Session / Replay
  -> Test Atelier API Bridge client

Test Atelier database remains the business source of truth.
Harness Session remains the model-visible execution and replay record.
```

第一阶段不把 Harness Web 的 React 18 组件直接挂到 Test Atelier React 19 的根节点。优先让 Harness 作为独立 Web Shell 运行，通过 API Bridge 和插件槽位接入；iframe 仅用于临时验证，不能作为最终组件集成方案。只有在完成 React 18/19 compatibility spike、卸载测试和构建产物检查后，才决定是否统一运行时。
`docs/harness-web-shell-spike.md` 记录当前锁定上游的可编译性和仍未完成的 Web
Profile、客户端插件及运行时权限验收；现有 Host/ACP 集成不能冒充 Shell 集成。

## 2. 所有权

| 能力 | 权威系统 | Harness 可做的事 | Test Atelier 可做的事 |
| --- | --- | --- | --- |
| 用户与登录 | Test Atelier | 读取短期作用域凭据 | 会话、CSRF、角色和审计 |
| 项目与成员 | Test Atelier | 携带 project context | ACL、成员角色、归档和写权限 |
| 项目技能启停 | Test Atelier `project_skills` | 发现和执行被授权的 Skill | 校验项目、用户、Skill、Session 和权限 |
| 模型与工具执行 | Harness | Agent、LLM、Tool、取消、流式事件 | 选择允许的 Profile/Provider，记录任务引用 |
| 对话和回放 | Harness Session | Agent turn、工具事件、模型可见上下文 | 保存 session_id 和摘要，不复制整份 Session |
| 需求分析业务结果 | Test Atelier `analysis_runs` | 生成并流式返回 `requirement-analysis.v1` | 校验、确认、编辑和落库 |
| 用例草稿 | Test Atelier `case_drafts` | 生成并流式返回 `test-cases.v1` | 质量门禁、编辑、发布和导出 |
| 已发布用例 | Test Atelier `cases` | 只读提供上下文 | 审核、评论、治理和版本控制 |
| 审计与指标 | Test Atelier | 发送 task/session 关联字段 | 记录业务动作和低敏指标 |

Harness 不直接连接 Test Atelier 数据库，不读取 Cookie Session，不执行任意项目 SQL。Node Host 只能通过 API Bridge 调用明确的资源接口。

需求分析页面对 `provider=harness` 的记录直接展示通过校验的原始 `harness_result`，
不把本地兼容层生成的 Markdown 报告、阶段进度或默认测试方法冒充 Harness 输出；
人工歧义确认单独标为 Test Atelier 业务操作。用例编写页面只展示 Harness 原始结果、
执行状态、确认、Session 和原始结果导出，不混排可编辑业务草稿；
报告和人工确认摘要中的 Markdown 图片仅显示替代文字，不在用户浏览时自动加载
模型给出的外部资源；外链必须由用户主动点击，且不发送本页 Referrer。受权限
保护的 `/result` 和 Markdown 下载仍保留原始字节，外部工具打开下载内容时需自行
遵守材料保密要求，不能将展示层的抑制当作原始结果已改写。
独立的「草稿审核」入口按所选分析记录展示 Test Atelier 投影并保留人工编辑、发布和旧草稿访问。
未配置 Harness 时需求分析和用例编写均不可退回传统模型执行；已有传统 Provider 报告
移至「历史分析」，旧草稿通过「草稿审核」读取与审核，业务 API 保留历史兼容。
Harness 分析投影中的兼容报告字段只能保存 Harness 原始 `report_markdown`；历史结果若未返回
该字段则保持缺失，不从结构化数据生成替代报告。旧报告编辑接口拒绝 Harness 分析，
人工歧义决定只写入独立的业务决策，不会重新渲染或覆盖原始报告。
分析记录若仍关联未结束的 Harness 任务，单独删除返回 409；任务结束后先以
`analysis_runs.status=deleting` 持久阻止新的关联用例任务，再逐个通过 Host 的
`task.delete` 清理任务与 Session，全部成功后同事务移除本地任务事件、任务、
分析和草稿。Host 不可用或清理失败时保留业务记录与删除状态，页面提供继续清理；
不能仅移除业务行或绕过数据库外键。创建和重试任务与设置/核对删除状态共用
项目行锁，删除期间拒绝关联旧分析的重试，避免检查后留下悬空引用；已有其他
分析引用被删除任务的重试链会置空，不删除其他任务。完整项目删除仍采用独立的停写标记。
旧 `ai/jobs/{job_id}/retry` 在 Harness-only 部署中拒绝重新执行传统模型任务；
即使暂未切换到 Harness-only，只要关联分析已进入删除流程或已不存在，也拒绝重试。
兼容模式也不允许旧 `/analyses/{id}/generate` 或旧任务重试以显式 Provider
重新执行 Harness 分析；`analysis_runs.provider=harness` 不得被旧生成路由改写。
worker 对升级前可能遗留的、指向 Harness 分析的旧 AI 任务同样失败关闭，
只结束旧任务并释放容量，不改变该分析的状态或原始结果。
兼容模式下的旧用例生成在技能注册提交后重新获取项目行锁、刷新分析记录，
再核对归属和状态并提交旧任务；与分析单独删除不能交错留下新任务。
旧任务详情和提示词查询仍可读取历史记录。

独立 Host 的部署契约位于
`/Users/lazy/huangsheng/project/test-atelier-harness-extensions/deploy/compose.harness.example.yaml`。
该片段只提供网络、健康检查、任务/Session 持久卷和运行时密钥注入，不把 Harness
核心源码或业务数据库并入 Test Atelier；镜像构建和 Compose 启动仍需在目标环境验收。

## 3. 执行边界

### Harness 负责

- 根据 Profile 组合 Agent、LLM、Tool、Skill、Session 和 Web 服务。
- 处理 Agent 生命周期、模型请求、工具调用、用户确认、取消和事件回放。
- 将每个模型可见输入和输出写入 Session 事件，使结果可以重放和审计。
- 按请求的 `task_type` 调用 Test Atelier 扩展插件，不决定业务角色和项目成员权限。

### Test Atelier 负责

- 校验用户已登录、CSRF、项目成员关系、角色、项目归档状态和 Skill 是否启用。
- 保存原始需求材料、提取文本、分析运行、草稿、用例、审核和导出结果。
- 管理持久任务、租约、worker 恢复、容量限制和业务级取消状态。
- 在结构化结果不符合版本 Schema 时拒绝提交，保留原始 Harness Session 引用和失败诊断。

## 4. 项目技能边界

`skills/registry.json` 和 `project_skills` 仍是 Test Atelier 的事实源。Harness 的 `ctx.skills` 只是适配层，不代表用户拥有全部技能。每次任务启动、Skill 调用和脚本执行都必须重新检查：

```text
project_id + user_id + skill_id + session_id + project role + enabled
```

官方 SDK ACP 的 `tool_call` 与 `tool_call_update` 都核对运行中技能 ID；后续更新若
尝试改为未授权技能，Host 失败关闭且不把原始工具参数写入任务事件。

技能执行必须沿用现有安全规则：限定 skill root、脚本白名单、`shell=False`、参数数组、超时和进程组终止、凭据隔离、`.memory` 只读、禁止写技能仓库。任何只在前端隐藏按钮的实现都不满足权限要求。

Harness 侧从独立挂载的 `TEST_ATELIER_SKILL_ROOT` 按任务读取真实 qa-doc-skills
Hub、目标 `SKILL.md` 和审查过的 references；子进程只能发现当前任务的技能 ID，
来源缺失或符号链接逃出挂载根目录时失败关闭。技能仓库根目录的全局 `.memory`
不跨项目复用；经授权的项目快照须来自另行配置的只读
`TEST_ATELIER_SKILL_MEMORY_ROOT/projects/<project_id>/`。原技能中的文件写入和脚本步骤
不会由技能适配器自动执行；两个 Profile 已将沙箱固定为 read-only，关闭 shell、文件、
Web、委派等模型工具，并在 Host 启动前核对已安装补丁。仍须在三平台发布验收中实际
验证模型不能通过其他工具执行写入。
交互检查点和原始 XMind 交付也需真实模型及人工流程验收。
上传材料、原始分析和任务 `input` 均作为待分析数据传入，不能提升为系统、用户或
项目技能指令；任务 `input` 仅允许语言标签及固定的结构化输出模式。Host 提示词
明确此信任边界，并由技能白名单及输出 Schema 继续强制约束。提示词和 Fake DSH
测试不保证真实模型能够抵御所有文档中的提示注入，仍需真实模型与人工复核。

## 5. 数据保存规则

不得用 Harness JSONL Session 替代业务表。Test Atelier 保存最小关联信息：

```text
task_id
session_id
project_id
user_id
skill_id
result_schema
result_version
result_json
source_document_ids
created_at / completed_at
```

Harness Session 保存模型可见对话、Agent turn、LLM/Tool/Skill 事件和回放信息。业务表保存原始输入引用、结构化结果、人工编辑、确认、发布和审计。敏感材料不写入普通访问日志；Session 回放和业务结果读取都要通过项目 ACL。Session 回放按任务绑定，不能按任意 Session ID 枚举。
独立的 headless/ACP Profile 必须禁用上游基础配置中的 `session-log-deepseek` 和
`session-telemetry-otel`，不能将需求原文或 Session 通过日志贡献、反馈上传到模型
API 之外的接收方。Profile 组合检查应验证最终有效配置，而不只检查补丁文件；
Host 与生产预检还应拒绝更高优先级的 `$DSH_HOME/cordis.patch.yml` 和额外 `--patch`
启动参数，防止它们覆盖已验证的禁用行。更新上游基线时必须重新核对新增的网络出口。
即使调用方显式传入 Session ID，API 也必须先确认该 ID 已记录在任务的 `session_ids`（旧记录可仅有主 `session_id`）中；尚无绑定 Session 的任务不得把请求转发给 Host。

Node Host 的任务索引文件不是业务文档存储：它只保留状态、来源标识、文件名和文本长度，
不持久化需求正文、Prompt 输入、进度文本或分析结果。独立的任务事件回放可能包含进度文本
和完整模型结果，Harness Session 另存模型可见上下文；两者均须通过任务绑定、项目 ACL、
目录权限、大小上限及受控备份/保留策略访问，不能视为公开资源。

## 6. 认证与请求关联

推荐同源反向代理：浏览器只访问 Test Atelier origin，API Bridge 在服务端转发到 Node Host。Bridge 为每个任务创建短期、最小权限的作用域凭据，至少包含：

```text
subject_user_id
project_id
task_id
allowed_operation
expires_at
request_id
```

生产或共享环境使用独立的 Harness 作用域密钥；联合 Compose 通过只读文件挂载到
API、Projector 和 Node Host，不能复用浏览器会话密钥。会话密钥和初始管理员密码也在
联合 Compose 中各由独立文件挂载，管理员密码不进入 worker、Projector 或 Host。
本地未配置独立密钥时，Bridge 仅为兼容旧开发环境
回退到会话密钥，生产前置检查在 Harness URL 启用时会拒绝这种缺省配置。容器 Host
显式绑定私有网络地址，开发默认仅绑定回环地址。

Node Host 不接受没有作用域凭据的业务调用。所有请求传播 `X-Request-ID`，SSE 事件带 `task_id`、`session_id` 和单调递增 `sequence`。凭据过期、项目被撤权或任务取消时，Bridge 必须拒绝后续写入。

共享网络中的 Host 可以额外配置 `TEST_ATELIER_HARNESS_TOKEN`。配置后 API Bridge
必须发送匹配的 Bearer Token，Host 在作用域校验前拒绝缺失或错误的 Token；未配置时
仍必须提供 HMAC 作用域凭据。该 Token 只用于 Host 入口的纵深防御，不替代项目、任务和
操作声明的作用域校验。

Node Host 启动 Harness 子进程时必须构造白名单环境。子进程只接收当前 Profile
需要的模型凭据和任务标识，不继承 `TEST_ATELIER_SCOPE_SECRET`、
`AI_STUDIO_SESSION_SECRET`、数据库连接串、管理员密码、指标 Token 或 Bridge Token，也不
继承未列入白名单的宿主变量。测试替身若需要额外变量，必须显式列入
`HARNESS_CHILD_ENV_ALLOWLIST`。该隔离要求同时适用于本地进程和容器进程，并由 Host 集成
测试与部署检查持续验证。

Host 还会对 Harness JSONL 中的 `skill` 工具调用做任务级校验，只允许当前任务已通过
Test Atelier 项目 ACL 授权的 `skill_id`；模型请求其他技能时返回 `skill_not_allowed`，不能
只依靠 Prompt 或前端隐藏按钮。

## 7. 失败和降级

- Harness 不可用：保留登录、项目、人工编辑、导入审核、转换和历史读取。
- Provider 不可用：任务进入 `failed`，不自动伪造结果；允许在原任务上重试并创建新的 Agent turn。
- Bridge 断开：页面可以用 `task_id` 恢复事件流，不能重复提交业务结果。
- 结果 Schema 失败：保留原始文本和 Session 引用，进入人工修复或重试，不丢弃需求材料。
- 项目技能停用：新任务拒绝，运行中的脚本取消；已完成的历史结果只读保留。

停用技能时，Test Atelier 在同一数据库事务中取消该项目该技能的未完成任务，
阻止已暂存的 `task.result` 随后发布，并用任务原所有者的作用域凭据尽力请求 Host
终止子进程。Host 暂时不可达时本地仍保持取消，远端进程可能直到超时才退出；
因此停用回执不应被解释为远端已停止的证明。

项目归档、成员移除或降为只读也会在权限变更的同一事务中取消受影响的未完成任务；
晚到的 Host 结果不能发布。Host 取消仍是尽力请求，不代表远端进程已经停止。
项目删除则拒绝尚有未结束 Harness 任务的项目（HTTP 409）；先取消任务或等待其终态，
随后持久化项目的删除待处理时间并拒绝新任务和业务访问。API 按任务原所有者作用域
调用 Host 的 `task.delete`，只有 Host 确认清理该任务专属 Session 目录、事件和索引后，
才删除业务记录与项目。Host 不可达、中途失败或仍有子进程时保留项目和任务行，
同一删除请求可重试；本地 `cancelled` 不会被当作远端进程已经停止的证明。
旧版混放的 Session 无法仅凭任务 ID 证明归属，Host 会拒绝自动删除；停写备份后
可用独立命令逐任务核对、复制、实际回放，再确认清除旧目录。副本与旧目录并存时
Host 继续拒绝删除，避免遗留源文件；不明确的归属仍由人工处理。跨数据库/文件卷
没有全局事务，恢复演练和运行中并发写入仍需目标环境验证。

通用 `skill-execution.v1` 只作为只读结果能力，当前允许 API 解析、开发文档分析和 Figma 转 PRD 三个 Skill；不自动创建需求、用例或发布记录。

## 8. 迁移顺序

先迁移需求分析和用例编写的执行面，再以 `skill-execution.v1` 只读结果接入已审查的 API 解析、开发文档分析和 Figma 转 PRD；最后评估其他用例库、治理、转换、模型配置和 Codex 工作台。认证、数据库、业务表和持久队列不因引入 Harness 而整体迁移。
