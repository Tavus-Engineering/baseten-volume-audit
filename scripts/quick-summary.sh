#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:-/root/.cache/team_artifacts}"
DEPTH="${2:-2}"
ENTRIES="${3:-30}"
if ! command -v juicefs >/dev/null 2>&1; then
  echo 'juicefs CLI not available; use preflight.sh and the Python scanner, or ask for the client matching the mount.' >&2
  exit 1
fi
# Native stats: no configuration changes, repair, strict scan, or credential access.
exec nice -n 19 juicefs summary --depth "$DEPTH" --entries "$ENTRIES" "$ROOT"
