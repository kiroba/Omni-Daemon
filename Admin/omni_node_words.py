"""The words and colours Omni Node shows, shared by every platform.

Android (kiroba/Omni-Node-Daemon, NodeStatus.kt + MainActivity.kt) and the desktop Omni Node
(omni_node_desktop.py: Windows, macOS, Linux) must say the same thing in the same state (issue #81:
one status line, the same words on every surface). This module is the desktop copy of those strings;
tests/test_node_desktop.py pins them to the Android ones so the two can't drift silently.

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


def node_line(node_id: str) -> str:
    return f"Your node {node_tag(node_id)}  ·  click to copy full key" if node_id else ""


def wan_line(wan: dict | None, network_on: bool) -> tuple[str, str]:
    """(text, colour) for the Internet section, from node-health "wan" (daemon v8.21.0+)."""
    if not network_on:
        return "Off", TEXT_3
    if not wan:
        return "Checking...", TEXT_2
    pub = wan.get("public_addr") or ""
    if wan.get("reachable") and pub:
        return f"Reachable at {pub}\nClick to copy, then a friend adds it under Add peer.", GREEN
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


def tray_tooltip(line: str) -> str:
    """Windows caps tray tooltips at 127 characters."""
    return f"Omni Node · {line}"[:127]
