# Test Atelier 基于 DeepSeek Harness 的最终重构开发计划

> 计划版本：2026-10-27  
> 状态：执行中；当前已完成结果展示边界加固，生产发布门禁仍未全部通过  
> 适用平台：macOS、Windows、Linux

## 1. 目标与结论

Test Atelier 保留项目管理、认证、成员权限、业务数据库、人工确认、审核、发布、导出和审计；DeepSeek Harness 作为独立的 AI 执行基座，负责 Agent、模型、工具、Skill、Session、流式事件、取消、确认和回放。

需求分析、用例编写和项目技能的新任务统一由 Harness 执行。Test Atelier 页面只显示通过版本 Schema 校验、且收到 `task.completed` 后的 Harness 原始结果，不把进度文本、兼容层生成的报告或失败前的暂存结果冒充交付物。传统 Provider 结果保留在历史分析和草稿审核入口，不再作为新任务的回退路径。

本计划不修改 `/Users/lazy/huangsheng/project/deepseek-harness` 的核心工程结构。所有领域适配放在独立的 `test-atelier-harness-extensions` 仓库，并通过公开 Profile、Bundle、Plugin、Remote、SDK/ACP 或 HTTP Bridge 接入。上游通过独立同步分支持续更新，任何不兼容版本停留在旧锁定 SHA，不强行合并。

## 2. 已完成的项目分析

### 2.0 首个执行切片（2026-10-27）

- 新增浏览器侧 Harness 结果包络校验器，需求分析、用例编写、项目技能和原始 JSON 导出统一核对任务、项目、Schema、结果任务 ID、Session、分析 ID 及来源文档范围。
- 需求分析已保存的历史 Harness 快照也经过同一结构核对后才渲染，缺失字段不会被补成兼容报告。
- 完成响应不匹配时只显示固定校验失败提示，不把旧任务或伪造结果渲染到结果区。
- 前端构建、API/脚本回归和隔离浏览器 Smoke 已通过；该切片不替代真实模型、Windows/Linux 或生产部署验收。
- 独立 `test-atelier-harness-extensions` 已整理为实现提交 `93c7e862449e909a26d0163f579b3707228a79c4` 及基线文档提交 `81a8862daba97e44860ff1bd0b2a257133b99a91`；锁定 Harness/Extension 基线校验已通过，后续同步有可复现的 Extension 起点。

### 2.1 DeepSeek Harness

- Node ESM/pnpm monorepo，根 `package.json` 固定 `pnpm@11.28.5`，要求 Node `>=24.9.0`。
- `apps/cli` 是正式 CLI 入口；`web`、`headless`、`sdk`、`sdk-minimal`、`acp` 是受支持的 Profile。
- `packages/core`、`packages/llm`、`packages/session`、`packages/skill`、`packages/api` 和 `packages/client` 提供 Agent、LLM、Session、Skill、RPC 和 Web Slot 能力。
- Cordis 以插件树组合运行时；Profile 和 patch 是扩展点，不能把 Test Atelier 业务代码写入上游核心。
- Session 是 append-only、可回放的模型执行记录；它不能替代 Test Atelier 的 SQLAlchemy/Alembic 业务数据。
- Harness Web 当前使用 React 18，Test Atelier 使用 React 19；第一阶段不直接合并两个根渲染树，优先使用独立 Shell、同源 Bridge 和可选 Slot 插件。

### 2.2 Test Atelier

- 后端为 FastAPI、SQLAlchemy、Alembic、SQLite/PostgreSQL，已有认证、项目成员、文档导入、分析、用例草稿、审核、转换、执行和导出能力。
- 前端为 React 19、Vite、TypeScript、Radix、Motion；现有页面和历史数据必须保持可用。
- 已有独立 Harness Host、ACP/Headless 适配、任务投影器、SSE、任务幂等/取消/确认/重试、Session 绑定、分段 map/reduce、结果 Schema、备份恢复和同源 Web Shell 试点代码。
- 已建立 `requirement-analysis.v1`、`test-cases.v1`、`skill-execution.v1` 三类契约，并把来源分析版本、项目、用户、技能和 Session 作为任务上下文的一部分。

### 2.3 当前验证边界

- Test Atelier API 与脚本测试、独立 Extension Host/预检测试、Fake Host 浏览器 Smoke、真实锁定 dsh 配回环确定性假模型的 macOS 闭环已有通过记录。
- 这些记录只证明协议、权限边界、结果投影、进程收尾、备份恢复和页面状态机，不证明真实外部模型语义质量。
- Windows/Linux 实机、Docker/PostgreSQL/Nginx/HTTPS、真实外部模型和整理后的可复现发布基线仍是开放门禁。

## 3. 不可变边界

### 3.1 仓库边界

允许修改：

- `test-atelier`：FastAPI Bridge、业务投影、数据库迁移、页面和测试。
- `test-atelier-harness-extensions`：Profile、Bundle、Plugin、Host、ACP/Headless 适配、网关、安装器、容器和跨平台脚本。

禁止在 Test Atelier 分支长期修改 Harness 的以下目录：

```text
vendor/
packages/core/
packages/llm/
packages/session/
packages/bundle/
packages/boot/
apps/cli/
apps/web/
scripts/
```

扩展只能使用 Harness 的公开包和公开启动入口，不导入私有 `src`/`lib` 路径，不复制上游源码，不把 SQLAlchemy 模型、项目 ACL、业务 Prompt 或业务状态机塞入 Harness。

### 3.2 数据所有权

| 数据/能力 | 权威系统 |
| --- | --- |
| 用户、登录、CSRF、项目、成员、项目技能开关 | Test Atelier |
| 任务归属、业务状态、分析快照、用例草稿、审核、发布、审计 | Test Atelier |
| Agent turn、模型输入输出、工具事件、技能事件、Session 回放 | Harness |
| API Key、Scope Secret、Session Secret | 外部 Secret Store/环境变量/Secret 文件 |

Harness Host 不直接访问 Test Atelier 数据库，不读取 Cookie，不执行任意 SQL。Node Host 只能通过明确的 Bridge 资源接口读写被授权的任务上下文。

## 4. 目标架构

```text
Browser
  -> Test Atelier React pages / optional Harness Web Shell
  -> same-origin API or restricted WebSocket gateway
  -> FastAPI authentication, CSRF, project ACL and task API
  -> SQLAlchemy/Alembic business tables
  -> Harness Bridge / Projector
  -> independent Node Host (headless or ACP Profile)
  -> DeepSeek Harness Agent, LLM, Skill, Tool and Session
```

### 4.1 任务生命周期

1. 用户在项目内选择需求材料、任务类型和已启用技能。
2. API 重新检查用户、项目、技能、分析版本和写权限，生成不可变任务上下文摘要。
3. Host 使用独立任务目录和 Session 目录启动锁定 Profile；模型 API 仅从环境变量或 Secret 文件读取。
4. Host 发送 `session_bound`、进度、确认、结果和终态事件；Projector 以远端序号幂等投影到 Test Atelier。
5. `task.result` 只能作为暂存结果；只有 `task.completed` 且 Schema、来源、摘要和权限再次校验通过，页面才读取 `/result`。
6. 失败、取消、超时、来源变更或技能停用时，结果接口返回空结果并保留低敏固定错误码；用户从最新分析重新发起任务。

### 4.2 三类任务

- `requirement-analysis.v1`：输出需求、验收条件、歧义和覆盖关系；人工歧义决定是独立业务操作，不改写 Harness 原始 Markdown。
- `test-cases.v1`：只能引用已完成且版本一致的 Harness 分析；输出用例、前置条件、步骤、预期、追溯和风险；完成后可生成只读结果或进入独立草稿审核。
- `skill-execution.v1`：仅允许白名单 `api-parsing`、`dev-doc-analysis`、`figma-to-prd` 等项目技能；技能停用、来源越界、脚本越界或工具能力超出沙箱时失败关闭。

## 5. 功能实施范围

### 5.1 第一阶段：基座冻结与适配层（P0）

- 锁定 Harness SHA、版本、Node、pnpm、lockfile 哈希和当前平台矩阵。
- 保持独立 Extension 仓库，完成 `test-atelier`、`test-atelier-acp`、可选 `test-atelier-web` Profile。
- 完成 Python Profile 安装器、Corepack 私有 shim、Windows 原生 Node 入口和 Host preflight。
- 完成 Bridge API：创建任务、详情、SSE、结果、取消、确认、重试、Session 回放和删除。
- 建立统一错误码、敏感信息脱敏、请求关联、幂等键和远端/本地序号。

验收：Fake Host、真实 dsh 加回环模型、刷新恢复、确认、取消、Session 回放和项目 ACL 全部通过；不修改 Harness 上游。

### 5.2 第二阶段：需求分析替换（P0）

- 新建分析只走 Harness；旧 Provider 仅保留历史读取。
- 保留材料上传、提取、来源文档 ID、版本和人工歧义确认。
- 结果区只读完成后的原始 `report_markdown` 和结构化结果；不显示未完成进度作为报告。
- 旧分析记录、历史报告和权限路径继续可读。

验收：未完成、失败、取消、来源变更、技能停用和跨项目读取均不能泄露或显示暂存结果；完成结果可下载、回放并审计。

### 5.3 第三阶段：用例编写替换（P0）

- 创建任务时固定分析 ID、版本、内容摘要、歧义决定摘要和发送上下文摘要。
- 分析版本或人工决定并发变化时拒绝旧任务投影，返回 `analysis_changed`，不生成草稿。
- 大材料采用有界窗口、map/reduce 和源文档偏移；中间报告不混入下一轮模型上下文。
- 页面展示 Harness 原始用例结果；编辑、审核、发布、导出放入明确的草稿审核入口。

验收：90 条以上材料、分段上下文、并发锁、失败重试、Session 绑定和结果来源校验通过；旧草稿读取不受影响。

### 5.4 第四阶段：项目技能统一入口（P1）

- 以 `skills/registry.json` 和 `project_skills` 为事实源，Harness 只执行当前项目已启用白名单技能。
- 保留 Hub 路由、Skill 阶段分离、safe-script 白名单、只读技能根、超时、进程组终止和 `.memory` 项目隔离。
- 统一输出 `skill-execution.v1`，支持报告、检查点、XMind 等原始交付，但交互检查点须真实模型验收。

验收：停用技能立即阻断新任务和未完成任务；越权技能、路径穿越、符号链接、Shell 写入和模型工具写入均失败关闭。

### 5.5 第五阶段：Web Shell 与交互统一（P1/P2）

- 第一阶段继续使用 Test Atelier 原生页面，Web Shell 仅作管理员 loopback 受限试点；结果区只读，任务写入仍经 Test Atelier API。
- 已完成管理员 loopback 同源代理、服务端 Session nonce 吊销和 Web Profile 原生设置/凭据/插件管理入口、原生 conversation/chat 入口及标准工具 preset 禁用；普通成员访问已增加显式 `TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS=1` 与 `TEST_ATELIER_WEB_SHELL_RESTRICTED_PROFILE=1` 双开关，仅允许拥有活跃项目成员关系的账号进入，项目 API 仍逐请求执行 ACL；生产预检在该开关开启时继续失败关闭，直到生产 Shell 能力边界完成验收。
- 通过 React 18/19 compatibility spike 后，使用 Harness 声明的 Slot/Plugin；不直接合并两个 React 根，不以 iframe 作为最终方案。
- 页面动画、过渡、消失和状态衔接遵循项目 UI 规范；动效不能掩盖权限、加载、错误和空状态。

验收：登录、刷新、登出、普通成员、只读成员、跨项目、插件卸载、上游更新和浏览器响应式行为均有真实浏览器证据。

### 5.6 第六阶段：交付可靠性与运维（P0/P1）

- API、Worker、Projector、Host 分离部署；任务租约、心跳、容量和故障恢复可观测。
- SQLite 用于本地开发；生产 PostgreSQL + Docker Compose + Nginx/HTTPS。
- 数据库、上传目录、Harness task/session 卷停写后联合备份；恢复后按任务、Session、结果和 ACL 对账。
- 项目删除、任务删除、终止未确认和旧 Session 迁移均采用可重试状态，不做强制清理。

验收：PostgreSQL 并发、容器故障、Projector 重启、Host 不可达、备份恢复和跨机器只读回放通过。

## 6. 模型与密钥策略

用户消息中提供的 API Key 已暴露，不能使用，必须先在服务端轮换或撤销。计划只保留以下非敏感默认配置：

```text
OPENAI_API_BASE=http://code.ugreencloud.com:8000/v1
OPENAI_MODEL=ugreen-ai-model
```

Key 只能通过环境变量、受限 Secret 文件或部署 Secret Store 注入；不得写入 Git、Profile、Compose、数据库普通字段、Session、任务 JSON、日志、浏览器 localStorage 或前端配置。真实模型质量验收必须使用轮换后的新凭据，并在验收后再次轮换。

## 7. 跨平台计划

| 平台 | 必须验证 |
| --- | --- |
| macOS | Node/pnpm 安装、Host/Projector 启停、ACP/Headless、进程组、浏览器、SQLite、备份恢复 |
| Windows | PowerShell/原生 Node 入口、ACL、`taskkill /T /F`、CIM 后代进程、Profile 安装、Session、浏览器、路径和 junction 防护 |
| Linux | Shell/容器入口、Docker Compose、PostgreSQL、Nginx/HTTPS、Secret 文件、Projector 多副本、恢复和权限 |

其他操作系统不纳入本轮兼容、部署和发布承诺。

## 8. 上游持续同步流程

1. 在干净 Harness checkout 添加官方 GitHub `upstream` remote，只执行 fetch。
2. 创建 `ta-harness-sync/<date>-<sha>` 分支；不使用 `reset --hard`、`clean`、强制覆盖或在脏工作树上合并。
3. 运行 Harness 自身 install/typecheck/build/相关测试，检查 Profile、Bundle、Session、RPC、LLM 配置和公开导出变化。
4. 在 Extension 重新安装依赖，运行 boundary、Host、预检、Profile、Bridge、Projector、浏览器和跨平台门禁。
5. 运行 Test Atelier API、迁移、权限、历史数据和前端回归；更新 `harness-baseline.json`、兼容矩阵和升级说明。
6. 任何门禁失败均保留旧 SHA；只有新 SHA 完成三平台证据后才能进入发布候选。
7. 回滚只切换 Harness/Extension 版本，不回滚业务数据库迁移；必要时使用双读/双写过渡。

## 9. 测试与发布门禁

### 自动化门禁

- Python：`pytest -q api/tests scripts/tests`、迁移与 Schema 回归。
- Extension：`npm test`、Python 预检、边界检查、Profile 安装测试。
- 前端：构建、lint、Playwright 页面状态、SSE、刷新恢复和权限。
- Harness：锁定 SHA 的 typecheck、相关 GUI/Session/Profile 测试和公开入口检查。
- 安全：密钥扫描、路径穿越、符号链接/junction、敏感错误回放、Shell/工具写入拒绝。

### 人工/实机门禁

- 真实轮换凭据 + 真实需求材料的 Gold 评审：覆盖率、歧义识别、验收条件、用例可执行性、追溯性和幻觉率。
- Windows 和 Linux 实机完整生命周期。
- Docker/PostgreSQL/Nginx/HTTPS、故障注入、备份恢复和跨机器只读回放。
- Web Shell 普通成员 ACL、长连接实时断开、插件卸载和上游升级；服务端 Session nonce 吊销已在本地 API 和网关回归中落地。

未完成人工/实机门禁前，不得将项目描述为生产就绪。

## 10. 交付物与当前阻塞

### 计划交付物

- 独立 Extension 仓库及三平台 Profile/Host/Projector。
- Test Atelier Bridge、任务/结果 Schema、迁移、页面和兼容历史入口。
- `docs/harness-*` 协议、边界、兼容、质量、Web Shell、备份恢复文档。
- 兼容矩阵、发布基线、升级/回滚说明、三平台验收记录和真实模型 Gold 报告。

### 当前必须先解决

1. 轮换用户消息中暴露的 API Key，完成真实模型质量评审。
2. 获取 Windows 实机证据：Profile、Host、ACP 确认、进程树、Session、ACL 和浏览器。
3. 获取 Linux/容器证据：Docker 构建、Compose、PostgreSQL、Nginx/HTTPS、Secret 和 Projector 故障恢复。
4. 完成 Web Shell 生产安全设计：普通成员 ACL、生产 Shell 能力限制和目标环境长连接验证；服务端 Session nonce 吊销及原生设置/凭据入口禁用已实现。
5. 整理 Extension 工作树后重新运行发布基线；脏工作树不能作为锁定发布版本。

## 11. 实施原则

- 先保证数据和权限不丢，再替换 AI 执行路径。
- 原始值、Harness 结果、人工决定、最终发布值分离保存。
- 任何失败都可重试但不可伪造成功；任何删除都可恢复但不可绕过停写和归属核对。
- 上游更新优先通过 Extension 适配，禁止为 Test Atelier 业务修改 Harness 核心。
- 真实运行证据、静态检查、回环模型和人工质量评审必须分开报告，不相互冒充。
