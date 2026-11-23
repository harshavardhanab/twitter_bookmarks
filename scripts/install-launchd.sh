#!/bin/bash
# Install (or remove) the daily `bm sync` LaunchAgent for the current user.
#
#   scripts/install-launchd.sh               # install, runs daily at 09:30
#   scripts/install-launchd.sh --at 07:15    # pick the time
#   scripts/install-launchd.sh --dry-run     # print the plist, change nothing
#   scripts/install-launchd.sh --uninstall   # unload and delete it
#
# Logs: data/logs/sync-YYYY-MM-DD.log (one per day, 30 days kept).
# Run once by hand: launchctl kickstart gui/$(id -u)/local.bm.sync
set -euo pipefail

LABEL="local.bm.sync"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/launchd/$LABEL.plist.template"
WRAPPER="$REPO/scripts/bm-sync.sh"
LOGDIR="${BM_HOME:-$REPO/data}/logs"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

AT="09:30"
MODE="install"
while [ $# -gt 0 ]; do
  case "$1" in
    --at) AT="${2:?--at needs HH:MM}"; shift 2 ;;
    --dry-run) MODE="dry-run"; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ "$MODE" = "uninstall" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$TARGET"
  echo "Removed $LABEL."
  exit 0
fi

if ! [[ "$AT" =~ ^([01]?[0-9]|2[0-3]):([0-5][0-9])$ ]]; then
  echo "--at must be HH:MM (24h), got: $AT" >&2
  exit 2
fi
HOUR=$((10#${BASH_REMATCH[1]}))
MINUTE=$((10#${BASH_REMATCH[2]}))

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
sed_escape() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }

render() {
  sed -e "s|__WRAPPER__|$(sed_escape "$(xml_escape "$WRAPPER")")|" \
      -e "s|__LOGDIR__|$(sed_escape "$(xml_escape "$LOGDIR")")|" \
      -e "s|__HOUR__|$HOUR|" \
      -e "s|__MINUTE__|$MINUTE|" \
      "$TEMPLATE"
}

TMP="$(mktemp -t "$LABEL")"
trap 'rm -f "$TMP"' EXIT
render > "$TMP"
plutil -lint "$TMP" >/dev/null

if [ "$MODE" = "dry-run" ]; then
  cat "$TMP"
  echo "--- plist is valid; would install to $TARGET (daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE"))" >&2
  exit 0
fi

command -v uv >/dev/null || [ -x "$HOME/.local/bin/uv" ] || [ -x /opt/homebrew/bin/uv ] || {
  echo "uv not found; install it first (https://docs.astral.sh/uv/)" >&2; exit 1; }
chmod +x "$WRAPPER"
mkdir -p "$LOGDIR" "$(dirname "$TARGET")"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
cp "$TMP" "$TARGET"
launchctl bootstrap "$DOMAIN" "$TARGET"
echo "Installed $LABEL: daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE"), logs in $LOGDIR"
echo "Run it now with: launchctl kickstart $DOMAIN/$LABEL"
