#!/bin/bash
# Distribute a new Telegram bot token to every place that uses it, after revoking the old one in BotFather
# (/revoke, then /token). Run from the Mac:  bash deploy/rotate_bot_token.sh
# The token is read from the keyboard, so it never appears in shell history or process lists.
set -u
cd "$(dirname "$0")/.."
read -r -s -p "New bot token: " TOKEN; echo
case "$TOKEN" in [0-9]*:*) ;; *) echo "that does not look like a bot token"; exit 1;; esac
ok=$(curl -s -m 10 "https://api.telegram.org/bot$TOKEN/getMe" | grep -o '"ok":true')
[ -n "$ok" ] || { echo "Telegram does not accept this token"; exit 1; }

S="-o IPQoS=none -o BatchMode=yes -o ConnectTimeout=25 -o ClearAllForwardings=yes"
SETENV='f="$1"; [ -f "$f" ] || exit 0; umask 077; { grep -v "^TG_BOT_TOKEN=" "$f"; echo "TG_BOT_TOKEN=$T"; } > "$f.new" && chmod 600 "$f.new" && mv "$f.new" "$f" && echo "  updated $f"'

python3 - "$TOKEN" <<'PY'
import sys, os
p = ".env"; t = sys.argv[1]
lines = [l for l in open(p).read().splitlines() if not l.startswith("TG_BOT_TOKEN=")] + ["TG_BOT_TOKEN=" + t]
open(p, "w").write("\n".join(lines) + "\n"); os.chmod(p, 0o600); print("  updated local .env")
PY
for h in root@2.26.22.251 root@2.27.33.175; do
  printf '%s' "$TOKEN" | ssh $S $h "T=\$(cat); export T; for f in /root/monitor_servers/.env /etc/monitor-heartbeat.env; do bash -c '$SETENV' _ \$f; done
    if grep -q '^BOT_TOKEN=' /root/.env 2>/dev/null; then sed -i \"s|^BOT_TOKEN=.*|BOT_TOKEN=\$T|\" /root/.env && echo '  updated /root/.env (VPN bot)' && systemctl restart tsech-vpn-bot; fi
    systemctl is-active --quiet monitor-heartbeat && systemctl restart monitor-heartbeat && echo '  restarted heartbeat receiver'"
done
for h in aak-first aak-second aak-third aak-llm sedan sherlock; do
  printf '%s' "$TOKEN" | ssh $S $h "T=\$(cat); export T; bash -c '$SETENV' _ \$HOME/monitor_servers/.env" 2>&1 | grep -vE "^bind|channel_setup|Could not request"
done
grep -q "BOT_TOKEN *= *\"[0-9]" <(ssh $S root@2.26.22.251 cat /root/vpn_bot.py) && \
  echo "NOTE: /root/vpn_bot.py still has the old token in its source. Apply deploy/vpn_bot_token_from_env.py first."
echo "done. Send a test:  ssh $S root@2.26.22.251 'python3 /root/monitor_servers/watch.py --digest horek_fi'"
