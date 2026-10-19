#!/bin/sh
# Keep Nginx access logs as JSON on stdout and normalize native error-log
# lines before they reach the container collector. Nginx has no configurable
# JSON error formatter, so this small PID 1 wrapper preserves one envelope for
# both streams while forwarding signals to the foreground server.
set -eu

pipe="/tmp/nginx-error-$$.pipe"
nginx_bin="${NGINX_BIN:-/usr/sbin/nginx}"
max_error_message_chars=4096
if ! mkfifo "$pipe" 2>/dev/null; then
  printf '{"timestamp":"%s","schema_version":1,"level":"ERROR","logger":"nginx","service":"test-atelier-web","environment":"production","event":"nginx.start_failed","request_id":"-","error_type":"FifoCreateFailed"}\n' "$(date -u +%Y-%m-%dT%H:%M:%S%z)" >&2
  exit 1
fi

timestamp() {
  date -u +%Y-%m-%dT%H:%M:%S%z
}

emit_event() {
  level="$1"
  event="$2"
  printf '{"timestamp":"%s","schema_version":1,"level":"%s","logger":"nginx","service":"test-atelier-web","environment":"production","event":"%s","request_id":"-"}\n' \
    "$(timestamp)" "$level" "$event"
}

json_escape() {
  # Nginx error messages are single-line by contract. Escape the characters
  # that could otherwise terminate the JSON string before writing it.
  # Replace all remaining ASCII control bytes as well; malformed request
  # targets can otherwise make the collector receive invalid JSON.
  printf '%s' "$1" | LC_ALL=C tr '\t\r' '  ' | LC_ALL=C tr '\000-\010\013\014\016-\037' ' ' | sed 's/\\/\\\\/g; s/"/\\"/g'
}

redact_error() {
  # Error lines can contain a rejected request target or an upstream URL.
  # Remove common credential/query forms before JSON encoding the message.
  printf '%s' "$1" | sed -E \
    -e 's#([A-Za-z][A-Za-z0-9+.-]*://[^:/[:space:]]+):[^@[:space:]]+@#\1:[REDACTED]@#g' \
    -e 's#(https?://[^:/[:space:]]+):[^@[:space:]]+@#\1:[REDACTED]@#g' \
    -e 's#((cookie|set[-_]?cookie)[[:space:]]*[=:]).*$#\1[REDACTED]#Ig' \
    -e 's#([?&](api[-_]?key|access[-_]?token|refresh[-_]?token|client[-_]?secret|cookie|password|secret|token)=)[^&[:space:]]+#\1[REDACTED]#Ig' \
    -e 's#((api[-_]?key|access[-_]?token|refresh[-_]?token|client[-_]?secret|cookie|set[-_]?cookie|password|secret|token)[[:space:]]*[=:])[^,;[:space:]]+#\1[REDACTED]#Ig' \
    -e 's#((OPENAI_API_KEY|ANTHROPIC_API_KEY|AZURE_OPENAI_API_KEY|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|GOOGLE_API_KEY|HF_TOKEN|GITHUB_TOKEN|NPM_TOKEN|DOCKER_AUTH_CONFIG|DATABASE_URL|POSTGRES_PASSWORD|METRICS_TOKEN|AI_STUDIO_SESSION_SECRET|AI_STUDIO_ADMIN_PASSWORD)[[:space:]]*=)[^,;[:space:]]+#\1[REDACTED]#Ig' \
    -e 's#(Bearer[[:space:]]+)[A-Za-z0-9._~+/=-]+#\1[REDACTED]#Ig' \
    -e 's#sk-[A-Za-z0-9_-]{12,}#[REDACTED]#g' \
    -e 's#AKIA[0-9A-Z]{16}#[REDACTED]#g' \
    -e 's#(gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}#[REDACTED]#g' \
    -e 's#eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}#[REDACTED]#g' \
    -e 's#-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----#[PRIVATE_KEY_REDACTED]#g'
}

emit_errors() {
  while IFS= read -r line; do
    # Bound native diagnostics before JSON encoding so an upstream response or
    # malformed request cannot create an oversized collector record.
    safe_line=$(redact_error "$line" | cut -c "1-${max_error_message_chars}")
    escaped=$(json_escape "$safe_line")
    level="WARNING"
    case "$line" in
      *'[emerg]'*|*'[alert]'*|*'[crit]'*|*'[error]'*) level="ERROR" ;;
    esac
    printf '{"timestamp":"%s","schema_version":1,"level":"%s","logger":"nginx","service":"test-atelier-web","environment":"production","event":"nginx.error","request_id":"-","message":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%S%z)" "$level" "$escaped"
  done < "$pipe"
}

emit_errors &
logger_pid=$!

cleanup() {
  trap - TERM INT QUIT EXIT
  if [ -n "$nginx_pid" ]; then
    kill "$nginx_pid" 2>/dev/null || true
    wait "$nginx_pid" 2>/dev/null || true
  fi
  # The Nginx process closes the FIFO after its stderr is drained. Wait for
  # the formatter so short-lived `nginx -t` diagnostics are not lost.
  wait "$logger_pid" 2>/dev/null || true
  kill "$logger_pid" 2>/dev/null || true
  rm -f "$pipe"
}

nginx_pid=""
trap 'if [ -n "$nginx_pid" ]; then kill -QUIT "$nginx_pid" 2>/dev/null || true; fi' TERM INT QUIT

if [ "$#" -eq 0 ]; then
  set -- -g 'daemon off;'
fi
emit_event "INFO" "nginx.starting"
"$nginx_bin" "$@" 2> "$pipe" &
nginx_pid=$!
set +e
wait "$nginx_pid"
status=$?
set -e
cleanup
if [ "$status" -eq 0 ]; then
  emit_event "INFO" "nginx.stopped"
else
  emit_event "ERROR" "nginx.stopped"
fi
exit "$status"
