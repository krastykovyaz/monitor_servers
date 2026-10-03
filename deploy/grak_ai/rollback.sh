#!/bin/bash
# Put grak_ai back in the lab: stop it on horek_ge, copy the newer state back, restart it in tmux "news".
set -u
LAB=aak-third; SRV=root@2.26.22.251; D=/home/alex/projects/grak_ai
S="-o IPQoS=none -o BatchMode=yes -o ConnectTimeout=25"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
ssh $S $SRV 'systemctl disable --now grak-ai 2>/dev/null; echo "horek_ge: $(systemctl is-active grak-ai)"'
rsync -a -e "ssh $S" "$SRV:/opt/grak_ai/session2.session" "$SRV:/opt/grak_ai/content_database.db" "$TMP/"
rsync -a -e "ssh $S" "$SRV:/opt/grak_ai/test_group_dedup.db" "$TMP/" 2>/dev/null || true
rsync -a -e "ssh $S" "$TMP/session2.session" "$TMP/content_database.db" "$LAB:$D/"
[ -f "$TMP/test_group_dedup.db" ] && rsync -a -e "ssh $S" "$TMP/test_group_dedup.db" "$LAB:/home/alex/projects/test_group_dedup.db"
ssh $S $LAB "rm -f $D/content_database.db-wal $D/content_database.db-shm; tmux has-session -t news 2>/dev/null || tmux new -d -s news -c $D; tmux send-keys -t news 'cd $D && venv/bin/python3 -u main.py' Enter; sleep 15; pgrep -fa 'venv/bin/python3 -u main.py' | head -3"
