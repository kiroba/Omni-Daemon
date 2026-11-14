"""Omni Node shared core (daemon v8.27.0): Admin/omni_node_core.py + GET /api/node/ui, POST /api/node/action,
POST /api/wan/config.

- Pure parts: peer/pair parsing, Relay calls policy, update picking, the Node version, settings migration
  from the old desktop and Android copies, two processes sharing one settings file.
- One state, every platform: the same node gives the same status words and colours on Android and desktop;
  only the platform-kind bits (Tap/Click, relay rules, caps, radios, sign-in start) differ.
- Actions offline and against a real daemon with the local token on (relay, router port, peers, pairing,
  update check against a local fake GitHub).
- The daemon's own loopback routes, the APK packaging hint, stdlib-only imports.
- Parity: the desktop shell holds no user-facing status words of its own (they come from the core).

Run: python3 -m unittest tests.test_node_core
"""
import ast
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests.daemon_harness import ADMIN, Daemon, http  # noqa: F401  (puts Admin/ on sys.path)

import omni_node_core as C
import omni_node_words as W

HEALTH = {"node_id": "4f23abcd" * 8, "version": "8.27.0", "active_peers": 2,
          "wan": {"public_addr": "203.0.113.7:5005", "reachable": True, "nat": "", "detail": ""},
          "relay": {"enabled": True, "allocations": 1, "mb_today": 12.4, "public": "203.0.113.7:3478", "error": ""},
          "quiet": {"mode": "auto", "active": False, "paired": 0}}
ANDROID = {"platform": "android", "charging": True, "unmetered": True, "battery_saver": False,
           "default_network_on": True, "radios": {"missing": False, "status": {"ble": "on", "ble_nearby": 2,
                                                                               "wifi_aware": "on"}}}
DESKTOP = {"platform": "windows", "autostart_available": True}

# Words a shell must never spell out itself (they come from omni_node_words through the core).
STATUS_PATTERNS = re.compile(r"Connected to|Looking for nearby|Network status|Quiet mode|Relaying ·|Reachable at|"
                             r"Problem: |Starting\.\.\.|Turn on Network|Help calls connect|Pair your phone|"
                             r"node token|Keeps you connected")


class _Home:
    """A temp HOME (Path.home() and ~/.omni) for one test."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.p = mock.patch.object(Path, "home", return_value=self.home)
        self.p.start()
        return self.home

    def __exit__(self, *a):
        self.p.stop()
        self.tmp.cleanup()


def offline_core(env, home: Path, health=None):
    core = C.NodeCore(env=env, client=C.NodeClient(1, 1, token_fn=lambda: "t" * 43),
                      runtime_path=home / ".omni" / "node_runtime.json")
    core.health = health
    return core


class Pure(unittest.TestCase):
    def test_parse_peer_and_key(self):
        self.assertEqual(C.parse_peer("http://1.2.3.4/"), ("1.2.3.4", 5005))
        self.assertEqual(C.parse_peer("2001:db8::1"), ("2001:db8::1", 5005))
        self.assertEqual(C.parse_peer("[2001:db8::1]:6000"), ("2001:db8::1", 6000))
        for bad in ("", "a b", "1.2.3.4:x", "1.2.3.4:0", "host!", "[::1]z", "[::1]:z"):
            self.assertIsNone(C.parse_peer(bad), bad)
        self.assertEqual(C.peer_key("1.2.3.4", 5005), "1.2.3.4")
        self.assertEqual(C.peer_key("1.2.3.4", 6000), "1.2.3.4:6000")
        self.assertEqual(C.peer_key("2001:db8::1", 6000), "[2001:db8::1]:6000")

    def test_relay_policy(self):
        on_wifi = {"charging": True, "unmetered": True}
        self.assertEqual(C.relay_policy(False, on_wifi, True), (False, "off"))
        self.assertEqual(C.relay_policy(True, {}, True), (False, W.RELAY_CHECKING))
        self.assertEqual(C.relay_policy(True, dict(on_wifi, unmetered=False), True), (False, W.RELAY_WAIT_WIFI))
        self.assertEqual(C.relay_policy(True, dict(on_wifi, charging=False), True), (False, W.RELAY_WAIT_CHARGING))
        self.assertEqual(C.relay_policy(True, dict(on_wifi, battery_saver=True), True), (False, W.RELAY_WAIT_SAVER))
        self.assertEqual(C.relay_policy(True, on_wifi, True), (True, "relaying"))
        self.assertEqual(C.relay_policy(True, {}, False), (True, "relaying"))     # desktops: whenever it's on

    def test_pick_update(self):
        rel = [{"tag_name": "v0.4.0-prealpha", "html_url": "u4"}, {"tag_name": "v0.6.0", "draft": True},
               {"tag_name": "v0.5.1-prealpha", "html_url": "u51"}, {"tag_name": "nightly"}]
        self.assertEqual(C.pick_update(rel, "0.5.0"),
                         {"latest": "0.5.1", "tag": "v0.5.1-prealpha", "url": "u51", "newer": True})
        self.assertFalse(C.pick_update(rel, "0.5.1")["newer"])
        self.assertTrue(C.pick_update(rel, "dev")["newer"])
        self.assertEqual(C.pick_update({"message": "rate limited"}, "0.5.0")["latest"], "")

    def test_node_version(self):
        with mock.patch.dict(os.environ, {"OMNI_NODE_VERSION": "0.5.0"}):
            self.assertEqual(C.node_version(), "0.5.0")
        with mock.patch.dict(os.environ, {"OMNI_NODE_VERSION": "not a version"}):
            self.assertEqual(C.node_version(), "dev")
        with mock.patch.dict(os.environ, {"OMNI_NODE_VERSION": ""}):
            expected = (ADMIN / "NODE_VERSION").read_text().strip() if (ADMIN / "NODE_VERSION").exists() else "dev"
            self.assertEqual(C.node_version(), expected)
            fake = type(sys)("omni_node_version")
            fake.VERSION = "0.5.0"
            with mock.patch.dict(sys.modules, {"omni_node_version": fake}):
                self.assertEqual(C.node_version(), "0.5.0")
        self.assertEqual(W.version_line("0.5.0", "8.27.0"), "Omni Node 0.5.0 · daemon v8.27.0")
        self.assertEqual(W.version_line("0.5.0"), "Omni Node 0.5.0")

    def test_words_are_complete_for_shells(self):
        words = W.all_words()
        for k in ("TITLE", "NETWORK", "CONNECT_APPS", "ADD_PEER", "PAIR_ENTER", "PAIR_LOOKING", "UPDATE_CHECK",
                  "TOKEN_COPY", "NO_LOCK_TITLE", "BATTERY_TITLE", "NOTIF_CHANNEL", "NOTIF_TURN_OFF", "TRAY_QUIT",
                  "RELAY_CAP_TITLE", "ADD_PEER_HOWTO", "CANCEL", "KEEP", "PAIR", "ADD", "OK"):
            self.assertIn(k, words)
        self.assertTrue(all(isinstance(v, str) and v for v in words.values()))


class SettingsFile(unittest.TestCase):
    def test_migrates_old_desktop_settings(self):
        with _Home() as home:
            (home / ".omni").mkdir()
            (home / ".omni" / "node_desktop.json").write_text(json.dumps(
                {"network_on": False, "relay_mb_day": 5120, "peers": ["1.2.3.4:5005"], "junk": 1}))
            s = C.Settings(defaults=C.profile(DESKTOP)["defaults"])
            self.assertFalse(s["network_on"])
            self.assertEqual(s["relay_mb_day"], 5120)
            self.assertEqual(s["peers"], ["1.2.3.4:5005"])
            self.assertTrue((home / ".omni" / "node_settings.json").exists())

    def test_migrates_old_android_files(self):
        with _Home() as home:   # Android: HOME = filesDir, where Node <= 0.4.0 kept peers.txt and relay.json
            (home / "peers.txt").write_text("203.0.113.7\n[2001:db8::1]:6000\nnot valid!\n")
            (home / "relay.json").write_text(json.dumps({"on": True, "mb_day": 250, "kbps": 1500}))
            s = C.Settings(defaults=C.profile(ANDROID)["defaults"])
            self.assertEqual(s["peers"], ["203.0.113.7", "[2001:db8::1]:6000"])
            self.assertTrue(s["relay_on"])
            self.assertEqual(s["relay_mb_day"], 250)
            self.assertTrue(s["network_on"])     # default_network_on from the old Android switch

    def test_two_processes_share_one_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "node_settings.json"
            ui, service = C.Settings(p), C.Settings(p)
            ui.set(relay_on=False)
            time.sleep(0.01)
            service.set(peers=["1.2.3.4"])           # must not undo the other process's change
            self.assertFalse(service["relay_on"])
            self.assertEqual(ui["peers"], ["1.2.3.4"])
            self.assertFalse(ui["relay_on"])


class OneStateEveryPlatform(unittest.TestCase):
    def test_same_node_same_words(self):
        with _Home() as home:
            a = offline_core(ANDROID, home, HEALTH).ui_state()
            d = offline_core(DESKTOP, home, HEALTH).ui_state()
        for k in ("line", "color", "detail"):
            self.assertEqual(a["status"][k], d["status"][k], k)
        self.assertEqual(a["status"]["line"], "Connected to 2 nearby")
        self.assertEqual(a["status"]["color"], W.GREEN)
        self.assertEqual(a["status"]["detail"], "Running · 2 peers · daemon v8.27.0")
        self.assertEqual(a["words"], d["words"])
        self.assertEqual(a["node_version"], d["node_version"])
        self.assertEqual(a["pairing"]["quiet_line"], d["pairing"]["quiet_line"])
        self.assertEqual(a["core"], C.CORE_SCHEMA)
        # Platform-kind bits only:
        self.assertIn("tap to copy", a["status"]["node_line"])
        self.assertIn("click to copy", d["status"]["node_line"])
        self.assertIn("Long-press to copy", a["internet"]["line"])
        self.assertIn("Click to copy", d["internet"]["line"])
        self.assertEqual([c["mb"] for c in a["relay"]["caps"]], [250, 1024, 5120])
        self.assertEqual([c["mb"] for c in d["relay"]["caps"]], [1024, 5120, 20480, 0])
        self.assertEqual(a["radios"]["line"], "Bluetooth: on · 2 nearby\nWi-Fi Aware: on")
        self.assertIsNone(d["radios"])
        self.assertIsNone(a["background"])
        self.assertEqual(d["background"]["label"], W.START_AT_LOGIN)
        self.assertEqual(a["connect"]["hint"], W.CONNECT_APPS_HINT_PHONE)
        self.assertEqual(d["connect"]["hint"], W.CONNECT_APPS_HINT_DESKTOP)

    def test_off_and_starting(self):
        with _Home() as home:
            core = offline_core(ANDROID, home)
            core.settings.set(network_on=False)
            st = core.ui_state()
            self.assertEqual((st["status"]["line"], st["status"]["color"]), (W.OFF, W.GREY))
            self.assertEqual(st["status"]["detail"], "Turn on Network to connect")
            self.assertEqual(st["pairing"]["quiet_line"], W.QUIET_NETWORK_OFF)
            self.assertEqual(st["relay"]["line"], "Off (turn on Network first)")
            self.assertEqual(st["radios"]["line"], W.OFF_SHORT)
            core.settings.set(network_on=True)
            st = core.ui_state()
            self.assertEqual((st["status"]["line"], st["status"]["color"]), (W.STARTING, W.AMBER))
            core.set_env(dict(ANDROID, node_error="ModuleNotFoundError: omni_x"))
            self.assertEqual(core.ui_state()["status"]["line"], "Problem: ModuleNotFoundError: omni_x")

    def test_phone_relay_lines(self):
        with _Home() as home:
            core = offline_core(dict(ANDROID, charging=False), home, HEALTH)
            core.settings.set(relay_on=True, relay_mb_day=1024)
            self.assertEqual(core.ui_state()["relay"]["line"], "Waiting: until the phone is charging · limit 1 GB a day")
            core.set_env(dict(ANDROID, unmetered=False))
            self.assertEqual(core.ui_state()["relay"]["line"],
                             "Waiting: for Wi-Fi (never relays on mobile data) · limit 1 GB a day")
            core.set_env(ANDROID)
            self.assertEqual(core.ui_state()["relay"]["line"], "Relaying · 1 active · 12 of 1 GB today")
            core.relay_state = {"daemon": W.RELAY_TOO_OLD}
            self.assertEqual(core.ui_state()["relay"]["line"], W.problem(W.RELAY_TOO_OLD))

    def test_radios_permission_and_router_off(self):
        with _Home() as home:
            core = offline_core(dict(ANDROID, radios={"missing": True}), home, HEALTH)
            r = core.ui_state()["radios"]
            self.assertEqual((r["line"], r["ask_permission"]), (W.RADIOS_NEED_PERMISSION, True))
            core.settings.set(internet_on=False)
            self.assertIn(W.INTERNET_ROUTER_OFF, core.ui_state()["internet"]["line"])

    def test_radios_ble_intro_line(self):
        # NodeRadio's ble_intro ledger (radio_status.json) must reach the screen: until this
        # wiring the core dropped ble_met/ble_intro and a failed GATT introduction left no
        # trace anywhere in the Node UI (field tests #1/#2, Nov 12-13).
        status = {"ble": "on", "ble_nearby": 1, "wifi_aware": "off", "ble_met": 1,
                  "ble_intro": {"attempts": 3, "connected": 1, "read_ok": 0, "posted": 1,
                                "served": 2, "served_empty": 1,
                                "last": {"stage": "connect_failed", "detail": "gatt status 133",
                                         "at_ms": 1}}}
        env = dict(ANDROID, radios={"missing": False, "status": status})
        with _Home() as home:
            line = offline_core(env, home, HEALTH).ui_state()["radios"]["line"]
        self.assertEqual(line, "Bluetooth: on · 1 nearby\nWi-Fi Aware: off\n"
                               "Introductions: 3 tries · 1 connected · 0 read · 1 posted · 2 served · "
                               "1 served empty · last: connect_failed (gatt status 133)")
        # No ledger yet (fresh node / older radio): the original two lines, word for word.
        self.assertEqual(W.radios_line("on", 2, "on"), "Bluetooth: on · 2 nearby\nWi-Fi Aware: on")
        self.assertEqual(W.radios_line("on", 0, "off", 0, {"attempts": 0, "last": None}),
                         "Bluetooth: on\nWi-Fi Aware: off\n"
                         "Introductions: 0 tries · 0 connected · 0 read · 0 posted · 0 served")
        # A posted count alone (ble_met without a ledger dict) still shows.
        self.assertEqual(W.radios_line("on", 1, "off", 2),
                         "Bluetooth: on · 1 nearby\nWi-Fi Aware: off\n"
                         "Introductions: 0 tries · 0 connected · 0 read · 2 posted · 0 served")

    def test_update_banner(self):
        with _Home() as home:
            core = offline_core(DESKTOP, home)
            self.assertEqual(core.ui_state()["update"]["line"], "")
            core.settings.set(update={"checked": 1, "latest": "9.0.0", "newer": True, "url": "https://x/rel"})
            u = core.ui_state()["update"]
            self.assertEqual((u["line"], u["url"], u["color"]), ("Omni Node 9.0.0 is available", "https://x/rel", W.TEAL))
            core.settings.set(update={"checked": 1, "latest": "0.0.1", "newer": False, "url": "u"})
            self.assertTrue(core.ui_state()["update"]["line"].startswith("You have the newest Omni Node"))
            core.settings.set(update={"checked": 1, "error": "timed out"})
            self.assertEqual(core.ui_state()["update"]["line"], "Couldn't check for updates: timed out")


class OfflineActions(unittest.TestCase):
    def test_peers(self):
        with _Home() as home:
            core = offline_core(DESKTOP, home)
            self.assertEqual(core.do("add_peer", {"text": "bad host!"})["toast"], W.PEER_BAD)
            out = core.do("add_peer", {"text": "203.0.113.7:5005"})
            self.assertEqual((out["toast"], out["effects"]), ("Added. Your node will sync with 203.0.113.7.",
                                                              ["settings_changed"]))
            core.do("add_peer", {"text": "203.0.113.7"})           # same peer, other spelling: no duplicate
            self.assertEqual(core.settings["peers"], ["203.0.113.7"])
            item = core.ui_state()["peers"]["items"][0]
            self.assertEqual((item["label"], item["color"]), ("\u25CB  203.0.113.7  ·  waiting for the node", W.AMBER))
            core.peer_state = {"203.0.113.7": "ok"}
            self.assertEqual(core.ui_state()["peers"]["items"][0]["label"], "\u25CF  203.0.113.7  ·  syncing")
            core.do("remove_peer", {"key": "203.0.113.7"})
            self.assertEqual(core.settings["peers"], [])

    def test_network_relay_internet_and_misc(self):
        with _Home() as home:
            core = offline_core(DESKTOP, home, HEALTH)
            self.assertEqual(core.do("network", {"on": False})["effects"], ["stop_node"])
            self.assertIsNone(core.health)
            self.assertEqual(core.do("token")["toast"], W.TOKEN_NOT_YET)
            self.assertEqual(core.do("pair_offer")["toast"], W.NETWORK_FIRST)
            self.assertEqual(core.do("network", {"on": True})["effects"], ["start_node"])
            core.do("relay", {"on": False})
            self.assertFalse(core.settings["relay_on"])
            self.assertFalse(core.do("relay_cap", {"mb": 250})["ok"])      # a phone cap isn't offered here
            core.do("relay_cap", {"mb": 20480})
            self.assertEqual(core.settings["relay_mb_day"], 20480)
            core.do("internet", {"on": False})
            self.assertFalse(core.settings["internet_on"])
            self.assertFalse(core.do("start_at_login", {"on": True, "error": "denied"})["ok"])
            self.assertFalse(core.settings["start_at_login"])
            core.do("start_at_login", {"on": True})
            self.assertTrue(core.settings["start_at_login"])
            self.assertIn("unknown action", core.do("__init__")["error"])
            self.assertIn("unknown action", core.do("nope")["error"])
            daemon_view = offline_core(dict(DESKTOP, can_stop_node=False), home)
            self.assertEqual(daemon_view.do("network", {"on": False})["toast"], W.NETWORK_HERE_ONLY)

    def test_toasts_are_taken_once(self):
        with _Home() as home:
            core = offline_core(DESKTOP, home)
            core.toast(W.PAIR_DONE)
            self.assertEqual(core.ui_state(take_toasts=False)["toasts"], [])
            self.assertEqual(core.ui_state()["toasts"], [W.PAIR_DONE])
            self.assertEqual(core.ui_state()["toasts"], [])

    def test_runtime_file_crosses_processes(self):
        """Android: the :daemon service runs duties(), the screen's core reads what it found."""
        with _Home() as home:
            owner = offline_core(dict(ANDROID, node_error="boom"), home)
            owner.peer_state = {"1.2.3.4": "ok"}
            owner.settings.set(network_on=False)
            owner.duties()
            screen = offline_core(ANDROID, home)
            screen._read_runtime()
            self.assertEqual(screen.env.get("node_error"), "boom")


class _FakeGitHub(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps([{"tag_name": "v99.0.0-prealpha", "html_url": "https://example.test/r99"},
                           {"tag_name": "v0.1.0", "html_url": "x"}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class AgainstARealDaemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.d = Daemon(Path(cls.tmp.name), "core", env={"OMNI_LOCAL_TOKEN": "1", "OMNI_UPNP": "0"})
        cls.token_file = cls.d.home / ".omni" / "local_token"

        def feed_token():
            for _ in range(400):
                if cls.token_file.exists():
                    cls.d.start_headers["X-Omni-Local-Token"] = cls.token_file.read_text().strip()
                    return
                time.sleep(0.05)
        threading.Thread(target=feed_token, daemon=True).start()
        cls.d.start()
        cls.token = cls.token_file.read_text().strip()
        cls.gh = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGitHub)
        threading.Thread(target=cls.gh.serve_forever, daemon=True).start()
        cls.env = mock.patch.dict(os.environ, {
            "OMNI_TELEMETRY_PORT": str(cls.d.ports["OMNI_TELEMETRY_PORT"]), "OMNI_IPC_PORT": str(cls.d.ipc_port),
            "OMNI_LOCAL_TOKEN_FILE": str(cls.token_file), "OMNI_NODE_AUTO_UPDATE": "0",
            "OMNI_NODE_UPDATE_URL": f"http://127.0.0.1:{cls.gh.server_port}/releases", "OMNI_NODE_VERSION": "0.5.0"})
        cls.env.start()
        cls.h = {"X-Omni-Local-Token": cls.token}

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.gh.shutdown()
        cls.d.stop()
        cls.tmp.cleanup()

    def core(self, env):
        tmp = Path(tempfile.mkdtemp(dir=self.tmp.name))
        return C.NodeCore(env=env, settings=C.Settings(tmp / "s.json", C.profile(env)["defaults"]),
                          runtime_path=tmp / "rt.json")

    def test_desktop_sync_relay_router_and_peers(self):
        core = self.core(DESKTOP)
        core.settings.set(peers=["198.51.100.9"], relay_on=False, relay_mb_day=5120)
        out = core.sync()
        self.assertEqual(out["line"], W.LOOKING)
        self.assertEqual(out["node_id"], self.d.node_id)
        st = core.ui_state()
        self.assertEqual(st["daemon_version"], self.d.version)
        self.assertEqual(st["version_line"], f"Omni Node 0.5.0 · daemon v{self.d.version}")
        self.assertEqual(core.relay_sent, (False, 5120))
        self.assertEqual(core.peer_state, {"198.51.100.9": "ok"})
        self.assertEqual(st["peers"]["items"][0]["color"], W.GREEN)
        code, peers = self.d.get("/api/sync/peers", headers=self.h)
        self.assertIn("198.51.100.9", json.dumps(peers))
        self.assertIs(core.wan_sent, True)
        self.assertEqual(core.do("internet", {"on": False})["ok"], True)
        self.assertIs(core.wan_sent, False)

    def test_phone_policy_reaches_the_daemon(self):
        core = self.core(dict(ANDROID, charging=False))
        core.settings.set(relay_on=True, relay_mb_day=250)
        out = core.sync()
        self.assertFalse(out["relay_active"])
        self.assertEqual(core.relay_sent, (False, 250))       # phone not charging: relay told to stay off
        code, body = http(f"{self.d.ipc}/api/relay/status", headers=self.h)
        self.assertEqual(code, 200, body)
        self.assertFalse(body.get("enabled"))

    def test_pairing_and_token_actions(self):
        core = self.core(DESKTOP)
        core.refresh()
        self.assertEqual(core.ui_state()["pairing"]["quiet_line"], "Quiet mode: off until you pair a device")
        self.assertTrue(core.do("pair_offer")["ok"])
        offer = core.ui_state()["pairing"]["offer"]
        self.assertRegex(offer["code"], r"^\d{3} \d{3}$")
        self.assertTrue(offer["payload"].startswith("omni://pair?p="))
        self.assertTrue(core.do("copy_pair_text")["sensitive"])
        core.do("pair_cancel")
        self.assertIsNone(core.ui_state()["pairing"]["offer"])
        self.assertEqual(core.do("pair_accept", {"text": "12"})["toast"], W.PAIR_BAD_INPUT)
        self.assertFalse(core.do("pair_remove", {"pubkey": "ab" * 32})["ok"])
        out = core.do("copy_token")
        self.assertEqual((out["copy"], out["sensitive"], out["toast"]), (self.token, True, W.TOKEN_COPIED))
        self.assertEqual(core.do("token")["howto"], W.TOKEN_HOWTO)

    def test_encounters_section_and_actions(self):
        core = self.core(DESKTOP)
        core.refresh()
        en = core.ui_state()["encounters"]
        self.assertTrue(en["available"])
        self.assertFalse(en["on"])
        self.assertEqual(en["items"], [])
        out = core.do("encounters", {"on": True})
        self.assertEqual((out["ok"], out.get("ask_name")), (False, True))
        self.assertFalse(core.do("encounter_name", {"name": "me@example.com"})["ok"])
        self.assertTrue(core.do("encounters", {"on": True, "name": "Kiro"})["ok"])
        core.refresh()
        en = core.ui_state()["encounters"]
        self.assertEqual((en["on"], en["name"]), (True, "Kiro"))
        self.assertTrue(en["line"].startswith("On as Kiro"))
        self.assertTrue(core.do("encounter_name", {"name": "Kiro B"})["ok"])
        self.assertFalse(core.do("encounter_forget", {"id": "00" * 8})["ok"])
        self.assertTrue(core.do("encounters_forget_all")["ok"])
        self.assertTrue(core.do("encounters", {"on": False})["ok"])
        core.refresh()
        self.assertFalse(core.ui_state()["encounters"]["on"])
        self.assertEqual(W.encounter_line({"display_name": "Ana", "meet_count": 2, "last_met": 0}, 3 * 86400000),
                         "Ana  ·  met 2 times  ·  last 3 days ago")

    def test_check_updates(self):
        core = self.core(DESKTOP)
        out = core.do("check_updates")
        self.assertEqual((out["toast"], out["url"]), ("Omni Node 99.0.0 is available", "https://example.test/r99"))
        self.assertEqual(core.ui_state()["update"]["line"], "Omni Node 99.0.0 is available")

    def test_daemon_routes(self):
        code, body = self.d.get("/api/node/ui", headers=self.h)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"]["line"], W.LOOKING)
        self.assertEqual(body["words"]["TITLE"], W.TITLE)
        self.assertEqual(body["daemon_version"], self.d.version)
        self.assertEqual(self.d.get("/api/node/ui")[0], 401)                      # local token required
        ok = self.d.post("/api/node/action", {"action": "add_peer", "args": {"text": "198.51.100.20"}}, headers=self.h)
        self.assertEqual((ok[0], ok[1]["status"]), (200, "OK"), ok)
        self.assertEqual(self.d.post("/api/node/action", {"action": "network", "args": {"on": False}},
                                     headers=self.h)[0], 409)
        self.assertEqual(self.d.post("/api/node/action", {"action": "rm_rf"}, headers=self.h)[0], 400)
        self.assertEqual(self.d.post("/api/node/action", {"args": {}}, headers=self.h)[0], 400)
        self.assertEqual(self.d.post("/api/node/action", {"action": "relay", "args": []}, headers=self.h)[0], 400)
        self.assertEqual(self.d.post("/api/wan/config", {"upnp": "no"}, headers=self.h)[0], 400)
        code, body = self.d.post("/api/wan/config", {"upnp": False}, headers=self.h)
        self.assertEqual((code, body["upnp"]), (200, False))


class PackagingAndParity(unittest.TestCase):
    def test_android_packager_finds_the_modules(self):
        """Omni-Node-Daemon's fetch_daemon.sh copies modules found by grep -E '^(import|from) omni_...'."""
        text = (ADMIN / "omni_live_daemon_server.py").read_text(encoding="utf-8")
        found = set(re.findall(r"^(?:import|from) (omni_[a-z0-9_]+)", text, re.M))
        self.assertTrue({"omni_node_core", "omni_node_words"} <= found, found)

    def test_core_is_stdlib_only(self):
        tree = ast.parse((ADMIN / "omni_node_core.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertTrue(names <= {"json", "os", "re", "sys", "threading", "time", "urllib", "pathlib",
                                  "omni_node_words", "omni_node_version"}, names)   # the last is generated

    def test_desktop_shell_has_no_status_words(self):
        """The Tk shell renders ui_state(); a status sentence typed into it would drift from Android."""
        bad = shell_words_violations(ADMIN / "omni_node_desktop.py")
        self.assertEqual(bad, [], "user-facing words belong in omni_node_words.py: " + repr(bad))


def shell_words_violations(path: Path) -> list:
    """String literals in a Python shell that spell out Node words (docstrings and comments excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            docs.add(id(node.body[0].value))
    long_words = [v for v in W.all_words().values() if len(v) >= 10]
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            s = node.value
            if STATUS_PATTERNS.search(s) or any(w in s for w in long_words):
                bad.append(s)
    return bad


if __name__ == "__main__":
    unittest.main()
