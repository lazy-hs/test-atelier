#!/usr/bin/env bash

# One-click local setup and startup for Linux.
# The layout intentionally mirrors the macOS and Windows launchers while
# using Linux-native tooling (bash, sha256sum and stat -c).
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_NAME="test-atelier"
LOG_DIR="$ROOT_DIR/.test-atelier-logs"
DEP_MARKER="$LOG_DIR/dependencies-$ENV_NAME.sha256"
API_PID_FILE="$LOG_DIR/api.pid"
API_PID=""
PROJECTOR_PID_FILE="$LOG_DIR/harness-projector.pid"
PROJECTOR_PID=""
PROJECTOR_HEARTBEAT=""

die() {
  printf '错误：%s\n' "$1" >&2
  exit 1
}

CONFIG_ONLY=0
SKIP_CONFIG=0
for argument in "$@"; do
  case "$argument" in
    --config-only) CONFIG_ONLY=1 ;;
    --skip-config) SKIP_CONFIG=1 ;;
    *) die "用法：$0 [--config-only] [--skip-config]" ;;
  esac
done
RUNTIME_CONFIG="$ROOT_DIR/config/runtime.json"

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
    # conda run is a wrapper; terminate its direct children before the
    # wrapper so the Python API does not remain orphaned after Ctrl+C.
    pkill -TERM -P "$API_PID" 2>/dev/null || true
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
  if [[ -n "$API_PID" && -f "$API_PID_FILE" && "$(cat "$API_PID_FILE")" == "$API_PID" ]]; then
    rm -f "$API_PID_FILE"
  fi
}
trap cleanup EXIT INT TERM

[[ -d "$ROOT_DIR/api" && -d "$ROOT_DIR/web" ]] || die "请从 test-atelier 工程目录运行此脚本"
[[ -s "$RUNTIME_CONFIG" ]] || die "未找到配置文件 $RUNTIME_CONFIG，请先运行 scripts/configure_full_runtime.py"
command -v npm >/dev/null 2>&1 || die "未找到 npm，请先安装 Node.js 20+"
command -v curl >/dev/null 2>&1 || die "未找到 curl，请先安装 curl"
command -v openssl >/dev/null 2>&1 || die "未找到 openssl，请先安装 openssl"
command -v sha256sum >/dev/null 2>&1 || die "未找到 sha256sum，请先安装 coreutils"
command -v stat >/dev/null 2>&1 || die "未找到 stat，请先安装 coreutils"

CONDA_EXE="$(command -v conda 2>/dev/null || true)"
if [[ -z "$CONDA_EXE" ]]; then
  for candidate in \
    "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" \
    "$HOME/miniconda/bin/conda" "$HOME/anaconda/bin/conda" \
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
  "$CONDA_EXE" env list | awk 'NF && $1 !~ /^#/ {print $1}' | grep -Fxq "$ENV_NAME" || die "未找到 Conda 环境 $ENV_NAME，请先运行 configure_environment_linux.sh"
elif ! "$CONDA_EXE" env list | awk 'NF && $1 !~ /^#/ {print $1}' | grep -Fxq "$ENV_NAME"; then
  printf '创建 Conda 环境 %s ...\n' "$ENV_NAME"
  "$CONDA_EXE" create -n "$ENV_NAME" python=3.12 -y
  ENV_CREATED="true"
fi

mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR"

if [[ "$SKIP_CONFIG" != "1" ]]; then
  DEP_FINGERPRINT="$(
    sha256sum "$ROOT_DIR/api/requirements-dev.txt" "$ROOT_DIR/web/package-lock.json" \
      | sha256sum | awk '{print $1}'
  )"
  DEP_MARKER_VALUE=""
  [[ -s "$DEP_MARKER" ]] && DEP_MARKER_VALUE="$(cat "$DEP_MARKER")"
  if [[ "$ENV_CREATED" == "true" || \
        "$DEP_MARKER_VALUE" != "$DEP_FINGERPRINT" || ! -d "$ROOT_DIR/web/node_modules" ]]; then
    printf '安装 Python 依赖 ...\n'
    "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" python -m pip install -r "$ROOT_DIR/api/requirements-dev.txt"
    printf '安装前端依赖 ...\n'
    (cd "$ROOT_DIR/web" && npm ci)
    printf '%s\n' "$DEP_FINGERPRINT" > "$DEP_MARKER"
  else
    printf '依赖未变化，跳过 Python 和前端依赖安装\n'
  fi
else
  [[ -d "$ROOT_DIR/web/node_modules" ]] || die "未找到前端依赖，请先运行 configure_environment_linux.sh"
fi

SECRET_FILE="$(python - "$RUNTIME_CONFIG" <<'PY'
import json, sys
from pathlib import Path
print(Path(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))['test_atelier']['session_secret_file']).expanduser())
PY
)"
CONFIG_DIR="$(dirname "$SECRET_FILE")"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
if [[ "$SKIP_CONFIG" == "1" && ! -s "$SECRET_FILE" ]]; then
  die "未找到会话密钥，请先运行 configure_environment_linux.sh"
fi
if [[ "$SKIP_CONFIG" != "1" && ! -s "$SECRET_FILE" ]]; then
  umask 077
  openssl rand -hex 32 > "$SECRET_FILE"
fi
BOOTSTRAP_MARKER="$LOG_DIR/admin-initialized"
if [[ "$CONFIG_ONLY" != "1" && ( "$ENV_CREATED" == "true" || ! -f "$BOOTSTRAP_MARKER" ) ]]; then
  ADMIN_PASSWORD_FILE="$(python - "$RUNTIME_CONFIG" <<'PY'
import json, sys
from pathlib import Path
print(Path(json.loads(Path(sys.argv[1]).read_text())['test_atelier']['admin_password_file']).expanduser())
PY
)"
  [[ -s "$ADMIN_PASSWORD_FILE" ]] || die "未找到管理员密码文件，请运行 scripts/configure_full_runtime.py"
fi

if [[ "$SKIP_CONFIG" != "1" ]]; then
  printf '执行数据库迁移 ...\n'
  (cd "$ROOT_DIR" && "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" python -m alembic upgrade head)
fi

if [[ "$CONFIG_ONLY" == "1" ]]; then
  printf '运行环境配置完成；请执行 scripts/start_linux.sh 启动服务\n'
  exit 0
fi

PORTS="$(cd "$ROOT_DIR" && "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" \
  python "$ROOT_DIR/scripts/read_server_ports.py" | tail -n 1)"
read -r API_PORT WEB_PORT <<< "$PORTS"
[[ "$API_PORT" =~ ^[0-9]+$ && "$WEB_PORT" =~ ^[0-9]+$ ]] || die "settings.json 中的端口配置无效"
HARNESS_ENABLED="$($CONDA_EXE run --no-capture-output -n "$ENV_NAME" python -c 'from api.runtime_config import runtime_environment; print(runtime_environment().get("TEST_ATELIER_HARNESS_URL", ""))' | tail -n 1)"
if [[ -n "$HARNESS_ENABLED" ]]; then
  "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" python \
    "$ROOT_DIR/scripts/harness_secret_file.py" --config "$RUNTIME_CONFIG" || die "已配置 Harness Host，但作用域密钥无效或不可读取"
elif [[ "$($CONDA_EXE run --no-capture-output -n "$ENV_NAME" python -c 'from api.runtime_config import runtime_environment; print(runtime_environment().get("TEST_ATELIER_HARNESS_ONLY", "0"))' | tail -n 1)" == "1" ]]; then
  die "Harness-only 模式必须先配置 TEST_ATELIER_HARNESS_URL"
fi

API_LOG="$LOG_DIR/api.log"
if [[ -f "$API_LOG" ]] && [[ "$(stat -c%s "$API_LOG" 2>/dev/null || printf 0)" -gt 52428800 ]]; then
  mv -f "$API_LOG" "$API_LOG.1"
fi

printf '启动 API（日志：%s）...\n' "$API_LOG"
(cd "$ROOT_DIR" && "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" \
  python -m api.uvicorn_entrypoint --host 127.0.0.1 --port "$API_PORT" > "$API_LOG" 2>&1) &
API_PID=$!
printf '%s\n' "$API_PID" > "$API_PID_FILE"

API_READY="false"
for _ in {1..20}; do
  if curl -fsS "http://127.0.0.1:$API_PORT/api/v1/health/ready" >/dev/null 2>&1; then
    API_READY="true"
    break
  fi
  kill -0 "$API_PID" 2>/dev/null || die "API 启动失败，请查看 $API_LOG"
  sleep 1
done
[[ "$API_READY" == "true" ]] || die "API 未在 20 秒内就绪，请查看 $API_LOG"
touch "$BOOTSTRAP_MARKER"

if [[ -n "$HARNESS_ENABLED" ]]; then
  # Use the configured path; do not assume the legacy harness-projector-heartbeat log location.
  PROJECTOR_HEARTBEAT="$(config_value paths projector_heartbeat)"
  [[ "$PROJECTOR_HEARTBEAT" = /* ]] || die "配置文件中的 Projector 心跳路径必须是绝对路径"
  mkdir -p "$(dirname "$PROJECTOR_HEARTBEAT")"
  rm -f "$PROJECTOR_HEARTBEAT"
  printf '启动 Harness Projector（日志：%s）...\n' "$LOG_DIR/harness-projector.log"
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

printf '启动前端：http://127.0.0.1:%s\n' "$WEB_PORT"
printf '按 Ctrl+C 可同时停止 API、Harness Projector 和前端\n'
# Keep Vite's development proxy on the same API port as this process.
export AI_STUDIO_API_TARGET="http://127.0.0.1:$API_PORT"
(cd "$ROOT_DIR/web" && npm run dev -- --host 127.0.0.1 --port "$WEB_PORT")
