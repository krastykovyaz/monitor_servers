#!/usr/bin/env python3
"""aak-llm: ~/.local/bin/videopushbot_sweep.py [--dry]   (cron, daily, as alex)

The video bot deletes a job's files once every upload succeeded, but jobs that never get that far
(unpublished, abandoned, the preview 'sample' extraction) stay forever. This sweeps them:
  - never touches a job that is queued for publishing (deferred_publishes.json) or was written in the last hour;
  - 'sample/' of any job older than 1 day is removed;
  - jobs older than 2 days lose everything except the finished video and its script;
  - jobs older than 14 days are removed completely."""
import json, os, shutil, sys, time

BOT = "/home/alex/videopushbot"
WS = os.path.join(BOT, "workspace")
KEEP = {"output_video.mp4", "script.json"}
SAMPLE_AGE, SLIM_AGE, FINAL_AGE, ACTIVE_WITHIN = 86400, 2 * 86400, 14 * 86400, 3600
LOG = os.path.expanduser("~/.local/share/videopushbot_sweep.log")
dry = "--dry" in sys.argv
now = time.time()


def size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try: total += os.lstat(os.path.join(root, f)).st_size
            except OSError: pass
    return total


def newest(path):
    latest = os.lstat(path).st_mtime
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            try: latest = max(latest, os.lstat(os.path.join(root, n)).st_mtime)
            except OSError: pass
    return latest


def job_time(name, path):
    try: return time.mktime(time.strptime(name[:15], "%Y%m%d_%H%M%S"))
    except ValueError: return os.lstat(path).st_mtime


def remove(path):
    freed = size(path) if os.path.isdir(path) else os.lstat(path).st_size
    if not dry:
        shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.remove(path)
    return freed


def slim(job):
    """Remove everything in a job except the finished video and the script; return bytes freed."""
    freed = 0
    for entry in sorted(os.listdir(job)):
        p = os.path.join(job, entry)
        if os.path.isdir(p) and not os.path.islink(p):
            if entry == "sample": freed += remove(p); continue
            for inner in sorted(os.listdir(p)):
                if inner not in KEEP: freed += remove(os.path.join(p, inner))
        elif entry not in KEEP:
            freed += remove(p)
    return freed


try:
    queued = {str(e.get("base_job_id")) for e in json.load(open(os.path.join(BOT, "deferred_publishes.json")))}
except (OSError, ValueError):
    queued = set()

total, lines = 0, []
for user in sorted(os.listdir(WS)) if os.path.isdir(WS) else []:
    udir = os.path.join(WS, user)
    if not os.path.isdir(udir): continue
    for name in sorted(os.listdir(udir)):
        job = os.path.join(udir, name)
        if not os.path.isdir(job) or name in queued or now - newest(job) < ACTIVE_WITHIN: continue
        age, freed = now - job_time(name, job), 0
        if age > FINAL_AGE: freed = remove(job); what = "removed"
        elif age > SLIM_AGE: freed = slim(job); what = "slimmed (kept video and script)"
        elif age > SAMPLE_AGE and os.path.isdir(os.path.join(job, "sample")): freed = remove(os.path.join(job, "sample")); what = "sample removed"
        else: continue
        if freed:
            total += freed; lines.append("%s %s: %.0f MB freed" % (name, what, freed / 1048576.0))
summary = "%s%s: freed %.0f MB in %d job(s)" % (time.strftime("%F %T"), " (dry run)" if dry else "", total / 1048576.0, len(lines))
print("\n".join(lines + [summary]))
if not dry and lines:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    open(LOG, "a").write("\n".join(lines + [summary]) + "\n")
