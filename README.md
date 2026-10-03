# monitor_servers

Daily server status report to Telegram.

- `check_servers.sh` — probes every host in `servers.conf` over SSH (uptime, load, RAM, disk, docker)
  and every endpoint in `ollama.conf` over HTTP, then posts one message to Telegram.
- `.env` — `TG_BOT_TOKEN`, `TG_CHAT_ID`, optional `SSH_KEY` (copy from `.env.example`, never commit).
- `--dry` prints the report instead of sending it.

Deploy: cron on horek_ge, `0 5 * * *`.

On demand: send `/servers` to the Telegram bot (handler added to `vpn_bot.py` by `deploy/vpn_bot_servers_cmd.py`).
Apply all server-side patches with `deploy/apply_on_horek_ge.sh`.

## Resource watch (`watch.py`)

Runs every 10 minutes from cron on horek_ge. For every host in `servers.conf` it collects host totals
(disk, RAM, swap, load), every custom systemd service (state, private RAM, restarts, folder size once a day)
and every Docker container (state, RAM, memory limit, OOM kills, restarts). It keeps a 24h history in
`state/watch_state.json` and sends a Telegram alert when:

- a service or container uses 1.5x its own 24h median RAM and at least 100 MB more, or over 35% of host RAM
- a container sits above 90% of its memory limit, gets OOM-killed, or restarts
- an enabled service stops being active, restarts, or a unit enters failed state
- root disk passes 85% or 93%, or grows more than 3 GB in a day; a service folder grows 30% and 300 MB in a day
- available RAM drops under 8%, swap passes 90%, load stays above 2x the CPU count
- a host becomes unreachable or reboots
- a port that was steadily listening on a public interface stops listening

Besides custom units in `/etc/systemd/system`, it follows nginx, apache2, postgresql, openvpn, wireguard, docker,
redis, mysql/mariadb and mongod when they are installed.

Each condition alerts once when it starts and once when it clears. Conditions already true on the first run
are listed once as "already abnormal" and stay quiet. Thresholds and mutes live in `watch.conf`.

    ./watch.py --show [host]   # the per-service table, on demand
    ./watch.py --dry           # evaluate and print, send nothing, save nothing

    ./watch.py --digest [host]  # send the per-service digest to Telegram; add --dry to print it

The digest also goes out daily at 05:05 from cron. `deploy/vpn_bot_resources_cmd.py` adds a `/resources [host]`
command to the Telegram bot for the same digest on demand.
