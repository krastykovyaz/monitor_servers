#!/bin/bash
# After a failover, hand grak_ai back to the lab. Run from the Mac.
# Stops the bot on horek_ge, copies its newer state to the lab, gives ownership back; the lab guard
# starts the lab copy within a minute.
set -eu -o pipefail
LAB=aak-third; SRV=root@2.26.22.251; D=/home/alex/projects/grak_ai
S="-o IPQoS=none -o BatchMode=yes -o ConnectTimeout=25"
HERE=$(cd "$(dirname "$0")" && pwd)
SUMS=$(mktemp); trap 'rm -f "$SUMS"' EXIT
[ "$(ssh -n $S $SRV 'cat /var/lib/grak-failover/owner')" = horek_ge ] || { echo "the lab already owns grak_ai"; exit 0; }
ssh -n $S $LAB 'touch ~/.local/share/grak_guard/paused'
ssh -n $S $SRV 'systemctl stop grak-ai; echo "horek_ge: $(systemctl is-active grak-ai || true)"'
ssh $S $SRV "cd /opt/grak_ai && runuser -u grakai -- python3 -" < "$HERE/../checkpoint.py"
ssh -n $S $SRV "cd /opt/grak_ai && sha256sum session2.session content_database.db" > "$SUMS"
ssh -n $S $SRV "tar -C /opt/grak_ai -cf - session2.session content_database.db" | ssh $S $LAB "tar -C $D -xf -"
ssh $S $LAB "cd $D && sha256sum -c --quiet" < "$SUMS"
ssh -n $S $SRV "tar -C /opt/grak_ai -cf - test_group_dedup.db" | ssh $S $LAB "tar -C /home/alex/projects -xf -"
ssh -n $S $LAB "rm -f $D/content_database.db-wal $D/content_database.db-shm"
ssh -n $S $SRV 'D=/var/lib/grak-failover; echo lab > $D/owner; touch $D/lease; echo 0 > $D/expired_count; echo "$(date "+%F %T") FAILBACK: ownership returned to the lab" >> $D/failover.log'
ssh -n $S $LAB 'rm -f ~/.local/share/grak_guard/paused'
echo "ownership returned to the lab; its guard starts the bot within a minute"
