#!/bin/bash
# Put grak_ai back in the lab: stop it on horek_ge, copy the newer state back, restart it in tmux "news".
set -eu -o pipefail
LAB=aak-third; SRV=root@2.26.22.251; D=/home/alex/projects/grak_ai
S="-o IPQoS=none -o BatchMode=yes -o ConnectTimeout=25"
SUMS=$(mktemp); trap 'rm -f "$SUMS"' EXIT
ssh -n $S $SRV 'systemctl disable --now grak-ai 2>/dev/null || true; echo "horek_ge: $(systemctl is-active grak-ai || true)"'
# Fold SQLite's journal into the main file first, or the last writes stay behind in content_database.db-wal.
ssh -n $S $SRV "cd /opt/grak_ai && runuser -u grakai -- python3 -c "import sqlite3; c = sqlite3.connect('content_database.db'); print('   checkpoint:', c.execute('pragma wal_checkpoint(TRUNCATE)').fetchone()); c.close()" && rm -f content_database.db-wal content_database.db-shm"
ssh -n $S $SRV "cd /opt/grak_ai && sha256sum session2.session content_database.db" > "$SUMS"
ssh -n $S $SRV "tar -C /opt/grak_ai -cf - session2.session content_database.db" | ssh $S $LAB "tar -C $D -xf -"
ssh $S $LAB "cd $D && sha256sum -c --quiet" < "$SUMS"
ssh -n $S $SRV "tar -C /opt/grak_ai -cf - test_group_dedup.db" | ssh $S $LAB "tar -C /home/alex/projects -xf -"
ssh -n $S $LAB "rm -f $D/content_database.db-wal $D/content_database.db-shm; tmux has-session -t news 2>/dev/null || tmux new -d -s news -c $D; tmux send-keys -t news 'cd $D && venv/bin/python3 -u main.py' Enter; sleep 15; for p in \$(pgrep -u alex -x python3); do [ \"\$(readlink /proc/\$p/cwd)\" = $D ] && echo \"lab bot running again, pid \$p\"; done"
echo "Restore the expect.conf line for aak-third if the lab copy stays."
