# Harness Web Shell 集成勘察（未完成集成）

## 当前证据

- 锁定上游 `22ddee3792402c3e70ec5f86a63c3e3e895a2f60` 的 Web 入口位于
  `apps/web/src/main.ts`，由 `@deepseek-ai/dsh-client-web` 的 `AppWebEntry` 启动，
  静态页面由 CLI 的 Web Profile 服务提供。上游 Web 使用 React 18；Test Atelier
  自身前端使用 React 19，不能把两个应用的组件直接装入同一个 React 根节点。
- 上游 `@deepseek-ai/dsh-client-ui-slots` 提供客户端插件注册和销毁约定，但具体
  页面入口、鉴权、浏览器 Bundle 和 Host 提供的 boot graph 必须在真实 Web Profile
  下验证，不能只凭存在 `slots` 包就声称业务页面已经挂入 Shell。当前锁定版本的
  页面扩展位是 keyed `main`，侧栏入口是 list `sidebar.panellist`；上游自身的
  `ui-schedule` 和 `ui-plugin-manager` 均通过这两个 Slot 注册主页面及导航，并在
  插件销毁时移除。不能替换整个 React 根节点或直接覆写上游侧栏组件。
- Host 的客户端模块系统读取插件 `package.json` 中的 `dsh.client`、`./client`
  导出和 `platform: web` 声明。浏览器 Bundle 必须采用上游的 lazy-CJS 模块工厂
  格式；`docs/cookbook/adding-a-settings-card.md` 明确说明仓库外插件需要自行
  复现构建配置，不能把普通 CJS 文件或上游私有源码路径当成稳定插件 API。
- 锁定 pnpm 11.28.5 下执行
  `pnpm --filter @deepseek-ai/dsh-web-frontend build` 成功，产出本地 `apps/web/dist`
  （约 21 MiB）；构建仅提示部分分包超过 500 kB。单独构建前端后立即启动
  Web Profile 会因客户端插件产物缺失而失败；继续仅构建客户端库时，HTTP
  页面虽可服务，浏览器仍显示多个客户端插件未激活。因此正式启动必须先使用
  上游完整 `pnpm run build`，不能把孤立的 Vite 成功当作 Shell 可用。
- 完整上游构建实际通过 10 个 gate。随后以临时 `DSH_HOME`、回环随机端口和
  未配置模型密钥的 Web Profile 启动，HTTP 完成临时访问令牌的 Cookie 换取后
  返回包含 `#root` 和 `__DSH_BOOT__` 的 HTML。隔离的 `agent-browser` 会话中，
  预览说明可关闭，真实侧栏、默认工作区、会话输入和模型选择均渲染；页面
  随后要求登录或添加 API Key，未执行模型调用。浏览器截图留在受控本机临时目录。
  临时 Web 服务与浏览器均已关闭，上游 Git 工作树仍干净。这证明原生 Shell
  能本机启动和渲染，**不**证明 Test Atelier 插件已挂入、业务会话已授权，
  或三平台运行和跨应用鉴权可用。
- 现有独立 Test Atelier Extension Profile 使用 `base` 加 `acp-app` 运行 `dsh`；
  已验证的需求分析、用例、技能和 Session 链路是 Host/API 与 Test Atelier 自身
  React 页面，并**非** Harness Web Shell 已接管页面。已有页面及历史结果需保持
  可用，不能因为 Web Shell 尚未接入就把它们撤下。

## 可选 Web 试点进展

- 独立 Extension 中新增 `packages/ta-web-panel` 和显式 `--with-web` 安装的
  `test-atelier-web` Profile。默认两份运行 Profile 不变；构建脚本从仓外源码
  生成锁定版本要求的 lazy-CJS 客户端文件，`lib/` 不作为源码提交。插件仅
  注册 `main` 和 `sidebar.panellist`，不写入上游仓库，不持有 Host Token，
  卸载时移除样式和 Slot。该 Profile 仍是实验性受限试点，结果区域只读，任务
  创建和确认走 Test Atelier 的既有权限 API，尚未通过正式发布门禁。
- 临时 Home 中实际安装三份 Profile 后，隔离浏览器可看到侧栏入口，点击后
  主区域展示只读项目、任务和完成结果面板；窄屏 390 px 截图确认单列布局。
  当前 Web origin 没有 Test Atelier 的 `/api/v1` 同源路由，因此面板如实提示
  “同源项目 API 尚未接入”，没有取得项目或任务数据、没有执行业务任务。
  临时 Web 服务及隔离浏览器已关闭；上游 Git 工作树保持干净。
- 面板通过 `credentials: same-origin` 访问现有受项目权限保护的 API，
  只读取 `completed` 且任务、项目和 Schema 一致的 `/result`；切换项目或类型
  时不显示上一个项目的缓存结果。**这只是客户端防错**；下述本机管理员登录
  试点只验到项目列表读取，不能代替普通成员的项目 ACL、任务归属、CSRF
  写入或生产跨应用登录验收。浏览器不能持有通用 Host 管理 Token。
- 面板现在具备可选的任务工作区：按当前项目加载材料、已完成 Harness 分析和
  已启用项目技能；需求分析、用例编写和项目技能分别调用现有项目权限 API 创建
  Harness 任务。创建使用客户端生成的幂等键，测试用例仅能选择已完成且已保留
  原始分析结果的分析；材料 ID、项目 ID、任务类型和 Schema 在客户端显示前再
  核对一次，服务端仍是最终权限与来源校验方。任务通过同源 SSE 回放，断线后
  从最后序号继续；`waiting_confirmation` 显示确认摘要、歧义选项和说明，确认、
  拒绝、取消及失败重试均转发至既有 API。只有状态为 `completed` 且结果 Schema、
  任务、项目、结果 ID 和 Session 均匹配时才读取 `/result`，暂存结果、进度和
  失败内容不会进入交付区。
- 这一批任务交互尚未做真实模型运行验收；此前管理员同源浏览器闭环只验证了
  项目列表和只读结果面板。需要用隔离假 Host 和真实回环模型分别覆盖三类任务、
  SSE 断线恢复、人工确认、取消、重试、刷新与项目切换，不能凭客户端代码存在
  就声称任务闭环完成。
- 随后用隔离 Node 假 API（固定项目、材料和已完成分析）与真实锁定版 `dsh`
  Web Profile 做了浏览器正向 Smoke：同源面板选中材料，点击“开始需求分析”，
  假 API 收到幂等任务创建；任务列表进入 `completed`，面板核对项目、任务、
  Schema、结果 ID 和 Session 后显示原始报告。第一次假 API 故意返回错误任务 ID，
  面板保持空交付区并报告“任务结果与项目、状态或交付 Schema 不一致”；修正夹具
  后再次通过。该 Smoke 只证明创建/完成/结果门禁和真实 Shell 挂载，不证明真实
  模型、人工确认、用例或项目技能语义。
- 本轮又收紧了结果门禁，要求原始结果带有非临时 Session 和字符串数组形式的
  `source_document_ids`；已更新隔离假 API 夹具并重新跑过需求分析 Smoke，
  真实 Shell 页面再次显示完成任务的结果 ID/Session。随后在同一浏览器中确认
  用例编写页能加载已确认 Harness 分析选择器，项目技能页能加载授权材料和
  `api-parsing` 技能选择器；这两项只验证表单门禁，未提交任务。
- 新增**只限本机**的独立 Node 同源网关试点：网关在 `/` 转发 Test Atelier
  前端，`/api/` 转发原 API，`/harness/` 剥离前缀转发上游 Web Profile；
  `session` 和 `dsh-auth-*` Cookie 分别只交给对应服务，Web Cookie 的路径
  重写至 `/harness/`。每个 Shell HTTP 请求与 WebSocket 握手先向 API 的
  `/api/v1/harness/web-access` 校验会话；该端点默认关闭，显式开启后默认只放行
  管理员。部署受限 `ta-restricted` Profile 后，可额外设置
  `TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS=1` 与
  `TEST_ATELIER_WEB_SHELL_RESTRICTED_PROFILE=1`，此时至少拥有一个未归档、未删除
  项目成员关系的普通账号也可进入 Shell；项目、材料和任务仍由各业务 API 单独执行 ACL。
  网关只绑定 `127.0.0.1`，不改变生产 Nginx/Compose 拓扑。
- `test-atelier-web` Profile 现显式禁用 Harness 原生设置、模型/API Key onboarding、
  Plugin Manager、原生 conversation/chat 和其他原生交互面；模型凭据只能由部署
  环境注入，不能在浏览器 Shell 中配置或安装插件，业务任务只能从 Test Atelier
  面板发起。该策略已通过锁定上游的临时 Profile composition 检查，并由独立
  Profile policy 回归保护。
- macOS 本机临时 SQLite、临时 Harness Home、Vite 与真实锁定上游 `dsh`
  Web Profile 同时启动；隔离浏览器先在网关同源登录 Test Atelier 并创建
  试点项目，再通过上游一次性访问 URL 进入挂载在 `/harness/` 的 Shell，点击
  原生侧栏后面板成功读取并显示该项目。关闭欢迎弹窗后面板没有“同源 API
  尚未接入”错误；登出后重访 Shell 被网关拒绝。浏览器及四个临时进程已停止。
  这一闭环**没有创建 Harness 业务任务或执行模型**；网关假后端测试覆盖 HTTP
  Cookie 隔离、权限拒绝、WebSocket 转发和重复 Cookie 拒绝，API 测试覆盖
  默认关闭、管理员放行与普通账号拒绝；新增回归覆盖成员开关、活跃项目成员和无项目账号。
  服务端现已为登录 Session 写入随机
  nonce，登出后将 nonce 哈希写入 `session_revocations`；网关 WebSocket 按有界
  间隔复核，API 拒绝后会关闭已有连接。该吊销和活动连接关闭已由迁移、API 回归
  与网关集成测试验证，但尚未在 Windows/Linux 实机和生产代理中验收。

## 实施顺序

1. 已在独立 Extension 仓库建立可选 Web Profile 和客户端插件，不修改上游
   `apps/web`、CLI 或通用 Slot；通过 `main` 与 `sidebar.panellist` 验证安装、
   资源加载和点击渲染。仍需实测插件禁用/卸载与上游更新后的兼容性、三平台构建
   和完整可复现发布基线；不能把 Slot 可用当作业务完成。
2. 已通过本机同源反向代理让浏览器访问 Harness Shell，业务 API 仍由 Test Atelier
   校验 Cookie、CSRF、项目 ACL、任务归属与只读结果；Shell/插件不可持有数据库
   连接或通用 Host 管理 Token。分析、用例和技能视图只展示已完成任务的受权限
   保护原始 `/result`，人工决定与业务审核明确分区。上游 Shell 自带会话、模型
   设置与 API Key 入口不受此只读面板保护：对外发布前必须限制直连端口，完成
   整个 Shell 的访问策略和未授权功能审计，不能只给 `/api/v1` 加权限就开放网页。
  当前仍是 loopback 管理员试点；普通成员开关已实现但默认关闭，且只适用于已
  部署受限 Profile。生产预检在普通成员开关开启时会失败关闭，直到生产代理和
  能力边界完成评审；正式开放前仍须完成 Shell 默认操作限制和三平台测试。
3. 客户端插件在 Harness 声明的 Slot 中注册导航和业务视图，随插件生命周期
   释放订阅、Session 监听和样式；不要跨 React 18/19 根节点移植组件，也不要
   以 iframe 作为最终方案。上线采用可回滚入口，不能让两个页面同时各自生成
   结果或形成第二份业务状态。
4. 在 macOS、Windows、Linux 的目标启动方式分别验证鉴权、项目隔离、确认、
   原始结果、取消、回放、刷新、故障恢复、移动端、插件卸载和上游版本更新；
   同时核对原 Test Atelier 路由可回退。真实模型质量评审、容器/数据库及发布
   基线仍由各自独立门禁完成，前端构建不替代这些门禁。

当前已完成原生 Slot 的可选挂载、本机管理员试点的同源项目读取和实验性任务交互；
**尚未形成可发布的同源代理与任务安全基线**。需要另行实现并验收生产代理、
Test Atelier Cookie/CSRF、普通项目成员的项目 ACL、任务创建与
人工确认、失败/取消/刷新恢复、插件卸载/更新和三平台实际运行。不得复用 Shell
默认“登录或添加 API Key”引导绕过项目权限，亦不得把已暴露的模型密钥输入浏览器。
真实模型质量、Docker/PostgreSQL 和整理提交后的发布基线也仍未验收；已有业务
页面与历史数据保持可用，Shell 集成不得标为完成。
