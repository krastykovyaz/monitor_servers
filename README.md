# monitor_servers

Daily server status report to Telegram.

- `check_servers.sh` — probes every host in `servers.conf` over SSH (uptime, load, RAM, disk, docker)
  and every endpoint in `ollama.conf` over HTTP, then posts one message to Telegram.
- `.env` — `TG_BOT_TOKEN`, `TG_CHAT_ID`, optional `SSH_KEY` (copy from `.env.example`, never commit).
- `--dry` prints the report instead of sending it.

Deploy: cron on horek_ge, `0 5 * * *`.

On demand: send `/servers` to the Telegram bot (handler added to `vpn_bot.py` by `deploy/vpn_bot_servers_cmd.py`).
Apply all server-side patches with `deploy/apply_on_horek_ge.sh`.
