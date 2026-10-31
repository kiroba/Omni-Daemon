"""Simulated mesh: four real daemons joined in a line through fault-injecting proxies.

Run: python -m unittest tests.test_mesh_sim -v

    A <-> B <-> C <-> D        (each <-> is a mesh_sim.Link: latency, connection drops, partitions)

No daemon can reach a non-neighbour, so an event posted on A only reaches D by hopping A->B->C->D
through catch-up sync (OMNI_CATCHUP_INTERVAL=1). That runs the paths a phone mesh depends on, without
any phones: multi-hop convergence over a lossy, slow link; a partition, writes on both sides, then
healing; a node going offline and coming back; repeated catch-ups storing no duplicates; an IPv6-only
hop handed in as a radio peer (the Wi-Fi Aware shape); and radio peer listing and refresh (TTL expiry
is unit-tested in test_radio_peers). Every daemon runs with a dead HTTP_PROXY set, so peer traffic that
leaks to a proxy fails loudly. Every stored event must still verify (content hash + Ed25519 signature) on every node.
"""
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_catchup_sync as tc  # noqa: E402  (Node, _http, _wait)
from mesh_sim import FaultProxy, Link, ipv6_loopback_ok  # noqa: E402

NAMES = ["SA", "SB", "SC", "SD"]
CONVERGE_S = 60


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _row_count(node):
    conn = sqlite3.connect(str(node.db))
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


class MeshSimTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="omni_mesh_sim_"))
        for n in NAMES:
            tc.PORTS[n] = {"TELEMETRY_PORT": _free_port(), "CONTINUITY_IPC_PORT": _free_port(),
                           "P2P_SYNC_PORT": _free_port(), "UDP_DISCOVERY_PORT": _free_udp_port()}
        p2p = [tc.PORTS[n]["P2P_SYNC_PORT"] for n in NAMES]
        cls.links = [Link(p2p[i], p2p[i + 1], seed=100 + 10 * i) for i in range(len(NAMES) - 1)]
        cls.nodes = []
        for i, name in enumerate(NAMES):
            node = tc.Node(name, cls.root, 1)
            peers = []
            if i > 0:
                peers.append(f"127.0.0.1:{cls.links[i - 1].to_a.port}")   # to my left neighbour
            if i < len(NAMES) - 1:
                peers.append(f"127.0.0.1:{cls.links[i].to_b.port}")       # to my right neighbour
            node.env["OMNI_SYNC_PEERS"] = ",".join(peers)
            node.env["OMNI_CATCHUP_INTERVAL"] = "1"
            # A dead HTTP proxy in the environment, like a corporate laptop or a Windows system proxy:
            # peer traffic must bypass it (v8.18.1), IPv6 hosts included.
            node.env["HTTP_PROXY"] = node.env["http_proxy"] = "http://127.0.0.1:9"
            cls.nodes.append(node)
        for node in cls.nodes:
            node.start()
        cls.A, cls.B, cls.C, cls.D = cls.nodes

    @classmethod
    def tearDownClass(cls):
        for node in cls.nodes:
            try:
                node.stop()
            except Exception:
                pass
        for link in cls.links:
            link.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        for link in self.links:
            link.set(latency=0.0, drop_rate=0.0, partitioned=False)

    # -- helpers --------------------------------------------------------------------------------
    def assertConverged(self, expected, nodes=None, timeout=CONVERGE_S, msg=""):
        nodes = nodes or self.nodes
        ok = tc._wait(lambda: all(expected <= n.ids() for n in nodes), timeout)
        if not ok:
            missing = {n.name: len(expected - n.ids()) for n in nodes}
            self.fail(f"no convergence in {timeout}s {msg}: missing per node {missing}")
        for n in nodes:
            self.assertEqual(n.all_signed_verify(), [], f"{n.name} holds an event that does not verify")
            self.assertEqual(_row_count(n), len(n.ids()), f"{n.name} stored a duplicate row")

    # -- scenarios ------------------------------------------------------------------------------
    def test_1_multihop_over_lossy_slow_links(self):
        """A and D are three hops apart; every hop adds 40 ms and resets 25% of connections."""
        for link in self.links:
            link.set(latency=0.04, drop_rate=0.25)
        t0 = time.time()
        ids = {self.A.post("from the left edge"), self.D.post("from the right edge"), self.B.post("middle")}
        self.assertConverged(ids, msg="over lossy links")
        dropped = sum(l.to_a.stats["dropped"] + l.to_b.stats["dropped"] for l in self.links)
        print(f"\n[mesh] 4 nodes, 3 lossy hops: converged in {time.time() - t0:.1f}s, "
              f"{dropped} connections dropped on the way", flush=True)
        self.assertGreater(dropped, 0, "the fault proxy never dropped anything; the test proved nothing")

    def test_2_partition_then_heal(self):
        """Cut B-C, write on both islands, check they really diverge, then heal and converge."""
        mid = self.links[1]
        mid.set(partitioned=True)
        time.sleep(1.5)
        left = {self.A.post("left island 1"), self.B.post("left island 2")}
        right = {self.C.post("right island 1"), self.D.post("right island 2")}
        self.assertConverged(left, nodes=[self.A, self.B], msg="inside the left island")
        self.assertConverged(right, nodes=[self.C, self.D], msg="inside the right island")
        time.sleep(3)  # several catch-up rounds while cut
        self.assertFalse(right & self.A.ids(), "an event crossed a partitioned link")
        self.assertFalse(left & self.D.ids(), "an event crossed a partitioned link")
        mid.set(partitioned=False)
        t0 = time.time()
        self.assertConverged(left | right, msg="after healing")
        print(f"\n[mesh] partition healed: all 4 nodes agree after {time.time() - t0:.1f}s", flush=True)

    def test_3_node_offline_then_back(self):
        """C goes offline (A-B and D keep writing), comes back, and catches up from both sides."""
        self.C.stop()
        try:
            a_id, d_id = self.A.post("while C sleeps"), self.D.post("D writes too")
            self.assertConverged({a_id}, nodes=[self.A, self.B], timeout=20, msg="A-B while C is down")
            self.assertNotIn(a_id, self.D.ids(), "D has no path to A while C is down")
        finally:
            self.C.start()
        ids = {a_id, d_id}
        self.assertConverged(ids, msg="after C came back")

    def test_4_repeated_catchup_stores_no_duplicates(self):
        eid = self.A.post("only once")
        self.assertConverged({eid})
        before = {n.name: _row_count(n) for n in self.nodes}
        for _ in range(3):
            for n in self.nodes:
                code, _ = n.catchup()
                self.assertEqual(code, 200)
        self.assertEqual({n.name: _row_count(n) for n in self.nodes}, before)

    def test_5_ipv6_only_radio_hop(self):
        """Add an extra A-D shortcut that exists only as an IPv6 radio peer (no OMNI_SYNC_PEERS entry)."""
        if not ipv6_loopback_ok():
            self.skipTest("no IPv6 loopback here")
        for link in self.links:
            link.set(partitioned=True)          # the wired line is down; only the radio hop is left
        to_d = FaultProxy(self.D.ports["P2P_SYNC_PORT"], listen_host="::1", seed=7).start()
        to_a = FaultProxy(self.A.ports["P2P_SYNC_PORT"], listen_host="::1", seed=8).start()
        try:
            to_d.latency = to_a.latency = 0.03
            for me, proxy in ((self.A, to_d), (self.D, to_a)):
                code, body = tc._http(f"{me.ipc}/api/sync/peers",
                                      {"host": "::1", "port": proxy.port, "transport": "wifi_aware"})
                self.assertEqual(code, 200, body)
            eid = self.A.post("straight to D over the IPv6 radio hop")
            self.assertConverged({eid}, nodes=[self.A, self.D], timeout=30, msg="over the IPv6 radio hop")
            self.assertGreater(to_d.stats["accepted"] + to_a.stats["accepted"], 0)
            self.assertNotIn(eid, self.B.ids(), "B should only get it once the line heals")
        finally:
            to_d.stop()
            to_a.stop()
        for link in self.links:
            link.set(partitioned=False)
        self.assertConverged({eid}, msg="after the line healed")

    def test_6_radio_peer_listing_and_refresh(self):
        """A handed-in radio peer is listed with its transport and a refresh is not 'new'."""
        port = self.links[0].to_b.port
        first = tc._http(f"{self.A.ipc}/api/sync/peers", {"host": "127.0.0.1", "port": port, "transport": "ble"})[1]
        again = tc._http(f"{self.A.ipc}/api/sync/peers", {"host": "127.0.0.1", "port": port, "transport": "ble"})[1]
        self.assertFalse(again["new"])
        listing = tc._http(f"{self.A.ipc}/api/sync/peers")[1]
        self.assertIn(("127.0.0.1", port, "ble"),
                      {(p["host"], p["port"], p["transport"]) for p in listing["radio"]})
        self.assertIn("new", first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
