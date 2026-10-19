#!/usr/bin/env bash

# Configure the local Conda and Node environment without starting services.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEST_ATELIER_CONFIG_ONLY=1 TEST_ATELIER_SKIP_CONFIG=0 exec "$SCRIPT_DIR/setup_and_start_linux.sh"
