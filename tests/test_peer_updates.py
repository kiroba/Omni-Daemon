"""Peer-to-peer update cache (daemon v8.30.0): Admin/omni_updates.py + /api/updates/* + /p2p/updates/*.

Run: python3 -m unittest tests.test_peer_updates -v

Unit tests (no daemon):
  - the real kiroba/The-Omniverse 0.1.24 update-manifest.json verifies against the pinned release key
    (Ed25519 over canonical JSON minus "signature"); one changed byte fails it
  - unsigned manifests are refused unless allow_unsigned; a bad signature is always refused
  - the cache adopts newer signed manifests, keeps the held one on the same build, refuses downgrades
  - a file is cached only when its bytes hash to the manifest entry
  - LRU eviction under the size cap never drops a file the manifest lists for this node's apps when
    something else can go
  - Range resume: a download cut off half-way resumes with "Range: bytes=N-" and completes
  - Range header parsing (a-b, a-, -n, unsatisfiable)
Real daemons on loopback (own HOME each), a local fake "GitHub" and a test-generated Ed25519 key injected
through OMNI_UPDATE_PUBKEY. A and B are paired; C is a stranger with OMNI_UPDATES=off:
  - node-health "updates" block + "peer_updates" feature
  - signed manifest from GitHub accepted; bad signature rejected; downgrade rejected
  - file fetched from GitHub, cached on A; Range (206) on the loopback endpoint
  - GitHub down: B gets the manifest and the file from paired peer A, hash-verified, cached
  - a file whose bytes don't match its manifest hash is not cached
  - :5005 /p2p/updates/*: a paired session is answered (with Range); a stranger or a forged session gets
    zero bytes; non-quiet nodes refuse with 403
  - OMNI_UPDATES=off
"""
import hashlib
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daemon_harness import ADMIN, Daemon, free_port, http  # noqa: E402,F401
from daemon_version import DAEMON_VERSION  # noqa: E402

import omni_updates as upd  # noqa: E402

# kiroba/The-Omniverse releases/latest/download/update-manifest.json, fetched 2026-11-04 (tag 0.1.24,
# build 1029), byte for byte. Signed by the release workflow's OMNI_OTA_SIGNING_KEY.
REAL_MANIFEST_0_1_24 = r'''{
  "tag": "0.1.24",
  "build": 1029,
  "transport": "github",
  "apps": {
    "idle-plaza": {
      "url": "https://github.com/kiroba/The-Omniverse/releases/download/0.1.24/idle-plaza-android-arm64.apk",
      "sha256": "384cfb830b29a24f68429699c6358523593c3024ba87537bc082603d4b462c01"
    },
    "kickback-social": {
      "url": "https://github.com/kiroba/The-Omniverse/releases/download/0.1.24/kickback-social-android-arm64.apk",
      "sha256": "bbc0624d36f4f9d14f5515b3dcc906e956b861bced666627af522a1f18dfad57"
    },
    "mesh-monitor": {
      "url": "https://github.com/kiroba/The-Omniverse/releases/download/0.1.24/mesh-monitor-android-arm64.apk",
      "sha256": "5c3a69e759ab027a3267f2dac314a05164ccb85b59995dd2ba073df45fbfcc08"
    },
    "omnicast": {
      "url": "https://github.com/kiroba/The-Omniverse/releases/download/0.1.24/omnicast-android-arm64.apk",
      "sha256": "33ddbee11a2f8dd4be11621f35d48a8c388e659ddfa0580bf08c7c2ecc025f08"
    },
    "omniverse-superapp": {
      "url": "https://github.com/kiroba/The-Omniverse/releases/download/0.1.24/omniverse-superapp-android-arm64.apk",
      "sha256": "675424d332f781d28b0e9460c4bf363ddd2332083a7724ab476818b853e4aac7"
    },
    "phaser-studio": {
      "url": "https://github.com/kiroba/The-Omniverse/releases/download/0.1.24/phaser-studio-android-arm64.apk",
      "sha256": "98b940e9f1f195d1096ce5fe1e15240fc03cf2acc31ce24db04a65ffa39d2433"
    },
    "plaza-brawler": {
      "url": "https://github.com/kiroba/The-Omniverse/releases/download/0.1.24/plaza-brawler-android-arm64.apk",
      "sha256": "049b4aacfb24df2314ed0564b649b2d7450ab1707b164a80ee3f2d226f25e698"
    }
  },
  "signature": "891017c3f6c8eda76d3d740bc60c69a51a2f26b7e5a0543e4c147afd2c19b2619e12aa8bbe1ed5aa2b2c5e38a3df924c184772925a010021adbbbd9bdd8da602"
}'''

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.generate()
    pub = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return sk, pub


def sign_manifest(m: dict, sk) -> dict:
    """What .github/workflows/release.yml does in kiroba/The-Omniverse."""
    m = {k: v for k, v in m.items() if k != "signature"}
    canon = json.dumps(m, sort_keys=True, separators=(",", ":")).encode()
    return dict(m, signature=sk.sign(canon).hex())


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def get_raw(url, headers=None, timeout=30):
    """(status, headers, body bytes). A body cut short comes back as what arrived + status -1."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            try:
                return r.status, dict(r.headers), r.read()
            except Exception as e:  # noqa: BLE001  (IncompleteRead: the server dropped the stream)
                return -1, dict(r.headers), getattr(e, "partial", b"")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def raw_exchange(port: int, data: bytes, timeout: float = 5.0) -> bytes:
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


class FakeGitHub:
    """Serves {path: bytes} with Range support. down=True -> 503. drop_once: paths whose first GET sends
    only half the body (full Content-Length) and closes."""

    def __init__(self):
        self.files, self.down, self.drop_once, self.log = {}, False, set(), []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                return

            def do_GET(self):
                outer.log.append((self.path, self.headers.get("Range")))
                body = outer.files.get(self.path)
                if outer.down or body is None:
                    code = 503 if outer.down else 404
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, status = 0, 200
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes=") and rng.endswith("-"):
                    start = int(rng[6:-1])
                    if start >= len(body):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(body)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
                part = body[start:]
                self.send_response(status)
                self.send_header("Content-Length", str(len(part)))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                self.end_headers()
                if self.path in outer.drop_once:
                    outer.drop_once.discard(self.path)
                    self.wfile.write(part[: len(part) // 2])
                    self.wfile.flush()
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.wfile.write(part)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def gets(self, path):
        return [r for p, r in self.log if p == path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


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


# --------------------------------------------------------------------------- unit tests

class ManifestVectors(unittest.TestCase):
    def test_real_release_manifest_verifies_against_pinned_key(self):
        m = json.loads(REAL_MANIFEST_0_1_24)
        self.assertEqual((m["tag"], m["build"]), ("0.1.24", 1029))
        self.assertEqual(upd.verify_manifest(m, upd.PINNED_OTA_PUBKEY), "signed")
        self.assertEqual(upd.pinned_pubkey(""), upd.PINNED_OTA_PUBKEY)

    def test_one_changed_byte_fails(self):
        m = json.loads(REAL_MANIFEST_0_1_24)
        m["build"] = 1030
        with self.assertRaises(upd.ManifestError):
            upd.verify_manifest(m, upd.PINNED_OTA_PUBKEY)
        m = json.loads(REAL_MANIFEST_0_1_24)
        m["apps"]["mesh-monitor"]["url"] = m["apps"]["mesh-monitor"]["url"].replace("github.com", "evil.example")
        with self.assertRaises(upd.ManifestError):
            upd.verify_manifest(m, upd.PINNED_OTA_PUBKEY)
        _, other = keypair()
        with self.assertRaises(upd.ManifestError):
            upd.verify_manifest(json.loads(REAL_MANIFEST_0_1_24), other)

    def test_unsigned_only_with_opt_in(self):
        m = json.loads(REAL_MANIFEST_0_1_24)
        m.pop("signature")
        with self.assertRaises(upd.ManifestError):
            upd.verify_manifest(m, upd.PINNED_OTA_PUBKEY)
        self.assertEqual(upd.verify_manifest(m, upd.PINNED_OTA_PUBKEY, allow_unsigned=True), "unsigned")
        m["signature"] = "00" * 64          # a bad signature is never "unsigned"
        with self.assertRaises(upd.ManifestError):
            upd.verify_manifest(m, upd.PINNED_OTA_PUBKEY, allow_unsigned=True)

    def test_shape_checks(self):
        sk, pub = keypair()
        for bad in ({"tag": "", "build": 1, "apps": {}}, {"tag": "x", "build": "1", "apps": {}},
                    {"tag": "x", "build": True, "apps": {}},
                    {"tag": "x", "build": 1, "apps": {"a": {"url": "ftp://x", "sha256": "0" * 64}}},
                    {"tag": "x", "build": 1, "apps": {"a": {"url": "https://x", "sha256": "ZZ"}}}):
            with self.assertRaises(upd.ManifestError):
                upd.verify_manifest(sign_manifest(bad, sk), pub)

    def test_modes_and_settings(self):
        self.assertEqual(upd.parse_mode(""), "on")
        self.assertEqual(upd.parse_mode("auto"), "on")
        self.assertEqual(upd.parse_mode("OFF"), "off")
        with self.assertRaises(ValueError):
            upd.parse_mode("sometimes")
        s = upd.settings({"OMNI_UPDATE_CACHE_MB": "2", "OMNI_UPDATE_APPS": "a, b"})
        self.assertEqual((s["cap_bytes"], s["apps"], s["mode"]), (2 * 1024 * 1024, ["a", "b"], "on"))
        self.assertEqual(upd.settings({})["cap_bytes"], 1024 * 1024 * 1024)
        self.assertTrue(upd.network_allows_download(False))
        self.assertTrue(upd.network_allows_download(True))   # desktop: the Wi-Fi-only hook is a no-op


class CacheUnit(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="omni_upd_"))
        self.sk, self.pub = keypair()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def man(self, build, files: dict, sk=None):
        return sign_manifest({"tag": f"0.0.{build}", "build": build, "transport": "github",
                              "apps": {k: {"url": f"https://example.invalid/{k}.apk", "sha256": sha(v)}
                                       for k, v in files.items()}}, sk or self.sk)

    def cache(self, cap=10 ** 9, apps=None):
        return upd.UpdateCache(self.dir / "c", cap, self.pub, apps=apps)

    def test_accept_newer_keep_same_refuse_downgrade_and_bad_sig(self):
        c = self.cache()
        self.assertEqual(c.offer_manifest(self.man(10, {"a": b"1"}), "github"), "updated")
        self.assertEqual(c.offer_manifest(self.man(10, {"a": b"2"}), "peer"), "same")
        self.assertEqual(c.manifest_view()["source"], "github")
        with self.assertRaisesRegex(upd.ManifestError, "downgrade"):
            c.offer_manifest(self.man(9, {"a": b"x"}), "peer")
        other, _ = keypair()
        with self.assertRaisesRegex(upd.ManifestError, "signature"):
            c.offer_manifest(self.man(11, {"a": b"x"}, sk=other), "peer")
        bad = self.man(11, {"a": b"x"})
        bad["build"] = 12
        with self.assertRaises(upd.ManifestError):
            c.offer_manifest(bad, "peer")
        self.assertEqual(c.offer_manifest(self.man(11, {"a": b"3"}), "peer"), "updated")
        self.assertEqual(self.cache().manifest_view()["build"], 11)       # persisted and re-verified on load

    def test_stored_manifest_reverified_with_new_pin(self):
        self.cache().offer_manifest(self.man(5, {"a": b"1"}), "github")
        _, other_pub = keypair()
        c2 = upd.UpdateCache(self.dir / "c", 10 ** 9, other_pub)
        self.assertIsNone(c2.manifest_view())
        self.assertIn("ignored", c2.last_error)

    def test_commit_only_matching_hash(self):
        c = self.cache()
        good = b"apk bytes" * 100
        part = c.part_for(sha(good))
        part.write_bytes(b"tampered" * 100)
        self.assertFalse(c.commit(part, sha(good)))
        self.assertFalse(c.has(sha(good)))
        self.assertFalse(part.exists())
        part.write_bytes(good)
        self.assertTrue(c.commit(part, sha(good)))
        self.assertEqual(c.path_for(sha(good)).read_bytes(), good)

    def put(self, c, data: bytes, mtime: float):
        p = c.part_for(sha(data))
        p.write_bytes(data)
        self.assertTrue(c.commit(p, sha(data)))
        os.utime(c.path_for(sha(data)), (mtime, mtime))

    def test_lru_eviction_keeps_manifest_files(self):
        mine = os.urandom(1000)
        c = self.cache(cap=3000, apps=["mine"])
        c.offer_manifest(self.man(1, {"mine": mine, "notmine": b"zz"}), "github")
        olds = [os.urandom(1000) for _ in range(2)]
        self.put(c, mine, 1000.0)            # oldest of all, but listed for this node's app
        self.put(c, olds[0], 2000.0)
        self.put(c, olds[1], 3000.0)
        newest = os.urandom(1000)
        self.put(c, newest, 4000.0)          # 4000 bytes > cap: one must go
        have = {e["sha256"] for e in c.entries()}
        self.assertIn(sha(mine), have)
        self.assertNotIn(sha(olds[0]), have)             # least recently used unprotected file
        self.assertIn(sha(olds[1]), have)
        self.assertIn(sha(newest), have)
        self.assertLessEqual(c.size_bytes(), 3000)
        c.touch(sha(olds[1]))                            # reading a file makes it recent
        self.put(c, os.urandom(1000), time.time() - 10)
        have = {e["sha256"] for e in c.entries()}
        self.assertIn(sha(olds[1]), have)
        self.assertNotIn(sha(newest), have)

    def test_protected_files_go_only_when_unavoidable(self):
        a, b = os.urandom(1000), os.urandom(1000)
        c = self.cache(cap=1500)
        c.offer_manifest(self.man(1, {"a": a, "b": b}), "github")
        self.put(c, a, 1000.0)
        self.put(c, b, 2000.0)               # both protected; over the cap, so the older one goes
        self.assertEqual({e["sha256"] for e in c.entries()}, {sha(b)})

    def test_range_resume(self):
        gh = FakeGitHub()
        try:
            data = os.urandom(300_000)
            gh.files["/big.apk"] = data
            gh.drop_once.add("/big.apk")
            part = self.dir / "big.part"
            starts = []
            n = upd.download_resume(upd.url_opener(gh.base + "/big.apk", 5, _OPENER), part,
                                    on_start=lambda total, off: starts.append((total, off)),
                                    backoff=lambda i: 0.05)
            self.assertEqual(n, len(data))
            self.assertEqual(part.read_bytes(), data)
            ranges = gh.gets("/big.apk")
            self.assertEqual(ranges[0], None)
            self.assertEqual(ranges[1], f"bytes={len(data) // 2}-")          # resumed, not restarted
            self.assertEqual(starts, [(len(data), 0), (len(data), len(data) // 2)])
        finally:
            gh.close()

    def test_resume_permanent_404(self):
        gh = FakeGitHub()
        try:
            with self.assertRaises(upd.PermanentFetchError):
                upd.download_resume(upd.url_opener(gh.base + "/nope", 5, _OPENER), self.dir / "x.part")
        finally:
            gh.close()

    def test_parse_range(self):
        self.assertEqual(upd.parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(upd.parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(upd.parse_range("bytes=-10", 100), (90, 99))
        self.assertEqual(upd.parse_range("bytes=95-200", 100), (95, 99))
        self.assertIsNone(upd.parse_range(None, 100))
        self.assertIsNone(upd.parse_range("bytes=0-1,5-6", 100))
        for bad in ("bytes=100-", "bytes=5-2", "bytes=-0"):
            with self.assertRaises(ValueError):
                upd.parse_range(bad, 100)


# --------------------------------------------------------------------------- real daemons

def manual_session(a: Daemon, b: Daemon):
    """Acts as node B (its node key and the mesh key from B's paired-devices file) and signs in to A's
    :5005. Returns (sid, session key). Same steps as tests/test_quiet_mesh.py manual_session."""
    import omni_quiet_mesh as qm
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    store = json.loads((b.home / ".omni" / "paired_peers.json").read_text())
    entry = [p for p in store["peers"] if p["pubkey"] == a.node_id][0]
    mk = qm.unb64u(entry["mesh_key"], 32)
    sk_node = Ed25519PrivateKey.from_private_bytes((b.home / ".omniverse" / "node_ed25519.key").read_bytes())
    nc = secrets.token_bytes(16)
    esk, ec = qm.x25519_pair()
    ts = int(time.time() * 1000)
    hello = {"nc": qm.b64u(nc), "ec": qm.b64u(ec), "ts": ts, "m": qm.hello_macs([{"mesh_key": mk}], nc, ec, ts)}
    code, r1 = http(f"http://127.0.0.1:{a.p2p}/p2p/auth", hello)
    assert code == 200, r1
    ns, es, sid = qm.unb64u(r1["ns"], 16), qm.unb64u(r1["es"], 32), r1["sid"]
    th = qm.auth_transcript(nc, ec, ts, ns, es, sid)
    code, r2 = http(f"http://127.0.0.1:{a.p2p}/p2p/auth",
                    {"sid": sid, "sig": sk_node.sign(qm.auth_sig_bytes("client", th)).hex()})
    assert code == 200, r2
    return sid, qm.session_key(mk, qm.x25519_shared(esk, es), th)


class UDaemon(Daemon):
    def __init__(self, root, name, env=None):
        # Every test connection comes from 127.0.0.1, so lift the per-IP :5005 limits (the quiet mesh suite
        # tests those); a refused connection would look like a stranger's silence.
        base = {"OMNI_NODE_NAME": f"node-{name}", "OMNI_P2P_FAIL_LIMIT": "1000",
                "OMNI_P2P_RATE_BURST": "1000", "OMNI_P2P_RATE_PER_S": "1000",
                "OMNI_UPDATE_GITHUB_INTERVAL_MIN": "0", "OMNI_UPDATE_PEER_INTERVAL_S": "0",
                "OMNI_UPDATE_TIMEOUT_S": "5"}
        base.update(env or {})
        super().__init__(root, name, env=base)
        self.env["OMNI_MESH_ID"] = "upd-test"
        for k in ("OMNI_QUIET_MESH", "OMNI_UPDATES", "OMNI_UPDATE_CACHE_DIR", "OMNI_UPDATE_ALLOW_UNSIGNED",
                  "OMNI_UPDATE_CACHE_MB", "OMNI_UPDATE_APPS", "OMNI_UPDATE_WIFI_ONLY"):
            if not env or k not in env:
                self.env.pop(k, None)
        self.p2p = self.ports["OMNI_P2P_PORT"]

    def health(self):
        return http(f"{self.tel}/api/v1/telemetry/node-health")[1]

    def status(self):
        code, body = self.get("/api/updates/status")
        assert code == 200, body
        return body

    def manifest(self, refresh=True):
        return get_raw(f"{self.ipc}/api/updates/manifest" + ("?refresh=1" if refresh else ""))

    def file(self, digest, headers=None):
        return get_raw(f"{self.ipc}/api/updates/file/{digest}", headers=headers, timeout=60)


class PeerUpdates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="omni_peer_upd_"))
        cls.gh = FakeGitHub()
        cls.sk, cls.pub = keypair()
        cls.f1 = os.urandom(400_000)
        cls.f2 = os.urandom(150_000)
        cls.good_bad = b"what the manifest promised" * 1000
        cls.gh.files["/dl/mesh-monitor.apk"] = cls.f1
        cls.gh.files["/dl/kickback.apk"] = cls.f2
        cls.gh.files["/dl/broken.apk"] = b"something else entirely" * 1000
        cls.m100 = cls.make(100)
        cls.serve_manifest(cls.m100)
        env = {"OMNI_UPDATE_PUBKEY": cls.pub, "OMNI_UPDATE_MANIFEST_URL": cls.gh.base + "/latest/update-manifest.json"}
        cls.a = UDaemon(cls.root, "ua", env=env)
        cls.b = UDaemon(cls.root, "ub", env=env)
        cls.c = UDaemon(cls.root, "uc", env=dict(env, OMNI_UPDATES="off", OMNI_QUIET_MESH="on"))
        cls.n = UDaemon(cls.root, "un", env=env)    # never paired: :5005 is not quiet
        for d in (cls.a, cls.b, cls.c, cls.n):
            d.start()
        code, off = cls.a.post("/api/pair/offer", {})
        assert code == 200, off
        code, body = cls.b.post("/api/pair/accept", {"code": off["code"], "host": "127.0.0.1", "port": cls.a.p2p},
                                timeout=40)
        assert code == 200, body

    @classmethod
    def make(cls, build, sk=None):
        return sign_manifest({"tag": f"0.2.{build}", "build": build, "transport": "github", "apps": {
            "mesh-monitor": {"url": cls.gh.base + "/dl/mesh-monitor.apk", "sha256": sha(cls.f1)},
            "kickback-social": {"url": cls.gh.base + "/dl/kickback.apk", "sha256": sha(cls.f2)},
            "broken": {"url": cls.gh.base + "/dl/broken.apk", "sha256": sha(cls.good_bad)}}}, sk or cls.sk)

    @classmethod
    def serve_manifest(cls, m):
        cls.gh.files["/latest/update-manifest.json"] = json.dumps(m, indent=2).encode()

    @classmethod
    def tearDownClass(cls):
        for d in (cls.a, cls.b, cls.c, cls.n):
            d.stop()
        cls.gh.close()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_01_health_block_and_feature(self):
        h = self.a.health()
        self.assertIn("peer_updates", h["features"])
        self.assertTrue(h["updates"]["enabled"])
        self.assertEqual(h["version"], DAEMON_VERSION)
        self.assertTrue(self.a.health()["quiet"]["active"])
        self.assertFalse(self.c.health()["updates"]["enabled"])

    def test_02_signed_manifest_from_github(self):
        code, hdr, body = self.a.manifest()
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body), self.m100)          # exactly the signed manifest
        self.assertEqual(hdr.get("X-Omni-Update-Source"), "github")
        self.assertEqual(hdr.get("X-Omni-Update-Trust"), "signed")
        st = self.a.status()
        self.assertEqual(st["manifest"]["build"], 100)
        self.assertEqual(st["pinned_pubkey"], self.pub)
        self.assertEqual(self.a.health()["updates"]["manifest_build"], 100)

    def test_03_bad_signature_rejected(self):
        bad = self.make(101)
        bad["signature"] = bad["signature"][:-2] + ("00" if bad["signature"][-2:] != "00" else "11")
        self.serve_manifest(bad)
        try:
            code, _, body = self.a.manifest()
            self.assertEqual(json.loads(body)["build"], 100)
            gh = [t for t in self.a.status()["sources_tried"] if t["source"] == "github"][0]
            self.assertEqual(gh["result"], "rejected")
            self.assertIn("signature", gh["detail"])
            other, _ = keypair()
            self.serve_manifest(self.make(102, sk=other))     # signed, but not by the pinned key
            code, _, body = self.a.manifest()
            self.assertEqual(json.loads(body)["build"], 100)
        finally:
            self.serve_manifest(self.m100)

    def test_04_downgrade_rejected(self):
        self.serve_manifest(self.make(99))
        try:
            code, _, body = self.a.manifest()
            self.assertEqual(json.loads(body)["build"], 100)
            gh = [t for t in self.a.status()["sources_tried"] if t["source"] == "github"][0]
            self.assertEqual(gh["result"], "rejected")
            self.assertIn("downgrade", gh["detail"])
        finally:
            self.serve_manifest(self.m100)

    def test_05_file_from_github_cached(self):
        code, hdr, body = self.a.file(sha(self.f1))
        self.assertEqual(code, 200)
        self.assertEqual(sha(body), sha(self.f1))
        wait_for(lambda: sha(self.f1) in {f["sha256"] for f in self.a.status()["files"]}, 20, "f1 cached on A")
        files = {f["sha256"]: f for f in self.a.status()["files"]}
        self.assertEqual(files[sha(self.f1)]["apps"], ["mesh-monitor"])
        code, hdr, body = self.a.file(sha(self.f1))
        self.assertEqual((code, hdr.get("X-Omni-Update-Source")), (200, "cache"))
        self.assertEqual(body, self.f1)
        code, _, _ = self.a.file("ab" * 32)
        self.assertEqual(code, 404)                            # not cached, not in the manifest
        code, _, _ = self.a.file("not-a-hash")
        self.assertEqual(code, 400)

    def test_06_range_on_loopback(self):
        code, hdr, body = self.a.file(sha(self.f1), {"Range": "bytes=1000-"})
        self.assertEqual(code, 206)
        self.assertEqual(hdr.get("Content-Range"), f"bytes 1000-{len(self.f1) - 1}/{len(self.f1)}")
        self.assertEqual(body, self.f1[1000:])
        code, _, body = self.a.file(sha(self.f1), {"Range": "bytes=-10"})
        self.assertEqual((code, body), (206, self.f1[-10:]))
        code, _, _ = self.a.file(sha(self.f1), {"Range": f"bytes={len(self.f1)}-"})
        self.assertEqual(code, 416)

    def test_07_peer_serves_when_github_is_down(self):
        self.gh.down = True
        try:
            gh_before = len(self.gh.gets("/dl/mesh-monitor.apk"))
            code, hdr, body = self.b.manifest()
            self.assertEqual(code, 200, body)
            self.assertEqual(json.loads(body), self.m100)
            self.assertTrue(hdr.get("X-Omni-Update-Source", "").startswith("peer "), hdr)
            tried = {t["source"].split()[0]: t for t in self.b.status()["sources_tried"]}
            self.assertEqual(tried["github"]["result"], "unreachable")
            code, hdr, body = self.b.file(sha(self.f1))
            self.assertEqual(code, 200)
            self.assertEqual(body, self.f1)
            self.assertTrue(hdr.get("X-Omni-Update-Source", "").startswith("peer "), hdr)
            wait_for(lambda: sha(self.f1) in {f["sha256"] for f in self.b.status()["files"]}, 20, "f1 cached on B")
            self.assertEqual(len(self.gh.gets("/dl/mesh-monitor.apk")), gh_before)    # GitHub never asked
            # A file no peer has, with GitHub down: a clean error, nothing cached.
            code, _, body = self.b.file(sha(self.f2))
            self.assertIn(code, (502, -1))
            self.assertNotIn(sha(self.f2), {f["sha256"] for f in self.b.status()["files"]})
        finally:
            self.gh.down = False

    def test_08_bad_hash_not_cached(self):
        digest = sha(self.good_bad)
        code, _, body = self.a.file(digest)
        self.assertFalse(code == 200 and sha(body) == digest)
        wait_for(lambda: not self.a.status()["downloads"], 20, "the fetch to finish")
        st = self.a.status()
        self.assertNotIn(digest, {f["sha256"] for f in st["files"]})
        self.assertIn("mismatch", st["last_error"])
        self.assertEqual(list((self.a.home / ".omni" / "updates" / "partial").glob(digest + "*")), [])

    def test_09_p2p_paired_session_answered_with_range(self):
        import omni_quiet_mesh as qm
        sid, sk = manual_session(self.a, self.b)
        path = f"/p2p/updates/file/{sha(self.f1)}"
        hdr = qm.session_header(sk, sid, 1, "GET", path, b"")
        code, h, body = get_raw(f"http://127.0.0.1:{self.a.p2p}{path}",
                                headers={qm.SESSION_HEADER: hdr, "Range": "bytes=5-"})
        self.assertEqual((code, body), (206, self.f1[5:]))
        hdr = qm.session_header(sk, sid, 2, "GET", "/p2p/updates/manifest", b"")
        code, _, body = get_raw(f"http://127.0.0.1:{self.a.p2p}/p2p/updates/manifest",
                                headers={qm.SESSION_HEADER: hdr})
        self.assertEqual((code, json.loads(body)), (200, self.m100))
        hdr = qm.session_header(sk, sid, 3, "GET", "/p2p/updates/file/" + "cd" * 32, b"")
        code, _, _ = get_raw(f"http://127.0.0.1:{self.a.p2p}/p2p/updates/file/" + "cd" * 32,
                             headers={qm.SESSION_HEADER: hdr})
        self.assertEqual(code, 404)                            # cache only; never fetched onward
        # the same counter again: replay -> silence
        out = raw_exchange(self.a.p2p, (f"GET /p2p/updates/manifest HTTP/1.1\r\nHost: x\r\n"
                                        f"{qm.SESSION_HEADER}: {hdr}\r\nConnection: close\r\n\r\n").encode())
        self.assertEqual(out, b"")

    def test_10_stranger_gets_silence(self):
        for req in (b"GET /p2p/updates/manifest HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
                    f"GET /p2p/updates/file/{sha(self.f1)} HTTP/1.1\r\nHost: x\r\nRange: bytes=0-\r\n"
                    f"Connection: close\r\n\r\n".encode(),
                    (f"GET /p2p/updates/manifest HTTP/1.1\r\nHost: x\r\nX-Omni-Session: {'a' * 32}.1.{'b' * 64}\r\n"
                     "Connection: close\r\n\r\n").encode()):
            self.assertEqual(raw_exchange(self.a.p2p, req), b"", req)
        # an unpaired node (not quiet) refuses in the clear, and never serves its cache
        code, _, body = get_raw(f"http://127.0.0.1:{self.n.p2p}/p2p/updates/manifest")
        self.assertEqual(code, 403, body)

    def test_11_updates_off(self):
        code, _, body = self.c.manifest()
        self.assertEqual(code, 409, body)
        self.assertFalse(self.c.status()["enabled"])

    def test_12_not_remote_hub_paths(self):
        from daemon_harness import daemon_module
        mod = daemon_module()
        self.assertFalse([p for p in mod.REMOTE_GET_PATHS | mod.REMOTE_POST_PATHS if "/updates" in p])
