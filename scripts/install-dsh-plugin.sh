#!/usr/bin/env bash
# Link this checkout's Cordis plugin into one dsh profile.
# The profile you wake (default: headless) and the profile you chat in (often web)
# are different. Run this once per profile.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN="$ROOT/integrations/dsh-memory-plugin"
PROFILE="${DSH_PROFILE:-headless}"
KERNEL_URL="${KERNEL_URL:-http://127.0.0.1:8741}"
MEMORY_USER_ID="${MEMORY_USER_ID:-default}"

echo "KERNEL_URL=$KERNEL_URL"
echo "MEMORY_USER_ID=$MEMORY_USER_ID"
echo "DSH_PROFILE=$PROFILE"
echo "plugin=$PLUGIN"

if curl -sf "$KERNEL_URL/health" >/dev/null; then
  echo "kernel health ok"
else
  echo "kernel is not reachable. Start it with: memory-kernel serve" >&2
fi

if ! command -v dsh >/dev/null 2>&1; then
  echo "dsh is not on PATH. After installing DeepSeek Harness, run:" >&2
  echo "  dsh plugin --profile $PROFILE add $PLUGIN" >&2
  exit 1
fi

dsh plugin --profile "$PROFILE" add "$PLUGIN"
echo "linked @memory-kernel/dsh-memory into profile $PROFILE"
echo "verify the row:"
echo "  dsh --profile $PROFILE --dump-config | grep memory-kernel-plugin"
echo "verify injection (kernel must be up, and this profile must be the one you just installed):"
echo "  MEMORY_USER_ID=$MEMORY_USER_ID memory-kernel remember --user $MEMORY_USER_ID --text '验证口令是青柑'"
echo "  KERNEL_URL=$KERNEL_URL MEMORY_USER_ID=$MEMORY_USER_ID dsh --profile $PROFILE \"我的验证口令是什么？只根据已经注入的记忆回答。\""
