#!/usr/bin/env bash
# Cron entry: tick the kernel, then wake dsh once per newly fired intent.
# tick is the only scheduler. This script does not scan due times itself.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export KERNEL_URL="${KERNEL_URL:-http://127.0.0.1:8741}"
export MEMORY_USER_ID="${MEMORY_USER_ID:-default}"
export DSH_PROFILE="${DSH_PROFILE:-headless}"

if [[ -x "$ROOT/.venv/bin/memory-kernel" ]]; then
  exec "$ROOT/.venv/bin/memory-kernel" wake "$@"
fi
exec memory-kernel wake "$@"
