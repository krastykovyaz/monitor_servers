"""Helpers shared by watch.py and probe.py. Standard library only, runs on Python 3.5+."""
import re
import urllib.error
import urllib.parse
import urllib.request

TG_LIMIT = 3900


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


def dur(sec):
    sec = int(sec)
    if sec < 3600:
        return "%d min" % max(1, sec // 60)
    if sec < 172800:
        return "%.0f h" % (sec / 3600.0)
    return "%.0f d" % (sec / 86400.0)


def chunks(text, limit=TG_LIMIT):
    """Splits a message on line boundaries. A <pre> block cut by a split is closed in one chunk and
    reopened in the next, so every chunk is valid HTML on its own."""
    out, cur, in_pre = [], "", False
    for line in text.split("\n"):
        while len(line) > limit - 20:                      # one enormous line: hard split
            out.append(cur + line[:limit - 20] + ("</pre>" if in_pre else ""))
            cur, line = ("<pre>" if in_pre else ""), line[limit - 20:]
        if cur.strip("<pre>\n") and len(cur) + len(line) + 8 > limit:
            out.append(cur + ("</pre>" if in_pre else ""))
            cur = "<pre>" if in_pre else ""
        cur += line + "\n"
        in_pre = (int(in_pre) + line.count("<pre>") - line.count("</pre>")) > 0
    if cur.strip():
        out.append(cur)
    return out


def tg_send(env, text):
    """Sends text to Telegram, raising on failure so callers can keep the alert for the next run.
    If Telegram rejects the markup (HTTP 400), the chunk is resent as plain text rather than lost."""
    url = "https://api.telegram.org/bot%s/sendMessage" % env["TG_BOT_TOKEN"]
    for ch in chunks(text):
        fields = {"chat_id": env["TG_CHAT_ID"], "text": ch, "parse_mode": "HTML", "disable_web_page_preview": "1"}
        try:
            urllib.request.urlopen(url, urllib.parse.urlencode(fields).encode(), timeout=20).read()
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            fields.pop("parse_mode")
            fields["text"] = re.sub(r"</?(b|pre|i|code)>", "", ch).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            urllib.request.urlopen(url, urllib.parse.urlencode(fields).encode(), timeout=20).read()


def hb_ping(env, name, max_silence=None):
    """Tells the heartbeat receiver that `name` is alive (HB_URL in .env). Never raises."""
    url = env.get("HB_URL")
    if not url:
        return False
    try:
        q = ("?max=%d" % max_silence) if max_silence else ""
        urllib.request.urlopen(url.rstrip("/") + "/" + urllib.parse.quote(name) + q, timeout=10).read()
        return True
    except Exception:
        return False
