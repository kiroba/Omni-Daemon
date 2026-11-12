"""BLE discovery (daemon v8.32.0): the engine speaks the NodeRadio wire format
(service UUID 0xFFA0, service data = first 16 bytes of the node pubkey), the
registry keeps hash-only sightings in memory, and the live daemon serves
POST :5000/api/ble/nearby + node-health "ble" without ever making a peer out
of a sighting.

Run: python -m unittest tests/test_ble_discovery.py -v
The live part reuses the telemetry test harness (real daemon subprocess).
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ROOT / "Admin"
sys.path.insert(0, str(ADMIN))
sys.path.insert(0, str(ROOT / "tests"))

import omni_ble_discovery_engine as ble  # noqa: E402
from test_telemetry_summary import Daemon, _http  # noqa: E402
import tempfile  # noqa: E402

NODE_ID = "b0ad355fd5e6aa11" + "22" * 24          # 64 hex chars, like a pubkey
PEER_PREFIX = "aa" * 16


class BleEngineTests(unittest.TestCase):
    def test_wire_format_matches_noderadio(self):
        frame = ble.encode_service_data(NODE_ID)
        self.assertEqual(len(frame), 16)
        self.assertEqual(frame.hex(), NODE_ID[:32])       # first 16 key bytes, nothing else
        self.assertEqual(ble.decode_service_data(frame), NODE_ID[:32])
        self.assertEqual(ble.OMNI_BLE_SERVICE_UUID, 0xFFA0)

    def test_decode_refuses_short_and_junk(self):
        self.assertIsNone(ble.decode_service_data(b"\x01" * 15))
        self.assertIsNone(ble.decode_service_data("not bytes"))
        self.assertIsNone(ble.decode_service_data(None))

    def test_key_prefix_mirrors_kotlin(self):
        self.assertEqual(ble.key_prefix_hex("AB" + "cd" * 31), "ab" + "cd" * 15)   # first 16 bytes only
        self.assertEqual(ble.key_prefix_hex("abcd"), "abcd" + "0" * 28)   # zero-padded right

    def test_registry_keeps_peers_refuses_own_and_junk(self):
        reg = ble.BleNearbyRegistry(own_node_id=NODE_ID)
        self.assertFalse(reg.note(NODE_ID[:32]))          # own advertisement
        self.assertFalse(reg.note("zz" * 16))             # not hex
        self.assertFalse(reg.note("aa" * 8))              # too short
        self.assertTrue(reg.note(PEER_PREFIX))
        snap = reg.snapshot()
        self.assertEqual(snap["nearby"], 1)
        self.assertEqual(snap["prefixes"], [PEER_PREFIX])
        self.assertEqual(snap["rejected"], 3)

    def test_registry_expires_and_bounds(self):
        reg = ble.BleNearbyRegistry(ttl_s=90.0, max_entries=2)
        reg.note("aa" * 16, ts=1000.0)
        reg.note("bb" * 16, ts=1001.0)
        reg.note("cc" * 16, ts=1002.0)
        self.assertEqual(reg.snapshot(now=1003.0)["prefixes"], ["cc" * 16, "bb" * 16])
        self.assertEqual(reg.snapshot(now=2000.0)["nearby"], 0)   # all past the 90 s TTL


class BleDaemonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="omni_ble_"))
        cls.d = Daemon(cls.tmp, "ble")
        cls.d.start()
        cls.d.tel = f"http://127.0.0.1:{cls.d.ports['OMNI_TELEMETRY_PORT']}"

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_endpoint_health_and_no_peer_creation(self):
        code, body, _ = _http(f"{self.d.ipc}/api/ble/nearby",
                              {"seen": [PEER_PREFIX, "junk", self.d.node_id[:32]]})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["kept"], 1)                  # peer kept; junk + own refused
        self.assertEqual(body["nearby"], 1)
        code, health, _ = _http(f"{self.d.tel}/api/v1/telemetry/node-health")
        self.assertEqual(code, 200)
        self.assertEqual(health["ble"]["nearby"], 1)
        self.assertEqual(health["ble"]["prefixes"], [PEER_PREFIX])
        self.assertIn("ble_discovery", health["features"])
        self.assertEqual(health["active_peers"], 0)       # a sighting is NOT a peer
        code, s, _ = self.d.summary()
        proto = self.d.proto(s, "ble_discovery")
        self.assertEqual(proto["nearby"], 1)
        self.assertEqual(proto["counts"]["reports"], 1)
        self.assertEqual(s["capabilities"]["ble_discovery"]["state"], "wired")

    def test_endpoint_rejects_bad_bodies(self):
        code, body, _ = _http(f"{self.d.ipc}/api/ble/nearby", {"seen": "nope"})
        self.assertEqual(code, 400, body)
        code, body, _ = _http(f"{self.d.ipc}/api/ble/nearby", {"seen": ["aa"] * 65})
        self.assertEqual(code, 400, body)


if __name__ == "__main__":
    unittest.main()
