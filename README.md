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

A small receiver on horek_ge (systemd unit `monitor-heartbeat` on 127.0.0.1:8787, published by nginx as https://tsech.online:8443; 8443 is one of the few ports the university firewall lets out). Every watcher calls it after each
run (`HB_URL` in its `.env`). A name that stays silent for 25 minutes raises an alert, and another when it
returns. This catches what a self-watching host cannot report: its own death, or its whole network going down.
`/status/<secret>` shows all names; `/forget/<secret>/<name>` retires one.

## grak_ai failover (`deploy/grak_ai/failover/`)

grak_ai runs in the lab (aak-third, tmux `news`). horek_ge holds a standby copy that starts automatically if
the lab goes silent, and never while the lab copy may still be running.

- **Lab guard** (`grak_guard.sh`, cron every minute as alex): renews a lease on horek_ge through a key that can
  only run `/usr/local/sbin/grak-gate` (renew, status, write-only snapshot upload), from 158.64.79.0/24. Every
  5 minutes it uploads consistent SQLite backups of the content database, the Telegram session and the shared
  dedup registry. It restarts the lab bot if it died. If it cannot renew for 5 minutes it stops the lab bot,
  unless horek_ge is completely offline while Telegram is reachable (then nobody can take over).
- **horek_ge watchdog** (`grak-failover`, systemd timer every minute): takes over only when the lease is over
  10 minutes old on 3 consecutive checks, aak-third's monitoring heartbeat has been silent for 15 minutes, and
  horek_ge itself has internet and 15 minutes of uptime. It records `owner=horek_ge` before starting the bot,
  so the lab guard then keeps the lab copy stopped. Alerts go to Telegram.
- **Switching back** is deliberate: `bash deploy/grak_ai/failover/failback.sh` from the Mac.
- **Maintenance in the lab:** `touch ~/.local/share/grak_guard/paused` stops the guard from restarting the bot.
- State: `/var/lib/grak-failover/{owner,lease,standby/,failover.log}` on horek_ge, `~/.local/share/grak_guard/` on aak-third.

Known limit: if the cron daemon on aak-third stops while the VM and the bot keep running, both the lease and
the heartbeat stop, and horek_ge would start a second copy.

## arxiv failover (`deploy/arxiv/failover/`)

The arxiv bots (`arxiv.service` posts papers, `arxiv_bot.service` answers questions) run in the lab on aak-second
as root systemd units. horek_fi (`/opt/arxiv_bot`, user `arxiv`, units in `deploy/arxiv/`) holds a standby copy that
starts automatically if the lab goes silent. It uses the same lease design as grak_ai, with these differences:

- The lab guard (`arxiv_guard.sh`) runs as **root** from `/etc/cron.d/arxiv-guard`, because it has to stop and
  start the root services. It is installed once with `sudo bash ~/.local/share/arxiv_guard/stage/install_lab.sh`.
  It restarts the bots only after it stopped them itself, or when `failback.sh` asks for it (`start_requested`).
- The 520 MB `papers.db` and `bot.session` are copied every 15 minutes: SQLite online backup into `/dev/shm`
  (about 2 s, no disk wear), then `rsync` delta upload. horek_fi checks the snapshot with `pragma quick_check`
  before it installs it. A paper posted in the last 15 minutes before a failover may be posted again.
- The witness is aak-second's monitoring heartbeat, read from the public receiver; it reports every 10 minutes,
  so takeover needs 20 minutes of silence on top of the 10 minute lease.
- After a reboot of the lab, `arxiv-guard-precheck` (an `ExecStartPre` drop-in) refuses to start the bots while
  horek_fi owns them.
- Switching back: `bash deploy/arxiv/failover/failback.sh`. Hold the guard: `touch ~/.local/share/arxiv_guard/paused`.
- State: `/var/lib/arxiv-failover/{owner,lease,standby/,failover.log}` on horek_fi, `/var/lib/arxiv-guard/` on aak-second.
