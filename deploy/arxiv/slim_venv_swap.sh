#!/bin/bash
# Run on aak-second as root:  sudo bash /home/alex/.local/share/arxiv_guard/stage/slim_venv_swap.sh
# Replaces the 507 MB venv (Jupyter, debugpy, babel and other dev tools nothing imports) with the 331 MB one built from
# the bots' real dependencies. Both services restart (about 20 s). The old venv stays as venv_old; rolls back by itself
# if either service does not come up clean. Only valid while the lab owns the bots (it checks).
set -eu -o pipefail
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 1; }
BOT=/home/alex/projects/arxiv_bot
[ -x "$BOT/venv_slim/bin/python" ] || { echo "venv_slim is missing"; exit 1; }
"$BOT/venv_slim/bin/python" -c "import PIL, feedparser, fitz, googletrans, requests, telethon, google.genai, google.generativeai" \
  || { echo "slim venv cannot import the bots' modules"; exit 1; }
for u in arxiv arxiv_bot; do systemctl is-active --quiet $u || { echo "$u is not running here (is horek_fi the owner?); not touching anything"; exit 1; }; done
systemctl stop arxiv arxiv_bot
mv "$BOT/venv" "$BOT/venv_old"; mv "$BOT/venv_slim" "$BOT/venv"
systemctl start arxiv arxiv_bot; sleep 25
if systemctl is-active --quiet arxiv && systemctl is-active --quiet arxiv_bot && \
   ! journalctl -u arxiv -u arxiv_bot --since "-30s" --no-pager | grep -qE "Traceback|ModuleNotFoundError|ImportError"; then
  echo "ok: arxiv=$(systemctl is-active arxiv) arxiv_bot=$(systemctl is-active arxiv_bot), venv $(du -sm "$BOT/venv" | cut -f1) MB"
  echo "delete the old one when you are happy: rm -rf $BOT/venv_old ($(du -sm "$BOT/venv_old" | cut -f1) MB)"
else
  echo "a service did not start cleanly; rolling back"; systemctl stop arxiv arxiv_bot || true
  mv "$BOT/venv" "$BOT/venv_slim"; mv "$BOT/venv_old" "$BOT/venv"; systemctl start arxiv arxiv_bot
  echo "rolled back: arxiv=$(systemctl is-active arxiv) arxiv_bot=$(systemctl is-active arxiv_bot)"; exit 1
fi
