"""Omni Node for Windows, macOS and Linux: the same screen as Omni Node on Android.

    python Admin/omni_node_desktop.py               opens the window (and a tray icon if pystray is installed)
    python Admin/omni_node_desktop.py --background  starts hidden in the tray (what "Start when I sign in" runs)

Same sections, words and colours as the Android app (kiroba/Omni-Node-Daemon), from omni_node_words.py:
  Network switch + status card (dot + one plain line), Connect apps (node token, shown after you confirm),
  Internet (your address and whether others can reach you), Relay calls (on by default on desktops, daily
  limit), Peers (add a friend's address, your phone, or a server), Pair a device (daemon v8.24.0: show a
  6-digit code + QR, enter a code, paired devices with Remove, and the Quiet mode line).

It starts the daemon (omni_live_daemon_server.py) as a child with OMNI_LOCAL_TOKEN=1, exactly like the
Android app, so other apps need the token to use :5000/:9100. If a daemon is already running it attaches
to it instead (then the Network switch can't stop it; the card says so).

Only the standard library is required (tkinter). Optional: `pip install pystray pillow` for a tray icon
with a status colour and Open / Pause / Quit (issue #80); `pip install qrcode` (pure Python) to also draw
the pairing QR code (without it the code and the pairing text are shown). Settings: ~/.omni/node_desktop.json.
"""
import json
import os
import subprocess
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

DESKTOP_CAPS_MB = (1024, 5120, 20480, 0)   # 0 = unlimited (the daemon's desktop default)
REFRESH_MS = 3000
PEER_REPOST_S = 60                          # the daemon forgets a handed-in peer after 120 s
APP_NAME = "OmniNode"


def _port(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def omni_dir() -> Path:
    return Path.home() / ".omni"


# --------------------------------------------------------------------------- settings

class Settings:
    DEFAULTS = {"network_on": True, "start_at_login": False, "relay_on": True, "relay_mb_day": 0, "peers": []}

    def __init__(self, path: Path = None):
        self.path = path or omni_dir() / "node_desktop.json"
        self.data = dict(self.DEFAULTS)
        try:
            loaded = json.loads(self.path.read_text())
            if isinstance(loaded, dict):
                self.data.update({k: v for k, v in loaded.items() if k in self.DEFAULTS})
        except (OSError, ValueError):
            pass
        if not isinstance(self.data.get("peers"), list):
            self.data["peers"] = []

    def __getitem__(self, k):
        return self.data[k]

    def set(self, **kw):
        self.data.update(kw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)


def parse_peer(text: str):
    """'1.2.3.4', '1.2.3.4:5005', '[2001:db8::1]:5005', 'host.example' -> (host, port) or None."""
    t = (text or "").strip()
    if not t or " " in t or len(t) > 255:
        return None
    port = 5005
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        if rest.startswith(":"):
            if not rest[1:].isdigit():
                return None
            port = int(rest[1:])
    elif t.count(":") == 1:
        host, p = t.split(":")
        if not p.isdigit():
            return None
        port = int(p)
    else:
        host = t
    if not host or not 0 < port < 65536:
        return None
    return host, port


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


def qr_matrix(text: str):
    """QR modules as a list of bool rows when the pure-Python `qrcode` package is installed, else None."""
    try:
        import qrcode
    except ImportError:
        return None
    try:
        qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(text)
        qr.make(fit=True)
        return qr.get_matrix()
    except Exception:
        return None


# --------------------------------------------------------------------------- talking to the node

class NodeClient:
    """Loopback client for this computer's daemon, with the local token when there is one."""

    def __init__(self):
        self.tel = f"http://127.0.0.1:{_port('OMNI_TELEMETRY_PORT', 9100)}"
        self.ipc = f"http://127.0.0.1:{_port('OMNI_IPC_PORT', 5000)}"
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def token_path() -> Path:
        o = os.environ.get("OMNI_LOCAL_TOKEN_FILE", "").strip()
        return Path(o) if o else omni_dir() / "local_token"

    def token(self):
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

    def relay_config(self, enabled: bool, mb_day: int):
        return self._call(f"{self.ipc}/api/relay/config", {"enabled": bool(enabled), "max_mb_day": int(mb_day)})

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
            return 400, {"message": "Type the 6-digit code, or paste the pairing text."}
        return self._call(f"{self.ipc}/api/pair/accept", body, timeout=45.0)

    def pair_remove(self, pubkey: str):
        return self._call(f"{self.ipc}/api/pair/remove", {"pubkey": pubkey})


# --------------------------------------------------------------------------- the daemon process

class DaemonProcess:
    def __init__(self, client: NodeClient):
        self.client = client
        self.proc = None
        self.attached = False     # a daemon we didn't start was already answering
        self.error = ""

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        self.error = ""
        if self.running():
            return
        health, _ = self.client.health()
        if health is not None:
            self.attached = True
            return
        self.attached = False
        env = dict(os.environ, OMNI_LOCAL_TOKEN="1", PYTHONUTF8="1")
        omni_dir().mkdir(parents=True, exist_ok=True)
        log = open(omni_dir() / "node_desktop_daemon.log", "a", encoding="utf-8")
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen([sys.executable, str(HERE / "omni_live_daemon_server.py")],
                                         cwd=str(omni_dir()), env=env, stdout=log, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, **kw)
        except OSError as e:
            self.error = f"couldn't start the daemon ({e})"
        finally:
            log.close()

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def exit_problem(self) -> str:
        """A plain reason when our child died (e.g. another daemon holds the ports)."""
        if self.proc is not None and self.proc.poll() is not None:
            try:
                tail = (omni_dir() / "node_desktop_daemon.log").read_text(encoding="utf-8", errors="replace")[-600:]
            except OSError:
                tail = ""
            if "already in use" in tail:
                return "another program is using the node's ports"
            return f"the daemon stopped (exit {self.proc.returncode})"
        return self.error


# --------------------------------------------------------------------------- start when I sign in

def login_command() -> list:
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"   # no console window at sign-in
    return [str(exe), str(Path(__file__).resolve()), "--background"]


def set_start_at_login(on: bool) -> str:
    """Per-user, no admin. Returns '' on success or a plain reason."""
    cmd = login_command()
    try:
        if os.name == "nt":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_SET_VALUE)
            with key:
                if on:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
        elif sys.platform == "darwin":
            p = Path.home() / "Library/LaunchAgents/com.omniverse.node.plist"
            if on:
                args = "".join(f"<string>{a}</string>" for a in cmd)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
                             "<key>Label</key><string>com.omniverse.node</string>"
                             f"<key>ProgramArguments</key><array>{args}</array>"
                             "<key>RunAtLoad</key><true/></dict></plist>\n")
            elif p.exists():
                p.unlink()
        else:
            p = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart/omni-node.desktop"
            if on:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("[Desktop Entry]\nType=Application\nName=Omni Node\n"
                             f"Exec={' '.join(cmd)}\nX-GNOME-Autostart-enabled=true\n")
            elif p.exists():
                p.unlink()
        return ""
    except OSError as e:
        return str(e)


# --------------------------------------------------------------------------- the window

class NodeWindow:
    def __init__(self, root, settings: Settings = None, client: NodeClient = None, manage_daemon=True):
        import tkinter as tk
        self.tk = tk
        self.root = root
        self.settings = settings or Settings()
        self.client = client or NodeClient()
        self.daemon = DaemonProcess(self.client)
        self.manage_daemon = manage_daemon
        self.health = None
        self.health_err = ""
        self.peer_state = {}
        self.last_peer_post = 0.0
        self.relay_sent = None
        self.tray = None
        self.node_id = ""
        self.wan_addr = ""
        self._busy = False
        self.pairing = None        # /api/pair/list answer
        self.offer = None          # /api/pair/offer answer while a code shows
        self._pair_busy = False
        self._offer_drawn = None   # the payload the QR canvas was drawn for

        root.title(W.TITLE)
        root.configure(bg=W.BG)
        root.geometry("460x780")
        root.minsize(400, 560)
        f = ("Segoe UI", 10) if os.name == "nt" else ("Helvetica", 10)
        self.font, self.small = f, (f[0], 9)
        self._wraps = []          # (label, inset): wraplength follows the window width
        outer = self._scroll_area(root)

        tk.Label(outer, text=W.TITLE, bg=W.BG, fg=W.TEXT, font=(f[0], 20, "bold"), anchor="w").pack(fill="x")
        sub = tk.Label(outer, text=W.SUBTITLE, bg=W.BG, fg=W.TEXT_2, font=f, wraplength=410, justify="left",
                       anchor="w")
        sub.pack(fill="x", pady=(2, 10))
        self._wraps.append((sub, 0))

        row = tk.Frame(outer, bg=W.BG)
        row.pack(fill="x")
        tk.Label(row, text="Network", bg=W.BG, fg=W.TEXT, font=(f[0], 14, "bold")).pack(side="left")
        self.net_btn = tk.Button(row, width=6, relief="flat", font=(f[0], 10, "bold"), command=self.toggle_network,
                                 cursor="hand2", bd=0, padx=8, pady=3)
        self.net_btn.pack(side="right")

        card = tk.Frame(outer, bg=W.CARD, highlightbackground=W.CARD_EDGE, highlightthickness=1, padx=14, pady=12)
        card.pack(fill="x", pady=(8, 4))
        top = tk.Frame(card, bg=W.CARD)
        top.pack(fill="x")
        self.dot = tk.Label(top, text="●", bg=W.CARD, fg=W.GREY, font=(f[0], 14))
        self.dot.pack(side="left")
        self.status = tk.Label(top, text=W.OFF, bg=W.CARD, fg=W.TEXT, font=(f[0], 12, "bold"), anchor="w")
        self.status.pack(side="left", padx=(6, 0))
        self.detail = tk.Label(card, text="", bg=W.CARD, fg=W.TEXT_2, font=f, anchor="w", justify="left",
                               wraplength=400)
        self.detail.pack(fill="x", pady=(4, 0))
        self._wraps.append((self.detail, 32))
        self.node = tk.Label(card, text="", bg=W.CARD, fg=W.TEXT_3, font=self.small, anchor="w", cursor="hand2")
        self.node.pack(fill="x")
        self.node.bind("<Button-1>", lambda _e: self.copy(self.node_id, "Node key copied"))

        self.connect_btn = self._button(outer, "Connect apps", self.connect_apps)
        self._hint(outer, W.CONNECT_APPS_HINT_DESKTOP)

        self._section(outer, "Internet")
        self.wan = self._line(outer)
        self.wan.configure(cursor="hand2")
        self.wan.bind("<Button-1>", lambda _e: self.copy(self.wan_addr, "Address copied"))

        self._section(outer, "Relay calls")
        rrow = tk.Frame(outer, bg=W.BG)
        rrow.pack(fill="x")
        tk.Label(rrow, text=W.RELAY_TITLE, bg=W.BG, fg=W.TEXT, font=(f[0], 11)).pack(side="left")
        self.relay_btn = tk.Button(rrow, width=6, relief="flat", font=(f[0], 9, "bold"), command=self.toggle_relay,
                                   cursor="hand2", bd=0, padx=6, pady=2)
        self.relay_btn.pack(side="right")
        self._hint(outer, W.RELAY_HINT_DESKTOP + " Click the line below to change the daily limit.")
        self.relay = self._line(outer)
        self.relay.configure(cursor="hand2")
        self.relay.bind("<Button-1>", lambda _e: self.pick_cap())

        self._section(outer, "Peers")
        self._hint(outer, W.PEERS_HINT.replace("Tap one", "Click one"))
        self._button(outer, "Add peer", self.add_peer_dialog)
        self.peers_box = tk.Frame(outer, bg=W.BG)
        self.peers_box.pack(fill="x")

        self._section(outer, W.PAIR_TITLE)
        self._hint(outer, W.PAIR_HINT)
        self.quiet = self._line(outer)
        prow = self.tk.Frame(outer, bg=W.BG)
        prow.pack(fill="x")
        self.pair_show_btn = self._button(prow, W.PAIR_SHOW, self.pair_show)
        self.pair_show_btn.pack_configure(side="left", padx=(0, 8))
        self.pair_enter_btn = self._button(prow, W.PAIR_ENTER, self.pair_enter_dialog)
        self.pair_enter_btn.pack_configure(side="left")
        self.offer_box = tk.Frame(outer, bg=W.CARD, highlightbackground=W.CARD_EDGE, highlightthickness=1,
                                  padx=14, pady=10)
        self.offer_code = tk.Label(self.offer_box, text="", bg=W.CARD, fg=W.TEXT, font=("Consolas", 26, "bold"),
                                   anchor="w")
        self.offer_code.pack(fill="x")
        self.offer_expiry = tk.Label(self.offer_box, text="", bg=W.CARD, fg=W.TEXT_2, font=self.small, anchor="w")
        self.offer_expiry.pack(fill="x")
        self.offer_howto = tk.Label(self.offer_box, text=W.PAIR_CODE_HOWTO, bg=W.CARD, fg=W.TEXT_2, font=self.small,
                                    wraplength=380, justify="left", anchor="w")
        self.offer_howto.pack(fill="x", pady=(4, 6))
        self._wraps.append((self.offer_howto, 32))
        self.offer_qr = tk.Canvas(self.offer_box, width=1, height=1, bg="#FFFFFF", highlightthickness=0)
        self.offer_text = tk.Entry(self.offer_box, font=("Consolas", 8), relief="flat", bg=W.BG, fg=W.TEXT_2,
                                   readonlybackground=W.BG)
        self.offer_text.pack(fill="x", pady=(0, 2))
        tk.Label(self.offer_box, text=W.PAIR_TEXT_WARNING, bg=W.CARD, fg=W.TEXT_3, font=self.small, anchor="w",
                 wraplength=380, justify="left").pack(fill="x")
        obtn = tk.Frame(self.offer_box, bg=W.CARD)
        obtn.pack(fill="x", pady=(6, 0))
        tk.Button(obtn, text=W.PAIR_COPY, command=self.pair_copy_text, relief="flat", bg="#2A2A2A", fg=W.TEXT,
                  bd=0, padx=10, pady=4, cursor="hand2").pack(side="left")
        tk.Button(obtn, text=W.PAIR_STOP, command=self.pair_stop, relief="flat", bg="#2A2A2A", fg=W.TEXT,
                  bd=0, padx=10, pady=4, cursor="hand2").pack(side="left", padx=(8, 0))
        self.paired_box = tk.Frame(outer, bg=W.BG)
        self.paired_box.pack(fill="x", pady=(6, 0))

        self._section(outer, "Background")
        self.login_var = tk.BooleanVar(value=bool(self.settings["start_at_login"]))
        tk.Checkbutton(outer, text="Start when I sign in", variable=self.login_var, command=self.toggle_login,
                       bg=W.BG, fg=W.TEXT_2, selectcolor=W.CARD, activebackground=W.BG, activeforeground=W.TEXT,
                       font=f, anchor="w").pack(fill="x")
        self.toast = tk.Label(outer, text="", bg=W.BG, fg=W.TEAL, font=self.small, anchor="w")
        self.toast.pack(fill="x", pady=(8, 0))

        self._paint_switches()
        if self.settings["network_on"] and self.manage_daemon:
            self.daemon.start()
        self.render()
        self.root.after(200, self.tick)

    # ---- scrolling (the Peers section was cut off at the default size on Windows)
    def _scroll_area(self, root):
        """Canvas + Scrollbar around the content frame. Returns the frame the sections pack into."""
        tk = self.tk
        box = tk.Frame(root, bg=W.BG)
        box.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(box, bg=W.BG, highlightthickness=0, bd=0)
        self.scrollbar = tk.Scrollbar(box, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=W.BG, padx=18, pady=14)
        self._content_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._on_content_size)
        self.canvas.bind("<Configure>", self._on_canvas_size)
        # Wheel: Windows and macOS send <MouseWheel>; X11 sends Button-4/5.
        root.bind_all("<MouseWheel>", self._on_wheel, add="+")
        root.bind_all("<Button-4>", self._on_wheel, add="+")
        root.bind_all("<Button-5>", self._on_wheel, add="+")
        return self.content

    def _on_content_size(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_size(self, e):
        self.canvas.itemconfigure(self._content_id, width=e.width)   # content is as wide as the canvas
        inner = e.width - 36                                            # content padx 18 on each side
        for lab, inset in self._wraps:
            lab.configure(wraplength=max(200, inner - inset))

    def _on_wheel(self, e):
        try:
            if e.widget.winfo_toplevel() is not self.root:
                return              # a dialog (Add peer, daily limit) is under the pointer
        except (AttributeError, KeyError, self.tk.TclError):
            return
        if self.content.winfo_height() <= self.canvas.winfo_height():
            return                  # everything fits: nothing to scroll
        if getattr(e, "num", None) == 4:
            step = -1
        elif getattr(e, "num", None) == 5:
            step = 1
        elif sys.platform == "darwin":
            step = -int(e.delta)
        else:
            step = -int(e.delta / 120) or (-1 if e.delta > 0 else 1)
        self.canvas.yview_scroll(step, "units")

    # ---- small builders
    def _section(self, parent, title):
        self.tk.Label(parent, text=title, bg=W.BG, fg=W.TEXT, font=(self.font[0], 13, "bold"),
                      anchor="w").pack(fill="x", pady=(14, 2))

    def _hint(self, parent, text):
        lab = self.tk.Label(parent, text=text, bg=W.BG, fg=W.TEXT_3, font=self.small, wraplength=410,
                            justify="left", anchor="w")
        lab.pack(fill="x")
        self._wraps.append((lab, 0))

    def _line(self, parent):
        lab = self.tk.Label(parent, text="", bg=W.BG, fg=W.TEXT_2, font=self.font, wraplength=410,
                            justify="left", anchor="w")
        lab.pack(fill="x", pady=(2, 0))
        self._wraps.append((lab, 0))
        return lab

    def _button(self, parent, text, cmd):
        b = self.tk.Button(parent, text=text, command=cmd, relief="flat", bg="#2A2A2A", fg=W.TEXT,
                           activebackground="#3A3A3A", activeforeground=W.TEXT, font=self.font, cursor="hand2",
                           bd=0, padx=10, pady=5)
        b.pack(anchor="w", pady=(8, 2))
        return b

    def _paint_switches(self):
        for btn, on in ((self.net_btn, self.settings["network_on"]), (self.relay_btn, self.settings["relay_on"])):
            btn.configure(text="On" if on else "Off", bg=W.GREEN if on else "#3A3A3A", fg=W.TEXT,
                          activebackground=W.GREEN if on else "#3A3A3A")

    def say(self, text):
        self.toast.configure(text=text)
        self.root.after(4000, lambda: self.toast.configure(text="") if self.toast.cget("text") == text else None)

    def copy(self, text, msg):
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.say(msg)

    # ---- actions
    def toggle_network(self):
        on = not self.settings["network_on"]
        self.settings.set(network_on=on)
        if self.manage_daemon:
            if on:
                self.daemon.start()
            else:
                if self.daemon.attached:
                    self.say("This node was started outside Omni Node, so stop it where it runs.")
                self.daemon.stop()
        self.health, self.health_err, self.relay_sent = None, "", None
        self._paint_switches()
        self.render()

    def toggle_relay(self):
        self.settings.set(relay_on=not self.settings["relay_on"])
        self.relay_sent = None
        self._paint_switches()
        self.render()

    def pick_cap(self):
        tk = self.tk
        win = tk.Toplevel(self.root, bg=W.BG, padx=16, pady=12)
        win.title("Daily relay limit")
        win.transient(self.root)
        var = tk.IntVar(value=int(self.settings["relay_mb_day"]))
        for mb in DESKTOP_CAPS_MB:
            label = "No limit" if mb == 0 else f"Up to {W.cap_label(mb)} a day"
            tk.Radiobutton(win, text=label, variable=var, value=mb, bg=W.BG, fg=W.TEXT, selectcolor=W.CARD,
                           activebackground=W.BG, activeforeground=W.TEXT, font=self.font, anchor="w").pack(fill="x")

        def ok():
            self.settings.set(relay_mb_day=int(var.get()))
            self.relay_sent = None
            win.destroy()
            self.render()
        tk.Button(win, text="OK", command=ok, relief="flat", bg=W.GREEN, fg=W.TEXT, bd=0, padx=12,
                  pady=4).pack(anchor="e", pady=(8, 0))

    def connect_apps(self):
        from tkinter import messagebox
        tok = self.client.token()
        if not tok:
            messagebox.showinfo("Connect apps", "Turn on Network first; the token is made when the node starts.",
                                parent=self.root)
            return
        if not messagebox.askyesno("Connect apps", "Show this computer's node token? Anyone who sees it can use "
                                   "your node, so check nobody is looking at your screen.", parent=self.root):
            return
        tk = self.tk
        win = tk.Toplevel(self.root, bg=W.BG, padx=16, pady=12)
        win.title("Connect apps")
        win.transient(self.root)
        tk.Label(win, text=W.TOKEN_HOWTO, bg=W.BG, fg=W.TEXT_2, font=self.font, wraplength=380,
                 justify="left").pack(fill="x")
        e = tk.Entry(win, font=("Consolas", 10), width=44, relief="flat", bg=W.CARD, fg=W.TEXT,
                     insertbackground=W.TEXT)
        e.insert(0, tok)
        e.configure(state="readonly", readonlybackground=W.CARD)
        e.pack(fill="x", pady=8)

        def copy_close():
            self.copy(tok, "Token copied. Paste it into the app's Hub settings.")
            win.destroy()
        tk.Button(win, text="Copy token", command=copy_close, relief="flat", bg=W.GREEN, fg=W.TEXT, bd=0,
                  padx=12, pady=4).pack(anchor="e")

    def add_peer_dialog(self):
        from tkinter import simpledialog
        text = simpledialog.askstring("Add peer", "Address of another node. Port 5005 unless you add one.",
                                      parent=self.root)
        if text is None:
            return
        p = parse_peer(text)
        if not p:
            self.say("That address doesn't look right.")
            return
        key = f"{p[0]}:{p[1]}"
        if key not in self.settings["peers"]:
            self.settings.set(peers=self.settings["peers"] + [key])
        self.last_peer_post = 0.0
        self.say(f"Added. Your node will sync with {key}.")
        self.render()

    def remove_peer(self, key):
        from tkinter import messagebox
        if messagebox.askyesno(f"Remove {key}?", "Your node stops syncing with it within two minutes.",
                               parent=self.root):
            self.settings.set(peers=[k for k in self.settings["peers"] if k != key])
            self.peer_state.pop(key, None)
            self.render()

    def toggle_login(self):
        on = bool(self.login_var.get())
        err = set_start_at_login(on)
        if err:
            self.login_var.set(not on)
            self.say(f"Couldn't change that: {err}")
        else:
            self.settings.set(start_at_login=on)

    # ---- Pair a device (network calls off the UI thread; results come back through root.after)
    def _bg(self, fn, done):
        def run():
            try:
                result = fn()
            except (OSError, ValueError) as e:
                result = (0, {"message": f"can't reach the node ({e})"})
            try:
                self.root.after(0, lambda: done(*result))
            except RuntimeError:
                pass
        threading.Thread(target=run, daemon=True).start()

    def pair_show(self):
        if not self.settings["network_on"]:
            self.say("Turn on Network first.")
            return

        def done(code, body):
            if code == 200 and body.get("active"):
                self.offer = body
            else:
                self.say(body.get("message") or f"The node answered {code}")
            self.render()
        self._bg(self.client.pair_offer, done)

    def pair_stop(self):
        self.offer = None
        self._bg(self.client.pair_cancel, lambda code, body: self.render())
        self.render()

    def pair_copy_text(self):
        if self.offer:
            self.copy(self.offer.get("payload", ""), "Pairing text copied.")

    def pair_enter_dialog(self):
        from tkinter import simpledialog
        if not self.settings["network_on"]:
            self.say("Turn on Network first.")
            return
        text = simpledialog.askstring(W.PAIR_ENTER, W.PAIR_ENTER_HOWTO, parent=self.root)
        if text is None:
            return
        if parse_pair_input(text) is None:
            self.say("Type the 6-digit code, or paste the pairing text.")
            return
        self._pair_busy = True
        self.say(W.PAIR_LOOKING)

        def done(code, body):
            self._pair_busy = False
            if code == 200:
                self.say(W.PAIR_DONE)
            elif code == 403:
                self.say(W.PAIR_WRONG)
            elif code == 410:
                self.say(W.PAIR_LOCKED)
            else:
                self.say(body.get("message") or f"The node answered {code}")
            self._bg(self.client.pair_list, self._got_pairing)
        self._bg(lambda: self.client.pair_accept(text), done)

    def pair_remove(self, peer: dict):
        from tkinter import messagebox
        if not messagebox.askyesno(W.PAIR_REMOVE, W.pair_remove_confirm(peer.get("name") or "this device"),
                                   parent=self.root):
            return

        def done(code, body):
            if code != 200:
                self.say(body.get("message") or f"The node answered {code}")
            self._bg(self.client.pair_list, self._got_pairing)
        self._bg(lambda: self.client.pair_remove(peer["pubkey"]), done)

    def _got_pairing(self, code, body):
        if code == 200:
            self.pairing = body
        self.render()

    # ---- refresh (network calls off the UI thread)
    def tick(self):
        if not self._busy and self.settings["network_on"]:
            self._busy = True
            threading.Thread(target=self._poll, daemon=True).start()
        elif not self.settings["network_on"]:
            self.render()
        self.root.after(REFRESH_MS, self.tick)

    def _poll(self):
        try:
            health, err = self.client.health()
            if health is not None:
                want = (bool(self.settings["relay_on"]), int(self.settings["relay_mb_day"]))
                if self.relay_sent != want:
                    try:
                        code, _ = self.client.relay_config(*want)
                        if code == 200:
                            self.relay_sent = want
                    except OSError:
                        pass
                if time.time() - self.last_peer_post > PEER_REPOST_S:
                    for key in list(self.settings["peers"]):
                        host, port = parse_peer(key) or (None, None)
                        if not host:
                            continue
                        try:
                            code, body = self.client.add_peer(host, port)
                            self.peer_state[key] = "ok" if code == 200 else (body.get("message") or f"answered {code}")
                        except OSError:
                            self.peer_state[key] = "not handed in yet"
                    self.last_peer_post = time.time()
                try:
                    code, body = self.client.pair_list()
                    self.pairing = body if code == 200 else None
                    if self.offer is not None:
                        code, body = self.client.pair_offer_status()
                        if code == 200 and body.get("active"):
                            self.offer = body
                        elif code == 200:
                            ended = body.get("ended")
                            self.offer = None
                            msg = W.PAIR_DONE if ended == "paired" else (W.PAIR_LOCKED if ended == "locked"
                                                                         else W.PAIR_ENDED)
                            self.root.after(0, lambda m=msg: self.say(m))
                except (OSError, ValueError):
                    pass
            elif self.manage_daemon and not self.daemon.attached:
                err = err or self.daemon.exit_problem()
            self.health, self.health_err = health, err
        finally:
            self._busy = False
            try:
                self.root.after(0, self.render)
            except RuntimeError:
                pass

    def render(self):
        on = bool(self.settings["network_on"])
        h = self.health if on else None
        line = W.status_line(h, on, self.health_err if on else "")
        self.status.configure(text=line)
        self.dot.configure(fg=W.status_color(line))
        detail = W.status_detail(line, h)
        if on and self.daemon.attached:
            detail += "\nThis node was started outside Omni Node."
        self.detail.configure(text=detail)
        if h and h.get("node_id"):
            self.node_id = h["node_id"]
        self.node.configure(text=W.node_line(self.node_id))
        wan = h.get("wan") if h else None
        self.wan_addr = (wan or {}).get("public_addr") or ""
        text, color = W.wan_line(wan, on)
        self.wan.configure(text=text, fg=color)
        text, color = W.relay_line(h.get("relay") if h else None, on, bool(self.settings["relay_on"]),
                                   int(self.settings["relay_mb_day"]))
        self.relay.configure(text=text, fg=color)
        for child in self.peers_box.winfo_children():
            child.destroy()
        for key in self.settings["peers"]:
            st = self.peer_state.get(key, "waiting")
            lab = self.tk.Label(self.peers_box, text=f"{key}  ·  {st}", bg=W.BG,
                                fg=W.GREEN if st == "ok" else W.AMBER, font=self.font, anchor="w", cursor="hand2")
            lab.pack(fill="x")
            lab.bind("<Button-1>", lambda _e, k=key: self.remove_peer(k))
        self.render_pairing(on, h)
        if self.tray is not None:
            self.tray.update(line)

    def render_pairing(self, on: bool, h):
        quiet = (h or {}).get("quiet") or ((self.pairing or {}).get("quiet") if on else None)
        text, color = W.quiet_line(quiet if on else None)
        if not on:
            text, color = "Quiet mode: off (turn on Network first)", W.TEXT_3
        self.quiet.configure(text=text, fg=color)
        offer = self.offer if on else None
        if offer:
            self.offer_code.configure(text=W.pair_code_display(offer.get("code", "")))
            self.offer_expiry.configure(text=W.pair_expires(offer.get("expires_in", 0), offer.get("attempts_left", 0)))
            payload = offer.get("payload", "")
            if self._offer_drawn != payload:
                self._offer_drawn = payload
                self.offer_text.configure(state="normal")
                self.offer_text.delete(0, "end")
                self.offer_text.insert(0, payload)
                self.offer_text.configure(state="readonly")
                self._draw_qr(payload)
            if not self.offer_box.winfo_manager():
                self.offer_box.pack(fill="x", pady=(8, 0), before=self.paired_box)
        else:
            self._offer_drawn = None
            self.offer_qr.pack_forget()
            self.offer_box.pack_forget()
        for child in self.paired_box.winfo_children():
            child.destroy()
        peers = (self.pairing or {}).get("peers") or []
        if not peers:
            self.tk.Label(self.paired_box, text=W.PAIR_NONE, bg=W.BG, fg=W.TEXT_3, font=self.font,
                          anchor="w").pack(fill="x")
        for p in peers:
            row = self.tk.Frame(self.paired_box, bg=W.BG)
            row.pack(fill="x", pady=1)
            self.tk.Label(row, text=W.paired_line(p), bg=W.BG, fg=W.GREEN if p.get("online") else W.TEXT_2,
                          font=self.font, anchor="w").pack(side="left", fill="x", expand=True)
            self.tk.Button(row, text=W.PAIR_REMOVE, command=lambda q=p: self.pair_remove(q), relief="flat",
                           bg="#2A2A2A", fg=W.TEXT, bd=0, padx=8, pady=2, cursor="hand2").pack(side="right")

    def _draw_qr(self, payload: str):
        m = qr_matrix(payload) if payload else None
        c = self.offer_qr
        c.delete("all")
        if not m:
            c.pack_forget()
            return
        cell = max(2, min(5, 220 // len(m)))
        size = cell * len(m)
        c.configure(width=size, height=size)
        for y, row in enumerate(m):
            for x, dark in enumerate(row):
                if dark:
                    c.create_rectangle(x * cell, y * cell, (x + 1) * cell, (y + 1) * cell, fill="#000000", width=0)
        c.pack(anchor="w", pady=(0, 6), before=self.offer_text)

    def quit(self):
        if self.tray is not None:
            self.tray.stop()
        if self.manage_daemon:
            self.daemon.stop()
        self.root.destroy()


# --------------------------------------------------------------------------- tray (optional)

class Tray:
    """pystray icon: status colour, tooltip with the status line, Open / Pause / Quit."""

    def __init__(self, win: NodeWindow):
        import pystray
        from PIL import Image, ImageDraw
        self.pystray, self.Image, self.ImageDraw = pystray, Image, ImageDraw
        self.win = win
        self.color = None
        menu = pystray.Menu(
            pystray.MenuItem("Open Omni Node", lambda: win.root.after(0, self.show), default=True),
            pystray.MenuItem(lambda _i: "Pause network" if win.settings["network_on"] else "Resume network",
                             lambda: win.root.after(0, win.toggle_network)),
            pystray.MenuItem("Quit", lambda: win.root.after(0, win.quit)))
        self.icon = pystray.Icon(APP_NAME, self._image(W.GREY), W.tray_tooltip(W.OFF), menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def _image(self, color):
        img = self.Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = self.ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=W.BG, outline=color, width=6)
        d.ellipse((22, 22, 42, 42), fill=color)
        return img

    def show(self):
        self.win.root.deiconify()
        self.win.root.lift()

    def update(self, line):
        color = W.status_color(line)
        try:
            if color != self.color:
                self.icon.icon = self._image(color)
                self.color = color
            self.icon.title = W.tray_tooltip(line)
            self.icon.update_menu()
        except Exception:
            pass

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    import tkinter as tk
    root = tk.Tk()
    win = NodeWindow(root)
    try:
        win.tray = Tray(win)
        # Closing the window keeps the node running in the tray, like closing the Android app.
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        if "--background" in argv:
            root.withdraw()
    except Exception:     # no pystray/pillow, or no tray on this desktop: closing the window quits
        win.tray = None
        root.protocol("WM_DELETE_WINDOW", win.quit)
        if "--background" in argv:
            root.iconify()
    root.mainloop()


if __name__ == "__main__":
    main()
