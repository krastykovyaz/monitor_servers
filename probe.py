#!/usr/bin/env python3
"""External checks: is each site or port reachable from the internet, and is its certificate valid?

Reads probes.conf, checks every target over the network, and alerts through Telegram when a target
fails twice in a row, when it recovers, and when a certificate is close to expiry.

  probe.py                 check, alert, save state      (cron, every 5 min)
  probe.py --dry           check and print, change nothing
  probe.py --status        print the current table
  probe.py --digest        send a one-message summary
  probe.py --only-ip IP    only targets that resolve to this address (for a second vantage point)

Targets that resolve to this machine's own addresses are skipped: a host cannot judge its own
reachability from outside.
"""
import http.client, json, os, socket, ssl, subprocess, sys, time, html
import urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import dur, hb_ping, load_kv, tg_send

DIR = Path(__file__).resolve().parent
STATE_FILE = DIR / "state" / "probe_state.json"
TIMEOUT = 15
CONFIRM = 2            # consecutive failures before alerting
CERT_WARN_DAYS = 14
CERT_REPEAT_H = 72


def load_probes():
    out = []
    for line in (DIR / "probes.conf").read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        p = [x.strip() for x in line.split("|")]
        if len(p) >= 2:
            out.append({"name": p[0], "target": p[1], "expect": p[2] if len(p) > 2 else ""})
    return out


def local_ips():
    ips = set()
    try:
        out = subprocess.run(["hostname", "-I"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=5).stdout
        ips.update(out.split())
    except Exception:
        pass
    return ips


def resolve(host):
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(host, None, socket.AF_INET)})
    except Exception:
        return []


def check(p):
    """Returns dict: ok, detail, ms, cert_days (or None), ip."""
    u = urllib.parse.urlparse(p["target"])
    host, port = u.hostname, u.port
    r = {"ok": False, "detail": "", "ms": None, "cert_days": None, "ip": ",".join(resolve(host)[:2])}
    if not r["ip"]:
        r["detail"] = "DNS does not resolve"
        return r
    t0 = time.time()
    try:
        if u.scheme == "tcp":
            with socket.create_connection((host, port), timeout=TIMEOUT):
                pass
            r.update(ok=True, detail="port open")
        elif u.scheme in ("http", "https"):
            port = port or (443 if u.scheme == "https" else 80)
            if u.scheme == "https":
                conn = http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
            conn.request("GET", u.path or "/", headers={"User-Agent": "monitor-probe/1.0", "Accept": "*/*"})
            if u.scheme == "https":
                cert = conn.sock.getpeercert()
                if cert and cert.get("notAfter"):
                    r["cert_days"] = int((ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400)
            resp = conn.getresponse()
            resp.read(2048)
            conn.close()
            want = [x.strip() for x in p["expect"].split(",") if x.strip()]
            good = (str(resp.status) in want) if want else (resp.status < 500)
            r.update(ok=good, detail="HTTP %d" % resp.status)
        else:
            r["detail"] = "unknown scheme"
    except ssl.SSLCertVerificationError as e:
        r["detail"] = "certificate problem: %s" % (getattr(e, "verify_message", "") or str(e))[:70]
    except socket.timeout:
        r["detail"] = "timeout after %ds" % TIMEOUT
    except ConnectionRefusedError:
        r["detail"] = "connection refused"
    except Exception as e:
        r["detail"] = ("%s: %s" % (type(e).__name__, e))[:90]
    r["ms"] = int((time.time() - t0) * 1000)
    return r


def send(env, text):
    tg_send(env, text)


def main():
    args = sys.argv[1:]
    dry, status, digest = "--dry" in args, "--status" in args, "--digest" in args
    only_ip = args[args.index("--only-ip") + 1] if "--only-ip" in args else None
    env, now = load_kv(DIR / ".env"), time.time()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    checks = state.setdefault("checks", {})
    first_run = not checks
    mine = local_ips()

    probes, skipped = [], []
    for p in load_probes():
        ips = resolve(urllib.parse.urlparse(p["target"]).hostname)
        if only_ip and only_ip not in ips:
            continue
        if not only_ip and ips and set(ips) & mine:
            skipped.append(p["name"])
            continue
        probes.append(p)
    # forget targets that were removed from probes.conf
    names = {p["name"] for p in load_probes()}
    for gone in [n for n in checks if n not in names]:
        checks.pop(gone)
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(zip(probes, ex.map(check, probes)))

    if status or digest:
        up = [(p, r) for p, r in results if r["ok"]]
        down = [(p, r) for p, r in results if not r["ok"]]
        if status:
            for p, r in sorted(results, key=lambda x: (x[1]["ok"], x[0]["name"])):
                print("%-4s %-34s %-22s %5s ms  %-15s %s" % ("up" if r["ok"] else "DOWN", p["name"][:34], r["detail"][:22], r["ms"],
                      r["ip"][:15], ("cert %dd" % r["cert_days"]) if r["cert_days"] is not None else ""))
            print("\n%d up, %d down, %d skipped (this machine's own)" % (len(up), len(down), len(skipped)))
            return
        lines = ["🌐 <b>External checks</b> %s — %d up, %d down" % (time.strftime("%Y-%m-%d %H:%M"), len(up), len(down))]
        for p, r in sorted(down, key=lambda x: x[0]["name"]):
            lines.append("❌ %s — %s" % (html.escape(p["name"]), html.escape(r["detail"])))
        soon = sorted((r["cert_days"], p["name"]) for p, r in results if r["cert_days"] is not None and r["cert_days"] < 30)
        if soon:
            lines.append("\n🔐 certificates expiring within 30 days:")
            lines += ["   %s — %d days" % (html.escape(n), d) for d, n in soon]
        slow = sorted(((r["ms"], p["name"]) for p, r in up if r["ms"] and r["ms"] > 3000), reverse=True)[:5]
        if slow:
            lines.append("\n🐢 slow: " + ", ".join("%s %.1fs" % (html.escape(n), ms / 1000.0) for ms, n in slow))
        text = "\n".join(lines)
        print(text) if dry else send(env, text)
        return

    fired, resolved, known = [], [], []
    for p, r in results:
        c = checks.setdefault(p["name"], {"fails": 0, "down_since": None, "cert_alert": 0})
        N = html.escape(p["name"])
        if r["ok"]:
            if c.get("down_since") and not c.get("known"):
                resolved.append("%s — reachable again (%s, was down %s)" % (N, r["detail"], dur(now - c["down_since"])))
            c.update(fails=0, down_since=None, known=False)
        else:
            c["fails"] = c.get("fails", 0) + 1
            if first_run:
                c.update(down_since=now, known=True)
                known.append("%s — %s" % (N, html.escape(r["detail"])))
            elif c["fails"] >= CONFIRM and not c.get("down_since"):
                c.update(down_since=now, known=False)
                fired.append("%s — %s" % (N, html.escape(r["detail"])))
        d = r["cert_days"]
        if d is not None and d < CERT_WARN_DAYS and now - c.get("cert_alert", 0) > CERT_REPEAT_H * 3600:
            c["cert_alert"] = now
            (known if first_run else fired).append("%s — certificate expires in %d days" % (N, d))
        c["last"] = {"ok": r["ok"], "detail": r["detail"], "ms": r["ms"], "cert_days": d, "t": now}

    parts = []
    if first_run:
        parts.append("🌐 <b>External checks started</b>\n%d targets checked from outside every 5 minutes, %d reachable." % (
            len(results), sum(1 for p, r in results if r["ok"])))
    if known:
        parts.append("<b>Already failing</b> (no repeat alert unless it changes):\n" + "\n".join("• " + t for t in known))
    if fired:
        parts.append("🚨 <b>Not reachable from the internet</b>\n" + "\n".join("• " + t for t in fired))
    if resolved:
        parts.append("✅ <b>Reachable again</b>\n" + "\n".join("• " + t for t in resolved))
    msg = "\n\n".join(parts)
    if dry:
        print(msg or "(nothing to send)")
        print("\n[dry run: %d checked, %d failing, %d skipped as this machine's own]" % (
            len(results), sum(1 for p, r in results if not r["ok"]), len(skipped)))
        return
    if msg:      # deliver first: a failed send must not mark the alert as delivered
        try:
            send(env, msg)
        except Exception as e:
            print("%s alert NOT delivered (%s: %s); state not saved, will retry next run" % (
                time.strftime("%F %T"), type(e).__name__, e))
            sys.exit(1)
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(str(tmp), str(STATE_FILE))
    hb_ping(env, env.get("HB_NAME", socket.gethostname().split(".")[0]) + "-probe")
    print("%s checked=%d failing=%d fired=%d resolved=%d" % (time.strftime("%F %T"), len(results),
          sum(1 for p, r in results if not r["ok"]), len(fired), len(resolved)))


if __name__ == "__main__":
    main()
