"""Quiet mesh (daemon v8.24.0): pairing, private beacons, stealth entry on :5005.

Run: python3 -m unittest tests.test_quiet_mesh -v

Unit tests (no daemon): beacon tokens (paired key recognised, stranger's key ignored, epoch +/-1),
RFC1918-only broadcast targets, the per-IP gate, QR payloads, the paired-devices file (0600).
Real daemons on loopback (each with its own HOME, so its own node key and paired-devices file):
  - pairing by code (happy path): both sides list each other, quiet mode turns on, file is owner-only
  - a wrong code: "didn't match" with attempts left, then the offer locks; the right code is refused after
  - pairing by QR payload, and a payload whose key doesn't match the device that answers
  - a paired peer syncing: an event posted on one node reaches the other through the signed session
  - a stranger on :5005 gets silence: the connection closes with zero bytes (no status, banner, id)
  - a captured session header replayed gets silence; a real session request is answered
  - rate limit and temporary block for a stranger hammering :5005
  - quiet beacons recognised by the paired node and ignored by a stranger node; pairing by code found
    through the "pairing open" beacon (OMNI_QUIET_BEACON_TARGETS points the beacons at loopback)
  - back-compat: OMNI_QUIET_MESH=off and fresh unpaired nodes answer :5005 exactly as before
  - pairing API is loopback + local token
"""
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daemon_harness import ADMIN, Daemon, free_port, http  # noqa: E402,F401

import omni_quiet_mesh as qm  # noqa: E402


def wait_for(pred, timeout=30.0, msg="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = pred()
            if last:
                return last
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {msg} (last: {last!r})")


def raw_exchange(port: int, data: bytes, timeout: float = 5.0) -> bytes:
    """Sends bytes to 127.0.0.1:port and returns every byte that comes back before the close."""
    out = b""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        try:
            s.sendall(data)
        except OSError:
            return out
        try:
            while True:
                part = s.recv(4096)
                if not part:
                    break
                out += part
        except (ConnectionResetError, socket.timeout, OSError):
            pass
    return out


def post_req(path: str, body: bytes, headers: dict = None) -> bytes:
    h = {"Host": "x", "Content-Type": "application/json", "Content-Length": str(len(body)), "Connection": "close"}
    h.update(headers or {})
    return (f"POST {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode() + body


class QDaemon(Daemon):
    """Daemon with quick catch-up so paired peers sync within the test."""

    def __init__(self, root, name, env=None):
        base = {"OMNI_CATCHUP_INTERVAL": "2", "OMNI_CATCHUP_STARTUP_DELAY": "1", "OMNI_NODE_NAME": f"node-{name}"}
        base.update(env or {})
        super().__init__(root, name, env=base)
        self.env["OMNI_MESH_ID"] = "quiet-test"       # same mesh name, so off/unpaired nodes behave as today
        self.env.pop("OMNI_QUIET_MESH", None)
        if env and "OMNI_QUIET_MESH" in env:
            self.env["OMNI_QUIET_MESH"] = env["OMNI_QUIET_MESH"]
        self.p2p = self.ports["OMNI_P2P_PORT"]
        self.udp = self.ports["OMNI_UDP_PORT"]

    def health(self):
        return http(f"{self.tel}/api/v1/telemetry/node-health")[1]

    def quiet(self):
        return self.health()["quiet"]

    def offer(self, **kw):
        code, body = self.post("/api/pair/offer", kw)
        assert code == 200, body
        return body

    def accept(self, timeout=40, **kw):
        return self.post("/api/pair/accept", kw, timeout=timeout)

    def paired(self):
        return self.get("/api/pair/list")[1]["peers"]

    def append(self, payload):
        code, body = self.post("/api/events/append", {"action": "CREATOR_POST_PUBLISHED", "payload": payload})
        assert code == 200, body
        return body["event_id"]

    def ids(self):
        return {e["event_id"] for e in self.get("/api/events/feed", {"limit": 200})[1]["events"]}

    def store(self):
        return json.loads((self.home / ".omni" / "paired_peers.json").read_text())

    def node_key_seed(self) -> bytes:
        return (self.home / ".omniverse" / "node_ed25519.key").read_bytes()


def pair_by_code(a: QDaemon, b: QDaemon):
    off = a.offer()
    code, body = b.accept(code=off["code"], host="127.0.0.1", port=a.p2p)
    assert code == 200, body
    return body


def manual_session(a: QDaemon, b: QDaemon):
    """Acts as node B (its key and mesh key, read from B's files) and signs in to A's :5005.
    Returns (sid, session key)."""
    import secrets
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    entry = [p for p in b.store()["peers"] if p["pubkey"] == a.node_id][0]
    mk = qm.unb64u(entry["mesh_key"], 32)
    sk_node = Ed25519PrivateKey.from_private_bytes(b.node_key_seed())
    nc = secrets.token_bytes(16)
    esk, ec = qm.x25519_pair()
    ts = int(time.time() * 1000)
    hello = {"nc": qm.b64u(nc), "ec": qm.b64u(ec), "ts": ts,
             "m": qm.hello_macs([{"mesh_key": mk}], nc, ec, ts)}
    code, r1 = http(f"http://127.0.0.1:{a.p2p}/p2p/auth", hello)
    assert code == 200, r1
    ns, es, sid = qm.unb64u(r1["ns"], 16), qm.unb64u(r1["es"], 32), r1["sid"]
    th = qm.auth_transcript(nc, ec, ts, ns, es, sid)
    assert qm.eq(qm.auth_server_mac(qm.auth_key(mk), th), qm.unb64u(r1["m"], 32))
    code, r2 = http(f"http://127.0.0.1:{a.p2p}/p2p/auth", {"sid": sid, "sig": sk_node.sign(qm.auth_sig_bytes("client", th)).hex()})
    assert code == 200, r2
    assert qm.ed25519_verify(a.node_id, r2["sig"], qm.auth_sig_bytes("server", th))
    return sid, qm.session_key(mk, qm.x25519_shared(esk, es), th)


# --------------------------------------------------------------------------- unit tests

class BeaconTokens(unittest.TestCase):
    def setUp(self):
        self.mine = {"pubkey": "a" * 64, "mesh_key": os.urandom(32)}
        self.other = {"pubkey": "b" * 64, "mesh_key": os.urandom(32)}

    def test_paired_key_recognised_stranger_ignored(self):
        pkt = qm.beacon_token(self.mine["mesh_key"])
        self.assertEqual(len(pkt), 48)
        self.assertIs(qm.match_beacon(pkt, [self.other, self.mine]), self.mine)
        self.assertIsNone(qm.match_beacon(pkt, [self.other]))        # a stranger's node: no match
        self.assertIsNone(qm.match_beacon(pkt, []))

    def test_nothing_identifying_in_the_clear_and_rotates(self):
        a, b = qm.beacon_token(self.mine["mesh_key"]), qm.beacon_token(self.mine["mesh_key"])
        self.assertNotEqual(a, b)                                     # fresh nonce every packet
        for needle in (self.mine["pubkey"].encode(), b"quiet", b"OMNI", b"8.24"):
            self.assertNotIn(needle, a)

    def test_epoch_window(self):
        now = time.time()
        e = qm.epoch_now(now)
        for d, ok in ((-1, True), (0, True), (1, True), (-2, False), (2, False)):
            pkt = qm.beacon_token(self.mine["mesh_key"], epoch=e + d)
            self.assertEqual(qm.match_beacon(pkt, [self.mine], now=now) is not None, ok, d)

    def test_bad_lengths(self):
        pkt = qm.beacon_token(self.mine["mesh_key"])
        self.assertIsNone(qm.match_beacon(pkt[:-1], [self.mine]))
        self.assertIsNone(qm.match_beacon(pkt + b"x", [self.mine]))


class PrivateInterfaces(unittest.TestCase):
    def test_only_rfc1918_directed_broadcasts(self):
        addrs = [("192.168.1.5", "255.255.255.0"), ("100.101.1.2", "255.192.0.0"), ("169.254.3.3", "255.255.0.0"),
                 ("127.0.0.1", "255.0.0.0"), ("10.1.2.3", "255.255.0.0"), ("172.20.1.1", "255.255.255.0"),
                 ("8.8.8.8", "255.255.255.0"), ("172.32.0.1", "255.255.255.0"), ("10.9.9.9", "255.255.255.255")]
        self.assertEqual(qm.private_broadcasts(addrs), ["192.168.1.255", "10.1.255.255", "172.20.1.255"])

    def test_classifier(self):
        for ip, want in (("10.0.0.1", True), ("172.16.0.1", True), ("192.168.0.9", True), ("100.64.0.1", False),
                         ("100.127.255.254", False), ("169.254.1.1", False), ("127.0.0.1", False), ("1.1.1.1", False)):
            self.assertEqual(qm.is_private_v4(ip), want, ip)

    def test_this_host_never_targets_global_broadcast(self):
        for b in qm.private_broadcasts():
            self.assertNotEqual(b, "255.255.255.255")
            self.assertTrue(qm.is_private_v4(b.rsplit(".", 1)[0] + ".1"), b)

    def test_targets_env(self):
        self.assertEqual(qm.parse_targets("127.0.0.1:9101, 10.0.0.2:5"), [("127.0.0.1", 9101), ("10.0.0.2", 5)])
        with self.assertRaises(ValueError):
            qm.parse_targets("bad host:x")


class Gate(unittest.TestCase):
    def test_token_bucket(self):
        g = qm.IPGate(burst=3, rate=1.0, fail_limit=99)
        self.assertEqual([g.allow("1.2.3.4", now=100.0) for _ in range(4)], [True, True, True, False])
        self.assertTrue(g.allow("1.2.3.4", now=101.1))       # refilled one
        self.assertTrue(g.allow("5.6.7.8", now=100.0))       # per IP

    def test_block_after_failures(self):
        g = qm.IPGate(burst=100, rate=100, fail_limit=3, block_s=60)
        self.assertFalse(g.fail("9.9.9.9", now=10.0))
        self.assertFalse(g.fail("9.9.9.9", now=11.0))
        self.assertTrue(g.fail("9.9.9.9", now=12.0))
        self.assertFalse(g.allow("9.9.9.9", now=13.0))
        self.assertTrue(g.allow("9.9.9.9", now=80.0))        # block expired


class PayloadAndStore(unittest.TestCase):
    def test_payload_roundtrip_and_damage(self):
        mk = os.urandom(32)
        p = qm.offer_payload("c" * 64, mk, "012345", ["192.168.1.4:5005", "100.70.1.2:5005"], "Kiro PC")
        self.assertTrue(p.startswith("omni://pair?p="))
        d = qm.parse_payload(p)
        self.assertEqual((d["pubkey"], d["mesh_key"], d["code"], d["name"]), ("c" * 64, mk, "012345", "Kiro PC"))
        self.assertEqual(d["addrs"], [("192.168.1.4", 5005), ("100.70.1.2", 5005)])
        self.assertEqual(qm.parse_payload(p[len("omni://pair?p="):])["code"], "012345")   # pasted without prefix
        for bad in ("", "omni://pair?p=xx", "hello"):
            with self.assertRaises(ValueError):
                qm.parse_payload(bad)
        self.assertEqual(qm.normalize_code(" 012 345 "), "012345")
        with self.assertRaises(ValueError):
            qm.normalize_code("12345")

    def test_store_owner_only_and_remove(self):
        d = Path(tempfile.mkdtemp(prefix="omni_store_"))
        try:
            s = qm.PairedStore(d / "x" / "paired_peers.json")
            s.add("d" * 64, os.urandom(32), "phone", "192.168.1.9", 5005)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(os.stat(s.path).st_mode), 0o600)
            s2 = qm.PairedStore(s.path)
            self.assertEqual(len(s2), 1)
            self.assertNotIn("mesh_key", qm.public_view(s2.get("d" * 64)))
            self.assertTrue(s2.remove("d" * 64))
            self.assertEqual(len(qm.PairedStore(s.path)), 0)
            s.path.write_text("{not json")
            self.assertEqual(len(qm.PairedStore(s.path)), 0)          # moved aside, not crashed
            self.assertTrue(s.path.with_suffix(".bad").exists())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_modes(self):
        self.assertEqual(qm.parse_mode(""), "auto")
        self.assertEqual(qm.parse_mode("ON"), "on")
        with self.assertRaises(ValueError):
            qm.parse_mode("quiet")


# --------------------------------------------------------------------------- real daemons

class PairedPair(unittest.TestCase):
    """A and B pair by code; C is a stranger on the same machine. Beacons are pointed at loopback."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="omni_quiet_"))
        udp = {n: free_port(socket.SOCK_DGRAM) for n in "ABC"}
        def tgt(*names):
            return ",".join(f"127.0.0.1:{udp[n]}" for n in names)
        cls.a = QDaemon(cls.root, "qa", env={"OMNI_UDP_PORT": str(udp["A"]), "OMNI_QUIET_BEACON_TARGETS": tgt("B", "C")})
        cls.b = QDaemon(cls.root, "qb", env={"OMNI_UDP_PORT": str(udp["B"]), "OMNI_QUIET_BEACON_TARGETS": tgt("A", "C")})
        cls.c = QDaemon(cls.root, "qc", env={"OMNI_UDP_PORT": str(udp["C"]), "OMNI_QUIET_MESH": "on"})
        for d in (cls.a, cls.b, cls.c):
            d.ports["OMNI_UDP_PORT"] = int(d.env["OMNI_UDP_PORT"])
            d.start()
        # Pair by code with NO address: B finds A through A's "pairing open" beacon.
        off = cls.a.offer()
        cls.offer = off
        cls.accept_code, cls.accept_body = cls.b.accept(code=off["code"])

    @classmethod
    def tearDownClass(cls):
        for d in (cls.a, cls.b, cls.c):
            d.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_1_pairing_happy_path(self):
        self.assertEqual(self.accept_code, 200, self.accept_body)
        self.assertEqual(self.accept_body["peer"]["pubkey"], self.a.node_id)
        self.assertEqual(self.accept_body["peer"]["name"], "node-qa")
        self.assertEqual([p["pubkey"] for p in self.a.paired()], [self.b.node_id])
        self.assertEqual([p["name"] for p in self.a.paired()], ["node-qb"])
        self.assertEqual([p["pubkey"] for p in self.b.paired()], [self.a.node_id])
        for d in (self.a, self.b):
            self.assertTrue(d.quiet()["active"])
            st = d.store()["peers"][0]
            self.assertEqual(len(qm.unb64u(st["mesh_key"])), 32)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(os.stat(d.home / ".omni" / "paired_peers.json").st_mode), 0o600)
        self.assertEqual(self.a.store()["peers"][0]["mesh_key"], self.b.store()["peers"][0]["mesh_key"])
        self.assertNotIn("mesh_key", json.dumps(self.a.get("/api/pair/list")[1]))
        self.assertEqual(self.a.get("/api/pair/offer")[1], {"status": "OK", "active": False, "ended": "paired"})
        self.assertTrue(self.c.quiet()["active"])              # C: quiet 'on' with nobody paired

    def test_2_quiet_beacons_recognised_and_ignored(self):
        wait_for(lambda: self.b.quiet()["counts"]["quiet_beacons_matched"] > 0, 20, "B to recognise A's beacons")
        wait_for(lambda: self.a.quiet()["counts"]["quiet_beacons_matched"] > 0, 20, "A to recognise B's beacons")
        wait_for(lambda: self.c.quiet()["counts"]["quiet_beacons_unmatched"] > 0, 20, "C to hear (and ignore) them")
        self.assertEqual(self.c.quiet()["counts"]["quiet_beacons_matched"], 0)
        self.assertEqual(self.c.health()["active_peers"], 0)
        wait_for(lambda: self.a.health()["active_peers"] >= 1, 20, "A to count B as connected")
        wait_for(lambda: self.b.paired()[0]["online"], 20, "B to show A online")

    def test_3_paired_peers_sync(self):
        e1 = self.a.append({"text": "from A, quietly"})
        wait_for(lambda: e1 in self.b.ids(), 30, "A's event on B")
        e2 = self.b.append({"text": "from B, quietly"})
        wait_for(lambda: e2 in self.a.ids(), 30, "B's event on A")
        self.assertNotIn(e1, self.c.ids())
        self.assertGreater(self.a.quiet()["counts"]["p2p_session_requests"], 0)

    def test_4_stranger_gets_silence(self):
        for req in (post_req("/p2p/sync", b"{}", {"X-Omni-Mesh": "quiet-test"}),
                    post_req("/p2p/reconcile", b'{"negentropy":"61"}', {"X-Omni-Mesh": "quiet-test"}),
                    post_req("/p2p/hello", b"{}"),
                    post_req("/p2p/pair", b'{"step":1}'),                    # no offer open: silent too
                    post_req("/p2p/sync", b"{}", {qm.SESSION_HEADER: "0" * 32 + ".1." + "0" * 64}),
                    b"GET / HTTP/1.0\r\n\r\n", b"garbage\r\n\r\n", b"OPTIONS * HTTP/1.1\r\nHost: x\r\n\r\n"):
            self.assertEqual(raw_exchange(self.c.p2p, req), b"", req[:40])
        # and a stranger hello with made-up MACs on a paired node
        bogus = {"nc": qm.b64u(os.urandom(16)), "ec": qm.b64u(os.urandom(32)), "ts": int(time.time() * 1000),
                 "m": [qm.b64u(os.urandom(32)) for _ in range(8)]}
        self.assertEqual(raw_exchange(self.a.p2p, post_req("/p2p/auth", json.dumps(bogus).encode()),), b"")

    def test_5_session_answered_and_replay_silenced(self):
        sid, sk = manual_session(self.a, self.b)
        body = json.dumps({"negentropy": "61"}).encode()   # reconcile: harmless, answers JSON
        hdr = qm.session_header(sk, sid, 1, "POST", "/p2p/reconcile", body)
        resp = raw_exchange(self.a.p2p, post_req("/p2p/reconcile", body, {qm.SESSION_HEADER: hdr}))
        self.assertTrue(resp.startswith(b"HTTP/1."), resp[:80])
        self.assertEqual(raw_exchange(self.a.p2p, post_req("/p2p/reconcile", body, {qm.SESSION_HEADER: hdr})), b"")
        # same counter, different body: MAC fails -> silence
        hdr2 = qm.session_header(sk, sid, 2, "POST", "/p2p/reconcile", body)
        self.assertEqual(raw_exchange(self.a.p2p, post_req("/p2p/reconcile", b'{"negentropy":"62"}',
                                                           {qm.SESSION_HEADER: hdr2})), b"")

    def test_6_pair_api_loopback_only_shape(self):
        code, body = self.a.post("/api/pair/remove", {"pubkey": "zz"})
        self.assertEqual(code, 400)
        code, body = self.a.post("/api/pair/accept", {"code": "12"})
        self.assertEqual(code, 400)


class WrongCodeLockout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="omni_quiet_lock_"))
        cls.a, cls.b = QDaemon(cls.root, "la"), QDaemon(cls.root, "lb")
        cls.a.start()
        cls.b.start()

    @classmethod
    def tearDownClass(cls):
        cls.a.stop()
        cls.b.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_wrong_code_then_lockout(self):
        off = self.a.offer()
        wrong = f"{(int(off['code']) + 1) % 1_000_000:06d}"
        lefts = []
        for _ in range(qm.PAIR_MAX_ATTEMPTS - 1):
            code, body = self.b.accept(code=wrong, host="127.0.0.1", port=self.a.p2p)
            self.assertEqual(code, 403, body)
            self.assertEqual(body["message"], "That code didn't match.")
            lefts.append(body["attempts_left"])
        self.assertEqual(lefts, [4, 3, 2, 1])
        code, body = self.b.accept(code=wrong, host="127.0.0.1", port=self.a.p2p)
        self.assertEqual(code, 410, body)
        self.assertIn("Too many wrong codes", body["message"])
        self.assertEqual(self.a.get("/api/pair/offer")[1]["ended"], "locked")
        code, body = self.b.accept(code=off["code"], host="127.0.0.1", port=self.a.p2p)   # right code, too late
        self.assertNotEqual(code, 200, body)
        self.assertEqual(self.a.paired(), [])
        self.assertEqual(self.b.paired(), [])
        self.assertFalse(self.a.quiet()["active"])

    def test_qr_payload_pairs_and_checks_the_key(self):
        off = self.a.offer()
        # A payload naming a different device key must not pair, even with the right code and mesh key.
        d = qm.parse_payload(off["payload"])
        forged = qm.offer_payload("e" * 64, d["mesh_key"], d["code"], [], "fake")
        code, body = self.b.accept(payload=forged, host="127.0.0.1", port=self.a.p2p)
        self.assertNotEqual(code, 200, body)
        off = self.a.offer()
        code, body = self.b.accept(payload=off["payload"], host="127.0.0.1", port=self.a.p2p)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["peer"]["pubkey"], self.a.node_id)
        self.assertEqual(self.a.store()["peers"][0]["mesh_key"], self.b.store()["peers"][0]["mesh_key"])
        self.assertEqual(qm.unb64u(self.b.store()["peers"][0]["mesh_key"]), qm.parse_payload(off["payload"])["mesh_key"])
        # Remove on both sides: back to the open, pre-8.24 behaviour.
        self.assertEqual(self.a.post("/api/pair/remove", {"pubkey": self.b.node_id})[0], 200)
        self.assertEqual(self.b.post("/api/pair/remove", {"pubkey": self.a.node_id})[0], 200)
        self.assertFalse(self.a.quiet()["active"])
        resp = raw_exchange(self.a.p2p, post_req("/p2p/sync", b"{}", {"X-Omni-Mesh": "wrong"}))
        self.assertTrue(resp.startswith(b"HTTP/1.0 403"), resp[:60])


class RateLimitAndBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="omni_quiet_rate_"))
        cls.d = QDaemon(cls.root, "ra", env={"OMNI_QUIET_MESH": "on", "OMNI_P2P_RATE_BURST": "6",
                                            "OMNI_P2P_RATE_PER_S": "0.2", "OMNI_P2P_FAIL_LIMIT": "100"})
        cls.e = QDaemon(cls.root, "rb", env={"OMNI_QUIET_MESH": "on", "OMNI_P2P_FAIL_LIMIT": "3",
                                            "OMNI_P2P_BLOCK_S": "60"})
        cls.d.start()
        cls.e.start()

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        cls.e.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_token_bucket_drops_a_flood(self):
        for _ in range(15):
            self.assertEqual(raw_exchange(self.d.p2p, post_req("/p2p/sync", b"{}")), b"")
        c = self.d.quiet()["counts"]
        self.assertGreater(c["p2p_rate_limited"], 0, c)

    def test_block_after_repeated_failures(self):
        for _ in range(3):
            self.assertEqual(raw_exchange(self.e.p2p, post_req("/p2p/hello", b"{}")), b"")
        wait_for(lambda: self.e.quiet()["counts"]["blocked_ips"] == 1, 5, "the IP to be blocked")
        before = self.e.quiet()["counts"]["p2p_rate_limited"]
        self.assertEqual(raw_exchange(self.e.p2p, post_req("/p2p/hello", b"{}")), b"")
        self.assertEqual(self.e.quiet()["counts"]["p2p_rate_limited"], before + 1)   # refused before reading
        # the loopback app API is untouched by the :5005 block
        self.assertEqual(self.e.get("/api/events/stats")[0], 200)

    def test_quiet_on_unpaired_sends_no_open_beacons(self):
        time.sleep(3.5)
        c = self.d.quiet()["counts"]
        self.assertEqual(c["quiet_beacons_sent"], 0)
        self.assertEqual(self.d.quiet()["mode"], "on")


class BackCompat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="omni_quiet_off_"))
        cls.off = QDaemon(cls.root, "off", env={"OMNI_QUIET_MESH": "off"})
        cls.fresh = QDaemon(cls.root, "fresh")
        cls.tok = QDaemon(cls.root, "tok", env={"OMNI_LOCAL_TOKEN": "1"})
        tf = cls.tok.home / ".omni" / "local_token"

        import threading

        def feed():
            for _ in range(200):
                if tf.exists():
                    cls.tok.start_headers["X-Omni-Local-Token"] = tf.read_text().strip()
                    return
                time.sleep(0.05)
        threading.Thread(target=feed, daemon=True).start()
        for d in (cls.off, cls.fresh, cls.tok):
            d.start()

    @classmethod
    def tearDownClass(cls):
        for d in (cls.off, cls.fresh, cls.tok):
            d.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_off_mode_is_the_old_daemon(self):
        q = http(f"{self.off.tel}/api/v1/telemetry/node-health")[1]["quiet"]
        self.assertEqual((q["mode"], q["active"]), ("off", False))
        code, body = self.off.post("/api/pair/offer", {})
        self.assertEqual(code, 409, body)
        self.assertEqual(self.off.post("/api/pair/accept", {"code": "123456"})[0], 409)
        resp = raw_exchange(self.off.p2p, post_req("/p2p/sync", b"{}", {"X-Omni-Mesh": "other-mesh"}))
        self.assertTrue(resp.startswith(b"HTTP/1.0 403"), resp[:60])
        self.assertIn(b"Mesh mismatch", resp)
        resp = raw_exchange(self.off.p2p, post_req("/p2p/pair", b'{"step":1}'))
        self.assertTrue(resp.startswith(b"HTTP/1.0 404"), resp[:60])

    def test_fresh_unpaired_node_answers_as_before(self):
        q = http(f"{self.fresh.tel}/api/v1/telemetry/node-health")[1]["quiet"]
        self.assertEqual((q["mode"], q["active"], q["paired"]), ("auto", False, 0))
        resp = raw_exchange(self.fresh.p2p, post_req("/p2p/sync", b"{}", {"X-Omni-Mesh": "other-mesh"}))
        self.assertTrue(resp.startswith(b"HTTP/1.0 403"), resp[:60])
        # an open offer exposes only the pairing endpoint
        self.fresh.offer()
        resp = raw_exchange(self.fresh.p2p, post_req("/p2p/pair", b'{"step":3}'))
        self.assertTrue(resp.startswith(b"HTTP/1.0 400"), resp[:60])
        self.assertEqual(self.fresh.post("/api/pair/cancel", {})[0], 200)
        resp = raw_exchange(self.fresh.p2p, post_req("/p2p/pair", b'{"step":1}'))
        self.assertTrue(resp.startswith(b"HTTP/1.0 404"), resp[:60])

    def test_pair_api_needs_local_token_when_on(self):
        code, _ = http(f"{self.tok.ipc}/api/pair/list")
        self.assertEqual(code, 401)
        code, body = http(f"{self.tok.ipc}/api/pair/list", headers=self.tok.start_headers)
        self.assertEqual(code, 200, body)


if __name__ == "__main__":
    unittest.main()
