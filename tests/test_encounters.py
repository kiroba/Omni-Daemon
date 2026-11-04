"""Encounters (daemon v8.29.0): opt-in "StreetPass"-style meetings over the LAN path, plus swarm healing.
Run: python3 -m unittest tests.test_encounters

Unit tests import the daemon in-process (tests/daemon_harness.daemon_module). TwoDaemonEncounters starts
two real daemons on free temp ports (tests/daemon_harness.Daemon, the same env-port pattern as
tests/test_catchup_sync.py) on one mesh, with OMNI_OPEN_BEACON_TARGETS pointing each one's open UDP
beacons at the other's UDP port, since a loopback-only sandbox has no shared broadcast domain.
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daemon_harness import Daemon, daemon_module, http, now_ms  # noqa: E402
from daemon_version import DAEMON_VERSION  # noqa: E402

MESH = "enc-test"


def _wait(pred, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return pred()


class Unit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = daemon_module()

    def setUp(self):
        e = self.m.ENCOUNTERS
        self._saved = (e.enabled, e.display_name)

    def tearDown(self):
        e = self.m.ENCOUNTERS
        e.enabled, e.display_name = self._saved

    def test_version_and_feature(self):
        self.assertEqual(self.m.VERSION, DAEMON_VERSION)
        self.assertEqual(self.m.VERSION, "8.29.0")
        self.assertIn("encounters", self.m.NODE_FEATURES)

    def test_default_off_and_no_enc_in_beacon(self):
        store = self.m.EncounterStore(Path(tempfile.mkdtemp()) / "e.json", Path(tempfile.mkdtemp()) / "k")
        self.assertFalse(store.enabled)
        self.assertEqual(store.display_name, "")
        self.m.ENCOUNTERS.enabled = False
        self.assertNotIn("enc", self.m.build_beacon())
        self.assertIsNone(self.m.encounter_beacon_id())

    def test_name_sanitization(self):
        s = self.m.sanitize_display_name
        self.assertEqual(s("  Kiro \t  Bash  "), "Kiro Bash")
        self.assertEqual(s("Ki\u200bro\u202e\x07"), "Kiro")             # zero-width, bidi override, BEL
        self.assertEqual(s("Ame\u0301lie"), "Am\u00e9lie")                 # NFC
        self.assertEqual(s("🌀 Omni"), "🌀 Omni")
        self.assertEqual(s("x" * 32), "x" * 32)
        for bad in ("", "   ", "\u200b", "x" * 33, None, 5, "me@example.com", "call 314 555 0100",
                    "+13146497994"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                s(bad)
        self.assertEqual(s("Player 123"), "Player 123")                    # a few digits are fine

    def test_card_verification(self):
        store = self.m.EncounterStore(Path(tempfile.mkdtemp()) / "e.json", Path(tempfile.mkdtemp()) / "k")
        store.display_name = "Jazz"
        card = store.my_card()
        self.assertEqual(set(card), {"schema", "display_name", "card_pubkey", "ts", "sig"})
        self.assertNotEqual(card["card_pubkey"], self.m.STATE.node_id)   # never the node key
        self.assertEqual(self.m.verify_encounter_card(card), card)
        for mutate in (lambda c: c.update(ip="10.0.0.5"), lambda c: c.update(location="x"),
                       lambda c: c.update(display_name="Jazzy"), lambda c: c.update(schema=2),
                       lambda c: c.update(ts=now_ms() + 3600_000), lambda c: c.pop("sig")):
            c = dict(card)
            mutate(c)
            with self.subTest(c=c), self.assertRaises(ValueError):
                self.m.verify_encounter_card(c)
        # A card signed over an unsanitized name is refused too.
        c = dict(card, display_name="Jazz  ")
        c["sig"] = store.key().sign(self.m.encounter_card_bytes(c)).hex()
        with self.assertRaises(ValueError):
            self.m.verify_encounter_card(c)

    def test_id_rotation(self):
        store = self.m.EncounterStore(Path(tempfile.mkdtemp()) / "e.json", Path(tempfile.mkdtemp()) / "k")
        t0 = time.time()
        a = store.beacon_id(t0)
        self.assertEqual(store.beacon_id(t0 + 1), a)                         # stable inside the window
        b = store.beacon_id(t0 + self.m.ENCOUNTER_ROTATE_S + 1)
        self.assertNotEqual(a, b)
        self.assertTrue(store.is_my_beacon_id(a) and store.is_my_beacon_id(b))   # previous kept for grace
        c = store.beacon_id(t0 + 2 * self.m.ENCOUNTER_ROTATE_S + 2)
        self.assertFalse(store.is_my_beacon_id(a))
        for x in (a, b, c):
            self.assertRegex(x, r"^[0-9a-f]{32}$")
            self.assertNotIn(x, self.m.STATE.node_id + store.pubkey())
        self.assertEqual(len({a, b, c}), 3)
        self.assertEqual(self.m.ENCOUNTER_ROTATE_S, 900.0)                  # default 15 min

    def test_beacon_carries_enc_only_when_on_and_still_verifies(self):
        e = self.m.ENCOUNTERS
        e.enabled, e.display_name = True, "Tester"
        msg = self.m.build_beacon()
        self.assertRegex(msg["enc"], r"^[0-9a-f]{32}$")
        self.assertTrue(self.m.beacon_signature_ok(msg))
        for k in ("display_name", "card_pubkey", "location", "model"):
            self.assertNotIn(k, msg)

    def test_hello_bound_to_our_beacon_id(self):
        e = self.m.ENCOUNTERS
        e.enabled, e.display_name = True, "Server"
        peer = self.m.EncounterStore(Path(tempfile.mkdtemp()) / "e.json", Path(tempfile.mkdtemp()) / "k")
        peer.display_name = "Peer"
        good = peer.my_hello(e.beacon_id())
        resp, code = self.m.answer_encounter({"schema": 1, "hello": good}, "127.0.0.1")
        self.assertEqual(code, 200, resp)
        self.assertTrue(resp["need_card"])
        self.assertNotIn("card", resp)                                       # no name unless asked
        bad = peer.my_hello("f" * 32)                                        # bound to someone else's id
        self.assertEqual(self.m.answer_encounter({"schema": 1, "hello": bad}, "127.0.0.1")[1], 401)
        e.enabled = False
        self.assertEqual(self.m.answer_encounter({"schema": 1, "hello": good}, "127.0.0.1")[1], 404)

    def test_catchup_rate_limited_per_peer(self):
        m = self.m
        calls = []
        real = m.reconcile_with_peer
        m.reconcile_with_peer = lambda h, p: calls.append((h, p)) or {"stored": 0, "pushed": 0, "error": None}
        try:
            self.assertEqual(m.encounter_heal("10.9.8.7", 5005)["status"], "OK")
            self.assertEqual(m.encounter_heal("10.9.8.7", 5005)["status"], "RATE_LIMITED")
            self.assertEqual(m.encounter_heal("10.9.8.8", 5005)["status"], "OK")   # other peer: own budget
        finally:
            m.reconcile_with_peer = real
        self.assertEqual(calls, [("10.9.8.7", 5005), ("10.9.8.8", 5005)])
        self.assertEqual(m.ENCOUNTER_CATCHUP_GAP_S, 300.0)

    def test_settings_validation_and_loopback_only(self):
        store = self.m.EncounterStore(Path(tempfile.mkdtemp()) / "e.json", Path(tempfile.mkdtemp()) / "k")
        self.assertEqual(store.settings({"enabled": True})[1], 400)          # needs a name first
        self.assertEqual(store.settings({"enabled": "yes", "display_name": "A"})[1], 400)
        self.assertEqual(store.settings({"display_name": "x" * 33})[1], 400)
        self.assertEqual(store.settings({"phone": "1"})[1], 400)
        resp, code = store.settings({"enabled": True, "display_name": " Kiro "})
        self.assertEqual((code, resp["enabled"], resp["display_name"]), (200, True, "Kiro"))
        for p in ("/api/encounters", "/api/encounters/settings"):
            self.assertNotIn(p, self.m.REMOTE_GET_PATHS)
            self.assertNotIn(p, self.m.REMOTE_POST_PATHS)


class TwoDaemonEncounters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="omni_encounters_"))
        env = {"OMNI_MESH_ID": MESH, "OMNI_ENCOUNTER_ROTATE_S": "2", "OMNI_ENCOUNTER_RETRY_S": "1",
               "OMNI_QUIET_MESH": "off"}
        cls.A = Daemon(cls.root, "a", env=dict(env))
        cls.B = Daemon(cls.root, "b", env=dict(env))
        cls.A.env["OMNI_OPEN_BEACON_TARGETS"] = f"127.0.0.1:{cls.B.ports['OMNI_UDP_PORT']}"
        cls.B.env["OMNI_OPEN_BEACON_TARGETS"] = f"127.0.0.1:{cls.A.ports['OMNI_UDP_PORT']}"
        cls.A.start()
        cls.B.start()

    @classmethod
    def tearDownClass(cls):
        for d in (cls.A, cls.B):
            d.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def enc(self, d):
        code, body = d.get("/api/encounters")
        self.assertEqual(code, 200, body)
        return body

    def store_file(self, d) -> Path:
        return d.home / ".omni" / "encounters.json"

    def test_flow(self):
        A, B = self.A, self.B
        # --- 1. Default off: peers discover each other, but nothing is exchanged or stored.
        self.assertTrue(_wait(lambda: http(f"{A.tel}/api/v1/telemetry/node-health")[1]["active_peers"] >= 1))
        h = http(f"{A.tel}/api/v1/telemetry/node-health")[1]
        self.assertEqual(h["version"], "8.29.0")
        self.assertIn("encounters", h["features"])
        time.sleep(4)
        for d in (A, B):
            body = self.enc(d)
            self.assertEqual((body["enabled"], body["count"], body["handshakes"]["ok"]), (False, 0, 0))
        p2p_b = f"http://127.0.0.1:{B.ports['OMNI_P2P_PORT']}/p2p/encounter"
        self.assertEqual(http(p2p_b, {"schema": 1}, headers={"X-Omni-Mesh": MESH})[0], 404)   # off: 404
        self.assertEqual(http(p2p_b, {"schema": 1})[0], 403)                                   # mesh gate

        # Events A has before the meeting: healing must bring them to B.
        before = [A.post("/api/events/append", {"action": "CHAT", "payload": {"text": f"pre-{i}"}})[1]["event_id"]
                  for i in range(3)]

        # --- 2. Settings validation over HTTP.
        self.assertEqual(A.post("/api/encounters/settings", {"enabled": True})[0], 400)
        self.assertEqual(A.post("/api/encounters/settings", {"display_name": "y" * 33})[0], 400)
        code, s = A.post("/api/encounters/settings", {"enabled": True, "display_name": "  Kiro\u200b  Bash "})
        self.assertEqual((code, s["display_name"]), (200, "Kiro Bash"), s)
        time.sleep(4)
        self.assertEqual(self.enc(A)["count"], 0)              # only one side opted in: nothing happens
        code, s = B.post("/api/encounters/settings", {"enabled": True, "display_name": "Jazz"})
        self.assertEqual(code, 200, s)

        # --- 3. First meeting: each side has exactly the other's card.
        self.assertTrue(_wait(lambda: self.enc(A)["count"] == 1 and self.enc(B)["count"] == 1),
                        (self.enc(A), self.enc(B)))
        ea, eb = self.enc(A)["encounters"][0], self.enc(B)["encounters"][0]
        self.assertEqual(ea["display_name"], "Jazz")
        self.assertEqual(eb["display_name"], "Kiro Bash")
        self.assertEqual(set(ea), {"id", "display_name", "card_pubkey", "first_met", "last_met", "meet_count"})
        self.assertEqual(ea["meet_count"], 1)
        self.assertNotIn(ea["card_pubkey"], (A.node_id, B.node_id))

        # --- 4. Swarm healing: B pulled A's earlier events without OMNI_SYNC_PEERS or the periodic loop.
        def b_has_all():
            feed = B.get("/api/events/feed", {"limit": 200})[1]
            return set(before) <= {e["event_id"] for e in feed["events"]}
        self.assertTrue(_wait(b_has_all), self.enc(B))
        self.assertGreaterEqual(self.enc(B)["healing"]["runs"], 1)

        # --- 5. Ids rotate every 2 s here: more handshakes, but catch-up stays rate-limited (300 s).
        ok0 = self.enc(B)["handshakes"]["ok"]
        self.assertTrue(_wait(lambda: self.enc(B)["handshakes"]["ok"] >= ok0 + 2, timeout=40), self.enc(B))
        self.assertTrue(_wait(lambda: self.enc(B)["healing"]["rate_limited"] >= 1), self.enc(B))
        hb = self.enc(B)
        self.assertEqual(hb["healing"]["runs"], 1, hb)
        self.assertEqual(hb["encounters"][0]["meet_count"], 1)     # same meeting still going on
        self.assertEqual(hb["handshakes"]["cards_received"], 1)    # the name crossed once

        # --- 6. Privacy: the stored record holds only card + times + count; no IP/port/location/node key.
        for d, other in ((A, B), (B, A)):
            raw = self.store_file(d).read_text(encoding="utf-8")
            data = json.loads(raw)
            self.assertEqual(set(data), {"schema", "enabled", "display_name", "encounters"})
            for rec in data["encounters"]:
                self.assertEqual(set(rec), {"card", "first_met", "last_met", "meet_count"})
                self.assertEqual(set(rec["card"]), {"schema", "display_name", "card_pubkey", "ts", "sig"})
            for needle in ("127.0.0.1", "\"ip\"", "\"host\"", "\"port\"", "location", "model", other.node_id,
                           str(other.ports["OMNI_P2P_PORT"])):
                self.assertNotIn(needle, raw)

        # --- 7. Cards never reach the feed, the event DB or catch-up.
        for d in (A, B):
            feed = json.dumps(d.get("/api/events/feed", {"limit": 200})[1])
            self.assertNotIn("Jazz", feed)
            self.assertNotIn("Kiro Bash", feed)
            conn = sqlite3.connect(str(d.db))
            try:
                rows = json.dumps(conn.execute("SELECT action, payload_json FROM events").fetchall())
            finally:
                conn.close()
            self.assertNotIn("Jazz", rows)
            self.assertNotIn("Kiro Bash", rows)
            self.assertNotIn("encounter", rows.lower())

        # --- 8. Forget endpoints (turn off first so a new meeting can't race the check).
        self.assertEqual(A.post("/api/encounters/settings", {"enabled": False})[0], 200)
        rid = self.enc(A)["encounters"][0]["id"]
        self.assertEqual(http(f"{A.ipc}/api/encounters/nothex", method="DELETE")[0], 400)
        self.assertEqual(http(f"{A.ipc}/api/encounters/{'0' * 16}", method="DELETE")[0], 404)
        code, r = http(f"{A.ipc}/api/encounters/{rid}", method="DELETE")
        self.assertEqual((code, r["forgotten"]), (200, 1))
        self.assertEqual(http(f"{A.ipc}/api/encounters/{rid}", method="DELETE")[0], 404)
        self.assertEqual(self.enc(A)["count"], 0)
        self.assertEqual(json.loads(self.store_file(A).read_text())["encounters"], [])
        self.assertEqual(B.post("/api/encounters/settings", {"enabled": False})[0], 200)
        code, r = http(f"{B.ipc}/api/encounters", method="DELETE")
        self.assertEqual((code, r["forgotten"]), (200, 1))
        self.assertEqual(self.enc(B)["count"], 0)
        # Settings survive a restart; the beacon id is fresh per run.
        B.restart()
        self.assertEqual((self.enc(B)["enabled"], self.enc(B)["display_name"]), (False, "Jazz"))


if __name__ == "__main__":
    unittest.main()
