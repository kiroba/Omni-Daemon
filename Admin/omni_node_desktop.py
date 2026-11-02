"""Omni Node for Windows, macOS and Linux: a thin Tk shell around omni_node_core (daemon v8.27.0).

    python Admin/omni_node_desktop.py               opens the window (and a tray icon if pystray is installed)
    python Admin/omni_node_desktop.py --background  starts hidden in the tray (what "Start when I sign in" runs)

Everything the screen says and decides comes from omni_node_core.NodeCore (the same core Omni Node on
Android runs through Chaquopy): this file only renders core.ui_state() and calls core.do(). What stays
here is the desktop environment: starting/stopping the daemon process, the tray icon, start at sign-in,
the clipboard and the pairing QR drawing. See Admin/docs/NODE_SHARED_CORE.md.

It starts the daemon (omni_live_daemon_server.py) as a child with OMNI_LOCAL_TOKEN=1, exactly like the
Android app, so other apps need the token to use :5000/:9100. If a daemon is already running it attaches
to it instead (then the Network switch can't stop it; the card says so).

Only the standard library is required (tkinter). Optional: `pip install pystray pillow` for a tray icon
with a status colour and Open / Pause / Quit (issue #80); `pip install qrcode` (pure Python) to also draw
the pairing QR code. Settings: ~/.omni/node_settings.json (shared with the core; the old
~/.omni/node_desktop.json is carried over on first start).
"""
import os
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import omni_node_core as C  # noqa: E402
import omni_node_words as W  # noqa: E402  (colours + the few words the process code needs)

# The core owns these; kept here so existing callers and tests keep working.
parse_peer = C.parse_peer
parse_pair_input = C.parse_pair_input
NodeClient = C.NodeClient
Settings = C.Settings
omni_dir = C.omni_dir
DESKTOP_CAPS_MB = C.DESKTOP_CAPS_MB
PEER_REPOST_S = C.PEER_REPOST_S
REFRESH_MS = 3000
APP_NAME = "OmniNode"
BUTTON_BG, BUTTON_BG_ACTIVE = "#2A2A2A", "#3A3A3A"


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


# --------------------------------------------------------------------------- the daemon process (desktop env)

class DaemonProcess:
    def __init__(self, client: NodeClient):
        self.client = client
        self.proc = None
        self.attached = False     # a daemon we didn't start was already answering
        self.error = ""

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, upnp: bool = True):
        self.error = ""
        if self.running():
            return
        health, _ = self.client.health()
        if health is not None:
            self.attached = True
            return
        self.attached = False
        env = dict(os.environ, OMNI_LOCAL_TOKEN="1", PYTHONUTF8="1")
        env.setdefault("OMNI_UPNP", "1" if upnp else "0")
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
            self.error = W.node_cant_start(e)
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
                return W.NODE_PORTS_BUSY
            return W.node_exited(self.proc.returncode)
        return self.error


# --------------------------------------------------------------------------- start when I sign in (desktop env)

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
                p.write_text(f"[Desktop Entry]\nType=Application\nName={W.TITLE}\n"
                             f"Exec={' '.join(cmd)}\nX-GNOME-Autostart-enabled=true\n")
            elif p.exists():
                p.unlink()
        return ""
    except OSError as e:
        return str(e)


# --------------------------------------------------------------------------- the window

class NodeWindow:
    """Renders core.ui_state(); every click is core.do(action). Network calls run off the Tk thread."""

    def __init__(self, root, settings: Settings = None, client: NodeClient = None, manage_daemon=True):
        import tkinter as tk
        self.tk = tk
        self.root = root
        self.client = client or NodeClient()
        self.daemon = DaemonProcess(self.client)
        self.manage_daemon = manage_daemon
        env = self._env()
        self.core = C.NodeCore(env=env, settings=settings or Settings(defaults=C.profile(env)["defaults"]),
                               client=self.client)
        self.settings = self.core.settings
        self.tray = None
        self.state = {}
        self._busy = False
        self._offer_drawn = None   # the payload the QR canvas was drawn for
        w = self.core.ui_state(take_toasts=False)["words"]
        self.w = w

        root.title(w["TITLE"])
        root.configure(bg=W.BG)
        root.geometry("460x820")
        root.minsize(400, 560)
        f = ("Segoe UI", 10) if os.name == "nt" else ("Helvetica", 10)
        self.font, self.small = f, (f[0], 9)
        self._wraps = []          # (label, inset): wraplength follows the window width
        outer = self._scroll_area(root)

        tk.Label(outer, text=w["TITLE"], bg=W.BG, fg=W.TEXT, font=(f[0], 20, "bold"), anchor="w").pack(fill="x")
        self._hint(outer, w["SUBTITLE"], fg=W.TEXT_2, font=f, pady=(2, 10))

        row = tk.Frame(outer, bg=W.BG)
        row.pack(fill="x")
        tk.Label(row, text=w["NETWORK"], bg=W.BG, fg=W.TEXT, font=(f[0], 14, "bold")).pack(side="left")
        self.net_btn = self._switch(row, lambda: self.act("network", {"on": not self.core.network_on}), big=True)

        card = tk.Frame(outer, bg=W.CARD, highlightbackground=W.CARD_EDGE, highlightthickness=1, padx=14, pady=12)
        card.pack(fill="x", pady=(8, 4))
        top = tk.Frame(card, bg=W.CARD)
        top.pack(fill="x")
        self.dot = tk.Label(top, text="\u25CF", bg=W.CARD, fg=W.GREY, font=(f[0], 14))
        self.dot.pack(side="left")
        self.status = tk.Label(top, text="", bg=W.CARD, fg=W.TEXT, font=(f[0], 12, "bold"), anchor="w")
        self.status.pack(side="left", padx=(6, 0))
        self.detail = tk.Label(card, text="", bg=W.CARD, fg=W.TEXT_2, font=f, anchor="w", justify="left",
                               wraplength=400)
        self.detail.pack(fill="x", pady=(4, 0))
        self._wraps.append((self.detail, 32))
        self.node = tk.Label(card, text="", bg=W.CARD, fg=W.TEXT_3, font=self.small, anchor="w", cursor="hand2")
        self.node.pack(fill="x")
        self.node.bind("<Button-1>", lambda _e: self.act("copy_node_key"))
        self.version = tk.Label(card, text="", bg=W.CARD, fg=W.TEXT_3, font=self.small, anchor="w")
        self.version.pack(fill="x")

        self.connect_btn = self._button(outer, w["CONNECT_APPS"], self.connect_apps)
        self.connect_hint = self._hint(outer, "")

        self._section(outer, w["INTERNET_TITLE"])
        self.wan = self._line(outer)
        self.wan.configure(cursor="hand2")
        self.wan.bind("<Button-1>", lambda _e: self.act("copy_address"))
        irow = tk.Frame(outer, bg=W.BG)
        irow.pack(fill="x", pady=(4, 0))
        tk.Label(irow, text=w["INTERNET_SWITCH"], bg=W.BG, fg=W.TEXT, font=(f[0], 11)).pack(side="left")
        self.internet_btn = self._switch(irow, lambda: self.act("internet", {"on": not self.state["internet"]["on"]}))
        self._hint(outer, w["INTERNET_HINT"])

        self._section(outer, w["RELAY_SECTION"])
        rrow = tk.Frame(outer, bg=W.BG)
        rrow.pack(fill="x")
        tk.Label(rrow, text=w["RELAY_TITLE"], bg=W.BG, fg=W.TEXT, font=(f[0], 11)).pack(side="left")
        self.relay_btn = self._switch(rrow, lambda: self.act("relay", {"on": not self.state["relay"]["on"]}))
        self.relay_hint = self._hint(outer, "")
        self.relay = self._line(outer)
        self.relay.configure(cursor="hand2")
        self.relay.bind("<Button-1>", lambda _e: self.pick_cap())

        self._section(outer, w["PEERS_TITLE"])
        self.peers_hint = self._hint(outer, "")
        self._button(outer, w["ADD_PEER"], self.add_peer_dialog)
        self.peers_box = tk.Frame(outer, bg=W.BG)
        self.peers_box.pack(fill="x")

        self._section(outer, w["PAIR_TITLE"])
        self._hint(outer, w["PAIR_HINT"])
        self.quiet = self._line(outer)
        prow = tk.Frame(outer, bg=W.BG)
        prow.pack(fill="x")
        self.pair_show_btn = self._button(prow, w["PAIR_SHOW"], lambda: self.act("pair_offer"))
        self.pair_show_btn.pack_configure(side="left", padx=(0, 8))
        self.pair_enter_btn = self._button(prow, w["PAIR_ENTER"], self.pair_enter_dialog)
        self.pair_enter_btn.pack_configure(side="left")
        self.offer_box = tk.Frame(outer, bg=W.CARD, highlightbackground=W.CARD_EDGE, highlightthickness=1,
                                  padx=14, pady=10)
        self.offer_code = tk.Label(self.offer_box, text="", bg=W.CARD, fg=W.TEXT, font=("Consolas", 26, "bold"),
                                   anchor="w")
        self.offer_code.pack(fill="x")
        self.offer_expiry = tk.Label(self.offer_box, text="", bg=W.CARD, fg=W.TEXT_2, font=self.small, anchor="w")
        self.offer_expiry.pack(fill="x")
        self.offer_howto = tk.Label(self.offer_box, text=w["PAIR_CODE_HOWTO"], bg=W.CARD, fg=W.TEXT_2,
                                    font=self.small, wraplength=380, justify="left", anchor="w")
        self.offer_howto.pack(fill="x", pady=(4, 6))
        self._wraps.append((self.offer_howto, 32))
        self.offer_qr = tk.Canvas(self.offer_box, width=1, height=1, bg="#FFFFFF", highlightthickness=0)
        self.offer_text = tk.Entry(self.offer_box, font=("Consolas", 8), relief="flat", bg=W.BG, fg=W.TEXT_2,
                                   readonlybackground=W.BG)
        self.offer_text.pack(fill="x", pady=(0, 2))
        tk.Label(self.offer_box, text=w["PAIR_TEXT_WARNING"], bg=W.CARD, fg=W.TEXT_3, font=self.small, anchor="w",
                 wraplength=380, justify="left").pack(fill="x")
        obtn = tk.Frame(self.offer_box, bg=W.CARD)
        obtn.pack(fill="x", pady=(6, 0))
        for label, action in ((w["PAIR_COPY"], "copy_pair_text"), (w["PAIR_STOP"], "pair_cancel")):
            tk.Button(obtn, text=label, command=lambda a=action: self.act(a), relief="flat", bg=BUTTON_BG,
                      fg=W.TEXT, bd=0, padx=10, pady=4, cursor="hand2").pack(side="left", padx=(0, 8))
        self.paired_box = tk.Frame(outer, bg=W.BG)
        self.paired_box.pack(fill="x", pady=(6, 0))

        self._section(outer, w["UPDATE_TITLE"])
        urow = tk.Frame(outer, bg=W.BG)
        urow.pack(fill="x")
        self._button(urow, w["UPDATE_CHECK"], lambda: self.act("check_updates")).pack_configure(side="left")
        self.update_get = self._button(urow, w["UPDATE_GET"], self.open_update)
        self.update_get.pack_configure(side="left", padx=(8, 0))
        self.update_line = self._line(outer)

        self._section(outer, w["BACKGROUND_TITLE"])
        self.login_var = tk.BooleanVar(value=bool(self.settings["start_at_login"]))
        tk.Checkbutton(outer, text=w["START_AT_LOGIN"], variable=self.login_var, command=self.toggle_login,
                       bg=W.BG, fg=W.TEXT_2, selectcolor=W.CARD, activebackground=W.BG, activeforeground=W.TEXT,
                       font=f, anchor="w").pack(fill="x")
        self.toast = tk.Label(outer, text="", bg=W.BG, fg=W.TEAL, font=self.small, anchor="w")
        self.toast.pack(fill="x", pady=(8, 0))

        if self.core.network_on and self.manage_daemon:
            self.daemon.start(bool(self.settings["internet_on"]))
        self.render()
        self.root.after(200, self.tick)

    # ---- the core's state, under the names older callers use
    health = property(lambda self: self.core.health, lambda self, v: setattr(self.core, "health", v))
    offer = property(lambda self: self.core.offer, lambda self, v: setattr(self.core, "offer", v))
    pairing = property(lambda self: self.core.pairing, lambda self, v: setattr(self.core, "pairing", v))

    def _env(self) -> dict:
        env = {"platform": C.detect_platform(), "os": sys.platform, "autostart_available": True,
               "attached": bool(self.daemon.attached)}
        if self.manage_daemon and not self.daemon.attached:
            problem = self.daemon.exit_problem()
            if problem:
                env["node_error"] = problem
        return env

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

    def _hint(self, parent, text, fg=None, font=None, pady=0):
        lab = self.tk.Label(parent, text=text, bg=W.BG, fg=fg or W.TEXT_3, font=font or self.small, wraplength=410,
                            justify="left", anchor="w")
        lab.pack(fill="x", pady=pady)
        self._wraps.append((lab, 0))
        return lab

    def _line(self, parent):
        lab = self.tk.Label(parent, text="", bg=W.BG, fg=W.TEXT_2, font=self.font, wraplength=410,
                            justify="left", anchor="w")
        lab.pack(fill="x", pady=(2, 0))
        self._wraps.append((lab, 0))
        return lab

    def _button(self, parent, text, cmd):
        b = self.tk.Button(parent, text=text, command=cmd, relief="flat", bg=BUTTON_BG, fg=W.TEXT,
                           activebackground=BUTTON_BG_ACTIVE, activeforeground=W.TEXT, font=self.font,
                           cursor="hand2", bd=0, padx=10, pady=5)
        b.pack(anchor="w", pady=(8, 2))
        return b

    def _switch(self, parent, cmd, big=False):
        b = self.tk.Button(parent, width=6, relief="flat", font=(self.font[0], 10 if big else 9, "bold"),
                           command=cmd, cursor="hand2", bd=0, padx=8 if big else 6, pady=3 if big else 2)
        b.pack(side="right")
        return b

    def _paint_switch(self, btn, on):
        btn.configure(text=self.w["ON_SHORT"] if on else self.w["OFF_SHORT"], bg=W.GREEN if on else BUTTON_BG_ACTIVE,
                      fg=W.TEXT, activebackground=W.GREEN if on else BUTTON_BG_ACTIVE)

    def say(self, text):
        if not text:
            return
        self.toast.configure(text=text)
        self.root.after(4000, lambda: self.toast.configure(text="") if self.toast.cget("text") == text else None)

    def copy(self, text, msg):
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.say(msg)

    # ---- actions: the core decides, this applies the desktop effects
    def act(self, action, args=None, background=None):
        """core.do(action). Actions that talk to the node run off the Tk thread."""
        slow = action in ("pair_offer", "pair_cancel", "pair_accept", "pair_remove", "check_updates", "internet")
        if background is None:
            background = slow
        if not background:
            self._apply(self.core.do(action, args))
            return

        def run():
            out = self.core.do(action, args)
            try:
                self.root.after(0, lambda: self._apply(out))
            except RuntimeError:
                pass
        if action == "check_updates":
            self.core.update_busy = True        # the line says "Checking..." right away
            self.render()
            self.core.update_busy = False
        threading.Thread(target=run, daemon=True).start()

    def _apply(self, out: dict):
        for effect in out.get("effects", []):
            if effect == "start_node" and self.manage_daemon:
                self.daemon.start(bool(self.settings["internet_on"]))
            elif effect == "stop_node" and self.manage_daemon:
                self.daemon.stop()
        self.core.set_env(self._env())
        if out.get("copy"):
            self.copy(out["copy"], out.get("toast", ""))
        else:
            self.say(out.get("toast", ""))
        self.render()

    def toggle_network(self):
        self.act("network", {"on": not self.core.network_on}, background=False)

    def toggle_relay(self):
        self.act("relay", {"on": not self.settings["relay_on"]}, background=False)

    def pick_cap(self):
        tk = self.tk
        r = self.state["relay"]
        win = tk.Toplevel(self.root, bg=W.BG, padx=16, pady=12)
        win.title(r["cap_title"])
        win.transient(self.root)
        var = tk.IntVar(value=int(r["cap_mb"]))
        for c in r["caps"]:
            tk.Radiobutton(win, text=c["label"], variable=var, value=c["mb"], bg=W.BG, fg=W.TEXT, selectcolor=W.CARD,
                           activebackground=W.BG, activeforeground=W.TEXT, font=self.font, anchor="w").pack(fill="x")

        def ok():
            win.destroy()
            self.act("relay_cap", {"mb": int(var.get())}, background=False)
        tk.Button(win, text=self.w["OK"], command=ok, relief="flat", bg=W.GREEN, fg=W.TEXT, bd=0, padx=12,
                  pady=4).pack(anchor="e", pady=(8, 0))

    def connect_apps(self):
        from tkinter import messagebox
        out = self.core.do("token")
        if not out.get("ok"):
            messagebox.showinfo(self.w["CONNECT_APPS"], out.get("toast", ""), parent=self.root)
            return
        if not messagebox.askyesno(self.w["CONNECT_APPS"], out["confirm"], parent=self.root):
            return
        tk = self.tk
        win = tk.Toplevel(self.root, bg=W.BG, padx=16, pady=12)
        win.title(out["title"])
        win.transient(self.root)
        tk.Label(win, text=out["howto"], bg=W.BG, fg=W.TEXT_2, font=self.font, wraplength=380,
                 justify="left").pack(fill="x")
        e = tk.Entry(win, font=("Consolas", 10), width=44, relief="flat", bg=W.CARD, fg=W.TEXT,
                     insertbackground=W.TEXT)
        e.insert(0, out["token"])
        e.configure(state="readonly", readonlybackground=W.CARD)
        e.pack(fill="x", pady=8)

        def copy_close():
            win.destroy()
            self.act("copy_token", background=False)
        tk.Button(win, text=out["copy_label"], command=copy_close, relief="flat", bg=W.GREEN, fg=W.TEXT, bd=0,
                  padx=12, pady=4).pack(anchor="e")

    def add_peer_dialog(self):
        from tkinter import simpledialog
        text = simpledialog.askstring(self.w["ADD_PEER"], self.w["ADD_PEER_HOWTO"], parent=self.root)
        if text is not None:
            self.act("add_peer", {"text": text}, background=False)

    def remove_peer(self, key):
        from tkinter import messagebox
        if messagebox.askyesno(W.peer_remove_title(key), self.w["PEER_REMOVE_BODY"], parent=self.root):
            self.act("remove_peer", {"key": key}, background=False)

    def toggle_login(self):
        on = bool(self.login_var.get())
        err = set_start_at_login(on)
        if err:
            self.login_var.set(not on)
        self.act("start_at_login", {"on": on, "error": err}, background=False)

    def pair_enter_dialog(self):
        from tkinter import simpledialog
        if not self.core.network_on:
            self.say(self.w["NETWORK_FIRST"])
            return
        text = simpledialog.askstring(self.w["PAIR_ENTER"], self.w["PAIR_ENTER_HOWTO"], parent=self.root)
        if text is None:
            return
        if parse_pair_input(text) is None:
            self.say(self.w["PAIR_BAD_INPUT"])
            return
        self.say(self.w["PAIR_LOOKING"])
        self.act("pair_accept", {"text": text})

    def pair_remove(self, peer: dict):
        from tkinter import messagebox
        if messagebox.askyesno(self.w["PAIR_REMOVE"], peer["confirm"], parent=self.root):
            self.act("pair_remove", {"pubkey": peer["pubkey"]})

    def open_update(self):
        url = (self.state.get("update") or {}).get("url")
        if url:
            import webbrowser
            webbrowser.open(url)

    # ---- refresh (network calls off the UI thread)
    def tick(self):
        if not self._busy and self.core.network_on:
            self._busy = True
            threading.Thread(target=self._poll, daemon=True).start()
        elif not self.core.network_on:
            self.render()
        self.root.after(REFRESH_MS, self.tick)

    def _poll(self):
        try:
            self.core.set_env(self._env())
            self.core.sync()
        finally:
            self._busy = False
            try:
                self.root.after(0, self.render)
            except RuntimeError:
                pass

    def render(self):
        st = self.state = self.core.ui_state()
        s = st["status"]
        self._paint_switch(self.net_btn, st["network"]["on"])
        self._paint_switch(self.relay_btn, st["relay"]["on"])
        self._paint_switch(self.internet_btn, st["internet"]["on"])
        self.status.configure(text=s["line"])
        self.dot.configure(fg=s["color"])
        self.detail.configure(text=s["detail"])
        self.node.configure(text=s["node_line"])
        self.version.configure(text=st["version_line"])
        self.connect_hint.configure(text=st["connect"]["hint"])
        self.wan.configure(text=st["internet"]["line"], fg=st["internet"]["color"])
        self.relay_hint.configure(text=st["relay"]["hint"])
        self.relay.configure(text=st["relay"]["line"], fg=st["relay"]["color"])
        self.peers_hint.configure(text=st["peers"]["hint"])
        for child in self.peers_box.winfo_children():
            child.destroy()
        for p in st["peers"]["items"]:
            lab = self.tk.Label(self.peers_box, text=p["label"], bg=W.BG, fg=p["color"], font=self.font, anchor="w",
                                cursor="hand2")
            lab.pack(fill="x")
            lab.bind("<Button-1>", lambda _e, k=p["key"]: self.remove_peer(k))
        self.render_pairing(st["pairing"])
        u = st["update"]
        self.update_line.configure(text=u["line"], fg=u["color"])
        if u["url"]:
            if not self.update_get.winfo_manager():
                self.update_get.pack(side="left", padx=(8, 0), pady=(8, 2))
        else:
            self.update_get.pack_forget()
        for t in st["toasts"]:
            self.say(t)
        if self.tray is not None:
            self.tray.update(st)

    def render_pairing(self, pr: dict):
        self.quiet.configure(text=pr["quiet_line"], fg=pr["quiet_color"])
        offer = pr["offer"]
        if offer:
            self.offer_code.configure(text=offer["code"])
            self.offer_expiry.configure(text=offer["expiry"])
            payload = offer["payload"]
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
        if not pr["paired"]:
            self.tk.Label(self.paired_box, text=pr["none"], bg=W.BG, fg=W.TEXT_3, font=self.font,
                          anchor="w").pack(fill="x")
        for p in pr["paired"]:
            row = self.tk.Frame(self.paired_box, bg=W.BG)
            row.pack(fill="x", pady=1)
            self.tk.Label(row, text=p["line"], bg=W.BG, fg=p["color"], font=self.font,
                          anchor="w").pack(side="left", fill="x", expand=True)
            self.tk.Button(row, text=self.w["PAIR_REMOVE"], command=lambda q=p: self.pair_remove(q), relief="flat",
                           bg=BUTTON_BG, fg=W.TEXT, bd=0, padx=8, pady=2, cursor="hand2").pack(side="right")

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


# --------------------------------------------------------------------------- tray (optional, desktop env)

class Tray:
    """pystray icon: status colour, tooltip with the status line, Open / Pause / Quit."""

    def __init__(self, win: NodeWindow):
        import pystray
        from PIL import Image, ImageDraw
        self.pystray, self.Image, self.ImageDraw = pystray, Image, ImageDraw
        self.win = win
        self.color = None
        w = win.w
        menu = pystray.Menu(
            pystray.MenuItem(w["TRAY_OPEN"], lambda: win.root.after(0, self.show), default=True),
            pystray.MenuItem(lambda _i: w["TRAY_PAUSE"] if win.core.network_on else w["TRAY_RESUME"],
                             lambda: win.root.after(0, win.toggle_network)),
            pystray.MenuItem(w["TRAY_QUIT"], lambda: win.root.after(0, win.quit)))
        st = win.state.get("status") or {}
        self.icon = pystray.Icon(APP_NAME, self._image(st.get("color", W.GREY)), st.get("tooltip", w["TITLE"]), menu)
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

    def update(self, state):
        s = state["status"]
        try:
            if s["color"] != self.color:
                self.icon.icon = self._image(s["color"])
                self.color = s["color"]
            self.icon.title = s["tooltip"]
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
