#!/bin/zsh

# Configure and start the complete local macOS stack:
# DeepSeek Harness Host -> Test Atelier API/Projector -> Vite frontend.
#
# The JSON runtime file is the only operator-facing configuration source. The
# launcher translates it to the legacy child-process contract while upstream
# Harness and Test Atelier are being migrated to native config-file loading.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_FILE="$ROOT_DIR/config/runtime.json"
if [[ ! -s "$CONFIG_FILE" ]]; then
  print "未找到运行配置，开始创建：$CONFIG_FILE"
  python3 "$ROOT_DIR/scripts/configure_full_runtime.py"
fi

config_value() {
  python3 - "$CONFIG_FILE" "$@" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
value: object = json.loads(path.read_text(encoding="utf-8"))
for key in sys.argv[2:]:
    if not isinstance(value, dict) or key not in value:
        raise SystemExit(f"配置缺少字段：{'.'.join(sys.argv[2:])}")
    value = value[key]
if isinstance(value, bool):
    print("1" if value else "0")
elif isinstance(value, str):
    path_keys={"harness_root", "extensions_root", "skill_root", "dsh_home", "scope_secret_file", "task_root", "session_root", "log_dir", "api_key_file", "session_secret_file", "admin_password_file"}
    print(str(Path(value).expanduser()) if sys.argv[-1] in path_keys else value)
elif isinstance(value, (int, float)):
    print(value)
else:
    raise SystemExit("配置字段必须是字符串、数字或布尔值")
PY
}

HARNESS_ROOT="$(config_value paths harness_root)"
EXT_ROOT="$(config_value paths extensions_root)"
SKILL_ROOT="$(config_value paths skill_root)"
DSH_HOME="$(config_value paths dsh_home)"
SCOPE_SECRET_FILE="$(config_value secrets scope_secret_file)"
SESSION_SECRET_FILE="$(config_value test_atelier session_secret_file)"
ADMIN_PASSWORD_FILE="$(config_value test_atelier admin_password_file)"
LOG_DIR="$(config_value paths log_dir)"
HOST_LOG="$LOG_DIR/harness-host.log"
HOST_PID_FILE="$LOG_DIR/harness-host.pid"
HARNESS_TASK_ROOT="$(config_value paths task_root)"
HARNESS_SESSION_ROOT="$(config_value paths session_root)"
API_PORT="$(config_value server api_port)"
WEB_PORT="$(config_value server web_port)"
HARNESS_PORT="$(config_value harness port)"
HARNESS_BIND_HOST="$(config_value harness bind_host)"
HARNESS_INPUT_MODE="$(config_value harness input_mode)"
HARNESS_PROTOCOL="$(config_value harness protocol)"
HARNESS_CONFIRMATION="$(config_value harness confirmation_transport)"
HARNESS_ONLY="$(config_value harness harness_only)"
HARNESS_TASK_TIMEOUT_MS="$(config_value harness task_timeout_ms)"
HARNESS_MAX_ACTIVE_TASKS="$(config_value harness max_active_tasks)"
WEB_SHELL_ENABLED="$(config_value harness web_shell enabled)"
HOST_PID=""

die() {
  print -u2 "错误：$1"
  exit 1
}

cleanup() {
  local exit_code=$?
  if [[ -n "$HOST_PID" ]] && kill -0 "$HOST_PID" 2>/dev/null; then
    print "停止 DeepSeek Harness Host ..."
    pkill -TERM -P "$HOST_PID" 2>/dev/null || true
    kill -TERM "$HOST_PID" 2>/dev/null || true
    wait "$HOST_PID" 2>/dev/null || true
  fi
  if [[ -f "$HOST_PID_FILE" && -n "$HOST_PID" && "$(cat "$HOST_PID_FILE" 2>/dev/null || true)" == "$HOST_PID" ]]; then
    rm -f "$HOST_PID_FILE"
  fi
  # Do not leave the model credential in a shell environment after Ctrl+C.
  unset OPENAI_API_KEY
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

[[ "$(uname -s)" == "Darwin" ]] || die "本脚本只支持 macOS"
[[ -d "$ROOT_DIR/api" && -d "$ROOT_DIR/web" ]] || die "Test Atelier 工程目录无效：$ROOT_DIR"
[[ -f "$HARNESS_ROOT/package.json" ]] || die "DeepSeek Harness 工程不存在：$HARNESS_ROOT"
[[ -f "$EXT_ROOT/scripts/start_host_macos.command" ]] || die "Harness Extension 工程不存在：$EXT_ROOT"
[[ -d "$SKILL_ROOT" ]] || die "qa-doc-skills 技能仓不存在：$SKILL_ROOT"

command -v openssl >/dev/null 2>&1 || die "未找到 openssl"
command -v curl >/dev/null 2>&1 || die "未找到 curl"
command -v node >/dev/null 2>&1 || die "未找到 Node.js"
command -v npm >/dev/null 2>&1 || die "未找到 npm"
command -v python3 >/dev/null 2>&1 || die "未找到 python3"

python3 - "$ROOT_DIR/data/config/settings.json" "$API_PORT" "$WEB_PORT" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
api_port, web_port = int(sys.argv[2]), int(sys.argv[3])
payload = {}
if path.exists():
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
if not isinstance(payload, dict):
    payload = {}
payload["schema_version"] = int(payload.get("schema_version") or 1)
server = payload.get("server") if isinstance(payload.get("server"), dict) else {}
server.update({"port": api_port, "web_port": web_port})
payload["server"] = server
path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
finally:
    Path(temporary).unlink(missing_ok=True)
PY

print "[1/8] 检查并配置 Test Atelier Conda/Node 环境 ..."
cd "$ROOT_DIR"
./scripts/configure_environment_macos.command
[[ -r "$SESSION_SECRET_FILE" ]] || die "会话密钥文件不可读：$SESSION_SECRET_FILE"

print "[2/8] 准备受保护的 Harness 作用域密钥 ..."
mkdir -p "$LOG_DIR" "$DSH_HOME"
mkdir -p "$HARNESS_TASK_ROOT" "$HARNESS_SESSION_ROOT"
chmod 700 "$LOG_DIR" "$DSH_HOME"
chmod 700 "$HARNESS_TASK_ROOT" "$HARNESS_SESSION_ROOT"
[[ -r "$SCOPE_SECRET_FILE" ]] || die "Harness 作用域密钥文件不可读：$SCOPE_SECRET_FILE"

print "[3/8] 获取 Harness 模型配置 ..."
MODEL_KEY_FILE="$(config_value model api_key_file)"
[[ "$MODEL_KEY_FILE" = /* ]] || die "配置文件中的模型 Key 路径必须是绝对路径"
[[ -r "$MODEL_KEY_FILE" ]] || die "模型 Key 文件不可读：$MODEL_KEY_FILE"

print "[4/8] 检查并安装 Harness Profile ..."
cd "$EXT_ROOT"
WEB_PROFILE_DIR="$DSH_HOME/profiles/test-atelier-web"
WEB_PROFILE_MARKER="$DSH_HOME/.test-atelier-web-profile-installed"
if [[ "$WEB_SHELL_ENABLED" == "1" && ( ! -f "$WEB_PROFILE_MARKER" || ! -d "$WEB_PROFILE_DIR" ) ]]; then
  python3 scripts/install_profile.py --with-web --harness-root "$HARNESS_ROOT"
  touch "$WEB_PROFILE_MARKER"
elif [[ "$WEB_SHELL_ENABLED" == "1" ]]; then
  print "Harness Web Shell Profile 已安装，跳过安装"
else
  python3 scripts/install_profile.py --harness-root "$HARNESS_ROOT"
fi

print "[5/8] 运行 Harness Host 生产前置检查 ..."
python3 scripts/preflight_host.py --production --config "$CONFIG_FILE"

print "[6/8] 启动 DeepSeek Harness Host ..."
if [[ -f "$HOST_PID_FILE" ]]; then
  old_pid="$(cat "$HOST_PID_FILE" 2>/dev/null || true)"
  if [[ "$old_pid" = <-> ]] && kill -0 "$old_pid" 2>/dev/null; then
    die "检测到已有 Harness Host（PID $old_pid），请先停止它或执行：kill $old_pid"
  fi
  rm -f "$HOST_PID_FILE"
fi
rm -f "$HOST_LOG"
(cd "$EXT_ROOT" && node host/server.mjs --config "$CONFIG_FILE" > "$HOST_LOG" 2>&1) &
HOST_PID=$!
print -r -- "$HOST_PID" > "$HOST_PID_FILE"

host_ready=0
for _ in {1..30}; do
  if curl -fsS "http://127.0.0.1:$HARNESS_PORT/health" >/dev/null 2>&1; then
    host_ready=1
    break
  fi
  kill -0 "$HOST_PID" 2>/dev/null || break
  sleep 1
done
if (( host_ready == 0 )); then
  print -u2 "Harness Host 未在 30 秒内就绪，日志最后 80 行："
  tail -n 80 "$HOST_LOG" 2>/dev/null || true
  die "Harness Host 启动失败"
fi

print "[7/8] 配置 Test Atelier 使用 Harness-only 模式 ..."
cd "$ROOT_DIR"
[[ -r "$ADMIN_PASSWORD_FILE" ]] || die "管理员密码文件不可读：$ADMIN_PASSWORD_FILE"

print "[8/8] 启动 Test Atelier API、Projector 和前端 ..."
print "Harness Host：http://$HARNESS_BIND_HOST:$HARNESS_PORT"
print "Test Atelier：启动后访问 http://127.0.0.1:5173"
print "Harness 日志：$HOST_LOG"
print "按 Ctrl+C 会同时停止 Test Atelier 启动器和本次启动的 Harness Host"
# The model credential belongs only to the Harness Host. Do not expose it to
# the Test Atelier API, Vite process, or their child processes.
./scripts/start_macos.command
