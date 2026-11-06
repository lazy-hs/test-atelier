#!/usr/bin/env bash

# Configure the local Conda and Node environment without starting services.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/setup_and_start_linux.sh" --config-only
