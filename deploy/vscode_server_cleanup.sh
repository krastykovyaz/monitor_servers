#!/bin/bash
# Trim ~/.vscode-server for the current user: keep the newest server build and anything in use,
# remove older builds, half-downloaded ones, stale launcher processes and old caches.
# Safe to run from cron. Usage: vscode_server_cleanup.sh [--dry]
set -u
DRY=0; [ "${1:-}" = "--dry" ] && DRY=1
V="$HOME/.vscode-server"
[ -d "$V" ] || { echo "$(date '+%F %T') no $V, nothing to do"; exit 0; }
run() { if [ "$DRY" = 1 ]; then echo "    would run: $*"; else "$@"; fi; }
before=$(du -sm "$V" | cut -f1)
echo "$(date '+%F %T') vscode-server cleanup for $(id -un)@$(hostname)$([ $DRY = 1 ] && echo ' (dry run)')"

# 1. launcher processes left behind by old sessions: older than 7 days and without children
for pid in $(pgrep -u "$(id -u)" -f "$V/code-[0-9a-f]+ "); do
  age=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
  if [ -n "$age" ] && [ "$age" -gt 604800 ] && [ -z "$(pgrep -P "$pid")" ]; then
    echo "  stale session launcher, pid $pid, $((age / 86400)) days old"; run kill "$pid"
  fi
done
[ "$DRY" = 1 ] || sleep 2

# builds referenced by any running process
inuse=$( { ls -l /proc/[0-9]*/exe /proc/[0-9]*/cwd 2>/dev/null; ps -eo args; } \
  | grep -oE "vscode-server/(cli/servers|bin)/[A-Za-z0-9._-]+" | sed 's#.*/##' | sort -u)
used() { echo "$inuse" | grep -qxF "$1"; }

# 2. server builds, new layout (cli/servers) and old layout (bin)
for base in "$V/cli/servers" "$V/bin"; do
  [ -d "$base" ] || continue
  newest=$(ls -td "$base"/*/ 2>/dev/null | grep -v '\.staging/$' | head -1)
  newest=$(basename "${newest:-/none}")
  for d in "$base"/*/; do
    [ -d "$d" ] || continue
    b=$(basename "$d")
    [ "$b" = "$newest" ] && { echo "  keep   $b (newest)"; continue; }
    used "$b" && { echo "  keep   $b (in use)"; continue; }
    echo "  remove $b ($(du -sh "$d" | cut -f1))"; run rm -rf -- "$d"
  done
done

# 3. launcher binaries: keep the newest and any still running
newest_bin=$(ls -t "$V"/code-* 2>/dev/null | head -1)
for f in "$V"/code-*; do
  [ -f "$f" ] || continue
  [ "$f" = "$newest_bin" ] && continue
  pgrep -f "$f " >/dev/null 2>&1 && { echo "  keep   $(basename "$f") (running)"; continue; }
  echo "  remove launcher $(basename "$f")"; run rm -f -- "$f"
done

# 4. caches that VS Code re-creates on demand
[ -d "$V/data/CachedExtensionVSIXs" ] && run find "$V/data/CachedExtensionVSIXs" -type f -mtime +14 -delete
[ -d "$V/data/logs" ] && run find "$V/data/logs" -type f -mtime +14 -delete

after=$(du -sm "$V" | cut -f1)
echo "  size: ${before} MB -> ${after} MB, freed $((before - after)) MB; disk now $(df -h --output=pcent "$HOME" | tail -1 | tr -d ' ') used"
