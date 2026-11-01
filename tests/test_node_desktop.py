"""Desktop Omni Node (Admin/omni_node_desktop.py + omni_node_words.py).

- The words match Omni Node on Android (NodeStatus.kt / MainActivity.kt), issue #81.
- Pure helpers: status line/colour/detail, Internet and Relay lines, peer parsing, settings file.
- NodeClient against a real daemon with the local token on (health, relay config, add peer).
- The Tk window builds and renders (skipped where there's no display, e.g. headless Linux CI).

Run: python3 -m unittest tests.test_node_desktop
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.daemon_harness import ADMIN, Daemon  # noqa: F401  (puts Admin/ on sys.path)

import omni_node_words as W
import omni_node_desktop as D

HEALTH = {"node_id": "4f23abcd" * 8, "version": "8.22.0", "active_peers": 2,
          "wan": {"public_addr": "203.0.113.7:5005", "reachable": True, "nat": "", "detail": ""},
          "relay": {"enabled": True, "allocations": 1, "mb_today": 12.4, "public": "203.0.113.7:3478", "error": ""}}


class SameWordsAsAndroid(unittest.TestCase):
    """Copied from kiroba/Omni-Node-Daemon NodeStatus.kt + MainActivity.kt (0.3.0). Change both together."""

    def test_status_strings(self):
        self.assertEqual(W.OFF, "Network status: Off")
        self.assertEqual(W.LOOKING, "Looking for nearby people...")
        self.assertEqual(W.STARTING, "Starting...")
        self.assertEqual(W.connected(3), "Connected to 3 nearby")
        self.assertEqual(W.problem("x"), "Problem: x")

    def test_colours(self):
        self.assertEqual((W.GREY, W.RED, W.GREEN, W.TEAL, W.AMBER),
                         ("#808080", "#CF6679", "#4CAF50", "#03DAC6", "#FFB74D"))
        self.assertEqual(W.BG, "#121212")

    def test_detail_and_relay_wording(self):
        self.assertEqual(W.status_detail(W.OFF, None), "Turn on Network to connect")
        self.assertEqual(W.status_detail(W.connected(2), HEALTH), "Running · 2 peers · daemon v8.22.0")
        self.assertEqual(W.status_detail(W.problem("x"), None), "Turn Network off and on. If it stays, send a report.")
        self.assertEqual(W.relay_line(None, False, True, 1024)[0], "Off (turn on Network first)")
        self.assertEqual(W.relay_line(None, True, False, 1024)[0], "Off · limit 1 GB a day")
        self.assertEqual(W.relay_line(HEALTH["relay"], True, True, 5120)[0], "Relaying · 1 active · 12 of 5 GB today")
        self.assertEqual(W.cap_label(250), "250 MB")
        self.assertEqual(W.cap_label(0), "unlimited")


class Helpers(unittest.TestCase):
    def test_status_line(self):
        self.assertEqual(W.status_line(HEALTH, False), W.OFF)
        self.assertEqual(W.status_line(None, True), W.STARTING)
        self.assertEqual(W.status_line({"active_peers": 0}, True), W.LOOKING)
        self.assertEqual(W.status_line(HEALTH, True), "Connected to 2 nearby")
        self.assertEqual(W.status_line(None, True, "boom"), "Problem: boom")
        self.assertEqual(W.status_color(W.status_line(HEALTH, True)), W.GREEN)
        self.assertEqual(W.status_color(W.STARTING), W.AMBER)

    def test_wan_line(self):
        self.assertTrue(W.wan_line(HEALTH["wan"], True)[0].startswith("Reachable at 203.0.113.7:5005"))
        blocked = W.wan_line({"public_addr": "198.51.100.2", "reachable": False, "nat": "symmetric"}, True)[0]
        self.assertIn("router blocks incoming connections (symmetric)", blocked)
        self.assertEqual(W.wan_line(None, True)[0], "Checking...")
        self.assertEqual(W.wan_line({"detail": "No IPv6"}, True)[0], "No IPv6")

    def test_relay_problem_and_private(self):
        self.assertEqual(W.relay_line({"error": "port 3478 busy"}, True, True, 0), ("Problem: port 3478 busy", W.AMBER))
        t = W.relay_line({"enabled": True, "allocations": 0, "mb_today": 0, "public": None}, True, True, 0)[0]
        self.assertIn("router didn't open the ports", t)

    def test_tag_and_tooltip(self):
        self.assertEqual(W.node_tag("4f23abcd"), "·4f23")
        self.assertEqual(W.node_line(""), "")
        self.assertLessEqual(len(W.tray_tooltip("x" * 300)), 127)

    def test_parse_peer(self):
        self.assertEqual(D.parse_peer("1.2.3.4"), ("1.2.3.4", 5005))
        self.assertEqual(D.parse_peer(" 1.2.3.4:6000 "), ("1.2.3.4", 6000))
        self.assertEqual(D.parse_peer("[2001:db8::1]:5005"), ("2001:db8::1", 5005))
        self.assertEqual(D.parse_peer("node.example.org"), ("node.example.org", 5005))
        for bad in ("", "a b", "1.2.3.4:x", "1.2.3.4:70000", "[::1]:z"):
            self.assertIsNone(D.parse_peer(bad), bad)

    def test_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            s = D.Settings(p)
            self.assertTrue(s["network_on"])
            self.assertTrue(s["relay_on"])
            s.set(peers=["1.2.3.4:5005"], relay_mb_day=5120)
            s2 = D.Settings(p)
            self.assertEqual(s2["peers"], ["1.2.3.4:5005"])
            self.assertEqual(s2["relay_mb_day"], 5120)
            p.write_text("not json")
            self.assertEqual(D.Settings(p)["peers"], [])

    def test_login_command_runs_this_file_in_background(self):
        cmd = D.login_command()
        self.assertTrue(cmd[1].endswith("omni_node_desktop.py"))
        self.assertEqual(cmd[-1], "--background")

    def test_linux_autostart_file(self):
        if os.name == "nt" or __import__("sys").platform == "darwin":
            self.skipTest("Linux autostart path")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            self.assertEqual(D.set_start_at_login(True), "")
            f = Path(d) / "autostart/omni-node.desktop"
            self.assertIn("--background", f.read_text())
            self.assertEqual(D.set_start_at_login(False), "")
            self.assertFalse(f.exists())


class AgainstARealDaemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.d = Daemon(Path(cls.tmp.name), "desk", env={"OMNI_LOCAL_TOKEN": "1"})
        cls.token_file = cls.d.home / ".omni" / "local_token"
        cls.d.start_headers = {}
        # node-health needs the token once it exists; the harness polls without it, so read the file first
        import threading
        import time

        def feed_token():
            for _ in range(200):
                if cls.token_file.exists():
                    cls.d.start_headers["X-Omni-Local-Token"] = cls.token_file.read_text().strip()
                    return
                time.sleep(0.05)
        threading.Thread(target=feed_token, daemon=True).start()
        cls.d.start()
        cls.env = mock.patch.dict(os.environ, {"OMNI_TELEMETRY_PORT": str(cls.d.ports["OMNI_TELEMETRY_PORT"]),
                                               "OMNI_IPC_PORT": str(cls.d.ipc_port),
                                               "OMNI_LOCAL_TOKEN_FILE": str(cls.token_file)})
        cls.env.start()
        cls.client = D.NodeClient()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.d.stop()
        cls.tmp.cleanup()

    def test_health_with_token(self):
        h, err = self.client.health()
        self.assertEqual(err, "")
        self.assertEqual(h["node_id"], self.d.node_id)
        self.assertIn("relay", h)
        self.assertEqual(W.status_line(h, True), W.LOOKING)

    def test_health_without_token_is_a_problem_not_a_crash(self):
        with mock.patch.object(D.NodeClient, "token", return_value=None):
            h, err = D.NodeClient().health()
        self.assertIsNone(h)
        self.assertEqual(err, "the node answered 401")

    def test_relay_config_and_peer(self):
        code, body = self.client.relay_config(False, 1024)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["max_mb_day"], 1024)
        self.assertFalse(body["enabled"])
        code, body = self.client.add_peer("198.51.100.9", 5005)
        self.assertEqual(code, 200, body)
        code, _ = self.client.add_peer("bad host!", 5005)
        self.assertEqual(code, 400)

    def test_daemon_process_attaches_to_running_node(self):
        p = D.DaemonProcess(self.client)
        p.start()
        self.assertTrue(p.attached)
        self.assertFalse(p.running())
        p.stop()   # never stops a node it didn't start

    def test_window_renders(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as e:  # no Tk or no display (headless Linux CI)
            self.skipTest(f"no display: {e}")
        try:
            root.withdraw()
            with tempfile.TemporaryDirectory() as d:
                win = D.NodeWindow(root, settings=D.Settings(Path(d) / "s.json"), client=self.client,
                                   manage_daemon=False)
                win.health = HEALTH
                win.render()
                self.assertEqual(win.status.cget("text"), "Connected to 2 nearby")
                self.assertEqual(win.dot.cget("fg"), W.GREEN)
                self.assertTrue(win.wan.cget("text").startswith("Reachable at"))
                win._poll()   # real round trip to the daemon, off the Tk thread in the app
                root.update()
                self.assertEqual(win.status.cget("text"), W.LOOKING)
                win.toggle_network()
                self.assertEqual(win.status.cget("text"), W.OFF)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
