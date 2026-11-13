"""BLE introductions (daemon v8.33.0): a completed GATT exchange hands the daemon a
full-key introduction signed over the omni.p2p.hello.v1 bytes. Verified introductions
are stored persistently (BleMetStore, ~/.omni/ble_met.json), register their endpoints
through the radio-peer path, and join the catch-up dial set - a bad signature, a
wrong mesh or a stale hello records nothing, and a sighting alone still makes no met.

Run: python -m unittest tests/test_ble_met.py -v
The live part reuses the telemetry test harness (real daemon subprocess).
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ROOT / "Admin"
sys.path.insert(0, str(ADMIN))
sys.path.insert(0, str(ROOT / "tests"))

import omni_ble_discovery_engine as ble  # noqa: E402
from test_telemetry_summary import Daemon, _http  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


def hello_bytes(node_id: str, mesh: str, ts: int) -> bytes:
    """The omni.p2p.hello.v1 bytes, exactly as omni_live_daemon_server.hello_bytes
    builds them (a GATT client signs these; if the daemon's format ever changes,
    the live verification below fails loudly, which is the point)."""
    return json.dumps({"type": "omni.p2p.hello.v1", "node_id": node_id, "mesh": mesh, "ts": ts},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def make_peer():
    sk = Ed25519PrivateKey.generate()
    pub = sk.public_key().public_bytes(serialization.Encoding.Raw,
                                       serialization.PublicFormat.Raw).hex()
    return sk, pub


def intro(sk, pub, mesh, endpoints=None, ts=None, name="Peer"):
    ts = int(time.time() * 1000) if ts is None else ts
    return {"node_id": pub, "mesh_id": mesh, "name": name,
            "endpoints": endpoints if endpoints is not None else [{"host": "192.0.2.77", "port": 5005}],
            "ts": ts, "sig": sk.sign(hello_bytes(pub, mesh, ts)).hex()}


class BleMetStoreTests(unittest.TestCase):
    def test_record_get_and_dial_window(self):
        st = ble.BleMetStore()
        _, pub = make_peer()
        now = time.time()
        st.record(pub, "mesh", "Peer", [{"host": "192.0.2.1", "port": 5005}], ts=now - 25 * 3600)
        self.assertEqual(st.dial_targets(now=now), [])                      # outside the 24 h window
        st.record(pub, "mesh", "Peer", [{"host": "192.0.2.1", "port": 5005}], ts=now)
        self.assertEqual(st.dial_targets(now=now), [("192.0.2.1", 5005, pub)])
        rec = st.get(pub)
        self.assertEqual(rec["first_met"], rec["last_met"] - 25 * 3600)      # first meeting kept
        self.assertEqual(st.count(), 1)

    def test_persistence_survives_reload(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".omni" / "ble_met.json"
            _, pub = make_peer()
            ble.BleMetStore(path).record(pub, "mesh", "Peer", [{"host": "192.0.2.2", "port": 5005}])
            self.assertTrue(path.exists())
            again = ble.BleMetStore(path)
            self.assertEqual(again.count(), 1)
            self.assertEqual(again.get(pub)["name"], "Peer")

    def test_store_bounds_evict_least_recently_met(self):
        st = ble.BleMetStore(max_entries=2)
        pubs = [make_peer()[1] for _ in range(3)]
        for i, pub in enumerate(pubs):
            st.record(pub, "mesh", "P", [], ts=1000.0 + i)
        self.assertEqual(st.count(), 2)
        self.assertIsNone(st.get(pubs[0]))
        self.assertIsNotNone(st.get(pubs[2]))


class BleMetDaemonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="omni_blemet_"))
        cls.d = Daemon(cls.tmp, "blemet")
        cls.d.start()
        cls.d.tel = f"http://127.0.0.1:{cls.d.ports['OMNI_TELEMETRY_PORT']}"

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def met_count(self):
        code, health, _ = _http(f"{self.d.tel}/api/v1/telemetry/node-health")
        self.assertEqual(code, 200)
        return health["ble"]["met"]["count"], health

    def test_verified_intro_registers_and_persists(self):
        before, _ = self.met_count()
        sk, pub = make_peer()
        code, body, _ = _http(f"{self.d.ipc}/api/ble/met", intro(sk, pub, self.d.mesh))
        self.assertEqual(code, 200, body)
        self.assertTrue(body["stored"])
        self.assertEqual(body["registered"], 1)            # endpoint went through the radio-peer path
        after, health = self.met_count()
        self.assertEqual(after, before + 1)
        self.assertIn(pub[:32], health["ble"]["met"]["prefixes"])
        saved = Path(self.d.home) / ".omni" / "ble_met.json"
        self.assertTrue(saved.exists())
        self.assertIn(pub, saved.read_text())
        code, s, _ = self.d.summary()
        self.assertEqual(self.d.proto(s, "ble_discovery")["counts"]["met"], after)

    def test_bad_signature_refused(self):
        before, _ = self.met_count()
        sk, pub = make_peer()
        other, _ = make_peer()
        body_in = intro(sk, pub, self.d.mesh)
        body_in["sig"] = other.sign(hello_bytes(pub, self.d.mesh, body_in["ts"])).hex()
        code, body, _ = _http(f"{self.d.ipc}/api/ble/met", body_in)
        self.assertEqual(code, 401, body)
        after, _ = self.met_count()
        self.assertEqual(after, before)

    def test_wrong_mesh_and_stale_hello_refused(self):
        before, _ = self.met_count()
        sk, pub = make_peer()
        code, body, _ = _http(f"{self.d.ipc}/api/ble/met", intro(sk, pub, "some-other-mesh"))
        self.assertEqual(code, 403, body)
        sk2, pub2 = make_peer()
        stale = intro(sk2, pub2, self.d.mesh, ts=int(time.time() * 1000) - 10 * 60 * 1000)
        code, body, _ = _http(f"{self.d.ipc}/api/ble/met", stale)
        self.assertEqual(code, 400, body)
        after, _ = self.met_count()
        self.assertEqual(after, before)

    def test_own_introduction_endpoint_signs_correctly(self):
        code, body, _ = _http(f"{self.d.ipc}/api/ble/introduction")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["node_id"], self.d.node_id)
        self.assertEqual(body["mesh_id"], self.d.mesh)
        # the served signature must verify against the daemon's own key - it is
        # what a peer's daemon will check when the introduction arrives via /api/ble/met
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.d.node_id)).verify(
            bytes.fromhex(body["sig"]), hello_bytes(body["node_id"], body["mesh_id"], body["ts"]))

    def test_summary_reports_met_accepted_and_rejected(self):
        # Mesh-Monitor's live BLE tile reads the telemetry summary (not node-health):
        # it must carry the full met picture - stored, accepted and rejected - so a
        # refused introduction is visible in the app, not only in the daemon.
        def met_counts():
            code, s, _ = self.d.summary()
            self.assertEqual(code, 200, s)
            return self.d.proto(s, "ble_discovery")["counts"]

        before = met_counts()
        sk, pub = make_peer()
        code, body, _ = _http(f"{self.d.ipc}/api/ble/met", intro(sk, pub, self.d.mesh))
        self.assertEqual(code, 200, body)
        other, _ = make_peer()
        bad = intro(sk, pub, self.d.mesh)
        bad["sig"] = other.sign(hello_bytes(pub, self.d.mesh, bad["ts"])).hex()
        code, body, _ = _http(f"{self.d.ipc}/api/ble/met", bad)
        self.assertEqual(code, 401, body)
        after = met_counts()
        self.assertEqual(after["met"], before["met"] + 1)
        self.assertEqual(after["met_accepted"], before["met_accepted"] + 1)
        self.assertEqual(after["met_rejected"], before["met_rejected"] + 1)

    def test_sighting_alone_makes_no_met(self):
        before, _ = self.met_count()
        _, pub = make_peer()
        code, body, _ = _http(f"{self.d.ipc}/api/ble/nearby", {"seen": [pub[:32]]})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["kept"], 1)
        after, _ = self.met_count()
        self.assertEqual(after, before)                    # a hint is still not a meeting


if __name__ == "__main__":
    unittest.main()
