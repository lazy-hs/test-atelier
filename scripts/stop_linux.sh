#!/usr/bin/env bash

# Stop the API started by setup_and_start_linux.sh without touching unrelated processes.
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
API_PID_FILE="$ROOT_DIR/.test-atelier-logs/api.pid"
PROJECTOR_PID_FILE="$ROOT_DIR/.test-atelier-logs/harness-projector.pid"

stop_recorded() {
  local pid_file="$1" marker="$2" label="$3" pid command_line
  if [[ ! -s "$pid_file" ]]; then
    printf 'Test Atelier %s 未发现运行中的 PID 文件\n' "$label"
    return 0
  fi
  pid="$(head -n 1 "$pid_file" | tr -d '[:space:]')"
  if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
    rm -f "$pid_file"
    printf 'Test Atelier %s PID 文件无效，已清理\n' "$label"
    return 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    printf 'Test Atelier %s 已停止（清理过期 PID）\n' "$label"
    return 0
  fi
  command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ "$command_line" != *"$marker"* ]]; then
    printf '错误：PID %s 不是由 Test Atelier 启动的 %s，未执行停止\n' "$pid" "$label" >&2
    return 1
  fi
  pkill -TERM -P "$pid" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    pkill -KILL -P "$pid" 2>/dev/null || true
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
  printf 'Test Atelier %s 已停止\n' "$label"
}

stop_result=0
stop_recorded "$PROJECTOR_PID_FILE" "api.harness_projector" "Harness Projector" || stop_result=1
stop_recorded "$API_PID_FILE" "api.uvicorn_entrypoint" "API" || stop_result=1
exit "$stop_result"
