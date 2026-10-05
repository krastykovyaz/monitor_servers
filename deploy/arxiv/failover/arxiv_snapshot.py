#!/usr/bin/env python3
"""aak-second: /usr/local/sbin/arxiv-snapshot BOT_DIR SNAP_DIR
Consistent copies of the bots' SQLite files while they run (SQLite online backup), written atomically.
SNAP_DIR is in RAM (/dev/shm), so a 500 MB copy every 15 minutes does not wear the disk."""
import os, sqlite3, sys

bot, snap = sys.argv[1], sys.argv[2]
for name in ("papers.db", "bot.session"):
    src = os.path.join(bot, name)
    if not os.path.exists(src):
        continue
    tmp = os.path.join(snap, "." + name + ".tmp")
    a = sqlite3.connect("file:%s?mode=ro" % src, uri=True, timeout=30)
    b = sqlite3.connect(tmp)
    a.backup(b)
    b.close()
    a.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, os.path.join(snap, name))
