#!/usr/bin/env python3
"""Dead man's switch for the watchers.

Every watcher calls  GET /hb/<secret>/<name>  after each run. This service remembers the time of the
last call per name and alerts through Telegram when a name has been silent for too long, and again
when it comes back. A machine cannot report its own death; this notices the silence instead.

  GET /hb/<secret>/<name>       record a heartbeat
  GET /status/<secret>          JSON: seconds since the last heartbeat of every name
  GET /forget/<secret>/<name>   stop expecting a name (after decommissioning a host)

Environment: HB_SECRET, TG_BOT_TOKEN, TG_CHAT_ID, optional HB_PORT (8787), HB_SILENT_MIN (25).
"""
import hmac, html, json, os, re, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ["HB_SECRET"]
TOKEN, CHAT = os.environ["TG_BOT_TOKEN"], os.environ["TG_CHAT_ID"]
PORT = int(os.environ.get("HB_PORT", "8787"))
SILENT_AFTER = int(os.environ.get("HB_SILENT_MIN", "25")) * 60
STATE = os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/monitor-heartbeat"), "beats.json")
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,48}")

lock = threading.Lock()
try:
    beats = json.load(open(STATE))
except Exception:
    beats = {}
pending_new, pending_back, dirty = [], [], [False]


def save():
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(beats, f)
    os.replace(tmp, STATE)


def tg(text):
    try:
        data = urllib.parse.urlencode({"chat_id": CHAT, "text": text, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % TOKEN, data, timeout=20).read()
    except Exception as e:
        print("telegram send failed: %s" % e, flush=True)


def dur(sec):
    sec = int(sec)
    return "%d min" % max(1, sec // 60) if sec < 3600 else ("%.1f h" % (sec / 3600.0) if sec < 172800 else "%.0f d" % (sec / 86400.0))


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="text/plain"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        ok = len(parts) >= 2 and hmac.compare_digest(parts[1].encode(), SECRET.encode())
        now = time.time()
        if ok and parts[0] == "hb" and len(parts) == 3 and NAME_RE.fullmatch(parts[2]):
            name = parts[2]
            with lock:
                b = beats.get(name)
                if b is None:
                    beats[name] = {"first": now, "last": now, "silent": False}
                    pending_new.append(name)
                else:
                    if b.get("silent"):
                        pending_back.append((name, now - b["last"]))
                    b.update(last=now, silent=False)
                beats[name]["ip"] = self.client_address[0]
                dirty[0] = True
            self.reply(200, "ok\n")
        elif ok and parts[0] == "status" and len(parts) == 2:
            with lock:
                self.reply(200, json.dumps({n: {"age_s": int(now - b["last"]), "silent": b.get("silent", False)}
                                            for n, b in sorted(beats.items())}, indent=1) + "\n", "application/json")
        elif ok and parts[0] == "forget" and len(parts) == 3:
            with lock:
                gone = beats.pop(parts[2], None) is not None
                dirty[0] = True
            self.reply(200, "forgotten\n" if gone else "unknown\n")
        else:
            self.reply(404, "not found\n")

    do_POST = do_HEAD = do_GET

    def log_message(self, *a):
        pass


def checker():
    while True:
        time.sleep(60)
        now, lines = time.time(), []
        with lock:
            if pending_new:
                lines.append("💓 <b>Heartbeat registered</b>: " + ", ".join(html.escape(n) for n in sorted(pending_new)))
                del pending_new[:]
            for name, gap in pending_back:
                lines.append("✅ <b>%s</b> is reporting again (silent for %s)" % (html.escape(name), dur(gap)))
            del pending_back[:]
            for name, b in sorted(beats.items()):
                if not b.get("silent") and now - b["last"] > SILENT_AFTER:
                    b["silent"] = True
                    dirty[0] = True
                    lines.append("🔇 <b>%s</b> has gone silent — last heartbeat %s ago. The host, its network or its monitoring is down."
                                 % (html.escape(name), dur(now - b["last"])))
            if dirty[0]:
                try:
                    save()
                    dirty[0] = False
                except Exception as e:
                    print("state save failed: %s" % e, flush=True)
        if lines:
            tg("\n".join(lines))


if __name__ == "__main__":
    threading.Thread(target=checker, daemon=True).start()
    print("heartbeat receiver on port %d, silence threshold %d min, %d names known" % (PORT, SILENT_AFTER // 60, len(beats)), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
