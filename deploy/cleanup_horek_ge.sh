#!/bin/bash
# One-off disk cleanup for horek_ge, written 2026-10-03 after inspecting the host.
# Removes only things no running service, container or live editor session uses.
# Each step re-checks usage and skips itself if something is in use.
# Run from the Mac:  ssh -o IPQoS=none root@2.26.22.251 "bash -s" < deploy/cleanup_horek_ge.sh
set -u
before=$(df --output=avail -BM / | tail -1 | tr -dc 0-9)
echo "free before: ${before} MB"

echo "== 1. old VS Code server builds (keep newest complete build)"
S=/root/.vscode-server/cli/servers; KEEP=Stable-07f806f999227108933c2e30515b26eecc1fda74
if [ -d "$S/$KEEP" ] && [ "$(ls -l /proc/[0-9]*/exe /proc/[0-9]*/cwd 2>/dev/null | grep -c 'vscode-server/cli/servers')" = 0 ]; then
  for d in "$S"/Stable-*; do [ "$(basename "$d")" = "$KEEP" ] && continue; rm -rf -- "$d" && echo "  removed $(basename "$d")"; done
else echo "  SKIPPED: a server build is in use or keep-target missing"; fi

echo "== 2. old Cursor server builds (keep newest)"
C=/root/.cursor-server/bin/linux-x64
if [ -d "$C/0c32194e3fb5ffaced9fb36430b860ec301e1fc0" ]; then for v in 280eca2911f1774689696e5f1efa5a4f97a87af0 d6f462cdd0a6a6d1cff570daf980e671d0a63de0; do
  if ls -l /proc/[0-9]*/exe 2>/dev/null | grep -q "$v"; then echo "  SKIPPED $v: in use"; else rm -rf -- "$C/$v" && echo "  removed $v"; fi; done; fi

echo "== 3. unused Claude CLI build 2.1.281"
if ps -eo args | grep -q "[c]cd-cli/2.1.281"; then echo "  SKIPPED: in use"; else rm -f -- /root/.claude/remote/ccd-cli/2.1.281 && echo "  removed"; fi

echo "== 4. Chromium build 1234 (only the host dev venv of force1 wants it; no service does)"
if pgrep -f "ms-playwright/chromium(_headless_shell)?-1234" >/dev/null; then echo "  SKIPPED: running"; else rm -rf -- /root/.cache/ms-playwright/chromium-1234 /root/.cache/ms-playwright/chromium_headless_shell-1234 && echo "  removed"; fi

echo "== 5. Docker: dead open-webui container and image (volume kept), stopped force1 test containers and images"
docker inspect open-webui > /root/open-webui.container.json 2>/dev/null && echo "  saved definition to /root/open-webui.container.json"
for c in open-webui force1_test_catalog_bot_1 force1_test_buyer_1; do [ "$(docker inspect -f '{{.State.Running}}' $c 2>/dev/null)" = "false" ] && docker rm $c >/dev/null && echo "  removed container $c"; done
for i in ghcr.io/open-webui/open-webui:main force1_test_catalog_bot:latest force1_test_buyer:latest 4e4bb1197103; do docker rmi $i >/dev/null 2>&1 && echo "  removed image $i" || echo "  kept image $i (still referenced)"; done
docker image prune -f 2>/dev/null | tail -1

echo "== 6. logs: journal down to 100 MB, rotated log files"
journalctl --vacuum-size=100M 2>&1 | tail -1
rm -f -- /var/log/*.gz /var/log/*.1 /var/log/*/*.gz 2>/dev/null; echo "  rotated files removed"

after=$(df --output=avail -BM / | tail -1 | tr -dc 0-9)
echo "free after: ${after} MB  (reclaimed $(( after-before )) MB)"
df -h / | tail -1
echo "== health check"
for u in compass lookwise reviewreports tsech-api tsech-frontend tsech-vpn-bot vless-bot vpn-web-portal; do printf "%s=%s " $u "$(systemctl is-active $u)"; done; echo
docker ps --format '{{.Names}}: {{.Status}}'; docker volume ls -q | sed 's/^/volume kept: /'
echo "claude sessions alive: $(pgrep -f 'ccd-cli/2.1.28[46]' | wc -l), cursor agent alive: $(pgrep -f cursor-agent-worker | wc -l)"
ls /root/.cache/ms-playwright | paste -sd' ' -
