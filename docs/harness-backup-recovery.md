# Harness 任务与 Session 备份恢复

适用范围：macOS、Windows、Linux。DeepSeek Harness 上游检出目录不参与归档；从固定 commit 重建运行时和独立 Extension 后再恢复数据。此流程不备份模型 API Key、浏览器 Session Secret、Scope Secret 等凭据；它们必须从新的 Secret Store 单独注入。

## 一致性边界

Test Atelier 数据库、上传目录、Extension Host 的 `HARNESS_TASK_ROOT` 和 `HARNESS_SESSION_ROOT` 是四个逻辑存储位置。前两者与后两者不共享事务，不能在服务运行期间依次复制并称为一致快照。归档包含任务索引、独立事件回放文件（可能有完整模型输出）、Session 文件和业务数据库，必须按敏感业务数据限制读取与保留。

1. 停止所有写入方：API、worker、harness-projector、Harness Host；阻止外部数据库写入与自动重启。Linux Compose 部署可在三份配置文件的同一 `docker compose -f ...` 命令下 `stop web api worker harness-projector harness`，保留 `db` 以便 `pg_dump`/`pg_restore`。macOS/Windows 本地运行要停止对应平台启动器管理的进程和独立 Host，并确认进程退出；仅关闭浏览器不算停写。
   分段传输中的任务可能在私有 Session 目录暂存上传正文；Host 正常停机会将中断任务标记失败，下一次启动清理暂存。停写备份可能仍包含这些暂存内容，必须把整个 Session 卷当作含原文的敏感数据，不能跳过私有任务目录。
2. 将数据库和三个文件目录挂载到同一备份操作环境，Harness 两个卷分别只读挂载。先检查四个路径指向同一部署、没有嵌套，当前没有其他客户端写数据库。`--services-stopped` 是操作者声明，脚本无法检测所有远端写入方。
3. 执行下列备份命令，保留校验成功的归档和独立密钥；失败时不要将残缺归档用于恢复。`verify` 会对清单和各文件 SHA-256 做校验。旧版仅含数据库和上传目录的归档不能恢复 Harness 任务与 Session。

```bash
python scripts/backup_data.py backup \
  --database-url "$DATABASE_URL" --data-dir /mounted/atelier-data \
  --harness-task-dir /mounted/harness-tasks \
  --harness-session-dir /mounted/harness-sessions \
  --services-stopped --output-dir /private/backups
python scripts/backup_data.py verify /private/backups/<archive>.tar.gz
```

PowerShell 使用相同的 Python 脚本与选项，以反引号续行或写成一行，路径换为 Windows 路径。SQLite 的 `--database-url` 应指向实际 `.db` 文件；PostgreSQL 使用具备 `pg_dump` 权限的地址，避免在 shell 历史中写入密码。归档自身未加密，传输与落盘加密由运维层负责。即使停写，跨卷归档并非原子快照；全程必须保持停机，不能在备份开始后重新启动服务。
联合 Compose 的操作容器挂载 `atelier_database_password` 且设置
`AI_STUDIO_DATABASE_PASSWORD_FILE` 时，备份命令可省略 `--database-url`，工具在进程内
拼接 PostgreSQL 地址并仅向 `pg_dump` 子进程传递 `PGPASSWORD`。不要把拼接后的 URL
放进命令行或写入归档。
备份与恢复还会拒绝上传目录、Host 持久目录和备份输出根目录自身或扫描内容中的符号链接及 Windows junction；不要用 junction 将卷外文件伪装为项目数据。此检查不代替停写期间对目录权限和并发路径替换的控制。

## 跨机器恢复

1. 新机器安装相同兼容版本的 Test Atelier 与独立 Extension，检查 `docs/harness-baseline.json` 中锁定的上游版本；不要修改 DeepSeek Harness 上游源码。先停掉目标机器的全部写入方。把归档安全传入目标机器并执行 `verify`，确认部署的 Secret Store 已重建但不把密钥写入归档。
2. 用 `restore` 指明新的数据库、上传目录和两个 Host 持久目录，并传入 `--services-stopped`。已有目标需要 `--force`，先保留旧目标独立备份。SQLite 在四个文件目标安装失败时回滚已安装文件；PostgreSQL 用单事务 `pg_restore`，Host/上传目录在失败时回滚。跨数据库和文件卷不存在全局事务，恢复期间必须阻止并发服务启动，异常退出时人工核对恢复结果。

```bash
python scripts/backup_data.py restore /private/backups/<archive>.tar.gz \
  --database-path /new/atelier/data/ai_studio.db \
  --data-dir /new/atelier/data \
  --harness-task-dir /new/harness/tasks \
  --harness-session-dir /new/harness/sessions \
  --services-stopped --force
```

PostgreSQL 将 `--database-path` 换成 `--database-url "$DATABASE_URL"`。对 Docker 命名卷，在一次性操作容器中挂载 `files_data`、`harness_tasks`、`harness_sessions` 到不同路径，再将这些路径传给脚本；不要依赖宿主机上 Compose 自动生成的卷名。保持数据库容器在线但独占写入，不能在 Host 仍写 Session 时恢复。
联合 Compose 的文件密钥模式改用 `--database-name ai_studio_restore` 指定**事先创建的独立目标库**，
不要提供含密码的 `--database-url`；脚本拒绝把这个便捷路径指向在线的 `ai_studio`。
目标库与三个文件目录仍需保持停写独占，完成校验前不要切换生产流量。

恢复后先检查数据库迁移版本、任务与 Session 目录、任务详情和 SSE 回放，再启动 Host、Projector、API/worker、Web；执行 ready 和只读 Smoke，并验证一个历史任务/Session 的权限与结果。真实模型 Smoke、Windows/Linux 实机恢复演练和 Linux 生产 PostgreSQL/HTTPS 验收仍是单独的发布门槛。

隔离测试 `python scripts/harness_browser_smoke.py` 已在 macOS 临时 SQLite 上演练停写归档、独立目录恢复、历史报告核对及活动冒烟执行恢复后继续保存并结束；它会写入**临时恢复库**，不应对生产恢复库直接执行其中的写入步骤。该结果不代替 PostgreSQL、三平台或实际跨机器恢复验收。

恢复后或删除项目前后可在停止 API、Projector 与 Host 写入方时运行只读对账
（macOS、Windows、Linux 使用相同 Python 入口）；运行中扫描只是瞬时结果，
不能据此判断跨存储删除完成：

```bash
DATABASE_URL='sqlite:////path/to/ai_studio.db' python scripts/audit_harness_project_storage.py \
  --project-id 42 --task-root /path/to/harness-tasks --session-root /path/to/harness-sessions
```

PostgreSQL 原生或基础 Compose 从环境变量 `DATABASE_URL` 读取连接；联合 Compose 的文件
密钥模式会在未提供 URL 时从 `AI_STUDIO_DATABASE_PASSWORD_FILE` 构造连接，均避免把
含凭据的地址写进命令行；
SQLite 以只读 URI 打开且不会创建不存在的数据库。退出码 0 表示数据库任务与 Host
索引、回放文件和 Session 引用一致；2 表示差异，1 表示无法完整对账。输出仅含数量，
不含文档正文、文件名和模型结果。项目已删除而 Host 仍有任务时，会报告
`host_only_tasks`，但此工具**不会删除任何数据**。使用应用项目删除接口前
必须有可用的 Host；若 Host 不可达或旧目录归属不明，删除待处理状态保留业务行，
恢复 Host 或核对迁移后重试。`global_orphan_event_replays` 和
`global_unreferenced_sessions` 是整个专用 Host 卷的计数：失去任务索引后无法再安全
归属到某个项目。`legacy_layout_sessions` 记录仍混放在旧目录的 Session，
只是升级提示而非内容丢失；新 Host 任务写入独立的 `<session-root>/<task-id>/`。
只读对账会拒绝根目录及扫描内容中的 Windows junction；无法完成完整遍历时返回错误，
不以部分扫描结果宣称存储一致。
报告中的 `unconfirmed_runner_stops` 仅统计本项目的隔离任务；即使文件与数据库完整，
只要大于零也返回 `status=inconsistent` 和退出码 2，不能据此放行项目删除。
即使两个任务使用相同的 Session ID，对账也会逐个核对任务目录的归属；只在其他
任务目录中找到同名 ID 时，该项目仍报告 `missing_sessions`。旧混放目录中的 ID
若被多个任务索引引用，视为归属不明，不作为已找到的任务 Session。
不能因为对账结果存在残留就直接批量删除目录。

Host 已提供任务级受权删除原语：写入 `<task-id>.deleted.json` 标记后，移除该任务
独立 Session 目录、事件回放和索引；重启时会恢复中断的清理，标记本身保留以防
任务 ID 被重用。只读对账将正常标记记为 `deleted_task_markers`，若标记对应的文件
仍未清理完则报告 `incomplete_deletions`。项目删除会先持久化待处理状态，再逐任务
调用该原语；所有 Host 回执成功后才提交业务库删除。两个存储没有全局事务，
失败时应重新提交项目删除请求，不能绕过 API 猜测旧混放 Session 文件归属。
Host 在删除任何文件前会刷新删除标记的内容到磁盘；macOS/Linux 还会刷新父目录
项。Windows 的 Node 运行时不提供等价的目录同步保证，突然断电场景仍需在
Windows 实机做故障注入，并以停写备份和恢复对账作为最后防线。
如果 Host `/health` 报告 `unconfirmed_runner_stops > 0`，代表某些任务的模型进程树
尚未确认退出；这类任务即使已标记失败或取消，仍会在 Host 重启后拒绝重复取消与
删除并返回 503。不要手工改写任务索引来绕过标记，也不要把任务状态当成停写证明。
先隔离并停止服务、确认模型进程和其子进程已退出，备份并对账业务库及两个 Host
持久目录；无法核实残留时保留待删除状态，由运维及开发共同处置，不能继续自动清理。
核实已停止旧运行器及所有后代后，可用同一台 macOS、Windows 或 Linux 上的 Python
入口逐任务解除隔离。**先停 API、worker、Projector、Host 与外部写入方，确认不再自动重启；
核对旧模型进程树已退出；创建并验证含数据库及两个 Host 目录的停写备份。**
无 `--apply` 时只读预检，执行时另加 `--runner-tree-verified --apply` 明确记录操作者的核查：

```bash
python scripts/remediate_harness_runner.py --services-stopped \
  --task-id TASK_ID --task-root /path/to/harness-tasks \
  --session-root /path/to/harness-sessions \
  --backup-archive /path/to/verified-backup.tar.gz
python scripts/remediate_harness_runner.py --services-stopped \
  --task-id TASK_ID --task-root /path/to/harness-tasks \
  --session-root /path/to/harness-sessions \
  --backup-archive /path/to/verified-backup.tar.gz \
  --runner-tree-verified --apply
```

`DATABASE_URL` 从环境变量注入；文件密钥模式也可使用
`AI_STUDIO_DATABASE_PASSWORD_FILE`，不要把含密码的 URL 放在命令行。
PostgreSQL 还必须把相同备份恢复到独立库，并通过
`TEST_ATELIER_VERIFIED_RESTORE_DATABASE_URL` 提供该库地址以核对任务归属。
预检核对终态回放、数据库归属、Session 绑定、完整卷哈希及项目其他差异；执行前
再次核对，原子更新单条索引并记录备份清单摘要，不删除任何 Session 或暂存输入。
保留解除前的备份，解除后重新备份并运行只读对账，再启动服务、验证受权回放，
最后重试原项目删除请求。脚本不能证明远端或主动脱离进程组的旧进程已经退出，
`--services-stopped` 与 `--runner-tree-verified` 都是操作者声明；无法完成核查时
**不要执行 `--apply`**。Windows 的目录项刷盘仍依赖目标文件系统保证，需实机故障演练。
上传目录或转换输出目录若仍有未归属文件、符号链接或清理失败，API 同样返回 503
并保留删除待处理状态；核对私有存储后重试，不会只删除数据库引用却遗留敏感文件。
删除待处理期间 Host 可能已清理前几个任务，但业务库仍保留全部任务；此时对账的
`business_only_tasks` 是可重试的中间态，不代表可以手动删除这些业务行。
正在运行的嵌入式 Codex 会话也会阻止项目删除；由 Test Atelier 创建的会话私有
工作区必须清理成功后才能删除其数据库记录。清理失败会保留待处理状态供重试，
旧版指向用户外部 checkout 的会话绝不会以项目清理的名义删除该目录。

## 删除并发与旧 Session 迁移

删除待处理标记会阻止新的正常项目 API 请求。文档上传、转换导出和 Codex 会话创建
在耗时处理后、文件落盘前会重新核对标记，并将项目行锁保持到文件及数据库提交完成；
旧 Codex 会话工作区修复、转换历史保留期清理及项目技能的延迟注册也会在写入前
重新核对删除意图。
迁移 `20261038_project_write_fence` 又为直接或间接归属项目的业务表加上新增/更新
触发器；SQLite 在写事务中拒绝删除待处理的项目写入，PostgreSQL 还会在检查时
锁住项目行。删除子行和将个人工作项的项目关联设为空仍允许。生产 API、worker
与 Projector 启动时核对表与触发器是否齐全；仅用 `create_all` 的开发数据库不宣称
具备此数据库栅栏。

数据库栅栏不保护已经开始的外部文件写入或不在模型中的额外表，也不代替停写
备份。不能把顺序删除和 Host 回执等同于跨数据库/Host/文件卷的全局事务。
新增数据表时必须同步扩展触发器和启动校验；其他只读端点中的惰性写入、
旧 worker 与绕过项目 API 的路径仍需逐项审查。在 PostgreSQL 与文件型 SQLite 上
分别验证“检查后写入、删除意图提交、恢复重试”的竞态；PostgreSQL 真实服务
及 Windows/Linux 实机证据目前仍缺失。

旧版混放 Session 可用 `scripts/migrate_legacy_harness_session.py` **逐任务**迁移，不会
仅凭文件或目录名推断归属。先停止 API、worker、Projector 和 Host 的全部写入方，
在独立目录创建并 `verify` 包含数据库、上传目录、Host 任务与 Session 两卷的备份：

```bash
python scripts/backup_data.py backup --services-stopped \
  --harness-task-dir /path/to/harness-tasks --harness-session-dir /path/to/harness-sessions
python scripts/backup_data.py verify /path/to/verified-backup.tar.gz
export DATABASE_URL='sqlite:////path/to/ai_studio.db'
python scripts/migrate_legacy_harness_session.py --services-stopped \
  --task-id TASK_ID --source-dir /path/to/harness-sessions/legacy-folder \
  --task-root /path/to/harness-tasks --session-root /path/to/harness-sessions \
  --backup-archive /path/to/verified-backup.tar.gz
```

无 `--apply` 的第一次运行只读预检。对账必须一致；数据库项目、用户、终态和
Session 绑定须与 Host 索引吻合；所选直属旧目录只能包含该任务独占的 Session
JSONL，且备份中的两卷文件必须与停写后的当前文件逐一同哈希。工具还会检查来源、
目标和目录树中没有符号链接或 Windows junction；任何重定向都停止迁移，不得借助
junction 跨卷绕过任务归属校验。确认后加 `--apply`
重复该命令：它在私有暂存目录校验副本并原子发布为 `<session-root>/<task-id>/`，
保留旧目录。旧目录与副本并存期间 Host 拒绝删除该任务，防止项目删除留下旧文件。
Windows PowerShell 使用同一个 Python 命令，先设置
`$env:DATABASE_URL = 'sqlite:///C:/path/to/ai_studio.db'`，将 POSIX 路径改为 Windows
路径，并以 PowerShell 的反引号续行或直接单行输入；不依赖 Bash。以上停止服务
由操作者确认，`--services-stopped` 不是进程锁，不可在写入方仍运行时使用。

启动 Host **仅做该任务的受权 Session 回放检查**，确认所有绑定 Session ID 都从
私有目录返回预期历史后，再次停止全部写入方。原备份应单独保留；在原命令上
改用 `--finalize --replay-verified`（不要同时传 `--apply`），工具会再次核对
旧目录剩余文件和私有副本与备份哈希，只清除已被完整复制的旧目录，原子发布
的私有副本仍在；中途删除失败可在停写条件下重试 `--finalize`。最后重新运行
只读对账，确认目标项目 `legacy_layout_sessions` 已降至 0、`status=consistent`，
再通过项目 API 重试删除。**不要直接手工删除旧目录**；未知文件、共享 ID、
损坏的索引/Session、多个旧目录、遗留暂存目录及来源不明的文件均停止自动迁移，
从备份核对后人工处置。

PostgreSQL 的预检、`--apply` 和 `--finalize` 还要求将同一备份恢复到独立数据库，设置
`TEST_ATELIER_VERIFIED_RESTORE_DATABASE_URL` 指向该恢复库以核对任务归属与绑定；
不能把在线生产库作为恢复库。此处只是迁移机制，不替代真实 PostgreSQL 和
Windows/Linux 实机的恢复及故障注入验收。

CI 的 Linux Compose 演练在停止应用写入方后使用真实 PostgreSQL `pg_dump`/`pg_restore`，但两个 Harness 目录是临时合成夹具，并非运行中的 Host 持久卷。它验证跨目录归档、哈希校验及恢复的机械流程，不证明真实 Host 重启回放或跨机器 Secret Store 恢复已经完成。

本机 `python scripts/harness_browser_smoke.py` 则在浏览器假模型任务完成后停止临时服务，对实际生成的 SQLite、上传文件、Host 事件和 Session 做同归档恢复，并从恢复目录重启 API/Host，认证后校验原始文件、分析与用例结果及 Session。它是本机真实 Host 文件恢复证据，不等同于 Windows/Linux 实机或 PostgreSQL 生产环境的跨机器演练。
