# 可观测性与故障处理

Test Atelier 的 API、worker 和 Nginx 使用统一的单行 JSON 日志。日志用于定位生命周期和状态变化，不保存需求原文、上传内容、提示词、模型响应、Cookie 或认证令牌。

## 日志契约

每条应用或启动日志至少包含：

- `timestamp`：UTC ISO-8601 时间
- `schema_version`：当前为 `1`
- `level`：`DEBUG`、`INFO`、`WARNING`、`ERROR` 或 `CRITICAL`
- `service`、`environment`、`request_id`
- `event` 或 `message`

如果设置 `AI_STUDIO_RELEASE`，API、worker、迁移和备份 CLI 日志还会包含经过脱敏、长度限制和字符清理的 `release` 字段；API live/ready 健康响应返回 API 版本，worker 健康响应另外返回心跳文件中的 `worker_release`，避免滚动发布时把旧消费者误判为新版本。建议填入 commit SHA 或不可变构建号，便于把异常关联到具体发布版本。未设置时不会输出空字段。

当 API 的 `AI_STUDIO_RELEASE` 与 worker 心跳中的 `worker_release` 不一致时，worker 健康探针仍保持可用，但会限流记录 `worker.readiness.version_mismatch` 警告；旧版 worker 只能写入时间戳、无法提供构建号时记录 `worker.readiness.version_unknown`。发布排查应先确认两者是否属于同一批构建，再决定是否继续接收新的 AI 任务。

HTTP 请求会把 `X-Request-ID` 贯穿 API、持久 AI 任务和 worker 日志。没有 HTTP 请求的迁移、启动、备份事件使用 `request_id="-"`。应用 formatter 只允许白名单业务字段，异常默认只记录类型；临时排查开启 `LOG_INCLUDE_TRACEBACK=1` 时仍会先脱敏。

生产环境即使误填 `LOG_FORMAT=text`，直接 Uvicorn 启动路径也会先保持 JSON 输出，再以结构化配置错误拒绝启动，避免“配置失败本身”产生无法采集的纯文本日志。

API/worker 启动包装器以及备份 CLI 在日志系统初始化前也会直接输出 JSON；这些字段同样经过白名单、长度限制和敏感信息脱敏，未知字段（包括提示词、请求体和令牌）会被丢弃，避免启动失败或恢复失败路径绕过应用 formatter。即使临时开启 `LOG_INCLUDE_TRACEBACK=1`，日志也只保留脱敏后的调用栈和异常类型，不记录异常消息，避免 provider/client 异常把提示词或模型响应带入日志。

SQLAlchemy engine/pool、HTTPX/HTTP core、urllib3 和 multipart 的低级日志在应用中强制保持 `WARNING` 级别，即使 `LOG_LEVEL=DEBUG` 也不会把 SQL、绑定参数、请求查询串或客户端请求元数据直接写入日志；业务层仍会记录经过白名单的路由、provider 和错误类型。

Python warnings（例如 PDF、Word 解析库发出的兼容性提示）以及 SQLAlchemy、HTTP 客户端和解析器等第三方 logger 也会被转发到同一 JSON sink，避免第三方库直接写入无法采集的纯文本 stderr；应用与 Uvicorn logger 使用专用 sink，根 logger 作为最后一道兜底并继续执行同样的字段白名单和脱敏。

Web 容器的 Nginx 访问日志和历史转换迁移 CLI 使用相同的 `environment`、`event` 和 `request_id` 字段；访问事件额外保留 `upstream_status`、上游耗时和请求字节数，便于区分边缘限流、API 5xx 与上游延迟，但不记录查询字符串或请求体。容器入口包装器会把 Nginx 原生错误诊断转换为 JSON 后写入 stdout，任何意外的非 JSON 行都会被采集校验器拒绝，防止静默混入结构化日志流。Nginx 对包含 `token`、`password`、`secret` 或 API Key 关键词的未知路径只记录 `[REDACTED]`，避免异常请求路径把凭据带入边缘日志。

采集前验证一批 JSONL 日志。检查器同时拒绝重复 JSON 键、限制单行 64 KiB 和嵌套深度，避免字段覆盖绕过敏感字段检查，或异常日志/恶意内容放大采集器资源：

```bash
docker compose logs --no-color --no-log-prefix api worker web \
  | python scripts/validate_log_stream.py
```

生产前置检查需要进入部署日志时使用单行事件模式，避免把漂亮打印的检查列表拆成多条无法关联的日志：

```bash
python scripts/preflight_production.py --compose --jsonl \
  | python scripts/validate_log_stream.py
```

`--json` 仍保留面向人工阅读的详细检查列表；`--jsonl` 只输出检查名称、状态和计数，不输出自由格式路径详情。

检查器失败时不要直接把原始日志发送到外部平台；先隔离未结构化输出或敏感字段，并保留失败行号用于修复。`web` 镜像直接以前台 Nginx 启动，避免官方 shell entrypoint 的启动文本混入 JSON 流。

没有 Docker 时可以用临时 SQLite 数据库做 API 启动验收。该检查会验证 ready 探针、`X-Request-ID`、Prometheus 指标鉴权以及真实日志流，不会触碰项目数据库：

```bash
tmp_dir="$(mktemp -d)"
AI_STUDIO_ENV=development \
AI_STUDIO_REQUIRE_MIGRATIONS=0 \
AI_STUDIO_SESSION_SECRET='local-runtime-secret-32-characters-minimum' \
AI_STUDIO_ADMIN_PASSWORD='local-runtime-admin-password' \
DATABASE_URL="sqlite:///$tmp_dir/runtime.db" \
AI_STUDIO_DATA_DIR="$tmp_dir/data" \
METRICS_TOKEN='local-metrics-token-1234' \
python -m api.uvicorn_entrypoint --host 127.0.0.1 --port 8765 >"$tmp_dir/api.log" 2>&1 &
api_pid=$!
trap 'kill "$api_pid" 2>/dev/null || true' EXIT
# 等待 /api/v1/health/ready 返回 200 后执行只读 smoke
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8765/api/v1/health/ready >/dev/null 2>&1 && break
  sleep 1
done
python scripts/production_smoke.py \
  --base-url http://127.0.0.1:8765 \
  --metrics-token 'local-metrics-token-1234' \
  --expected-release "${AI_STUDIO_RELEASE:-}" --jsonl
python scripts/validate_log_stream.py "$tmp_dir/api.log"
```

这只能证明单进程 API 的启动、探针、优雅停止和日志契约，不等价于 PostgreSQL、多副本 worker、Nginx 和 HTTPS 的生产验收；生产仍需执行 Compose smoke。

独立 worker 也可以在临时 SQLite 数据库上做启动与停止日志验收。先执行 `python -m alembic upgrade head`，再以 `AI_STUDIO_EXTERNAL_WORKER=1` 启动 `python -m api.worker_entrypoint`；确认心跳文件生成后发送 `SIGTERM`，最后用同一个日志校验器检查 `worker.migrations.checked`、`worker.started`、`worker.stop_requested` 和 `worker.stopped` 事件。该流程只验证 worker 生命周期，不代表 SQLite 可用于生产队列。

仓库 CI 会在 Linux runner 上自动执行这套独立 worker 启停检查，并额外执行 Web 镜像的 `nginx -t` 结构化日志校验；因此 API 单进程和 worker 生命周期都具备可重复的 JSON 日志契约门禁。Compose/PostgreSQL、多副本 worker、HTTPS 和备份恢复仍由具备 Docker 的 runner 执行，本机没有 Docker 时不能用 SQLite smoke 代替这些目标环境证据。

## 指标与建议告警

`GET /api/v1/metrics` 输出低基数 Prometheus 指标。生产环境必须设置 `METRICS_TOKEN`，采集端使用 `X-Metrics-Token`，不要把令牌写入 URL。

`scripts/production_smoke.py` 不只检查 metrics 的 HTTP 状态，还会拒绝空正文、格式错误、重复或超大的样本、`NaN/Inf` 和疑似提示词/令牌/文档字段，避免代理或错误处理器返回 200 但指标流已经不可采集。请求路由标签中的 `/documents`、`/prompt` 等路径属于低敏维度，不会被当作指标正文泄露。

建议在 Prometheus 或部署平台配置以下告警，阈值按实际流量校准：

仓库提供可直接纳入 Prometheus `rule_files` 的示例规则：`ops/prometheus/test-atelier.rules.yml`。其中 `up` 和 `probe_success` 的 job 名称需要按实际 scrape 配置调整，应用自身的 `test_atelier_*` 指标名称保持不变。API、ready 和 worker 告警同时使用 `absent(...)`，因此目标完全消失时不会因为指标序列消失而误判为健康。

| 信号 | 示例判断 | 处理方向 |
| --- | --- | --- |
| API 不可用 | `up == 0` 或 live 连续失败 2 分钟 | 查看 API 容器退出原因和 `application.start_failed` |
| API 未 ready | `ready` 返回 `503` 超过 1 分钟 | 检查数据库、迁移、数据目录和技能仓挂载 |
| Worker 失联 | `test_atelier_worker_up == 0` | 检查 worker 日志、数据库连接和心跳文件权限 |
| Worker 心跳过期 | `test_atelier_worker_heartbeat_threshold_seconds > 0 and test_atelier_worker_heartbeat_age_seconds > test_atelier_worker_heartbeat_threshold_seconds` | 确认 worker 是否卡在数据库或模型调用 |
| 队列接近满载 | `test_atelier_ai_jobs_active / test_atelier_ai_jobs_capacity > 0.8` | 降低提交速率或扩容 worker，检查 provider 延迟 |
| 数据库连接池饱和 | `test_atelier_db_pool_size > 0 and test_atelier_db_pool_checked_out >= test_atelier_db_pool_size` 持续 5 分钟 | 检查慢查询、事务是否及时结束和连接池配置；不要只盲目提高上限 |
| Provider 超时 | `rate(test_atelier_model_requests_total{outcome="timeout"}[5m]) > 0` | 检查 CLI 登录、网络出口和 `AI_STUDIO_CLI_TIMEOUT` |
| CLI 不可用 | `test_atelier_cli_enabled{provider=~"codex_cli|claude_code"} == 1 and on(provider) test_atelier_cli_available{provider=~"codex_cli|claude_code"} == 0` 持续 5 分钟 | 检查执行任务的 API/worker 镜像是否安装 CLI、PATH 和登录凭据；宿主机可用不代表容器可用 |
| 5xx 增长 | `rate(test_atelier_requests_total{status=~"5.."}[5m]) > 0.05` | 按 `request_id` 关联 API 和 worker 日志 |

不要按 `job_id`、用户输入、文件名或完整请求路径创建 Prometheus label；这些值会造成高基数和敏感数据泄漏。

Nginx 只在 `$scheme=https` 时返回 HSTS，HTTP 本机验收不会错误地缓存 HTTPS 强制策略；如果 TLS 在更外层的网关终止，应由该网关设置 HSTS，并用 `scripts/production_smoke.py --require-https` 验证最终入口。Compose 验收应同时使用 `--require-web`，确认边缘层实际返回 SPA HTML，而不仅是 API 健康探针。

API 和 Nginx 同时返回同源 Content-Security-Policy（禁止插件、内联脚本和跨源连接）；Nginx 代理会隐藏上游重复值，浏览器入口只保留边缘层策略。部署 smoke 会检查策略至少包含 `default-src 'self'`、`script-src 'self'` 和 `object-src 'none'`。

## 事件检索

常用事件包括：

- 生命周期：`application.started`、`application.start_failed`、`worker.started`、`worker.stopped`、`nginx.starting`、`nginx.start_failed`、`nginx.error`、`nginx.stopped`
- 队列：`ai_queue.slot_reserved`、`ai_queue.capacity_exhausted`、`worker.jobs.recovered`
- Provider：`model.request.started`、`model.request.timeout`、`model.request.failed`
- 文档解析：`document.extract.failed`（包含 `DocumentExtractTimeout` 等受控错误类型，不记录文件正文）
- Worker 基础设施：`worker.claim.failed`、`worker.heartbeat_failed`、`worker.lease_renew_failed`、`worker.lease_lost`、`worker.config.reload_failed`、`worker.job.requeued`、`worker.job.finished`；完成事件包含最终状态和 `duration_ms`，可用于区分模型处理耗时与基础设施故障。同类错误按 `AI_STUDIO_WORKER_ERROR_LOG_INTERVAL_SECONDS` 限流，窗口结束时用 `count` 汇总被抑制的事件。租约线程会继承任务的 `request_id`，因此失去所有权时仍可从请求追踪到异步任务。配置热加载失败时任务会清除当前租约并重新排队，不会使用旧的 provider 凭据继续执行
- 数据库：`database.migration.completed`、`database.migration.failed`、`database.migrations.version_mismatch`（包含当前和期望 revision，不包含连接串）
- 配置密钥：`ai.settings.secret_unavailable`（会话密钥轮换后无法解密已保存的远程模型密钥；事件只记录错误类型，不记录密文）
- 安全：`auth.login.failed`、`auth.login.rate_limited`、`document.download.rejected`
- 运维脚本：备份 CLI 使用 `backup.created`、`backup.verified`、`backup.restored`、`backup.failed` 和 `backup.cancelled` 单行事件；遗留转换迁移使用 `legacy_migration.completed`、`legacy_migration.failed` 和 `legacy_migration.cancelled`；生产前置检查和部署 smoke 在 `--jsonl` 模式下使用 `production.preflight.cancelled` / `production.smoke.cancelled` 表示操作者中断，均只保留低敏计数、耗时与错误类型

排查一条请求时先按 `request_id` 查询，再用 `job_id` 关联异步任务。不要在工单、聊天或外部日志中复制 `message` 原文中的敏感诊断；优先使用 `error_type`、状态码和持续时间。

## 故障处理顺序

1. 记录发生时间、部署版本、服务状态和对应 `request_id`，不要记录密码或原始文档。
2. 运行只读检查：`scripts/production_smoke.py`、`/api/v1/health/ready`、`/api/v1/health/worker` 和受保护的 `/api/v1/metrics`。
3. 若 worker 或数据库异常，先暂停新 AI 提交，不要删除队列表或心跳文件；租约恢复需要保留持久任务记录。
4. 修复配置或依赖后查看 `worker.jobs.recovered`、`ai_job.succeeded`/`ai_job.failed`，确认任务最终状态和容量计数回落。
5. 迁移或恢复前先执行备份并用 `scripts/backup_data.py verify` 校验归档；恢复后重新执行 ready、worker、日志契约和业务只读 smoke。

Worker 停止或租约失效时，CLI 子进程会被进程组终止；Ollama/远程 HTTP
请求会在可取消的守护线程中等待，因此不会把 180 秒 provider 超时带入
`AI_STUDIO_SHUTDOWN_TIMEOUT` 的停止宽限。OCR 与 LibreOffice 文档转换同样在
独立进程组中运行，超时会回收整个进程组，且解析器环境会移除应用和云服务凭据。任务不会在取消路径伪造成功或失败，
而是保留租约状态，交给后续 worker 在租约过期后恢复。
