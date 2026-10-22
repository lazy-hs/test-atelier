# Test Atelier 架构与工程手册

> 本文是**工程视角**的技术文档，回答"代码怎么组织、数据怎么流转、改哪里会牵动什么"。
> 操作视角看 [USAGE.md](USAGE.md)，部署与配置看根 [README.md](../README.md)，生产排障看 [OBSERVABILITY.md](OBSERVABILITY.md)。
> 接口细节以 API 启动后的 `http://127.0.0.1:8000/docs` 为准。

---

## 1. 项目定位

**Test Atelier** 是面向个人和团队的测试资产工作台：把需求材料、AI 分析、用例编写、人工审核、用例治理和格式转换放在同一个项目空间中。

三条设计底线：

1. **AI 只是候选**。AI 产出永远是建议/草稿，关键歧义未人工确认会阻断下游生成，模型离线时人工流程照常可用。
2. **原值 / 建议 / 最终值分离**。任何自动结论都不直接改写人工结论，所有决策留痕可追溯。
3. **可降级运行**。AI 服务不可用时，登录、项目协作、导入审核、转换和历史下载仍独立可用。

产品语境：优先服务 **NAS 云存储 / 文件管理 / 多端同步** 类产品（`api/ai_rules/` 下的规则文档按此语境编写）。

---

## 2. 技术栈

| 层 | 选型 | 版本约束 |
| --- | --- | --- |
| 后端 | FastAPI + SQLAlchemy 2.0（`DeclarativeBase`/`Mapped`）+ Alembic | `sqlalchemy>=2,<3` |
| 认证 | Cookie Session（`SessionMiddleware`）+ Argon2 口令哈希 + 双提交 CSRF | `argon2-cffi>=23,<26` |
| 数据库 | SQLite（开发）/ PostgreSQL + `psycopg`（生产） | `psycopg[binary]>=3,<4` |
| 表格 IO | `openpyxl` / `xlrd` / `xlwt` | `.xlsx` / `.xls` 读写 |
| 文档解析 | `pypdf` + `pypdfium2`（PDF）、`python-docx`（docx）、`Pillow`+Tesseract（OCR）、LibreOffice（`.doc`） | 均为可选外部依赖 |
| 加密 | `cryptography` Fernet（远程密钥落库加密） | Linux/Apple Silicon 用 50+ 系列；Intel macOS 仅开发 |
| 前端 | React 19 + Vite 8 + TypeScript 6 + Tailwind v4（CSS-first）+ Radix UI + Motion | Node 20+ |
| 前端 Lint | Oxlint（非 ESLint） | — |
| E2E | Playwright（本机 Chrome，`channel: 'chrome'`） | 需 `AI_STUDIO_E2E_PASSWORD` |

---

## 3. 仓库结构

```text
api/                    后端包
  main.py               应用装配 + 内联端点（2111 行）+ 权限依赖
  db.py                 全部 ORM 模型（639 行 / 34 张表）
  ai_engine.py          模型 provider 抽象（Ollama / 远程 / Codex CLI / Claude Code）
  ai_jobs.py            AI 任务持久化、入队、容量预留、恢复
  worker.py             独立 worker 进程（数据库即队列 + 租约 + 心跳）
  skill_runner.py       外部 qa-doc-skills 技能仓发现与脚本执行
  ai_routes.py          AI 工作流路由（需求分析 / 草稿 / Codex 会话 / 模型配置）
  suggestion_routes.py  分类法 + 规则/模型建议
  smoke_routes.py       冒烟套件与执行（后端保留，前端入口已下线）
  conversion_routes.py  XMind/Excel 互转与项目历史
  governance_routes.py  用例治理（新）
  governance_core.py    治理纯函数算法（新，不依赖 ORM，便于回归）
  document_extract.py   需求文档文本抽取（含安全边界校验）
  classification.py     分类规则候选与模型候选
  importers.py          .xls/.xlsx/.xmind 导入解析
  storage.py            安全文件 IO 原语（私有权限原子写、防符号链接）
  observability.py      结构化日志、脱敏、请求 ID、故障窗口
  metrics.py            Prometheus 指标收集与文本渲染
  ai_rules/             5 份业务规则文档（喂给模型的领域知识）
  migrations/versions/  Alembic，20 个 revision，head=20261032_case_timestamps
  tests/                27 个测试文件
web/                    前端
  src/App.tsx           登录态 + 路由解析 + 用例域状态中枢（355 行）
  src/components/       页面组件 + components/ui（12 个 Radix 原语）
  src/converter/        用例转换子应用（JSX/JS，Shadow DOM 隔离，1234 行）
  src/lib/api.ts        fetch 封装 + 全项目唯一类型定义（169 行）
  e2e/                  9 个 Playwright spec
scripts/                环境配置/启动、生产前置检查、部署 smoke、备份、日志校验
ops/prometheus/         可直接纳入 rule_files 的告警规则示例
config/                 settings.example.json（运行时配置实际写 data/config/）
data/                   运行时数据：ai_studio.db、documents、conversions、governance-snapshots
```

---

## 4. 后端架构

### 4.1 应用装配与路由

`api/main.py:608` 创建 `FastAPI(title="Test Atelier", version="0.2.0")`。

**路由注册采用"工厂 + 依赖注入"而非全局单例**（`main.py:2100-2111`），把 `get_db`/`current_user`/`csrf`/`project_access`/`writable` 等依赖显式传给 `build_*router()`，以此打破循环导入：

| 工厂 | 文件 | 前缀 | 域 |
| --- | --- | --- | --- |
| `build_router` | `conversion_routes.py:86` | `/api/v1/projects/{id}/conversion` | 用例转换 |
| `build_ai_router` | `ai_routes.py:298` | `/api/v1/projects/{id}` | AI 工作流 |
| `build_suggestion_router` | `suggestion_routes.py:78` | `/api/v1/projects/{id}` | 分类建议 |
| `build_smoke_router` | `smoke_routes.py:68` | `/api/v1/projects/{id}` | 冒烟执行 |
| `build_governance_router` | `governance_routes.py:163` | `/api/v1/projects/{id}/governance` | 用例治理 |

`main.py` 内联端点分组：

| 分组 | 行号 | 端点 |
| --- | --- | --- |
| 健康检查 | 739/740/749/779 | `/health`、`/health/live`、`/health/ready`、`/health/worker` |
| 监控 | 864 | `/api/v1/metrics`（PlainText，Prometheus） |
| 认证 | 1217/1224/1252/1261 | `/auth/csrf`、`/auth/login`、`/auth/logout`、`/auth/me` |
| 工作板 | 1284/1325/1352/1386 | `/workboard` 的增改查删（**用户级，非项目级**） |
| 项目 | 1395-1512 | 列表/创建/改档/归档/恢复/复制/删除 |
| 项目技能 | 1523/1532 | 技能注册表查询与启停 |
| 概览 | 1546 | `/projects/{id}/overview` |
| 用户与成员 | 1603-1725 | 建用户、改密码、成员增删改查 + 变更事件流 |
| 导入 | 1736/1768/1797 | 预览 / 确认 / 列表 |
| 用例 | 1851-1959 | 列表/批量删/单删/批量审核/详情/改派 |
| 协作 | 1994/2011/2027 | 活动流 / 评论 / 审核流转 |
| 导出 | 2045 | openpyxl 流式 xlsx |

### 4.2 核心模块职责

| 模块 | 职责 | 关键符号 |
| --- | --- | --- |
| `ai_engine.py` (968) | 四通道 provider 统一为 `request_structured(prompt, schema, provider)` | `request_structured:812`、`_run_cli:393`、`stream_cli_text:659`（SSE）、`test_cli_connection:745`、`encrypt_secret/decrypt_secret:114/118`；契约 `Requirement:205`、`Ambiguity:214`、`AnalysisOutput:221`、`DraftOutput:232`；入口 `analyze:900`、`generate_cases:935`、`assess_cases:941` |
| `ai_jobs.py` (491) | 任务持久化和入队，进程内线程池 或 外部 worker 二选一 | `enqueue:223`、`process_job:240`、`recover_jobs:472`；容量 `reserve_external_slot:151`、`release_external_slot:206`；Prompt 留痕 `_PromptCapture:34` |
| `worker.py` (493) | 独立常驻消费者，数据库即队列 | `claim_next:217`、`_claim:199`、`_renew_lease:241`、`_lease_loop:264`、`_heartbeat:223`、`_recover_running:183`、`run:393` |
| `skill_runner.py` (305) | 加载外部 `qa-doc-skills`：Hub 路由 → 唯一子 Skill → 脚本 | `discover_skills:84`、`load_context:120`、`run_script:162`（白名单 `_safe_script:135`）、`generate_xmind:232`、`analyze:245`、`generate_cases:284` |
| `governance_core.py` (251) | **纯函数**治理算法，只吃 dict，不依赖 ORM/存储 | `name_key:46`、`fingerprint:55`、`predict_domain:94`、`score:114`、`classify_group:130`、`analyze:139`、`validate_rows:193` |
| `document_extract.py` (217) | 需求文档抽取 | `extract:125`、`_docx:89`、`_ocr:75`、`_validate_docx_archive:98`（zip-slip/解压炸弹防护）、`_run_external:54`（超时 kill 进程组） |
| `classification.py` (118) | 规则候选 + 模型候选 | `rule_candidate:75`、`model_candidate:103`、`validate_categories:60` |
| `importers.py` (173) | 多格式导入 | `parse:144` → `_xls:39`/`_xlsx:61`/`_xmind:69` |
| `storage.py` (129) | 安全文件 IO | `write_private_atomic:62`、`open_private_regular_file:106`（防符号链接穿越）、`ensure_private_directory:11` |
| `observability.py` (461) | JSON 日志、脱敏、请求 ID、日志风暴抑制 | `configure_logging:392`、`JsonFormatter:320`、`redact_sensitive_text:137`、`sanitize_log_fields:185`、`FailureWindow:213` |
| `metrics.py` (261) | Prometheus | `observe_request:59`、`set_ai_queue:74`、`set_worker_health:82`、`set_database_pool:97`、`observe_model:106`、`render_prometheus:137` |

### 4.3 数据模型（34 张表，按域）

**身份 / 项目 / 成员**

| 表 | 关键字段 |
| --- | --- |
| `users` | `username` unique、`password_hash`(Argon2)、`role` ∈ admin/reviewer/reader |
| `projects` | `name`、`archived_at`（软归档，归档后只读） |
| `memberships` | 复合 PK `(project_id, user_id)`、`role` ∈ manager/reviewer/reader |
| `membership_events` | `before_role`/`after_role`/`actor_id` — 成员变更审计 |
| `project_skills` | PK `(project_id, skill_id)`、`source_path`、`enabled` |
| `work_items` | **工作板**：归属 `user_id`（非项目）、`kind`/`status`/`start_at`/`due_at`/`completed_at` |

**用例 / 审核 / 分类 / 建议**

| 表 | 关键字段 |
| --- | --- |
| `cases` | `title`、`directory`、`level`/`smoke`、`original`(JSON 原行)、`final_level`/`final_smoke`、`review_status`、`version`（乐观锁）、`assignee_id`、`final_categories`；治理扩展：`platform_id`、`name_key`、`governance_status`(active/retired)、`domain`/`subdomain`、`endpoint`、`steps`/`precondition`/`expected`/`remark`、`fingerprint`、`content_hash`、`merged_into` |
| `review_events` | `before`/`after` JSON、`action`、`reason` |
| `case_comments` | 用例级讨论，不改版本 |
| `taxonomy_versions` | `number`、`status` draft/published（发布后不可变）、`revision`、`definitions` JSON |
| `suggestion_jobs` | `status`、`case_ids`、`total`/`completed`、**租约三件套**、`queue_reserved`、`request_id` |
| `case_suggestions` | `source` rule/model、`categories`、`level`/`smoke`、`rationale`、`decided_by/at/reason` |
| `import_batches` | `headers`/`rows`/`warnings` JSON、`kind`(workspace/governance)、`sha256`、`snapshot_path`、`rules_hash` |

**需求分析 / 草稿 / AI 任务**

| 表 | 关键字段 |
| --- | --- |
| `source_documents` | `extracted_text`、`warnings` |
| `analysis_runs` | `source_ids`、`status`、`provider`、`result` JSON（需求/歧义）、`version` |
| `case_drafts` | `analysis_id`、`payload`、`status` draft/published、`case_id` |
| `ai_jobs` | `kind`、`status`、`request_id`、`lease_owner`/`lease_until`/`lease_heartbeat`、`queue_reserved`、`prompts`/`prompt_sha256`（Prompt 全量留痕） |
| `ai_queue_state` | **单行 `id=1`**，`active_jobs` — 跨副本原子容量计数器 |
| `ai_settings` / `ai_models` / `ai_tools` | local/remote/codex/claude 四通道；models 带 `protocol`、`api_key_encrypted`；tools 为 `codex_cli`/`claude_code` |

**转换 / 冒烟 / Codex**

| 表 | 关键字段 |
| --- | --- |
| `conversion_settings` / `conversion_records` | 默认前缀路径、`history_retention_days`(180)、`preview` JSON |
| `smoke_suites` / `smoke_items` | 唯一 `(suite_id, case_id)`、`case_version`、`chain_name`、`estimated_seconds`、`revision` |
| `execution_runs` / `run_cases` / `execution_events` | 执行期**字段快照**（title/level/smoke 冗余存）、`result` not_run/pass/fail/blocked/skip、`before`/`after` 审计 |
| `codex_sessions` / `codex_messages` | 会话 PK `String(64)`、`working_directory`（隔离工作区）、追加式消息 |

**治理（新增 7 张表）**

| 表 | 关键字段 |
| --- | --- |
| `governance_duplicate_groups` | `gid`(D001…)、`name_key`、`size`、`cluster_count`、**`category` A/B/C**、`suggested_discard_count`、`status`、`revision`（乐观锁） |
| `governance_duplicate_members` | `case_id`、`cluster_fp`、`score`、`decision` keep/merge/discard/retired |
| `governance_validation_runs` | `rows`、`result` JSON、`status` blocked/passed |
| `governance_directories` | PK `(project_id, path)`、`parent`/`level`、`domain`/`subdomain`、`whitelisted`、`frozen` |
| `governance_change_logs` | `entity`、`before_json`/`after_json`、**`batch_key`**（批量撤销单元）、`revert_of` 自引用 |
| `governance_metric_snapshots` | `key`/`value` 体检指标快照 |
| `governance_export_jobs` | `kind`/`format`、`path`、`rows`、`selfcheck` JSON |

### 4.4 权限模型

三层角色，全部通过 `Depends` 函数校验（`main.py:912-957`），**无自定义装饰器**：

| 依赖 | 逻辑 |
| --- | --- |
| `current_user:912` | 读 `session["uid"]`，无则 401 |
| `csrf:920` | `compare_digest(session["csrf"], headers["x-csrf-token"])`，不匹配 403；只挂在写操作 |
| `project_access:927` | 项目存在；`admin` 直通，否则必须有 `Membership` |
| `project_role:936` | 先过 `project_access`，admin 返回 `"admin"` |
| `writable:943` | 归档项目 409；`reader` 403 |
| `project_manager:951` | `role not in (admin, manager)` → 403 |

业务级细粒度补充：
- `main.py:1829` reviewer 只能操作 `assignee_id in (None, self)` 的用例
- `main.py:1695/1715` 禁止非 admin 改动 admin/manager 成员
- `ai_routes.py:349` Codex 会话额外校验 `session.user_id == user.id`
- `governance_routes.py:99` apply/undo 限 admin/manager；decide 只需 writable

### 4.5 AI 任务队列与 Worker

两种运行模式（`AI_STUDIO_EXTERNAL_WORKER`）：

```text
内置模式（开发默认）  HTTP 进程 → ThreadPoolExecutor(max_workers=2) → 直接跑模型
外部模式（生产）      HTTP 进程只写库 → api.worker 进程领取 → 跑模型
```

**提交链路**（外部模式）：
1. 路由创建 `AnalysisRun` + `AiJob(status=queued)`，并从 HTTP 请求复制 `request_id`
2. `reserve_external_slot` 先给 job 打 `queue_reserved` 标记（防重试重复占额）
3. 对 `ai_queue_state` 单例行做**条件自增** `UPDATE ... WHERE active_jobs < MAX` → 跨 API 副本原子准入
4. 容量耗尽 → 任务标记队列满，返回 `503`

**领取与租约**（`worker.py:199`）：条件更新 `WHERE id=? AND status='queued'`，`rowcount != 1` 即抢锁失败；优先级先 `ai` 后 `suggestion`。

| 常量 | 默认值 |
| --- | --- |
| `POLL_SECONDS` / `HEARTBEAT_SECONDS` | 1s / 5s |
| `LEASE_SECONDS` | 900s（必须大于 CLI 与技能脚本超时） |
| `RECOVERY_INTERVAL` | `clamp(LEASE/3, 5, 60)` |

**心跳有两个层次**，容易混淆：
- 任务租约续期（`_lease_loop`）：续期失败 → 置 `lease_lost`、**立即停止模型调用**，防止租约过期后被他人重领导致重复写
- 进程存活心跳（`_heartbeat_loop`）：写 `data/.worker-heartbeat` 文件；**关闭时刻意不删除**（多副本共享路径，删文件会误删健康副本心跳）

**取消传播**：SIGTERM → `stop_event` 注入 `ai_engine.set_cancel_event` → CLI 子进程随 worker 退出，不把 180s provider 超时带入 25s 停止宽限。
**崩溃恢复**：`_recover_running` 只回滚 `lease_until <= now` 的 running 任务，启动时跑一次，之后每 `RECOVERY_INTERVAL` 跑。

### 4.6 文档解析与安全边界

- 上传文件按私有权限原子写入（`write_private_atomic`），下载用不跟随符号链接的 fd
- `.docx` 解析前校验压缩包：路径穿越、条目数、解压后大小（防 ZIP 炸弹）
- OCR / LibreOffice / CLI / 技能脚本一律**无 shell 参数数组**启动，并剥离应用会话密钥、数据库地址、远程 API Key、指标令牌、技能仓路径和云凭据；CLI 还移除 `AI_STUDIO_*` 与桌面 app-server 的 `CODEX_*`，避免后台任务接入交互式桌面会话
- 超时一律**终止整个进程组**，不留残留子进程

---

## 5. 前端架构

### 5.1 构建

- Vite dev 代理 `/api` → `http://127.0.0.1:8000`（可用 `AI_STUDIO_API_TARGET` 覆盖）；**未配 `ws: true`**，SSE 走普通 HTTP 流
- 手动分包：`react-vendor` / `motion-vendor` / `icons-vendor`，避免共享入口超 500 kB
- 路径别名 `@/* → ./src/*`；`allowJs: true`（为 converter 的 JSX 放行）

### 5.2 页面地图

**工作区概览**
| 组件 | 职责 |
| --- | --- |
| `HomePage.tsx` | 工作台首页：4 个指标卡 + 数据资产 + 快捷入口 + 最近活动时间线 |
| `WorkboardPage.tsx` | 我的看板，**唯一跨项目页面**（走用户级 `/workboard`） |

**需求 → 用例 AI 生产**
| 组件 | 职责 |
| --- | --- |
| `RequirementsPage.tsx` | 材料上传、Markdown 报告编辑、歧义项确认、3s 轮询任务；用 `reportRevision` 防止轮询覆盖未保存编辑 |
| `AuthoringPage.tsx` | 草稿生成（关键歧义未确认则阻断）、结构化编辑（模块/P0-P3/设计方法 EP·BVA·ST·EG）、发布、导出 XMind/Excel |
| `PromptViewerDialog.tsx` | 查看实际发送给模型的完整提示词（新接口失败回落旧接口） |

**用例库与审核（权限最密）**
| 组件 | 职责 |
| --- | --- |
| `CaseTable.tsx` | 分页 30/页、三重筛选、跨页累积勾选、批量操作栏 |
| `ReviewPane.tsx` | 三 Tab 抽屉：审核/建议/讨论。**核心规则**：reviewer 遇到已分派给他人的用例 ⇒ 控件禁用 |
| `SuggestionPanel.tsx` | rule/model 双来源候选，采纳前可修改，过期建议不可采纳，**决策必填理由** |
| `BatchReviewDialog.tsx` | 逐条返回 `saved/unchanged/conflict/forbidden/invalid/not_found` |
| `ImportDialog.tsx` | 两段式：上传 → 预览映射 → 确认 |

**治理与配置**
| 组件 | 职责 |
| --- | --- |
| `GovernancePage.tsx` | 5 Tab：体检看板 / 重复治理(A·B·C) / 入库闸门 / 目录治理 / 回归与导出 |
| `TaxonomyPage.tsx` | 四维分类法草稿-发布版本模型，`revision` 乐观锁 |
| `TeamPage.tsx` / `SkillsPage.tsx` / `ModelSettingsPage.tsx` | 成员（自保护：不能改自己）、技能开关、模型配置（**仅 admin 可见**） |

**AI 协作与转换**
| 组件 | 职责 |
| --- | --- |
| `CodexPage.tsx` | **全项目唯一 SSE 消费者**；`AbortController` 中止本轮 |
| `converter/` | 1234 行 JSX/JS 子应用，Shadow DOM 隔离，4 种转换模式 |

**已软下线**：`SmokePage.tsx`（269 行）全仓库无 import，但类型保留在 `api.ts`，`e2e/smoke.spec.ts` 反向断言它不在导航中 —— 有意保留以便回滚。

### 5.3 路由与状态

路由不是声明式的，而是**正则 + 三元链**（`App.tsx:78`）：

```ts
location.pathname.match(/^\/projects\/(\d+)\/(home|cases|imports|governance|conversion|
  requirements|authoring|taxonomy|team|skills|model-settings|codex|workboard)$/)
```

- 13 个 view 名硬编码在正则里，view 驱动超长三元选组件
- **无 Context / 无 Redux / 无全局 store**。跨 App 数据靠 `api.ts` 导出的类型共享
- 项目切换：多数子页面带 `key={projectId}` 强制重挂载，避免跨项目脏状态；`HomePage` 不传 key（靠 `overview?.project.id` 防御）
- 有效角色由 `projects.find(id)?.role` **派生**，不单独存

**并发与竞态三件套**（代码里反复出现，改代码时要沿用）：
1. `requestId = useRef(0)` 递增对比 → 丢弃过期 reload
2. `let active = true; return () => { active = false }` → 卸载守卫
3. 乐观锁：写操作带 `version`/`revision`，服务端冲突报错含"刷新"则自动重拉最新

### 5.4 API 层（`src/lib/api.ts`，169 行）

**既是 fetch 封装，也是全项目唯一的类型定义文件**（无独立 `types.ts`，40+ 导出类型）。

```ts
rawApi(path, init)   // 非 FormData 自动 JSON；非 GET 自动注入 x-csrf-token；
                     // credentials: 'same-origin'；错误统一为 {detail} 并附 x-request-id
api<T>(path, init)   // 泛型 JSON 包装
streamApi(...)       // SSE：复用 CSRF/鉴权，手工 \n\n 分帧
refreshCsrf() / login() / upload() / formatAiError()
```

### 5.5 设计体系

双轨并行：Tailwind v4 `@theme inline`（`index.css`）+ 817 行手写 `App.css`。

- 白/黑/蓝三元色系：底 `#fff`、描边 `#e6eaf0`/`#edf0f3`、主蓝 `#165ed8`、蓝调浅底 `#edf4ff`/`#f8fbff`、灰阶文字 `#8290a0`/`#171d26`、错误 `#bb3030`
- 响应式断点统一 `max-width: 720px`
- `prefers-reduced-motion` 下暂停粒子/呼吸/网格漂移等环境动画
- `.dark` 类定义完整但**无切换入口**，深色模式属预置未启用资产
- converter 子应用有独立 `converter.css`（略偏紫灰），由 Shadow DOM 完全隔离

---

## 6. 用例治理子模块（在研，未提交）

### 6.1 数据流

```text
监控中心快照导入 → sha256 去重 → 落盘 data/governance-snapshots/ → validate_rows(mode=legacy)
   → upsert 到 cases（填充 name_key/domain/fingerprint/content_hash）→ _sync_groups + _metrics
   → 重复组 A/B/C 分类 → 人工 decide → apply（写 ChangeLog）→ 可按 batch_key 撤销
```

### 6.2 A/B/C 分类算法（`governance_core.py:130`）

```python
def classify_group(rows):
    directories = {用例目录 for row in rows}
    if len(directories) < len(rows):
        return "A"                                    # 同组内目录重复 → 真重复
    sims = [SequenceMatcher(None, signature(l), signature(r)).ratio() for pairs]
    return "B" if max(sims) >= 0.90 else "C"          # 步骤高度相似 → B；否则 C
```

- 分组键 `name_key`：NFKC 归一 + casefold + 去所有非字母数字（中英混排健壮）
- 组内二次聚类 `fingerprint` = md5(步骤|预期结果)
- 打分 `score`：状态（正常 +3 / 待更新 +1 / 已废弃 −6）+ 等级（1→+2 … 4→−1）+ 有预期结果 +2 + 有前置条件 +1 + 目录深度≤6 +1 −（含"废弃" −6 / "自测" −3）+ 目录与用例名预测域一致 +2
- 决策：每 cluster 保留 score 最高者；cluster 内其余 A→`discard`、B/C→`merge`；keeper 源状态为已废弃则标 `retired`
- 领域映射 `predict_domain`：10 条关键词 → 域（09_权限与隐私、01_设备接入、08_云台与对讲…），未命中返回「（未匹配，需人工归类）」

### 6.3 可撤销治理

```
decide → 写 member.decision + ChangeLog(batch_key, before/after)
apply  → 按 decision 改 Case.governance_status/merged_into + ChangeLog(entity="case")
undo   → 仅当 case.governance_status == log.after_json[...] 才回滚 before_json（防覆盖后续人工改动）
```

- 乐观锁：`expected_revision` / `expected_revisions` 与 `group.revision` 比对，冲突 409
- `batch_key = uuid4().hex` 串联一次批量操作的所有日志，是**撤销的原子单元**
- apply 语义：`keep` → active、`merge` → retired 且 `merged_into` 指向 keeper、`discard`/`retired` → retired；支持 `preview=true` 只返计数

### 6.4 其余治理端点

| 端点 | 能力 |
| --- | --- |
| `GET /rules` | 只读暴露 `rules.json` + 白名单（root 路径、max_depth=6、8 必填字段、9 种用例类型、禁用目录词、备注标签正则 `[迭代:\d{8}]`） |
| `POST /validations` | `mode=new|legacy`：必填/枚举/目录根/深度/白名单/禁用词/重名/相似名(≥0.85)/ID 冲突/标签/步骤编号/步骤-预期条数一致/前置条件/冒烟等级/端类型 → 输出 `blocked`/`warnings`/`can_promote` |
| `/directories/*` | 目录树、移动、迁移、白名单应用 |
| `GET /regression` | 按迭代/等级/域筛选回归集 |
| `POST /exports` + `selfcheck` | 导出 XLSX/XLS + 回读自校验 |

---

## 7. AI 规则库（`api/ai_rules/`）

喂给模型的领域知识，5 份文档：

| 文档 | 内容 |
| --- | --- |
| `PARSING-RULES.md` | 需求文档结构识别（PRD / 用户故事等格式）、场景提取模式 |
| `TEST-DESIGN-METHODS.md` | 核心 4 法 EP / BVA / ST / EG + NAS 云存储增强测试对象 |
| `TEST-CASE-RULES.md` | 功能测试之外的**扩展维度**与 Phase 2 / 2.8 缺口扫描检查清单（文件数据安全、权限隐私、多端同步、共享协作、大文件/批量/并发、容量与系统依赖、异步索引/转码/AI） |
| `TEST-PRIORITY.md` | P0-P3 优先级定义、判断规则、回归策略 |
| `TRACEABILITY.md` | 需求-用例双向追溯矩阵结构与覆盖率计算 |

产品语境明确：**NAS 云存储 / 文件管理 / 多端同步**。修改这些文档会直接改变模型输出风格，属于影响面较大的改动。

---

## 8. 可观测性要点

- 统一单行 JSON 日志：必带 `timestamp`/`schema_version`/`level`/`service`/`environment`/`request_id`
- HTTP 请求贯穿 `X-Request-ID`；异步 AI 任务持久化创建请求 ID，重启恢复后仍可关联
- 非 HTTP 事件（启动、worker、备份、迁移）用 `request_id="-"`
- **绝不落盘**：请求正文、上传文件、提示词、模型响应、Cookie、Authorization、密钥、SQL 绑定参数
- SQLAlchemy/HTTPX/urllib3/multipart 的低级日志强制 `WARNING` 以上，防 `DEBUG` 泄露
- 采集前跑 `scripts/validate_log_stream.py` 校验 envelope（还拒绝重复键、限制单行 64 KiB 和嵌套深度）
- 关键指标：`test_atelier_ai_jobs_active/capacity`、`worker_up`、心跳年龄 vs `worker_heartbeat_threshold_seconds`、DB 池 `checked_out/size`、`model_requests_total{outcome=...}`、`cli_available`
- 告警规则示例：`ops/prometheus/test-atelier.rules.yml`（`up`/`probe_success` 的 job 名需按实际 scrape 调整）

---

## 9. 环境、配置与启动

### 9.1 两步式启动（配置与启动分离）

```bash
./scripts/configure_environment_macos.command   # 建环境、装依赖、生成密钥、执行迁移
./scripts/start_macos.command                   # 只校验并启动 API + Vite
```

Windows / Linux 有对应 `.ps1|.bat` / `.sh`。启动脚本会**显式关闭 `AI_STUDIO_EXTERNAL_WORKER`**，避免继承生产变量后任务进入无人消费的持久队列。

手动方式：

```bash
python -m api.uvicorn_entrypoint --host 127.0.0.1 --port 8000   # 终端一
cd web && npm run dev -- --host 127.0.0.1                        # 终端二
```

### 9.2 配置持久化

运行时配置写 `data/config/settings.json`（`AI_STUDIO_CONFIG_FILE` 可改路径），私有权限 + 临时文件 `os.replace` 原子写。
`models` 保存本地/远程/CLI provider 与供应商目录；远程密钥**只存加密密文**；`skills` 只存 ID/名称/路径/描述，不复制技能正文。

> 轮换 `AI_STUDIO_SESSION_SECRET` 会让旧密文无法解密 → 记录 `ai.settings.secret_unavailable` 并拒绝使用 → 须重新保存远程模型配置。

### 9.3 必记的环境变量

| 变量 | 要点 |
| --- | --- |
| `AI_STUDIO_SESSION_SECRET` | 必填 ≥32 字符，且**每次启动必须一致**，否则现有会话失效 |
| `AI_STUDIO_ADMIN_PASSWORD` | 开发默认 `123456`；生产必填 |
| `AI_STUDIO_EXTERNAL_WORKER` | `1` = API 只写队列，由独立 worker 消费（Compose 默认开） |
| `AI_STUDIO_WORKER_LEASE_SECONDS` | 900，必须 > CLI 与技能脚本超时 |
| `AI_STUDIO_CLI_TEST_TIMEOUT` | 必须 ≤ `AI_STUDIO_CLI_TIMEOUT` |
| `TEST_ATELIER_SKILL_ROOT` | 技能仓根，默认 `/Users/lazy/huangsheng/project/qa-doc-skills`；Compose 必须显式挂载 |
| `AI_STUDIO_REQUIRE_MIGRATIONS` | 生产强制 `1`，不能靠设 `0` 回退到隐式 `create_all` |

完整列表见根 README 的配置表。

---

## 10. 测试与 CI

### 10.1 后端

```bash
python -m pytest -q      # 当前基线 361 passed
```

27 个测试文件，覆盖：AI 设置与工作流、CLI provider、Codex 工作台、转换、文档抽取、治理（core + api）、迁移、队列容量、审核协作、技能 runner、冒烟、存储、建议、团队、worker（含独立 worker 启停）、uvicorn entrypoint。

### 10.2 前端

```bash
cd web && npm run build && npm run lint
```

**E2E 需真实 API + Vite + 本机 Chrome**：

```bash
cd web
AI_STUDIO_E2E_PASSWORD='管理员密码' npm run test:e2e
```

9 个 spec：`workstation`（冒烟主链 + 移动端 390×844）、`workflow`、`collaboration`、`model-settings`、`authoring`、`suggestions`、`model-suggestions`、`team`、`smoke`（反向断言冒烟入口已移除）。

> **踩坑**：每个 spec 开头 `if (!password) test.skip(...)`，未设环境变量时**整文件跳过**，CI 会全绿但实际未执行。附加变量：`AI_STUDIO_E2E_URL`、`AI_STUDIO_E2E_SAMPLE`、`AI_STUDIO_E2E_MODEL_STUB=1`。

### 10.3 CI（`.github/workflows/ci.yml`）

push / PR 触发，执行：后端测试与编译检查、`pip check`、Linux 依赖审计、前端依赖高危漏洞审计、前端构建/lint、构建后 SPA preview smoke、Compose 插值校验、API/Web 镜像构建（Web 还跑 `nginx -t`）、单进程 API smoke、SQLite 独立 worker 启停 + JSON 日志契约校验、一次 PostgreSQL + worker + Nginx 的 Compose smoke、临时 PostgreSQL 上的备份/校验/恢复演练。

**尚未覆盖**：真实浏览器、OCR、真实模型、PostgreSQL 多副本、目标环境恢复演练。

---

## 11. 当前工作区状态（相对 `HEAD=9fbc652`）

**未跟踪（新增）**：`api/governance_core.py`、`api/governance_routes.py`、`web/src/components/GovernancePage.tsx`、`web/src/components/WorkboardPage.tsx`、`api/migrations/versions/2026102{9_workboard,30_workboard_schedule,31_case_governance}.py`、`api/tests/test_governance_{core,api}.py`

**已修改未提交**：`api/db.py`、`api/main.py`、`api/requirements.txt`、`api/worker.py`、`api/tests/test_migrations.py`、`compose.yaml`、`scripts/preflight_production.py`、若干 `setup_and_start_*` 脚本、`web/src/App.tsx`、`web/src/App.css`、`web/src/lib/api.ts`、`README.md`、`.env.example`

即：**本地 head 领先 origin/master 三个迁移版本**（工作板 → 工作板排期 → 用例治理），提交前需补跑 `python -m pytest -q` 与 `test_migrations.py` 的 head 断言。

---

## 12. 开发约定与已知陷阱

| 陷阱 | 说明 |
| --- | --- |
| `main.py` 路由注册在文件末尾 | 新增路由模块必须走 `build_*router(依赖...)` 工厂，不要直接 import 依赖函数，否则循环导入 |
| 写操作必须挂 `Depends(csrf)` | 前端 `api.ts` 对非 GET 自动注入 `x-csrf-token` |
| 改 `version`/`revision` 语义要小心 | 用例用 `version`、分类法与治理组用 `revision`，都是乐观锁；前端靠错误信息含"刷新"触发自动重拉 |
| worker 心跳文件关闭时不删 | 多副本共享路径，删除会误杀健康副本心跳 |
| 迁移 head 已变 | 新增迁移后 `test_migrations.py` 的 head 断言和 README 的 `20261032_case_timestamps` 都要同步 |
| 不要给 Prometheus 加高基数 label | 禁止用 `job_id`、用户输入、文件名、完整请求路径做 label |
| 前端无单元测试 | 只有 e2e，且默认 skip；改组件后至少跑 `npm run build` |
| `src/converter/` 是 JSX/JS 孤岛 | 无类型校验，无法共享 `ui/` 与 `@/lib`，改动要单独自测 |
| 提示条逻辑重复 8 处 | 各组件各自 `useState('') + 4s setTimeout`，是明确的抽取机会 |
| Intel macOS 的 cryptography 是 48.x | 受 `pip-audit` 告警影响，**只能开发用**，绝不能做生产镜像 |
| `test-results/` 未在 `.gitignore` | 根目录与 `web/` 各有一份，是两种执行姿势造成的残留，建议统一 `cd web && npm run test:e2e` |
