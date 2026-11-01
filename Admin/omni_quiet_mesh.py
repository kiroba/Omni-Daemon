"""omni_quiet_mesh.py - quiet mesh (daemon v8.24.0): pairing, private beacons, stealth entry on :5005.

Pure helpers used by omni_live_daemon_server.py. Standard library plus `cryptography`, which the daemon
already needs for Ed25519 (X25519, AES-GCM and Ed25519 all come from it), so nothing new to install on
desktop or under Chaquopy on Android.

Pieces:
  * PairedStore     paired devices: pubkey, per-pair 32-byte mesh key, name, last address. JSON file,
                    owner-only (0600 / icacls), under the node's data dir (~/.omni/paired_peers.json).
  * PairOffer       a short-lived pairing offer: 6-digit code + QR payload (node pubkey, mesh key,
                    addresses). Few attempts, short expiry.
  * pairing crypto  X25519 exchange authenticated with a key derived from the code (PBKDF2, 200k
                    rounds) or, for a scanned QR, from the 32-byte mesh key in it. Not a PAKE: see
                    Admin/docs/QUIET_MESH.md for what that means.
  * quiet beacons   48 bytes: nonce(16) || HMAC-SHA256(beacon key, epoch || nonce). No ids in the clear.
  * auth crypto     the signed challenge-response a peer completes before any /p2p data.
  * IPGate          per-IP token bucket + temporary block after repeated failures.
  * private_broadcasts()  directed broadcast addresses of RFC1918 interfaces only.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import struct
import sys
import threading
import time
from pathlib import Path

MODE_ENV = "OMNI_QUIET_MESH"              # auto (default) | on | off
STORE_ENV = "OMNI_PAIRED_PEERS_FILE"      # default ~/.omni/paired_peers.json
TARGETS_ENV = "OMNI_QUIET_BEACON_TARGETS"  # extra unicast beacon targets "host:udpport,..." (labs, tailnets, tests)
NAME_ENV = "OMNI_NODE_NAME"               # the name a paired device sees for this node
MODES = ("auto", "on", "off")

EPOCH_S = 300                 # beacon tokens rotate every 5 minutes
NONCE_LEN = 16
TAG_LEN = 32
BEACON_LEN = NONCE_LEN + TAG_LEN
OFFER_TTL_DEFAULT = 300
OFFER_TTL_MIN = 60
OFFER_TTL_MAX = 600
PAIR_MAX_ATTEMPTS = 5         # wrong codes before the offer is locked
PAIR_MAX_SESSIONS = 8         # concurrent half-done pairing exchanges per offer
PBKDF2_ITERS = 200_000
AUTH_TS_WINDOW_MS = 120_000
HANDSHAKE_TTL_S = 30.0
SESSION_TTL_S = 600.0
MAX_PAIRED = 64
MAC_SLOTS = 8                 # auth hello carries MACs in groups of 8 so the count of pairings isn't shown
NAME_MAX = 40

PAIR_DOMAIN = b"omni-pair-v1"
AUTH_DOMAIN = b"omni-auth-v1"
QR_SCHEME = "omni://pair?p="


def parse_mode(value=None) -> str:
    v = (os.environ.get(MODE_ENV, "") if value is None else value).strip().lower() or "auto"
    if v not in MODES:
        raise ValueError(f"{MODE_ENV} must be auto, on or off (got {v!r})")
    return v


def node_name() -> str:
    raw = os.environ.get(NAME_ENV, "").strip() or socket.gethostname() or "Omni node"
    return clean_name(raw)


def clean_name(value) -> str:
    s = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()
    return s[:NAME_MAX] or "Omni node"


# --------------------------------------------------------------------------- small crypto helpers

def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def unb64u(s, n: int = None) -> bytes:
    if not isinstance(s, str) or len(s) > 4096:
        raise ValueError("expected base64url text")
    out = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if n is not None and len(out) != n:
        raise ValueError(f"expected {n} bytes")
    return out


def _lp(*parts) -> bytes:
    """Length-prefixed concatenation, so no two different part lists hash the same."""
    out = bytearray()
    for p in parts:
        if isinstance(p, str):
            p = p.encode("utf-8")
        elif isinstance(p, int):
            p = str(p).encode("ascii")
        out += len(p).to_bytes(4, "big") + p
    return bytes(out)


def _hmac(key: bytes, *parts) -> bytes:
    return hmac.new(key, _lp(*parts), hashlib.sha256).digest()


def _sha(*parts) -> bytes:
    return hashlib.sha256(_lp(*parts)).digest()


def eq(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


def x25519_pair():
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    sk = X25519PrivateKey.generate()
    pub = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return sk, pub


def x25519_shared(sk, peer_pub: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    shared = sk.exchange(X25519PublicKey.from_public_bytes(peer_pub))
    if shared == b"\x00" * 32:
        raise ValueError("low-order X25519 key")
    return shared


def ed25519_verify(pubkey_hex: str, sig_hex: str, data: bytes) -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(bytes.fromhex(sig_hex), data)
        return True
    except (ValueError, TypeError, InvalidSignature):
        return False


def seal(key: bytes, plaintext: bytes, aad: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return b64u(nonce + AESGCM(key).encrypt(nonce, plaintext, aad))


def unseal(key: bytes, box: str, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = unb64u(box)
    if len(raw) < 12 + 16:
        raise ValueError("box too short")
    return AESGCM(key).decrypt(raw[:12], raw[12:], aad)


def is_pubkey(v) -> bool:
    return isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)


# --------------------------------------------------------------------------- paired-peers store

class PairedStore:
    """{pubkey: {"pubkey", "mesh_key" (bytes), "name", "host", "port", "paired_at", "last_seen"}}.
    Saved as JSON (mesh keys base64url) with owner-only permissions. A bad file is kept aside, never
    silently emptied over: it is renamed to .bad and the node starts with no pairings."""

    def __init__(self, path, restrict=None):
        self.path = Path(path)
        self.restrict = restrict
        self.lock = threading.RLock()
        self.peers = {}
        self._saved_seen = {}
        self.load()

    def load(self):
        with self.lock:
            self.peers = {}
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                rows = data.get("peers", []) if isinstance(data, dict) else []
                for r in rows:
                    pk = str(r.get("pubkey", ""))
                    mk = unb64u(r.get("mesh_key", ""), 32)
                    if not is_pubkey(pk):
                        continue
                    self.peers[pk] = {"pubkey": pk, "mesh_key": mk, "name": clean_name(r.get("name")),
                                      "host": str(r.get("host") or "")[:255], "port": int(r.get("port") or 5005),
                                      "paired_at": int(r.get("paired_at") or 0), "last_seen": int(r.get("last_seen") or 0)}
            except (OSError, ValueError, TypeError, AttributeError) as e:
                bad = self.path.with_suffix(".bad")
                try:
                    os.replace(self.path, bad)
                except OSError:
                    pass
                print(f"[!] Paired devices file was unreadable ({e}); moved to {bad}. Pair again.", flush=True)
                self.peers = {}

    def save(self):
        with self.lock:
            rows = [dict(v, mesh_key=b64u(v["mesh_key"])) for v in self.peers.values()]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.restrict:
                try:
                    self.restrict(self.path.parent, True)
                except Exception:
                    pass
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "peers": rows}, f, indent=1)
            os.replace(tmp, self.path)
            try:
                if self.restrict:
                    self.restrict(self.path, False)
                else:
                    os.chmod(self.path, 0o600)
            except Exception as e:
                print(f"[!] Could not lock {self.path} to the current user: {e}", flush=True)

    def add(self, pubkey: str, mesh_key: bytes, name: str, host: str, port: int) -> dict:
        if not is_pubkey(pubkey) or len(mesh_key) != 32:
            raise ValueError("bad pubkey or mesh key")
        with self.lock:
            if pubkey not in self.peers and len(self.peers) >= MAX_PAIRED:
                raise ValueError(f"at most {MAX_PAIRED} paired devices")
            now = int(time.time())
            self.peers[pubkey] = {"pubkey": pubkey, "mesh_key": bytes(mesh_key), "name": clean_name(name),
                                  "host": str(host or "")[:255], "port": int(port or 5005),
                                  "paired_at": now, "last_seen": now}
            self.save()
            return dict(self.peers[pubkey])

    def remove(self, pubkey: str) -> bool:
        with self.lock:
            if self.peers.pop(pubkey, None) is None:
                return False
            self.save()
            return True

    def get(self, pubkey: str):
        with self.lock:
            v = self.peers.get(pubkey)
            return dict(v) if v else None

    def entries(self) -> list:
        with self.lock:
            return [dict(v) for v in self.peers.values()]

    def keys(self) -> set:
        with self.lock:
            return set(self.peers)

    def __len__(self):
        with self.lock:
            return len(self.peers)

    def touch(self, pubkey: str, host: str = None, port: int = None):
        """Seen now (and maybe at a new address). Saved when the address changes, else at most every 10 min."""
        with self.lock:
            v = self.peers.get(pubkey)
            if not v:
                return
            changed = False
            if host and host != v["host"]:
                v["host"], changed = host, True
            if port and int(port) != v["port"]:
                v["port"], changed = int(port), True
            v["last_seen"] = int(time.time())
            if changed or v["last_seen"] - self._saved_seen.get(pubkey, 0) > 600:
                self._saved_seen[pubkey] = v["last_seen"]
                try:
                    self.save()
                except OSError:
                    pass


def public_view(entry: dict, online: bool = False) -> dict:
    """What the loopback API shows: never the mesh key."""
    return {"pubkey": entry["pubkey"], "tag": "·" + entry["pubkey"][:4], "name": entry["name"],
            "address": f"{entry['host']}:{entry['port']}" if entry.get("host") else "",
            "paired_at": entry["paired_at"], "last_seen": entry["last_seen"], "online": bool(online)}


# --------------------------------------------------------------------------- pairing offer

class PairOffer:
    def __init__(self, ttl_s: float = OFFER_TTL_DEFAULT):
        self.code = f"{secrets.randbelow(10 ** 6):06d}"
        self.mesh_key = secrets.token_bytes(32)
        self.salt = secrets.token_bytes(16)
        self.created = time.time()
        self.expires = self.created + float(ttl_s)
        self.attempts = 0
        self.locked = False
        self.used_by = None
        self.sessions = {}          # sid -> half-done exchange
        self.lock = threading.Lock()
        self._code_key = None

    def active(self) -> bool:
        return self.used_by is None and not self.locked and time.time() < self.expires

    def expires_in(self) -> int:
        return max(0, int(self.expires - time.time()))

    def attempts_left(self) -> int:
        return max(0, PAIR_MAX_ATTEMPTS - self.attempts)

    def secret(self, mode: str) -> bytes:
        if mode == "qr":
            return pair_secret("qr", self.salt, self.code, self.mesh_key)
        if self._code_key is None:
            self._code_key = pair_secret("code", self.salt, self.code)
        return self._code_key


def offer_payload(pubkey: str, mesh_key: bytes, code: str, addrs: list, name: str) -> str:
    """The QR text: omni://pair?p=<base64url JSON>. Contains the secret mesh key: show it, never log it."""
    body = {"v": 1, "pk": pubkey, "mk": b64u(mesh_key), "c": code, "a": list(addrs)[:6], "n": clean_name(name)}
    return QR_SCHEME + b64u(json.dumps(body, separators=(",", ":")).encode("utf-8"))


def parse_payload(text) -> dict:
    t = str(text or "").strip()
    if t.startswith(QR_SCHEME):
        t = t[len(QR_SCHEME):]
    try:
        body = json.loads(unb64u(t).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("that isn't an Omni pairing code")
    if not isinstance(body, dict) or body.get("v") != 1:
        raise ValueError("that isn't an Omni pairing code")
    pk, code = body.get("pk"), str(body.get("c", ""))
    if not is_pubkey(pk) or not (len(code) == 6 and code.isdigit()):
        raise ValueError("that pairing code is damaged")
    addrs = []
    for a in body.get("a") or []:
        host, port = split_hostport(str(a))
        if host:
            addrs.append((host, port))
    return {"pubkey": pk, "mesh_key": unb64u(body.get("mk", ""), 32), "code": code, "addrs": addrs,
            "name": clean_name(body.get("n"))}


def split_hostport(text: str, default_port: int = 5005):
    t = (text or "").strip()
    if not t or len(t) > 255 or " " in t:
        return None, None
    port = default_port
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        if rest.startswith(":"):
            if not rest[1:].isdigit():
                return None, None
            port = int(rest[1:])
    elif t.count(":") == 1:
        host, p = t.split(":")
        if not p.isdigit():
            return None, None
        port = int(p)
    else:
        host = t
    if not host or not 0 < port < 65536:
        return None, None
    return host, port


def normalize_code(code) -> str:
    c = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(c) != 6:
        raise ValueError("the code is 6 digits")
    return c


# --------------------------------------------------------------------------- pairing crypto

def pair_secret(mode: str, salt: bytes, code: str, mesh_key: bytes = None, iters: int = PBKDF2_ITERS) -> bytes:
    if mode == "code":
        return hashlib.pbkdf2_hmac("sha256", code.encode("ascii"), PAIR_DOMAIN + salt, iters, 32)
    if mode == "qr":
        return _hmac(mesh_key, PAIR_DOMAIN, b"qr", salt, code)
    raise ValueError("mode must be code or qr")


def pair_transcript(mode, eb, nb, ea, na, salt, sid) -> bytes:
    return _sha(PAIR_DOMAIN, b"transcript", mode, eb, nb, ea, na, salt, sid)


def pair_keys(secret: bytes, dh: bytes, th: bytes) -> tuple:
    k = _hmac(secret, b"k", dh, th)
    return _hmac(k, b"mac"), _hmac(k, b"enc")


def pair_mac(k_mac: bytes, role: str, th: bytes, pubkey: str, port: int, name: str) -> bytes:
    return _hmac(k_mac, role, th, pubkey, int(port), clean_name(name))


def pair_sig_bytes(role: str, th: bytes, pubkey: str) -> bytes:
    return _lp(PAIR_DOMAIN, b"sig", role, th, pubkey)


# --------------------------------------------------------------------------- quiet beacons

def epoch_now(now: float = None) -> int:
    return int((time.time() if now is None else now) // EPOCH_S)


def beacon_key(mesh_key: bytes) -> bytes:
    return hmac.new(mesh_key, b"omni-beacon-v1", hashlib.sha256).digest()


def beacon_token(mesh_key: bytes, epoch: int = None, nonce: bytes = None) -> bytes:
    """nonce(16) || HMAC-SHA256(beacon key, epoch(8 bytes BE) || nonce). 48 random-looking bytes."""
    nonce = nonce or secrets.token_bytes(NONCE_LEN)
    e = epoch_now() if epoch is None else epoch
    tag = hmac.new(beacon_key(mesh_key), struct.pack(">Q", e) + nonce, hashlib.sha256).digest()
    return nonce + tag


def match_beacon(packet: bytes, entries: list, now: float = None):
    """The paired entry whose mesh key made this beacon (epoch +/-1), or None."""
    if not isinstance(packet, (bytes, bytearray)) or len(packet) != BEACON_LEN:
        return None
    nonce, tag = bytes(packet[:NONCE_LEN]), bytes(packet[NONCE_LEN:])
    e = epoch_now(now)
    for entry in entries:
        bk = beacon_key(entry["mesh_key"])
        for ep in (e, e - 1, e + 1):
            if eq(hmac.new(bk, struct.pack(">Q", ep) + nonce, hashlib.sha256).digest(), tag):
                return entry
    return None


# --------------------------------------------------------------------------- auth (challenge-response)

def auth_key(mesh_key: bytes) -> bytes:
    return hmac.new(mesh_key, b"omni-auth-v1", hashlib.sha256).digest()


def auth_hello_mac(ak: bytes, nc: bytes, ec: bytes, ts: int) -> bytes:
    return _hmac(ak, AUTH_DOMAIN, b"hello", nc, ec, int(ts))


def hello_macs(entries: list, nc: bytes, ec: bytes, ts: int) -> list:
    """One MAC per paired key, padded with random filler to a multiple of MAC_SLOTS, shuffled."""
    macs = [auth_hello_mac(auth_key(e["mesh_key"]), nc, ec, ts) for e in entries]
    while not macs or len(macs) % MAC_SLOTS:
        macs.append(secrets.token_bytes(32))
    out = [b64u(m) for m in macs]
    secrets.SystemRandom().shuffle(out)
    return out


def auth_transcript(nc, ec, ts, ns, es, sid) -> bytes:
    return _sha(AUTH_DOMAIN, b"transcript", nc, ec, int(ts), ns, es, sid)


def auth_server_mac(ak: bytes, th: bytes) -> bytes:
    return _hmac(ak, AUTH_DOMAIN, b"server", th)


def auth_sig_bytes(role: str, th: bytes) -> bytes:
    return _lp(AUTH_DOMAIN, b"sig", role, th)


def session_key(mesh_key: bytes, dh: bytes, th: bytes) -> bytes:
    return _hmac(_hmac(mesh_key or b"", b"session", dh), th)


def request_mac(sk: bytes, sid: str, ctr: int, method: str, path: str, body: bytes) -> str:
    return _hmac(sk, sid, int(ctr), method, path, hashlib.sha256(body or b"").digest()).hex()


SESSION_HEADER = "X-Omni-Session"


def session_header(sk: bytes, sid: str, ctr: int, method: str, path: str, body: bytes) -> str:
    return f"{sid}.{ctr}.{request_mac(sk, sid, ctr, method, path, body)}"


def parse_session_header(value):
    try:
        sid, ctr, mac = str(value).strip().split(".")
        if len(sid) != 32 or len(mac) != 64:
            return None
        return sid, int(ctr), mac
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- per-IP gate

class IPGate:
    """Token bucket per IP on new :5005 connections, plus a temporary block after repeated failures
    (failed auth, unknown caller). Memory is bounded: oldest entries are dropped past 4096 IPs."""

    def __init__(self, burst: float = 30.0, rate: float = 5.0, fail_limit: int = 5, fail_window_s: float = 300.0,
                 block_s: float = 600.0):
        self.burst, self.rate = float(burst), float(rate)
        self.fail_limit, self.fail_window_s, self.block_s = int(fail_limit), float(fail_window_s), float(block_s)
        self.buckets = {}     # ip -> [tokens, last]
        self.fails = {}       # ip -> [count, first]
        self.blocked = {}     # ip -> until
        self.lock = threading.Lock()

    def _trim(self, d):
        if len(d) > 4096:
            for k in list(d)[:1024]:
                d.pop(k, None)

    def is_blocked(self, ip: str, now: float = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            until = self.blocked.get(ip)
            if until and until > now:
                return True
            if until:
                self.blocked.pop(ip, None)
            return False

    def allow(self, ip: str, now: float = None) -> bool:
        now = time.time() if now is None else now
        if self.is_blocked(ip, now):
            return False
        with self.lock:
            tokens, last = self.buckets.get(ip, [self.burst, now])
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self.buckets[ip] = [tokens, now]
                return False
            self.buckets[ip] = [tokens - 1.0, now]
            self._trim(self.buckets)
            return True

    def fail(self, ip: str, now: float = None) -> bool:
        """Records a failure; True when this one blocks the IP."""
        now = time.time() if now is None else now
        with self.lock:
            count, first = self.fails.get(ip, [0, now])
            if now - first > self.fail_window_s:
                count, first = 0, now
            count += 1
            self.fails[ip] = [count, first]
            self._trim(self.fails)
            if count >= self.fail_limit:
                self.blocked[ip] = now + self.block_s
                self.fails.pop(ip, None)
                self._trim(self.blocked)
                return True
            return False

    def ok(self, ip: str):
        with self.lock:
            self.fails.pop(ip, None)


# --------------------------------------------------------------------------- private interfaces

_PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
_EXCLUDED = [ipaddress.ip_network(n) for n in ("100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8")]


def is_private_v4(ip: str) -> bool:
    """RFC1918 only. Tailscale/CGNAT (100.64/10), link-local and loopback are excluded."""
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    if any(a in n for n in _EXCLUDED):
        return False
    return any(a in n for n in _PRIVATE)


def _linux_interfaces() -> list:
    import fcntl
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _idx, name in socket.if_nameindex():
            req = struct.pack("256s", name.encode("utf-8")[:15])
            try:
                ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])     # SIOCGIFADDR
                mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891B, req)[20:24])   # SIOCGIFNETMASK
            except OSError:
                continue
            out.append((ip, mask))
    finally:
        s.close()
    return out


def interface_addresses(default_ip: str = None) -> list:
    """[(ipv4, netmask)]. Linux/Android read every interface; elsewhere (Windows, macOS) the host's
    addresses are used with an assumed /24, which matches nearly every home and office Wi-Fi."""
    found = []
    if sys.platform.startswith(("linux", "android")):
        try:
            found = _linux_interfaces()
        except (OSError, ImportError, AttributeError):
            found = []
    if not found:
        ips = set()
        try:
            for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(ai[4][0])
        except OSError:
            pass
        if default_ip:
            ips.add(default_ip)
        found = [(ip, "255.255.255.0") for ip in sorted(ips)]
    return found


def private_broadcasts(addrs: list = None, default_ip: str = None) -> list:
    """Directed broadcast address of every private IPv4 interface (never '<broadcast>' / 255.255.255.255)."""
    out = []
    for ip, mask in (addrs if addrs is not None else interface_addresses(default_ip)):
        if not is_private_v4(ip):
            continue
        try:
            net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
        except ValueError:
            continue
        if net.prefixlen >= 31:
            continue
        b = str(net.broadcast_address)
        if b not in out:
            out.append(b)
    return out


def private_addresses(default_ip: str = None) -> list:
    return [ip for ip, _ in interface_addresses(default_ip) if is_private_v4(ip)]


def parse_targets(value=None) -> list:
    """OMNI_QUIET_BEACON_TARGETS="192.168.1.20:9101,127.0.0.1:29101" -> [(host, port)]."""
    raw = os.environ.get(TARGETS_ENV, "") if value is None else value
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        host, port = split_hostport(part, 9101)
        if not host:
            raise ValueError(f"{TARGETS_ENV}: bad entry {part!r} (want host:port)")
        out.append((host, port))
    return out
