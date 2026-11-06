# 平台支持范围

本次 Test Atelier + DeepSeek Harness 重构只对以下桌面/服务器操作系统负责：

| 平台 | 运行方式 | 支持边界 |
| --- | --- | --- |
| macOS | `.command` / `.sh` 启动脚本，Node Host 本地运行 | Intel 与 Apple Silicon 的实际依赖差异必须分别通过验证；Intel macOS 的旧 `cryptography` 仅允许开发验收 |
| Windows | PowerShell、`.bat` 启动入口，Node Host 本地运行 | 不依赖 Bash；路径、进程终止、临时目录和 PowerShell 环境必须通过 Windows 验收 |
| Linux | `.sh` 启动入口，Node Host 或 Docker Compose | 生产部署优先使用 Linux；PostgreSQL、worker、Nginx、HTTPS 和恢复演练在 Linux/Docker 环境验证 |

## 明确不在本次范围

- iOS、iPadOS、Android、ChromeOS、tvOS 等移动或嵌入式系统
- FreeBSD、OpenBSD、NetBSD、Solaris 等其他 Unix 系统
- 未列出的桌面兼容层、虚拟机和容器运行时组合
- DeepSeek Harness 桌面 Electron 打包在 Windows/macOS 之外的发行

不在范围的平台不会获得兼容性承诺。Node Host 和 Test Atelier 生产前置检查会将
`darwin`、`win32`、`linux` 之外的平台标记为不支持，避免误报为可部署环境。
测试用例中的 `Pc`、`Web`、`Android`、`IOS` 是被测产品的适用端标签，
不是 Test Atelier 或 DeepSeek Harness 的部署平台承诺；本轮仍只在 macOS、Windows、Linux 运行工作台。

## 验收矩阵

每个支持平台至少需要完成：

1. 环境配置与启动入口验证。
2. Test Atelier API 单元/集成测试。
3. 前端 build、lint 和浏览器 Smoke。
4. Harness Extension Host 协议、取消、Session 回放和环境隔离测试。
5. 目标平台的真实模型 Smoke；密钥只从环境变量或 Secret Store 注入。
6. 停写后的数据库、上传数据与 Host 两个持久目录的跨机器备份恢复演练（见 `docs/harness-backup-recovery.md`）。

独立 Extension 提供对应的 Host 入口：
`test-atelier-harness-extensions/scripts/start_host_macos.command`、
`start_host_windows.ps1`/`start_host_windows.bat` 和 `start_host_linux.sh`。这些入口
会先执行 Host preflight，再启动 Node Host；静态检查不等于目标平台实机验收。

本机先单独启动 Extension Host，再以相同的作用域密钥和
`TEST_ATELIER_HARNESS_URL` 启动 Test Atelier。三个平台的 Test Atelier 启动器在 URL
非空时额外启动独立 `api.harness_projector`，等待新心跳文件出现后才启动前端；
缺少至少 32 字符的作用域密钥会在启动 API 前失败；三个入口都接受内联值或绝对路径的
`TEST_ATELIER_SCOPE_SECRET_FILE`，但两者不可同时设置。独立 Host 的原生预检也支持同一
文件接口。Projector 持续投影 Host
任务，即使浏览器关闭也不依赖详情页轮询。启动器不接管独立 Host 的生命周期，
停写备份必须另外停止 Host。脚本静态/单机测试尚不能证明三平台真实启动。

仓库 CI 已为 Extension Host 契约配置 `ubuntu-latest`、`macos-latest` 和
`windows-latest` 矩阵，会在三种 runner 上执行 Host 协议、取消、Session 回放、环境
隔离和 preflight 测试，并从锁定的上游 SHA 安装真实 dsh，以回环假模型检查技能加载与
跨技能拒绝。Windows Host 通过 Node 执行锁定上游 `package.json` 的公开 `scripts.dsh`
入口，不依赖无法以 `shell:false` 安全启动的 Corepack/pnpm `.cmd` 包装器；脚本
形态或目标路径变化会在预检及 Host 启动时失败关闭，不从旧版本猜测私有源路径。
macOS 上以同一 Node 命令配回环假模型验证了技能加载与越权拒绝，不代替 Windows 实机验收。
Test Atelier 的三平台矩阵也运行有作用域的 Bridge、Schema、
Projector、API 任务、Session 回放、Codex CLI 进程树终止和私有工作区生命周期测试。
Codex 临时目录的创建、归属与删除检查会拒绝符号链接和 Windows junction；CI 使用
跨平台模拟验证此规则。停写备份、只读 Host 存储对账和旧 Session 逐任务迁移也会
拒绝 Windows junction；矩阵包含 Windows 上创建真实 junction 的测试，但其 CI
实际运行结果和并发路径替换故障注入仍待确认。
异常重启后的运行器隔离解除使用三个平台共用的
`scripts/remediate_harness_runner.py`，并已加入同一 CI 矩阵；它要求停写备份、
项目对账和人工核实进程树已退出。macOS 本机用独立 Host 实际重载验证了
解除后的受权任务删除，Windows/Linux 实机运维核查仍须单独验收。
独立 Host 的同一三平台 CI 矩阵现运行实际父子进程夹具，核对取消后模型子进程停止；
macOS 本机已验证。CI 配置和 macOS 结果不能代替 Windows/Linux 的实际运行记录，
也不涵盖模型主动脱离进程组的非受控进程。
项目删除和转换历史清理也会先核对私有文件的存储根、项目目录与目标文件；
若其中有 junction，保留业务记录和待删除状态，不删除被重定向的外部文件。
两套仓库的 CI 不构成同一次跨仓库集成测试；
真实 dsh + 浏览器闭环目前只有 macOS 本机证据。矩阵配置本身不证明 CI 已运行，
自动化兼容性证据也不替代用户目标机器上的真实模型和持久目录验收。

macOS 本机还用真实 dsh 的 ACP Profile 和回环假模型验证了 API 分析/确认/用例、
多 Session 分窗、浏览器技能运行、受保护回放与停写恢复。SDK 传输在取得真实
Session ID 后会先发 `session_bound`，否则 API 按协议拒绝未绑定结果。该测试
不涉及外部模型凭据，也不证明 Windows/Linux 实机或真实模型质量。
回环模型还通过浏览器及独立 Projector 对 Phase 2.5 后的逐项 Phase 2.6
歧义标记待确认、同 Session 续写及 Phase 2.8 自检/报告一致性门禁做了接线验证；
回环模型是固定回执，只证明门禁接线，不构成真实模型语义质量通过的证据。
用例编写另以 `node scripts/harness_local_model_smoke.mjs --case-checkpoint` 和
`--browser-case-checkpoint` 在 macOS 跑通真实上游 dsh、回环假模型、API 和浏览器：
适用端选择、P0/P1 歧义确认、Phase 2.8 预审先于原始用例结果；浏览器在
Phase 2.5 暂停时刷新后仍能恢复确认，并导出 XMind、回放 Session 及演练停写恢复。
这些结果不代表 Windows/Linux 实机通过，也不验证外部模型的语义质量。
同一 macOS 本机用 `--staged-cases-checkpoint` 对 90 条已确认需求执行了分段上传、
多 Session 分窗和最终归并 Session 的 Phase 2.5/2.6/2.8；最终返回的 90 条用例
覆盖全部需求并保留优先级、风险分类和适用端；Phase 2.8 的内部 v2 清单现逐项
对账最终风险类别名称，而非仅比较数量；还通过原技能脚本导出并校验了
90 条用例的 XMind ZIP。测试时发现并修复 Host 对 HTTP
字节分块逐段转字符串导致中文跨块损坏的问题，补充了跨 UTF-8 字符边界的回归测试。
这仍只是回环模型与 macOS 单机证据。
`--browser-case-exception` 另外在 macOS 用真实 dsh 配回环假模型验证单条用例
适用端例外的逐条输入批准、浏览器刷新恢复、原始结果与 XMind 导出；Host 测试
还覆盖拒绝例外不会接受结果。这些不是模型自行判断例外依据的语义验收，也不
替代 Windows/Linux 实机运行。
最新 macOS 浏览器闭环改用独立的 CRLF Scope Secret 文件，同时覆盖 Bridge、
Projector 和 Host 的原生读取路径及停写恢复；这不能替代 Windows 本地文件密钥验收。
Host API Token 另使用独立 CRLF 文件，已在 macOS 的真实 dsh/回环假模型浏览器与
停写恢复闭环中覆盖 Bridge、Projector 和 Host；Windows/Linux 原生文件读取及联合
部署容器运行仍须目标平台验证。
需求分析与受限项目技能的分段材料传输已在 macOS 用调低至 64 KiB 的请求预算
验证真实 dsh 分窗和结果投影；用例分析上下文的独立分段通道已通过跨仓 Fake CLI
闭环和 macOS 真实 dsh 配回环假模型的 90 条需求、单条长需求分片用例 Smoke；
全局元数据/决策及结果超预算仍会失败关闭；
独立 Host 的 Windows/Linux 分段文件创建、原子写入、重启清理和 100 MiB 负载
仍需目标环境验收，不以本机测试替代。

在迁移 head `20261047_auth_session_revocations` 的本机代码上，重新运行
`node scripts/harness_local_model_smoke.mjs --browser` 及
`HARNESS_DSH_PROTOCOL=acp node scripts/harness_local_model_smoke.mjs --browser`：
两种传输均通过真实上游 `dsh`、回环假模型、需求分析、用例编写、项目技能、Session、
浏览器及停写备份恢复闭环。此处只验证接线及可恢复性，不调用用户外部密钥，
不代表真实模型质量或 Windows/Linux 目标环境通过。
ACP 恢复后的同一隔离工程另以真实 Host 删除一条已完成、关联用例任务的分析：
先核对备份恢复后的证据与 Session，停止隔离 Host 时删除返回 503 并保留
`deleting` 状态与任务行，重启相同持久目录的 Host 后重试，再核对任务索引、
事件、私有 Session 目录和业务行已按顺序清理，已发布用例仍保留。单次请求内
部分任务清理失败的重试只有 API 替身回归；Windows/Linux 实机仍待验收。

浏览器和真实 dsh 冒烟脚本现在将自己启动的服务置于独立进程组；结束时在 POSIX
向整组发送终止信号，在 Windows 使用 `taskkill /T /F` 并等待回收。Windows 若父进程
已退出或树终止无法确认，脚本报错而不是宣称安全停写；CI 三平台矩阵包含实际父子
进程写入夹具，但矩阵配置本身不代表 Windows/Linux 运行结果已通过。

需求材料与转换结果的创建时身份只对新记录保存；对应迁移的旧行保持 `NULL`，下载及证据包标记
`unverified`。SQLite 升降级、同尺寸篡改的下载/导出/离线核对已在 macOS 本机验证；
三平台 CI 矩阵配置了上述迁移和证据导出回归，但不代表 Windows/Linux 或 PostgreSQL
已完成目标环境验收。三个平台升级前都须停写备份数据库与上传目录。

本机 API/Projector 停止入口也属于平台契约：macOS 使用 `scripts/stop_macos.command`，Windows 使用
`scripts/stop_windows.ps1` 或 `scripts/stop_windows.bat`，Linux 使用
`scripts/stop_linux.sh`。这些入口只终止启动器分别写入 `.test-atelier-logs/api.pid`
和 `.test-atelier-logs/harness-projector.pid`、且命令行匹配对应模块的进程树；
不按端口查杀其他程序，也不停止独立 Host。

Docker Compose、PostgreSQL、Nginx 和 HTTPS 只在 Linux 生产目标环境验收；macOS 和
Windows 的本地运行不等同于生产部署验收。
