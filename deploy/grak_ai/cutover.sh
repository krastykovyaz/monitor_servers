#!/bin/bash
# Move the live grak_ai bot from aak-third (tmux session "news") to horek_ge (service grak-ai).
# Run from the Mac. The lab copy is stopped first, so the Telegram session never runs in two places.
#
#   bash deploy/grak_ai/cutover.sh --stop-test            also stop grak_ai_test in the lab (shared dedup stays correct)
#   bash deploy/grak_ai/cutover.sh --accept-split-dedup   keep grak_ai_test running in the lab; the two bots stop
#                                                          seeing each other's posts and may duplicate in VK group 204810838
set -u
MODE="${1:-}"
case "$MODE" in --stop-test|--accept-split-dedup) ;; *) sed -n '2,9p' "$0"; exit 2;; esac
LAB=aak-third; SRV=root@2.26.22.251; D=/home/alex/projects/grak_ai
S="-o IPQoS=none -o BatchMode=yes -o ConnectTimeout=25"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
labpid() { ssh $S $LAB "for p in \$(pgrep -u alex -x python3); do [ \"\$(readlink /proc/\$p/cwd)\" = \"$1\" ] && echo \$p; done | head -1"; }

echo "1. preflight"
[ "$(ssh $S $SRV 'systemctl is-active grak-ai')" != active ] || { echo "   grak-ai already runs on horek_ge, stopping here"; exit 1; }
P=$(labpid $D); [ -n "$P" ] || { echo "   no running grak_ai in the lab, stopping here"; exit 1; }
echo "   lab bot pid $P"

echo "2. stop the lab bot (SIGINT, so Telethon and SQLite close cleanly)"
ssh $S $LAB "kill -INT $P"; for i in $(seq 1 30); do [ -z "$(labpid $D)" ] && break; sleep 1; done
[ -z "$(labpid $D)" ] || { ssh $S $LAB "kill -TERM $P"; sleep 5; }
[ -z "$(labpid $D)" ] || { echo "   lab bot did not stop, aborting; nothing was changed on horek_ge"; exit 1; }
echo "   stopped"
if [ "$MODE" = --stop-test ]; then
  T=$(labpid /home/alex/projects/grak_ai_test); [ -n "$T" ] && ssh $S $LAB "kill -INT $T" && sleep 10 && echo "   grak_ai_test stopped too"
fi

echo "3. copy the live state: session, content database, shared dedup registry"
rsync -a -e "ssh $S" "$LAB:$D/session2.session" "$LAB:$D/content_database.db" "$TMP/"
rsync -a -e "ssh $S" "$LAB:$D/content_database.db-wal" "$LAB:$D/content_database.db-shm" "$TMP/" 2>/dev/null || true
rsync -a -e "ssh $S" "$LAB:/home/alex/projects/test_group_dedup.db" "$TMP/test_group_dedup.db"
rsync -a -e "ssh $S" "$TMP/" "$SRV:/opt/grak_ai/"
ssh $S $SRV 'chown grakai: /opt/grak_ai/session2.session /opt/grak_ai/content_database.db* /opt/grak_ai/test_group_dedup.db && chmod 600 /opt/grak_ai/session2.session && ls -la /opt/grak_ai/session2.session /opt/grak_ai/content_database.db /opt/grak_ai/test_group_dedup.db | awk "{print \"   \"\$5, \$9}"'

echo "4. start on horek_ge and watch for 90 seconds"
ssh $S $SRV 'systemctl enable --now grak-ai >/dev/null 2>&1; sleep 90; echo "   state: $(systemctl is-active grak-ai), restarts: $(systemctl show -p NRestarts --value grak-ai)"; tail -n 400 /opt/grak_ai/grak_prod.log | grep -iE "Traceback|Error|phone|code|HEARTBEAT|подключ|connected|запущ" | tail -6 | sed "s/[0-9]\{8,\}:[A-Za-z0-9_-]\{30,\}/<token>/g; s/^/   /"'
if [ "$(ssh $S $SRV 'systemctl is-active grak-ai')" != active ]; then
  echo "   NOT running on horek_ge. Roll back with: bash deploy/grak_ai/rollback.sh"; exit 1
fi
echo "done. Keep watching:  ssh $S $SRV 'tail -f /opt/grak_ai/grak_prod.log'"
echo "Then update expect.conf: aak-third now runs $( [ "$MODE" = --stop-test ] && echo 0 || echo 1 ) copy of venv/bin/python3 -u main.py"
