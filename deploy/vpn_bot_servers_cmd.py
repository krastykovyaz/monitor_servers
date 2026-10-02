#!/usr/bin/env python3
"""Add a /servers command to /root/vpn_bot.py on horek_ge: runs
/root/monitor_servers/check_servers.sh --dry and replies with the report.
Idempotent. Run as root, then: systemctl restart tsech-vpn-bot.service"""
import shutil, sys, time
p = "/root/vpn_bot.py"
s = open(p).read()
if "cmd_servers" in s:
    print("already patched"); sys.exit(0)
shutil.copy(p, f"{p}.bak-{time.strftime('%Y%m%d-%H%M')}")

handler = '''
MONITOR_SCRIPT = "/root/monitor_servers/check_servers.sh"


async def cmd_servers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Отчёт по всем серверам по запросу (тот же, что приходит в 5 утра)."""
    if not is_owner(update):
        return
    wait_msg = await update.message.reply_text("⏳ Опрашиваю серверы, ~30 сек...")
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", MONITOR_SCRIPT, "--dry",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=240)
        text = out.decode(errors="replace").strip() or "пустой ответ"
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка: {e}")
        return
    chunks, cur = [], ""
    for line in text.split("\\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur); cur = ""
        cur += line + "\\n"
    chunks.append(cur)
    for i, c in enumerate(chunks):
        try:
            if i == 0:
                await wait_msg.edit_text(c, parse_mode="HTML")
            else:
                await update.message.reply_text(c, parse_mode="HTML")
        except Exception:
            await update.message.reply_text(c)

'''
anchor = "def main():"
assert anchor in s, "main() not found"
s = s.replace(anchor, handler + anchor, 1)
reg = '    app.add_handler(CommandHandler("disconnect", cmd_disconnect))\n'
assert reg in s, "handler registration not found"
s = s.replace(reg, reg + '    app.add_handler(CommandHandler("servers",    cmd_servers))\n', 1)
s = s.replace("/status — статус\"", "/status — статус\\n/servers — отчёт по всем серверам\"", 1)
open(p, "w").write(s)
print("patched; now: systemctl restart tsech-vpn-bot.service")
