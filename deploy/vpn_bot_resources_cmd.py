#!/usr/bin/env python3
"""Add a /resources [host] command to /root/vpn_bot.py on horek_ge: replies with the
per-service resource digest from /root/monitor_servers/watch.py.
Idempotent. Run as root, then: systemctl restart tsech-vpn-bot.service"""
import shutil, sys, time
p = "/root/vpn_bot.py"
s = open(p).read()
if "cmd_resources" in s:
    print("already patched"); sys.exit(0)
shutil.copy(p, f"{p}.bak-{time.strftime('%Y%m%d-%H%M')}")

handler = '''
WATCH_SCRIPT = "/root/monitor_servers/watch.py"


async def cmd_resources(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Дайджест ресурсов по сервисам: /resources или /resources <host>."""
    if not is_owner(update):
        return
    host = [a for a in (ctx.args or []) if a.replace("_", "").replace(".", "").replace("-", "").isalnum()][:1]
    wait_msg = await update.message.reply_text("⏳ Собираю данные по сервисам, ~20 сек...")
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/python3", WATCH_SCRIPT, "--digest", "--dry", *host,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=240)
        text = out.decode(errors="replace").strip() or "пустой ответ"
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка: {e}")
        return
    # режем по пустым строкам между хостами, чтобы не разорвать <pre>
    chunks, cur = [], ""
    for block in text.split("\\n\\n"):
        if cur and len(cur) + len(block) + 2 > 3800:
            chunks.append(cur); cur = ""
        cur += block + "\\n\\n"
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
s = s.replace(reg, reg + '    app.add_handler(CommandHandler("resources",  cmd_resources))\n', 1)
s = s.replace("/servers — отчёт по всем серверам\"", "/servers — отчёт по всем серверам\\n/resources — ресурсы по сервисам\"", 1)
open(p, "w").write(s)
print("patched; now: systemctl restart tsech-vpn-bot.service")
