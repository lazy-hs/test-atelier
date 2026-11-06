# Runtime Configuration

Test Atelier production启动不依赖用户导出的环境变量。所有非敏感参数写入
`config/runtime.json`，敏感值写入该文件引用的独立单行文件。实际文件被 `.gitignore`
忽略，权限必须是 `600`；示例文件 `config/runtime.example.json` 不包含任何凭据。

## 初始化

```bash
cd /Users/lazy/huangsheng/project/test-atelier
python3 scripts/configure_full_runtime.py
```

脚本会写入以下文件：

- `config/runtime.json`：路径、端口、Harness 协议、超时、模型地址和密钥文件路径
- `~/.config/test-atelier/harness-model-api-key`：模型 API Key
- `~/.config/test-atelier/harness-scope-secret`：Host 与 API 的作用域密钥
- `~/.config/ai-studio/session-secret`：浏览器会话签名密钥
- `~/.config/ai-studio/admin-password`：首次启动管理员密码

## 启动

```bash
./scripts/start_full_macos.command
```

启动器会读取 JSON，校验路径和文件权限。旧模块若仍通过 `os.environ` 读取参数，
启动进程会在内存中建立兼容映射，并在生产模式启动前清除继承的配置覆盖；这些变量
不是操作员配置入口。不会要求操作员设置 `OPENAI_*`、`TEST_ATELIER_*` 或
`AI_STUDIO_*` 环境变量，也不会把密钥写入仓库、数据库或日志。

## 轮换管理员密码

管理员密码文件只用于首次创建账号。已有账号不会在服务启动时自动跟随文件变化；请使用
显式轮换命令，让密码文件和数据库中的 Argon2 哈希一起更新：

```bash
cd /Users/lazy/huangsheng/project/test-atelier
conda activate test-atelier
python scripts/rotate_admin_password.py
```

命令会交互式要求输入两次新密码，不会把密码放进命令行参数或日志。自动化维护场景可
通过标准输入传入一次密码；注意示例中的明文仍可能被 shell 历史记录，生产环境应改用
受保护的 stdin 管道：

```bash
printf '%s\n' '新密码' | python scripts/rotate_admin_password.py --password-stdin
```

支持显式指定运行配置和管理员用户名：

```bash
python scripts/rotate_admin_password.py \
  --config /path/to/config/runtime.json \
  --username admin
```

轮换失败时会恢复原密码文件；成功后需要使用新密码重新登录，旧浏览器会话不受影响。

## 轮换密钥

重新运行配置脚本并输入新值，然后重启服务。不要把密钥写进 JSON、shell 历史、Compose
插值或提交记录。旧 API Key 曾经暴露时必须先在提供方撤销，再写入新 Key。

## 生产 Compose

在生产主机执行一次：

```bash
cd /path/to/test-atelier
python3 scripts/prepare_production_runtime.py
docker compose -f compose.yaml -f compose.harness-overlay.yaml \
  -f /path/to/test-atelier-harness-extensions/deploy/compose.harness.example.yaml \
  config --quiet
docker compose -f compose.yaml -f compose.harness-overlay.yaml \
  -f /path/to/test-atelier-harness-extensions/deploy/compose.harness.example.yaml \
  up --build -d
```

`prepare_production_runtime.py` 会在 `config/runtime.json` 和
`config/secrets/` 下生成受保护文件。三份 Compose 文件不再依赖 `${...}` 变量插值；
服务只读取固定挂载的 JSON 和 Docker Secret。生产发布禁止要求操作者手动导出变量。

## Harness 模型输出预算

`model.max_tokens` 控制 Harness 每次模型回复的最大输出 token 数。需求分析同时返回结构化结果和完整 Markdown 报告，建议保持 `32768` 或更高；过小会导致 Harness Session 以 `max-tokens` 结束并产生不完整 JSON，最终被 Test Atelier 拒绝为 `schema_invalid`。

## Harness Web Shell 开关

默认不会启动 Harness 原生 Web 界面。需要由一键启动脚本连带启动时，将 `config/runtime.json` 中的 `harness.web_shell.enabled` 改为 `true`：

```json
"web_shell": {
  "enabled": true,
  "member_access": false,
  "restricted_profile": true,
  "port": 8765,
  "gateway_port": 8091
}
```

macOS 一键启动会自动安装 `test-atelier-web` Profile，启动 Harness Web Shell 和本机同源网关，访问 `http://127.0.0.1:8091/harness/`。设置为 `false`（默认值）时不会安装或启动 Web Shell，也不会改变现有 Harness Host、Projector 和 Test Atelier 页面启动流程。Web Shell 仍只绑定回环地址，不应暴露到局域网或公网。
