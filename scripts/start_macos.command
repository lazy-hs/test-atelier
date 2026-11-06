#!/bin/zsh

# Start services after configure_environment_macos.command has completed.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/setup_and_start_macos.command" --skip-config
