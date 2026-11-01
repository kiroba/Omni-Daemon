"""Built-in STUN + TURN relay (daemon v8.22.0): codec, auth, allocation, permissions, channels,
safety limits, and the daemon wiring (node-health, ice-config, loopback config, peer relays).

Run: python -m unittest tests.test_turn_relay -v
"""
import json
import socket
import struct
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daemon_harness import Daemon, Key, http, now_ms, free_port  # noqa: E402

ADMIN = Path(__file__).resolve().parent.parent / "Admin"
sys.path.insert(0, str(ADMIN))
import omni_relay as R  # noqa: E402


def lan_ip() -> str:
    """A non-loopback local address (the relay refuses loopback peers by design)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


LAN = lan_ip()
SECRET = "test-secret-0123456789abcdef0123456789abcdef"


class Client:
    def __init__(self, server, username=None, password=None):
        self.server = server
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind((LAN, 0))
        self.s.settimeout(2)
        self.username, self.password = username, password
        self.nonce = None
        self.key = R.long_term_key(username, password) if username else None

    def tx(self, method, attrs=(), auth=True, cls=R.REQUEST):
        tid = R.secrets.token_bytes(12)
        a = list(attrs)
        key = None
        if auth and self.username:
            a += [(R.A_USERNAME, self.username.encode()), (R.A_REALM, R.REALM.encode())]
            if self.nonce:
                a.append((R.A_NONCE, self.nonce))
            key = self.key
        self.s.sendto(R.build(method, cls, tid, a, key=key), self.server)
        if cls == R.INDICATION:
            return None
        while True:
            data, _ = self.s.recvfrom(65535)
            m = R.parse(data)
            if m.tid == tid:
                return m

    def req(self, method, attrs=()):
        m = self.tx(method, attrs)
        if m.cls == R.ERROR and m.get(R.A_ERROR)[2] * 100 + m.get(R.A_ERROR)[3] in (401, 438) and m.get(R.A_NONCE):
            self.nonce = m.get(R.A_NONCE)
            m = self.tx(method, attrs)
        return m


def err(m) -> int:
    e = m.get(R.A_ERROR)
    return e[2] * 100 + e[3] if e else 0


class Codec(unittest.TestCase):
    def test_type_roundtrip(self):
        for method in (R.BINDING, R.ALLOCATE, R.REFRESH, R.SEND, R.DATA, R.CREATE_PERMISSION, R.CHANNEL_BIND):
            for cls in (R.REQUEST, R.INDICATION, R.SUCCESS, R.ERROR):
                self.assertEqual(R.split_type(R.msg_type(method, cls)), (method, cls))
        self.assertEqual(R.msg_type(R.BINDING, R.SUCCESS), 0x0101)
        self.assertEqual(R.msg_type(R.ALLOCATE, R.ERROR), 0x0113)

    def test_xor_address_v4_v6(self):
        tid = bytes(range(12))
        for ip, port in (("203.0.113.9", 3478), ("2001:db8::1", 50000)):
            self.assertEqual(R.parse_xor_addr(R.xor_addr(ip, port, tid), tid), (ip, port))

    def test_integrity_and_fingerprint(self):
        key = R.long_term_key("123:abc", "pw")
        tid = b"\x01" * 12
        raw = R.build(R.ALLOCATE, R.REQUEST, tid, [(R.A_USERNAME, b"123:abc"), (R.A_LIFETIME, b"\0\0\2X")], key=key)
        m = R.parse(raw)
        self.assertTrue(R.check_integrity(m, key))
        self.assertFalse(R.check_integrity(m, R.long_term_key("123:abc", "wrong")))
        fp = m.get(R.A_FINGERPRINT)
        crc = (R.binascii.crc32(raw[:-8][:2] + struct.pack("!H", len(raw) - 20) + raw[4:-8]) & 0xFFFFFFFF) ^ R.FP_XOR
        self.assertEqual(struct.unpack("!I", fp)[0], crc)
        tampered = bytearray(raw)
        tampered[25] ^= 1
        self.assertFalse(R.check_integrity(R.parse(bytes(tampered)), key))

    def test_rfc5769_sample_request_integrity(self):
        raw = bytes.fromhex(
            "000100582112a442b7e7a701bc34d686fa87dfae"
            "802200105354554e207465737420636c69656e74"
            "002400046e0001ff"
            "80290008932ff9b151263b36"
            "000600096576746a3a68367659202020"
            "000800149aeaa70cbfd8cb56781ef2b5b2d3f249c1b571a2"
            "80280004e57a3bcf")
        m = R.parse(raw)
        self.assertTrue(R.check_integrity(m, b"VOkJxbRl1RmTxUk/WvJxBt"))

    def test_rest_credentials(self):
        u, p = R.rest_credential("s", "ab" * 32, 600, now=1000)
        self.assertEqual(u, "1600:" + "ab" * 32)
        self.assertEqual(p, R.rest_password("s", u))

    def test_peer_policy(self):
        for ip in ("127.0.0.1", "0.0.0.0", "169.254.1.1", "224.0.0.1", "::1", "fe80::1", "::ffff:127.0.0.1"):
            self.assertFalse(R.peer_allowed(ip, True), ip)
        self.assertFalse(R.peer_allowed("192.168.1.5", False))
        self.assertFalse(R.peer_allowed("100.64.0.2", False))
        self.assertTrue(R.peer_allowed("192.168.1.5", True))
        self.assertTrue(R.peer_allowed("8.8.8.8", False))


@unittest.skipIf(LAN == "127.0.0.1", "needs a non-loopback interface")
class LiveRelay(unittest.TestCase):
    def setUp(self):
        env = {"OMNI_RELAY_PORT": "0", "OMNI_RELAY_PORTS": f"{free_port(socket.SOCK_DGRAM)}-{free_port(socket.SOCK_DGRAM) + 0}",
               "OMNI_RELAY_ALLOW_PRIVATE": "1", "OMNI_RELAY_BIND": LAN, "OMNI_RELAY_MAX_ALLOC": "2"}
        lo = 40000 + (int(time.time() * 1000) % 15000)
        env["OMNI_RELAY_PORTS"] = f"{lo}-{lo + 20}"
        self.relay = R.Relay(SECRET, public_ip_fn=lambda: None, local_ip_fn=lambda: LAN, env=env)
        self.assertTrue(self.relay.start(), self.relay.error)
        self.server = (LAN, self.relay.port)
        self.user, self.pw = R.rest_credential(SECRET, "ab" * 32, 600)

    def tearDown(self):
        self.relay.stop()

    def client(self, pw=None):
        return Client(self.server, self.user, pw or self.pw)

    def test_binding(self):
        c = Client(self.server)
        m = c.tx(R.BINDING, auth=False)
        self.assertEqual(m.cls, R.SUCCESS)
        self.assertEqual(R.parse_xor_addr(m.get(R.A_XOR_MAPPED), m.tid), c.s.getsockname())

    def test_auth_required_and_bad_password(self):
        c = self.client()
        m = c.tx(R.ALLOCATE, [(R.A_REQ_TRANSPORT, b"\x11\0\0\0")])   # no nonce yet -> 401 + nonce
        self.assertEqual(err(m), 401)
        self.assertTrue(m.get(R.A_NONCE))
        bad = self.client(pw="nope")
        self.assertEqual(err(bad.req(R.ALLOCATE, [(R.A_REQ_TRANSPORT, b"\x11\0\0\0")])), 401)
        expired_u, expired_p = R.rest_credential(SECRET, "cd" * 32, -10)
        old = Client(self.server, expired_u, expired_p)
        self.assertEqual(err(old.req(R.ALLOCATE, [(R.A_REQ_TRANSPORT, b"\x11\0\0\0")])), 401)

    def _alloc(self, c):
        m = c.req(R.ALLOCATE, [(R.A_REQ_TRANSPORT, b"\x11\0\0\0")])
        self.assertEqual(m.cls, R.SUCCESS, err(m))
        self.assertTrue(R.check_integrity(m, c.key))
        return R.parse_xor_addr(m.get(R.A_XOR_RELAYED), m.tid)

    def test_send_and_data_indications(self):
        c = self.client()
        relayed = self._alloc(c)
        self.assertEqual(relayed[0], LAN)
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind((LAN, 0))
        peer.settimeout(2)
        pa = peer.getsockname()
        # no permission yet: peer traffic is dropped
        peer.sendto(b"early", relayed)
        m = self._perm(c, pa)
        self.assertEqual(m.cls, R.SUCCESS, err(m))
        peer.sendto(b"hello-client", relayed)
        data, _ = c.s.recvfrom(65535)
        d = R.parse(data)
        if d.get(R.A_DATA) == b"early":
            d = R.parse(c.s.recvfrom(65535)[0])
        self.assertEqual((d.method, d.cls), (R.DATA, R.INDICATION))
        self.assertEqual(d.get(R.A_DATA), b"hello-client")
        self.assertEqual(R.parse_xor_addr(d.get(R.A_XOR_PEER), d.tid), pa)
        tid = R.secrets.token_bytes(12)
        c.s.sendto(R.build(R.SEND, R.INDICATION, tid, [(R.A_XOR_PEER, R.xor_addr(pa[0], pa[1], tid)),
                                                        (R.A_DATA, b"hello-peer")]), self.server)
        got, src = peer.recvfrom(65535)
        self.assertEqual((got, src), (b"hello-peer", relayed))

    def _perm(self, c, pa, method=R.CREATE_PERMISSION, extra=()):
        """Request whose XOR-PEER-ADDRESS is encoded with its own transaction id."""
        for _ in range(2):
            tid = R.secrets.token_bytes(12)
            attrs = list(extra) + [(R.A_XOR_PEER, R.xor_addr(pa[0], pa[1], tid)),
                                   (R.A_USERNAME, c.username.encode()), (R.A_REALM, R.REALM.encode())]
            if c.nonce:
                attrs.append((R.A_NONCE, c.nonce))
            c.s.sendto(R.build(method, R.REQUEST, tid, attrs, key=c.key), self.server)
            while True:
                m = R.parse(c.s.recvfrom(65535)[0])
                if m.tid == tid:
                    break
            if err(m) == 438:
                c.nonce = m.get(R.A_NONCE)
                continue
            return m
        return m

    def test_channel_data_both_ways(self):
        c = self.client()
        relayed = self._alloc(c)
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind((LAN, 0))
        peer.settimeout(2)
        pa = peer.getsockname()
        m = self._perm(c, pa, R.CHANNEL_BIND, [(R.A_CHANNEL, struct.pack("!HH", 0x4001, 0))])
        self.assertEqual(m.cls, R.SUCCESS, err(m))
        c.s.sendto(struct.pack("!HH", 0x4001, 5) + b"media", self.server)
        self.assertEqual(peer.recvfrom(65535), (b"media", relayed))
        peer.sendto(b"back!", relayed)
        data, _ = c.s.recvfrom(65535)
        self.assertEqual(data, struct.pack("!HH", 0x4001, 5) + b"back!")

    def test_loopback_peer_refused(self):
        c = self.client()
        self._alloc(c)
        m = self._perm(c, ("127.0.0.1", 5000))
        self.assertEqual(err(m), 403)

    def test_quota_and_refresh_zero(self):
        a, b, d = self.client(), self.client(), self.client()
        self._alloc(a)
        self._alloc(b)
        self.assertEqual(err(d.req(R.ALLOCATE, [(R.A_REQ_TRANSPORT, b"\x11\0\0\0")])), 486)
        m = a.req(R.REFRESH, [(R.A_LIFETIME, b"\0\0\0\0")])
        self.assertEqual(m.cls, R.SUCCESS, err(m))
        self.assertEqual(self.relay.snapshot()["allocations"], 1)
        self._alloc(d)

    def test_duplicate_allocate_and_tcp_refused(self):
        c = self.client()
        self._alloc(c)
        self.assertEqual(err(c.req(R.ALLOCATE, [(R.A_REQ_TRANSPORT, b"\x11\0\0\0")])), 437)
        t = self.client()
        self.assertEqual(err(t.req(R.ALLOCATE, [(R.A_REQ_TRANSPORT, b"\x06\0\0\0")])), 442)

    def test_daily_cap(self):
        self.relay.set_config(max_mb_day=1)
        self.relay.bytes_today = 2_000_000
        self.assertEqual(err(self.client().req(R.ALLOCATE, [(R.A_REQ_TRANSPORT, b"\x11\0\0\0")])), 486)


def ice_query(k: Key) -> str:
    ts = now_ms()
    sig = k.sign(json.dumps({"type": "omnicast.ice-config.v1", "pubkey": k.pub, "ts": ts}, sort_keys=True,
                            separators=(",", ":")).encode())
    return urllib.parse.urlencode({"pubkey": k.pub, "ts": ts, "sig": sig})


QUIET = {"OMNI_MESH_ID": "relay-test", "OMNI_UPNP": "0", "OMNI_WAN_INTERVAL": "3600"}


class DaemonWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="omni_relay_"))
        cls.relay_port = free_port(socket.SOCK_DGRAM)
        lo = 30000 + (int(time.time()) % 5000)
        cls.hub = Daemon(cls.root, "hub", env=dict(QUIET, OMNI_RELAY="on", OMNI_RELAY_PORT=str(cls.relay_port),
                                                   OMNI_RELAY_PORTS=f"{lo}-{lo + 10}")).start()
        cls.phone = Daemon(cls.root, "phone", env=dict(
            QUIET, OMNI_SYNC_PEERS=f"127.0.0.1:{cls.hub.ports['OMNI_P2P_PORT']}")).start()

    @classmethod
    def tearDownClass(cls):
        cls.phone.stop()
        cls.hub.stop()

    def test_1_node_health_reports_relay(self):
        code, h = http(f"{self.hub.tel}/api/v1/telemetry/node-health")
        self.assertEqual(code, 200)
        self.assertTrue(h["relay"]["enabled"], h["relay"])
        self.assertEqual(h["relay"]["port"], self.relay_port)
        code, h = http(f"{self.phone.tel}/api/v1/telemetry/node-health")
        self.assertFalse(h["relay"]["enabled"])

    def test_2_hub_ice_config_is_self_hosted(self):
        k = Key()
        code, cfg = self.hub.get("/api/signal/ice-config?" + ice_query(k))
        self.assertEqual(code, 200, cfg)
        urls = [u for s in cfg["ice_servers"] for u in s["urls"]]
        self.assertNotIn("stun:stun.l.google.com:19302", urls)
        turn = [s for s in cfg["ice_servers"] if any(u.startswith("turn:") for u in s["urls"])][0]
        self.assertIn(f":{self.relay_port}?transport=udp", turn["urls"][0])
        self.assertTrue(turn["username"].endswith(":" + k.pub))
        self.assertEqual(turn["credential"], R.rest_password(
            (self.hub.home / ".omni" / "relay_secret").read_text().strip(), turn["username"]))
        self.assertEqual(cfg["ice_transport_policy"], "all")

    def test_3_phone_gets_hub_relay_via_peer(self):
        k = Key()
        code, cfg = self.phone.get("/api/signal/ice-config?" + ice_query(k), timeout=20)
        self.assertEqual(code, 200, cfg)
        turn = [s for s in cfg["ice_servers"] if "username" in s]
        self.assertEqual(len(turn), 1, cfg)
        self.assertEqual(turn[0]["urls"], [f"turn:127.0.0.1:{self.relay_port}?transport=udp"])
        self.assertEqual(cfg["relays"], 1)

    def test_4_peer_cred_needs_signed_query(self):
        p2p = self.hub.ports["OMNI_P2P_PORT"]
        k = Key()
        code, _ = http(f"http://127.0.0.1:{p2p}/p2p/relay/cred", {"query": "pubkey=" + "ab" * 32 + "&ts=1&sig=" + "00" * 64},
                       headers={"X-Omni-Mesh": "relay-test"})
        self.assertIn(code, (400, 401))
        code, _ = http(f"http://127.0.0.1:{p2p}/p2p/relay/cred", {"query": ice_query(k)}, headers={"X-Omni-Mesh": "other"})
        self.assertEqual(code, 403)

    def test_5_loopback_config_toggles(self):
        code, r = self.phone.post("/api/relay/config", {"enabled": "yes"})
        self.assertEqual(code, 400)
        code, r = self.phone.post("/api/relay/config", {"max_kbps": 10})
        self.assertEqual(code, 400)
        code, r = self.hub.post("/api/relay/config", {"enabled": False})
        self.assertEqual(code, 200, r)
        self.assertFalse(r["enabled"])
        code, cfg = self.hub.get("/api/signal/ice-config?" + ice_query(Key()))
        self.assertEqual(cfg["ice_servers"], [{"urls": ["stun:stun.l.google.com:19302"]}])
        code, r = self.hub.post("/api/relay/config", {"enabled": True, "max_kbps": 800, "max_mb_day": 500})
        self.assertEqual(code, 200, r)
        self.assertTrue(r["enabled"], r)
        self.assertEqual((r["max_kbps"], r["max_mb_day"]), (800, 500))
        code, s = self.hub.get("/api/relay/status")
        self.assertEqual(code, 200)
        self.assertTrue(s["enabled"])


if __name__ == "__main__":
    unittest.main()
