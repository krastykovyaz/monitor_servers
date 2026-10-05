#!/bin/bash
# Run on aak-second as root:  sudo bash /home/alex/.local/share/arxiv_guard/stage/install_lab.sh
# Installs the lease guard. The running bots are not touched or restarted.
# Undo: sudo rm /etc/cron.d/arxiv-guard /etc/systemd/system/arxiv.service.d/guard.conf /etc/systemd/system/arxiv_bot.service.d/guard.conf && sudo systemctl daemon-reload
set -eu -o pipefail
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 1; }
ST=/home/alex/.local/share/arxiv_guard/stage
SRV=root@2.27.33.175
install -d -m 755 /etc/arxiv-guard /var/lib/arxiv-guard
install -m 755 -o root -g root "$ST/arxiv_guard.sh" /usr/local/sbin/arxiv-guard
install -m 755 -o root -g root "$ST/arxiv_guard_precheck.sh" /usr/local/sbin/arxiv-guard-precheck
install -m 755 -o root -g root "$ST/arxiv_snapshot.py" /usr/local/sbin/arxiv-snapshot
install -m 600 -o root -g root "$ST/key" /etc/arxiv-guard/key
r=$(ssh -i /etc/arxiv-guard/key -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20 -o IPQoS=none \
    -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/etc/arxiv-guard/known_hosts "$SRV" renew)
[ "$r" = "owner=lab ok" ] || { echo "gate answered: $r"; exit 1; }
echo "gate on horek_fi: $r"
echo '* * * * * root /usr/local/sbin/arxiv-guard' > /etc/cron.d/arxiv-guard; chmod 644 /etc/cron.d/arxiv-guard
for u in arxiv arxiv_bot; do
  install -d /etc/systemd/system/$u.service.d
  printf '[Service]\nExecStartPre=/usr/local/sbin/arxiv-guard-precheck\n' > /etc/systemd/system/$u.service.d/guard.conf
done
systemctl daemon-reload
shred -u "$ST/key"
echo "installed. bots now: arxiv=$(systemctl is-active arxiv) arxiv_bot=$(systemctl is-active arxiv_bot)"
echo "the first snapshot is pushed to horek_fi within about a minute; log: /var/lib/arxiv-guard/guard.log"
