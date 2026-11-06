#!/bin/zsh

# Configure the local Conda and Node environment without starting services.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/setup_and_start_macos.command" --config-only
