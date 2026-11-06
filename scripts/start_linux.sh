#!/usr/bin/env bash

# Start services after configure_environment_linux.sh has completed.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/setup_and_start_linux.sh" --skip-config
