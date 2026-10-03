#!/usr/bin/env python3
"""aak-third: ~/.local/bin/grak_snapshot.py BOT_DIR SNAP_DIR
Consistent copies of the bot's SQLite files while it runs (SQLite online backup), written atomically."""
import os, sqlite3, sys

bot, snap = sys.argv[1], sys.argv[2]
files = [(os.path.join(bot, "content_database.db"), "content_database.db"),
         (os.path.join(bot, "session2.session"), "session2.session"),
         ("/home/alex/projects/test_group_dedup.db", "test_group_dedup.db")]
for src, name in files:
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
