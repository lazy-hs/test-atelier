#!/bin/zsh

# Stop the complete stack started by start_full_macos.command.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${TEST_ATELIER_FULL_LOG_DIR:-$HOME/.local/state/test-atelier}"
PID_FILE="$LOG_DIR/harness-host.pid"

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ "$pid" = <-> ]] && kill -0 "$pid" 2>/dev/null; then
    command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$command_line" == *"host/server.mjs"* || "$command_line" == *"start_host_macos.command"* ]]; then
      print "停止 DeepSeek Harness Host（PID $pid）..."
      kill -TERM "$pid" 2>/dev/null || true
      for _ in {1..20}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.25
      done
      if kill -0 "$pid" 2>/dev/null; then
        print -u2 "警告：Harness Host 未在 5 秒内退出，未强制杀死进程：$pid"
      fi
    else
      print -u2 "拒绝停止 PID $pid：命令行不是 Test Atelier Harness Host"
    fi
  fi
  rm -f "$PID_FILE"
fi

if [[ -x "$ROOT_DIR/scripts/stop_macos.command" ]]; then
  "$ROOT_DIR/scripts/stop_macos.command"
fi
