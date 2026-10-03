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

Apps run by PM2 are discovered automatically and followed like services. Processes and ports that belong to
no service can be declared per host in `expect.conf`; a missing one alerts.

Besides custom units in `/etc/systemd/system`, it follows nginx, apache2, postgresql, openvpn, wireguard, docker,
redis, mysql/mariadb and mongod when they are installed.

Each condition alerts once when it starts and once when it clears. Conditions already true on the first run
are listed once as "already abnormal" and stay quiet. Thresholds and mutes live in `watch.conf`.

    ./watch.py --show [host]   # the per-service table, on demand
    ./watch.py --dry           # evaluate and print, send nothing, save nothing

    ./watch.py --digest [host]  # send the per-service digest to Telegram; add --dry to print it

The digest also goes out daily at 05:05 from cron. `deploy/vpn_bot_resources_cmd.py` adds a `/resources [host]`
command to the Telegram bot for the same digest on demand.

### Hosts the central watcher cannot reach

A host can watch itself: copy `watch.py`, `watch.conf`, `expect.conf` and a `.env` with the Telegram settings into
`~/monitor_servers`, write a `servers.conf` with the single line `NAME | local | -`, and add the same two cron
entries for that user. aak-first runs this way. `deploy/vscode_server_cleanup.sh` trims `~/.vscode-server`
weekly on hosts where editor builds pile up.

## External checks (`probe.py`)

Checks every target in `probes.conf` over the internet every 5 minutes: HTTP status, TLS certificate validity
and days to expiry, or a plain TCP connect. Alerts after two consecutive failures, on recovery, and when a
certificate has under 14 days left. Targets that resolve to the prober's own address are skipped, so a second
prober on another server covers them: horek_ge probes everything else, horek_fi probes horek_ge
(`probe.py --only-ip 2.26.22.251`).

## Heartbeats (`heartbeat_server.py`)

A small receiver on horek_ge (systemd unit `monitor-heartbeat`, port 8787). Every watcher calls it after each
run (`HB_URL` in its `.env`). A name that stays silent for 25 minutes raises an alert, and another when it
returns. This catches what a self-watching host cannot report: its own death, or its whole network going down.
`/status/<secret>` shows all names; `/forget/<secret>/<name>` retires one.
