"""Behaviour tests for the monitoring scripts.

Each test states how a component should behave. A failing test is a confirmed defect, described in
its docstring. Run from the repository root:

    python3 -m unittest discover -s tests -v
"""
import http.server
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import common  # noqa: E402


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


watch = load("watch", "watch.py")
probe = load("probe", "probe.py")


def host_output(load1=0.5, cpus=2, disk_pct=10, containers=(), services=(), ports="22"):
    """Synthetic collector output, in the exact line format the remote script prints."""
    total = 1000000
    lines = ["H|cpus=%d|load1=%s|disk_total_kb=%d|disk_used_kb=%d|mem_total_kb=4000000|mem_avail_kb=3000000"
             "|swap_total_kb=0|swap_free_kb=0|uptime_s=100000" % (cpus, load1, total, total * disk_pct // 100)]
    for name, state, mem_pct in containers:
        limit = 900 * 1048576
        mem = limit * mem_pct // 100
        lines.append("C|%s|state=%s|restarts=0|limit=%d|mem=%d|oom_kill=0|anon=%d" % (name, state, limit, mem, mem))
    for name, active, enabled in services:
        lines.append("S|%s|ActiveState=%s|SubState=running|UnitFileState=%s|MemoryCurrent=104857600|NRestarts=0"
                     "|Type=simple|WorkingDirectory=/srv/%s|Anon=104857600" % (name, active, enabled, name))
    lines += ["L|ports=%s" % ports, "LA|ports=%s" % ports, "END"]
    return "\n".join(lines)


def run_checks(outputs, start=1_000_000.0, step=600, state=None):
    """Feeds successive collector outputs through Watch.host and returns (state, list of (fired, resolved))."""
    state = {} if state is None else state
    cfg = dict(watch.DEFAULTS)
    log = []
    for i, out in enumerate(outputs):
        w = watch.Watch(state, cfg, start + i * step)
        data = watch.parse(out)
        w.host("h", data)
        log.append((list(w.fired), list(w.resolved)))
    return state, log


class ParsingAndConfig(unittest.TestCase):
    def test_incomplete_output_is_rejected(self):
        """A collector run cut off before END must count as a failed collection, not as data."""
        self.assertIsNone(watch.parse(host_output().replace("END", "")))

    def test_expect_conf_rejects_shell_metacharacters(self):
        """expect.conf patterns are pasted into a remote shell script; anything able to break out is refused."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "expect.conf").write_text(
                "h | proc | good-name\n"
                "h | proc | $(touch /tmp/pwned)\n"
                "h | proc | a\"; rm -rf /; echo \"\n"
                "h | dir  | /ok/path\n"
                "h | dir  | /tmp/`id`\n")
            with mock.patch.object(watch, "DIR", Path(d)):
                exp = watch.load_expect()
        self.assertEqual(exp["h"]["proc"], ["good-name"])
        self.assertEqual(exp["h"]["dir"], ["/ok/path"])


    def test_inline_comments_in_watch_conf_are_ignored(self):
        """Uncommenting a sample line exactly as written in watch.conf (value, spaces, '# note') must work.
        Defect: the whole line was parsed as the number and the watcher crashed with ValueError."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "watch.conf").write_text("SERVICE_GROW_FACTOR=2.5    # RAM above this x the median\n"
                                             "MUTE=h:swap,h:disk:*   # silenced on purpose\n"
                                             "LOAD_FACTOR@h=3  # per server\n")
            with mock.patch.object(watch, "DIR", Path(d)):
                cfg = watch.load_cfg()
        self.assertEqual(cfg["SERVICE_GROW_FACTOR"], 2.5)
        self.assertEqual(cfg["MUTE"], "h:swap,h:disk:*")
        self.assertEqual(cfg["_host"]["h"]["LOAD_FACTOR"], 3.0)

    def test_unreadable_line_in_watch_conf_is_skipped_not_fatal(self):
        """A typo in one threshold must not stop the watcher; the other lines still apply."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "watch.conf").write_text("DISK_WARN_PCT=abc\nLOAD_FACTOR=3\nLOAD_FACTOR@h=xyz\n")
            with mock.patch.object(watch, "DIR", Path(d)), mock.patch("sys.stderr"):
                cfg = watch.load_cfg()
        self.assertEqual(cfg["DISK_WARN_PCT"], watch.DEFAULTS["DISK_WARN_PCT"])
        self.assertEqual(cfg["LOAD_FACTOR"], 3.0)
        self.assertNotIn("_host", cfg)


class AlertLogic(unittest.TestCase):
    def test_empty_readings_do_not_crash_the_evaluation(self):
        """A host that returns an empty value (a failed awk or cut on the remote side) is evaluated without
        that reading. Defect: float('') or None * 100 raised and aborted the run for every host."""
        good = host_output()
        for old, new in (("mem_avail_kb=3000000", "mem_avail_kb="), ("load1=0.5", "load1="),
                         ("disk_used_kb=100000", "disk_used_kb="), ("swap_free_kb=0", "swap_free_kb=")):
            self.assertIn(old, good)
            run_checks([good.replace(old, new)] * 2)

    def test_pm2_app_with_a_colon_in_its_name_alerts(self):
        """A pm2 app called api:worker that stays errored must alert.
        Defect: drop() cut the key at the first ':', treated 'api' as an unknown item and erased its failure counter
        on every run, so the alert never confirmed."""
        base = host_output()
        pm2 = lambda status: base.replace("L|ports=22", "M|api:worker|status=%s|restarts=0|mem=1000\nL|ports=22" % status, 1)
        state, log = run_checks([pm2("online"), pm2("errored"), pm2("errored"), pm2("errored")])
        fired = [t for f, r in log for t in f]
        self.assertTrue(any("api:worker" in t and "errored" in t for t in fired), "no alert for the errored app: %s" % fired)

    def test_ram_share_alert_does_not_flap_at_the_threshold(self):
        """A service hovering around 35% of host RAM must not alert, resolve and alert again.
        Defect: the ':max' condition cleared on the first reading under the line."""
        base = host_output()

        def with_service(pct):
            m = 4000000 * 1024 * pct // 100
            line = ("S|big|ActiveState=active|SubState=running|UnitFileState=enabled|MemoryCurrent=%d|NRestarts=0"
                    "|Type=simple|Anon=%d\nL|ports=22" % (m, m))
            return base.replace("L|ports=22", line, 1)

        state, log = run_checks([with_service(p) for p in [30, 36, 36, 34, 36, 36, 34, 36, 36]])
        fired = sum(1 for f, r in log for t in f if "of the host" in t)
        self.assertEqual(fired, 1, "RAM-share alert fired %d times in 90 minutes" % fired)

    def test_removed_host_is_forgotten_after_three_runs(self):
        """A host removed from servers.conf must not keep alerts and history in the state forever.
        Defect: nothing ever cleaned them (the stale DigitalOcean alerts)."""
        def holds(state, host):
            return any(k.split(":", 1)[0] == host for b in ("active", "pending", "cooldown", "hist") for k in state[b]) \
                or any(host in state[b] for b in ("seen", "prev", "fails", "ports"))

        state, cfg = {}, dict(watch.DEFAULTS)
        for i, pct in enumerate((10, 96)):
            for name in ("keep", "gone"):
                watch.Watch(state, cfg, 1_000_000.0 + i * 600).host(name, watch.parse(host_output(disk_pct=pct)))
        self.assertTrue(holds(state, "gone"))
        with mock.patch("builtins.print"):
            for _ in range(2):
                watch.Watch(state, cfg, 0).forget_removed_hosts(["keep"])
            self.assertTrue(holds(state, "gone"), "forgotten too early: a half-saved servers.conf would wipe the history")
            watch.Watch(state, cfg, 0).forget_removed_hosts(["keep", "gone"])      # it came back: the countdown restarts
            for _ in range(2):
                watch.Watch(state, cfg, 0).forget_removed_hosts(["keep"])
            self.assertTrue(holds(state, "gone"))
            watch.Watch(state, cfg, 0).forget_removed_hosts(["keep"])
        self.assertFalse(holds(state, "gone"), "state of the removed host is still there: %s" % state)
        self.assertTrue(holds(state, "keep"))

    def test_disk_alert_fires_and_resolves_once(self):
        state, log = run_checks([host_output(disk_pct=10), host_output(disk_pct=90), host_output(disk_pct=90),
                                 host_output(disk_pct=50)])
        fired = [t for f, r in log for t in f]
        resolved = [t for f, r in log for t in r]
        self.assertEqual(sum("disk" in t for t in fired), 1)
        self.assertEqual(sum("disk" in t for t in resolved), 1)

    def test_load_alert_does_not_flap_around_the_threshold(self):
        """Load near 2x CPUs must not alert, resolve and alert again within an hour (seen live on horek_ge2).
        Defect: the load condition clears on the first reading below the line, with no hysteresis."""
        seq = [1.0, 4.5, 4.5, 4.5, 3.9, 4.2, 4.5, 4.5, 4.5]
        state, log = run_checks([host_output(load1=v) for v in seq])
        fired = sum(1 for f, r in log for t in f if "load" in t)
        self.assertEqual(fired, 1, "load alert fired %d times in 90 minutes" % fired)

    def test_vanished_container_leaves_no_state_behind(self):
        """Short-lived containers (force1-memtest) must not leave entries in state forever.
        Defect: drop() clears active alerts only; pending counters and history stay (seen live in state)."""
        outs = [host_output(), host_output(containers=[("memtest", "running", 95)]), host_output()]
        state, log = run_checks(outs)
        leftovers = [k for k in list(state["pending"]) + list(state["hist"]) + list(state["cooldown"]) if "memtest" in k]
        self.assertEqual(leftovers, [], "state still holds %s" % leftovers)

    def test_known_problems_stay_visible_in_the_daily_digest(self):
        """A problem present at first start is never alerted; it must at least appear in the digest.
        Risk: digest() filters out 'known' conditions, so they vanish from every message."""
        state, _ = run_checks([host_output(disk_pct=96)])
        data = watch.parse(host_output(disk_pct=96))
        text = watch.digest([("h", data)], state, 1_000_000.0)
        self.assertIn("disk 96%", text, "the known disk problem does not appear in the digest")


class MainLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "servers.conf").write_text("h | local | -\n")
        self.patches = [mock.patch.object(watch, "DIR", d),
                        mock.patch.object(watch, "STATE_FILE", d / "state" / "watch_state.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_main(self, output, send_fails=False):
        sent, beats = [], []

        def fake_send(env, text):
            if send_fails:
                raise OSError("telegram unreachable")
            sent.append(text)

        with mock.patch.object(watch, "collect", lambda s, e, du, x=None: (s[0], watch.parse(output))), \
             mock.patch.object(watch, "send", fake_send), \
             mock.patch.object(watch, "heartbeat", lambda env, servers: beats.append(1)), \
             mock.patch.object(sys, "argv", ["watch.py"]), mock.patch("builtins.print"):
            try:
                watch.main()
            except (OSError, SystemExit):
                pass
        return sent, beats

    def test_alert_is_delivered_after_a_failed_send(self):
        """If Telegram is unreachable, the alert must be delivered on a later run.
        Defect: state is saved before sending, so the condition is marked active and never sent again."""
        self.run_main(host_output(disk_pct=10))
        self.run_main(host_output(disk_pct=96), send_fails=True)
        sent, _ = self.run_main(host_output(disk_pct=96))
        self.assertTrue(any("disk" in m for m in sent), "the disk alert was lost")

    def test_no_heartbeat_when_alerts_cannot_be_sent(self):
        """A watcher that cannot deliver alerts is broken and should look broken.
        Defect: the heartbeat is sent before the alert, so a dead Telegram path is invisible."""
        self.run_main(host_output(disk_pct=10))
        _, beats = self.run_main(host_output(disk_pct=96), send_fails=True)
        self.assertEqual(beats, [], "heartbeat was sent although the alert failed")

    def test_one_broken_host_does_not_stop_the_others(self):
        """If evaluating one host raises, the other hosts are still evaluated and alerted, and the broken
        host is reported. Defect: the exception ended the run, so nobody got any alert."""
        (Path(watch.DIR) / "servers.conf").write_text("bad | local | -\ngood | local | -\n")
        sent, real_host = [], watch.Watch.host

        def host(self, name, d):
            if name == "bad":
                raise RuntimeError("unexpected value")
            return real_host(self, name, d)

        def run(good_output):
            outs = {"bad": host_output(), "good": good_output}
            with mock.patch.object(watch, "collect", lambda s, e, du, x=None: (s[0], watch.parse(outs[s[0]]))), \
                 mock.patch.object(watch.Watch, "host", host), \
                 mock.patch.object(watch, "send", lambda env, text: sent.append(text)), \
                 mock.patch.object(watch, "heartbeat", lambda env, servers: None), \
                 mock.patch.object(sys, "argv", ["watch.py"]), mock.patch("builtins.print"), mock.patch("sys.stderr"):
                watch.main()

        run(host_output(disk_pct=10))
        run(host_output(disk_pct=96))
        text = "\n".join(sent)
        self.assertIn("disk", text, "the healthy host's disk alert was lost")
        self.assertIn("cannot evaluate", text, "the broken host was not reported")

    def test_show_one_host_contacts_only_that_host(self):
        """--show HOST must collect from that host only, not the whole fleet.
        Defect: every host was contacted and its folders scanned, which took minutes."""
        (Path(watch.DIR) / "servers.conf").write_text("a | x@a | 22\nb | x@b | 22\nc | x@c | 22\n")
        contacted = []

        def fake_collect(s, env, du, x=None):
            contacted.append(s[0])
            return s[0], watch.parse(host_output())

        with mock.patch.object(watch, "collect", fake_collect), mock.patch.object(sys, "argv", ["watch.py", "--show", "b"]), \
             mock.patch("builtins.print"):
            watch.main()
        self.assertEqual(contacted, ["b"])

    def test_empty_server_list_does_not_crash(self):
        """An empty servers.conf should be handled, not crash with ThreadPoolExecutor(max_workers=0)."""
        Path(watch.DIR, "servers.conf").write_text("# nothing\n")
        try:
            self.run_main(host_output())
        except ValueError as e:
            self.fail("crashed: %s" % e)


class TelegramChunking(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()      # delivery notes of these tests never touch the real state folder
        p = mock.patch.object(common, "partial_path", lambda: os.path.join(self._tmp.name, "tg_partial.json"))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_long_digest_chunks_keep_html_balanced(self):
        """Telegram rejects a message with unbalanced HTML. A digest over 3900 characters is split into chunks;
        each chunk must be valid on its own. Defect: send() splits by line, inside <pre> blocks."""
        services = [("service-%02d" % i, "active", "enabled") for i in range(10)]
        results = [("host-%02d" % i, watch.parse(host_output(services=services))) for i in range(12)]
        text = watch.digest(results, {}, 1_000_000.0)
        self.assertGreater(len(text), 3900)
        chunks = []

        class Resp:
            def read(self):
                return b"{}"

        def fake_urlopen(url, data, timeout):
            from urllib.parse import parse_qs
            chunks.append(parse_qs(data.decode())["text"][0])
            return Resp()

        with mock.patch.object(common.urllib.request, "urlopen", fake_urlopen):
            watch.send({"TG_CHAT_ID": "1", "TG_BOT_TOKEN": "x"}, text)
        broken = [i for i, c in enumerate(chunks) if c.count("<pre>") != c.count("</pre>")]
        self.assertEqual(broken, [], "chunks %s split a <pre> block" % broken)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 4096 for c in chunks), "a chunk exceeds Telegram's limit")
        strip = lambda t: t.replace("<pre>", "").replace("</pre>", "").replace("\n", "")
        self.assertEqual(strip("".join(chunks)), strip(text), "content was lost or duplicated by chunking")

    class Resp:
        def read(self):
            return b"{}"

    def test_failed_later_chunk_is_not_sent_twice_on_retry(self):
        """A message in several chunks whose second chunk fails is retried later; chunks that already arrived must
        not be sent again. Defect: the whole message was repeated, so the first chunk appeared twice."""
        from urllib.parse import parse_qs
        text = "\n".join("line %04d %s" % (i, "x" * 60) for i in range(150))
        want = common.chunks(text)
        self.assertGreater(len(want), 2)
        tries, ok, env = [0], [], {"TG_CHAT_ID": "1", "TG_BOT_TOKEN": "x"}

        def fake(url, data, timeout):
            tries[0] += 1
            if tries[0] == 2:
                raise OSError("network down")
            ok.append(parse_qs(data.decode())["text"][0])
            return self.Resp()

        with tempfile.TemporaryDirectory() as d, mock.patch.object(common, "partial_path", lambda: os.path.join(d, "p.json")), \
             mock.patch.object(common.urllib.request, "urlopen", fake):
            with self.assertRaises(OSError):
                common.tg_send(env, text)
            self.assertEqual(len(ok), 1)
            common.tg_send(env, text)                       # the retry
            self.assertEqual(ok, want, "chunks were repeated or lost")
            self.assertFalse(os.path.exists(os.path.join(d, "p.json")), "the delivery note was not cleaned up")
            ok[:] = []
            common.tg_send(env, text)                       # a later, identical message must be sent in full
            self.assertEqual(ok, want)

    def fallback_env(self, **extra):
        env = {"TG_CHAT_ID": "1", "TG_BOT_TOKEN": "MAIN"}
        env.update(extra)
        return env

    def run_with_main_bot_blocked(self, env, text="<b>disk</b> 96%"):
        import urllib.error
        from urllib.parse import parse_qs
        seen = []

        def fake(url, data, timeout):
            seen.append((url, parse_qs(data.decode())))
            if "/botMAIN/" in url:
                raise urllib.error.HTTPError(url, 403, "Forbidden: bot was blocked by the user", None, None)
            return self.Resp()

        with tempfile.TemporaryDirectory() as d, mock.patch.object(common, "partial_path", lambda: os.path.join(d, "p.json")), \
             mock.patch.object(common.urllib.request, "urlopen", fake), mock.patch("sys.stderr"):
            try:
                common.tg_send(env, text)
            finally:
                self.seen = seen

    def test_fallback_bot_takes_over_when_the_main_bot_fails(self):
        """If the main bot is revoked or blocked, a configured fallback bot still delivers, and says so.
        Defect: there was no second channel, so a broken bot silenced every alert."""
        self.run_with_main_bot_blocked(self.fallback_env(TG_FALLBACK_BOT_TOKEN="FB", TG_FALLBACK_CHAT_ID="7"))
        fb = [f for url, f in self.seen if "/botFB/" in url]
        self.assertEqual(len(fb), 1, "the fallback bot was not used exactly once")
        self.assertEqual(fb[0]["chat_id"], ["7"])
        self.assertTrue(fb[0]["text"][0].startswith("⚠️ via the fallback bot"))
        self.assertIn("<b>disk</b> 96%", fb[0]["text"][0])

    def test_fallback_chat_defaults_to_the_main_chat(self):
        self.run_with_main_bot_blocked(self.fallback_env(TG_FALLBACK_BOT_TOKEN="FB"))
        self.assertEqual([f["chat_id"] for url, f in self.seen if "/botFB/" in url], [["1"]])

    def test_without_a_fallback_the_failure_is_still_raised(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError):
            self.run_with_main_bot_blocked(self.fallback_env())

    def test_rejected_markup_falls_back_to_plain_text(self):
        """If Telegram rejects the HTML (HTTP 400), the message is resent as plain text instead of being lost."""
        import urllib.error
        calls = []

        class Resp:
            def read(self):
                return b"{}"

        def fake_urlopen(url, data, timeout):
            from urllib.parse import parse_qs
            f = parse_qs(data.decode())
            calls.append(f)
            if "parse_mode" in f:
                raise urllib.error.HTTPError(url, 400, "Bad Request", None, None)
            return Resp()

        with mock.patch.object(common.urllib.request, "urlopen", fake_urlopen):
            common.tg_send({"TG_CHAT_ID": "1", "TG_BOT_TOKEN": "x"}, "<b>host</b> a &lt; b")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["text"][0].strip(), "host a < b")


class Prober(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                code = 200 if self.path == "/ok" else 503
                self.send_response(code)
                self.end_headers()

            def log_message(self, *a):
                pass

        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def p(self, target, expect=""):
        return probe.check({"name": "t", "target": target, "expect": expect})

    def test_http_ok_and_server_error(self):
        self.assertTrue(self.p("http://127.0.0.1:%d/ok" % self.port)["ok"])
        self.assertFalse(self.p("http://127.0.0.1:%d/broken" % self.port)["ok"])

    def test_only_ip_without_a_value_is_a_usage_error(self):
        """probe.py --only-ip with no address must print usage and exit 2, not crash with IndexError."""
        with mock.patch.object(sys, "argv", ["probe.py", "--only-ip"]), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as cm:
                probe.main()
        self.assertEqual(cm.exception.code, 2)

    def test_tcp_open_and_refused(self):
        self.assertTrue(self.p("tcp://127.0.0.1:%d" % self.port)["ok"])
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        s.close()
        r = self.p("tcp://127.0.0.1:%d" % closed)
        self.assertFalse(r["ok"])
        self.assertIn("refused", r["detail"])

    def test_alert_is_delivered_after_a_failed_send(self):
        """Same ordering defect as the watcher: state is saved before send()."""
        src = (ROOT / "probe.py").read_text()
        self.assertLess(src.index("send(env, msg)"), src.index("os.replace(str(tmp), str(STATE_FILE))"),
                        "probe.py saves state before sending, so a failed send loses the alert")
        self.assertLess(src.index("os.replace(str(tmp), str(STATE_FILE))"), src.index("hb_ping(env"),
                        "probe.py must send its heartbeat only after a fully successful run")


class HeartbeatReceiver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        env = {"HB_SECRET": "s3cret", "HB_ADMIN_SECRET": "adm1n", "TG_BOT_TOKEN": "x", "TG_CHAT_ID": "1",
               "STATE_DIRECTORY": cls.tmp.name}
        with mock.patch.dict(os.environ, env):
            cls.hb = load("heartbeat_server", "heartbeat_server.py")
        cls.srv = cls.hb.ThreadingHTTPServer(("127.0.0.1", 0), cls.hb.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def get(self, path):
        import urllib.request, urllib.error
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_auth_and_name_validation(self):
        self.assertEqual(self.get("/hb/s3cret/host-a"), 200)
        self.assertEqual(self.get("/hb/wrong/host-a"), 404)
        self.assertEqual(self.get("/hb/s3cret/a%20b"), 404)
        self.assertEqual(self.get("/status/wrong"), 404)

    def test_forget_needs_the_admin_secret(self):
        """The secret every host knows must not be able to remove hosts from watch."""
        self.assertEqual(self.get("/hb/s3cret/doomed"), 200)
        self.assertEqual(self.get("/forget/s3cret/doomed"), 404)
        self.assertIn("doomed", self.hb.beats)
        self.assertEqual(self.get("/forget/adm1n/doomed"), 200)
        self.assertNotIn("doomed", self.hb.beats)

    def test_per_name_silence_limit(self):
        """A daily job announces its own limit (?max=) and is not reported silent after 25 minutes."""
        self.assertEqual(self.get("/hb/s3cret/daily-job?max=93600"), 200)
        with self.hb.lock:
            self.hb.beats["daily-job"]["last"] = time.time() - 7200
        sent, calls = [], {"n": 0}

        def one_tick(_):
            calls["n"] += 1
            if calls["n"] > 1:
                raise StopIteration

        with mock.patch.object(self.hb, "tg", sent.append), mock.patch.object(self.hb.time, "sleep", one_tick):
            try:
                self.hb.checker()
            except StopIteration:
                pass
        self.assertFalse(self.hb.beats["daily-job"]["silent"])
        self.assertFalse(any("daily-job" in m and "silent" in m for m in sent))

    def test_silence_is_detected(self):
        with self.hb.lock:
            self.hb.beats["old-host"] = {"first": 0, "last": time.time() - 3600, "silent": False}
        sent = []
        calls = {"n": 0}

        def one_tick(_):
            calls["n"] += 1
            if calls["n"] > 1:
                raise StopIteration

        with mock.patch.object(self.hb, "tg", sent.append), mock.patch.object(self.hb.time, "sleep", one_tick):
            try:
                self.hb.checker()
            except StopIteration:
                pass
        self.assertTrue(any("old-host" in m and "silent" in m for m in sent))
        self.assertTrue(self.hb.beats["old-host"]["silent"])

    def rounds(self, n, tg):
        """Runs n checker rounds with the given Telegram sender."""
        calls = {"n": 0}

        def sleep(_):
            calls["n"] += 1
            if calls["n"] > n:
                raise StopIteration

        with mock.patch.object(self.hb, "tg", tg), mock.patch.object(self.hb.time, "sleep", sleep), \
             mock.patch("builtins.print"):
            try:
                self.hb.checker()
            except StopIteration:
                pass

    def test_silence_alert_survives_a_failed_telegram_send(self):
        """If Telegram cannot be reached when a host goes silent, the alert is sent in a later round.
        Defect: the host was marked silent before sending, so the alert was lost for good."""
        self.addCleanup(self.hb.beats.pop, "lost-host", None)
        with self.hb.lock:
            self.hb.beats["lost-host"] = {"first": 0, "last": time.time() - 7200, "silent": False}

        def down(text):
            raise OSError("telegram unreachable")

        self.rounds(1, down)
        self.assertFalse(self.hb.beats["lost-host"]["silent"], "marked silent although nobody was told")
        sent = []
        self.rounds(1, sent.append)
        self.assertTrue(any("lost-host" in m and "silent" in m for m in sent), "the alert never arrived")
        self.assertTrue(self.hb.beats["lost-host"]["silent"])

    def test_a_damaged_entry_does_not_stop_the_checker(self):
        """One entry without a 'last' time must not end the checker thread, which would stop every later
        silence alert while the receiver keeps answering. Defect: KeyError escaped the loop."""
        self.addCleanup(self.hb.beats.pop, "broken", None)
        self.addCleanup(self.hb.beats.pop, "zz-dead", None)
        with self.hb.lock:
            self.hb.beats["broken"] = {"first": 0}
            self.hb.beats["zz-dead"] = {"first": 0, "last": time.time() - 7200, "silent": False}
        sent = []
        self.rounds(2, sent.append)          # a second round proves the loop is still alive
        self.assertTrue(any("zz-dead" in m and "silent" in m for m in sent), "the healthy entry was not checked")

    def test_fallback_bot_delivers_when_the_main_bot_fails(self):
        """The dead man's switch must still be able to speak when the main bot is revoked or blocked."""
        from urllib.parse import parse_qs
        seen = []

        def fake(url, data, timeout):
            seen.append((url, parse_qs(data.decode())))
            if "/botx/" in url:
                raise OSError("main bot blocked")

            class R:
                def read(self):
                    return b"{}"
            return R()

        with mock.patch.object(self.hb.urllib.request, "urlopen", fake), mock.patch.object(self.hb, "FB_TOKEN", "fbtok"), \
             mock.patch.object(self.hb, "FB_CHAT", "99"), mock.patch("builtins.print"):
            self.hb.tg("host-a has gone silent")
        fb = [f for url, f in seen if "/botfbtok/" in url]
        self.assertEqual(len(fb), 1)
        self.assertEqual(fb[0]["chat_id"], ["99"])
        self.assertIn("host-a has gone silent", fb[0]["text"][0])
        self.assertIn("fallback", fb[0]["text"][0])

    def test_damaged_state_file_is_cleaned_on_load(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "beats.json").write_text(json.dumps({
                "good": {"first": 1, "last": 2, "silent": False}, "no-last": {"first": 1}, "text": "oops", "none": None}))
            with mock.patch.dict(os.environ, {"HB_SECRET": "s", "TG_BOT_TOKEN": "x", "TG_CHAT_ID": "1", "STATE_DIRECTORY": d}), \
                 mock.patch("builtins.print"):
                hb2 = load("heartbeat_server_reload", "heartbeat_server.py")
        self.assertEqual(sorted(hb2.beats), ["good"])

    def test_idle_connections_are_dropped(self):
        """A client that connects and sends nothing must be disconnected, or idle connections pile up
        threads until the 64 MB memory cap kills the service. Defect: the handler has no timeout."""
        s = socket.create_connection(("127.0.0.1", self.port))
        s.settimeout(8)
        try:
            data = s.recv(1)
            self.assertEqual(data, b"")
        except socket.timeout:
            self.fail("server kept an idle connection open for more than 8 seconds")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
