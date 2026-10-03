#!/bin/bash
# Move the live grak_ai bot from aak-third (tmux session "news") to horek_ge (service grak-ai).
# Run from the Mac. The lab copy is stopped first, so the Telegram session never runs in two places.
# Used on 2026-10-03; kept for reference and for a repeat after a rollback.
#
#   bash deploy/grak_ai/cutover.sh --stop-test            also stop grak_ai_test in the lab (shared dedup stays correct)
#   bash deploy/grak_ai/cutover.sh --accept-split-dedup   keep grak_ai_test running in the lab; the two bots stop
#                                                          seeing each other's posts and may duplicate in VK group 204810838
set -eu -o pipefail
MODE="${1:-}"
case "$MODE" in --stop-test|--accept-split-dedup) ;; *) sed -n '2,10p' "$0"; exit 2;; esac
LAB=aak-third; SRV=root@2.26.22.251; D=/home/alex/projects/grak_ai
S="-o IPQoS=none -o BatchMode=yes -o ConnectTimeout=25"
SUMS=$(mktemp); trap 'rm -f "$SUMS"' EXIT
labpid() { ssh -n $S $LAB "for p in \$(pgrep -u alex -x python3); do [ \"\$(readlink /proc/\$p/cwd)\" = \"$1\" ] && echo \$p; done | head -1" || true; }

echo "1. preflight"
[ "$(ssh -n $S $SRV 'systemctl is-active grak-ai' || true)" != active ] || { echo "   grak-ai already runs on horek_ge"; exit 1; }
P=$(labpid $D); [ -n "$P" ] || { echo "   no running grak_ai in the lab"; exit 1; }
echo "   lab bot pid $P"

echo "2. stop the lab bot (SIGINT, so Telethon and SQLite close cleanly)"
ssh -n $S $LAB "kill -INT $P"
for i in $(seq 1 30); do [ -z "$(labpid $D)" ] && break; sleep 1; done
[ -z "$(labpid $D)" ] || { echo "   lab bot did not stop; nothing was changed on horek_ge"; exit 1; }
if [ "$MODE" = --stop-test ]; then
  T=$(labpid /home/alex/projects/grak_ai_test); [ -z "$T" ] || { ssh -n $S $LAB "kill -INT $T"; sleep 10; echo "   grak_ai_test stopped too"; }
fi

echo "3. copy session, content database and shared dedup registry (tar stream, checksum verified)"
ssh $S $LAB "cd $D && python3 -" < "$(dirname "$0")/checkpoint.py"
FILES=$(ssh -n $S $LAB "cd $D && ls session2.session content_database.db content_database.db-wal content_database.db-shm 2>/dev/null | paste -sd' ' -" || true)
ssh -n $S $LAB "cd $D && sha256sum $FILES" > "$SUMS"
ssh -n $S $LAB "tar -C $D -cf - $FILES" | ssh $S $SRV "tar -C /opt/grak_ai --no-same-owner -xf -"
ssh -n $S $LAB "tar -C /home/alex/projects -cf - test_group_dedup.db" | ssh $S $SRV "tar -C /opt/grak_ai --no-same-owner -xf -"
ssh $S $SRV "cd /opt/grak_ai && sha256sum -c --quiet" < "$SUMS"
ssh -n $S $SRV 'chown -R grakai:grakai /opt/grak_ai && chmod 750 /opt/grak_ai && chmod 600 /opt/grak_ai/.env /opt/grak_ai/session2.session'
echo "   copied and verified: $FILES test_group_dedup.db"

echo "4. start on horek_ge and watch for 90 seconds"
ssh -n $S $SRV 'systemctl reset-failed grak-ai 2>/dev/null; systemctl enable --now grak-ai >/dev/null 2>&1; sleep 90; echo "   state: $(systemctl is-active grak-ai), restarts: $(systemctl show -p NRestarts --value grak-ai)"; grep -E "Telegram client started|Traceback|Error" /opt/grak_ai/grak_prod.log | tail -3 | sed "s/^/   /"'
[ "$(ssh -n $S $SRV 'systemctl is-active grak-ai' || true)" = active ] || { echo "   NOT running. Roll back with: bash deploy/grak_ai/rollback.sh"; exit 1; }
echo "done. Watch:  ssh $S $SRV 'tail -f /opt/grak_ai/grak_prod.log'"
