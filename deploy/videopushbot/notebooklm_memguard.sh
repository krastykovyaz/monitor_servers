#!/bin/bash
# aak-llm: /usr/local/sbin/notebooklm-memguard, root cron every 30 minutes.
# The bot's Python heap grows with every video job and glibc keeps the freed memory. If the process holds more
# than LIMIT_MB of private memory and is idle (no ffmpeg or other child, no workspace file written in 15 minutes),
# restart it. At most once every 6 hours. Hold it with: touch /home/alex/.local/share/notebooklm_memguard_paused
set -u
SVC=notebooklm-bot; LIMIT_MB=600; COOLDOWN=21600; WS=/home/alex/videopushbot/workspace
ST=/var/lib/notebooklm-memguard; PAUSE=/home/alex/.local/share/notebooklm_memguard_paused
mkdir -p "$ST"; [ -f "$PAUSE" ] && exit 0
pid=$(systemctl show -p MainPID --value "$SVC"); [ "${pid:-0}" -gt 0 ] || exit 0
rss=$(awk '/^RssAnon/{print int($2/1024)}' /proc/$pid/status 2>/dev/null); [ "${rss:-0}" -ge "$LIMIT_MB" ] || exit 0
cg=$(systemctl show -p ControlGroup --value "$SVC")
[ "$(wc -l < "/sys/fs/cgroup$cg/cgroup.procs" 2>/dev/null || echo 9)" -le 1 ] || exit 0
[ -z "$(find "$WS" -type f -mmin -15 -print -quit 2>/dev/null)" ] || exit 0
[ $(( $(date +%s) - $(stat -c %Y "$ST/last_restart" 2>/dev/null || echo 0) )) -ge "$COOLDOWN" ] || exit 0
systemctl restart "$SVC"; touch "$ST/last_restart"; sleep 20
echo "$(date '+%F %T') restarted $SVC at ${rss} MB; now $(systemctl is-active "$SVC")" >> "$ST/memguard.log"
( set -a; . /home/alex/monitor_servers/.env; set +a
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TG_BOT_TOKEN" |
    curl -s -m 15 -o /dev/null -K - --data-urlencode "chat_id=$TG_CHAT_ID" \
      --data-urlencode "text=♻️ notebooklm-bot held ${rss} MB while idle, restarted it (now $(systemctl is-active "$SVC"))." ) || true
