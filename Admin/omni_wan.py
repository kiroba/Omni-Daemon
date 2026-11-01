"""Internet reachability for an Omni node without Tailscale (daemon v8.21.0). Standard library only.

What it does, every OMNI_WAN_INTERVAL seconds (default 600):
  1. STUN (RFC 5389) over UDP: learns this network's public IPv4 address.
  2. UPnP IGD (when OMNI_UPNP is not "0"): asks the home router to forward TCP <port> to this
     device (lease 1 h, renewed), so other nodes on the internet can dial :5005 directly.
     Phones on mobile data have no UPnP router; that just reports "no router answered".
  3. A global IPv6 address, if the device has one (many mobile carriers hand these out).
  4. Dial-back: asks peers outside this network to open a TCP connection back to the public
     address. Only that proves "reachable"; the router mapping alone doesn't.

Nodes that are not reachable still work over the internet: they dial out to any peer that is
(a friend whose router mapped the port, a PC, a small server), and catch-up sync pulls and pushes
in both directions over that one outgoing connection. Hole punching between two nodes that are
both behind carrier NAT is not here; that case needs a reachable peer in the middle.
"""
import ipaddress
import os
import re
import socket
import struct
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from xml.etree import ElementTree

STUN_SERVERS = (("stun.l.google.com", 19302), ("stun.cloudflare.com", 3478), ("stun1.l.google.com", 19302))
SSDP_ADDR = ("239.255.255.250", 1900)
IGD_SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2",
                "urn:schemas-upnp-org:service:WANIPConnection:1",
                "urn:schemas-upnp-org:service:WANPPPConnection:1")
LEASE_S = 3600
MAGIC = 0x2112A442
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def stun_public_ipv4(timeout: float = 2.0, servers=STUN_SERVERS):
    """(ip, port) of this network's public UDP mapping, or None."""
    for host, port in servers:
        try:
            tid = os.urandom(12)
            req = struct.pack("!HHI", 0x0001, 0, MAGIC) + tid
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(req, (host, port))
                data, _ = s.recvfrom(2048)
            parsed = parse_stun_response(data, tid)
            if parsed:
                return parsed
        except OSError:
            continue
    return None


def parse_stun_response(data: bytes, tid: bytes):
    """XOR-MAPPED-ADDRESS (or MAPPED-ADDRESS) from a Binding Success Response, IPv4 only."""
    if len(data) < 20:
        return None
    mtype, mlen, magic = struct.unpack("!HHI", data[:8])
    if mtype != 0x0101 or magic != MAGIC or data[8:20] != tid:
        return None
    pos, end = 20, min(len(data), 20 + mlen)
    fallback = None
    while pos + 4 <= end:
        atype, alen = struct.unpack("!HH", data[pos:pos + 4])
        val = data[pos + 4:pos + 4 + alen]
        if atype in (0x0020, 0x0001) and len(val) >= 8 and val[1] == 0x01:
            port = struct.unpack("!H", val[2:4])[0]
            raw = val[4:8]
            if atype == 0x0020:
                port ^= MAGIC >> 16
                raw = bytes(b ^ m for b, m in zip(raw, struct.pack("!I", MAGIC)))
                return socket.inet_ntoa(raw), port
            fallback = (socket.inet_ntoa(raw), port)
        pos += 4 + alen + ((4 - alen % 4) % 4)
    return fallback


def global_ipv6():
    """This device's global IPv6 source address, or None (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
            s.connect(("2001:4860:4860::8888", 80))
            ip = s.getsockname()[0].split("%")[0]
        addr = ipaddress.IPv6Address(ip)
        return ip if addr.is_global else None
    except (OSError, ValueError):
        return None


# ---- UPnP IGD ----

def ssdp_find_igd(timeout: float = 2.5):
    """LOCATION url of the first Internet Gateway Device that answers, or None."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.settimeout(timeout)
            s.sendto(msg, SSDP_ADDR)
            deadline = time.time() + timeout
            while time.time() < deadline:
                data, _ = s.recvfrom(4096)
                m = re.search(rb"(?im)^location:\s*(\S+)", data)
                if m:
                    return m.group(1).decode("ascii", "replace")
    except OSError:
        pass
    return None


def igd_control(location: str, timeout: float = 4.0):
    """(control_url, service_type) of the router's WAN connection service, or None."""
    with _NO_PROXY.open(location, timeout=timeout) as r:
        xml = r.read(256 * 1024)
    return parse_igd_description(xml, location)


def parse_igd_description(xml: bytes, location: str):
    root = ElementTree.fromstring(xml)
    services = {}
    for svc in root.iter():
        if not svc.tag.endswith("service"):
            continue
        st = ctrl = None
        for child in svc:
            if child.tag.endswith("serviceType"):
                st = (child.text or "").strip()
            elif child.tag.endswith("controlURL"):
                ctrl = (child.text or "").strip()
        if st and ctrl:
            services[st] = ctrl
    for st in IGD_SERVICES:
        if st in services:
            return urllib.parse.urljoin(location, services[st]), st
    return None


def soap(control_url: str, service: str, action: str, args: dict, timeout: float = 4.0) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    env = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(control_url, data=env, method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"'})
    try:
        with _NO_PROXY.open(req, timeout=timeout) as r:
            return r.read(64 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        text = e.read(64 * 1024).decode("utf-8", "replace")
        code = re.search(r"<errorCode>(\d+)</errorCode>", text)
        raise RuntimeError(f"router refused {action} (UPnP error {code.group(1) if code else e.code})")


def upnp_map(local_ip: str, port: int, description: str = "Omni node"):
    """{"external_ip", "external_port", "service"} after mapping TCP port -> local_ip:port, or raises."""
    loc = ssdp_find_igd()
    if not loc:
        raise LookupError("no router answered (UPnP off, or not on a home network)")
    found = igd_control(loc)
    if not found:
        raise LookupError("the router has no WAN connection service")
    ctrl, st = found
    last = None
    for ext in [port] + list(range(port + 1, port + 6)):
        try:
            soap(ctrl, st, "AddPortMapping", {
                "NewRemoteHost": "", "NewExternalPort": ext, "NewProtocol": "TCP",
                "NewInternalPort": port, "NewInternalClient": local_ip, "NewEnabled": 1,
                "NewPortMappingDescription": description, "NewLeaseDuration": LEASE_S})
            ext_ip = None
            try:
                txt = soap(ctrl, st, "GetExternalIPAddress", {})
                m = re.search(r"<NewExternalIPAddress>([^<]*)</NewExternalIPAddress>", txt)
                ext_ip = m.group(1).strip() if m else None
            except RuntimeError:
                pass
            return {"external_ip": ext_ip or None, "external_port": ext, "service": st}
        except RuntimeError as e:
            last = e      # 718 = port taken by another device: try the next one
    raise last or RuntimeError("router refused the mapping")


def is_cgnat(ip: str) -> bool:
    try:
        return not ipaddress.ip_address(ip.split("%", 1)[0]).is_global   # CGNAT 100.64/10, RFC 1918, link-local...
    except ValueError:
        return False


class WanProbe:
    """Keeps the "wan" block of node-health current. dialback(host, port, my_port) -> dict is
    supplied by the daemon (it asks a peer over /p2p/dialback)."""

    def __init__(self, port: int, local_ip_fn, peers_fn, dialback_fn, env=os.environ):
        self.port = port
        self.local_ip_fn = local_ip_fn
        self.peers_fn = peers_fn
        self.dialback_fn = dialback_fn
        self.upnp_on = env.get("OMNI_UPNP", "1").strip() != "0"
        self.manual = env.get("OMNI_PUBLIC_ADDR", "").strip()       # e.g. "mypc.example.com:5005"
        try:
            self.interval = max(60.0, float(env.get("OMNI_WAN_INTERVAL", "600")))
        except ValueError:
            self.interval = 600.0
        self.state = {"public_addr": "", "reachable": None, "nat": "", "upnp": "off" if not self.upnp_on else "checking",
                      "ipv6": "", "detail": "Checking...", "checked_ago_s": None}
        self._checked = None
        self._lock = threading.Lock()
        self.wake = threading.Event()

    def snapshot(self) -> dict:
        with self._lock:
            out = dict(self.state)
        if self._checked:
            out["checked_ago_s"] = int(time.time() - self._checked)
        return out

    def run_forever(self):
        while True:
            try:
                self.check()
            except Exception as e:  # never kill the thread
                with self._lock:
                    self.state["detail"] = f"check failed: {type(e).__name__}"
            self.wake.wait(self.interval)
            self.wake.clear()

    def check(self):
        st = {"public_addr": "", "reachable": None, "nat": "", "upnp": "off", "ipv6": "", "detail": ""}
        local_ip = self.local_ip_fn()
        stun = stun_public_ipv4()
        pub_ip = stun[0] if stun else None
        ext_port = self.port
        if self.upnp_on and local_ip and local_ip != "127.0.0.1":
            try:
                m = upnp_map(local_ip, self.port)
                ext_port = m["external_port"]
                st["upnp"] = f"mapped :{ext_port}"
                if m["external_ip"] and is_cgnat(m["external_ip"]):
                    st["upnp"] += " (router is behind carrier NAT)"
                    st["nat"] = "carrier NAT"
            except Exception as e:
                st["upnp"] = str(e)[:120]
        st["ipv6"] = global_ipv6() or ""
        if self.manual:
            st["public_addr"] = self.manual
        elif pub_ip:
            st["public_addr"] = f"{pub_ip}:{ext_port}"
        elif st["ipv6"]:
            st["public_addr"] = f"[{st['ipv6']}]:{self.port}"
        st["reachable"] = self._dialback(pub_ip, ext_port, st["ipv6"])
        if not st["nat"]:
            st["nat"] = "no NAT" if (pub_ip and pub_ip == local_ip) else ("NAT" if pub_ip else "")
        if st["reachable"] is True:
            st["detail"] = "Other nodes can reach this one from the internet."
        elif st["reachable"] is False:
            st["detail"] = "Incoming connections are blocked. Add a peer that is reachable."
        elif st["public_addr"]:
            st["detail"] = "Not confirmed yet: no peer outside this network has checked it."
        else:
            st["detail"] = "No internet path found (offline?)."
        with self._lock:
            self.state = st
            self._checked = time.time()

    def _dialback(self, pub_ip, ext_port, ipv6):
        """True / False when a peer outside this network tested us; None when none could."""
        verdict = None
        for host, port in list(self.peers_fn())[:6]:
            try:
                r = self.dialback_fn(host, port, ext_port)
            except Exception:
                continue
            seen = str(r.get("seen_ip") or "")
            if not seen or is_cgnat(seen):
                continue                      # same LAN / tailnet: proves nothing about the internet
            if seen == pub_ip or (ipv6 and seen == ipv6) or self.manual:
                if r.get("open") is True:
                    return True
                verdict = False
        return verdict


def dialback_check(seen_ip: str, port: int, timeout: float = 3.0) -> dict:
    """Server side of /p2p/dialback: can we open TCP to the caller's address on `port`?"""
    try:
        fam = socket.AF_INET6 if ":" in seen_ip else socket.AF_INET
        with socket.socket(fam, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((seen_ip, int(port)))
        return {"seen_ip": seen_ip, "port": int(port), "open": True}
    except OSError as e:
        return {"seen_ip": seen_ip, "port": int(port), "open": False, "error": type(e).__name__}
