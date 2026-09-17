#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/setup_wsl.sh"
exec python3 "$SCRIPT_DIR/acupoint_demo.py" "$@"
