#!/usr/bin/env bash
set -euo pipefail
if (( $# < 2 )); then
  echo 'Usage: scripts/scan.sh ROOT LOCAL_AUDIT_DB [scan options...]' >&2
  exit 1
fi
ROOT="$1"
DB="$2"
shift 2
# ionice helps some local disks; it does not govern NFS/Lustre server load.
command=(python3 "$(dirname "$0")/../volume_audit.py" scan "$ROOT" --db "$DB" --rate 1000 "$@")
if command -v ionice >/dev/null 2>&1; then
  exec nice -n 19 ionice -c 3 "${command[@]}"
fi
exec nice -n 19 "${command[@]}"
