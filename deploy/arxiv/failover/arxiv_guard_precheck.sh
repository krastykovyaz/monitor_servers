#!/bin/bash
# aak-second: /usr/local/sbin/arxiv-guard-precheck, run as ExecStartPre of arxiv.service and arxiv_bot.service.
# After a reboot the lab must not start the bots while horek_fi owns them. If horek_fi cannot be asked, start anyway.
KEY=/etc/arxiv-guard/key; SRV=root@2.27.33.175
r=$(timeout 20 ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 -o IPQoS=none -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/etc/arxiv-guard/known_hosts "$SRV" status 2>/dev/null) || exit 0
case "$r" in
  owner=horek_fi*) echo "arxiv: horek_fi owns the bots; refusing to start them here (run failback.sh to hand them back)" >&2; exit 1 ;;
esac
exit 0
