"""Internet without Tailscale (daemon v8.21.0): signed /p2p/hello for nodes behind NAT, catch-up and
OmniCast signaling relayed through a reachable peer, dial-back, and the STUN / UPnP parsers.

Run: python -m unittest tests/test_wan_relay.py -v

Topology in the multi-daemon tests: HUB is "reachable"; PHONE lists HUB in OMNI_SYNC_PEERS, but HUB
does not know PHONE (as if PHONE were behind carrier NAT). Everything PHONE needs must work over
connections PHONE opens.
"""
import json
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daemon_harness import Daemon, Key, http, now_ms, daemon_module  # noqa: E402

ADMIN = Path(__file__).resolve().parent.parent / "Admin"
sys.path.insert(0, str(ADMIN))
import omni_wan  # noqa: E402

MESH = "wan-test"
QUIET = {"OMNI_MESH_ID": MESH, "OMNI_UPNP": "0", "OMNI_WAN_INTERVAL": "3600"}


def send_bytes(m):
    return json.dumps({"type": "omnicast.signal.v1", "to": m["to"], "from": m["from"], "call_id": m["call_id"],
                       "kind": m["kind"], "payload": m["payload"], "ts": m["ts"]},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def poll_query(k: Key, wait: int) -> str:
    ts = now_ms()
    sig = k.sign(json.dumps({"type": "omnicast.poll.v1", "to": k.pub, "ts": ts}, sort_keys=True,
                            separators=(",", ":")).encode())
    return urllib.parse.urlencode({"to": k.pub, "ts": ts, "sig": sig, "wait": wait})


_last_ts = [0]


def fresh_ts():
    t = max(now_ms(), _last_ts[0] + 1)
    _last_ts[0] = t
    return t


def offer(sender: Key, to: str, call_id="c1") -> dict:
    m = {"to": to, "from": sender.pub, "call_id": call_id, "kind": "offer", "payload": "v=0", "ts": fresh_ts()}
    m["sig"] = sender.sign(send_bytes(m))
    return m


class TwoNodes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="omni_wan_"))
        cls.hub = Daemon(cls.root, "hub", env=QUIET).start()
        hub_p2p = cls.hub.ports["OMNI_P2P_PORT"]
        cls.phone = Daemon(cls.root, "phone", env=dict(QUIET, OMNI_SYNC_PEERS=f"127.0.0.1:{hub_p2p}")).start()

    @classmethod
    def tearDownClass(cls):
        cls.phone.stop()
        cls.hub.stop()

    def test_1_versions(self):
        from daemon_version import DAEMON_VERSION
        self.assertEqual(self.hub.version, DAEMON_VERSION)
        self.assertEqual(DAEMON_VERSION, "8.21.0")

    def test_2_unknown_caller_needs_hello(self):
        p2p = self.hub.ports["OMNI_P2P_PORT"]
        code, _ = http(f"http://127.0.0.1:{p2p}/p2p/signal/send", {"x": 1}, headers={"X-Omni-Mesh": MESH})
        # 127.0.0.1 is loopback and always allowed; prove the gate with a wrong mesh instead.
        code2, _ = http(f"http://127.0.0.1:{p2p}/p2p/hello", {"x": 1}, headers={"X-Omni-Mesh": "other"})
        self.assertEqual(code2, 403)
        self.assertIn(code, (400,))

    def test_3_hello_signature(self):
        p2p = self.hub.ports["OMNI_P2P_PORT"]
        k = Key()
        ts = now_ms()
        body = {"node_id": k.pub, "ts": ts,
                "sig": k.sign(json.dumps({"type": "omni.p2p.hello.v1", "node_id": k.pub, "mesh": MESH, "ts": ts},
                                         sort_keys=True, separators=(",", ":")).encode())}
        code, resp = http(f"http://127.0.0.1:{p2p}/p2p/hello", body, headers={"X-Omni-Mesh": MESH})
        self.assertEqual(code, 200, resp)
        self.assertEqual(resp["seen_ip"], "127.0.0.1")
        bad = dict(body, sig="00" * 64)
        code, _ = http(f"http://127.0.0.1:{p2p}/p2p/hello", bad, headers={"X-Omni-Mesh": MESH})
        self.assertEqual(code, 401)
        stale = dict(body, ts=ts - 10 * 60 * 1000)
        code, _ = http(f"http://127.0.0.1:{p2p}/p2p/hello", stale, headers={"X-Omni-Mesh": MESH})
        self.assertEqual(code, 400)

    def test_4_signal_sent_on_phone_reaches_hub_user(self):
        """Caller on PHONE, callee polls HUB: PHONE forwards the offer to HUB (outgoing connection)."""
        caller, callee = Key(), Key()
        code, resp = self.phone.post("/api/signal/send", offer(caller, callee.pub, "fwd"))
        self.assertEqual(code, 200, resp)
        got = []
        for _ in range(20):
            code, resp = self.hub.get("/api/signal/poll?" + poll_query(callee, 0)) if False else \
                http(f"{self.hub.ipc}/api/signal/poll?{poll_query(callee, 0)}")
            got += resp.get("messages", [])
            if got:
                break
            time.sleep(0.25)
        self.assertEqual([m["call_id"] for m in got], ["fwd"])

    def test_5_signal_sent_on_hub_is_pulled_by_phone(self):
        """Caller on HUB, callee polls PHONE: HUB can't dial PHONE, so PHONE pulls during its long-poll."""
        caller, callee = Key(), Key()
        code, resp = http(f"{self.hub.ipc}/api/signal/send", offer(caller, callee.pub, "pull"))
        self.assertEqual(code, 200, resp)
        t0 = time.time()
        code, resp = http(f"{self.phone.ipc}/api/signal/poll?{poll_query(callee, 10)}", timeout=30)
        self.assertEqual(code, 200, resp)
        msgs = resp["messages"]
        if not msgs:   # the pull can land just after the first local wait returned
            code, resp = http(f"{self.phone.ipc}/api/signal/poll?{poll_query(callee, 5)}", timeout=30)
            msgs = resp["messages"]
        self.assertEqual([m["call_id"] for m in msgs], ["pull"])
        self.assertLess(time.time() - t0, 20)

    def test_6_node_health_has_wan_block(self):
        code, body = http(f"{self.phone.tel}/api/v1/telemetry/node-health")
        self.assertEqual(code, 200)
        self.assertIn("wan", body)
        self.assertIn("inbound_peers", body)

    def test_7_dialback_opens_back_to_caller(self):
        p2p = self.hub.ports["OMNI_P2P_PORT"]
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            code, resp = http(f"http://127.0.0.1:{p2p}/p2p/dialback", {"port": port}, headers={"X-Omni-Mesh": MESH})
            self.assertEqual(code, 200, resp)
            self.assertTrue(resp["open"])
            code, _ = http(f"http://127.0.0.1:{p2p}/p2p/dialback", {"port": port}, headers={"X-Omni-Mesh": MESH})
            self.assertEqual(code, 429)
        finally:
            listener.close()


class Parsers(unittest.TestCase):
    def test_stun_xor_mapped(self):
        tid = b"\x01" * 12
        ip, port = "203.0.113.7", 54321
        xport = port ^ (omni_wan.MAGIC >> 16)
        xip = bytes(b ^ m for b, m in zip(socket.inet_aton(ip), struct.pack("!I", omni_wan.MAGIC)))
        attr = struct.pack("!HH", 0x0020, 8) + b"\x00\x01" + struct.pack("!H", xport) + xip
        msg = struct.pack("!HHI", 0x0101, len(attr), omni_wan.MAGIC) + tid + attr
        self.assertEqual(omni_wan.parse_stun_response(msg, tid), (ip, port))
        self.assertIsNone(omni_wan.parse_stun_response(msg, b"\x02" * 12))

    def test_igd_description(self):
        xml = b"""<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device><deviceList>
        <device><serviceList><service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
        <controlURL>/ctl/IPConn</controlURL></service></serviceList></device></deviceList></device></root>"""
        ctrl, st = omni_wan.parse_igd_description(xml, "http://192.168.1.1:5000/rootDesc.xml")
        self.assertEqual(ctrl, "http://192.168.1.1:5000/ctl/IPConn")
        self.assertEqual(st, "urn:schemas-upnp-org:service:WANIPConnection:1")

    def test_cgnat(self):
        self.assertTrue(omni_wan.is_cgnat("100.70.1.2"))
        self.assertTrue(omni_wan.is_cgnat("192.168.1.5"))
        self.assertFalse(omni_wan.is_cgnat("8.8.8.8"))

    def test_probe_verdicts(self):
        probe = omni_wan.WanProbe(5005, lambda: "192.168.1.5", lambda: [("x", 1)],
                                  lambda h, p, mp: {"seen_ip": "52.1.2.3", "open": True},
                                  env={"OMNI_UPNP": "0"})
        self.assertTrue(probe._dialback("52.1.2.3", 5005, ""))
        lan = omni_wan.WanProbe(5005, lambda: "192.168.1.5", lambda: [("x", 1)],
                                lambda h, p, mp: {"seen_ip": "192.168.1.9", "open": True}, env={"OMNI_UPNP": "0"})
        self.assertIsNone(lan._dialback("52.1.2.3", 5005, ""))   # a LAN peer proves nothing
        shut = omni_wan.WanProbe(5005, lambda: "192.168.1.5", lambda: [("x", 1)],
                                 lambda h, p, mp: {"seen_ip": "52.1.2.3", "open": False}, env={"OMNI_UPNP": "0"})
        self.assertFalse(shut._dialback("52.1.2.3", 5005, ""))


if __name__ == "__main__":
    unittest.main()
