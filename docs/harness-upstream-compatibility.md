# DeepSeek Harness 上游兼容策略

本文定义 Test Atelier 接入 DeepSeek Harness 后的上游同步、版本锁定和回滚规则。目标是复用 Harness 的运行时能力，同时让 Harness 继续保持一个可以从官方仓库重复更新的独立上游工程。

## 1. 当前基线

本轮只验收 `darwin`（macOS）、`win32`（Windows）和 `linux`；其他操作系统不在
兼容矩阵、部署脚本或生产发布承诺内。

| 项目 | 值 |
| --- | --- |
| 本地路径 | `/Users/lazy/huangsheng/project/deepseek-harness` |
| 用户指定上游 | `https://github.com/deepseek-ai/deepseek-harness` |
| 当前本地 `origin` | `https://gitee.com/giaog/deepseek-harness.git` |
| 当前 SHA | `22ddee3792402c3e70ec5f86a63c3e3e895a2f60` |
| 版本 | `0.2.1-rc.3` |
| Node | `>=24.9.0` |
| pnpm | `11.28.5` |
| 许可证 | MIT；第三方依赖以 Harness 的 `THIRD_PARTY_NOTICES.md` 为准 |
| 稳定性 | Developer Preview，公开 API 可能发生破坏性变化 |

首次实现前必须把远程仓库 URL、默认分支、当前 SHA、lockfile 哈希和本地工作区状态记录到发布清单。不能把当前工作区存在的 Test Atelier 未提交修改混入 Harness 仓库。

当前本地 `origin` 与用户指定的 GitHub 地址不一致。Harness 仓库保留现有 `origin`，并使用独立的 `upstream` remote 指向 GitHub；本轮在干净的 `ta-harness-sync/20261024-22ddee379240` 分支完成了上游候选验证，并将该 SHA 作为新的扩展锁定基线。后续仍不自动合并到生产分支；每次更新都必须重复本节同步流程。

2026-10-25 再次运行只取候选的同步入口：官方 `upstream/master` 的本次 `FETCH_HEAD`
仍为 `22ddee3792402c3e70ec5f86a63c3e3e895a2f60`，没有创建新分支、切换工作区或合并；
只读锁定基线门禁通过。该结果只说明当时抓取的候选与本地锁定 SHA 相同，不证明后续日期仍无更新。

锁定 SHA 的上游 `pnpm run typecheck` 已通过 5 个 gate。相关 GUI 测试文件在显式 30 秒
超时下通过（43/43）；聚合 `pnpm run test:gui` 使用默认 5 秒测试超时，在 Intel macOS
出现 2 项 binary-RPC 超时，因此不能把完整 GUI 套件标记为全绿。完整 `pnpm test` 仍未作为
通过证据：其中包含依赖 Linux 资源限制、桌面打包、外部 Codex 和大量故障注入 fixture 的
测试；这不改变锁定工作区的 clean 状态，也不代表上游核心被修改。

## 2. 仓库边界

### 允许的修改

所有 Test Atelier 特有代码放在独立扩展仓库或目录，例如：

```text
/Users/lazy/huangsheng/project/test-atelier-harness-extensions/
  packages/ta-domain-contracts
  packages/ta-api-bridge
  packages/ta-requirement-analysis
  packages/ta-case-authoring
  packages/ta-project-skills
  packages/ta-client-ui
  profiles/test-atelier.yml
```

扩展只能依赖 Harness 明确导出的包名和类型，不能导入 Harness 的私有 `src` 路径、构建产物内部路径或 `vendor` 源码。扩展仓库应通过 workspace 或已发布包引用 Harness，不能复制粘贴 Harness 核心实现。

### 禁止的修改

除非上游本身的修复需要且单独提交到上游，否则不得在 Test Atelier 分支长期修改以下目录：

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

不得把 Test Atelier 的 SQLAlchemy 模型、项目权限、业务 Prompt、业务表或业务状态机直接塞进 Harness 核心。不得修改 Harness 的默认 Profile 来偷偷启用 Test Atelier 功能；使用独立 Profile 和插件组合。

## 3. 同步流程

每次同步上游都使用独立分支，并保留可回退的上一份锁定版本：

```text
upstream/<sha>
  -> ta-harness-sync/<date>-<sha>
  -> compatibility checks
  -> extension integration checks
  -> release candidate
```

建议步骤：

1. 记录 Test Atelier API、扩展包和生产 Profile 的当前版本。
2. 在干净的 Harness 工作区获取上游提交，不在本地直接改核心目录。
3. 执行 Harness 自己要求的 `pnpm install --frozen-lockfile`、类型检查、相关单元测试和构建检查。
4. 在扩展仓库重新安装依赖并运行扩展包类型检查、协议测试和最小 Profile 启动测试。
5. 执行 Test Atelier API Bridge 的契约测试、任务状态测试、结果 Schema 测试和权限测试。
6. 通过后更新兼容矩阵、锁定 SHA 和升级说明；不通过时保留旧 SHA，不把新版本部署到生产。

扩展仓库提供 `scripts/prepare_upstream_sync.py` 作为安全的同步准备入口。脚本与
只读 `check:upstream` 校验默认寻找扩展 checkout 同级的 `deepseek-harness`，也可
通过 `HARNESS_ROOT`（同步脚本还可通过 `--harness-root`）指定非同级路径；不依赖
开发机的 macOS 绝对路径，因此 macOS、Windows 和 Linux 使用相同的目录约定。Test Atelier 的
`scripts/verify_harness_baseline.py` 同样从自身同级寻找这两份 checkout；非同级部署时
分别用绝对路径设置 `HARNESS_ROOT` 和 `HARNESS_EXTENSIONS_ROOT`，基线 JSON 只记录版本与
哈希，不再承担开发机目录定位。默认模式只
fetch 官方 GitHub `upstream/master` 并输出候选 SHA；`--apply` 才会在干净 Harness
checkout 创建 `ta-harness-sync/<date>-<sha>` 分支。脚本不执行 `reset`、`clean`、`merge`
或覆盖文件，远端 URL 必须精确对应官方 HTTPS/SSH 仓库，拒绝伪装路径；候选直接取本次
fetch 的 `FETCH_HEAD`，切换前复核工作区与 HEAD。之后仍必须按上述兼容性检查完成评审。

生产或共享环境还必须使用独立的 `TEST_ATELIER_SCOPE_SECRET`，通过 Secret Store 同时
注入 API 与 Host；不能把它写入 Profile、Compose 文件、Session、任务 JSON 或日志。

不能使用 `git reset --hard` 覆盖 Test Atelier 工作区。同步脚本必须在 Harness 仓库和扩展仓库之间分开运行，并输出实际 SHA、Node/pnpm 版本、依赖 lockfile 哈希和检查结果。

## 4. 兼容矩阵

扩展仓库维护一份机器可读的矩阵，至少包含：

| Harness SHA | Harness 版本 | 扩展版本 | Node | pnpm | Test Atelier API | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| `22ddee379240...` | `0.2.1-rc.3` | 锁定基线 `0.1.8` (`333967c`) | 24.9+ | 11.28.5 | 当前主线 | Harness typecheck 五个 gate、Profile composition、Host/API/技能拒绝/Session replay、白名单 generic skill-execution、Host 敏感回放与任务索引隔离、Fake Host 浏览器 refresh smoke 通过；部署镜像只复制运行时输入，不复制 `.git`、本地 `data`、`node_modules` 或测试产物；Profile 安装已提供无 Bash 的 Python 入口，兼容 macOS、Windows、Linux；相关 binary-RPC 文件在 30 秒超时下 43/43 通过，但聚合 `test:gui` 默认 5 秒超时在 Intel macOS 出现 2 项超时，因此未宣称完整上游测试全绿；当前扩展检出有未提交修改，发布前须重新运行基线校验并更新锁定 SHA；真实模型闭环和生产部署待验收 |

矩阵的“通过”必须同时覆盖：

- Profile 可以启动和关闭，插件卸载后没有残留注册。
- Harness Session 可以创建、追加事件、回放和恢复。
- API Bridge 能完成认证、项目权限、流式事件、取消和确认。
- `requirement-analysis.v1`、`test-cases.v1` 与白名单 `skill-execution.v1` 能被 Test Atelier 严格解析。
- 项目技能停用后，服务端拒绝任务，不能只依赖前端隐藏按钮。
- Test Atelier 原有历史数据、旧 API 读取和人工流程不受影响。

## 5. 破坏性变化处理

Harness 当前为 Developer Preview。每次升级必须检查：

- Profile、Bundle、Plugin、Session、RPC 和 Client UI 的公开导出是否变化。
- Session 格式版本、事件判别字段和回放行为是否变化。
- LLM、Tool、Skill 和 Web 服务的配置键是否变化。
- Node 原生依赖、Electron/桌面依赖和跨平台打包是否影响服务端部署。

发现破坏性变化时，先在扩展层写兼容适配，不在 Harness 核心加 Test Atelier 专用分支。若无法兼容，应冻结在上一份经过验收的 SHA，并在 `docs/ROADMAP.md` 或发布说明中记录阻塞原因。
Windows 原生 Host 无法通过 `shell:false` 启动 Corepack/pnpm 的 `.cmd` 包装器，
因此从锁定 checkout 的 `package.json` 读取上游公开 `scripts.dsh`，只解析目前审核过的
`node --import tsx/esm <checkout 内 CLI 文件>` 单进程格式；缺失、越界、符号链接或
需要 shell 的新脚本均由预检和 Host 拒绝，不猜测新的上游内部路径。同步新 SHA 时
必须核对此入口并分别在 Windows 实机重新运行 Profile、技能和取消/回放验收。

## 6. 回滚

部署配置必须能通过一个版本变量切换 Harness SHA 和扩展版本。回滚只回滚 Harness 运行时和扩展包，不回滚 Test Atelier 数据库迁移；数据库迁移必须向后兼容，或在升级前提供明确的双读/双写过渡期。

回滚后必须验证：

1. 新建任务不会继续发送到已回滚的运行时。
2. 已完成的 `analysis_runs` 和 `case_drafts` 仍能读取。
3. 运行中的任务会被取消、恢复或标记为未知，不得伪造为成功。
4. Harness Session 仍可作为只读回放记录访问。
5. 审计日志包含升级、回滚、任务取消和结果提交事件。

## 7. 责任分界

Harness 上游负责通用 Agent、LLM、Tool、Skill、Session、回放、取消和 Web Shell。Test Atelier 扩展负责领域结果、项目权限桥接、业务落库和页面集成。任何跨界修改都必须先更新 `harness-test-atelier-boundary.md` 和兼容矩阵，再实现代码。
