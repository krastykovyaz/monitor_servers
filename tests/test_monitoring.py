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


class AlertLogic(unittest.TestCase):
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
            except OSError:
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

    def test_empty_server_list_does_not_crash(self):
        """An empty servers.conf should be handled, not crash with ThreadPoolExecutor(max_workers=0)."""
        Path(watch.DIR, "servers.conf").write_text("# nothing\n")
        try:
            self.run_main(host_output())
        except ValueError as e:
            self.fail("crashed: %s" % e)


class TelegramChunking(unittest.TestCase):
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

        with mock.patch.object(watch.urllib.request, "urlopen", fake_urlopen):
            watch.send({"TG_CHAT_ID": "1", "TG_BOT_TOKEN": "x"}, text)
        broken = [i for i, c in enumerate(chunks) if c.count("<pre>") != c.count("</pre>")]
        self.assertEqual(broken, [], "chunks %s split a <pre> block" % broken)


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


class HeartbeatReceiver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        env = {"HB_SECRET": "s3cret", "TG_BOT_TOKEN": "x", "TG_CHAT_ID": "1", "STATE_DIRECTORY": cls.tmp.name}
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
