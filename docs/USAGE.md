# Test Atelier 使用说明

本文是给测试工程师、项目管理员和部署人员的操作手册。开发命令和技术边界见根目录 [README.md](../README.md)；接口细节以 API 启动后的 `/docs` 为准。

## 环境准备

Python 依赖不限定虚拟环境工具：可以使用 `conda create -n test-atelier python=3.12`，也可以使用 `python3.12 -m venv .venv312`。激活所选环境后安装 `api/requirements-dev.txt`，再执行根目录 README 中的迁移和启动命令。每个新终端都需要重新激活对应环境。

### 核心配置文件

启动后会自动生成 `data/config/settings.json`。`server.port` 和 `server.web_port` 分别控制 API 与 Vite 的本地端口，平台启动脚本会读取它们；修改端口后重启服务即可生效。模型设置保存后会立即同步 `models` 节点；技能仓根路径和注册表会同步到 `skills_root`、`skills` 节点，技能节点仅包含路径和基本元数据，不写入技能正文。文件采用私有权限和原子替换写入，适合随 `data/` 一起备份。若部署环境需要独立位置，可设置 `AI_STUDIO_CONFIG_FILE`；不要把包含加密密钥的运行时文件提交到版本库。结构参考 `config/settings.example.json`。

本机环境配置与服务启动分开执行。先运行对应系统的 `configure_environment_*` 脚本创建 Conda 环境、按依赖指纹安装依赖、生成会话密钥并迁移数据库，再运行 `start_*` 脚本启动 API 与 Vite。启动脚本只校验已配置的 Conda 环境、前端依赖和会话密钥，不会安装依赖、创建密钥或执行迁移；缺少这些前置文件时会明确提示先完成配置。macOS 使用 `configure_environment_macos.command` + `start_macos.command`，Windows 使用 `configure_environment_windows.ps1` + `start_windows.ps1`，Linux 使用 `configure_environment_linux.sh` + `start_linux.sh`。启动脚本会明确关闭 `AI_STUDIO_EXTERNAL_WORKER`，避免继承生产环境变量后任务进入无人消费的持久队列。原有 `setup_and_start_*` 文件保留兼容，但新部署建议使用分离脚本。Compose 部署时必须额外挂载完整的 `qa-doc-skills` 源目录并设置 `TEST_ATELIER_SKILL_ROOT`。

### 生产探针与日志

- `GET /api/v1/health/live`：只检查 API 进程是否能响应
- `GET /api/v1/health/ready`：检查 API 是否能访问数据库、数据目录可写且已完成启动；数据目录为符号链接时也会拒绝 ready；启动或优雅停止期间返回 `503`
- `GET /api/v1/health/worker`：外部 worker 模式下检查数据库租约 worker 心跳；未启用时返回 `disabled`。心跳文件在 worker 停止时保留为最后一次状态，避免多副本滚动重启时一个 worker 删除另一个健康 worker 的心跳；超过阈值后自动判定为 stale。
- `GET /api/v1/health`：存活探针兼容别名
- `GET /api/v1/metrics`：低基数 Prometheus 指标；设置 `METRICS_TOKEN` 后必须使用 `X-Metrics-Token` 或 Bearer 令牌。除请求计数/耗时外，还会暴露数据库连接池 size/checked-out/overflow、AI 任务进程队列的 active/capacity gauge、独立 worker 的 `worker_up`、当前心跳年龄和实际心跳过期阈值、Codex/Claude CLI 启用状态与可执行性 gauge，以及按 provider/outcome 聚合的模型请求次数和耗时，其中 CLI 超时使用独立的 `timeout` outcome

API 默认输出 JSON 日志。每条日志包含 `service`、`environment` 和 `request_id`；每条请求都会返回 `X-Request-ID` 并在日志中记录相同的 `request_id`，方便从网关日志追踪到任务日志；异步 AI 任务会持久化创建请求 ID，重启恢复后仍可关联原始请求。启动脚本、worker 启动和备份 CLI 没有 HTTP 请求，会使用 `request_id="-"`，但仍遵循同一日志信封。所有写请求还会产生不含请求体的 `request.audit` 事件，包含路由、状态码和已登录操作者 ID。日志记录请求路径、路由模板、状态码、耗时、AI 任务状态和错误类型，不记录请求正文、上传文件、提示词、模型响应或密钥；提示词查看只记录操作者、任务和字符数等审计元数据，不记录提示词内容。请求完成事件按 HTTP 结果分级：`2xx` 为 `INFO`、`4xx` 为 `WARNING`、`5xx` 为 `ERROR`。CLI 超时单独记录为 `model.request.timeout` / `CliTimeout`，并在指标中区分 `outcome="timeout"`，便于和一般 provider 失败区分。健康探针在依赖持续故障时仍返回 `503`，但同一种失败日志按短窗口限流，避免监控轮询淹没日志系统。异常默认只记录类型；仅在临时排查且确认日志权限后设置 `LOG_INCLUDE_TRACEBACK=1`，输出脱敏后的异常栈。直接启动 API 时使用 `python -m api.uvicorn_entrypoint`，它会在应用导入失败、端口参数错误或 Uvicorn 启动异常时输出结构化错误，而不是把 Python traceback 混入日志流；该入口也会阻止 Uvicorn 默认文本日志配置覆盖 JSON sink，并把 Uvicorn 的优雅停止超时设置为 `AI_STUDIO_SHUTDOWN_TIMEOUT + 5` 秒，避免应用仍在取消 AI 任务时被服务器提前截断。通过 `LOG_LEVEL` 和 `LOG_FORMAT=text` 可调整级别和本地输出格式。

转换预览/导出、文档提取、用例导入解析和结果持久化会记录对应的 `*.completed`/`*.failed` 事件（如 `conversion.export.completed`、`document.uploaded` 和 `import.confirmed`），解析类事件还会记录 `duration_ms`，只保留项目、操作者、文件类型、大小、行数和异常类型等低敏元数据。独立 worker 会记录 `worker.claim.failed`、`worker.jobs.recovered`、`worker.job.crashed` 和租约续期失败事件；`worker.job.finished` 还会带有任务状态和 `duration_ms`，便于区分模型耗时与数据库故障。数据库短暂不可用时 worker 会按轮询间隔重试，不会静默退出，并暂停文件心跳，避免健康检查把无法领取任务的 worker 误判为健康。`worker.claim.failed`、`worker.heartbeat_failed`、租约续期和状态查询错误按 `AI_STUDIO_WORKER_ERROR_LOG_INTERVAL_SECONDS` 限流；窗口结束后会补发一次 `count` 汇总，既保留故障证据又避免日志风暴。

API 停止时会停止接收新 AI 任务，取消尚未执行的排队任务，并按 `AI_STUDIO_SHUTDOWN_TIMEOUT` 等待正在执行的任务；外部 worker 收到停止信号时会主动终止正在运行的 Codex/Claude 子进程，保留租约让其他 worker 或下次启动自动恢复。运行中的 worker 会按租约间隔主动扫描并重新排队已过期任务，不需要先重启存活进程；超过宽限期仍为 `running` 的任务不会被当作成功。

Compose 为 PostgreSQL、API、worker 和 Web 容器设置了 Docker `json-file` 日志轮转（API/数据库/worker 50 MB、Web 20 MB，最多保留 3 个文件）；Nginx 访问日志和错误日志写入容器 stdout/stderr，确保 Docker 日志驱动和集中式采集器能够读取。所有服务启用 `no-new-privileges`，API/worker 使用只读根文件系统，只允许共享数据卷写入，并将 `/tmp` 与解析器需要的 HOME 限制在带 `nosuid,nodev` 的内存文件系统中；Web 容器同样使用只读根文件系统、`cap_drop: ALL` 和镜像内置的非 root `nginx` 用户，监听非特权端口 8080，仅开放 Nginx 必要的临时目录，并保留 30 秒优雅停止窗口以排空连接。Web 容器还会等待 API 与 worker 健康后才对外提供入口，避免队列没有消费者时误接收任务。Nginx 边缘层同时限制单客户端并发连接数、请求体、请求速率、连接读取、发送和上游读取超时，访问日志使用 JSON 格式并保留请求 ID、路由、状态码和耗时，不记录 Cookie、Authorization、查询字符串或请求体；同时通过同源 CSP 禁止内联脚本、插件和跨源连接。生产环境仍应将关键日志转发到集中式日志系统。

采集前可运行 `python scripts/validate_log_stream.py` 校验 JSONL 日志契约。该检查器不依赖应用包，会验证 envelope 字段、拒绝正文/提示词/密钥字段、非有限数字和常见凭据格式；Compose 验收示例：

```bash
docker compose logs --no-color --no-log-prefix api worker web \
  | python scripts/validate_log_stream.py
```

Web 镜像直接以前台 Nginx 进程启动，避免官方 shell entrypoint 的非 JSON 启动输出混入日志流；代理错误栈应单独分流后再提交检查器。

## 工作流总览

推荐按以下顺序使用：

```text
创建项目 → 添加成员 → 导入用例 → 审核和分派
       ↘ 上传需求 → 分析和确认歧义 → 编写草稿 → 发布用例
```

每个项目的数据、转换历史、需求文档、分析任务和草稿彼此隔离。全局管理员可以跨项目救援，但普通账号必须先成为项目成员。

## 登录和项目

1. 首次启动后使用 `AI_STUDIO_ADMIN_USER`（默认 `admin`）登录
2. 创建项目，创建者自动成为项目管理员
3. 从顶部项目选择器切换项目；切换后页面、历史和权限随项目变化
4. 不要在多个环境之间复用同一数据库，除非已确认会话密钥、文件目录和迁移版本一致

### 角色

| 角色 | 可读 | 可写 | 成员管理 | 跨项目 |
| --- | --- | --- | --- | --- |
| 全局管理员 | 所有项目 | 所有项目 | 创建账号、管理项目成员 | 是 |
| 项目管理员 | 当前项目 | 当前项目 | 添加、调级、移除和查看事件 | 否 |
| 评审员 | 当前项目 | 审核、评论、分派、分析和冒烟 | 否 | 否 |
| 只读成员 | 当前项目 | 否 | 否 | 否 |

成员被移除或降为只读后，未完成的用例分派会自动解除并留下事件记录。

## 用例导入和审核

### 导入

支持 `.xls`、`.xlsx` 和 `.xmind`。点击“导入用例”后先查看预览，再确认入库；取消预览不会写入用例库。建议每批导入后立即检查：

- 用例 ID 是否重复或缺失
- 标题、目录和原始等级是否符合预期
- 冒烟字段是否为空（空值不会被自动猜成“是”）
- XMind 节点层级是否与目录映射一致

### 审核

审核时原始等级、原始冒烟值、建议值和最终值分开保存。批量审核逐条提交，结果可能包含成功、无权、冲突和无效；遇到冲突先刷新详情，再重新提交，不要覆盖他人的最新版本。

评审员可以：

- 认领未分派用例或处理分派给自己的用例
- 添加评论而不改变用例版本
- 查看审核事件、负责人变化和建议决策
- 导出当前项目的 Excel 结果

## 需求分析与用例编写

项目技能由 `/Users/lazy/huangsheng/project/qa-doc-skills/` 提供。Skill Runner
先加载 `skills/hub/SKILL.md`，再按任务选择唯一子 Skill；规则、记忆和 XMind
生成脚本均以该仓库为准。项目管理员可在“项目技能”中停用不需要的 Skill。

1. 在“需求分析”上传 Markdown、文本、图片、PDF、`.doc` 或 `.docx`
2. 检查提取文本和警告，再选择文档提交分析任务
3. 任务完成后逐项确认关键歧义；未确认项会阻断草稿生成
4. 编辑报告或生成用例草稿，逐条修订后保存
5. 发布到用例库，或单独导出 XMind/Excel

需求分析报告和用例编写工具栏都提供“查看提示词”，可查看本次任务实际发送给 AI 的完整提示词并复制；任务失败时也会保留已发送的提示词，便于排查。

大文件策略参考 OpenAI Codex 开源实现（Apache-2.0）：Codex 将原文件留在工作目录，按需读取并对项目指令设置总预算，而不是把整个文件一次性放进 prompt。本项目在远程模型和技能适配层使用等价的有界窗口、来源偏移和分层聚合；技能上下文超预算时只截断注入内容，原始技能文件与上传材料仍保留在受控存储中。实现参考：[agents_md.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/agents_md.rs)、[history.rs](https://github.com/openai/codex/blob/main/codex-rs/core/src/context_manager/history.rs)。

图片和扫描 PDF 需要 Tesseract 的 `chi_sim`、`eng` 语言包；`.doc` 需要 LibreOffice。需求材料单文件默认上限为 100 MiB，可用 `AI_STUDIO_MAX_DOCUMENT_BYTES` 调低或提高；multipart 请求上限由 `AI_STUDIO_MAX_DOCUMENT_REQUEST_BYTES`（默认 110 MiB）控制，边缘 Nginx 也必须同步放宽。Skill Runner 不把大文件全文塞入一次模型上下文，而是按 12000 字符窗口分段执行 requirement-analysis，再以 `AI_STUDIO_ANALYSIS_MERGE_BATCH_SIZE`（默认 16）和 `AI_STUDIO_ANALYSIS_MERGE_BATCH_CHARS`（默认 48000）做分层来源聚合；Hub、子 Skill 和 references 的共享总上下文受 `AI_STUDIO_SKILL_CONTEXT_MAX_CHARS`（默认 120000）限制，超预算文件会被标注截断。提取文本默认不截断（`AI_STUDIO_MAX_EXTRACTED_TEXT_CHARS=0`），可设置正数启用硬上限并返回截断警告；浏览器预览默认最多返回 200000 字符，可用 `AI_STUDIO_DOCUMENT_PREVIEW_CHARS` 调整，不影响完整提取文本或原始下载；`AI_STUDIO_MAX_ANALYSIS_CHUNKS`（默认 128）用于防止模型调用失控，超出时应拆分材料。PDF 最多 100 页；`.docx` 会在解析前检查压缩包路径、条目数和解压后大小，拒绝疑似 ZIP 炸弹或路径穿越文件。OCR 和 LibreOffice 转换均有硬超时，超时会终止整个子进程组，避免解析器子进程残留；解析器子进程不会继承应用会话密钥、数据库地址、指标令牌或云服务凭据。AI 生成只提供候选，不替代人工审核；云端模型暂时不可用时可稍后重试，规则候选和手工审核不受影响。

### 模型选择

- `remote`：仅全局管理员在显式启用后可选；启用前确认正文外发范围和供应商合规要求
- `codex`：本机 Codex CLI，管理员启用后可用于需求分析、用例编写和分类建议。服务端调用使用临时会话、只读沙箱并跳过项目规则与桌面通知钩子，避免把交互式配置带入后台任务
- `claude`：本机 Claude Code，管理员启用后可用于需求分析、用例编写和分类建议

模型密钥只在 API 进程读取，不会发送给浏览器。不要把密钥写入前端环境变量或提交到仓库。

网页保存的远程模型密钥使用 `AI_STUDIO_SESSION_SECRET` 加密。更换会话密钥
会让旧密钥无法解密；API 和独立 worker 会记录
`ai.settings.secret_unavailable` 并拒绝继续使用失效配置，请在确认新密钥后
重新保存远程模型配置，不要把旧密文直接复制到新环境。

本机 CLI provider 由 API 进程使用无 shell 的参数数组启动，并在临时目录内以只读沙箱请求结构化 JSON；页面接受单个可执行文件名或已存在的绝对路径（绝对路径可以包含空格），但不接受带空格的 shell 命令。CLI 子进程和技能仓库内的辅助脚本都不会继承应用会话密钥、数据库地址、远程 API Key、指标令牌或技能仓路径；同时会移除 `AI_STUDIO_*` 以及 Codex 桌面 app-server 的 `CODEX_*` 管道和会话变量，避免后台任务接入交互式桌面会话；CLI 仍会保留自身所需的用户目录、PATH 和认证环境变量。技能仓库辅助脚本受 `AI_STUDIO_SKILL_SCRIPT_TIMEOUT` 限制，worker 停止或租约失效时会终止脚本进程组，不把脚本卡死带入关闭宽限期。空数据库首次启动时会读取 `AI_STUDIO_*` provider 环境变量作为初始配置，后续以“模型配置”页面保存的数据库配置为准。请先在 API 所属用户环境中安装并登录对应 CLI，再在“模型配置”中启用、测试连接，最后在需求分析或用例建议的模型下拉框选择。CLI 的本地账号权限、费用和数据留存策略仍由 CLI 自身控制，启用前应单独审计。

### Codex 工作台

启用 Codex CLI 后，项目侧栏的“Codex 工作台”会提供嵌入式会话界面。创建会话后可连续发送任务，服务端会把最近的对话作为下一轮上下文，并通过 SSE 流式返回状态和文本增量。每轮执行使用 `codex exec`、无 shell 参数数组、隔离的应用环境和 `workspace-write` 沙箱；每个会话都会创建独立的空白工作区，不继承 API 进程当前目录、项目代码或项目规则；工作区根目录固定在系统临时目录。Codex 只能修改当前会话工作区中的文件，因此只读成员不能创建会话或发送任务。前端在 CLI 首次响应前显示等待动画，并支持中止当前请求。

会话和消息记录持久化保存在数据库的 `codex_sessions`、`codex_messages` 表中，API 重启或多副本切换后仍可按项目和用户恢复；会话工作区中的文件仍按对应会话保存。正式部署前请固定 CLI 版本、完成 `codex login`，并单独审计会话工作区权限、费用和网络出口。

## 分类建议

项目管理员在“分类规则”维护业务模块、业务场景、测试维度和端类型，发布后形成不可变版本。

- 规则候选只匹配明确的原始等级、冒烟值和已发布分类
- AI 候选和规则候选都不会直接改写最终值
- 评审员可接受、修改接受或驳回，并保留决策记录
- 未匹配项保持“待归类”，不要把空值当作默认分类

## 用例转换和历史

“用例转换”保留原转换工程的四种模式：Excel/CSV → Excel、XMind → Excel（7 列或 15 列）、Excel → XMind。页面支持配置、预览、表格/XMind 结构画布/并排查看、全文搜索、导出以及项目级历史；Excel → XMind 会在导出前展示可缩放、平移的思维导图预览。

转换结果先写临时文件，再原子替换到 `AI_STUDIO_DATA_DIR`；数据库只保存预览和元数据。历史按项目保留 30、60 或 180 天，清理后文件不可恢复。

## 数据、备份和安全

需要一起备份：

- `DATABASE_URL` 指向的数据库（SQLite 默认是 `data/ai_studio.db`）
- `AI_STUDIO_DATA_DIR`（默认 `data/`）
- `.env` 中的会话密钥应通过安全的密钥管理保存，不要放入备份公开目录

仓库提供带完整性校验的归档脚本。SQLite 会先用在线 backup API 创建一致性快照；PostgreSQL 会调用本机 `pg_dump`，因此生产机需要安装对应客户端：

```bash
python scripts/backup_data.py backup --output-dir ./backups --keep 7
python scripts/backup_data.py verify ./backups/test-atelier-<timestamp>.tar.gz
```

备份工具的成功、失败和操作取消输出也是单行 JSON 事件，包含 `schema_version`、`service`、`environment`、`component=backup`、`request_id`、`event` 和 `duration_ms`，可以直接交给集中式日志采集器；成功事件只输出归档名、大小、数据库类型、迁移版本和文件数量，不输出主机绝对路径或上传文件名；没有 `alembic_version` 表的旧 SQLite 数据库会标记为 `schema_revision=unversioned`，仍可完成一致性快照；失败消息会继续脱敏数据库密码、Bearer 令牌和 API Key。`pg_dump`/`pg_restore` 会清理继承的 `PG*` 连接覆盖变量，只使用命令目标和 URL 中明确提供的连接参数。

SQLite 恢复必须显式指定目标并确认覆盖：

```bash
python scripts/backup_data.py restore ./backups/test-atelier-<timestamp>.tar.gz \
  --database-path ./restored/ai_studio.db --data-dir ./restored/data --force
```

PostgreSQL 恢复必须显式提供目标数据库地址并确认覆盖：

```bash
python scripts/backup_data.py restore ./backups/test-atelier-<timestamp>.tar.gz \
  --database-url 'postgresql+psycopg://ai_studio:密码@db:5432/ai_studio' \
  --data-dir ./restored/data --force
```

恢复前脚本会校验归档清单、数据库文件存在性和 SHA-256；同时校验归档成员大小并限制解压后的总大小（默认 8 GiB，可用 `AI_STUDIO_BACKUP_MAX_UNCOMPRESSED_BYTES` 调整），避免压缩炸弹或误指向超大数据卷。恢复提取逻辑兼容 Python 3.10+，不依赖 Python 3.12 才提供的 TarFile 过滤参数，并在写入 staging 目录时再次校验文件摘要。SQLite 额外执行 `integrity_check`，并把数据库与 `data/` 目录先在目标卷旁边完成私有权限 staging，再原子替换；替换任一侧失败时会回滚已替换的目标，避免出现新数据库配旧文件或反过来的半恢复状态。PostgreSQL 恢复会先在目标卷旁边准备并安装 `data/`，再使用 `pg_restore --clean --if-exists --single-transaction --exit-on-error` 导入数据库 dump；任一步失败都会回滚文件目录，数据库侧由单事务回滚，避免留下半恢复状态。`AI_STUDIO_BACKUP_TIMEOUT` 会限制 `pg_dump`/`pg_restore` 的最长执行时间，避免网络故障造成无限等待。备份会排除 `.worker-heartbeat` 等运行时心跳文件，避免恢复后在 worker 尚未启动时误报健康。PostgreSQL 恢复前应确认目标数据库已停止写入并完成最新备份。

生产环境应使用 HTTPS、反向代理限流、强管理员密码和独立数据库账号。浏览器会话默认 8 小时，生产前置检查要求 `AI_STUDIO_SESSION_MAX_AGE` 至少 300 秒、`COOKIE_SECURE=1` 和至少 16 个字符的 `METRICS_TOKEN`；API 对登录失败提供单进程窗口防护，并在读取路由前拒绝超过 `AI_STUDIO_MAX_REQUEST_BYTES` 的声明请求体；Compose 的 Nginx 已提供基础 API 限流，API 只有在位于受控代理之后时才应设置 `AI_STUDIO_TRUST_PROXY_HEADERS=1`，让登录失败窗口按 `X-Real-IP` 区分客户端，直连 API 必须保持默认值 `0`。外层网关仍应为登录接口按客户端地址补充更严格规则。Compose 默认将 AI 任务交给独立 `worker` 服务，任务状态写入数据库并可在进程重启后恢复；`AI_STUDIO_MAX_SUBMITTED_JOBS` 通过数据库单例预留跨 API 副本原子限制持久队列 admission，超过后返回 `503`；worker 使用 `AI_STUDIO_WORKER_LEASE_SECONDS` 租约和续租心跳；续租失败会立即停止当前 provider 工作、暂停健康心跳并让任务等待租约恢复，避免旧 worker 在失去数据库所有权后重复写入。过期任务可被其他 worker 恢复，并在每个新任务领取前重新加载管理员保存的 provider 配置，无需重启 worker。API 启动时会清理终态任务的遗留容量预留、补回外部 worker 模式下未完成预留的活动任务并重新计算队列计数，避免异常退出后永久占用容量或绕过 admission；生产或独立 worker 模式下若数据库无法完成这一步会拒绝启动，避免在未知计数下接收任务。监控应使用 `/metrics` 暴露的 `test_atelier_worker_heartbeat_threshold_seconds` 与年龄指标比较，而不要把默认 30 秒阈值硬编码到告警规则中。单机开发不启用外部 worker 时仍使用进程内线程池。

Compose 镜像默认不包含 Codex CLI 或 Claude Code，也不包含宿主机的登录状态。CLI provider 只在执行 AI 任务的 API/worker 进程环境中可用；若采用 Compose，应在自定义镜像中安装 CLI，并单独设计凭据挂载、版本固定和网络出口策略。启用前先在同一个 worker 环境完成版本检查和最小结构化连接测试，不要仅凭宿主机上的 `codex --version` 判断容器已可用。
生产环境会在 API 和独立 worker 启动前执行前置配置检查，再由 API 执行迁移；生产环境强制 `AI_STUDIO_REQUIRE_MIGRATIONS=1`，并校验当前版本为配置的 head，不能通过设置 `0` 回退到隐式 `create_all`。API 前置检查强制 HTTPS Cookie 和 metrics 令牌，API 的 `initialize()` 也会在直接运行 Uvicorn 时重复校验这两项配置；worker 只执行任务、不提供 HTTP 接口，因此跳过这两项 HTTP 专属密钥检查。PostgreSQL 在线迁移会在 Alembic 事务内持有 transaction-scoped advisory lock，多个 API 副本同时启动时会串行迁移，并随提交或回滚自动释放。迁移入口会输出结构化的成功/失败事件且不会回显数据库 URL，前置检查失败、迁移失败或版本不匹配时服务不会进入 ready 状态。

部署前建议在仓库根目录运行：

```bash
python scripts/preflight_production.py --compose --json
```

需要把前置检查直接交给日志采集器时改用单行 JSON 事件：

```bash
python scripts/preflight_production.py --compose --jsonl \
  | python scripts/validate_log_stream.py
```

`--jsonl` 只包含检查名称、状态和计数，不输出自由格式路径详情。

脚本只做本地前置校验，检查 Docker Compose CLI、会话密钥和管理员密码强度、PostgreSQL/独立 worker 拓扑、`qa-doc-skills` 入口、数据目录写权限、数据库连接池和登录限流参数、启用的 Codex/Claude 可执行文件、cryptography 生产基线、迁移 revision 是否存在、请求/队列/租约/CLI 超时数值边界及连接测试超时不超过完整任务超时、provider/技能脚本超时不超过 worker 租约、HTTPS Cookie、指标令牌、发布标识、日志级别和 JSON 结构化日志配置。Intel macOS 的旧 universal2 cryptography wheel 在开发环境只报告 warning，在生产环境会直接失败；生产 Linux/Apple Silicon 必须使用 50+ 系列。生产环境未设置 `AI_STUDIO_RELEASE` 时会给出 warning；它通过时仍需继续执行 Compose 构建、迁移、`ready`/`worker` 探针和备份恢复演练。

Compose 启动后可运行只读部署 smoke，不会上传文档、调用模型或修改数据库：

```bash
python scripts/production_smoke.py \
  --base-url http://127.0.0.1:8080 \
  --metrics-token "$METRICS_TOKEN" \
  --require-worker --require-web \
  --expected-release "${AI_STUDIO_RELEASE:-}" --jsonl
```

smoke 会检查 live、ready、metrics、worker（启用时）、`X-Request-ID`、`no-store`、数据库连接池与 CLI 可用性指标和基础安全响应头；设置 `--metrics-token` 时还会验证未授权 metrics 请求被拒绝。传入 `--expected-release` 后还会核对 live/ready/worker 的发布标识（worker 使用心跳中的 `worker_release`），确认实际流量和任务消费者命中的构建版本。`--json` 保留详细检查列表，`--jsonl` 输出可直接采集的单行日志事件。HTTPS 入口可加 `--require-https`，额外校验 HTTPS 和 HSTS；失败时返回非零退出码。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 启动提示 `AI_STUDIO_SESSION_SECRET` 无效 | 设置至少 32 个字符，并在后续启动中保持不变 |
| 首次启动提示管理员密码 | 首次创建运行环境时可输入至少 6 个字符；直接回车使用默认密码 123456。已有环境会跳过密码配置；生产环境仍建议使用密码管理器生成并轮换强密码 |
| 页面提示无法连接 API | 检查 `data/config/settings.json` 的 `server.port`，并确认前端使用同一 API 端口；手动启动前端时设置 `AI_STUDIO_API_TARGET` |
| 分析任务失败 | 检查云端模型地址、模型名称和 API Key；修复后在页面重试 |
| 测试 Codex CLI 提示远程模型缺少 API Key | 远程模型的密钥已保存时页面不会回显原文；不需要重新输入，更新到包含 CLI provider 修复的 API 后重新加载页面再测试 |
| Codex CLI 测试失败 | 在运行 API 的同一用户环境执行 `codex --version` 和一次最小结构化请求；确认 `PATH`、Codex 登录状态和页面中的命令路径一致 |
| CLI 需求分析超时 | 连接测试会执行最小结构化会话，需求分析还要加载技能上下文并等待模型输出；默认任务超时为 600 秒，连接测试超时可设置 `AI_STUDIO_CLI_TEST_TIMEOUT`，且不能大于 `AI_STUDIO_CLI_TIMEOUT`；仍超时可拆分较长需求材料 |
| 图片或扫描 PDF 没有内容 | 安装 Tesseract、`chi_sim` 和 `eng`，再重新上传 |
| `.doc` 上传失败 | 安装 LibreOffice；`.docx` 不需要转换器 |
| 审核/冒烟提示“请刷新” | 其他成员已更新版本，刷新详情并基于最新版本重试 |
| 历史下载找不到文件 | 检查 `AI_STUDIO_DATA_DIR` 是否被清理或未挂载；数据库记录不会自动恢复文件 |
| E2E 全部跳过 | 设置 `AI_STUDIO_E2E_PASSWORD`；需要样本的场景再设置 `AI_STUDIO_E2E_SAMPLE` |
