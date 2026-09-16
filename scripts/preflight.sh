#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:?Usage: scripts/preflight.sh /path/to/volume}"
exec python3 "$(dirname "$0")/../volume_audit.py" preflight "$ROOT"
