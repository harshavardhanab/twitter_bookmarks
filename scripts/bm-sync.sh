#!/bin/bash
# Daily `bm sync`, run by launchd (see scripts/install-launchd.sh).
# Logs to $BM_HOME/logs/sync-YYYY-MM-DD.log and keeps 30 days of logs.
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
export BM_HOME="${BM_HOME:-$REPO/data}"
# launchd starts with a bare PATH; uv lives in ~/.local/bin or Homebrew
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

LOGS="$BM_HOME/logs"
mkdir -p "$LOGS"
LOG="$LOGS/sync-$(date +%Y-%m-%d).log"

# One run at a time (a manual sync may still be going)
LOCK="$BM_HOME/.sync.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') another sync is running ($LOCK exists); skipped" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

{
  echo "=== $(date '+%F %T') bm sync $* ==="
  uv run --project "$REPO" bm sync "$@"
  echo "=== exit $? at $(date '+%F %T') ==="
} >> "$LOG" 2>&1

find "$LOGS" -name 'sync-*.log' -mtime +30 -delete 2>/dev/null
exit 0
