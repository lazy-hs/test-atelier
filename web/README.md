# Test Atelier 前端

React 19、Vite、TypeScript、shadcn/ui、Motion 与 Lucide。登录会话、项目导航和 `/api/v1/projects/{project_id}` 项目边界由根应用统一管理；需求分析、用例编写、用例库、导入记录、分类规则、团队协作、冒烟执行和转换工具共享同一项目上下文。

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

开发服务器默认将 `/api` 转发到 `http://127.0.0.1:8000`，API 端口变化时：

```bash
AI_STUDIO_API_TARGET=http://127.0.0.1:8010 npm run dev -- --host 127.0.0.1
```

## E2E

E2E 需要已启动的 API、Vite 和系统 Chrome。设置管理员密码后运行：

```bash
AI_STUDIO_E2E_PASSWORD='管理员密码' npm run test:e2e
```

`AI_STUDIO_E2E_URL` 切换测试站点，`AI_STUDIO_E2E_SAMPLE` 指定真实 `.xls` 样本，`AI_STUDIO_E2E_MODEL_STUB=1` 用于模型替身场景。未设置前置变量的测试会主动跳过。

## 目录约定

```text
src/App.tsx             登录、项目选择和一级导航
src/components/         首页、审核、分析、团队和冒烟页面
src/converter/           旧转换器的 React 宿主和 Shadow DOM 样式
src/lib/api.ts           带会话和 CSRF 的 API 客户端
e2e/                    Playwright 工作流回归
```

转换组件沿用 `ecxel_check` 的交互语义，但通过 Shadow DOM 隔离样式。修改 API 或路由时请同步更新 E2E 场景和根目录 [使用说明](../docs/USAGE.md)。
