#!/bin/bash
# Apply all bot patches on horek_ge and restart the bot. Run as root on the server.
set -e
cd "$(dirname "$0")"
python3 vpn_bot_routes.py
python3 vpn_bot_servers_cmd.py
python3 vpn_bot_resources_cmd.py
python3 -m py_compile /root/vpn_bot.py
systemctl restart tsech-vpn-bot.service
sleep 3
systemctl is-active tsech-vpn-bot.service && echo "bot restarted OK"
