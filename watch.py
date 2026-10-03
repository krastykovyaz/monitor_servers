#!/usr/bin/env python3
"""Resource watcher.

Collects per-host, per-service and per-container metrics over SSH for every host in
servers.conf, keeps a rolling 24h history, and sends Telegram alerts when something
grows abnormally or changes state.

  watch.py            collect, evaluate, alert, save state   (cron, every 10 min)
  watch.py --dry      collect and evaluate, print what would be sent, change nothing
  watch.py --show [host]   print the current per-service table and exit
  watch.py --digest [host] send the per-service digest to Telegram (add --dry to print it)
"""
import fnmatch, html, json, os, statistics, subprocess, sys, time
import urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DIR = Path(__file__).resolve().parent
STATE_FILE = DIR / "state" / "watch_state.json"

DEFAULTS = {
    "SERVICE_GROW_FACTOR": 1.5,   # service/container RAM above this x its 24h median ...
    "SERVICE_GROW_MIN_MB": 100,   # ... and at least this many MB above it
    "SERVICE_MAX_PCT": 35,        # one service using more than this % of host RAM
    "CONTAINER_LIMIT_PCT": 90,    # container RAM as % of its memory limit
    "DISK_WARN_PCT": 85,
    "DISK_CRIT_PCT": 93,
    "DISK_GROW_GB_24H": 3,        # root disk grew by more than this in ~24h
    "MEM_AVAIL_MIN_PCT": 8,       # available RAM below this % of total
    "SWAP_USED_PCT": 90,
    "LOAD_FACTOR": 2.0,           # load1 above cpus x this
    "DIR_GROW_PCT": 30,           # service folder grew by this % ...
    "DIR_GROW_MIN_MB": 300,       # ... and at least this many MB since the last daily scan
    "EVENT_COOLDOWN_H": 6,        # repeat counter-type alerts (OOM kills, restarts) at most this often
    "UNREACHABLE_CONFIRM": 2,     # consecutive failed checks before "unreachable"
    "MUTE": "",                   # comma-separated fnmatch patterns of alert keys to silence
}

COLLECT = r'''
export LC_ALL=C
mt=$(awk '/^MemTotal:/{print $2}' /proc/meminfo); ma=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
st=$(awk '/^SwapTotal:/{print $2}' /proc/meminfo); sf=$(awk '/^SwapFree:/{print $2}' /proc/meminfo)
set -- $(df -Pk / | awk 'NR==2{print $2, $3}')
echo "H|cpus=$(nproc)|load1=$(cut -d' ' -f1 /proc/loadavg)|disk_total_kb=$1|disk_used_kb=$2|mem_total_kb=$mt|mem_avail_kb=$ma|swap_total_kb=$st|swap_free_kb=$sf|uptime_s=$(cut -d. -f1 /proc/uptime)"
svc() {
  cg=$(systemctl show "$1.service" -p ControlGroup --value 2>/dev/null); an=
  [ -n "$cg" ] && [ -r "/sys/fs/cgroup$cg/memory.stat" ] && an=$(awk '/^(anon|shmem) /{s+=$2} END{print s}' "/sys/fs/cgroup$cg/memory.stat")
  echo "S|$1|$(systemctl show "$1.service" -p ActiveState,SubState,UnitFileState,MemoryCurrent,NRestarts,Type,WorkingDirectory 2>/dev/null | paste -sd'|' -)|Anon=$an"
}
# well-known package services, when installed
for u in $(systemctl list-units --type=service --all --no-legend --plain 'nginx.service' 'apache2.service' 'postgresql@*.service' 'openvpn-server@*.service' 'openvpn@*.service' 'wg-quick@*.service' 'docker.service' 'redis-server.service' 'mysql.service' 'mariadb.service' 'mongod.service' 2>/dev/null | awk '{print $1}'); do
  u=${u%.service}
  [ -f "/etc/systemd/system/$u.service" ] && [ ! -L "/etc/systemd/system/$u.service" ] && continue
  svc "$u"
done
# listeners reachable from outside (loopback-only ones are ignored)
echo "L|ports=$( { ss -tlnH 2>/dev/null | awk '{print $4}' | grep -vE '^(127\.|\[::1\])' | sed 's/.*://' | sort -un; ss -ulnH 2>/dev/null | awk '{print $4}' | grep -vE '^(127\.|\[::1\])|%' | sed 's/.*://' | sort -un | awk '$1<32768{print $1"/udp"}'; } | paste -sd, -)"
for f in /etc/systemd/system/*.service; do
  [ -f "$f" ] && [ ! -L "$f" ] || continue
  u=$(basename "$f" .service)
  case "$u" in *@|snap.*|cloud-*|ssh|sshd*|dbus-*|qemu-guest*|do-agent*|droplet-agent*) continue;; esac
  svc "$u"
  if [ "${DU:-0}" = 1 ]; then
    wd=$(systemctl show "$u.service" -p WorkingDirectory --value 2>/dev/null)
    case "$wd" in ""|/|/root|/root/|/home/*/*) ;; /home/*) wd="";; esac
    case "$wd" in ""|/|/root|/root/) ;; *) [ -d "$wd" ] && echo "D|$u|kb=$(timeout 60 du -sxk "$wd" 2>/dev/null | cut -f1)";; esac
  fi
done
systemctl --failed --no-legend --plain 2>/dev/null | awk 'NF{print "F|"$1}'
if command -v docker >/dev/null 2>&1 && docker ps >/dev/null 2>&1; then
  for id in $(docker ps -aq --no-trunc); do
    i=$(docker inspect -f '{{.Name}}|state={{.State.Status}}|restarts={{.RestartCount}}|limit={{.HostConfig.Memory}}' "$id" 2>/dev/null) || continue
    cur=; oom=; an=
    for p in /sys/fs/cgroup/system.slice/docker-$id.scope /sys/fs/cgroup/docker/$id; do
      if [ -r "$p/memory.current" ]; then cur=$(cat "$p/memory.current"); oom=$(awk '/^oom_kill /{print $2}' "$p/memory.events" 2>/dev/null); an=$(awk '/^(anon|shmem) /{s+=$2} END{print s}' "$p/memory.stat" 2>/dev/null); break; fi
    done
    echo "C|${i#/}|mem=$cur|oom_kill=$oom|anon=$an"
  done
fi
echo "END"
'''


def load_kv(path):
    d = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def load_cfg():
    cfg = dict(DEFAULTS)
    for k, v in load_kv(DIR / "watch.conf").items():
        if k in cfg:
            cfg[k] = v if k == "MUTE" else float(v)
    return cfg


def load_servers():
    out = []
    for line in (DIR / "servers.conf").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = [x.strip() for x in line.split("|")]
        out.append((p[0], p[1], p[2] if len(p) > 2 and p[2] else "22"))
    return out


def num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def svc_mem(f):
    """Private memory of a service: anonymous memory from its cgroup, else MemoryCurrent."""
    a, m = num(f.get("Anon")), num(f.get("MemoryCurrent"))
    if m is not None and m >= 2 ** 60:   # systemd's "not set" sentinel
        m = None
    return a if a is not None else m


def kv(parts):
    d = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            d[k] = v
    return d


def parse(out):
    data = {"host": {}, "services": {}, "containers": {}, "failed": [], "du": {}, "ports": []}
    complete = False
    for line in out.splitlines():
        parts = line.strip().split("|")
        t = parts[0]
        if t == "END":
            complete = True
        elif t == "H":
            data["host"] = kv(parts[1:])
        elif t == "S" and len(parts) > 2:
            data["services"][parts[1]] = kv(parts[2:])
        elif t == "C" and len(parts) > 2:
            data["containers"][parts[1]] = kv(parts[2:])
        elif t == "L":
            data["ports"] = [x for x in kv(parts[1:]).get("ports", "").split(",") if x]
        elif t == "F" and len(parts) > 1:
            data["failed"].append(parts[1])
        elif t == "D" and len(parts) > 2:
            data["du"][parts[1]] = kv(parts[2:])
    return data if complete and data["host"] else None


def collect(server, env, with_du):
    name, target, port = server
    cmd = ["ssh", "-p", port, "-o", "IPQoS=none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR", "-o", "ClearAllForwardings=yes"]
    if env.get("SSH_KEY"):
        cmd += ["-i", env["SSH_KEY"]]
    cmd += [target, ("DU=1 " if with_du else "") + "bash -s"]
    try:
        r = subprocess.run(cmd, input=COLLECT, capture_output=True, text=True, timeout=300 if with_du else 90)
        return name, parse(r.stdout)
    except Exception:
        return name, None


def mb(b):
    return "-" if b is None else "%d MB" % round(b / 1048576)


def dur(sec):
    sec = int(sec)
    if sec < 3600:
        return "%d min" % max(1, sec // 60)
    if sec < 172800:
        return "%.0f h" % (sec / 3600)
    return "%.0f d" % (sec / 86400)


class Watch:
    def __init__(self, state, cfg, now):
        self.s, self.cfg, self.now = state, cfg, now
        for k in ("active", "pending", "cooldown", "hist", "seen", "prev", "fails", "ports"):
            self.s.setdefault(k, {})
        self.mutes = [m.strip() for m in str(cfg["MUTE"]).split(",") if m.strip()]
        self.fired, self.resolved, self.known = [], [], []

    def muted(self, key):
        return any(fnmatch.fnmatch(key, p) for p in self.mutes)

    def cond(self, key, bad, text, confirm=1, baseline=False, clear=None, ok="", unconfirm=1):
        """State-type alert: fires once when it starts, reports once when it clears."""
        active = self.s["active"]
        if bad:
            self.s["pending"].pop(key + "#ok", None)
            if key in active:
                active[key]["text"] = text
                return
            if baseline:
                active[key] = {"since": self.now, "text": text, "known": True}
                self.known.append(text)
                return
            n = self.s["pending"].get(key, 0) + 1
            self.s["pending"][key] = n
            if n >= confirm:
                self.s["pending"].pop(key, None)
                active[key] = {"since": self.now, "text": text}
                if not self.muted(key):
                    self.fired.append(text)
        else:
            self.s["pending"].pop(key, None)
            if key in active and (clear is None or clear):
                n = self.s["pending"].get(key + "#ok", 0) + 1
                if n < unconfirm:
                    self.s["pending"][key + "#ok"] = n
                    return
                self.s["pending"].pop(key + "#ok", None)
                a = active.pop(key)
                if not self.muted(key):
                    self.resolved.append("%s%s (was abnormal for %s)" % (
                        a["text"].split(" — ")[0], (" — now " + ok) if ok else "", dur(self.now - a["since"])))

    def drop(self, prefix, keep):
        """Forget alerts for items that no longer exist (removed service or container)."""
        for key in [k for k in self.s["active"] if k.startswith(prefix) and k[len(prefix):].split(":")[0] not in keep]:
            self.s["active"].pop(key, None)

    def event(self, key, text, baseline=False, cooldown_h=None):
        """Counter-type alert: repeats at most once per cooldown."""
        if baseline:
            return
        cd = (self.cfg["EVENT_COOLDOWN_H"] if cooldown_h is None else cooldown_h) * 3600
        if self.now - self.s["cooldown"].get(key, 0) >= cd:
            self.s["cooldown"][key] = self.now
            if not self.muted(key):
                self.fired.append(text)

    def hist_add(self, key, val):
        h = self.s["hist"].setdefault(key, [])
        h.append([self.now, val])
        cut = self.now - 26 * 3600
        while h and h[0][0] < cut:
            h.pop(0)

    def hist_median(self, key):
        vals = [v for t, v in self.s["hist"].get(key, []) if self.now - 24 * 3600 <= t <= self.now - 50 * 60]
        return statistics.median(vals) if len(vals) >= 5 else None

    def hist_old(self, key, min_age_h):
        old = [v for t, v in self.s["hist"].get(key, []) if self.now - t >= min_age_h * 3600]
        return old[0] if old else None

    def growth(self, key, label, cur, host_ram, baseline):
        """RAM growth against the item's own 24h median, plus an absolute ceiling."""
        c = self.cfg
        base = self.hist_median(key)
        self.hist_add(key, cur)
        if base:
            over = cur > base * c["SERVICE_GROW_FACTOR"] and cur - base > c["SERVICE_GROW_MIN_MB"] * 1048576
            self.cond(key + ":grow", over,
                      "%s RAM — %s, usual %s (+%d%%)" % (label, mb(cur), mb(base), round((cur / base - 1) * 100)),
                      confirm=2, baseline=baseline, clear=cur < base * 1.2, ok=mb(cur))
        if host_ram:
            pct = cur * 100.0 / host_ram
            self.cond(key + ":max", pct > c["SERVICE_MAX_PCT"],
                      "%s RAM — %s, %d%% of the host's memory" % (label, mb(cur), round(pct)),
                      confirm=2, baseline=baseline, ok=mb(cur))

    def unreachable(self, name):
        if name not in self.s["seen"]:
            return
        n = self.s["fails"].get(name, 0) + 1
        self.s["fails"][name] = n
        key = "%s:unreachable" % name
        if n >= self.cfg["UNREACHABLE_CONFIRM"] and key not in self.s["active"]:
            self.s["active"][key] = {"since": self.now, "text": "<b>%s</b> unreachable" % name}
            if not self.muted(key):
                self.fired.append("<b>%s</b> unreachable — no SSH answer for %d checks" % (name, n))

    def host(self, name, d):
        c, s = self.cfg, self.s
        baseline = name not in s["seen"]
        prev = s["prev"].get(name, {})
        B = "<b>%s</b>" % html.escape(name)
        key = "%s:unreachable" % name
        s["fails"][name] = 0
        if key in s["active"]:
            a = s["active"].pop(key)
            self.resolved.append("%s reachable again (was down for %s)" % (B, dur(self.now - a["since"])))

        h = {k: (float(v) if k == "load1" else num(v)) for k, v in d["host"].items()}
        ram = (h.get("mem_total_kb") or 0) * 1024
        # reboot
        if prev.get("uptime_s") is not None and h.get("uptime_s") is not None and h["uptime_s"] < prev["uptime_s"]:
            self.event("%s:reboot" % name, "%s rebooted — up %s" % (B, dur(h["uptime_s"])), cooldown_h=0)
        # disk
        if h.get("disk_total_kb"):
            pct = h["disk_used_kb"] * 100.0 / h["disk_total_kb"]
            free_gb = (h["disk_total_kb"] - h["disk_used_kb"]) / 1048576.0
            txt = "%s disk — %d%% used, %.1f GB free" % (B, round(pct), free_gb)
            self.cond("%s:disk:warn" % name, pct >= c["DISK_WARN_PCT"], txt, baseline=baseline, clear=pct < c["DISK_WARN_PCT"] - 2, ok="%d%%" % round(pct))
            self.cond("%s:disk:crit" % name, pct >= c["DISK_CRIT_PCT"], "🔥 " + txt, baseline=baseline, ok="%d%%" % round(pct))
            old = self.hist_old("%s:disk" % name, 20)
            self.hist_add("%s:disk" % name, h["disk_used_kb"])
            if old is not None and (h["disk_used_kb"] - old) / 1048576.0 > c["DISK_GROW_GB_24H"]:
                self.event("%s:disk:grow" % name, "%s disk grew %.1f GB in the last day — now %d%% used" % (
                    B, (h["disk_used_kb"] - old) / 1048576.0, round(pct)), baseline=baseline, cooldown_h=12)
        # memory, swap, load
        if h.get("mem_total_kb"):
            ap = h["mem_avail_kb"] * 100.0 / h["mem_total_kb"]
            self.cond("%s:mem" % name, ap < c["MEM_AVAIL_MIN_PCT"],
                      "%s memory — only %s available (%d%%)" % (B, mb(h["mem_avail_kb"] * 1024), round(ap)),
                      confirm=2, baseline=baseline, clear=ap > c["MEM_AVAIL_MIN_PCT"] + 4, ok=mb(h["mem_avail_kb"] * 1024) + " available")
        if h.get("swap_total_kb"):
            sp = (h["swap_total_kb"] - h["swap_free_kb"]) * 100.0 / h["swap_total_kb"]
            self.cond("%s:swap" % name, sp >= c["SWAP_USED_PCT"], "%s swap — %d%% used" % (B, round(sp)),
                      confirm=2, baseline=baseline, clear=sp < c["SWAP_USED_PCT"] - 15, ok="%d%% used" % round(sp))
        if h.get("cpus") and h.get("load1") is not None:
            self.cond("%s:load" % name, h["load1"] > h["cpus"] * c["LOAD_FACTOR"],
                      "%s load — %.2f on %d cpu" % (B, h["load1"], h["cpus"]), confirm=3, baseline=baseline, ok="%.2f" % h["load1"])

        # services
        pserv = prev.get("services", {})
        for u, f in d["services"].items():
            U = "%s · %s" % (B, html.escape(u))
            k = "%s:svc:%s" % (name, u)
            expected = f.get("UnitFileState", "").startswith("enabled") and f.get("Type") != "oneshot"
            st = f.get("ActiveState", "?")
            self.cond(k + ":state", expected and st != "active",
                      "%s — service is %s/%s" % (U, st, f.get("SubState", "?")), confirm=2, baseline=baseline, ok="active", unconfirm=2)
            r, pr = num(f.get("NRestarts")), pserv.get(u, {}).get("restarts")
            if r is not None and pr is not None and r > pr and (k + ":state") not in s["active"]:
                self.event(k + ":restart", "%s — restarted %d time(s) since the last check, %d total" % (U, r - pr, r), baseline=baseline)
            m = svc_mem(f)
            if m is not None and st == "active":
                self.growth(k + ":mem", U, m, ram, baseline)
        self.drop("%s:svc:" % name, set(d["services"]))

        # failed units
        covered = set(x + ".service" for x in d["services"])   # already reported through the service state
        newf = set(d["failed"]) - set(prev.get("failed", [])) - covered
        for u in sorted(newf):
            if baseline:
                self.known.append("%s · %s — unit failed" % (B, html.escape(u)))
            elif not self.muted("%s:failed:%s" % (name, u)):
                self.fired.append("%s · %s — unit entered failed state" % (B, html.escape(u)))

        # containers
        pcont = prev.get("containers", {})
        for cn, f in d["containers"].items():
            Cn = "%s · container %s" % (B, html.escape(cn))
            k = "%s:ctr:%s" % (name, cn)
            st, p = f.get("state", "?"), pcont.get(cn, {})
            # a restart loop is a lasting condition; a container that stops is reported once
            self.cond(k + ":state", st in ("restarting", "dead"), "%s — is %s" % (Cn, st), confirm=2,
                      baseline=baseline, ok=st, unconfirm=2)
            if p.get("state") == "running" and st not in ("running", "restarting", "dead"):
                self.event(k + ":stopped", "%s — stopped (%s)" % (Cn, st), baseline=baseline, cooldown_h=0)
            m, lim = num(f.get("mem")), num(f.get("limit"))
            if st == "running" and m is not None:
                if lim:
                    pct = m * 100.0 / lim
                    self.cond(k + ":limit", pct >= c["CONTAINER_LIMIT_PCT"],
                              "%s RAM — %s of its %s limit (%d%%)" % (Cn, mb(m), mb(lim), round(pct)),
                              confirm=2, baseline=baseline, clear=pct < c["CONTAINER_LIMIT_PCT"] - 10, ok=mb(m))
                a = num(f.get("anon"))
                self.growth(k + ":mem", Cn, a if a is not None else m, ram, baseline)
            o, po = num(f.get("oom_kill")), p.get("oom_kill")
            if o is not None and po is not None and o > po:
                self.event(k + ":oom", "%s — %d out-of-memory kill(s) since the last check, %d total" % (Cn, o - po, o), baseline=baseline)
            r, pr = num(f.get("restarts")), p.get("restarts")
            if r is not None and pr is not None and r > pr and (k + ":state") not in s["active"]:
                self.event(k + ":restart", "%s — restarted %d time(s), %d total" % (Cn, r - pr, r), baseline=baseline)
        self.drop("%s:ctr:" % name, set(d["containers"]))

        # listening ports: one that was steadily open disappears
        first = name not in s["ports"]
        pc, cur = s["ports"].setdefault(name, {}), set(d["ports"])
        for p in cur:
            pc[p] = 6 if first else min(pc.get(p, 0) + 1, 6)
        for p in list(pc):
            k = "%s:port:%s" % (name, p)
            if p in cur:
                self.cond(k, False, "", ok="listening again")
            elif pc[p] >= 6:
                a = s["active"].get(k)
                if a and self.now - a["since"] > 24 * 3600:      # gone for a day: accept it as removed
                    s["active"].pop(k, None); pc.pop(p, None)
                else:
                    self.cond(k, True, "%s · port %s — stopped listening" % (B, p), confirm=2)
            else:
                pc.pop(p, None)

        # daily folder sizes
        for u, f in d["du"].items():
            kb = num(f.get("kb"))
            if kb is None:
                continue
            k = "%s:du:%s" % (name, u)
            old = s["prev"].get(name, {}).get("du", {}).get(u)
            if old and kb > old * (1 + c["DIR_GROW_PCT"] / 100.0) and (kb - old) / 1024.0 > c["DIR_GROW_MIN_MB"]:
                self.event(k, "%s · %s folder grew %s since the last daily scan — now %s" % (
                    B, html.escape(u), mb((kb - old) * 1024), mb(kb * 1024)), cooldown_h=20)

        du = dict(prev.get("du", {}))
        du.update({u: num(f.get("kb")) for u, f in d["du"].items() if num(f.get("kb")) is not None})
        s["prev"][name] = {
            "uptime_s": h.get("uptime_s"), "failed": d["failed"], "du": du,
            "services": {u: {"restarts": num(f.get("NRestarts"))} for u, f in d["services"].items()},
            "containers": {cn: {"state": f.get("state"), "oom_kill": num(f.get("oom_kill")), "restarts": num(f.get("restarts"))}
                           for cn, f in d["containers"].items()},
        }
        s["seen"][name] = self.now


def show(results, only=None):
    for name, d in results:
        if only and name != only:
            continue
        if not d:
            print("\n== %s: unreachable" % name)
            continue
        h = d["host"]
        g = lambda k: num(h.get(k)) or 0
        print("\n== %s: disk %d%% (%.1f GB free) · ram %s available of %s · swap %s of %s used · load %s on %s cpu" % (
            name, round(g("disk_used_kb") * 100.0 / max(1, g("disk_total_kb"))), (g("disk_total_kb") - g("disk_used_kb")) / 1048576.0,
            mb(g("mem_avail_kb") * 1024), mb(g("mem_total_kb") * 1024),
            mb((g("swap_total_kb") - g("swap_free_kb")) * 1024), mb(g("swap_total_kb") * 1024), h.get("load1"), h.get("cpus")))
        for u, f in sorted(d["services"].items()):
            du = d["du"].get(u, {}).get("kb")
            print("  %-28s %-10s %-9s ram %-8s restarts %-3s%s" % (
                u, f.get("ActiveState", "?"), f.get("UnitFileState", "?"), mb(svc_mem(f)),
                f.get("NRestarts", "-"), ("  disk %s" % mb(int(du) * 1024)) if du else ""))
        for cn, f in sorted(d["containers"].items()):
            lim, a = num(f.get("limit")), num(f.get("anon"))
            print("  [container] %-24s %-10s ram %s (%s with cache)%s  oom kills %s  restarts %s" % (
                cn, f.get("state"), mb(a), mb(num(f.get("mem"))), (" of %s limit" % mb(lim)) if lim else "", f.get("oom_kill") or "-", f.get("restarts")))
        if d["failed"]:
            print("  failed units: " + ", ".join(d["failed"]))
        if d["ports"]:
            print("  listening: " + ", ".join(sorted(d["ports"], key=lambda x: (int(x.split("/")[0]), x))))


def digest(results, state, now, only=None):
    """The per-service picture we used to collect by hand, as one Telegram message."""
    hist, active = state.get("hist", {}), state.get("active", {})

    def trend(key, cur):
        old = [v for t, v in hist.get(key, []) if now - t >= 6 * 3600]
        if not old or not old[0]:
            return ""
        d = cur - old[0]
        if abs(d) < 20 * 1048576 or abs(d) < old[0] * 0.2:
            return ""
        return "  %s%+d%% in %dh" % ("▲" if d > 0 else "▼", round(d * 100.0 / old[0]), round((now - hist[key][0][0]) / 3600))

    out, missing = ["📊 <b>Resource digest</b> " + time.strftime("%Y-%m-%d %H:%M", time.localtime(now))], []
    for name, d in results:
        if only and name != only:
            continue
        if not d:
            missing.append(name)
            continue
        h = d["host"]
        g = lambda k: num(h.get(k)) or 0
        swap = ("%d%%" % round((g("swap_total_kb") - g("swap_free_kb")) * 100.0 / g("swap_total_kb"))) if g("swap_total_kb") else "none"
        out.append("\n<b>%s</b> — disk %d%% (%.1f GB free) · RAM %.1f of %.1f GB free · swap %s · load %s on %s cpu" % (
            html.escape(name), round(g("disk_used_kb") * 100.0 / max(1, g("disk_total_kb"))),
            (g("disk_total_kb") - g("disk_used_kb")) / 1048576.0, g("mem_avail_kb") / 1048576.0, g("mem_total_kb") / 1048576.0,
            swap, h.get("load1"), h.get("cpus")))
        rows, down = [], []
        for u, f in d["services"].items():
            m = svc_mem(f)
            if f.get("ActiveState") == "active" and m is not None:
                rows.append((m, u, trend("%s:svc:%s:mem" % (name, u), m)))
            elif f.get("UnitFileState", "").startswith("enabled") and f.get("Type") != "oneshot" and f.get("ActiveState") != "active":
                down.append("%s (%s)" % (u, f.get("SubState", f.get("ActiveState", "?"))))
        for cn, f in d["containers"].items():
            if f.get("state") == "running":
                a, lim = num(f.get("anon")), num(f.get("limit"))
                m = a if a is not None else (num(f.get("mem")) or 0)
                extra = (" of %s" % mb(lim) if lim else "") + ("  oom kills %s" % f["oom_kill"] if num(f.get("oom_kill")) else "")
                rows.append((m, "▣ " + cn, extra + trend("%s:ctr:%s:mem" % (name, cn), m)))
            elif f.get("state") in ("restarting", "dead"):
                down.append("container %s (%s)" % (cn, f.get("state")))
        rows.sort(reverse=True)
        if rows:
            lines = ["%-24s %7s%s" % (html.escape(u[:24]), mb(m), html.escape(x)) for m, u, x in rows[:10]]
            if len(rows) > 10:
                lines.append("… %d more, %s together" % (len(rows) - 10, mb(sum(r[0] for r in rows[10:]))))
            out.append("<pre>" + "\n".join(lines) + "</pre>")
        if down:
            out.append("⛔ not running: " + html.escape(", ".join(down)))
        if d["failed"]:
            extra_failed = [u for u in d["failed"] if u[:-8] not in d["services"]]
            if extra_failed:
                out.append("⛔ failed units: " + html.escape(", ".join(extra_failed)))
        if d["ports"]:
            out.append("ports: " + ", ".join(sorted(d["ports"], key=lambda x: (int(x.split("/")[0]), x))))
        fresh = [a["text"] for k, a in active.items() if k.startswith(name + ":") and not a.get("known")]
        for t in fresh:
            out.append("🚨 " + t)
    if missing:
        out.append("\nNot reachable from here: " + ", ".join(missing))
    return "\n".join(out)


def send(env, text):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for ch in chunks:
        data = urllib.parse.urlencode({"chat_id": env["TG_CHAT_ID"], "text": ch, "parse_mode": "HTML",
                                       "disable_web_page_preview": "1"}).encode()
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % env["TG_BOT_TOKEN"], data, timeout=20).read()


def main():
    args = sys.argv[1:]
    dry, do_show, do_digest = "--dry" in args, "--show" in args, "--digest" in args
    only = next((a for a in args if not a.startswith("--")), None)
    env, cfg, now = load_kv(DIR / ".env"), load_cfg(), time.time()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    first_run = not state.get("seen")
    with_du = do_show or (not do_digest and now - state.get("last_du", 0) > 20 * 3600)
    servers = load_servers()
    with ThreadPoolExecutor(max_workers=len(servers)) as ex:
        results = list(ex.map(lambda s: collect(s, env, with_du), servers))
    if do_show:
        show(results, only)
        return
    if do_digest:
        text = digest(results, state, now, only)
        if dry:
            print(text)
        else:
            send(env, text)
            print("%s digest sent" % time.strftime("%F %T"))
        return

    w = Watch(state, cfg, now)
    never = []
    for name, d in results:
        if d:
            w.host(name, d)
        elif name in state.get("seen", {}):
            w.unreachable(name)
        else:
            never.append(name)
    if with_du:
        state["last_du"] = now

    parts = []
    if first_run:
        ok = [n for n, d in results if d]
        parts.append("👀 <b>Resource watch started</b>\nWatching %d host(s): %s\n%d services, %d containers. Checks run every 10 minutes." % (
            len(ok), ", ".join(ok), sum(len(d["services"]) for n, d in results if d), sum(len(d["containers"]) for n, d in results if d)))
        if never:
            parts.append("Not reachable from here, not watched yet: " + ", ".join(never))
    if w.known:
        parts.append("<b>Already abnormal</b> (no repeat alert unless it changes):\n" + "\n".join("• " + t for t in w.known))
    if w.fired:
        parts.append("🚨 <b>Alert</b>\n" + "\n".join("• " + t for t in w.fired))
    if w.resolved:
        parts.append("✅ <b>Resolved</b>\n" + "\n".join("• " + t for t in w.resolved))
    msg = "\n\n".join(parts)

    if dry:
        print(msg or "(nothing to send)")
        print("\n[dry run: %d reachable, %d active conditions, state not saved]" % (sum(1 for n, d in results if d), len(state["active"])))
        return
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE_FILE)
    if msg:
        send(env, msg)
    print("%s reachable=%d fired=%d resolved=%d active=%d" % (
        time.strftime("%F %T"), sum(1 for n, d in results if d), len(w.fired), len(w.resolved), len(state["active"])))


if __name__ == "__main__":
    main()
