#!/usr/bin/env python3
"""Patch /root/vpn_bot.py on horek_ge so sherlock.uni.lux and the VPN's DNS servers
are routed through the openconnect tunnel. Run as root, then:
    systemctl restart tsech-vpn-bot.service
A backup is written next to the file."""
import shutil, sys, time
p = "/root/vpn_bot.py"
s = open(p).read()
if "ROUTE_HOSTS" in s:
    print("already patched"); sys.exit(0)
shutil.copy(p, f"{p}.bak-{time.strftime('%Y%m%d')}")
s = s.replace("WAIT_CODE = 1\n",
    "# extra hosts routed through the VPN (monitoring jump host etc.)\n"
    "ROUTE_HOSTS = [\"sherlock.uni.lux\"]\n\nWAIT_CODE = 1\n", 1)
old = """    # Добавить маршруты только для Ollama
    for ip in ips:"""
new = """    # DNS-серверы VPN тоже через туннель, иначе имена uni.lu не резолвятся
    dns = subprocess.run(["resolvectl", "dns", tun], capture_output=True, text=True).stdout
    extra = [t for t in dns.replace(":", " ").split() if t.count(".") == 3 and t[0].isdigit()]
    for host in ROUTE_HOSTS:
        try:
            extra.append(socket.gethostbyname(host))
        except Exception:
            log.warning(f"не удалось резолвить {host}, маршрут не добавлен")
    # Добавить маршруты только для Ollama и доп. хостов
    for ip in list(dict.fromkeys(list(ips) + extra)):"""
assert old in s, "setup_routes block not found; patch manually"
open(p, "w").write(s.replace(old, new, 1))
print("patched; now: systemctl restart tsech-vpn-bot.service")
