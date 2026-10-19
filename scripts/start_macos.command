#!/bin/zsh

# Start services after configure_environment_macos.command has completed.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_ATELIER_CONFIG_ONLY=0 TEST_ATELIER_SKIP_CONFIG=1 exec "$SCRIPT_DIR/setup_and_start_macos.command"
