#!/bin/bash
# Daily server status -> Telegram. Run: ./check_servers.sh  (add --dry to print only)
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="${HOME:-$(eval echo ~$(id -un))}"
. "$DIR/.env"
KEYOPT=(); [ -n "${SSH_KEY:-}" ] && KEYOPT=(-i "$SSH_KEY")
DRY=0; [ "${1:-}" = "--dry" ] && DRY=1
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

REMOTE='printf "HOST=%s|UP=%s|LOAD=%s|CPUS=%s|MEM=%s|DISK=%s|DOCKER=%s\n" "$(hostname)" "$(uptime -p 2>/dev/null | sed "s/^up //")" "$(cut -d" " -f1-3 /proc/loadavg)" "$(nproc)" "$(free -m | awk "/Mem:/{printf \"%d%%\", \$3*100/\$2}")" "$(df -h / | awk "NR==2{print \$5}")" "$(command -v docker >/dev/null && docker ps -q 2>/dev/null | wc -l | tr -d " " || echo -)"; exit'

probe() {  # name target port
  local name="$1" target="$2" port="$3" out
  out=$(ssh -tt "${KEYOPT[@]}" -p "$port" -o IPQoS=none -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 \
        -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR "$target" "$REMOTE" 2>&1 | tr -d '\r')
  local line; line=$(echo "$out" | grep -m1 '^HOST=')
  if [ -n "$line" ]; then
    local up load cpus mem disk dock
    up=$(echo "$line"   | sed 's/.*|UP=\([^|]*\).*/\1/')
    load=$(echo "$line" | sed 's/.*|LOAD=\([^|]*\).*/\1/')
    cpus=$(echo "$line" | sed 's/.*|CPUS=\([^|]*\).*/\1/')
    mem=$(echo "$line"  | sed 's/.*|MEM=\([^|]*\).*/\1/')
    disk=$(echo "$line" | sed 's/.*|DISK=\([^|]*\).*/\1/')
    dock=$(echo "$line" | sed 's/.*|DOCKER=\([^|]*\).*/\1/')
    local warn=""
    [ "${disk%\%}" -ge 85 ] 2>/dev/null && warn="$warn ⚠️disk"
    [ "${mem%\%}" -ge 85 ] 2>/dev/null && warn="$warn ⚠️ram"
    local l1=${load%% *}; awk -v l="$l1" -v c="$cpus" 'BEGIN{exit !(l>c)}' && warn="$warn ⚠️load"
    printf '✅ <b>%s</b>%s\n   up %s · load %s/%s cpu · ram %s · disk %s · docker %s\n' \
      "$name" "$warn" "$up" "$l1" "$cpus" "$mem" "$disk" "$dock"
  else
    local err; err=$(echo "$out" | grep -v '^$' | tail -1 | sed 's/^ssh: //; s/.*Permission denied.*/Permission denied (key not accepted)/; s/.*timed out.*/timed out/; s/.*Could not resolve.*/DNS failed/; s/.*Broken pipe.*/session dropped/')
    printf '❌ <b>%s</b> — %s\n' "$name" "${err:-no response}"
  fi
}

i=0
while IFS='|' read -r name target port; do
  name=$(echo "$name" | xargs); target=$(echo "$target" | xargs); port=$(echo "${port:-22}" | xargs)
  [ -z "$name" ] || [ "${name#\#}" != "$name" ] && continue
  i=$((i+1)); probe "$name" "$target" "$port" > "$TMP/$(printf '%02d' $i)" &
done < "$DIR/servers.conf"
wait

body=$(cat "$TMP"/* 2>/dev/null)
ok=$(grep -c '^✅' <<<"$body"); bad=$(grep -c '^❌' <<<"$body")
if [ -f "$DIR/ollama.conf" ]; then
  ol=""
  while read -r ep; do
    ep=$(echo "$ep" | xargs); [ -z "$ep" ] || [ "${ep#\#}" != "$ep" ] && continue
    n=$(python3 - "$ep" <<'PY2'
import sys, json, urllib.request
ep=sys.argv[1]; best=None
for path,key in (("/api/tags","models"),("/v1/models","data")):
    try:
        d=json.load(urllib.request.urlopen(f"http://{ep}{path}", timeout=5))
        if isinstance(d.get(key), list): best=max(best or 0, len(d[key]))
    except Exception: pass
print("" if best is None else best)
PY2
)
    if [ -n "$n" ]; then ol="$ol✅ $ep · $n models"$'\n'; else ol="$ol❌ $ep · no answer"$'\n'; fi
  done < "$DIR/ollama.conf"
  body="$body"$'\n'"🧠 <b>Ollama API</b>"$'\n'"$ol"
fi
msg="🖥 <b>Server status</b> $(date '+%Y-%m-%d %H:%M')  —  ${ok} up, ${bad} down"$'\n\n'"$body"

if [ "$DRY" = 1 ]; then echo "$msg"; exit 0; fi

# Auto-register chat id on first run: the first chat that messaged the bot
if [ -z "${TG_CHAT_ID:-}" ]; then
  TG_CHAT_ID=$(curl -s -m 15 "https://api.telegram.org/bot$TG_BOT_TOKEN/getUpdates" | python3 -c 'import sys,json
for u in json.load(sys.stdin).get("result",[]):
    m=u.get("message") or u.get("channel_post")
    if m: print(m["chat"]["id"]); break')
  if [ -z "$TG_CHAT_ID" ]; then echo "$(date) no chat id: send /start to the bot first" >&2; echo "$msg"; exit 2; fi
  sed -i '' "s/^TG_CHAT_ID=.*/TG_CHAT_ID=$TG_CHAT_ID/" "$DIR/.env"
fi

python3 - "$TG_BOT_TOKEN" "$TG_CHAT_ID" "$msg" <<'PY'
import sys, json, urllib.request, urllib.parse
tok, chat, msg = sys.argv[1], sys.argv[2], sys.argv[3]
chunks, cur = [], ""
for line in msg.split("\n"):
    if len(cur) + len(line) + 1 > 3900: chunks.append(cur); cur = ""
    cur += line + "\n"
chunks.append(cur)
for c in chunks:
    data = urllib.parse.urlencode({"chat_id": chat, "text": c, "parse_mode": "HTML", "disable_web_page_preview": "1"}).encode()
    r = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=20))
    print("sent" if r.get("ok") else f"telegram error: {r}")
PY
