#!/bin/bash
# After a failover, hand the arxiv bots back to the lab. Run from the Mac.
# Stops the bots on horek_fi, copies their newer state to the lab, gives ownership back; the lab guard
# starts the lab copies within a minute. Needs the lab bots to be stopped (the guard stops them once
# horek_fi owns the bots).
set -eu -o pipefail
LAB=aak-second; SRV=root@2.27.33.175; D=/home/alex/projects/arxiv_bot
S="-o IPQoS=none -o BatchMode=yes -o ConnectTimeout=25"
SUMS=$(mktemp)
trap 'rm -f "$SUMS"; ssh -n $S $SRV "rm -f /var/lib/arxiv-failover/hold" || true' EXIT
[ "$(ssh -n $S $SRV 'cat /var/lib/arxiv-failover/owner')" = horek_fi ] || { echo "the lab already owns the arxiv bots"; exit 0; }
[ "$(ssh -n $S $LAB 'systemctl is-active arxiv arxiv_bot | grep -c "^active"' || true)" = 0 ] || { echo "the lab bots are still running; stop them first (sudo systemctl stop arxiv arxiv_bot on aak-second)"; exit 1; }
ssh -n $S $LAB 'mkdir -p ~/.local/share/arxiv_guard && touch ~/.local/share/arxiv_guard/paused'
ssh -n $S $SRV 'touch /var/lib/arxiv-failover/hold'   # the watchdog must not restart the bots while data moves
ssh -n $S $SRV 'systemctl stop arxiv arxiv_bot; echo "horek_fi: arxiv=$(systemctl is-active arxiv || true) arxiv_bot=$(systemctl is-active arxiv_bot || true)"'
ssh $S $SRV "cd /opt/arxiv_bot && runuser -u arxiv -- python3 -" <<'PY'
import sqlite3
for n in ("papers.db", "bot.session"):
    c = sqlite3.connect(n); print(n, c.execute("pragma wal_checkpoint(TRUNCATE)").fetchone()); c.close()
PY
ssh -n $S $SRV "cd /opt/arxiv_bot && sha256sum papers.db bot.session" > "$SUMS"
ssh -n $S $SRV "tar -C /opt/arxiv_bot -cf - papers.db bot.session" | ssh $S $LAB "tar -C $D -xf -"
ssh $S $LAB "cd $D && sha256sum -c --quiet" < "$SUMS"
ssh -n $S $LAB "rm -f $D/papers.db-wal $D/papers.db-shm"
ssh -n $S $SRV 'D=/var/lib/arxiv-failover; echo lab > $D/owner; touch $D/lease; echo 0 > $D/expired_count; echo "$(date "+%F %T") FAILBACK: ownership returned to the lab" >> $D/failover.log'
ssh -n $S $LAB 'touch ~/.local/share/arxiv_guard/start_requested; rm -f ~/.local/share/arxiv_guard/paused'
echo "ownership returned to the lab; its guard starts the bots within a minute"
