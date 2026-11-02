"""omni_node_core.py - the one Omni Node: screen state and actions for every platform (daemon v8.27.0).

Android (kiroba/Omni-Node-Daemon, Kotlin through Chaquopy), the desktop window (omni_node_desktop.py:
Windows, macOS, Linux) and, later, iOS are thin shells around this module:

    core = NodeCore(env={"platform": "android", "charging": True, "unmetered": True, ...})
    core.refresh()                 # I/O: node-health, paired devices, the open pairing code
    core.duties()                  # I/O, only in the process that owns the daemon: relay policy,
                                   #   router port, re-hand added peers, daily update check
    state = core.ui_state()        # pure: everything the screen shows (words, colours, switches, lists)
    result = core.do("add_peer", {"text": "203.0.113.7"})   # an action; may block (call off the UI thread)

A shell renders `state` and calls do(); it keeps only what its platform needs: starting/stopping the
node process, a foreground service, boot and sign-in start, permissions, radios, the clipboard, a
screen lock, a tray. Platform facts come in through the `env` dict (see ENV_KEYS); platform effects go
out through do()'s "effects" list (see EFFECTS). The words all come from omni_node_words.py.

The daemon also serves this on loopback (GET /api/node/ui, POST /api/node/action, with the local
token) for tools and future shells. Stdlib only, so it runs under Chaquopy.
See Admin/docs/NODE_SHARED_CORE.md.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import omni_node_words as W  # noqa: E402

CORE_SCHEMA = 1                                 # bump when ui_state()'s shape changes incompatibly
UPDATE_REPO = "kiroba/Omni-Node-Daemon"         # where Omni Node releases (APK + desktop zips) live
UPDATE_EVERY_S = 24 * 3600
PEER_REPOST_S = 60                              # the daemon forgets a handed-in peer after 120 s
DESKTOP_CAPS_MB = (1024, 5120, 20480, 0)        # 0 = unlimited (the daemon's desktop default)
PHONE_CAPS_MB = W.CAPS_MB                       # 250 MB, 1 GB, 5 GB
PHONE_RELAY_KBPS = 1500

# What a shell may tell the core about its platform. Every key is optional.
ENV_KEYS = {
    "platform": "android | ios | windows | macos | linux (default: detected)",
    "os": "human OS name, e.g. 'Android 14' (shown nowhere yet; kept for reports)",
    "charging": "bool: phone is plugged in (Relay calls policy on phones)",
    "unmetered": "bool: on Wi-Fi/Ethernet that isn't metered (Relay calls policy on phones)",
    "battery_saver": "bool: battery saver is on (phones don't relay then)",
    "autostart_available": "bool: the shell can start the node at sign-in (desktop)",
    "default_network_on": "bool: Network switch before the person ever touched it",
    "node_error": "str: why the node process stopped, when the shell knows",
    "attached": "bool: a node this shell didn't start is answering (desktop)",
    "can_stop_node": "bool: the shell can stop/start the node (False for the daemon's own HTTP view)",
    "radios": "dict|None: {'missing': bool, 'status': radio_status.json} (phones)",
    "auto_update_check": "bool: check for a new Omni Node once a day (default True)",
}

# What do() may ask a shell to do. Everything else (settings, calls to the node) the core already did.
EFFECTS = {
    "start_node": "start the node process / service",
    "stop_node": "stop the node process / service",
    "settings_changed": "tell the process that owns the node to run duties() now",
    "set_autostart": "turn start-at-sign-in on/off (args['on']), then call do('start_at_login', ...)",
}


# --------------------------------------------------------------------------- versions

def node_version() -> str:
    """The Omni Node version, identical on every platform: OMNI_NODE_VERSION, else a NODE_VERSION file
    next to this module (written by the Omni-Node-Daemon build from its single VERSION file), else 'dev'."""
    v = os.environ.get("OMNI_NODE_VERSION", "").strip()
    if not v:
        try:
            v = (HERE / "NODE_VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            v = ""
    return v if re.fullmatch(r"\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?", v or "") else "dev"


def parse_version(text: str):
    """'v0.5.0-prealpha' / '0.5.0' -> (0, 5, 0); anything else -> None."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def detect_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if "ANDROID_ROOT" in os.environ or hasattr(sys, "getandroidapilevel"):
        return "android"
    return "linux"


def profile(env: dict) -> dict:
    """What differs by platform kind, and nothing else (the words stay the same)."""
    platform = (env or {}).get("platform") or detect_platform()
    mobile = platform in ("android", "ios")
    return {
        "platform": platform,
        "mobile": mobile,
        "verb": "Tap" if mobile else "Click",
        "copy_verb": "Long-press" if mobile else "Click",
        "caps": PHONE_CAPS_MB if mobile else DESKTOP_CAPS_MB,
        "connect_hint": W.CONNECT_APPS_HINT_PHONE if mobile else W.CONNECT_APPS_HINT_DESKTOP,
        "relay_hint": W.RELAY_HINT_PHONE if mobile else W.RELAY_HINT_DESKTOP,
        "defaults": {
            "network_on": bool((env or {}).get("default_network_on", not mobile)),
            "relay_on": not mobile,             # desktops relay by default; phones only if asked
            "relay_mb_day": 1024 if mobile else 0,
            "internet_on": True,                # router port (UPnP), the daemon's default since v8.21.0
            "start_at_login": False,
            "peers": [],
            "update": {},
        },
    }


def omni_dir() -> Path:
    return Path.home() / ".omni"


# --------------------------------------------------------------------------- input parsing

_HOST_RE = re.compile(r"[A-Za-z0-9.\-:%_]{1,255}")


def parse_peer(text: str):
    """'1.2.3.4', '1.2.3.4:5005', '[2001:db8::1]:5005', '2001:db8::1', 'host.example' -> (host, port) or None."""
    t = (text or "").strip()
    if t.startswith("http://"):
        t = t[len("http://"):]
    t = t.rstrip("/")
    if not t or " " in t or len(t) > 255:
        return None
    port = 5005
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        if rest.startswith(":"):
            if not rest[1:].isdigit():
                return None
            port = int(rest[1:])
        elif rest:
            return None
    elif t.count(":") == 1:
        host, p = t.split(":")
        if not p.isdigit():
            return None
        port = int(p)
    else:
        host = t
    if not host or not 0 < port < 65536 or not _HOST_RE.fullmatch(host):
        return None
    return host, port


def peer_key(host: str, port: int) -> str:
    """How an added peer is written and shown: port 5005 is left out, IPv6 gets brackets with a port."""
    if port == 5005:
        return host
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def parse_pair_input(text: str):
    """What the person typed in Enter a code -> the /api/pair/accept body, or None.
    '123 456' -> code; 'omni://pair?p=...' -> payload; '123456 192.168.1.20[:5005]' -> code + address."""
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("omni://pair") or (len(t) > 40 and " " not in t):
        return {"payload": t}
    parts = t.split()
    addr = None
    if len(parts) > 1 and not parts[-1].replace("-", "").isdigit():
        addr = parse_peer(parts[-1])
        if addr is None:
            return None
        parts = parts[:-1]
    digits = "".join(ch for ch in "".join(parts) if ch.isdigit())
    if len(digits) != 6 or any(not (ch.isdigit() or ch in " -") for ch in " ".join(parts)):
        return None
    body = {"code": digits}
    if addr:
        body.update(host=addr[0], port=addr[1])
    return body


# --------------------------------------------------------------------------- settings (one file, every platform)

class Settings:
    """~/.omni/node_settings.json. Re-read when the file changes, so two processes (Android's screen and
    its :daemon service, or the desktop window and the daemon's /api/node/*) always agree."""
    DEFAULTS = profile({"platform": "windows"})["defaults"]   # desktop defaults when no profile is given

    def __init__(self, path: Path = None, defaults: dict = None, migrate: bool = None):
        self.path = Path(path) if path else omni_dir() / "node_settings.json"
        self.defaults = dict(defaults or self.DEFAULTS)
        self.data = dict(self.defaults)
        self._mtime = None
        self._lock = threading.RLock()
        if (path is None if migrate is None else migrate) and not self.path.exists():
            self._migrate()
        self._load()

    def _load(self):
        try:
            st = self.path.stat()
            if self._mtime == (st.st_mtime_ns, st.st_size):
                return
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            data = dict(self.defaults)
            if isinstance(loaded, dict):
                data.update({k: v for k, v in loaded.items() if k in self.defaults})
            self.data, self._mtime = data, (st.st_mtime_ns, st.st_size)
        except (OSError, ValueError):
            self._mtime = None
        if not isinstance(self.data.get("peers"), list):
            self.data["peers"] = []
        if not isinstance(self.data.get("update"), dict):
            self.data["update"] = {}

    def _migrate(self):
        """First run of the shared core: carry over what the old per-platform copies saved."""
        old = {}
        legacy_desktop = omni_dir() / "node_desktop.json"            # desktop Omni Node <= v8.26.0
        try:
            j = json.loads(legacy_desktop.read_text(encoding="utf-8"))
            if isinstance(j, dict):
                old.update({k: v for k, v in j.items() if k in self.defaults})
        except (OSError, ValueError):
            pass
        home = Path.home()                                             # Android <= 0.4.0 (HOME = filesDir)
        try:
            peers = [parse_peer(x) for x in (home / "peers.txt").read_text(encoding="utf-8").splitlines()]
            old["peers"] = [peer_key(*p) for p in peers if p]
        except OSError:
            pass
        try:
            j = json.loads((home / "relay.json").read_text(encoding="utf-8"))
            old["relay_on"] = bool(j.get("on", False))
            old["relay_mb_day"] = int(j.get("mb_day", 1024))
        except (OSError, ValueError, TypeError):
            pass
        if old:
            self.data.update(old)
            self._write()

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            st = self.path.stat()
            self._mtime = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._mtime = None

    def __getitem__(self, k):
        with self._lock:
            self._load()
            return self.data[k]

    def snapshot(self) -> dict:
        with self._lock:
            self._load()
            return json.loads(json.dumps(self.data))

    def set(self, **kw):
        with self._lock:
            self._load()                        # read-modify-write against the newest file
            self.data.update(kw)
            self._write()


# --------------------------------------------------------------------------- talking to the node

class NodeClient:
    """Loopback client for this device's daemon, with the local token when there is one."""

    def __init__(self, tel_port: int = None, ipc_port: int = None, token_fn=None):
        self.tel = f"http://127.0.0.1:{tel_port or _port('OMNI_TELEMETRY_PORT', 9100)}"
        self.ipc = f"http://127.0.0.1:{ipc_port or _port('OMNI_IPC_PORT', 5000)}"
        self._token_fn = token_fn
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def token_path() -> Path:
        o = os.environ.get("OMNI_LOCAL_TOKEN_FILE", "").strip()
        return Path(o) if o else omni_dir() / "local_token"

    def token(self):
        if self._token_fn is not None:
            return self._token_fn()
        try:
            t = self.token_path().read_text().strip()
            return t if len(t) >= 32 else None
        except OSError:
            return None

    def _call(self, url, body=None, timeout=2.0):
        h = {}
        tok = self.token()
        if tok:
            h["X-Omni-Local-Token"] = tok
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def health(self):
        """(health dict or None, error text or '')."""
        try:
            code, body = self._call(f"{self.tel}/api/v1/telemetry/node-health")
        except (OSError, ValueError):
            return None, ""          # not answering yet: "Starting..."
        if code != 200:
            return None, f"the node answered {code}"
        return body, ""

    def relay_config(self, enabled: bool, mb_day: int, kbps: int = None):
        body = {"enabled": bool(enabled), "max_mb_day": int(mb_day)}
        if kbps:
            body["max_kbps"] = int(kbps)
        return self._call(f"{self.ipc}/api/relay/config", body)

    def wan_config(self, upnp: bool):
        return self._call(f"{self.ipc}/api/wan/config", {"upnp": bool(upnp)})

    def add_peer(self, host: str, port: int):
        return self._call(f"{self.ipc}/api/sync/peers", {"host": host, "port": port, "transport": "manual"})

    # ---- pairing (daemon v8.24.0, loopback + local token)
    def pair_list(self):
        return self._call(f"{self.ipc}/api/pair/list")

    def pair_offer(self):
        return self._call(f"{self.ipc}/api/pair/offer", {})

    def pair_offer_status(self):
        return self._call(f"{self.ipc}/api/pair/offer")

    def pair_cancel(self):
        return self._call(f"{self.ipc}/api/pair/cancel", {})

    def pair_accept(self, text: str):
        body = parse_pair_input(text)
        if body is None:
            return 400, {"message": W.PAIR_BAD_INPUT}
        return self._call(f"{self.ipc}/api/pair/accept", body, timeout=45.0)

    def pair_remove(self, pubkey: str):
        return self._call(f"{self.ipc}/api/pair/remove", {"pubkey": pubkey})

    # ---- updates (public GitHub API, no token)
    def releases(self, timeout: float = 8.0):
        url = os.environ.get("OMNI_NODE_UPDATE_URL", "").strip() or \
            f"https://api.github.com/repos/{UPDATE_REPO}/releases?per_page=15"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": f"OmniNode/{node_version()}"})
        # Not the no-proxy opener: a phone or PC behind a proxy still reaches GitHub through it.
        opener = urllib.request.build_opener() if url.startswith("https://") else self._opener
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read() or b"[]")


def _port(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------- policies (pure)

def relay_policy(relay_on: bool, env: dict, mobile: bool) -> tuple:
    """(active, reason). Phones relay only while charging on unmetered Wi-Fi with battery saver off
    (Node 0.3.0 rule, now the same code on every platform); desktops whenever Relay calls is on."""
    if not relay_on:
        return False, "off"
    if mobile:
        if "unmetered" not in env or "charging" not in env:
            return False, W.RELAY_CHECKING
        if not env.get("unmetered"):
            return False, W.RELAY_WAIT_WIFI
        if not env.get("charging"):
            return False, W.RELAY_WAIT_CHARGING
        if env.get("battery_saver"):
            return False, W.RELAY_WAIT_SAVER
    return True, "relaying"


def pick_update(releases, current: str) -> dict:
    """Newest non-draft release by version (pre-releases count: every Omni Node is pre-alpha)."""
    best = None
    for r in releases if isinstance(releases, list) else []:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        v = parse_version(str(r.get("tag_name") or ""))
        if v and (best is None or v > best[0]):
            best = (v, r)
    if best is None:
        return {"latest": "", "tag": "", "url": "", "newer": False}
    v, r = best
    cur = parse_version(current)
    return {"latest": ".".join(map(str, v)), "tag": r.get("tag_name") or "", "url": r.get("html_url") or "",
            "newer": cur is None or v > cur}


# --------------------------------------------------------------------------- the core

class NodeCore:
    """Screen state + actions. Thread-safe: shells call refresh()/duties()/do() off their UI thread."""

    def __init__(self, env: dict = None, settings: Settings = None, client: NodeClient = None,
                 runtime_path: Path = None):
        self.env = dict(env or {})
        self.prof = profile(self.env)
        self.settings = settings or Settings(defaults=self.prof["defaults"])
        self.client = client or NodeClient()
        self.runtime_path = Path(runtime_path) if runtime_path else omni_dir() / "node_runtime.json"
        self.lock = threading.RLock()
        self.health = None
        self.health_err = ""
        self.pairing = None          # GET /api/pair/list
        self.offer = None            # GET/POST /api/pair/offer while a code shows
        self.peer_state = {}         # peer key -> "ok" / reason (from duties(), here or in another process)
        self.relay_state = {}        # {"active", "reason", "daemon"} from duties()
        self.relay_sent = None
        self.wan_sent = None
        self.peers_sent = None
        self.last_peer_post = 0.0
        self.update_busy = False
        self._toasts = []
        self._owner = False          # True once duties() ran in this process

    # ---- env
    def set_env(self, env: dict):
        with self.lock:
            self.env = dict(env or {})
            self.prof = profile(self.env)

    @property
    def network_on(self) -> bool:
        return bool(self.settings["network_on"])

    def toast(self, text: str):
        if text:
            with self.lock:
                self._toasts.append(text)

    # ---- I/O: what the screen shows
    def refresh(self):
        """Read the node: health, paired devices, the code that shows. Safe from any process."""
        if not self.network_on:
            with self.lock:
                self.health, self.health_err = None, ""
            return
        health, err = self.client.health()
        pairing, offer, ended = self.pairing, self.offer, None
        if health is not None:
            try:
                code, body = self.client.pair_list()
                pairing = body if code == 200 else None
                if offer is not None:
                    code, body = self.client.pair_offer_status()
                    if code == 200 and body.get("active"):
                        offer = body
                    elif code == 200:
                        ended, offer = body.get("ended") or "expired", None
            except (OSError, ValueError):
                pass
        with self.lock:
            self.health, self.health_err = health, err
            self.pairing = pairing
            if self.offer is not None or offer is None:
                self.offer = offer
        if ended:
            self.toast(W.PAIR_DONE if ended == "paired" else (W.PAIR_LOCKED if ended == "locked" else W.PAIR_ENDED))
        if not self._owner:
            self._read_runtime()

    # ---- I/O: what the node owner does in the background
    def duties(self, force_peers: bool = False):
        """Run by the process that owns the node (desktop window, Android :daemon service) every few
        seconds and right after a settings change: apply Relay calls policy and the router-port switch,
        re-hand added peers, check for updates once a day, and write node_runtime.json for other processes."""
        self._owner = True
        s = self.settings.snapshot()
        on = bool(s["network_on"])
        mobile = self.prof["mobile"]
        active, reason = relay_policy(bool(s["relay_on"]), self.env, mobile)
        daemon_msg = self.relay_state.get("daemon", "")
        if on and self.health is not None:
            want = (active, int(s["relay_mb_day"]))
            if self.relay_sent != want:
                try:
                    code, body = self.client.relay_config(active, want[1], PHONE_RELAY_KBPS if mobile else None)
                    if code == 200:
                        self.relay_sent = want
                        daemon_msg = (W.relay_cant_start(str(body.get("error") or "unknown"))
                                      if active and not body.get("enabled") else "ok")
                    else:
                        daemon_msg = W.RELAY_TOO_OLD if code == 404 else f"daemon answered {code}"
                except (OSError, ValueError) as e:
                    daemon_msg = f"not handed in ({type(e).__name__})"
            if self.wan_sent != bool(s["internet_on"]):
                try:
                    code, _ = self.client.wan_config(bool(s["internet_on"]))
                    if code in (200, 404):      # 404: daemon < 8.27.0 has no switch; don't retry every tick
                        self.wan_sent = bool(s["internet_on"])
                except (OSError, ValueError):
                    pass
            peers = list(s["peers"])
            if force_peers or self.peers_sent != peers or time.time() - self.last_peer_post > PEER_REPOST_S:
                states = {}
                for key in peers:
                    hp = parse_peer(key)
                    if not hp:
                        continue
                    try:
                        code, body = self.client.add_peer(*hp)
                        states[key] = "ok" if code == 200 else (body.get("message") or f"answered {code}")
                    except (OSError, ValueError):
                        states[key] = "not handed in yet"
                self.peer_state, self.peers_sent, self.last_peer_post = states, peers, time.time()
        elif not on:
            self.relay_sent = self.wan_sent = self.peers_sent = None
            self.peer_state = {}
        self.relay_state = {"active": bool(active and on), "reason": reason, "daemon": daemon_msg}
        upd = s.get("update") or {}
        if (self.env.get("auto_update_check", True) and on and not self.update_busy
                and os.environ.get("OMNI_NODE_AUTO_UPDATE", "1") != "0"
                and time.time() - float(upd.get("checked") or 0) > UPDATE_EVERY_S):
            threading.Thread(target=self.check_updates, name="omni-node-update", daemon=True).start()
        self._write_runtime()
        return {"line": self.status_line(), "node_id": (self.health or {}).get("node_id", ""),
                "relay_active": self.relay_state["active"], "network_on": on}

    def sync(self, force_peers: bool = False):
        """refresh() + duties(): what a shell that owns its node (the desktop window) runs each tick."""
        self.refresh()
        return self.duties(force_peers)

    def _write_runtime(self):
        data = {"peer_state": self.peer_state, "relay": self.relay_state,
                "node_error": str(self.env.get("node_error") or ""), "at": int(time.time())}
        try:
            old = json.loads(self.runtime_path.read_text(encoding="utf-8"))
            if {k: v for k, v in old.items() if k != "at"} == {k: v for k, v in data.items() if k != "at"} \
                    and time.time() - float(old.get("at") or 0) < 30:
                return
        except (OSError, ValueError, AttributeError):
            pass
        try:
            self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.runtime_path.with_name(self.runtime_path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.runtime_path)
        except OSError:
            pass

    def _read_runtime(self):
        try:
            j = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if time.time() - float(j.get("at") or 0) > 300:
            return                                   # the owner stopped writing: don't show stale states
        with self.lock:
            self.peer_state = j.get("peer_state") or {}
            self.relay_state = j.get("relay") or {}
            if j.get("node_error") and not self.env.get("node_error"):
                self.env["node_error"] = j["node_error"]

    def check_updates(self) -> dict:
        """Ask GitHub for the newest Omni Node release and remember the answer (shown as the banner)."""
        with self.lock:
            if self.update_busy:
                return self.settings["update"]
            self.update_busy = True
        try:
            try:
                info = pick_update(self.client.releases(), node_version())
                info["error"] = ""
            except (OSError, ValueError) as e:
                reason = getattr(e, "reason", None) or getattr(e, "code", None) or type(e).__name__
                info = dict(self.settings["update"] or {}, error=str(reason)[:80])
            info["checked"] = int(time.time())
            self.settings.set(update=info)
            return info
        finally:
            self.update_busy = False

    # ---- pure: the screen
    def status_line(self) -> str:
        on = self.network_on
        err = (self.health_err or str(self.env.get("node_error") or "")) if on else ""
        return W.status_line(self.health if on else None, on, err if self.health is None else "")

    def ui_state(self, take_toasts: bool = True) -> dict:
        with self.lock:
            s = self.settings.snapshot()
            p, env = self.prof, self.env
            on = bool(s["network_on"])
            h = self.health if on else None
            line = self.status_line()
            detail = W.status_detail(line, h)
            if on and env.get("attached"):
                detail += "\n" + W.ATTACHED_NOTE
            node_id = (h or {}).get("node_id") or ""
            daemon_v = (h or {}).get("version") or ""
            nv = node_version()

            wan = (h or {}).get("wan")
            wan_text, wan_color = W.wan_line(wan, on, p["copy_verb"])
            if on and not s["internet_on"]:
                wan_text = wan_text + "\n" + W.INTERNET_ROUTER_OFF if wan else W.INTERNET_ROUTER_OFF

            caps = list(p["caps"])
            mb = int(s["relay_mb_day"])
            if mb not in caps:
                caps.append(mb)
            relay_text, relay_color = self._relay_line(s, h, on)

            peers = []
            for key in s["peers"]:
                text, color = W.peer_line(key, self.peer_state.get(key, "") if on else "")
                peers.append({"key": key, "label": text, "color": color})

            quiet = (h or {}).get("quiet") or ((self.pairing or {}).get("quiet") if on else None)
            q_text, q_color = W.quiet_line(quiet) if on else (W.QUIET_NETWORK_OFF, W.TEXT_3)
            offer = self.offer if on else None
            paired = [{"pubkey": x.get("pubkey", ""), "name": x.get("name") or "",
                       "line": W.paired_line(x), "color": W.GREEN if x.get("online") else W.TEXT_2,
                       "confirm": W.pair_remove_confirm(x.get("name") or "this device")}
                      for x in ((self.pairing or {}).get("peers") or []) if on]

            upd = s.get("update") or {}
            if self.update_busy:
                u_text, u_color = W.UPDATE_CHECKING, W.TEXT_2
            elif upd.get("error"):
                u_text, u_color = W.update_failed(upd["error"]), W.AMBER
            elif upd.get("checked") and upd.get("newer"):
                u_text, u_color = W.update_available(upd.get("latest", "")), W.TEAL
            elif upd.get("checked"):
                u_text, u_color = W.update_newest(nv), W.TEXT_3
            else:
                u_text, u_color = "", W.TEXT_3

            radios = None
            if "radios" in env:
                r = env.get("radios") or {}
                st = r.get("status")
                if not on:
                    radios = {"line": W.OFF_SHORT, "color": W.TEXT_2, "ask_permission": False}
                elif r.get("missing"):
                    radios = {"line": W.RADIOS_NEED_PERMISSION, "color": W.AMBER, "ask_permission": True}
                elif not st:
                    radios = {"line": W.STARTING, "color": W.TEXT_2, "ask_permission": False}
                else:
                    radios = {"line": W.radios_line(st.get("ble", ""), st.get("ble_nearby", 0), st.get("wifi_aware", "")),
                              "color": W.TEXT_2, "ask_permission": False}

            state = {
                "core": CORE_SCHEMA,
                "platform": p["platform"],
                "node_version": nv,
                "daemon_version": daemon_v,
                "version_line": W.version_line(nv, daemon_v),
                "title": W.TITLE,
                "subtitle": W.SUBTITLE,
                "words": W.all_words(),
                "colors": {"bg": W.BG, "card": W.CARD, "card_edge": W.CARD_EDGE, "text": W.TEXT, "text_2": W.TEXT_2,
                           "text_3": W.TEXT_3, "green": W.GREEN, "teal": W.TEAL, "amber": W.AMBER, "red": W.RED,
                           "grey": W.GREY},
                "network": {"label": W.NETWORK, "on": on},
                "status": {"line": line, "color": W.status_color(line), "detail": detail,
                           "node_id": node_id, "node_line": W.node_line(node_id, p["verb"].lower()),
                           "tooltip": W.tray_tooltip(line)},
                "connect": {"button": W.CONNECT_APPS, "hint": p["connect_hint"]},
                "radios": radios,
                "internet": {"title": W.INTERNET_TITLE, "line": wan_text, "color": wan_color,
                             "address": (wan or {}).get("public_addr") or "", "switch": W.INTERNET_SWITCH,
                             "on": bool(s["internet_on"]), "hint": W.INTERNET_HINT},
                "relay": {"section": W.RELAY_SECTION, "title": W.RELAY_TITLE, "on": bool(s["relay_on"]),
                          "hint": p["relay_hint"] + " " + W.relay_cap_hint(p["verb"]), "line": relay_text,
                          "color": relay_color, "cap_mb": mb, "cap_title": W.RELAY_CAP_TITLE,
                          "caps": [{"mb": c, "label": W.cap_choice(c)} for c in caps]},
                "peers": {"title": W.PEERS_TITLE, "hint": W.peers_hint(p["verb"]), "button": W.ADD_PEER,
                          "items": peers},
                "pairing": {"title": W.PAIR_TITLE, "hint": W.PAIR_HINT, "quiet_line": q_text, "quiet_color": q_color,
                            "offer": None if not offer else {
                                "code": W.pair_code_display(offer.get("code", "")),
                                "expiry": W.pair_expires(offer.get("expires_in", 0), offer.get("attempts_left", 0)),
                                "payload": offer.get("payload", ""), "howto": W.PAIR_CODE_HOWTO,
                                "warning": W.PAIR_TEXT_WARNING},
                            "paired": paired, "none": W.PAIR_NONE},
                "update": {"title": W.UPDATE_TITLE, "button": W.UPDATE_CHECK, "line": u_text, "color": u_color,
                           "url": upd.get("url", "") if upd.get("newer") else "", "get": W.UPDATE_GET,
                           "newer": bool(upd.get("newer"))},
                "background": None if not env.get("autostart_available") else {
                    "title": W.BACKGROUND_TITLE, "label": W.START_AT_LOGIN, "on": bool(s["start_at_login"])},
                "toasts": [],
            }
            if take_toasts:
                state["toasts"], self._toasts = self._toasts, []
            return state

    def _relay_line(self, s: dict, h, on: bool):
        cap = W.cap_label(int(s["relay_mb_day"]))
        if not on or not s["relay_on"] or not self.prof["mobile"]:
            if on and s["relay_on"] and self.relay_state.get("daemon") not in (None, "", "ok"):
                return W.problem(self.relay_state["daemon"]), W.AMBER
            return W.relay_line((h or {}).get("relay") if h else None, on, bool(s["relay_on"]),
                                int(s["relay_mb_day"]))
        active, reason = relay_policy(True, self.env, True)
        if not active:
            return (reason if reason == W.RELAY_CHECKING else W.relay_waiting(reason, cap)), W.TEXT_2
        msg = self.relay_state.get("daemon", "")
        if msg and msg != "ok":
            return W.problem(msg), W.AMBER
        return W.relay_line((h or {}).get("relay"), on, True, int(s["relay_mb_day"]))

    # ---- actions
    def do(self, action: str, args: dict = None) -> dict:
        """Run one action. Returns {"ok": bool, "toast": str, "effects": [...], plus action extras}.
        May block on the node (pair_accept up to 45 s, check_updates up to 8 s): call off the UI thread."""
        args = args if isinstance(args, dict) else {}
        fn = getattr(self, "_do_" + str(action), None)
        if fn is None or not str(action).isidentifier():
            return {"ok": False, "toast": "", "effects": [], "error": f"unknown action {action!r}"}
        try:
            out = fn(args) or {}
        except (OSError, ValueError) as e:
            out = {"ok": False, "toast": f"can't reach the node ({type(e).__name__})"}
        out.setdefault("ok", True)
        out.setdefault("toast", "")
        out.setdefault("effects", [])
        return out

    def _do_network(self, a):
        on = bool(a.get("on", not self.network_on))
        if self.env.get("can_stop_node") is False:
            return {"ok": False, "toast": W.NETWORK_HERE_ONLY}
        self.settings.set(network_on=on)
        with self.lock:
            self.health, self.health_err, self.offer = None, "", None
            self.relay_sent = self.wan_sent = self.peers_sent = None
        toast = W.ATTACHED_STOP if (not on and self.env.get("attached")) else ""
        return {"toast": toast, "effects": ["start_node" if on else "stop_node"]}

    def _do_token(self, a):
        tok = self.client.token() if self.network_on else None
        if not tok:
            return {"ok": False, "toast": W.TOKEN_NOT_YET}
        return {"token": tok, "title": W.TOKEN_TITLE, "howto": W.TOKEN_HOWTO, "copy_label": W.TOKEN_COPY,
                "confirm": W.TOKEN_CONFIRM_DESKTOP, "unlock_title": W.CONNECT_APPS, "unlock_subtitle": W.UNLOCK_SUBTITLE}

    def _do_copy_token(self, a):
        tok = self.client.token() if self.network_on else None
        if not tok:
            return {"ok": False, "toast": W.TOKEN_NOT_YET}
        return {"copy": tok, "sensitive": True, "toast": W.TOKEN_COPIED}

    def _do_copy_node_key(self, a):
        nid = (self.health or {}).get("node_id") or ""
        return {"ok": bool(nid), "copy": nid, "toast": W.NODE_KEY_COPIED if nid else ""}

    def _do_copy_address(self, a):
        addr = ((self.health or {}).get("wan") or {}).get("public_addr") or ""
        return {"ok": bool(addr), "copy": addr, "toast": W.ADDRESS_COPIED if addr else ""}

    def _do_add_peer(self, a):
        hp = parse_peer(str(a.get("text", "")))
        if not hp:
            return {"ok": False, "toast": W.PEER_BAD}
        key = peer_key(*hp)
        peers = list(self.settings["peers"])
        if hp not in [parse_peer(k) for k in peers]:
            self.settings.set(peers=peers + [key])
        self.last_peer_post = 0.0
        return {"toast": W.peer_added(key), "effects": ["settings_changed"], "key": key}

    def _do_remove_peer(self, a):
        key = str(a.get("key", ""))
        self.settings.set(peers=[k for k in self.settings["peers"] if k != key])
        self.peer_state.pop(key, None)
        return {"effects": ["settings_changed"]}

    def _do_relay(self, a):
        self.settings.set(relay_on=bool(a.get("on", not self.settings["relay_on"])))
        self.relay_sent = None
        return {"effects": ["settings_changed"]}

    def _do_relay_cap(self, a):
        mb = int(a.get("mb", -1))
        if mb not in self.prof["caps"]:
            return {"ok": False, "toast": W.problem(f"no {mb} MB limit here")}
        self.settings.set(relay_mb_day=mb)
        self.relay_sent = None
        return {"effects": ["settings_changed"]}

    def _do_internet(self, a):
        on = bool(a.get("on", not self.settings["internet_on"]))
        self.settings.set(internet_on=on)
        self.wan_sent = None
        if self.network_on and self.health is not None:
            try:
                code, _ = self.client.wan_config(on)
                if code == 200:
                    self.wan_sent = on
            except (OSError, ValueError):
                pass
        return {"effects": ["settings_changed"]}

    def _do_pair_offer(self, a):
        if not self.network_on:
            return {"ok": False, "toast": W.NETWORK_FIRST}
        code, body = self.client.pair_offer()
        if code == 200 and body.get("active"):
            self.offer = body
            return {}
        return {"ok": False, "toast": self._node_msg(code, body)}

    def _do_pair_cancel(self, a):
        self.offer = None
        try:
            self.client.pair_cancel()
        except (OSError, ValueError):
            pass
        return {}

    def _do_copy_pair_text(self, a):
        payload = (self.offer or {}).get("payload", "")
        return {"ok": bool(payload), "copy": payload, "sensitive": True,
                "toast": W.PAIR_TEXT_COPIED if payload else ""}

    def _do_pair_accept(self, a):
        if not self.network_on:
            return {"ok": False, "toast": W.NETWORK_FIRST}
        text = str(a.get("text", ""))
        if parse_pair_input(text) is None:
            return {"ok": False, "toast": W.PAIR_BAD_INPUT}
        code, body = self.client.pair_accept(text)
        self._reload_pairing()
        toast = {200: W.PAIR_DONE, 403: W.PAIR_WRONG, 410: W.PAIR_LOCKED}.get(code) or self._node_msg(code, body)
        return {"ok": code == 200, "toast": toast}

    def _do_pair_remove(self, a):
        code, body = self.client.pair_remove(str(a.get("pubkey", "")))
        self._reload_pairing()
        return {"ok": code == 200, "toast": "" if code == 200 else self._node_msg(code, body)}

    def _do_check_updates(self, a):
        info = self.check_updates()
        if info.get("error"):
            return {"ok": False, "toast": W.update_failed(info["error"])}
        return {"toast": W.update_available(info["latest"]) if info.get("newer") else W.update_newest(node_version()),
                "url": info.get("url", "") if info.get("newer") else ""}

    def _do_start_at_login(self, a):
        """The shell changes the OS setting first (env code), then reports here."""
        if a.get("error"):
            return {"ok": False, "toast": W.autostart_failed(str(a["error"]))}
        self.settings.set(start_at_login=bool(a.get("on")))
        return {}

    def _reload_pairing(self):
        try:
            code, body = self.client.pair_list()
            if code == 200:
                self.pairing = body
        except (OSError, ValueError):
            pass

    @staticmethod
    def _node_msg(code, body) -> str:
        if code == 404 and not (body or {}).get("status"):
            return W.PAIR_NEEDS_UPDATE
        return (body or {}).get("message") or W.node_answered(code)


ACTIONS = sorted(n[4:] for n in dir(NodeCore) if n.startswith("_do_"))
