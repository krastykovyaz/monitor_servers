#!/bin/bash
# aak-third: ~/.local/bin/grak_guard.sh, run every minute from cron as alex.
# Renews the lab's lease on horek_ge, keeps the lab bot running while the lab owns it, pushes a state
# snapshot every 5 minutes, and stops the lab bot when it can no longer prove it is the owner.
#
# Maintenance: touch ~/.local/share/grak_guard/paused to stop the guard from starting the bot.
set -u
D=/home/alex/projects/grak_ai; ST=$HOME/.local/share/grak_guard
KEY=$HOME/.ssh/id_ed25519_grak_gate; SRV=root@2.26.22.251
S="-i $KEY -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 -o IPQoS=none -o StrictHostKeyChecking=accept-new"
FENCE_AFTER=300; PUSH_EVERY=290
mkdir -p "$ST/snap"
exec 9>"$ST/lock"; flock -n 9 || exit 0
log() { echo "$(date '+%F %T') $*" >> "$ST/guard.log"; }
botpid() { for p in $(pgrep -u "$(id -u)" -x python3); do [ "$(readlink /proc/$p/cwd)" = "$D" ] && echo "$p"; done | head -1; }
stopbot() {
  local p; p=$(botpid); [ -n "$p" ] || return 0
  kill -INT "$p"; for i in $(seq 1 20); do [ -z "$(botpid)" ] && return 0; sleep 1; done
  p=$(botpid); [ -n "$p" ] && kill -TERM "$p"; sleep 5
  p=$(botpid); [ -n "$p" ] && kill -KILL "$p"; return 0
}
startbot() {
  tmux has-session -t news 2>/dev/null || tmux new -d -s news -c "$D"
  tmux send-keys -t news "cd $D && venv/bin/python3 -u main.py" Enter
}
alert() {
  ( set -a; . "$HOME/monitor_servers/.env"; set +a
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TG_BOT_TOKEN" |
      curl -s -m 15 -o /dev/null -K - --data-urlencode "chat_id=$TG_CHAT_ID" --data-urlencode "text=$1" ) || true
}

r=$(timeout 25 ssh $S "$SRV" renew 2>/dev/null || true)
case "$r" in
  "owner=lab ok")
    date +%s > "$ST/last_ok"
    if [ -f "$ST/fenced" ]; then rm -f "$ST/fenced"; log "lease renewed again"; fi
    if [ -z "$(botpid)" ] && [ ! -f "$ST/paused" ]; then
      startbot; log "bot was not running; started it"
      alert "▶️ grak_ai: the lab bot was not running, the guard started it."
    fi
    if [ $(( $(date +%s) - $(stat -c %Y "$ST/last_push" 2>/dev/null || echo 0) )) -ge "$PUSH_EVERY" ]; then
      if python3 "$HOME/.local/bin/grak_snapshot.py" "$D" "$ST/snap" && \
         timeout 240 rsync -a -e "ssh $S" "$ST/snap/" "$SRV:./" 2>>"$ST/guard.log"; then
        touch "$ST/last_push"
      else log "snapshot push failed"; fi
    fi ;;
  owner=horek_ge*)
    if [ -n "$(botpid)" ]; then
      stopbot; log "horek_ge owns the bot; stopped the lab copy"
      alert "⏹ grak_ai: horek_ge owns the bot, so the lab copy was stopped."
    fi ;;
  *)
    age=$(( $(date +%s) - $(cat "$ST/last_ok" 2>/dev/null || echo 0) ))
    if [ "$age" -ge "$FENCE_AFTER" ] && [ -n "$(botpid)" ]; then
      tg=$(curl -s -m 8 -o /dev/null -w "%{http_code}" https://api.telegram.org/ 2>/dev/null || true)
      hg=$( (timeout 6 bash -c "echo > /dev/tcp/2.26.22.251/443") 2>/dev/null && echo up || echo down)
      if [ "$hg" = down ] && [ "$tg" != "000" ] && [ -n "$tg" ]; then
        log "no renewal for $age s, but horek_ge is offline and Telegram works; keeping the lab bot"
      else
        stopbot; touch "$ST/fenced"
        log "no renewal for $age s (horek_ge https $hg, telegram $tg); stopped the lab bot so horek_ge can take over"
        alert "⏹ grak_ai: the lab could not reach horek_ge for $(( age / 60 )) min and stopped its copy. horek_ge takes over after 10 min."
      fi
    fi ;;
esac
