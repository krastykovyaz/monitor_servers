#!/usr/bin/env python3
"""Move the bot token out of /root/vpn_bot.py into /root/.env, which the service already loads
(EnvironmentFile=/root/.env). Also makes /root/.env readable by root only.
Run on horek_ge as root, then:  systemctl restart tsech-vpn-bot"""
import os, re, shutil, sys, time

SRC, ENV = "/root/vpn_bot.py", "/root/.env"
s = open(SRC).read()
m = re.search(r'^BOT_TOKEN\s*=\s*"([0-9]+:[A-Za-z0-9_-]+)"\s*$', s, re.M)
if not m:
    print("no hardcoded token found; nothing to do")
    sys.exit(0)
shutil.copy2(SRC, SRC + ".bak-" + time.strftime("%Y%m%d-%H%M"))
env = open(ENV).read() if os.path.exists(ENV) else ""
if not re.search(r"^BOT_TOKEN=", env, re.M):
    env = env.rstrip("\n") + "\nBOT_TOKEN=" + m.group(1) + "\n"
    open(ENV, "w").write(env)
os.chmod(ENV, 0o600)
s = s.replace(m.group(0), 'BOT_TOKEN  = os.environ["BOT_TOKEN"]', 1)
open(SRC, "w").write(s)
print("token moved to %s (mode 600); vpn_bot.py now reads it from the environment" % ENV)
