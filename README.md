# Test Atelier

面向个人和团队的测试资产工作台：把需求材料、AI 分析、用例编写、人工审核、格式转换和冒烟执行放在同一个项目空间中。AI 服务不可用时，登录、项目协作、导入审核、转换和历史下载仍可独立使用。

仓库建议使用名称 `test-atelier`。运行时环境变量仍保留 `AI_STUDIO_*` 前缀，以兼容现有部署和数据库，不影响品牌名称变更。

> 当前版本适合内网试用和功能验收。真实模型质量、跨机器 Docker 部署、HTTPS 和备份恢复仍需在目标环境单独验收。

## 文档导航

- [完整使用说明](docs/USAGE.md)：首次登录、推荐工作流、角色权限、导入导出和故障排查
- [路线图与验收状态](docs/ROADMAP.md)
- [来源工程基线](docs/source-baseline.md)
- [前端开发说明](web/README.md)
- API 启动后可访问 `http://127.0.0.1:8000/docs` 查看 OpenAPI 文档

## 能力范围

| 模块 | 能力 | 关键约束 |
| --- | --- | --- |
| 工作台 | 项目概览、待审核、分析、草稿、转换和冒烟指标 | 数据按项目隔离 |
| 团队协作 | 项目管理员、评审员、只读成员；成员变更记录 | 全局管理员可救援；只读成员不能写入 |
| 用例库 | `.xls`、`.xlsx`、`.xmind` 预览导入、审核、分派、评论、审计、Excel 导出 | 原值、建议和最终值分开保存；并发修改返回冲突 |
| 智能梳理 | 分类版本、规则候选、AI 候选、逐条接受/修改/驳回 | AI 结果永远是候选，关键歧义需人工确认 |
| 需求分析 | Markdown、文本、图片、PDF、`.doc`、`.docx` 提取、报告编辑和追溯 | 图片/扫描 PDF 依赖 Tesseract；`.doc` 依赖 LibreOffice |
| 用例编写 | 从已确认分析生成草稿，编辑后发布或导出 XMind/Excel | 未确认的关键歧义会阻断生成；导出不等于发布 |
| 项目技能 | 按项目启用/停用 qa-doc-skills 子技能，统一加载外部 Skill 规范和脚本 | 项目状态保存在 `project_skills`；技能源默认位于 `/Users/lazy/huangsheng/project/qa-doc-skills` |
| 用例转换 | Excel/CSV → Excel、XMind → Excel、Excel → XMind，预览和项目级历史 | 单文件 20 MB；历史保留 30/60/180 天 |
| 冒烟执行 | 冒烟集、核心链路、30 分钟预算提示、通过/失败/阻塞/跳过和事件记录 | 执行时冻结套件修订和用例版本 |

## 快速开始

### 1. 准备环境

- Python 3.10+（实测 3.12）
- Node.js 20+
- 可选：Ollama、Tesseract（含 `chi_sim` 和 `eng`）、LibreOffice
- 首次启动前备份已有的 `ai_studio.db` 和 `data/`

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
export AI_STUDIO_ADMIN_PASSWORD='123456'
python -m alembic upgrade head
```

后续命令中的 `python` 指向当前已激活的环境；如果打开新终端，使用 Conda 时先运行 `conda activate test-atelier`，使用 venv 时先运行 `source .venv312/bin/activate`。`AI_STUDIO_SESSION_SECRET` 必须在每次启动时保持不变，否则现有登录会话会失效。首次启动会创建 `AI_STUDIO_ADMIN_USER`（默认 `admin`）账号；修改 `AI_STUDIO_ADMIN_PASSWORD` 不会覆盖已有账号密码。

当前默认管理员密码为 `123456`，仅适合个人本机使用；如果通过局域网、反向代理或共享仓库部署，请在启动前修改为更强的密码。

### 3. 启动 API 和前端

终端一：

```bash
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

终端二：

```bash
cd web
npm run dev -- --host 127.0.0.1
```

打开 Vite 输出的地址，使用 `admin` 和初始化密码登录。前端开发服务器默认把 `/api` 代理到 `http://127.0.0.1:8000`；API 端口变化时设置 `AI_STUDIO_API_TARGET`。

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
7. 在“冒烟执行”配置核心链路，确认用例版本后开始执行

详细操作顺序、角色矩阵、文件限制和失败处理见 [docs/USAGE.md](docs/USAGE.md)。

## 配置

复制 `.env.example` 后按部署方式设置变量。常用变量如下：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `AI_STUDIO_SESSION_SECRET` | 无，必填 | Cookie 会话密钥，至少 32 个字符 |
| `AI_STUDIO_ADMIN_USER` | `admin` | 首次初始化的管理员用户名 |
| `AI_STUDIO_ADMIN_PASSWORD` | `123456`（个人使用） | 首次初始化的管理员密码，至少 6 个字符；共享部署请立即修改 |
| `DATABASE_URL` | `sqlite:///./ai_studio.db` | SQLite 或 `postgresql+psycopg://...` |
| `AI_STUDIO_DATA_DIR` | `./data` | 原始文档和转换结果目录 |
| `AI_STUDIO_OLLAMA_URL` | `http://127.0.0.1:11434` | 本地 Ollama 地址 |
| `AI_STUDIO_OLLAMA_MODEL` | `qwen3:8b` | 本地模型名称，需自行下载 |
| `AI_STUDIO_REMOTE_ENABLED` | `0` | 是否允许管理员使用远程 OpenAI 兼容接口 |
| `AI_STUDIO_REMOTE_URL` / `AI_STUDIO_REMOTE_MODEL` / `AI_STUDIO_REMOTE_API_KEY` | 空 | 远程模型配置；密钥只在 API 进程使用 |
| `AI_STUDIO_CODEX_ENABLED` / `AI_STUDIO_CODEX_COMMAND` / `AI_STUDIO_CODEX_MODEL` | `0` / `codex` / 空 | 可选 Codex CLI provider；也可登录后在“模型配置”中启用 |
| `AI_STUDIO_CLAUDE_ENABLED` / `AI_STUDIO_CLAUDE_COMMAND` / `AI_STUDIO_CLAUDE_MODEL` | `0` / `claude` / 空 | 可选 Claude Code provider；也可登录后在“模型配置”中启用 |
| `COOKIE_SECURE` | `0` | 仅 HTTPS 反向代理时设为 `1` |

登录后进入项目侧栏的“模型配置”，全局管理员可以直接修改本地 Ollama 地址与模型、启用远程 OpenAI 兼容接口，或启用本机 Codex CLI/Claude Code 并填写可执行文件路径和可选模型，然后使用“测试连接”验证服务。CLI 任务使用无 shell 的参数数组、临时 JSON Schema 和只读沙箱启动，不把命令字符串交给 shell；仍应先确认 CLI 的账号、工作区和权限策略。网页保存的远程密钥使用当前会话密钥加密存储，只返回“已配置”状态，不回显原文；修改只影响新建 AI 任务。

Codex CLI 与 Claude Code 均是可选的本机 provider。安装对应开源 CLI 后，在页面启用并选择它即可用于需求分析、用例编写和分类建议；未安装或未启用时，原有 Ollama、规则候选和人工审核流程不受影响。不要把包含敏感信息的材料提交给未审计的本机 CLI 账号。

不要把包含敏感信息的需求文件上传到不受信任的实例；日志不应记录原文、令牌或模型密钥。

## 一键部署与启动

脚本会检查 Conda 和 Node.js，创建或复用名为 `test-atelier` 的 Conda Python 3.12 环境，首次运行时安装 API/前端依赖，执行数据库迁移，然后启动 API 和 Vite。后续运行会根据依赖文件指纹跳过未变化的安装；依赖文件变化时自动重新安装。首次运行会安全地生成会话密钥，并提示输入管理员密码；密码不会写入仓库文件。

macOS（双击 `.command`，或在终端执行）：

```bash
./scripts/setup_and_start_macos.command
```

Windows（双击 `.bat`，或在 PowerShell 执行）：

```powershell
.\scripts\setup_and_start_windows.bat
```

Windows PowerShell 也可以直接运行 `.ps1`；脚本默认自动检测依赖文件是否变化，`-SkipInstall` 可强制跳过依赖安装：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_and_start_windows.ps1 -SkipInstall
```

脚本需要预先安装 Conda（Miniconda/Anaconda）和 Node.js 20+；不会自动安装系统级 Conda、Node.js、Tesseract 或 LibreOffice。API 日志和依赖指纹写入 `.test-atelier-logs/`，该目录已加入 `.gitignore`。需要强制重装时，macOS 可设置 `TEST_ATELIER_FORCE_INSTALL=1`，Windows PowerShell 可设置同名环境变量或省略 `-SkipInstall` 后删除指纹文件。

## 验证与测试

```bash
python -m pytest api/tests -q
cd web && npm run build && npm run lint
```

当前本机验证结果：后端 `38 passed`，前端构建和 lint 退出码为 0；lint 仍会输出少量 React/Hook warning。浏览器回归需要已启动的 API、Vite 和本机 Chrome：

```bash
cd web
AI_STUDIO_E2E_PASSWORD='管理员密码' npm run test:e2e
```

可用 `AI_STUDIO_E2E_URL` 切换站点，`AI_STUDIO_E2E_SAMPLE` 指定真实 `.xls` 样本，`AI_STUDIO_E2E_MODEL_STUB=1` 启用模型替身场景。未提供这些变量的场景会跳过，不代表业务回归已通过。

## 数据库迁移与历史导入

升级数据库（当前 head 为 `20261019_cli_providers`）：

```bash
python -m alembic upgrade head
```

从旧 `ecxel_check` 工程迁移转换历史时，先 `--dry-run`，确认来源目录后再执行。命令只复制文件和预览，不删除来源文件：

```bash
python -m api.migrate_converter_history <ECXEL_CHECK_ROOT>/history --dry-run
python -m api.migrate_converter_history <ECXEL_CHECK_ROOT>/history
```

迁移前备份数据库和 `AI_STUDIO_DATA_DIR`。清空或到期清理转换历史不可恢复；迁移完成后在目标项目的“导入记录/用例转换”页面核对数量和下载。

## Docker Compose

`compose.yaml` 提供 PostgreSQL、单进程 API（含 Tesseract/LibreOffice）和 Nginx 前端。设置变量后启动：

```bash
export POSTGRES_PASSWORD='数据库强密码'
export AI_STUDIO_SESSION_SECRET="$(openssl rand -hex 32)"
export AI_STUDIO_ADMIN_PASSWORD='123456'
docker compose up --build -d
docker compose ps
```

本机访问 `http://127.0.0.1:8080`。`db_data` 和 `files_data` 必须一起备份；跨机器访问前配置 HTTPS、限流并设置 `COOKIE_SECURE=1`。Compose 构建、跨机器恢复和健康检查尚未在本机实测，首次部署请检查镜像构建、数据库迁移和 OCR 语言包。

## 工程结构

```text
api/                 FastAPI、SQLAlchemy、Alembic、任务线程池和测试
web/                 React 19 + Vite + TypeScript 前端
scripts/             macOS 与 Windows 的 Conda 一键部署/启动脚本
docs/                使用说明、路线图和来源基线
data/                运行时文档与转换产物（不要提交业务文件）
ai_studio.db         默认 SQLite 数据库（生产请按部署方案备份）
compose.yaml         PostgreSQL + API + Nginx 部署示例
```

## 开源参考

工程在流程和基础设施选型上参考了 [Kiwi TCMS](https://github.com/kiwitcms/Kiwi)、[FastAPI](https://github.com/fastapi/fastapi)、[Vite](https://github.com/vitejs/vite)、[shadcn/ui](https://github.com/shadcn-ui/ui)、[Ollama](https://github.com/ollama/ollama)、[OpenAI Codex](https://github.com/openai/codex) 和 [Claude Code](https://github.com/anthropics/claude-code) 的公开文档与实践；业务代码、数据模型和界面按本项目需求实现。
