"""Radio peer hand-off (daemon v8.18.0): an app finds a peer over BLE / Wi-Fi Aware and hands it to
its local daemon on loopback POST /api/sync/peers; the daemon then syncs with it.

Run: python -m unittest tests.test_radio_peers -v

TwoDaemonRadioTest starts two real daemons with NO OMNI_SYNC_PEERS and different UDP ports (so they
can't find each other on their own), hands each the other's P2P port the way the app would, and
checks an event posted on C reaches D. RadioPeerUnitTests imports the daemon in-process.
"""
import importlib
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_catchup_sync as tc  # noqa: E402  (Node, _http, _wait)

tc.PORTS["C"] = {"TELEMETRY_PORT": 39100, "CONTINUITY_IPC_PORT": 35000, "P2P_SYNC_PORT": 35005, "UDP_DISCOVERY_PORT": 39101}
tc.PORTS["D"] = {"TELEMETRY_PORT": 49100, "CONTINUITY_IPC_PORT": 45000, "P2P_SYNC_PORT": 45005, "UDP_DISCOVERY_PORT": 49101}


class TwoDaemonRadioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="omni_radio_test_"))
        cls.C = tc.Node("C", cls.root, 1)
        cls.D = tc.Node("D", cls.root, 1)
        for n in (cls.C, cls.D):
            n.env["OMNI_SYNC_PEERS"] = ""          # only the radio hand-off connects them
            n.env["OMNI_CATCHUP_INTERVAL"] = "2"
        cls.C.start()
        cls.D.start()

    @classmethod
    def tearDownClass(cls):
        for n in (cls.C, cls.D):
            try:
                n.stop()
            except Exception:
                pass
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_handoff_then_sync(self):
        eid = self.C.post("found you over wifi aware")
        self.assertNotIn(eid, self.D.ids())
        for me, other, transport in ((self.C, self.D, "wifi_aware"), (self.D, self.C, "ble")):
            code, body = tc._http(f"{me.ipc}/api/sync/peers",
                                  {"host": "127.0.0.1", "port": other.ports["P2P_SYNC_PORT"],
                                   "transport": transport, "node_hash": "ab" * 16})
            self.assertEqual(code, 200, body)
            self.assertTrue(body["new"])
        code, listing = tc._http(f"{self.C.ipc}/api/sync/peers")
        self.assertEqual(code, 200)
        self.assertEqual([p["transport"] for p in listing["radio"]], ["wifi_aware"])
        self.assertTrue(tc._wait(lambda: eid in self.D.ids(), 20), "event never reached D")
        self.assertEqual(self.D.all_signed_verify(), [])
        # refresh is not new
        code, body = tc._http(f"{self.C.ipc}/api/sync/peers",
                              {"host": "127.0.0.1", "port": self.D.ports["P2P_SYNC_PORT"], "transport": "wifi_aware"})
        self.assertEqual((code, body["new"]), (200, False))


class RadioPeerUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="omni_radio_unit_")
        cls.old = (os.environ.get("HOME"), os.getcwd())
        os.environ["HOME"] = cls.tmp
        os.environ.pop("OMNI_SYNC_PEERS", None)
        os.chdir(cls.tmp)
        sys.path.insert(0, str(tc.ADMIN))
        cls.d = importlib.import_module("omni_live_daemon_server")

    @classmethod
    def tearDownClass(cls):
        if cls.old[0] is not None:
            os.environ["HOME"] = cls.old[0]
        os.chdir(cls.old[1])
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.d.STATE.radio_peers.clear()

    def test_peer_url_ipv6(self):
        u = self.d.peer_url
        self.assertEqual(u("10.0.0.5", 5005, "/p2p/sync"), "http://10.0.0.5:5005/p2p/sync")
        self.assertEqual(u("fe80::1%aware_data0", 5005, "/x"), "http://[fe80::1%25aware_data0]:5005/x")
        self.assertEqual(u("[fd00::2]", 5005, "/x"), "http://[fd00::2]:5005/x")

    def test_validation(self):
        add = self.d.add_radio_peer
        self.assertEqual(add({"host": "", "port": 5005})[1], 400)
        self.assertEqual(add({"host": "evil host", "port": 5005})[1], 400)
        self.assertEqual(add({"host": "10.0.0.9", "port": 70000})[1], 400)
        self.assertEqual(add({"host": "10.0.0.9", "port": 5005, "transport": "carrier-pigeon"})[1], 400)
        self.assertEqual(add({"host": "127.0.0.1", "port": self.d.P2P_SYNC_PORT})[1], 400)  # itself
        self.assertEqual(add(["not", "a", "dict"])[1], 400)
        resp, code = add({"host": "fe80::1%aware_data0", "port": 5005, "transport": "wifi_aware"})
        self.assertEqual((code, resp["new"]), (200, True))

    def test_allowed_and_catchup_and_ttl(self):
        self.d.add_radio_peer({"host": "10.1.2.3", "port": 5005, "transport": "ble"})
        self.d.add_radio_peer({"host": "fe80::7%aware_data0", "port": 5005, "transport": "wifi_aware"})
        self.assertTrue(self.d.catchup_peer_allowed("10.1.2.3"))
        self.assertTrue(self.d.catchup_peer_allowed("fe80::7"))
        self.assertFalse(self.d.catchup_peer_allowed("10.9.9.9"))
        self.assertIn(("10.1.2.3", 5005), self.d.catchup_peers())
        for v in self.d.STATE.radio_peers.values():
            v["last_seen"] -= self.d.RADIO_PEER_TTL + 1
        self.assertFalse(self.d.catchup_peer_allowed("10.1.2.3"))
        self.assertNotIn(("10.1.2.3", 5005), self.d.catchup_peers())

    def test_not_remote_reachable(self):
        self.assertNotIn("/api/sync/peers", self.d.REMOTE_POST_PATHS)
        self.assertNotIn("/api/sync/peers", self.d.REMOTE_GET_PATHS)


if __name__ == "__main__":
    unittest.main()
