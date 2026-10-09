#!/bin/bash
# Run on aak-llm as root:  sudo bash /home/alex/.local/share/videopushbot/stage/install_root.sh
# 1. swaps the 6.2 GB venv (torch, CUDA, nothing the bot uses) for the 356 MB one built from requirements.txt;
# 2. caps glibc malloc arenas (MALLOC_ARENA_MAX=2) so freed memory goes back to the system;
# 3. installs the idle-restart guard. Refuses to restart the bot while it is working on a video.
# The old venv stays as venv_old until you delete it. Rollback: stop, mv venv venv_slim, mv venv_old venv, start.
set -eu -o pipefail
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 1; }
BOT=/home/alex/videopushbot; SVC=notebooklm-bot; ST=/home/alex/.local/share/videopushbot/stage
[ -x "$BOT/venv_slim/bin/python3" ] || { echo "venv_slim is missing"; exit 1; }
runuser -u alex -- "$BOT/venv_slim/bin/python3" -c "import telethon, fitz, edge_tts, googleapiclient, trafilatura, pydub, PIL, bs4, dotenv, google.generativeai, httpx, requests" \
  || { echo "slim venv cannot import the bot's modules"; exit 1; }
cg=$(systemctl show -p ControlGroup --value "$SVC")
if [ "$(wc -l < "/sys/fs/cgroup$cg/cgroup.procs")" -gt 1 ] || [ -n "$(find "$BOT/workspace" -type f -mmin -10 -print -quit)" ]; then
  echo "the bot is working on a video right now; run this again in a few minutes"; exit 1
fi
install -m 755 -o root -g root "$ST/notebooklm_memguard.sh" /usr/local/sbin/notebooklm-memguard
echo '*/30 * * * * root /usr/local/sbin/notebooklm-memguard' > /etc/cron.d/notebooklm-memguard; chmod 644 /etc/cron.d/notebooklm-memguard
install -d /etc/systemd/system/$SVC.service.d
printf '[Service]\nEnvironment=MALLOC_ARENA_MAX=2\nEnvironment=MALLOC_TRIM_THRESHOLD_=131072\nMemoryMax=2G\n' > /etc/systemd/system/$SVC.service.d/memory.conf
systemctl daemon-reload
systemctl stop "$SVC"
mv "$BOT/venv" "$BOT/venv_old"; mv "$BOT/venv_slim" "$BOT/venv"
systemctl start "$SVC"; sleep 25
if systemctl is-active --quiet "$SVC" && ! journalctl -u "$SVC" --since "-30s" --no-pager | grep -qE "Traceback|ModuleNotFoundError|ImportError"; then
  echo "ok: $SVC is $(systemctl is-active "$SVC"), private memory $(awk '/^RssAnon/{print int($2/1024)}' /proc/$(systemctl show -p MainPID --value "$SVC")/status) MB, venv $(du -sm "$BOT/venv" | cut -f1) MB"
  echo "venv_old ($(du -sm "$BOT/venv_old" | cut -f1) MB) is kept for rollback; delete it with: rm -rf $BOT/venv_old"
else
  echo "the bot did not start cleanly; rolling back"; systemctl stop "$SVC" || true
  mv "$BOT/venv" "$BOT/venv_slim"; mv "$BOT/venv_old" "$BOT/venv"; systemctl start "$SVC"; echo "rolled back: $(systemctl is-active "$SVC")"; exit 1
fi
