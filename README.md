# Test Atelier

> 本次重构只支持 macOS、Windows、Linux。其他操作系统暂不在兼容性和生产验收范围内，详见 [`docs/platform-support.md`](docs/platform-support.md)。

面向个人和团队的测试资产工作台：把需求材料、AI 分析、用例编写、人工审核和格式转换放在同一个项目空间中。AI 服务不可用时，登录、项目协作、导入审核、转换和历史下载仍可独立使用。

生产运行时以 `config/runtime.json` 为唯一配置源，不要求操作员导出环境变量。启动器只在
进程内部把配置文件转换为兼容旧模块的子进程参数；模型 Key、会话密钥、管理员密码和
Harness 作用域密钥均放在配置文件指定的 checkout 外 `600` 权限文件中。

> 当前版本适合内网试用和功能验收。真实模型质量、跨机器 Docker 部署、HTTPS 和备份恢复仍需在目标环境单独验收。

## 文档导航

- [完整使用说明](docs/USAGE.md)：首次登录、推荐工作流、角色权限、导入导出和故障排查
- [架构与工程手册](docs/ARCHITECTURE.md)：路由与模块职责、数据模型、权限、任务队列和开发约定
- [可观测性与故障处理](docs/OBSERVABILITY.md)：日志契约、Prometheus 告警和生产排障顺序
- [安全策略](SECURITY.md)：生产安全基线、敏感数据边界和漏洞响应
- [路线图与验收状态](docs/ROADMAP.md)
- [来源工程基线](docs/source-baseline.md)
- [DeepSeek Harness 接入协议与上游兼容策略](docs/harness-upstream-compatibility.md)
- [Harness 真实模型质量验收与离线评审门禁](docs/harness-quality-acceptance.md)
- [前端开发说明](web/README.md)
- API 启动后可访问 `http://127.0.0.1:8000/docs` 查看 OpenAPI 文档

## 能力范围

| 模块 | 能力 | 关键约束 |
| --- | --- | --- |
| 工作台 | 项目概览、待审核、分析、草稿和转换 | 数据按项目隔离 |
| 团队协作 | 项目管理员、评审员、只读成员；成员变更记录 | 全局管理员可救援；只读成员不能写入 |
| 用例库 | `.xls`、`.xlsx`、`.xmind` 预览导入、审核、分派、评论、审计、Excel 导出 | 原值、建议和最终值分开保存；并发修改返回冲突；XMind 导入限制压缩比、节点数量和层级 |
| 用例治理 | 监控中心快照导入、体检指标、A/B/C 同名用例组、内容相同用例组、可撤销治理、入库校验、回归清单和 `Tcase_list` 导出 | 治理规则从 `TEST_ATELIER_CASE_RULES_ROOT` 只读加载；废弃采用软状态和合并指针 |
| 智能梳理 | 分类版本、规则候选、AI 候选、逐条接受/修改/驳回 | AI 结果永远是候选，关键歧义需人工确认 |
| 需求分析 | Markdown、文本、JSON/YAML、图片、PDF、`.doc`、`.docx` 提取、报告编辑和追溯 | `.json`/`.yaml`/`.yml` 只按 UTF-8 原文传递，不校验语法或执行内容；图片/扫描 PDF 依赖 Tesseract；`.doc` 依赖 LibreOffice；`.docx` 解析前检查压缩包安全边界 |
| 用例编写 | 从已确认分析生成草稿，编辑后发布或导出 XMind/Excel | 未确认的关键歧义会阻断生成；导出不等于发布 |
| 项目技能 | 按项目启用/停用 qa-doc-skills 子技能，统一加载外部 Skill 规范和脚本 | 项目状态保存在 `project_skills`；开发默认查找同级 `qa-doc-skills` 或 `~/qa-doc-skills` |
| 用例转换 | Excel/CSV → Excel、XMind → Excel、Excel → XMind，预览和项目级历史 | 单文件 20 MB；新记录留存原始上传文件和摘要；历史保留 30/60/180 天 |
| 项目证据包 | 导出项目材料、分析、Harness 已校验结果、用例、转换、执行报告、缺陷和知识历史 | 逐文件 SHA-256 可离线校验；不含模型凭据或 Harness Session；不是数据库备份 |
| 证据关系 | 按项目目录浏览并沿直接来源、冻结报告和知识引用逐级追踪 | 项目权限先行；直接关联每页 100 条可继续加载，不按同名文件推断来源 |

## 快速开始

### 1. 准备环境

- Python 3.10+（实测 3.12）
- Node.js 20+
- 可选：Tesseract（含 `chi_sim` 和 `eng`）、LibreOffice；AI 推理统一使用云端 API
- 首次启动前备份已有的 `data/ai_studio.db` 和 `data/`

### 2. 安装依赖并初始化

可以使用 Conda 或 Python 自带的 `venv`。以下示例优先使用 Conda；两种方式只需选择一种，激活环境后后续统一使用 `python -m`。

**方式 A：Conda**

```bash
conda create -n test-atelier python=3.12 -y
conda activate test-atelier
python -m pip install -r api/requirements-dev.txt
```

**方式 B：venv**

```bash
python3.12 -m venv .venv312
source .venv312/bin/activate
python -m pip install -r api/requirements-dev.txt
```

安装前端依赖（两种方式都需要）：

```bash
cd web && npm ci && cd ..

mkdir -p ~/.config/ai-studio
python3 scripts/configure_full_runtime.py
```

后续命令中的 `python` 指向当前已激活的环境；如果打开新终端，使用 Conda 时先运行 `conda activate test-atelier`，使用 venv 时先运行 `source .venv312/bin/activate`。会话密钥、管理员密码和模型 Key 都由 `runtime.json` 指向的文件持久化，启动时不需要 `export`；修改管理员密码文件不会覆盖已有数据库账号密码。已有账号请使用 `python scripts/rotate_admin_password.py` 显式轮换密码。

### 3. 启动 API 和前端

终端一：

```bash
python -m api.uvicorn_entrypoint --host 127.0.0.1
```

终端二：

```bash
cd web
npm run dev -- --host 127.0.0.1
```

打开 Vite 输出的地址，使用 `admin` 和初始化密码登录。端口统一保存在 `config/runtime.json` 的 `server.api_port` 和 `server.web_port` 中；平台启动脚本和 Vite 代理都会读取该文件，修改后重启服务即可生效。

### 4. macOS 一键配置并启动 Harness 完整链路

如果需要同时启动独立的 DeepSeek Harness Host、Test Atelier API、Harness
Projector 和 Vite 前端，可运行仓库内的完整启动器：

```bash
cd /Users/lazy/huangsheng/project/test-atelier
./scripts/start_full_macos.command
```

启动器会检查三个固定的本地检出：

```text
/Users/lazy/huangsheng/project/deepseek-harness
/Users/lazy/huangsheng/project/test-atelier-harness-extensions
/Users/lazy/huangsheng/project/qa-doc-skills
```

首次执行会交互式生成 `config/runtime.json`，配置固定的三个检出路径、模型地址和模型名，
并将模型 Key、作用域密钥、会话密钥和管理员密码写入配置文件指定的受保护文件；随后安装
两个 Harness Profile 并执行生产前置检查。路径或端口变更直接编辑 JSON 后重新启动，不要
通过 shell `export` 覆盖。

该启动器默认设置 `TEST_ATELIER_HARNESS_ONLY=1`，因此需求分析和用例编写不会回退到
旧 Provider。按 `Ctrl+C` 会停止本次启动的 Harness Host 和 Test Atelier 启动器；Host
日志写入 `~/.local/state/test-atelier/harness-host.log`。如果启动终端已关闭，可运行：

Harness 原生 Web 界面默认不启动。需要一键启动时，在 `config/runtime.json` 将
`harness.web_shell.enabled` 改为 `true`；启动器会额外安装受限 Web Profile、启动本机
同源网关，并打印 `http://127.0.0.1:8091/harness/` 入口。保持为 `false`（默认）即可只
启动 Host、Projector、API 和 Test Atelier 前端。

```bash
cd /Users/lazy/huangsheng/project/test-atelier
./scripts/stop_full_macos.command
```

### 技能源配置

需求分析和用例编写通过 Skill Runner 加载 `qa-doc-skills` 的 Hub、子 Skill、
`references/` 和 `scripts/`，不再把业务规则复制到 API。开发环境会先查找
Test Atelier 同级的 `qa-doc-skills`，再查找用户目录下的 `~/qa-doc-skills`；
生产环境把 `paths.skill_root` 写入 `config/runtime.json`，并由 Compose 以只读卷挂载，
不要求设置 `TEST_ATELIER_SKILL_ROOT`。

项目创建时会把技能注册表写入 `project_skills`，之后可在“项目技能”页面按项目启用或停用。
停用 `requirement-analysis` 或 `test-cases` 后，对应任务会被后端拒绝，而不是只在前端隐藏按钮。

### 4. 第一次使用

1. 创建项目并确认项目管理员身份
2. 在“项目成员”中添加评审员或只读成员
3. 在“导入记录”上传用例文件，检查预览后确认入库
4. 在“用例审核”分派、评论并保存最终等级和冒烟值
5. 需要智能分析时，在“需求分析”上传材料并等待任务完成
6. 确认关键歧义后进入“用例编写”，审核草稿再发布

详细操作顺序、角色矩阵、文件限制和失败处理见 [docs/USAGE.md](docs/USAGE.md)。

## 配置

生产和跨平台启动统一读取 `config/runtime.json`。运行
`python3 scripts/configure_full_runtime.py`（本机）或
`python3 scripts/prepare_production_runtime.py`（Compose）生成配置；下表中的旧变量名
仅用于兼容代码内部的进程参数，不要求操作者导出。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `AI_STUDIO_SESSION_SECRET` | 无，必填 | Cookie 会话密钥，至少 32 个字符 |
| `AI_STUDIO_SESSION_MAX_AGE` | `28800` | 浏览器会话有效期（秒）；生产前置检查要求至少 300 秒 |
| `AI_STUDIO_ENV` | `development`（Compose 为 `production`） | 结构化日志中的部署环境标识 |
| `AI_STUDIO_RELEASE` | 空 | 可选 commit SHA/构建号，写入 API、worker、迁移和备份日志用于发布关联 |
| `AI_STUDIO_REQUIRE_PREFLIGHT` | `0`（生产环境自动开启） | 在 API/worker 导入运行时依赖前执行生产配置检查 |
| `AI_STUDIO_ADMIN_USER` | `admin` | 首次初始化的管理员用户名 |
| `AI_STUDIO_ADMIN_PASSWORD` | 开发首次启动默认 `123456`；生产环境必填 | 首次初始化的管理员密码，至少 6 个字符 |
| `DATABASE_URL` | `sqlite:///./data/ai_studio.db` | SQLite 或 `postgresql+psycopg://...`；本地 SQLite 默认存放在 `data/` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_TIMEOUT` | `5` / `10` / `30` | PostgreSQL 连接池上限、溢出连接数和等待秒数 |
| `AI_STUDIO_SQLITE_TIMEOUT` | `30` | 本机 SQLite 并发写入等待秒数；仅适用于未启用独立 worker 的开发环境 |
| `AI_STUDIO_BACKUP_TIMEOUT` | `120` | PostgreSQL `pg_dump`/`pg_restore` 超时秒数 |
| `AI_STUDIO_BACKUP_MAX_UNCOMPRESSED_BYTES` | `8589934592` | 备份归档解压后的总大小上限，防止压缩炸弹 |
| `AI_STUDIO_DATA_DIR` | `./data` | 原始文档、转换结果及新版转换记录的原始上传文件目录 |
| `AI_STUDIO_REQUIRE_MIGRATIONS` / `AI_STUDIO_MIGRATION_HEAD` / `AI_STUDIO_MIGRATION_TIMEOUT` | 开发 `0`、生产 `1` / `20261047_auth_session_revocations` / `120` | 启动时执行并校验数据库迁移版本；生产前置检查强制保持 `1`，迁移超时后以结构化错误退出 |
| `AI_STUDIO_REMOTE_ENABLED` | `0` | 是否允许管理员使用远程 OpenAI 兼容接口 |
| `AI_STUDIO_REMOTE_URL` / `AI_STUDIO_REMOTE_MODEL` / `AI_STUDIO_REMOTE_API_KEY` | 空 | 旧远程模型配置；联合 Harness Compose 禁止通过环境插值传入密钥，需要时由管理员在模型配置页面设置 |
| `AI_STUDIO_CODEX_ENABLED` / `AI_STUDIO_CODEX_COMMAND` / `AI_STUDIO_CODEX_MODEL` | `0` / `codex` / 空 | 可选 Codex CLI provider（`1`、`true`、`yes`、`on` 均表示启用）；也可登录后在“模型配置”中启用 |
| `AI_STUDIO_CLAUDE_ENABLED` / `AI_STUDIO_CLAUDE_COMMAND` / `AI_STUDIO_CLAUDE_MODEL` | `0` / `claude` / 空 | 可选 Claude Code provider（`1`、`true`、`yes`、`on` 均表示启用）；也可登录后在“模型配置”中启用 |
| `TEST_ATELIER_HARNESS_URL` | 空（关闭） | 独立 DeepSeek Harness Node Host 地址；必须是无凭据、无查询参数的 HTTP(S) 地址 |
| `OPENAI_API_KEY` / `OPENAI_API_BASE` / `OPENAI_MODEL` | Host 环境注入 | Harness Host 使用的模型凭据、OpenAI 兼容地址和模型名；Key 必须轮换后注入，联合 Compose 部署使用 checkout 外的 `TEST_ATELIER_HARNESS_KEY_FILE`，不能写入 Test Atelier 配置、任务或日志 |
| `TEST_ATELIER_HARNESS_ONLY` | `0` | 设为 `1` 后，需求分析和用例编写拒绝旧 AI Provider 路由，只允许通过 Harness Bridge 执行 |
| `TEST_ATELIER_SCOPE_SECRET` | 空（Harness 关闭） | API 与 Node Host 之间独立的短期作用域签名密钥；启用 Harness 时至少 32 个字符，不要复用会话密钥 |
| `TEST_ATELIER_SCOPE_SECRET_FILE` / `TEST_ATELIER_SCOPE_KEY_FILE` | 空 | 前者是 API、Projector 和独立 Host 可用的进程内绝对路径，不能与内联密钥同时设置；后者是联合 Compose 部署的宿主机 Secret 源文件绝对路径，两者指向同一份独立密钥 |
| `AI_STUDIO_SESSION_SECRET_FILE` / `TEST_ATELIER_SESSION_KEY_FILE` | 空 | 前者供 API、worker、Projector 从绝对路径读取会话密钥；后者是联合 Compose 的宿主机源文件，不与内联值同时设置 |
| `AI_STUDIO_ADMIN_PASSWORD_FILE` / `TEST_ATELIER_ADMIN_PASSWORD_FILE` | 空 | 初始管理员密码的 API 进程文件路径和联合 Compose 宿主机源文件；文件不挂到 worker 或 Host |
| `AI_STUDIO_DATABASE_PASSWORD_FILE` / `TEST_ATELIER_DB_PASSWORD_FILE` | 空 | 联合 Compose 中 API、worker、Projector 的数据库口令文件路径与宿主机源文件；数据库容器使用同一文件的 `POSTGRES_PASSWORD_FILE` |
| `TEST_ATELIER_HARNESS_TOKEN` | 空 | 可选的 Host API 凭据；不写入日志或前端 |
| `TEST_ATELIER_HARNESS_TOKEN_FILE` / `TEST_ATELIER_HOST_TOKEN_FILE` | 空 | 原生 API/Projector/Host 使用前者的同一绝对路径；联合 Compose 使用后者指定 checkout 外独立文件，不与内联 Token 同时设置 |
| `TEST_ATELIER_HARNESS_TIMEOUT` / `TEST_ATELIER_HARNESS_MAX_RESPONSE_BYTES` / `TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES` | `30` / `16777216` / `8388608` | Bridge 请求超时、响应上限和发送给 Host 的普通任务请求体上限；需求材料超限或超过普通分窗安全预算时走有界分段传输，不可分段的请求仍在启动模型前失败 |
| `TEST_ATELIER_HARNESS_PROJECTOR_ENABLED` / `..._POLL_SECONDS` / `..._LEASE_SECONDS` / `..._MAX_EVENTS` | `0` / `1` / `120` / `256` | 独立 Harness Projector Worker 开关、轮询间隔、任务租约和每次批量事件上限；Compose 服务显式开启，租约过期后可恢复 |
| `TEST_ATELIER_HARNESS_CHUNK_CHARS` / `TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS` / `TEST_ATELIER_HARNESS_MAX_CHUNKS` | `12000` / `400` / `128` | Harness 需求分析 map/reduce 的窗口大小、重叠字符和总窗口上限 |
| `HARNESS_REDUCE_BATCH_CHARS` / `HARNESS_REDUCE_BATCH_SIZE` | `48000` / `16` | Node Host 分层 reduce 的单批 JSON 字符预算和 partial result 数量上限（数量至少 2），避免单次聚合超过模型上下文 |
| `HARNESS_MAX_OUTPUT_BYTES` | `16777216` | Node Host 每个 map/reduce 子进程的 JSONL 输出上限；超限任务失败，不把 `thinking` 或 Provider stderr 发送到页面 |
| `HARNESS_DSH_PROTOCOL` | 原生 `headless`；联合 Compose `acp` | `headless` 使用一次性 JSONL；联合 Compose 默认启用 SDK ACP 与分析/用例检查点，可显式同时关闭两个检查点后回退 `headless` |
| `HARNESS_CONFIRMATION_TRANSPORT` | `none` | 一次性 headless 运行器默认不支持后续确认；只有已验证的交互式运行器或测试替身才可设置为 `stdin-lines` |

| `AI_STUDIO_CLI_TIMEOUT` | `600` | Claude Code 等有界 CLI 结构化任务超时秒数；Codex CLI 需求分析和用例编写默认不设置人工超时，连接测试仍使用独立超时 |
| `AI_STUDIO_CLI_TEST_TIMEOUT` | `45` | CLI 连接测试的最小结构化会话超时秒数；用于确认登录和会话初始化，不代替完整任务超时，且必须不大于 `AI_STUDIO_CLI_TIMEOUT` |
| `AI_STUDIO_SKILL_SCRIPT_TIMEOUT` | `120` | 技能仓库辅助脚本最长执行秒数；worker 停止或租约失效时会提前终止子进程 |
| `AI_STUDIO_SHUTDOWN_TIMEOUT` | `25` | API 停止时等待 AI 任务完成的宽限秒数；超时任务保持可恢复状态 |
| `AI_STUDIO_MAX_SUBMITTED_JOBS` | `100` | 单进程 AI 任务提交上限，防止线程池无界排队；超限返回 `503` |
| `AI_STUDIO_EXTERNAL_WORKER` | `0` | 设为 `1` 时 API 只写持久任务队列，由独立 `python -m api.worker_entrypoint` 执行；Compose 默认开启，任务使用数据库容量预留和租约 |
| `AI_STUDIO_WORKER_POLL_SECONDS` / `AI_STUDIO_WORKER_HEARTBEAT_SECONDS` | `1` / `5` | 独立任务进程轮询间隔和心跳间隔 |
| `AI_STUDIO_WORKER_HEARTBEAT` | `$AI_STUDIO_DATA_DIR/.worker-heartbeat` | API 和独立 worker 共享的心跳文件路径；Compose 固定为 `/app/data/.worker-heartbeat` |
| `AI_STUDIO_WORKER_LEASE_SECONDS` | `900` | 外部 worker 数据库租约时长；必须大于 CLI 和技能脚本超时，租约过期后存活 worker 会在轮询中恢复任务 |
| `AI_STUDIO_WORKER_ERROR_LOG_INTERVAL_SECONDS` | `30` | worker 基础设施错误日志的限流窗口；同类数据库、心跳或租约错误在窗口内合并，避免故障时刷满日志 |
| `AI_STUDIO_LOGIN_FAILURE_LIMIT` / `AI_STUDIO_LOGIN_FAILURE_WINDOW_SECONDS` | `8` / `60` | 单进程登录失败窗口；生产环境仍应在反向代理按客户端地址限流 |
| `AI_STUDIO_MAX_REQUEST_BYTES` | `26214400` | 普通 API 请求体上限（同时检查 Content-Length 和分块流） |
| `AI_STUDIO_MAX_DOCUMENT_BYTES` | `104857600` | 需求材料单文件上限（默认 100 MiB）；上传接口以流式请求上限和解析器配额双重限制 |
| `AI_STUDIO_MAX_DOCUMENT_REQUEST_BYTES` | `115343360` | 需求材料 multipart 请求体上限（默认 110 MiB，应略高于单文件上限） |
| `AI_STUDIO_MAX_EXTRACTED_TEXT_CHARS` / `AI_STUDIO_MAX_ANALYSIS_CHUNKS` | `0` / `128` | 大文件提取文本和分析窗口配额；提取文本默认不截断以避免遗漏，设置为正数可启用硬上限；Skill Runner 按 12000 字符窗口逐段调用并分层聚合，超出分析窗口配额时提示拆分 |
| `AI_STUDIO_DOCUMENT_PREVIEW_CHARS` | `200000` | 浏览器预览返回的字符上限；不影响数据库中的完整提取文本或原始文件下载 |
| `AI_STUDIO_ANALYSIS_MERGE_BATCH_SIZE` | `16` | 大文档分析结果每轮聚合批量，避免把所有分片结果一次性塞入模型上下文 |
| `AI_STUDIO_ANALYSIS_MERGE_BATCH_CHARS` | `48000` | 聚合批次 JSON 的字符预算；与批次数量上限同时生效 |
| `AI_STUDIO_SKILL_CONTEXT_MAX_CHARS` | `120000` | Hub、子 Skill 和 references 的共享总上下文预算；超出时按文件顺序截断并在提示词中标注 |
| `AI_STUDIO_TRUST_PROXY_HEADERS` | `0` | 仅受控反向代理部署设为 `1`，让登录失败防护使用 `X-Real-IP`；直连 API 必须保持 `0` |
| `COOKIE_SECURE` | `0` | 仅 HTTPS 反向代理时设为 `1`；生产前置检查强制要求为 `1` |
| `LOG_LEVEL` / `LOG_FORMAT` / `LOG_INCLUDE_TRACEBACK` | `INFO` / `json` / `0` | API 日志级别、输出格式和是否包含脱敏 traceback；生产默认不输出完整异常栈 |
| `METRICS_ENABLED` / `METRICS_TOKEN` / `METRICS_TOKEN_FILE` | `1` / 空 / 空 | Prometheus 指标开关和访问令牌；可从绝对路径的单行文件读取，不能与内联令牌同时设置；生产前置检查要求至少 16 个字符 |
| `TEST_ATELIER_METRICS_KEY_FILE` | 空 | 联合 Harness Compose 中 API 独占的指标令牌宿主机源文件，必须位于 checkout 外且不得复用其他密钥 |

### DeepSeek Harness 真实闭环 smoke

扩展 Profile 安装完成后，可以使用 Python 进程控制运行真实模型闭环，不依赖
macOS 的 GNU `timeout`。生产模式只从 `config/runtime.json` 及其引用的 Secret 文件读取配置，
不会要求操作者导出环境变量，也不会把 API Key 写入日志：

两个 Profile 现已显式关闭上游继承的 Session 日志贡献与 OTel Session 上传；
已有安装在升级扩展后必须重新安装 Profile，否则 Host 预检会拒绝启动。

先在独立扩展仓库安装两个 Profile；推荐使用无 Bash 的 Python 入口，macOS、Windows、Linux
均可运行（原有 `install_profile.sh` 仍保留给容器兼容流程）：

```bash
python /path/to/test-atelier-harness-extensions/scripts/install_profile.py \
  --harness-root /path/to/deepseek-harness
```

Windows PowerShell 使用相同的参数形式，不要把 POSIX 的 `VAR=value` 前缀复制到命令前。

```bash
python scripts/prepare_production_runtime.py
python /path/to/test-atelier-harness-extensions/scripts/preflight_host.py \
  --production --config config/runtime.json
```

真实 Host 从 JSON 引用的 Secret 文件读取模型密钥，不接受内联环境变量；
`model.api_base` 和 `model.model` 默认分别为
`http://code.ugreencloud.com:8000/v1` 和 `ugreen-ai-model`。启动任务前脚本会运行扩展仓库的
`preflight_host.py --production`，再通过 `GET /health` 验证真实 Node Host 已可访问；只有这两步
通过后才会提交需求分析任务。失败时仅输出 Host 日志尾部，不输出环境变量或 API Key。
未设置 `TEST_ATELIER_HARNESS_PORT` 时使用临时空闲回环端口，避免固定端口冲突。

该脚本未显式提供 `AI_STUDIO_SESSION_SECRET` 或 `TEST_ATELIER_SCOPE_SECRET` 时会为本次
临时 smoke 生成随机内存密钥，不使用可预测的开发默认值；生产部署仍必须由 Secret Store
注入稳定且独立的密钥。

该 smoke 使用临时 SQLite 和临时 `DSH_HOME`，覆盖需求分析、关键歧义确认、
用例编写、草稿保存和发布；Host 另提供按任务绑定的受保护 Session 只读回放，
但这不代表 Docker/PostgreSQL/Nginx 或真实浏览器验收已经完成。

不提供模型凭据时，可用真实 API、真实 dsh 和真实项目 Skill 配合本机确定性假模型
检查同一业务链路（当前已在 macOS 验证）：

```bash
node scripts/harness_local_model_smoke.mjs
```

如需连同真实 dsh Session 回放、浏览器页面与停机备份恢复一起验证：

```bash
node scripts/harness_local_model_smoke.mjs --browser
node scripts/harness_local_model_smoke.mjs --browser-case-checkpoint
node scripts/harness_local_model_smoke.mjs --all-generic
node scripts/harness_local_model_smoke.mjs --staged
node scripts/harness_local_model_smoke.mjs --staged-cases
node scripts/harness_local_model_smoke.mjs --fragment-cases
```

`--browser` 与浏览器 ACP 检查点模式会核对三个项目技能的实际调用；这些模式使用
隔离的浏览器、临时服务和回环假模型，既不使用真实模型凭据，也不修改 Harness 上游。

`--all-generic` 用独立来源材料逐个运行 `api-parsing`、`dev-doc-analysis` 和
`figma-to-prd`，核对真实 dsh 技能调用、任务归属、结果来源 ID 与 Session 回放；
确定性假模型不证明解析、分析或 PRD 的内容质量。`--staged` 将任务请求上限暂时降到 64 KiB，以较小的模拟材料验证需求分析和
`api-parsing` 项目技能的分批传输、摘要校验、分窗及结果投影；`--staged-cases`
使用 90 条已确认需求验证用例上下文的分段上传、按需求分窗、聚合和发布；
`--fragment-cases` 验证单条长需求原文分片、用例保留和发布。以上命令只验证
模型调用和结果投影的接线，不代表
100 MiB 真实材料处理成本、真实模型质量或 Windows/Linux 实机验收。

不需要模型凭据也可在隔离目录对独立 Host 执行恰好 100 MiB 的真实 HTTP 分段上传、
摘要组装和确认取消，并验证私有暂存已清理：

```bash
python scripts/harness_staged_transport_smoke.py
python scripts/harness_staged_transport_smoke.py --bytes 655360 --control
```

第二条使用包含 JSON 转义的 NUL 文本检查请求预算；`--bytes` 必须为 10 的倍数。
测试进程不会把父进程的模型、Harness 或 Test Atelier 凭据传给隔离 Host，
只使用临时作用域密钥和不会请求模型的占位进程。这项传输测试不能证明大材料的
分窗覆盖、质量或三平台实机部署。

要验证持久 ACP 确认通道，使用轮换后的临时凭据运行同一脚本：

```bash
python /path/to/test-atelier-harness-extensions/scripts/preflight_host.py \
  --production --config config/runtime.json
```

该命令会自动选择 `test-atelier-acp` Profile；真实权限请求是否出现取决于模型和
Skill 行为，未出现权限请求不等于 ACP 权限链路已被模型触发。

不需要模型凭据的 Harness 浏览器协议闭环可以运行：

```bash
python scripts/harness_browser_smoke.py
```

该脚本启动临时 API、独立 Projector、Fake DSH、Node Host 和 Vite，覆盖流式结果、
Session 回放、页面刷新恢复和用例生成；浏览器退出后还会提交一个没有详情轮询的分析任务，
仅观察数据库确认 Projector 独立完成投影，然后停写、归档数据库/上传文件/Host
任务与 Session，恢复到新目录，并重启 API/Host 通过认证接口核对原始文档、分析/用例
结果和 Session。它不代表真实模型质量、Windows/Linux 实机或 PostgreSQL 生产恢复验收。

同步或发布前，验证 DeepSeek Harness 核心和独立扩展仓库仍与锁定基线一致：

```bash
python scripts/verify_harness_baseline.py --json
```

默认从 Test Atelier 同级目录寻找 `deepseek-harness` 和 `test-atelier-harness-extensions`；其他位置用绝对路径设置 `HARNESS_ROOT` 与 `HARNESS_EXTENSIONS_ROOT`。该检查只读取两个仓库的 Git 状态、SHA、Harness/扩展版本、上游 remote 和两个 lockfile 哈希，并拒绝当前平台不在基线兼容矩阵内的情况；不会 fetch、merge、reset 或修改文件。扩展仓库存在未提交修改时检查会失败，需先在独立同步分支完成审查，不要在 Test Atelier 工作区直接覆盖上游文件。

Node Host 另使用 `HARNESS_TASK_TIMEOUT_MS`（默认 15 分钟，范围 100 ms 至 1 小时）限制单个 Harness 任务的最长运行时间；超时任务会终止子进程并投影为 `timeout` 失败。`HARNESS_SESSION_MAX_BYTES` 和 `HARNESS_SESSION_MAX_EVENTS` 分别限制只读 Session 回放的大小和事件数量，`HARNESS_MAX_REQUEST_BYTES` 限制 Host 单次请求体。需求分析超过单窗口字符预算时，Host 会在同一个业务任务内执行有界、分层 map/reduce，所有窗口和聚合轮次共享总超时和最大窗口数，任一窗口或中间聚合失败都不会产生可发布部分结果。

Host 还以 `HARNESS_MAX_ACTIVE_TASKS`（默认每台 4 个）限制同时接受的模型任务及待上传分段任务；满额返回 `queue_full`，不创建 Host 任务。未启动的分段上传超过 `HARNESS_STAGED_UPLOAD_TIMEOUT_MS`（默认 15 分钟）后标记为 `input_timeout` 并尝试清理部分输入；这两个已确认失败的页面请求可重新发起，网络超时等不确定结果仍保留原幂等键以避免重复任务。此上限仅作用于单台 Host，不是跨副本的全局队列。

### 核心配置持久化

模型供应商和技能注册信息会在 API 启动、模型配置保存或项目技能读取时同步到 `data/config/settings.json`。该文件位于持久化数据目录，使用临时文件加 `os.replace` 原子写入，并设置为仅当前用户可读写；容器部署时它会随 `files_data` 卷保留。生产启动路径、端口和 Secret 不使用该兼容文件，而由 `config/runtime.json` 统一声明。

配置文件中的 `models` 保存远程云端模型、CLI provider 和供应商目录；远程密钥只保存服务端加密密文，不保存明文。`skills_root` 保存技能仓根路径，`skills` 只保存技能 ID、名称、标签、相对路径和简短描述等基本信息，不复制技能正文。`config/settings.example.json` 是结构示例，不包含真实密钥；数据库仍保存项目启停状态和业务数据。

登录后进入项目侧栏的“模型配置”，全局管理员可以启用远程 OpenAI 兼容接口，或启用 Codex CLI/Claude Code 云端工具 并填写可执行文件路径和可选模型，然后使用“测试连接”验证服务。空数据库首次启动时也会读取 `AI_STUDIO_*` provider 环境变量作为初始配置，之后以页面保存的数据库配置为准。CLI 任务和技能仓库辅助脚本使用无 shell 的参数数组启动，不把命令字符串交给 shell；技能仓库辅助脚本还会隔离应用会话密钥、数据库地址、远程 API Key、指标令牌、技能仓路径以及常见云服务和源码托管凭据。CLI 子进程还会移除 `AI_STUDIO_*` 和 Codex 桌面 app-server 的 `CODEX_*` 管道/会话变量，避免后台任务意外连接当前桌面会话，同时保留 CLI 自身需要的 `HOME`、`PATH` 和认证变量。CLI 任务额外使用临时 JSON Schema 和只读沙箱，Codex 后台任务还会跳过项目规则并清空桌面通知钩子，避免交互式配置阻塞服务；仍应先确认 CLI 的账号、工作区和权限策略。网页保存的远程密钥使用当前会话密钥加密存储，只返回“已配置”状态，不回显原文；修改只影响新建 AI 任务，独立 worker 会在领取新任务时重新加载配置。

Codex CLI 与 Claude Code 均是可选的本机 provider。安装对应 CLI 后，在页面启用并选择它即可用于需求分析、用例编写和分类建议；未安装或未启用时，规则候选和人工审核流程不受影响。不要把包含敏感信息的材料提交给未审计的本机 CLI 账号。

启用 Codex 后，项目侧栏的“Codex 工作台”可直接创建会话并发送任务。工作台使用 `codex exec` 在每个会话独立创建的空白临时工作区中执行，采用 `workspace-write` 沙箱；不会把当前项目目录或项目规则带入会话。工作区根目录固定在系统临时目录，避免被配置到项目源码目录。会话通过 SSE 流式返回，并将会话和消息持久化到数据库。只读成员不能执行 Codex 任务，部署前请固定 CLI 版本并审计会话工作区权限。

不要把包含敏感信息的需求文件上传到不受信任的实例；日志不应记录原文、令牌或模型密钥。

API 提供 `/api/v1/health/live`（进程存活）、`/api/v1/health/ready`（数据库可用、数据目录可写且服务已完成启动）、`/api/v1/health/worker`（独立 worker 心跳）和 `/api/v1/health`（存活别名）探针；设置 `AI_STUDIO_RELEASE` 后 API 健康响应会返回 API 发布标识，worker 心跳还会返回实际消费者版本 `worker_release`，便于识别滚动发布中的旧 worker；启动或优雅停止期间 ready 返回 `503`。上传文件按私有权限原子写入，下载时使用不跟随符号链接的文件描述符，替换后的链接会被拒绝并记录低敏安全事件。`/api/v1/metrics` 提供低基数 Prometheus 指标，可通过 `METRICS_TOKEN` 保护，并暴露数据库连接池 size/checked-out/overflow、AI 进程队列的 active/capacity gauge、独立 worker 的 `worker_up`、心跳年龄和实际过期阈值 gauge、Codex/Claude CLI 启用状态与可执行性，以及按 provider/outcome 聚合的模型请求次数和耗时，便于发现连接池耗尽、CLI 未安装、CLI 超时和 provider 故障。应用日志默认输出 JSON，每条日志带有 `schema_version`、service/environment，每条请求带有 `request_id` 和 `X-Request-ID` 响应头；启动脚本、worker 启动和备份 CLI 使用 `request_id="-"` 表示非 HTTP 事件；异步任务保存创建请求 ID，重启恢复后仍可关联原始请求。日志记录路由模板、状态码、耗时和任务元数据，但不会写入请求正文、上传文件、提示词、模型响应、SQL 语句或绑定参数。SQLAlchemy、HTTPX、HTTP core、urllib3、multipart 的低级日志固定在 `WARNING` 以上，避免 `LOG_LEVEL=DEBUG` 时泄露请求和数据库细节。Compose 的 Nginx 边缘访问日志同样使用带 `schema_version` 的 JSON 格式并写入容器 stdout，只保留路由、状态码、耗时和请求 ID，不写入 Cookie、Authorization、查询字符串或请求体；Nginx 原生 error log 由容器入口包装器转换为同一 JSON envelope 后写入 stdout，避免代理错误把非结构化文本混入采集流。API 和 Nginx 返回同源 Content-Security-Policy，禁止内联脚本、插件和跨源连接。API 停止时会停止接收新任务并在宽限期内处理运行中的任务；独立 worker 会主动终止正在运行的 CLI 子进程，远程 HTTP provider 也会响应停止或租约失效信号，不把完整 provider 超时拖过停止宽限；被取消的任务保持可恢复状态。
转换、文档提取和导入解析失败会记录低敏的失败事件；独立 worker 会记录数据库领取失败、任务恢复和任务崩溃，并在短暂数据库故障后继续重试。

部署验收时可用无依赖的日志契约检查器扫描 JSONL 文件或 Compose 日志。它会校验统一 envelope、拒绝请求正文/提示词/密钥字段、非有限数字和常见凭据格式；成功时输出一条 `log_stream.validated` 事件：

```bash
docker compose logs --no-color --no-log-prefix api worker web \
  | python scripts/validate_log_stream.py
python scripts/validate_log_stream.py ./logs/test-atelier.jsonl
```

检查器只接受结构化日志；若输出中混入未结构化的代理错误栈，应先修复日志路由或单独分流后再交给采集器。

## 环境配置与启动

环境配置和服务启动已经拆成两步。配置脚本负责检查 Conda/Node.js、创建 Python 3.12 环境、按依赖指纹安装依赖、生成会话密钥并执行数据库迁移；启动脚本只校验并复用已配置的环境，随后启动 API 与 Vite，不会再次安装依赖、创建密钥或执行迁移。缺少配置产物时启动脚本会提示先运行配置脚本。首次启动会提示管理员密码；密码不会写入仓库文件。三个系统都只启动本机 API 和 Vite，不启动独立 worker。

macOS：

```bash
./scripts/configure_environment_macos.command
./scripts/start_macos.command
```

Windows：

```powershell
.\scripts\configure_environment_windows.ps1
.\scripts\start_windows.ps1
```

Windows 也可以双击对应的 `.bat` 文件。需要跳过依赖安装时：

```powershell
.\scripts\configure_environment_windows.ps1 -SkipInstall
```

Linux：

```bash
./scripts/configure_environment_linux.sh
./scripts/start_linux.sh
```

如果仓库来自不保留执行权限的压缩包，先运行 `chmod +x scripts/configure_environment_linux.sh scripts/start_linux.sh`。原有的 `setup_and_start_macos.command`、`setup_and_start_windows.ps1/.bat` 和 `setup_and_start_linux.sh` 仍保留作为兼容的一体化入口；新部署建议使用上述分离后的两个步骤。

脚本需要预先安装 Conda（Miniconda/Anaconda）和 Node.js 20+；不会自动安装系统级 Conda、Node.js、Tesseract 或 LibreOffice。API 日志和依赖指纹写入 `.test-atelier-logs/`，该目录已加入 `.gitignore`；日志超过 50 MB 时会保留一个 `.1` 轮转文件。

## 验证与测试

```bash
python -m pytest -q
cd web && npm run build && npm run lint
```

2026-10-25 本机验证快照：Python 全量测试 `808 passed, 6 skipped`，独立扩展仓库的 `npm test` 包含 `57 passed` Host/Skill/分段传输/事件分页/进程树及 Profile 覆盖拒绝契约、`23 passed` Python preflight/镜像上下文/Secret 文件契约测试；真实 dsh 配回环假模型的需求分析、用例编写、受限项目技能分段 map/reduce、90 条需求与单条长需求的用例上下文分段及 Session 链路通过。关闭继承的 Session 上传后，headless 和 ACP 的分析、歧义确认、用例编写及发布已分别复测通过。浏览器 2 项回归、独立投影和停写恢复闭环已重新通过；一次失败由浏览器 Smoke 的 Fake DSH 从提示词示例提取错误来源 ID 导致，改为读取任务输入 JSON 后复测通过。分段传输 Smoke 将请求上限临时调低至 64 KiB 以验证跨仓接线；9 MiB/100 MiB 合成正文分别验证 Host 分块重组，100 MiB 合成文本验证窗口规划未超 2000；另以 96 MiB 合成单条需求验证原文分片规划为 1579 窗且字符总量不丢失。尚未以真实大材料和真实模型验收成本、时长与语义覆盖率。前端 build/lint 已单独通过，lint 仍有 React Hook 提示。此前的单进程 API runtime smoke、优雅停止和 JSON 日志契约也有本机验证记录。以上都不证明真实模型质量、Windows/Linux 实机或 PostgreSQL/HTTPS 生产部署已验收。命令应在已安装 `api/requirements-dev.txt` 的 Python 环境中执行；浏览器回归需要已启动的 API、Vite 和本机 Chrome：

```bash
cd web
AI_STUDIO_E2E_PASSWORD='管理员密码' npm run test:e2e
```

可用 `AI_STUDIO_E2E_URL` 切换站点，`AI_STUDIO_E2E_SAMPLE` 指定真实 `.xls` 样本，`AI_STUDIO_E2E_MODEL_STUB=1` 启用模型替身场景。未提供这些变量的场景会跳过，不代表业务回归已通过。

仓库中的 `.github/workflows/ci.yml` 会在 push 和 Pull Request 上分别执行 macOS、Windows、Linux 三平台启动器/前置检查契约、后端测试/编译检查、`pip check`、Linux Python 依赖审计、前端依赖高危漏洞审计、前端构建/lint、构建后 SPA preview smoke、Compose 插值校验、API/Web 镜像构建、单进程 API smoke、SQLite 独立 worker 启停与 JSON 日志契约校验、一次带 PostgreSQL、独立 worker 和 Nginx 的 Compose smoke，以及在临时 PostgreSQL 数据库上的备份/校验/恢复演练；Web 镜像还会执行 `nginx -t` 并校验其结构化输出。真实浏览器、OCR、模型、PostgreSQL 多副本和目标环境恢复演练仍需在具备对应依赖的环境执行。

## 数据库迁移与历史导入

升级数据库（当前 head 为 `20261047_auth_session_revocations`）前，请先停写并备份数据库与 Harness 的任务、Session 持久目录；迁移 `20261043_document_references` 回填文档来源索引且不改写原有 JSON 来源字段，`20261044_artifact_directory_indexes` 增加证据关系查询索引，`20261045_source_document_integrity` 为新上传需求文件保存上传时摘要和大小，`20261046_conversion_output_integrity` 为新生成转换结果保存创建时摘要，`20261047_auth_session_revocations` 持久化浏览器 Session nonce 吊销记录；旧文件保持未验证：

```bash
python -m alembic upgrade head
```

从旧 `ecxel_check` 工程迁移转换历史时，先 `--dry-run`，确认来源目录后再执行。命令只复制文件和预览，不删除来源文件：

```bash
python -m api.migrate_converter_history <ECXEL_CHECK_ROOT>/history --dry-run
python -m api.migrate_converter_history <ECXEL_CHECK_ROOT>/history
# 接入日志采集器时输出单行 JSON 事件
python -m api.migrate_converter_history <ECXEL_CHECK_ROOT>/history --json
```

迁移前备份数据库和 `AI_STUDIO_DATA_DIR`。可使用 `python scripts/backup_data.py backup --output-dir ./backups` 创建带 SHA-256 清单的归档，并用 `verify` 做完整性校验；SQLite 可用同一脚本的 `restore --database-path ... --force` 恢复，PostgreSQL 使用 `restore --database-url ... --force` 通过 `pg_restore` 恢复。启用 Harness 后必须另外纳入任务和 Session 两个持久目录，且在 API、worker、Projector、Host 全部停写后备份及恢复；具体参数和跨机器流程见 [Harness 备份恢复](docs/harness-backup-recovery.md)。仅备份数据库与上传目录的旧归档不包含 Harness 回放。恢复逻辑兼容项目支持的 Python 3.10+，不会依赖 Python 3.12 才提供的 TarFile 过滤参数，并会在恢复前重新校验归档文件。旧 SQLite 数据库如果没有 `alembic_version` 表，会以 `schema_revision=unversioned` 完成快照并在归档事件中明确标记。`api.migrate_converter_history` 只迁移历史记录，不再调用隐式 `create_all`，运行前应先完成 Alembic 迁移。清空或到期清理转换历史不可恢复；迁移完成后在目标项目的“导入记录/用例转换”页面核对数量和下载。

## Docker Compose

`compose.yaml` 提供 PostgreSQL、API、独立 AI worker（API 和 worker 共用同一镜像）以及 Nginx 前端。API 镜像内含 Tesseract/LibreOffice，但不会自动安装 Codex CLI 或 Claude Code。生产部署先生成 JSON 与 Secret 文件，再启动：

```bash
python3 scripts/prepare_production_runtime.py
python3 scripts/preflight_production.py --compose --config config/runtime.json
docker compose -f compose.yaml -f compose.harness-overlay.yaml up --build -d
docker compose ps
python scripts/production_smoke.py --base-url http://127.0.0.1:8080 \
  --metrics-token "$METRICS_TOKEN" --require-worker --require-web \
  --expected-harness-mode off \
  --expected-release "${AI_STUDIO_RELEASE:-}" --json
# 接入结构化日志采集器时使用：
python scripts/production_smoke.py --base-url http://127.0.0.1:8080 \
  --metrics-token "$METRICS_TOKEN" --require-worker --require-web \
  --expected-harness-mode off \
  --expected-release "${AI_STUDIO_RELEASE:-}" --jsonl
```

Compose 会从 `config/runtime.json` 读取完整的 `qa-doc-skills` 路径和所有运行参数，并将技能目录以只读方式挂载到 API 和 worker 容器。生产环境容器启动时会先执行一次配置前置检查，再执行迁移；检查失败会以结构化事件退出，不会让服务进入假 ready 状态。PostgreSQL 在线迁移会在 Alembic 事务内使用 advisory lock，多个 API 副本同时启动时不会并发修改 schema，锁会随提交或回滚自动释放。数据库密码通过 `/run/secrets/postgres_password` 读取，不会进入 Compose 环境变量或 URL 插值。本机访问 `http://127.0.0.1:8080`（Web 容器内部由非 root Nginx 监听 8080）。`db_data` 和 `files_data` 必须一起备份；Compose 自带 API 请求速率和单客户端并发连接限制，所有服务启用 `no-new-privileges`，API/worker/Web 均丢弃全部 Linux capabilities，API/worker 的临时文件使用带 `nosuid,nodev` 的内存挂载；跨机器访问前仍需配置 HTTPS、外层限流并设置 `COOKIE_SECURE=1`。如果要在 Compose 中使用 CLI provider，必须自行构建包含对应 CLI 的镜像，并把 CLI 所需的登录凭据以受控方式提供给实际执行任务的 worker；仅在宿主机安装 CLI 不会让容器自动可用。启动前应检查镜像构建、数据库迁移、worker 心跳和 OCR 语言包。

启动 Compose 前可先运行生产前置检查，提前发现 Docker Compose CLI 缺失、弱密码、未启用独立 worker、SQLite 与独立 worker 组合、技能仓缺失、数据目录不可写、数据库连接池或登录限流参数错误、迁移 revision 不存在、请求/队列/租约/CLI 超时参数越界、日志级别错误、CLI 不在执行环境、cryptography 未达到生产基线以及未设置发布标识等问题：

```bash
python scripts/preflight_production.py --compose
# 接入结构化日志采集器时使用单行 JSON 事件
python scripts/preflight_production.py --compose --jsonl
```

检查失败时脚本返回非零退出码；`--json` 保留详细检查列表，`--jsonl` 输出符合统一日志契约的单行事件且不会包含自由格式路径详情。`--compose` 除了检查 Docker Compose CLI，还会运行无副作用的 `docker compose config --quiet`，提前发现必填变量缺失或 compose 插值错误，不会启动容器。启用 HTTPS 的生产入口可额外传入 `--require-https`，它会校验 HTTPS 和 HSTS；设置 `--metrics-token` 或互斥的 `--metrics-token-file /absolute/path/to/metrics-token` 时 smoke 还会验证未授权 metrics 请求被拒绝。文件必须是有界的单行普通文件，非法文件会失败且不打印令牌或路径。通过 `--require-web` 可额外确认 Nginx 返回 HTML SPA，而不仅是 API 健康探针。部署系统已知构建号时传入 `--expected-release "$AI_STUDIO_RELEASE"`，smoke 会同时核对 live/ready/worker 返回的发布标识，避免流量命中旧副本或 worker 版本漂移。Compose 启动后可运行 `scripts/production_smoke.py` 做只读的 Web、live、ready、metrics、worker 和安全响应头验收；它不会上传文档、调用模型或修改数据库。前置检查和 smoke 都不能替代 `docker compose build`、迁移和备份恢复演练。

### 与 DeepSeek Harness Host 联合部署

Harness Host 保持在独立扩展仓库和镜像中，不复制到 Test Atelier 镜像。使用扩展仓库的
`deploy/compose.harness.example.yaml` 提供 `harness` 服务，再叠加本仓库的
`compose.harness-overlay.yaml`，API 和 Projector 会通过内部网络访问 `http://harness:8787`：

```bash
python3 scripts/prepare_production_runtime.py
python3 scripts/verify_harness_compose.py \
  --extension-compose /path/to/test-atelier-harness-extensions/deploy/compose.harness.example.yaml
docker compose \
  -f compose.yaml \
  -f /path/to/test-atelier-harness-extensions/deploy/compose.harness.example.yaml \
  -f compose.harness-overlay.yaml config --quiet
docker compose \
  -f compose.yaml \
  -f /path/to/test-atelier-harness-extensions/deploy/compose.harness.example.yaml \
  -f compose.harness-overlay.yaml up --build -d
```

`config/runtime.json` 统一声明 API、Projector 和 Harness 的 URL、协议、模型地址以及
所有 Secret 路径；Compose 只负责按固定名称挂载这些文件。`compose.harness-overlay.yaml`
不定义 Harness
镜像，也不改变上游 Harness 工程结构。镜像必须先从干净且锁定的两个检出通过独立扩展
仓库的 `scripts/build_host_image.py` 构建并推送；不要把包含本地依赖和潜在凭据的父目录
直接交给 `docker build`。模型密钥、作用域密钥和 Host Token 从 `config/secrets/` 下的只读
文件挂载，不能放在 Compose 环境变量中。密钥文件必须只允许受信任的操作员和 Docker 读取，
且不能复用浏览器会话密钥。联合部署叠加文件只负责合并固定文件 Secret，不再提供环境变量
覆盖入口。
联合部署前置脚本会读取三份配置的合并结果，核对数据库及内部应用服务未暴露端口或切换到共享网络命名空间、
只读根文件系统、禁用提权和 capabilities，以及 API/Projector/Host
的 Secret 挂载与文件路径、七份密钥互不复用且都在 checkout 外；不会打印 Compose 配置
或密钥。它只验证配置，不构建镜像或启动容器，且仍需目标机器具备 Docker Compose 插件。
macOS 本机已用 SHA-256 与官方清单一致的临时 Compose v5.6.1 插件和七份临时随机
CRLF 文件密钥，通过三份配置的真实合并及完整校验器；此前六份文件的检查已由此取代。
另外用真实 dsh、回环假模型和 CRLF Host Token 文件复测了 API 的分析、歧义确认、
用例发布，以及浏览器的项目技能、Session、独立投影和停写恢复链路；不证明外部模型质量。
本机没有 Docker daemon，此结果不证明镜像或容器部署。
联合部署将浏览器会话密钥、初始管理员密码和 PostgreSQL 密码也改为只读文件；已有
数据库必须继续使用原会话密钥和数据库密码，否则会话/模型配置无法解密或数据库无法连接。
数据库口令只在应用进程内拼接连接 URL，并在 PostgreSQL 容器使用官方
`POSTGRES_PASSWORD_FILE`；不要同时设置内联 `POSTGRES_PASSWORD` 或 `DATABASE_URL`。
文件密钥可以有单个 LF 或 CRLF 结尾，适用于 Windows PowerShell 生成的文件；
内容中间的换行仍会被拒绝，且不能复用七份密钥。
基础 Compose 与联合 Compose 均不再使用环境插值；模型 Key、作用域密钥、Host Token、
会话密钥、管理员密码和数据库密码均通过固定 Secret 文件路径挂载。
运行中不要原地改写会话或作用域密钥文件；轮换需停写、备份并协调 API、worker、
Projector 与 Host 的重启和历史加密数据迁移，不能只替换文件后继续接收任务。
上面的镜像名只是待替换的占位符，
不能直接运行。真实镜像构建、
Secret Store、模型调用和故障恢复仍需在目标部署环境验收。

## 工程结构

```text
api/                 FastAPI、SQLAlchemy、Alembic、任务线程池和测试
web/                 React 19 + Vite + TypeScript 前端
scripts/             macOS、Windows 与 Linux 的环境配置/启动脚本、生产前置检查、部署 smoke、备份校验工具
docs/                使用说明、路线图和来源基线
config/              可提交的核心配置结构示例（运行时配置写入 data/config/）
data/                运行时文档与转换产物（不要提交业务文件）
data/ai_studio.db     默认 SQLite 数据库（生产请按部署方案备份）
compose.yaml         PostgreSQL + API + Nginx 部署示例
compose.harness-overlay.yaml  独立 Harness Host 联合部署覆盖层
```

## 开源参考

工程在流程和基础设施选型上参考了 [Kiwi TCMS](https://github.com/kiwitcms/Kiwi)、[FastAPI](https://github.com/fastapi/fastapi)、[Vite](https://github.com/vitejs/vite)、[shadcn/ui](https://github.com/shadcn-ui/ui)、[OpenAI Codex](https://github.com/openai/codex) 和 [Claude Code](https://github.com/anthropics/claude-code) 的公开文档与实践；业务代码、数据模型和界面按本项目需求实现。
