#!/bin/zsh

# One-click local setup and startup for macOS.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_CONFIG="$ROOT_DIR/config/runtime.json"
ENV_NAME="test-atelier"
LOG_DIR="$ROOT_DIR/.test-atelier-logs"
DEP_MARKER="$LOG_DIR/dependencies-$ENV_NAME.sha256"
API_PID_FILE="$LOG_DIR/api.pid"
API_PID=""
PROJECTOR_PID_FILE="$LOG_DIR/harness-projector.pid"
PROJECTOR_PID=""
PROJECTOR_HEARTBEAT=""
WEB_SHELL_PID=""
GATEWAY_PID=""
CONFIG_ONLY=0
SKIP_CONFIG=0
for argument in "$@"; do
  case "$argument" in
    --config-only) CONFIG_ONLY=1 ;;
    --skip-config) SKIP_CONFIG=1 ;;
    *) print -u2 "用法：$0 [--config-only] [--skip-config]"; exit 2 ;;
  esac
done

cleanup() {
  if [[ -n "$PROJECTOR_HEARTBEAT" ]]; then
    rm -f "$PROJECTOR_HEARTBEAT"
  fi
  if [[ -n "$PROJECTOR_PID" ]] && kill -0 "$PROJECTOR_PID" 2>/dev/null; then
    pkill -TERM -P "$PROJECTOR_PID" 2>/dev/null || true
    kill "$PROJECTOR_PID" 2>/dev/null || true
    wait "$PROJECTOR_PID" 2>/dev/null || true
  fi
  if [[ -n "$PROJECTOR_PID" && -f "$PROJECTOR_PID_FILE" && "$(cat "$PROJECTOR_PID_FILE")" == "$PROJECTOR_PID" ]]; then
    rm -f "$PROJECTOR_PID_FILE"
  fi
  if [[ -n "$API_PID" ]] && kill -0 "$API_PID" 2>/dev/null; then
    pkill -TERM -P "$API_PID" 2>/dev/null || true
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
  if [[ -n "$API_PID" && -f "$API_PID_FILE" && "$(cat "$API_PID_FILE")" == "$API_PID" ]]; then
    rm -f "$API_PID_FILE"
  fi
  for child_pid in "$GATEWAY_PID" "$WEB_SHELL_PID"; do
    if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
      pkill -TERM -P "$child_pid" 2>/dev/null || true
      kill -TERM "$child_pid" 2>/dev/null || true
      wait "$child_pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

die() {
  print -u2 "错误：$1"
  exit 1
}

[[ -d "$ROOT_DIR/api" && -d "$ROOT_DIR/web" ]] || die "请从 test-atelier 工程目录运行此脚本"
[[ -s "$RUNTIME_CONFIG" ]] || die "未找到配置文件 $RUNTIME_CONFIG，请先运行 scripts/configure_full_runtime.py"
command -v npm >/dev/null 2>&1 || die "未找到 npm，请先安装 Node.js 20+"
command -v openssl >/dev/null 2>&1 || die "未找到 openssl，请先安装 OpenSSL"
command -v python3 >/dev/null 2>&1 || die "未找到 python3，请先安装 Python 3"

CONDA_EXE="$(command -v conda 2>/dev/null || true)"
if [[ -z "$CONDA_EXE" ]]; then
  for candidate in "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" \
    "/opt/miniconda3/bin/conda" "/opt/anaconda3/bin/conda"; do
    if [[ -x "$candidate" ]]; then
      CONDA_EXE="$candidate"
      break
    fi
  done
fi
[[ -n "$CONDA_EXE" ]] || die "未找到 Conda，请先安装 Miniconda 或 Anaconda"

ENV_CREATED="false"
if [[ "$SKIP_CONFIG" == "1" ]]; then
  "$CONDA_EXE" env list | awk '{print $1}' | grep -qx "$ENV_NAME" || die "未找到 Conda 环境 $ENV_NAME，请先运行 configure_environment_macos.command"
elif ! "$CONDA_EXE" env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  print "创建 Conda 环境 $ENV_NAME ..."
  "$CONDA_EXE" create -n "$ENV_NAME" python=3.12 -y
  ENV_CREATED="true"
fi

CONFIG_DIR="$HOME/.config/ai-studio"
config_value() {
  python3 - "$RUNTIME_CONFIG" "$@" <<'PY'
import json, sys
from pathlib import Path
value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for key in sys.argv[2:]:
    value = value[key]
if isinstance(value, bool): print("1" if value else "0")
else: print(Path(value).expanduser() if sys.argv[-1].endswith("_file") else value)
PY
}
SECRET_FILE="$(config_value test_atelier session_secret_file)"
ADMIN_PASSWORD_FILE="$(config_value test_atelier admin_password_file)"
SECRET_DIR="$(dirname "$SECRET_FILE")"
ADMIN_PASSWORD_DIR="$(dirname "$ADMIN_PASSWORD_FILE")"
mkdir -p "$CONFIG_DIR" "$SECRET_DIR" "$ADMIN_PASSWORD_DIR" "$LOG_DIR"
chmod 700 "$CONFIG_DIR" "$SECRET_DIR" "$ADMIN_PASSWORD_DIR" "$LOG_DIR"

if [[ "$SKIP_CONFIG" != "1" ]]; then
  DEP_FINGERPRINT="$(
    shasum -a 256 "$ROOT_DIR/api/requirements-dev.txt" "$ROOT_DIR/web/package-lock.json" \
      | shasum -a 256 | awk '{print $1}'
  )"
  DEP_MARKER_VALUE=""
  [[ -s "$DEP_MARKER" ]] && DEP_MARKER_VALUE="$(cat "$DEP_MARKER")"
  if [[ "$ENV_CREATED" == "true" || \
        "$DEP_MARKER_VALUE" != "$DEP_FINGERPRINT" || ! -d "$ROOT_DIR/web/node_modules" ]]; then
    print "安装 Python 依赖 ..."
    "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" python -m pip install -r "$ROOT_DIR/api/requirements-dev.txt"
    print "安装前端依赖 ..."
    (cd "$ROOT_DIR/web" && npm ci)
    print -r "$DEP_FINGERPRINT" > "$DEP_MARKER"
  else
    print "依赖未变化，跳过 Python 和前端依赖安装"
  fi
else
  [[ -d "$ROOT_DIR/web/node_modules" ]] || die "未找到前端依赖，请先运行 configure_environment_macos.command"
fi

if [[ "$SKIP_CONFIG" == "1" && ! -s "$SECRET_FILE" ]]; then
  die "未找到会话密钥，请先运行 configure_environment_macos.command"
fi
if [[ "$SKIP_CONFIG" != "1" && ! -s "$SECRET_FILE" ]]; then
  umask 077
  openssl rand -hex 32 > "$SECRET_FILE"
fi
BOOTSTRAP_MARKER="$LOG_DIR/admin-initialized"
if [[ "$CONFIG_ONLY" != "1" && ( "$ENV_CREATED" == "true" || ! -f "$BOOTSTRAP_MARKER" ) ]]; then
  [[ -r "$ADMIN_PASSWORD_FILE" ]] || die "管理员密码文件不可读：$ADMIN_PASSWORD_FILE"
else
  # The marker is created after the first successful API readiness check.
fi

if [[ "$SKIP_CONFIG" != "1" ]]; then
  print "执行数据库迁移 ..."
  (cd "$ROOT_DIR" && "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" python -m alembic upgrade head)
fi

if [[ "$CONFIG_ONLY" == "1" ]]; then
  print "运行环境配置完成；请执行 scripts/start_macos.command 启动服务"
  exit 0
fi

PORTS="$(cd "$ROOT_DIR" && "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" \
  python "$ROOT_DIR/scripts/read_server_ports.py" | tail -n 1)"
read -r API_PORT WEB_PORT <<< "$PORTS"
[[ "$API_PORT" == <-> && "$WEB_PORT" == <-> ]] || die "settings.json 中的端口配置无效"
HARNESS_ENABLED="$($CONDA_EXE run --no-capture-output -n "$ENV_NAME" python -c 'from api.runtime_config import runtime_environment; print(runtime_environment().get("TEST_ATELIER_HARNESS_URL", ""))' | tail -n 1)"
WEB_SHELL_ENABLED="$(config_value harness web_shell enabled)"
WEB_SHELL_PORT="$(config_value harness web_shell port)"
GATEWAY_PORT="$(config_value harness web_shell gateway_port)"
HARNESS_ROOT="$(config_value paths harness_root)"
EXTENSIONS_ROOT="$(config_value paths extensions_root)"
DSH_HOME="$(config_value paths dsh_home)"
HARNESS_SESSION_ROOT="$(config_value paths session_root)"
WEB_SHELL_LOG="$LOG_DIR/harness-web-shell.log"
GATEWAY_LOG="$LOG_DIR/harness-web-gateway.log"
if [[ -n "$HARNESS_ENABLED" ]]; then
  "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" python \
    "$ROOT_DIR/scripts/harness_secret_file.py" --config "$RUNTIME_CONFIG" || die "已配置 Harness Host，但作用域密钥无效或不可读取"
elif [[ "$($CONDA_EXE run --no-capture-output -n "$ENV_NAME" python -c 'from api.runtime_config import runtime_environment; print(runtime_environment().get("TEST_ATELIER_HARNESS_ONLY", "0"))' | tail -n 1)" == "1" ]]; then
  die "Harness-only 模式必须先配置 TEST_ATELIER_HARNESS_URL"
fi

print "启动 API（日志：$LOG_DIR/api.log）..."
if [[ -f "$LOG_DIR/api.log" ]]; then
  LOG_BYTES="$(stat -f%z "$LOG_DIR/api.log" 2>/dev/null || print 0)"
  if (( LOG_BYTES > 52428800 )); then
    mv "$LOG_DIR/api.log" "$LOG_DIR/api.log.1"
  fi
fi
API_REUSED=0
EXISTING_API_PID=""
if command -v lsof >/dev/null 2>&1; then
  EXISTING_API_PID="$(lsof -nP -tiTCP:"$API_PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
fi
if [[ "$EXISTING_API_PID" == <-> ]]; then
  EXISTING_API_COMMAND="$(ps -p "$EXISTING_API_PID" -o command= 2>/dev/null || true)"
  EXISTING_API_CWD="$(lsof -p "$EXISTING_API_PID" -a -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
  if [[ "$EXISTING_API_COMMAND" == *"api.uvicorn_entrypoint"* && "$EXISTING_API_CWD" == "$ROOT_DIR" ]] && \
     curl -fsS "http://127.0.0.1:$API_PORT/api/v1/health/ready" >/dev/null 2>&1; then
    API_REUSED=1
    print "复用已运行的 Test Atelier API（PID $EXISTING_API_PID，端口 $API_PORT）"
  else
    die "端口 $API_PORT 已被其他进程占用（PID $EXISTING_API_PID），请先停止该进程或修改 config/runtime.json 的 server.api_port"
  fi
fi
if [[ "$API_REUSED" == "0" ]]; then
  (cd "$ROOT_DIR" && "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" python -m api.uvicorn_entrypoint --host 127.0.0.1 --port "$API_PORT" > "$LOG_DIR/api.log" 2>&1) &
  API_PID=$!
  printf '%s\n' "$API_PID" > "$API_PID_FILE"
  for _ in {1..20}; do
    if curl -fsS "http://127.0.0.1:$API_PORT/api/v1/health/ready" >/dev/null 2>&1; then
      break
    fi
    kill -0 "$API_PID" 2>/dev/null || die "API 启动失败，请查看 $LOG_DIR/api.log"
    sleep 1
  done
  curl -fsS "http://127.0.0.1:$API_PORT/api/v1/health/ready" >/dev/null 2>&1 || die "API 未在 20 秒内就绪，请查看 $LOG_DIR/api.log"
fi
touch "$BOOTSTRAP_MARKER"

if [[ -n "$HARNESS_ENABLED" ]]; then
  # Use the configured path; do not assume the legacy harness-projector-heartbeat log location.
  PROJECTOR_HEARTBEAT="$(config_value paths projector_heartbeat)"
  [[ "$PROJECTOR_HEARTBEAT" = /* ]] || die "配置文件中的 Projector 心跳路径必须是绝对路径"
  mkdir -p "$(dirname "$PROJECTOR_HEARTBEAT")"
  rm -f "$PROJECTOR_HEARTBEAT"
  print "启动 Harness Projector（日志：$LOG_DIR/harness-projector.log）..."
  (cd "$ROOT_DIR" && "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" \
    python -m api.harness_projector > "$LOG_DIR/harness-projector.log" 2>&1) &
  PROJECTOR_PID=$!
  printf '%s\n' "$PROJECTOR_PID" > "$PROJECTOR_PID_FILE"
  for _ in {1..20}; do
    [[ -f "$PROJECTOR_HEARTBEAT" ]] && break
    kill -0 "$PROJECTOR_PID" 2>/dev/null || die "Harness Projector 启动失败，请查看 $LOG_DIR/harness-projector.log"
    sleep 1
  done
  [[ -f "$PROJECTOR_HEARTBEAT" ]] && kill -0 "$PROJECTOR_PID" 2>/dev/null || die "Harness Projector 未在 20 秒内就绪，请查看 $LOG_DIR/harness-projector.log"
fi

if [[ "$WEB_SHELL_ENABLED" == "1" ]]; then
  [[ -d "$HARNESS_ROOT" && -d "$EXTENSIONS_ROOT" ]] || die "Harness Web Shell 路径无效"
  WEB_PROFILE_DIR="$DSH_HOME/profiles/test-atelier-web"
  WEB_PROFILE_MARKER="$DSH_HOME/.test-atelier-web-profile-installed"
  if [[ ! -f "$WEB_PROFILE_MARKER" || ! -d "$WEB_PROFILE_DIR" ]]; then
    print "首次安装 Harness Web Shell Profile ..."
    (cd "$EXTENSIONS_ROOT" && python3 scripts/install_profile.py --with-web --harness-root "$HARNESS_ROOT")
    touch "$WEB_PROFILE_MARKER"
  else
    print "Harness Web Shell Profile 已安装，跳过安装"
  fi
  MODEL_KEY_FILE="$(config_value model api_key_file)"
  MODEL_API_BASE="$(config_value model api_base)"
  MODEL_NAME="$(config_value model model)"
  [[ -r "$MODEL_KEY_FILE" ]] || die "Harness Web Shell 模型 Key 文件不可读"
  print "启动 Harness Web Shell：http://127.0.0.1:$WEB_SHELL_PORT/harness/（日志：$WEB_SHELL_LOG）"
  # `--profile test-atelier-web` already selects the Web app. Passing a
  # trailing `web` would be forwarded as a positional app argument and the
  # Web command would reject it with "Expected 0 arguments but got 1".
  (cd "$HARNESS_ROOT" && \
    DSH_HOME="$DSH_HOME" HARNESS_ROOT="$HARNESS_ROOT" HARNESS_SESSION_ROOT="$HARNESS_SESSION_ROOT" \
    OPENAI_API_BASE="$MODEL_API_BASE" OPENAI_MODEL="$MODEL_NAME" OPENAI_API_KEY="$(cat "$MODEL_KEY_FILE")" \
    node --import tsx/esm apps/cli/src/bin.ts --profile test-atelier-web \
      --host 127.0.0.1 --port "$WEB_SHELL_PORT" \
      --public-url "http://127.0.0.1:$GATEWAY_PORT/harness" \
      --trusted-host "127.0.0.1:$GATEWAY_PORT" --no-open > "$WEB_SHELL_LOG" 2>&1) &
  WEB_SHELL_PID=$!
  web_shell_reachable() {
    local status_code
    # The Web Shell protects `/` with authentication and legitimately returns
    # 401 before a token is supplied. Treat any HTTP response as readiness;
    # only a connection failure means the server is not listening yet.
    status_code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$WEB_SHELL_PORT/" 2>/dev/null || true)"
    [[ "$status_code" == <1-5><-><-> ]]
  }
  for _ in {1..30}; do
    web_shell_reachable && break
    kill -0 "$WEB_SHELL_PID" 2>/dev/null || die "Harness Web Shell 启动失败，请查看 $WEB_SHELL_LOG"
    sleep 1
  done
  web_shell_reachable || die "Harness Web Shell 未在 30 秒内就绪"
  print "启动 Harness Web Shell 网关：http://127.0.0.1:$GATEWAY_PORT/（日志：$GATEWAY_LOG）"
  (cd "$EXTENSIONS_ROOT" && \
    TEST_ATELIER_API_ORIGIN="http://127.0.0.1:$API_PORT" \
    TEST_ATELIER_FRONTEND_ORIGIN="http://127.0.0.1:$WEB_PORT" \
    TEST_ATELIER_WEB_SHELL_ORIGIN="http://127.0.0.1:$WEB_SHELL_PORT" \
    TEST_ATELIER_GATEWAY_PORT="$GATEWAY_PORT" \
    node gateway/server.mjs > "$GATEWAY_LOG" 2>&1) &
  GATEWAY_PID=$!
fi

print "启动前端：http://127.0.0.1:$WEB_PORT"
[[ "$WEB_SHELL_ENABLED" == "1" ]] && print "Harness 界面：http://127.0.0.1:$GATEWAY_PORT/harness/"
print "按 Ctrl+C 可同时停止 API、Harness Projector 和前端"
# Keep Vite's development proxy on the same API port as this process.
export AI_STUDIO_API_TARGET="http://127.0.0.1:$API_PORT"
(cd "$ROOT_DIR/web" && npm run dev -- --host 127.0.0.1 --port "$WEB_PORT")
