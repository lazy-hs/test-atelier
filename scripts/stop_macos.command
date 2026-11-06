#!/bin/zsh

# Stop the API started by setup_and_start_macos.command without touching unrelated processes.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/stop_linux.sh"
