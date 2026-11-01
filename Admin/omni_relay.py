"""Built-in STUN + TURN relay for OmniCast (daemon v8.22.0). Self-hosted: no Cloudflare, no Google.

One UDP port (OMNI_RELAY_PORT, default 3478) answers:
  * STUN Binding requests (RFC 8489), unauthenticated, so a phone learns its public address from a
    node it already trusts instead of a public STUN server.
  * TURN over UDP (RFC 8656 subset): Allocate, Refresh, CreatePermission, ChannelBind, Send/Data
    indications and ChannelData. Long-term credentials in the TURN REST form every WebRTC stack
    understands: username "<expiry>:<pubkey>", password base64(HMAC-SHA1(secret, username)). The
    daemon mints these only for apps that signed an ice-config request, so the secret never leaves
    this node.

Relayed (per-allocation) sockets come from OMNI_RELAY_PORTS (default 49160-49200). On a home network
the daemon asks the router (UPnP) to forward UDP 3478 + that range; otherwise forward them by hand.

Safety limits (all env, all also settable at runtime over loopback POST /api/relay/config):
  * peers on loopback / link-local / multicast / unspecified addresses are always refused, and
    private + carrier-NAT (RFC 1918, 100.64/10) only with OMNI_RELAY_ALLOW_PRIVATE=1, so the relay can
    never be used to reach services on this machine or its LAN;
  * OMNI_RELAY_MAX_ALLOC (16) allocations in total, 4 per credential;
  * OMNI_RELAY_MAX_KBPS (2500) per allocation, both directions combined;
  * OMNI_RELAY_MAX_MB_DAY (0 = unlimited) total relayed per day; when spent, new allocations get 486.

Not implemented: TURN over TCP/TLS, IPv6 relayed addresses, EVEN-PORT / RESERVATION-TOKEN, DONT-FRAGMENT.
"""
import base64
import binascii
import hashlib
import hmac
import ipaddress
import os
import secrets
import selectors
import socket
import struct
import sys
import threading
import time

MAGIC = 0x2112A442
MAGIC_B = struct.pack("!I", MAGIC)
FP_XOR = 0x5354554E
REALM = "omniverse"
SOFTWARE = "omni-relay/1"

# methods
BINDING, ALLOCATE, REFRESH, SEND, DATA, CREATE_PERMISSION, CHANNEL_BIND = 0x001, 0x003, 0x004, 0x006, 0x007, 0x008, 0x009
# classes
REQUEST, INDICATION, SUCCESS, ERROR = 0x000, 0x010, 0x100, 0x110
# attributes
A_MAPPED, A_USERNAME, A_MI, A_ERROR, A_UNKNOWN = 0x0001, 0x0006, 0x0008, 0x0009, 0x000A
A_CHANNEL, A_LIFETIME, A_XOR_PEER, A_DATA, A_REALM, A_NONCE = 0x000C, 0x000D, 0x0012, 0x0013, 0x0014, 0x0015
A_XOR_RELAYED, A_REQ_FAMILY, A_EVEN_PORT, A_REQ_TRANSPORT, A_DONT_FRAG = 0x0016, 0x0017, 0x0018, 0x0019, 0x001A
A_XOR_MAPPED, A_RES_TOKEN, A_SOFTWARE, A_FINGERPRINT = 0x0020, 0x0022, 0x8022, 0x8028
_KNOWN_REQUIRED = {A_MAPPED, A_USERNAME, A_MI, A_ERROR, A_UNKNOWN, A_CHANNEL, A_LIFETIME, A_XOR_PEER, A_DATA,
                   A_REALM, A_NONCE, A_XOR_RELAYED, A_REQ_FAMILY, A_REQ_TRANSPORT, A_DONT_FRAG, A_XOR_MAPPED}

DEFAULT_LIFETIME, MAX_LIFETIME = 600, 3600
PERMISSION_LIFETIME, CHANNEL_LIFETIME = 300, 600
NONCE_TTL = 600
MAX_PER_USER = 4


def is_android() -> bool:
    return hasattr(sys, "getandroidapilevel") or "ANDROID_ROOT" in os.environ


def rest_password(secret: str, username: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()).decode()


def rest_credential(secret: str, who: str, ttl: int, now=None) -> tuple:
    username = f"{int(now if now is not None else time.time()) + int(ttl)}:{who}"
    return username, rest_password(secret, username)


def long_term_key(username: str, password: str, realm: str = REALM) -> bytes:
    return hashlib.md5(f"{username}:{realm}:{password}".encode()).digest()


# --- STUN codec -------------------------------------------------------------------------------

def msg_type(method: int, cls: int) -> int:
    m = method
    return ((m & 0x0F80) << 2) | ((m & 0x0070) << 1) | (m & 0x000F) | cls


def split_type(t: int) -> tuple:
    cls = t & 0x0110
    method = (t & 0x000F) | ((t & 0x00E0) >> 1) | ((t & 0x3E00) >> 2)
    return method, cls


def _pad(b: bytes) -> bytes:
    return b + b"\x00" * ((4 - len(b) % 4) % 4)


def xor_addr(ip: str, port: int, tid: bytes) -> bytes:
    a = ipaddress.ip_address(ip)
    xport = port ^ (MAGIC >> 16)
    if a.version == 4:
        x = int(a) ^ MAGIC
        return struct.pack("!BBH", 0, 1, xport) + struct.pack("!I", x)
    key = int.from_bytes(MAGIC_B + tid, "big")
    return struct.pack("!BBH", 0, 2, xport) + (int(a) ^ key).to_bytes(16, "big")


def parse_xor_addr(v: bytes, tid: bytes):
    if len(v) < 8:
        raise ValueError("short address")
    fam, xport = v[1], struct.unpack("!H", v[2:4])[0]
    port = xport ^ (MAGIC >> 16)
    if fam == 1:
        return str(ipaddress.IPv4Address(struct.unpack("!I", v[4:8])[0] ^ MAGIC)), port
    if fam == 2 and len(v) >= 20:
        key = int.from_bytes(MAGIC_B + tid, "big")
        return str(ipaddress.IPv6Address(int.from_bytes(v[4:20], "big") ^ key)), port
    raise ValueError("bad address family")


class Msg:
    __slots__ = ("method", "cls", "tid", "attrs", "raw", "mi_offset")

    def __init__(self, method, cls, tid, attrs=None, raw=b"", mi_offset=None):
        self.method, self.cls, self.tid = method, cls, tid
        self.attrs = attrs if attrs is not None else []
        self.raw, self.mi_offset = raw, mi_offset

    def get(self, t):
        for k, v in self.attrs:
            if k == t:
                return v
        return None


def is_stun(data: bytes) -> bool:
    return len(data) >= 20 and data[0] < 4 and data[4:8] == MAGIC_B


def parse(data: bytes) -> Msg:
    if not is_stun(data):
        raise ValueError("not STUN")
    t, ln = struct.unpack("!HH", data[:4])
    if ln % 4 or 20 + ln > len(data):
        raise ValueError("bad length")
    method, cls = split_type(t)
    tid = data[8:20]
    attrs, off, mi_off = [], 20, None
    end = 20 + ln
    while off + 4 <= end:
        at, al = struct.unpack("!HH", data[off:off + 4])
        v = data[off + 4: off + 4 + al]
        if len(v) != al:
            raise ValueError("truncated attribute")
        if mi_off is None:
            if at == A_MI:
                mi_off = off
            attrs.append((at, v))
        elif at == A_FINGERPRINT:
            attrs.append((at, v))       # only FINGERPRINT may follow MESSAGE-INTEGRITY
        off += 4 + al + ((4 - al % 4) % 4)
    return Msg(method, cls, tid, attrs, data[:end], mi_off)


def build(method: int, cls: int, tid: bytes, attrs, key: bytes = None, fingerprint: bool = True) -> bytes:
    body = b"".join(struct.pack("!HH", t, len(v)) + _pad(v) for t, v in attrs)
    if key is not None:
        hdr = struct.pack("!HHI", msg_type(method, cls), len(body) + 24, MAGIC) + tid
        mac = hmac.new(key, hdr + body, hashlib.sha1).digest()
        body += struct.pack("!HH", A_MI, 20) + mac
    if fingerprint:
        hdr = struct.pack("!HHI", msg_type(method, cls), len(body) + 8, MAGIC) + tid
        crc = (binascii.crc32(hdr + body) & 0xFFFFFFFF) ^ FP_XOR
        body += struct.pack("!HHI", A_FINGERPRINT, 4, crc)
    return struct.pack("!HHI", msg_type(method, cls), len(body), MAGIC) + tid + body


def check_integrity(m: Msg, key: bytes) -> bool:
    if m.mi_offset is None:
        return False
    raw = m.raw
    off = m.mi_offset
    hdr = raw[:2] + struct.pack("!H", off - 20 + 24) + raw[4:20]
    mac = hmac.new(key, hdr + raw[20:off], hashlib.sha1).digest()
    return hmac.compare_digest(mac, raw[off + 4: off + 24])


def error_attr(code: int, reason: str) -> bytes:
    return struct.pack("!HBB", 0, code // 100, code % 100) + reason.encode()


# --- Relay ------------------------------------------------------------------------------------

def peer_allowed(ip: str, allow_private: bool) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.is_loopback or a.is_unspecified or a.is_multicast or a.is_link_local or a.is_reserved:
        return False
    if a.version == 4 and a.packed[0] == 0:
        return False
    if not a.is_global and not allow_private:
        return False          # RFC 1918, 100.64/10 carrier NAT, ULA...
    return True


def _env_int(env, name, default):
    try:
        return int(str(env.get(name, "")).strip() or default)
    except ValueError:
        return default


def parse_port_range(text: str) -> tuple:
    try:
        a, _, b = text.strip().partition("-")
        lo, hi = int(a), int(b or a)
        if 1024 <= lo <= hi <= 65535:
            return lo, hi
    except ValueError:
        pass
    return 49160, 49200


class Allocation:
    def __init__(self, client, sock, username, key, lifetime):
        self.client = client          # (ip, port) 5-tuple on our listening socket
        self.sock = sock
        self.username = username
        self.key = key
        self.expires = time.time() + lifetime
        self.perms = {}               # peer ip -> expiry
        self.ch_by_num = {}           # channel -> [(ip, port), expiry]
        self.ch_by_peer = {}          # (ip, port) -> channel
        self.tokens = 0.0
        self.tok_at = time.time()
        self.bytes = 0


class Relay:
    """STUN + TURN server. Create, then start(); set_config() works while running."""

    def __init__(self, secret: str, public_ip_fn=lambda: None, local_ip_fn=lambda: "127.0.0.1",
                 env=os.environ, log=print):
        self.secret = secret
        self.nonce_key = secrets.token_bytes(16)
        self.public_ip_fn = public_ip_fn
        self.local_ip_fn = local_ip_fn
        self.log = log
        self.port = _env_int(env, "OMNI_RELAY_PORT", 3478)
        self.port_range = parse_port_range(env.get("OMNI_RELAY_PORTS", "49160-49200"))
        self.bind_host = env.get("OMNI_RELAY_BIND", "0.0.0.0").strip() or "0.0.0.0"
        self.allow_private = env.get("OMNI_RELAY_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes")
        self.max_alloc = max(1, _env_int(env, "OMNI_RELAY_MAX_ALLOC", 16))
        self.max_kbps = max(64, _env_int(env, "OMNI_RELAY_MAX_KBPS", 2500))
        self.max_mb_day = max(0, _env_int(env, "OMNI_RELAY_MAX_MB_DAY", 0))
        self.lock = threading.RLock()
        self.allocs = {}              # client (ip, port) -> Allocation
        self.by_sock = {}             # relayed socket -> Allocation
        self.sock = None
        self.sel = None
        self.thread = None
        self.running = False
        self.error = ""
        self.day = time.strftime("%Y-%m-%d")
        self.bytes_today = 0
        self.stats = {"binding": 0, "allocate_ok": 0, "allocate_denied": 0, "relayed_packets": 0,
                      "peer_refused": 0, "rate_dropped": 0}

    # -- lifecycle --
    def start(self) -> bool:
        with self.lock:
            if self.running:
                return True
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((self.bind_host, self.port))
                s.setblocking(False)
            except OSError as e:
                self.error = f"cannot bind UDP {self.port}: {e.strerror or e}"
                return False
            self.sock = s
            self.port = s.getsockname()[1]
            self.sel = selectors.DefaultSelector()
            self.sel.register(s, selectors.EVENT_READ, None)
            self.running = True
            self.error = ""
            self.thread = threading.Thread(target=self._loop, name="omni-relay", daemon=True)
            self.thread.start()
            return True

    def stop(self):
        with self.lock:
            self.running = False
            for a in list(self.allocs.values()):
                self._free(a)
            if self.sock:
                try:
                    self.sel.unregister(self.sock)
                except Exception:
                    pass
                self.sock.close()
                self.sock = None

    def set_config(self, max_kbps=None, max_mb_day=None, allow_private=None):
        with self.lock:
            if max_kbps is not None:
                self.max_kbps = max(64, int(max_kbps))
            if max_mb_day is not None:
                self.max_mb_day = max(0, int(max_mb_day))
            if allow_private is not None:
                self.allow_private = bool(allow_private)

    def credential(self, who: str, ttl: int = 3600) -> tuple:
        return rest_credential(self.secret, who, max(60, min(int(ttl), 24 * 3600)))

    def relay_ip(self) -> str:
        return self.public_ip_fn() or self.local_ip_fn() or "127.0.0.1"

    def snapshot(self) -> dict:
        with self.lock:
            self._roll_day()
            return {"enabled": self.running, "port": self.port if self.running else None,
                    "relay_ports": f"{self.port_range[0]}-{self.port_range[1]}",
                    "allocations": len(self.allocs), "max_allocations": self.max_alloc,
                    "max_kbps": self.max_kbps, "max_mb_day": self.max_mb_day,
                    "mb_today": round(self.bytes_today / 1e6, 2), "allow_private": self.allow_private,
                    "error": self.error, "stats": dict(self.stats)}

    # -- internals --
    def _roll_day(self):
        d = time.strftime("%Y-%m-%d")
        if d != self.day:
            self.day, self.bytes_today = d, 0

    def _over_daily(self) -> bool:
        return self.max_mb_day > 0 and self.bytes_today >= self.max_mb_day * 1_000_000

    def _make_nonce(self) -> bytes:
        ts = f"{int(time.time()):x}"
        return (ts + "-" + hmac.new(self.nonce_key, ts.encode(), hashlib.sha256).hexdigest()[:16]).encode()

    def _nonce_ok(self, nonce: bytes) -> bool:
        try:
            ts, _, mac = nonce.decode().partition("-")
            good = hmac.new(self.nonce_key, ts.encode(), hashlib.sha256).hexdigest()[:16]
            return hmac.compare_digest(mac, good) and time.time() - int(ts, 16) < NONCE_TTL
        except Exception:
            return False

    def _loop(self):
        last_sweep = time.time()
        while self.running:
            try:
                events = self.sel.select(timeout=1.0)
            except Exception:
                break
            for key, _ in events:
                sock = key.fileobj
                try:
                    data, addr = sock.recvfrom(65535)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    continue
                try:
                    with self.lock:
                        if sock is self.sock:
                            self._on_client(data, addr[:2])
                        else:
                            self._on_peer(sock, data, addr[:2])
                except Exception as e:      # never kill the relay on one bad packet
                    self.error = f"last packet error: {type(e).__name__}"
            if time.time() - last_sweep > 5:
                last_sweep = time.time()
                with self.lock:
                    self._sweep()

    def _sweep(self):
        now = time.time()
        for a in list(self.allocs.values()):
            if a.expires < now:
                self._free(a)
                continue
            a.perms = {ip: t for ip, t in a.perms.items() if t > now}
            for ch, (peer, t) in list(a.ch_by_num.items()):
                if t <= now:
                    a.ch_by_num.pop(ch, None)
                    a.ch_by_peer.pop(peer, None)

    def _free(self, a: Allocation):
        self.allocs.pop(a.client, None)
        self.by_sock.pop(a.sock, None)
        try:
            self.sel.unregister(a.sock)
        except Exception:
            pass
        a.sock.close()

    def _send(self, data: bytes, addr):
        try:
            self.sock.sendto(data, addr)
        except OSError:
            pass

    def _rate_ok(self, a: Allocation, n: int) -> bool:
        now = time.time()
        rate = self.max_kbps * 125.0           # bytes per second
        a.tokens = min(rate, a.tokens + (now - a.tok_at) * rate)   # one second of burst
        a.tok_at = now
        self._roll_day()
        if a.tokens < n or self._over_daily():
            self.stats["rate_dropped"] += 1
            return False
        a.tokens -= n
        a.bytes += n
        self.bytes_today += n
        return True

    def _on_peer(self, sock, data: bytes, peer):
        a = self.by_sock.get(sock)
        if a is None or a.expires < time.time():
            return
        if a.perms.get(peer[0], 0) < time.time():
            return                              # no permission: silently drop (RFC 8656 §10.3)
        if not self._rate_ok(a, len(data)):
            return
        self.stats["relayed_packets"] += 1
        ch = a.ch_by_peer.get(peer)
        if ch is not None:
            self._send(struct.pack("!HH", ch, len(data)) + data, a.client)
        else:
            tid = secrets.token_bytes(12)
            self._send(build(DATA, INDICATION, tid, [(A_XOR_PEER, xor_addr(peer[0], peer[1], tid)),
                                                     (A_DATA, data)], fingerprint=False), a.client)

    def _on_client(self, data: bytes, client):
        if len(data) >= 4 and 0x40 <= data[0] <= 0x7F:     # ChannelData
            a = self.allocs.get(client)
            if not a:
                return
            ch, ln = struct.unpack("!HH", data[:4])
            ent = a.ch_by_num.get(ch)
            payload = data[4:4 + ln]
            if ent and len(payload) == ln and a.perms.get(ent[0][0], 0) > time.time() and self._rate_ok(a, ln):
                self.stats["relayed_packets"] += 1
                try:
                    a.sock.sendto(payload, ent[0])
                except OSError:
                    pass
            return
        try:
            m = parse(data)
        except ValueError:
            return
        if m.method == BINDING and m.cls == REQUEST:
            self.stats["binding"] += 1
            self._send(build(BINDING, SUCCESS, m.tid, [(A_XOR_MAPPED, xor_addr(client[0], client[1], m.tid)),
                                                       (A_SOFTWARE, SOFTWARE.encode())]), client)
            return
        if m.method == SEND and m.cls == INDICATION:
            a = self.allocs.get(client)
            peer_v, payload = m.get(A_XOR_PEER), m.get(A_DATA)
            if not a or peer_v is None or payload is None:
                return
            try:
                peer = parse_xor_addr(peer_v, m.tid)
            except ValueError:
                return
            if a.perms.get(peer[0], 0) > time.time() and self._rate_ok(a, len(payload)):
                self.stats["relayed_packets"] += 1
                try:
                    a.sock.sendto(payload, peer)
                except OSError:
                    pass
            return
        if m.cls != REQUEST or m.method not in (ALLOCATE, REFRESH, CREATE_PERMISSION, CHANNEL_BIND):
            return
        unknown = [t for t, _ in m.attrs if t < 0x8000 and t not in _KNOWN_REQUIRED]
        if unknown:
            self._err(m, client, 420, "Unknown Attribute", extra=[(A_UNKNOWN, b"".join(struct.pack("!H", t) for t in unknown))])
            return
        key = self._authenticate(m, client)
        if key is None:
            return
        handler = {ALLOCATE: self._allocate, REFRESH: self._refresh,
                   CREATE_PERMISSION: self._permission, CHANNEL_BIND: self._channel}[m.method]
        handler(m, client, key)

    def _err(self, m, client, code, reason, key=None, extra=()):
        attrs = [(A_ERROR, error_attr(code, reason))] + list(extra)
        if code in (401, 438):
            attrs += [(A_REALM, REALM.encode()), (A_NONCE, self._make_nonce())]
        self._send(build(m.method, ERROR, m.tid, attrs, key=key), client)

    def _authenticate(self, m: Msg, client):
        user, realm, nonce = m.get(A_USERNAME), m.get(A_REALM), m.get(A_NONCE)
        if m.mi_offset is None or user is None or realm is None or nonce is None:
            self._err(m, client, 401, "Unauthorized")
            return None
        if not self._nonce_ok(nonce):
            self._err(m, client, 438, "Stale Nonce")
            return None
        username = user.decode("utf-8", "replace")
        try:
            expiry = int(username.split(":", 1)[0])
        except ValueError:
            expiry = 0
        key = long_term_key(username, rest_password(self.secret, username), realm.decode("utf-8", "replace"))
        if expiry < time.time() or not check_integrity(m, key):
            self._err(m, client, 401, "Unauthorized")
            return None
        a = self.allocs.get(client)
        if a is not None and m.method != ALLOCATE and a.username != username:
            self._err(m, client, 441, "Wrong Credentials", key=key)
            return None
        return key

    def _allocate(self, m, client, key):
        if client in self.allocs:
            self._err(m, client, 437, "Allocation Mismatch", key=key)
            return
        rt = m.get(A_REQ_TRANSPORT)
        if rt is None or len(rt) < 1:
            self._err(m, client, 400, "Bad Request", key=key)
            return
        if rt[0] != 17:
            self._err(m, client, 442, "Unsupported Transport Protocol", key=key)
            return
        fam = m.get(A_REQ_FAMILY)
        if fam is not None and fam[:1] != b"\x01":
            self._err(m, client, 440, "Address Family not Supported", key=key)
            return
        if m.get(A_EVEN_PORT) is not None or m.get(A_RES_TOKEN) is not None:
            self._err(m, client, 508, "Insufficient Capacity", key=key)
            return
        username = m.get(A_USERNAME).decode("utf-8", "replace")
        per_user = sum(1 for a in self.allocs.values() if a.username == username)
        if len(self.allocs) >= self.max_alloc or per_user >= MAX_PER_USER:
            self.stats["allocate_denied"] += 1
            self._err(m, client, 486, "Allocation Quota Reached", key=key)
            return
        if self._over_daily():
            self.stats["allocate_denied"] += 1
            self._err(m, client, 486, "Allocation Quota Reached (daily cap)", key=key)
            return
        sock = None
        lo, hi = self.port_range
        ports = list(range(lo, hi + 1))
        secrets.SystemRandom().shuffle(ports)
        for p in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind((self.bind_host, p))
                sock = s
                break
            except OSError:
                s.close()
        if sock is None:
            self.stats["allocate_denied"] += 1
            self._err(m, client, 508, "Insufficient Capacity", key=key)
            return
        sock.setblocking(False)
        lifetime = self._lifetime(m)
        a = Allocation(client, sock, username, key, lifetime)
        a.tokens = self.max_kbps * 125.0
        self.allocs[client] = a
        self.by_sock[sock] = a
        self.sel.register(sock, selectors.EVENT_READ, None)
        self.stats["allocate_ok"] += 1
        rport = sock.getsockname()[1]
        self._send(build(ALLOCATE, SUCCESS, m.tid, [
            (A_XOR_RELAYED, xor_addr(self.relay_ip(), rport, m.tid)),
            (A_LIFETIME, struct.pack("!I", lifetime)),
            (A_XOR_MAPPED, xor_addr(client[0], client[1], m.tid)),
            (A_SOFTWARE, SOFTWARE.encode())], key=key), client)

    def _lifetime(self, m) -> int:
        v = m.get(A_LIFETIME)
        req = struct.unpack("!I", v[:4])[0] if v and len(v) >= 4 else DEFAULT_LIFETIME
        return max(DEFAULT_LIFETIME, min(req, MAX_LIFETIME))

    def _refresh(self, m, client, key):
        a = self.allocs.get(client)
        if a is None:
            self._err(m, client, 437, "Allocation Mismatch", key=key)
            return
        v = m.get(A_LIFETIME)
        req = struct.unpack("!I", v[:4])[0] if v and len(v) >= 4 else DEFAULT_LIFETIME
        if req == 0:
            self._free(a)
            life = 0
        else:
            life = max(DEFAULT_LIFETIME, min(req, MAX_LIFETIME))
            a.expires = time.time() + life
        self._send(build(REFRESH, SUCCESS, m.tid, [(A_LIFETIME, struct.pack("!I", life))], key=key), client)

    def _peers_of(self, m):
        out = []
        for t, v in m.attrs:
            if t == A_XOR_PEER:
                out.append(parse_xor_addr(v, m.tid))
        return out

    def _permission(self, m, client, key):
        a = self.allocs.get(client)
        if a is None:
            self._err(m, client, 437, "Allocation Mismatch", key=key)
            return
        try:
            peers = self._peers_of(m)
        except ValueError:
            peers = []
        if not peers:
            self._err(m, client, 400, "Bad Request", key=key)
            return
        if not all(peer_allowed(ip, self.allow_private) for ip, _ in peers):
            self.stats["peer_refused"] += 1
            self._err(m, client, 403, "Forbidden", key=key)
            return
        exp = time.time() + PERMISSION_LIFETIME
        for ip, _ in peers:
            a.perms[ip] = exp
        self._send(build(CREATE_PERMISSION, SUCCESS, m.tid, [], key=key), client)

    def _channel(self, m, client, key):
        a = self.allocs.get(client)
        if a is None:
            self._err(m, client, 437, "Allocation Mismatch", key=key)
            return
        chv = m.get(A_CHANNEL)
        try:
            peers = self._peers_of(m)
        except ValueError:
            peers = []
        if chv is None or len(chv) < 2 or len(peers) != 1:
            self._err(m, client, 400, "Bad Request", key=key)
            return
        ch = struct.unpack("!H", chv[:2])[0]
        peer = peers[0]
        if not 0x4000 <= ch <= 0x4FFF:
            self._err(m, client, 400, "Bad Request", key=key)
            return
        if not peer_allowed(peer[0], self.allow_private):
            self.stats["peer_refused"] += 1
            self._err(m, client, 403, "Forbidden", key=key)
            return
        bound = a.ch_by_num.get(ch)
        if (bound and bound[0] != peer) or (a.ch_by_peer.get(peer, ch) != ch):
            self._err(m, client, 400, "Bad Request", key=key)
            return
        now = time.time()
        a.ch_by_num[ch] = [peer, now + CHANNEL_LIFETIME]
        a.ch_by_peer[peer] = ch
        a.perms[peer[0]] = max(a.perms.get(peer[0], 0), now + PERMISSION_LIFETIME)
        self._send(build(CHANNEL_BIND, SUCCESS, m.tid, [], key=key), client)


def load_or_create_secret(path) -> str:
    """Relay secret at ~/.omni/relay_secret (owner-only). OMNI_RELAY_SECRET overrides."""
    env = os.environ.get("OMNI_RELAY_SECRET", "").strip()
    if env:
        return env
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = f.read().strip()
            if len(s) >= 32:
                return s
    except OSError:
        pass
    s = secrets.token_hex(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(s)
    return s
