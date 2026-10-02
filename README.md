# monitor_servers

Daily server status report to Telegram.

- `check_servers.sh` — probes every host in `servers.conf` over SSH (uptime, load, RAM, disk, docker)
  and every endpoint in `ollama.conf` over HTTP, then posts one message to Telegram.
- `.env` — `TG_BOT_TOKEN` and `TG_CHAT_ID` (copy from `.env.example`, never commit).
- `--dry` prints the report instead of sending it.

Deploy: cron on horek_ge, `0 5 * * *`.
