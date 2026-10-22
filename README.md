# Test Atelier

面向个人和团队的测试资产工作台：把需求材料、AI 分析、用例编写、人工审核和格式转换放在同一个项目空间中。AI 服务不可用时，登录、项目协作、导入审核、转换和历史下载仍可独立使用。

仓库建议使用名称 `test-atelier`。运行时环境变量仍保留 `AI_STUDIO_*` 前缀，以兼容现有部署和数据库，不影响品牌名称变更。

> 当前版本适合内网试用和功能验收。真实模型质量、跨机器 Docker 部署、HTTPS 和备份恢复仍需在目标环境单独验收。

## 文档导航

- [完整使用说明](docs/USAGE.md)：首次登录、推荐工作流、角色权限、导入导出和故障排查
- [架构与工程手册](docs/ARCHITECTURE.md)：路由与模块职责、数据模型、权限、任务队列和开发约定
- [可观测性与故障处理](docs/OBSERVABILITY.md)：日志契约、Prometheus 告警和生产排障顺序
- [安全策略](SECURITY.md)：生产安全基线、敏感数据边界和漏洞响应
- [路线图与验收状态](docs/ROADMAP.md)
- [来源工程基线](docs/source-baseline.md)
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
| 需求分析 | Markdown、文本、图片、PDF、`.doc`、`.docx` 提取、报告编辑和追溯 | 图片/扫描 PDF 依赖 Tesseract；`.doc` 依赖 LibreOffice；`.docx` 解析前检查压缩包安全边界 |
| 用例编写 | 从已确认分析生成草稿，编辑后发布或导出 XMind/Excel | 未确认的关键歧义会阻断生成；导出不等于发布 |
| 项目技能 | 按项目启用/停用 qa-doc-skills 子技能，统一加载外部 Skill 规范和脚本 | 项目状态保存在 `project_skills`；技能源默认位于 `/Users/lazy/huangsheng/project/qa-doc-skills` |
| 用例转换 | Excel/CSV → Excel、XMind → Excel、Excel → XMind，预览和项目级历史 | 单文件 20 MB；历史保留 30/60/180 天 |

## 快速开始

### 1. 准备环境

- Python 3.10+（实测 3.12）
- Node.js 20+
- 可选：Ollama、Tesseract（含 `chi_sim` 和 `eng`）、LibreOffice
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
test -s ~/.config/ai-studio/session-secret || \
  (umask 077; openssl rand -hex 32 > ~/.config/ai-studio/session-secret)
export AI_STUDIO_SESSION_SECRET="$(cat ~/.config/ai-studio/session-secret)"
export AI_STUDIO_ADMIN_PASSWORD='replace-with-a-strong-admin-password'
python -m alembic upgrade head
```

后续命令中的 `python` 指向当前已激活的环境；如果打开新终端，使用 Conda 时先运行 `conda activate test-atelier`，使用 venv 时先运行 `source .venv312/bin/activate`。`AI_STUDIO_SESSION_SECRET` 必须在每次启动时保持不变，否则现有登录会话会失效。首次启动会创建 `AI_STUDIO_ADMIN_USER`（默认 `admin`）账号；修改 `AI_STUDIO_ADMIN_PASSWORD` 不会覆盖已有账号密码。

首次创建运行环境时，启动脚本会交互式要求输入管理员密码，至少 6 个字符；直接回车使用默认密码 `123456`。已有环境会跳过密码配置，命令行开发启动未设置该变量时也使用此默认值，生产环境必须显式配置并应使用密码管理器生成和轮换强密码。

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

打开 Vite 输出的地址，使用 `admin` 和初始化密码登录。运行端口保存在 `data/config/settings.json` 的 `server.port`（API，默认 `8000`）和 `server.web_port`（Vite，默认 `5173`）中；平台启动脚本会读取这两个值并同步配置前端代理，修改后重启服务即可生效。手动启动前端且 API 端口变化时设置 `AI_STUDIO_API_TARGET`。

```bash
AI_STUDIO_API_TARGET=http://127.0.0.1:8010 npm run dev -- --host 127.0.0.1 --port 5173
```

### 技能源配置

需求分析和用例编写通过 Skill Runner 加载 `qa-doc-skills` 的 Hub、子 Skill、
`references/` 和 `scripts/`，不再把业务规则复制到 API。默认路径是：

```bash
export TEST_ATELIER_SKILL_ROOT=/Users/lazy/huangsheng/project/qa-doc-skills
```

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

复制 `.env.example` 后按部署方式设置变量。常用变量如下：

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
| `AI_STUDIO_DATA_DIR` | `./data` | 原始文档和转换结果目录 |
| `AI_STUDIO_REQUIRE_MIGRATIONS` / `AI_STUDIO_MIGRATION_HEAD` / `AI_STUDIO_MIGRATION_TIMEOUT` | 开发 `0`、生产 `1` / `20261032_case_timestamps` / `120` | 启动时执行并校验数据库迁移版本；生产前置检查强制保持 `1`，迁移超时后以结构化错误退出 |
| `AI_STUDIO_OLLAMA_URL` | `http://127.0.0.1:11434` | 本地 Ollama 地址 |
| `AI_STUDIO_OLLAMA_MODEL` | `qwen3:8b` | 本地模型名称，需自行下载 |
| `AI_STUDIO_REMOTE_ENABLED` | `0` | 是否允许管理员使用远程 OpenAI 兼容接口 |
| `AI_STUDIO_REMOTE_URL` / `AI_STUDIO_REMOTE_MODEL` / `AI_STUDIO_REMOTE_API_KEY` | 空 | 远程模型配置；密钥只在 API 进程使用 |
| `AI_STUDIO_CODEX_ENABLED` / `AI_STUDIO_CODEX_COMMAND` / `AI_STUDIO_CODEX_MODEL` | `0` / `codex` / 空 | 可选 Codex CLI provider（`1`、`true`、`yes`、`on` 均表示启用）；也可登录后在“模型配置”中启用 |
| `AI_STUDIO_CLAUDE_ENABLED` / `AI_STUDIO_CLAUDE_COMMAND` / `AI_STUDIO_CLAUDE_MODEL` | `0` / `claude` / 空 | 可选 Claude Code provider（`1`、`true`、`yes`、`on` 均表示启用）；也可登录后在“模型配置”中启用 |
| `AI_STUDIO_CLI_TIMEOUT` | `600` | CLI 结构化任务超时秒数；需求分析包含技能上下文，通常比连接测试耗时更长 |
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
| `AI_STUDIO_MAX_REQUEST_BYTES` | `26214400` | API 请求体上限（同时检查 Content-Length 和分块流）；上传接口自身限制单文件 20 MB |
| `AI_STUDIO_TRUST_PROXY_HEADERS` | `0` | 仅受控反向代理部署设为 `1`，让登录失败防护使用 `X-Real-IP`；直连 API 必须保持 `0` |
| `COOKIE_SECURE` | `0` | 仅 HTTPS 反向代理时设为 `1`；生产前置检查强制要求为 `1` |
| `LOG_LEVEL` / `LOG_FORMAT` / `LOG_INCLUDE_TRACEBACK` | `INFO` / `json` / `0` | API 日志级别、输出格式和是否包含脱敏 traceback；生产默认不输出完整异常栈 |
| `METRICS_ENABLED` / `METRICS_TOKEN` | `1` / 空 | Prometheus 指标开关和访问令牌；生产前置检查要求令牌至少 16 个字符 |

### 核心配置持久化

模型供应商和技能注册信息会在 API 启动、模型配置保存或项目技能读取时同步到 `data/config/settings.json`。该文件位于持久化数据目录，使用临时文件加 `os.replace` 原子写入，并设置为仅当前用户可读写；容器部署时它会随 `files_data` 卷保留。可以通过 `AI_STUDIO_CONFIG_FILE` 指定其他路径。

配置文件中的 `models` 保存本地模型、远程模型、CLI provider 和供应商目录；远程密钥只保存服务端加密密文，不保存明文。`skills_root` 保存技能仓根路径，`skills` 只保存技能 ID、名称、标签、相对路径和简短描述等基本信息，不复制技能正文。`config/settings.example.json` 是结构示例，不包含真实密钥；数据库仍保存项目启停状态和业务数据。

登录后进入项目侧栏的“模型配置”，全局管理员可以直接修改本地 Ollama 地址与模型、启用远程 OpenAI 兼容接口，或启用本机 Codex CLI/Claude Code 并填写可执行文件路径和可选模型，然后使用“测试连接”验证服务。空数据库首次启动时也会读取 `AI_STUDIO_*` provider 环境变量作为初始配置，之后以页面保存的数据库配置为准。CLI 任务和技能仓库辅助脚本使用无 shell 的参数数组启动，不把命令字符串交给 shell；技能仓库辅助脚本还会隔离应用会话密钥、数据库地址、远程 API Key、指标令牌、技能仓路径以及常见云服务和源码托管凭据。CLI 子进程还会移除 `AI_STUDIO_*` 和 Codex 桌面 app-server 的 `CODEX_*` 管道/会话变量，避免后台任务意外连接当前桌面会话，同时保留 CLI 自身需要的 `HOME`、`PATH` 和认证变量。CLI 任务额外使用临时 JSON Schema 和只读沙箱，Codex 后台任务还会跳过项目规则并清空桌面通知钩子，避免交互式配置阻塞服务；仍应先确认 CLI 的账号、工作区和权限策略。网页保存的远程密钥使用当前会话密钥加密存储，只返回“已配置”状态，不回显原文；修改只影响新建 AI 任务，独立 worker 会在领取新任务时重新加载配置。

Codex CLI 与 Claude Code 均是可选的本机 provider。安装对应 CLI 后，在页面启用并选择它即可用于需求分析、用例编写和分类建议；未安装或未启用时，原有 Ollama、规则候选和人工审核流程不受影响。不要把包含敏感信息的材料提交给未审计的本机 CLI 账号。

启用 Codex 后，项目侧栏的“Codex 工作台”可直接创建会话并发送任务。工作台使用 `codex exec` 在每个会话独立创建的空白临时工作区中执行，采用 `workspace-write` 沙箱；不会把当前项目目录或项目规则带入会话。工作区根目录固定在系统临时目录，避免被配置到项目源码目录。会话通过 SSE 流式返回，并将会话和消息持久化到数据库。只读成员不能执行 Codex 任务，部署前请固定 CLI 版本并审计会话工作区权限。

不要把包含敏感信息的需求文件上传到不受信任的实例；日志不应记录原文、令牌或模型密钥。

API 提供 `/api/v1/health/live`（进程存活）、`/api/v1/health/ready`（数据库可用、数据目录可写且服务已完成启动）、`/api/v1/health/worker`（独立 worker 心跳）和 `/api/v1/health`（存活别名）探针；设置 `AI_STUDIO_RELEASE` 后 API 健康响应会返回 API 发布标识，worker 心跳还会返回实际消费者版本 `worker_release`，便于识别滚动发布中的旧 worker；启动或优雅停止期间 ready 返回 `503`。上传文件按私有权限原子写入，下载时使用不跟随符号链接的文件描述符，替换后的链接会被拒绝并记录低敏安全事件。`/api/v1/metrics` 提供低基数 Prometheus 指标，可通过 `METRICS_TOKEN` 保护，并暴露数据库连接池 size/checked-out/overflow、AI 进程队列的 active/capacity gauge、独立 worker 的 `worker_up`、心跳年龄和实际过期阈值 gauge、Codex/Claude CLI 启用状态与可执行性，以及按 provider/outcome 聚合的模型请求次数和耗时，便于发现连接池耗尽、CLI 未安装、CLI 超时和 provider 故障。应用日志默认输出 JSON，每条日志带有 `schema_version`、service/environment，每条请求带有 `request_id` 和 `X-Request-ID` 响应头；启动脚本、worker 启动和备份 CLI 使用 `request_id="-"` 表示非 HTTP 事件；异步任务保存创建请求 ID，重启恢复后仍可关联原始请求。日志记录路由模板、状态码、耗时和任务元数据，但不会写入请求正文、上传文件、提示词、模型响应、SQL 语句或绑定参数。SQLAlchemy、HTTPX、HTTP core、urllib3、multipart 的低级日志固定在 `WARNING` 以上，避免 `LOG_LEVEL=DEBUG` 时泄露请求和数据库细节。Compose 的 Nginx 边缘访问日志同样使用带 `schema_version` 的 JSON 格式并写入容器 stdout，只保留路由、状态码、耗时和请求 ID，不写入 Cookie、Authorization、查询字符串或请求体；Nginx 原生 error log 由容器入口包装器转换为同一 JSON envelope 后写入 stdout，避免代理错误把非结构化文本混入采集流。API 和 Nginx 返回同源 Content-Security-Policy，禁止内联脚本、插件和跨源连接。API 停止时会停止接收新任务并在宽限期内处理运行中的任务；独立 worker 会主动终止正在运行的 CLI 子进程，Ollama/远程 HTTP provider 也会响应停止或租约失效信号，不把完整 provider 超时拖过停止宽限；被取消的任务保持可恢复状态。
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

当前本机验证结果：Python 测试合计 `361 passed`，前端构建和 lint 退出码为 0；生产构建已将 React、Motion 和图标依赖拆成独立 vendor chunk，避免共享入口超过 500 kB，lint 仍会输出少量 React/Hook warning。单进程 API runtime smoke、优雅停止生命周期和 JSON 日志契约校验也已通过。该命令应在已安装 `api/requirements-dev.txt` 的 Python 环境中执行。浏览器回归需要已启动的 API、Vite 和本机 Chrome：

```bash
cd web
AI_STUDIO_E2E_PASSWORD='管理员密码' npm run test:e2e
```

可用 `AI_STUDIO_E2E_URL` 切换站点，`AI_STUDIO_E2E_SAMPLE` 指定真实 `.xls` 样本，`AI_STUDIO_E2E_MODEL_STUB=1` 启用模型替身场景。未提供这些变量的场景会跳过，不代表业务回归已通过。

仓库中的 `.github/workflows/ci.yml` 会在 push 和 Pull Request 上分别执行后端测试/编译检查、`pip check`、Linux Python 依赖审计、前端依赖高危漏洞审计、前端构建/lint、构建后 SPA preview smoke、Compose 插值校验、API/Web 镜像构建、单进程 API smoke、SQLite 独立 worker 启停与 JSON 日志契约校验、一次带 PostgreSQL、独立 worker 和 Nginx 的 Compose smoke，以及在临时 PostgreSQL 数据库上的备份/校验/恢复演练；Web 镜像还会执行 `nginx -t` 并校验其结构化输出。真实浏览器、OCR、模型、PostgreSQL 多副本和目标环境恢复演练仍需在具备对应依赖的环境执行。

## 数据库迁移与历史导入

升级数据库（当前 head 为 `20261032_case_timestamps`）：

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

迁移前备份数据库和 `AI_STUDIO_DATA_DIR`。可使用 `python scripts/backup_data.py backup --output-dir ./backups` 创建带 SHA-256 清单的归档，并用 `verify` 做完整性校验；SQLite 可用同一脚本的 `restore --database-path ... --force` 恢复，PostgreSQL 使用 `restore --database-url ... --force` 通过 `pg_restore` 恢复。恢复逻辑兼容项目支持的 Python 3.10+，不会依赖 Python 3.12 才提供的 TarFile 过滤参数，并会在恢复前重新校验归档文件。旧 SQLite 数据库如果没有 `alembic_version` 表，会以 `schema_revision=unversioned` 完成快照并在归档事件中明确标记。`api.migrate_converter_history` 只迁移历史记录，不再调用隐式 `create_all`，运行前应先完成 Alembic 迁移。清空或到期清理转换历史不可恢复；迁移完成后在目标项目的“导入记录/用例转换”页面核对数量和下载。

## Docker Compose

`compose.yaml` 提供 PostgreSQL、API、独立 AI worker（API 和 worker 共用同一镜像）以及 Nginx 前端。API 镜像内含 Tesseract/LibreOffice，但不会自动安装 Codex CLI 或 Claude Code。设置变量后启动：

```bash
export POSTGRES_PASSWORD='replace-with-db-password-123'
export AI_STUDIO_SESSION_SECRET="$(openssl rand -hex 32)"
export AI_STUDIO_ADMIN_PASSWORD='replace-with-strong-admin-password'
export METRICS_TOKEN="$(openssl rand -hex 24)"
export COOKIE_SECURE=1
python scripts/preflight_production.py --compose
docker compose up --build -d
docker compose ps
python scripts/production_smoke.py --base-url http://127.0.0.1:8080 \
  --metrics-token "$METRICS_TOKEN" --require-worker --require-web \
  --expected-release "${AI_STUDIO_RELEASE:-}" --json
# 接入结构化日志采集器时使用：
python scripts/production_smoke.py --base-url http://127.0.0.1:8080 \
  --metrics-token "$METRICS_TOKEN" --require-worker --require-web \
  --expected-release "${AI_STUDIO_RELEASE:-}" --jsonl
```

Compose 启动前还必须设置 `TEST_ATELIER_SKILL_ROOT`，指向完整的 `qa-doc-skills` 源目录；该目录会以只读方式挂载到 API 和 worker 容器。生产环境容器启动时会先执行一次配置前置检查，再执行迁移；检查失败会以结构化事件退出，不会让服务进入假 ready 状态。PostgreSQL 在线迁移会在 Alembic 事务内使用 advisory lock，多个 API 副本同时启动时不会并发修改 schema，锁会随提交或回滚自动释放。`POSTGRES_PASSWORD` 会被插入连接 URL，必须只使用字母、数字、点、下划线或连字符；前置检查会拒绝其他字符。前置检查和 readiness 会确认 `AGENTS.md`、Hub、需求分析和用例编写四个工作流入口都存在，避免错误的 volume 挂载让服务假 ready。本机访问 `http://127.0.0.1:8080`（Web 容器内部由非 root Nginx 监听 8080）。`db_data` 和 `files_data` 必须一起备份；Compose 自带 API 请求速率和单客户端并发连接限制，所有服务启用 `no-new-privileges`，API/worker/Web 均丢弃全部 Linux capabilities，API/worker 的临时文件使用带 `nosuid,nodev` 的内存挂载；跨机器访问前仍需配置 HTTPS、外层限流并设置 `COOKIE_SECURE=1`。如果要在 Compose 中使用 CLI provider，必须自行构建包含对应 CLI 的镜像，并把 CLI 所需的登录凭据以受控方式提供给实际执行任务的 worker；仅在宿主机安装 CLI 不会让容器自动可用。启动前应检查镜像构建、数据库迁移、worker 心跳和 OCR 语言包。

启动 Compose 前可先运行生产前置检查，提前发现 Docker Compose CLI 缺失、弱密码、未启用独立 worker、SQLite 与独立 worker 组合、技能仓缺失、数据目录不可写、数据库连接池或登录限流参数错误、迁移 revision 不存在、请求/队列/租约/CLI 超时参数越界、日志级别错误、CLI 不在执行环境、cryptography 未达到生产基线以及未设置发布标识等问题：

```bash
python scripts/preflight_production.py --compose
# 接入结构化日志采集器时使用单行 JSON 事件
python scripts/preflight_production.py --compose --jsonl
```

检查失败时脚本返回非零退出码；`--json` 保留详细检查列表，`--jsonl` 输出符合统一日志契约的单行事件且不会包含自由格式路径详情。`--compose` 除了检查 Docker Compose CLI，还会运行无副作用的 `docker compose config --quiet`，提前发现必填变量缺失或 compose 插值错误，不会启动容器。启用 HTTPS 的生产入口可额外传入 `--require-https`，它会校验 HTTPS 和 HSTS；设置 `--metrics-token` 时 smoke 还会验证未授权 metrics 请求被拒绝。通过 `--require-web` 可额外确认 Nginx 返回 HTML SPA，而不仅是 API 健康探针。部署系统已知构建号时传入 `--expected-release "$AI_STUDIO_RELEASE"`，smoke 会同时核对 live/ready/worker 返回的发布标识，避免流量命中旧副本或 worker 版本漂移。Compose 启动后可运行 `scripts/production_smoke.py` 做只读的 Web、live、ready、metrics、worker 和安全响应头验收；它不会上传文档、调用模型或修改数据库。前置检查和 smoke 都不能替代 `docker compose build`、迁移和备份恢复演练。

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
```

## 开源参考

工程在流程和基础设施选型上参考了 [Kiwi TCMS](https://github.com/kiwitcms/Kiwi)、[FastAPI](https://github.com/fastapi/fastapi)、[Vite](https://github.com/vitejs/vite)、[shadcn/ui](https://github.com/shadcn-ui/ui)、[Ollama](https://github.com/ollama/ollama)、[OpenAI Codex](https://github.com/openai/codex) 和 [Claude Code](https://github.com/anthropics/claude-code) 的公开文档与实践；业务代码、数据模型和界面按本项目需求实现。
