"""The words and colours Omni Node shows, shared by every platform.

Since daemon v8.27.0 this is the ONLY copy. omni_node_core.py builds the whole Node screen from it
(ui_state()), and every shell renders that: Android (kiroba/Omni-Node-Daemon, Kotlin, through
Chaquopy), the desktop window (omni_node_desktop.py: Windows, macOS, Linux) and, later, iOS. The
shells hold no user-facing words of their own; a CI parity check fails if one does (issue #81: one
status line, the same words on every surface). See Admin/docs/NODE_SHARED_CORE.md.

Pure functions only (no Tk, no network), so they're unit-tested on every CI OS.
"""

# ---- status line (NodeStatus.kt) ----
OFF = "Network status: Off"
LOOKING = "Looking for nearby people..."
STARTING = "Starting..."


def connected(n: int) -> str:
    return f"Connected to {n} nearby"


def problem(reason: str) -> str:
    return f"Problem: {reason}"


# ---- colours (MainActivity.kt) ----
BG = "#121212"
CARD = "#1E1E1E"
CARD_EDGE = "#333333"
TEXT = "#FFFFFF"
TEXT_2 = "#B0B0B0"
TEXT_3 = "#808080"
GREEN = "#4CAF50"
TEAL = "#03DAC6"
AMBER = "#FFB74D"
RED = "#CF6679"
GREY = "#808080"

# ---- fixed copy ----
TITLE = "Omni Node"
SUBTITLE = "Keeps you connected to people nearby, even when your other Omni apps are closed."
CONNECT_APPS_HINT_DESKTOP = ("KickBack, Mesh Monitor and other Omni apps need this computer's node token. "
                             "It's only shown after you confirm.")
TOKEN_HOWTO = ("In KickBack or Mesh Monitor open Hub settings, choose Remote hub, type 127.0.0.1 as the "
               "address and paste this token. Don't share it: any app with it can use your node.")
PEERS_HINT = ("Add a friend's address (from their Internet line), your PC or a server. Your node keeps "
              "syncing with it over the internet. Tap one to remove it.")
RELAY_TITLE = "Help calls connect"
RELAY_HINT_DESKTOP = ("When two people on your mesh can't reach each other directly, their call can pass "
                      "through this computer. On by default on desktops, with a daily limit.")
CAPS_MB = (250, 1024, 5120)


def cap_label(mb: int) -> str:
    """Same as NodeRelay.capLabel on Android (plus 0 = unlimited, which only desktops offer)."""
    return "unlimited" if mb == 0 else (f"{mb // 1024} GB" if mb >= 1024 else f"{mb} MB")


def status_line(health: dict | None, network_on: bool, error: str = "") -> str:
    """The one status line, from :9100 node-health (or None while it isn't answering)."""
    if not network_on:
        return OFF
    if error:
        return problem(error)
    if not health:
        return STARTING
    peers = int(health.get("active_peers") or 0)
    return connected(peers) if peers > 0 else LOOKING


def status_color(line: str) -> str:
    if line == OFF:
        return GREY
    if line.startswith("Problem"):
        return RED
    if line.startswith("Connected"):
        return GREEN
    if line == LOOKING:
        return TEAL
    return AMBER  # starting


def status_detail(line: str, health: dict | None) -> str:
    if line == OFF:
        return "Turn on Network to connect"
    if line.startswith("Problem"):
        return "Turn Network off and on. If it stays, send a report."
    if health and health.get("version"):
        n = int(health.get("active_peers") or 0)
        return f"Running · {n} peer{'' if n == 1 else 's'} · daemon v{health['version']}"
    return "Starting the node..."


def node_tag(node_id: str) -> str:
    return "·" + node_id[:4] if len(node_id or "") >= 4 else ""


def node_line(node_id: str, verb: str = "click") -> str:
    return f"Your node {node_tag(node_id)}  ·  {verb} to copy full key" if node_id else ""


def wan_line(wan: dict | None, network_on: bool, copy_verb: str = "Click") -> tuple[str, str]:
    """(text, colour) for the Internet section, from node-health "wan" (daemon v8.21.0+).
    copy_verb: "Click" on desktops, "Long-press" on phones."""
    if not network_on:
        return "Off", TEXT_3
    if not wan:
        return "Checking...", TEXT_2
    pub = wan.get("public_addr") or ""
    if wan.get("reachable") and pub:
        return f"Reachable at {pub}\n{copy_verb} to copy, then a friend adds it under Add peer.", GREEN
    if pub:
        return (f"Your address is {pub}, but the router blocks incoming connections ({wan.get('nat') or 'NAT'}).\n"
                "You can still add a friend or server that is reachable."), TEXT_2
    return wan.get("detail") or "No internet path found yet.", TEXT_2


def relay_line(relay: dict | None, network_on: bool, want_on: bool, cap_mb: int) -> tuple[str, str]:
    """(text, colour) for Relay calls, from node-health "relay" (daemon v8.22.0+)."""
    cap = cap_label(cap_mb)
    if not network_on:
        return "Off (turn on Network first)", TEXT_3
    if not want_on:
        return f"Off · limit {cap} a day", TEXT_3
    if relay is None:
        return "Starting the relay...", TEXT_2
    if relay.get("error"):
        return problem(str(relay["error"])), AMBER
    if relay.get("enabled"):
        used = float(relay.get("mb_today") or 0)
        calls = int(relay.get("allocations") or 0)
        extra = "" if relay.get("public") else "\nOnly reachable on this network so far (router didn't open the ports)."
        return f"Relaying · {calls} active · {used:.0f} of {cap} today{extra}", GREEN
    return "Starting the relay...", TEXT_2


# ---- Pair a device / quiet mode (daemon v8.24.0; NodePairing.kt on Android) ----
PAIR_TITLE = "Pair a device"
PAIR_HINT = ("Pair your phone, your PC or a friend's device. Paired nodes find each other quietly: "
             "other people on the same Wi-Fi can't see who you are.")
PAIR_SHOW = "Show a code"
PAIR_ENTER = "Enter a code"
PAIR_STOP = "Stop showing the code"
PAIR_CODE_HOWTO = ("On the other device open Omni Node, choose Pair a device, then Enter a code and type this "
                   "code. Keep both on the same Wi-Fi.")
PAIR_ENTER_HOWTO = ("Type the 6-digit code the other device shows, or paste its pairing text. If it can't be "
                    "found, add its address after the code, like 123456 192.168.1.20")
PAIR_LOOKING = "Looking for the other device..."
PAIR_DONE = "Paired. Your nodes now find each other quietly."
PAIR_WRONG = "That code didn't match."
PAIR_LOCKED = "Too many wrong codes. Make a new code on the other device."
PAIR_NONE = "No paired devices yet."
PAIR_COPY = "Copy pairing text"
PAIR_TEXT_WARNING = "Anyone with this text can pair with you while the code shows. Only send it to yourself."
PAIR_REMOVE = "Remove"
PAIR_ENDED = "The code stopped working. Show a new one."


def pair_expires(seconds: int, tries_left: int) -> str:
    s = max(0, int(seconds))
    return f"Expires in {s // 60}:{s % 60:02d} · {tries_left} {'try' if tries_left == 1 else 'tries'} left"


def pair_code_display(code: str) -> str:
    """'123456' -> '123 456' (easier to read out)."""
    return f"{code[:3]} {code[3:]}" if len(code or "") == 6 else (code or "")


def pair_remove_confirm(name: str) -> str:
    return f"Remove {name}? Your nodes stop syncing with each other. To connect again, pair again."


def paired_line(peer: dict) -> str:
    tag = peer.get("tag") or ""
    where = "nearby now" if peer.get("online") else "not nearby"
    return f"{peer.get('name') or 'Omni node'} {tag}  ·  {where}"


def quiet_line(quiet: dict | None) -> tuple[str, str]:
    """(text, colour) for the Quiet mode status line, from node-health "quiet"."""
    if not quiet:
        return "Quiet mode: checking...", TEXT_3
    if quiet.get("mode") == "off":
        return "Quiet mode: off on this node", TEXT_3
    n = int(quiet.get("paired") or 0)
    if quiet.get("active") and n > 0:
        return f"Quiet mode: on · only your {n} paired device{'' if n == 1 else 's'} can see this node", GREEN
    if quiet.get("active"):
        return "Quiet mode: on · no devices paired yet", AMBER
    return "Quiet mode: off until you pair a device", TEXT_3


def tray_tooltip(line: str) -> str:
    """Windows caps tray tooltips at 127 characters."""
    return f"Omni Node · {line}"[:127]


# =================================================================================================
# v8.27.0: everything else the Node screen says, so no shell (Kotlin, Tk, later Swift) holds copy.
# Change a word here and every platform changes with it.
# =================================================================================================

# ---- buttons and dialog bits ----
NETWORK = "Network"
CANCEL = "Cancel"
CLOSE = "Close"
KEEP = "Keep"
ADD = "Add"
PAIR = "Pair"
OK = "OK"
SHOW = "Show"
NOT_NOW = "Not now"
ALLOW = "Allow"
OFF_SHORT = "Off"
ON_SHORT = "On"
NETWORK_FIRST = "Turn on Network first."
NODE_KEY_COPIED = "Node key copied"
ADDRESS_COPIED = "Address copied"
ATTACHED_NOTE = "This node was started outside Omni Node."
ATTACHED_STOP = "This node was started outside Omni Node, so stop it where it runs."
NETWORK_HERE_ONLY = "Turn the network on or off in the Omni Node app on this device."
NODE_PORTS_BUSY = "another program is using the node's ports"


def node_exited(code) -> str:
    return f"the daemon stopped (exit {code})"


def node_cant_start(err) -> str:
    return f"couldn't start the daemon ({err})"


def node_answered(code) -> str:
    return f"The node answered {code}"


def version_line(node_version: str, daemon_version: str = "") -> str:
    """'Omni Node 0.5.0 · daemon v8.27.0' (the same Node version on every platform)."""
    line = f"Omni Node {node_version or 'dev'}"
    return f"{line} · daemon v{daemon_version}" if daemon_version else line


# ---- Connect apps ----
CONNECT_APPS = "Connect apps"
CONNECT_APPS_HINT_PHONE = ("KickBack, Mesh Monitor and other Omni apps need this phone's node token. "
                           "It's behind your screen lock.")
TOKEN_TITLE = "Your node token"
TOKEN_NOT_YET = "Turn on Network first; the token is made when the node starts."
TOKEN_CONFIRM_DESKTOP = ("Show this computer's node token? Anyone who sees it can use your node, so check nobody "
                         "is looking at your screen.")
TOKEN_COPY = "Copy token"
TOKEN_COPIED = "Token copied. Paste it into the app's Hub settings."
UNLOCK_SUBTITLE = "Unlock to see your node token"
NO_LOCK_TITLE = "No screen lock"
NO_LOCK_BODY = "Set a screen lock (PIN, pattern or fingerprint) so only you can see this token. Show it anyway?"

# ---- Nearby radios (phones: Bluetooth + Wi-Fi Aware) ----
RADIOS_TITLE = "Nearby radios"
RADIOS_NEED_PERMISSION = "Bluetooth and Wi-Fi Aware need the Nearby devices permission. Tap to allow."


def radios_line(ble: str, ble_nearby: int, wifi_aware: str) -> str:
    n = int(ble_nearby or 0)
    return f"Bluetooth: {ble or 'off'}" + (f" · {n} nearby" if n > 0 else "") + f"\nWi-Fi Aware: {wifi_aware or 'off'}"


# ---- Internet ----
INTERNET_TITLE = "Internet"
INTERNET_SWITCH = "Open a port on my router"
INTERNET_HINT = ("Asks your router (UPnP) to let friends outside your network reach this node. Turn it off on "
                 "networks you don't trust; you can still add peers that are reachable.")
INTERNET_ROUTER_OFF = "Router port: off. Friends outside your network reach you only if you add each other."

# ---- Relay calls ----
RELAY_SECTION = "Relay calls"
RELAY_HINT_PHONE = ("When two people on your mesh are both on mobile data, their call can pass through this phone. "
                    "It only relays while the phone is charging on Wi-Fi, never on mobile data.")
RELAY_CAP_TITLE = "Daily relay limit"
RELAY_CHECKING = "Checking Wi-Fi and charging..."
RELAY_WAIT_WIFI = "waiting for Wi-Fi (never relays on mobile data)"
RELAY_WAIT_CHARGING = "waiting until the phone is charging"
RELAY_WAIT_SAVER = "waiting: battery saver is on"
RELAY_TOO_OLD = "this node's daemon is too old for relaying (needs v8.22.0)"


def relay_cap_hint(verb: str) -> str:
    return f"{verb} the line below to change the daily limit."


def cap_choice(mb: int) -> str:
    return "No limit" if mb == 0 else f"Up to {cap_label(mb)} a day"


def relay_waiting(reason: str, cap: str) -> str:
    return f"Waiting: {reason.removeprefix('waiting ').removeprefix('waiting: ')} · limit {cap} a day"


def relay_cant_start(err: str) -> str:
    return f"daemon couldn't start the relay: {err}"


# ---- Peers ----
PEERS_TITLE = "Peers"
ADD_PEER = "Add peer"
ADD_PEER_HOWTO = "Address of another node. Port 5005 unless you add one."
ADD_PEER_EXAMPLE = "203.0.113.7 or [2001:db8::1]:5005 or mypc.example.com"
PEER_BAD = "That address doesn't look right."
PEER_REMOVE_BODY = "Your node stops syncing with it within two minutes."
PEER_WAITING = "waiting for the node"
PEER_SYNCING = "syncing"


def peers_hint(verb: str = "Tap") -> str:
    return PEERS_HINT.replace("Tap one", f"{verb} one")


def peer_added(key: str) -> str:
    return f"Added. Your node will sync with {key}."


def peer_remove_title(key: str) -> str:
    return f"Remove {key}?"


def peer_line(key: str, state: str) -> tuple[str, str]:
    """(text, colour) for one added peer. state: 'ok', '' (not handed in yet) or a plain reason."""
    if state == "ok":
        return f"\u25CF  {key}  ·  {PEER_SYNCING}", GREEN
    return f"\u25CB  {key}  ·  {state or PEER_WAITING}", AMBER


# ---- Pair a device (extra bits) ----
PAIR_BAD_INPUT = "Type the 6-digit code, or paste the pairing text."
PAIR_TEXT_COPIED = "Pairing text copied."
PAIR_EXAMPLE = "123 456"
PAIR_NEEDS_UPDATE = "This node is too old to pair (needs daemon v8.24.0 or newer)."
QUIET_NETWORK_OFF = "Quiet mode: off (turn on Network first)"

# ---- Check for updates ----
UPDATE_TITLE = "Updates"
UPDATE_CHECK = "Check for updates"
UPDATE_CHECKING = "Checking for updates..."
UPDATE_GET = "Get it"


def update_available(latest: str) -> str:
    return f"Omni Node {latest} is available"


def update_newest(current: str) -> str:
    return f"You have the newest Omni Node ({current})"


def update_failed(reason: str) -> str:
    return f"Couldn't check for updates: {reason}"


# ---- Background / desktop tray ----
BACKGROUND_TITLE = "Background"
START_AT_LOGIN = "Start when I sign in"
TRAY_OPEN = "Open Omni Node"
TRAY_PAUSE = "Pause network"
TRAY_RESUME = "Resume network"
TRAY_QUIT = "Quit"


def autostart_failed(err: str) -> str:
    return f"Couldn't change that: {err}"


# ---- Android: notification and battery (the words only; the code that shows them is Android's) ----
NOTIF_CHANNEL = "Network status"
NOTIF_CHANNEL_DESC = "Shows whether your Omni node is on and who it's connected to."
NOTIF_TURN_OFF = "Turn off"
BATTERY_TITLE = "Stay connected in the background?"
BATTERY_BODY = ("Android may pause Omni Node to save battery, which drops your connections. Allow it to keep "
                "running? You can change this anytime in Settings.")


def all_words() -> dict:
    """Every fixed string above, by name (what ui_state() hands a shell for its buttons and dialogs)."""
    return {k: v for k, v in globals().items() if k.isupper() and isinstance(v, str)}
