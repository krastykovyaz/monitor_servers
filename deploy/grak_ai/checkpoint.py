"""Fold SQLite's write-ahead journal into content_database.db so the main file alone holds every write.
Run in the bot's folder with the bot stopped:  python3 - < checkpoint.py"""
import sqlite3
c = sqlite3.connect("content_database.db")
busy, frames, done = c.execute("pragma wal_checkpoint(TRUNCATE)").fetchone()
c.close()
print("   checkpoint: %d journal frames, %d written to the main file%s" % (frames, done, ", BUSY" if busy else ""))
raise SystemExit(1 if busy else 0)
