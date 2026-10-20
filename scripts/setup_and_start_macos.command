#!/bin/zsh

# One-click local setup and startup for macOS.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_NAME="test-atelier"
LOG_DIR="$ROOT_DIR/.test-atelier-logs"
DEP_MARKER="$LOG_DIR/dependencies-$ENV_NAME.sha256"
API_PID=""
CONFIG_ONLY="${TEST_ATELIER_CONFIG_ONLY:-0}"
SKIP_CONFIG="${TEST_ATELIER_SKIP_CONFIG:-0}"

cleanup() {
  if [[ -n "$API_PID" ]] && kill -0 "$API_PID" 2>/dev/null; then
    pkill -TERM -P "$API_PID" 2>/dev/null || true
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

die() {
  print -u2 "错误：$1"
  exit 1
}

[[ -d "$ROOT_DIR/api" && -d "$ROOT_DIR/web" ]] || die "请从 test-atelier 工程目录运行此脚本"
command -v npm >/dev/null 2>&1 || die "未找到 npm，请先安装 Node.js 20+"
command -v openssl >/dev/null 2>&1 || die "未找到 openssl，请先安装 OpenSSL"

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
SECRET_FILE="$CONFIG_DIR/session-secret"
mkdir -p "$CONFIG_DIR" "$LOG_DIR"
chmod 700 "$CONFIG_DIR" "$LOG_DIR"

if [[ "$SKIP_CONFIG" != "1" ]]; then
  DEP_FINGERPRINT="$(
    shasum -a 256 "$ROOT_DIR/api/requirements-dev.txt" "$ROOT_DIR/web/package-lock.json" \
      | shasum -a 256 | awk '{print $1}'
  )"
  DEP_MARKER_VALUE=""
  [[ -s "$DEP_MARKER" ]] && DEP_MARKER_VALUE="$(cat "$DEP_MARKER")"
  if [[ "$ENV_CREATED" == "true" || "${TEST_ATELIER_FORCE_INSTALL:-0}" == "1" || \
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
export AI_STUDIO_SESSION_SECRET="$(cat "$SECRET_FILE")"
export AI_STUDIO_REQUIRE_MIGRATIONS=1
export AI_STUDIO_MIGRATION_HEAD="20261028_codex_sessions"
# This launcher starts only the API and Vite. Never inherit production's
# external-worker flag from the shell or queued AI jobs would have no consumer.
export AI_STUDIO_EXTERNAL_WORKER=0
export AI_STUDIO_ENV=development

BOOTSTRAP_MARKER="$LOG_DIR/admin-initialized"
if [[ "$CONFIG_ONLY" != "1" && ( "$ENV_CREATED" == "true" || ! -f "$BOOTSTRAP_MARKER" ) ]]; then
  if [[ -z "${AI_STUDIO_ADMIN_PASSWORD:-}" ]]; then
    read -r -s "AI_STUDIO_ADMIN_PASSWORD?管理员密码（至少 6 位，直接回车使用默认密码 123456）："
    print
  fi
  AI_STUDIO_ADMIN_PASSWORD="${AI_STUDIO_ADMIN_PASSWORD:-123456}"
  [[ ${#AI_STUDIO_ADMIN_PASSWORD} -ge 6 ]] || die "管理员密码至少需要 6 位"
  export AI_STUDIO_ADMIN_PASSWORD
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

print "启动 API（日志：$LOG_DIR/api.log）..."
if [[ -f "$LOG_DIR/api.log" ]]; then
  LOG_BYTES="$(stat -f%z "$LOG_DIR/api.log" 2>/dev/null || print 0)"
  if (( LOG_BYTES > 52428800 )); then
    mv "$LOG_DIR/api.log" "$LOG_DIR/api.log.1"
  fi
fi
(cd "$ROOT_DIR" && "$CONDA_EXE" run --no-capture-output -n "$ENV_NAME" python -m api.uvicorn_entrypoint --host 127.0.0.1 --port 8000 > "$LOG_DIR/api.log" 2>&1) &
API_PID=$!
for _ in {1..20}; do
  if curl -fsS http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1; then
    break
  fi
  kill -0 "$API_PID" 2>/dev/null || die "API 启动失败，请查看 $LOG_DIR/api.log"
  sleep 1
done
curl -fsS http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1 || die "API 未在 20 秒内就绪，请查看 $LOG_DIR/api.log"
touch "$BOOTSTRAP_MARKER"

print "启动前端：http://127.0.0.1:5173"
print "按 Ctrl+C 可同时停止 API 和前端"
(cd "$ROOT_DIR/web" && npm run dev -- --host 127.0.0.1)
