# Test Atelier 前端

React 19、Vite、TypeScript、shadcn/ui、Motion 与 Lucide。登录会话、项目导航和 `/api/v1/projects/{project_id}` 项目边界由根应用统一管理；需求分析、用例编写、用例库、导入记录、分类规则、团队协作和转换工具共享同一项目上下文。

## 本地开发

在仓库根目录先准备 Python API（可用 Conda 或 `venv`，不限定工具），再在本目录执行：

```bash
npm ci
npm run dev -- --host 127.0.0.1
```

常用命令：

| 命令 | 用途 |
| --- | --- |
| `npm run dev` | 启动 Vite 开发服务器 |
| `npm run build` | TypeScript 检查并构建生产静态文件 |
| `npm run lint` | 运行 Oxlint |
| `npm run test:e2e` | 使用本机 Chrome 运行 Playwright 回归 |
| `npm run preview` | 预览已构建的静态文件 |

生产构建会把 React、Motion 和 Lucide 拆成独立 vendor chunk，页面功能模块继续按路由懒加载；这样首屏共享入口不会因为工作台全部依赖增长到不可控的单块体积。

开发服务器默认将 `/api` 转发到 `http://127.0.0.1:8000`。使用根目录平台启动脚本时，代理和 Vite 端口会读取 `config/runtime.json` 的 `server.api_port` 与 `server.web_port`；手动开发且没有该文件时才回退到默认端口。

```bash
npm run dev -- --host 127.0.0.1 --port 5173
```

## E2E

E2E 需要已启动的 API、Vite 和系统 Chrome。设置管理员密码后运行：

```bash
AI_STUDIO_E2E_PASSWORD='管理员密码' npm run test:e2e
```

`AI_STUDIO_E2E_URL` 切换测试站点，`AI_STUDIO_E2E_SAMPLE` 指定真实 `.xls` 样本，`AI_STUDIO_E2E_MODEL_STUB=1` 用于模型替身场景。未设置前置变量的测试会主动跳过。

Harness 浏览器闭环使用一次性数据库和独立 Node Host，可直接运行封装脚本：

```bash
python scripts/harness_browser_smoke.py
```

脚本使用 Fake DSH、临时 SQLite、临时 Harness Host 和临时管理员密码，覆盖 Harness
流式结果、Session 回放、刷新恢复、历史任务切换和用例生成；停写后还会将数据库、
文档和 Host 任务/Session 归档并恢复到新目录，通过认证接口验证回放。它不读取模型 API Key，
不代替真实模型或其他平台实机验收。
Harness 分析记录显示原始结构化结果，不将本地兼容报告显示为 Harness 原文；
用例编写只展示 Harness 任务的原始结果与确认、Session、导出，不混排业务草稿。
可编辑草稿在独立的「草稿审核」入口归 Test Atelier 管理；旧模型分析只在
「历史分析」入口查看，旧草稿仍可审核与发布。未配置 Harness 时，
需求分析和用例编写页均不能退回传统模型生成。直接执行
`npm run test:e2e -- harness.spec.ts` 时仍需自行启动 API 和 Host，并设置
`TEST_ATELIER_HARNESS_E2E=1`，以避免误连生产 Harness。
一次性浏览器封装同时运行 `historical-boundary.spec.ts`，只在其创建的临时 SQLite
数据库中插入历史分析与草稿夹具，核对旧数据入口、人工保存和发布。

## 目录约定

```text
src/App.tsx             登录、项目选择和一级导航
src/components/         首页、审核、分析、团队和项目管理页面
src/converter/           旧转换器的 React 宿主和 Shadow DOM 样式
src/lib/api.ts           带会话和 CSRF 的 API 客户端
e2e/                    Playwright 工作流回归
```

转换组件沿用 `ecxel_check` 的交互语义，但通过 Shadow DOM 隔离样式。修改 API 或路由时请同步更新 E2E 场景和根目录 [使用说明](../docs/USAGE.md)。
