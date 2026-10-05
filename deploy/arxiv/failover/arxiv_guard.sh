#!/bin/bash
# aak-second: /usr/local/sbin/arxiv-guard, run every minute from /etc/cron.d/arxiv-guard as root.
# Renews the lab's lease on horek_fi, pushes a state snapshot every 15 minutes, and stops the lab bots when
# it can no longer prove it is the owner (restarting them once ownership is confirmed again).
#
# The guard restarts the bots only after it stopped them itself (fenced) or after failback.sh asked for it
# (start_requested). Maintenance: touch /home/alex/.local/share/arxiv_guard/paused to hold both.
set -u
D=/home/alex/projects/arxiv_bot; ST=/var/lib/arxiv-guard
PAUSE=/home/alex/.local/share/arxiv_guard/paused; REQ=/home/alex/.local/share/arxiv_guard/start_requested
KEY=/etc/arxiv-guard/key; SRV=root@2.27.33.175; SNAP=/dev/shm/arxiv_snap
SVCS="arxiv arxiv_bot"
S="-i $KEY -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 -o IPQoS=none -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/etc/arxiv-guard/known_hosts"
FENCE_AFTER=300; PUSH_EVERY=890
mkdir -p "$ST"
exec 9>"$ST/lock"; flock -n 9 || exit 0
log() { echo "$(date '+%F %T') $*" >> "$ST/guard.log"; }
running() { local s; for s in $SVCS; do systemctl is-active --quiet "$s" && return 0; done; return 1; }
alert() {
  ( set -a; . /home/alex/monitor_servers/.env; set +a
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TG_BOT_TOKEN" |
      curl -s -m 15 -o /dev/null -K - --data-urlencode "chat_id=$TG_CHAT_ID" --data-urlencode "text=$1" ) || true
}

r=$(timeout 25 ssh $S "$SRV" renew 2>/dev/null || true)
case "$r" in
  "owner=lab ok")
    date +%s > "$ST/last_ok"
    if { [ -f "$ST/fenced" ] || [ -f "$REQ" ]; } && [ ! -f "$PAUSE" ]; then
      rm -f "$ST/fenced" "$REQ"; systemctl reset-failed $SVCS 2>/dev/null; systemctl start $SVCS
      log "lease confirmed again; started the bots"
      alert "▶️ arxiv: the lab's lease on horek_fi is confirmed again, the guard started the bots here."
    fi
    if [ $(( $(date +%s) - $(stat -c %Y "$ST/last_push" 2>/dev/null || echo 0) )) -ge "$PUSH_EVERY" ]; then
      mkdir -p "$SNAP"; chmod 700 "$SNAP"
      if python3 /usr/local/sbin/arxiv-snapshot "$D" "$SNAP" && \
         timeout 900 rsync -a -e "ssh $S" "$SNAP/" "$SRV:./" 2>>"$ST/guard.log"; then
        touch "$ST/last_push"
      else log "snapshot push failed"; fi
      rm -rf "$SNAP"
    fi ;;
  owner=horek_fi*)
    if running; then
      systemctl stop $SVCS; touch "$ST/fenced"; log "horek_fi owns the bots; stopped the lab copies"
      alert "⏹ arxiv: horek_fi owns the bots, so the lab copies were stopped."
    fi ;;
  *)
    age=$(( $(date +%s) - $(cat "$ST/last_ok" 2>/dev/null || echo 0) ))
    if [ "$age" -ge "$FENCE_AFTER" ] && running; then
      tg=$(curl -s -m 8 -o /dev/null -w "%{http_code}" https://api.telegram.org/ 2>/dev/null || true)
      hg=$( (timeout 6 bash -c "echo > /dev/tcp/2.27.33.175/22") 2>/dev/null && echo up || echo down)
      if [ "$hg" = down ] && [ "$tg" != "000" ] && [ -n "$tg" ]; then
        log "no renewal for $age s, but horek_fi is offline and Telegram works; keeping the lab bots"
      else
        systemctl stop $SVCS; touch "$ST/fenced"
        log "no renewal for $age s (horek_fi ssh $hg, telegram $tg); stopped the lab bots so horek_fi can take over"
        alert "⏹ arxiv: the lab could not reach horek_fi for $(( age / 60 )) min and stopped its copies. horek_fi takes over after 10 min."
      fi
    fi ;;
esac
