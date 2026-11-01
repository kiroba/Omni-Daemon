# Omni-Daemon

The networked protocol layer of The Omniverse. One daemon runs on each device. It keeps the signed event log, syncs with peers (negentropy catch-up), reaches other nodes over the Internet, relays calls, offers remote access over Tailscale, runs the test-credits ledger, and handles OmniCast signaling. Apps only ever talk to their own device's daemon.

- Daemon and engines: `Admin/` (docs in `Admin/docs/`)
- Tests: `python3 -m unittest tests.<name>`, run from the repo root
- **Omni Node for desktop** (Windows, macOS, Linux): `python Admin/omni_node_desktop.py`. It has the same screen and words as Omni Node on Android, with an optional tray icon. See `Admin/docs/NODE_DESKTOP.md`

## What a node does

| Area | What it does | Docs |
| --- | --- | --- |
| Event log | Ed25519-signed, hash-chained events in SQLite (WAL); loopback API on `127.0.0.1:5000` | `Admin/docs/` |
| Local token | Loopback endpoints require the node's token (`OMNI_LOCAL_TOKEN=1`; Bearer or `X-Omni-Local-Token`) | |
| Peer sync | LAN discovery (UDP `:9101`) and negentropy catch-up over `:5005` | `CATCHUP_SYNC.md` |
| Internet reach (v8.21) | Nodes behind a router join by dialing any reachable peer (signed `/p2p/hello`); UPnP, STUN and dial-back reachability show in node-health `wan` | `WAN.md` |
| Call relay (v8.22, in review) | Built-in STUN + TURN (UDP 3478, relay ports 49160-49200). Desktops relay by default; phones only when Omni Node turns it on. Apps get this node's relay plus up to four peers' relays. No third-party STUN/TURN when an Omni relay is known | `RELAY.md` |
| OmniCast signaling | Memory-only, signed mailbox `/api/signal/{send,poll,ice-config}` | `OMNICAST_SIGNALING.md` |
| Remote hub | Opt-in, bearer token, for use over Tailscale | `REMOTE_HUB.md` |
| Quiet mesh (v8.24, in review) | Pair devices with a 6-digit code or QR (`/api/pair/*`, loopback). Once paired, beacons carry only a rotating HMAC token on private subnets, and `:5005` closes silently for anyone who hasn't passed a signed challenge-response. `OMNI_QUIET_MESH=auto\|on\|off` | `QUIET_MESH.md` |
| Social import (v8.23, in review) | Person-signed `PROFILE_SOCIAL_IMPORT` events from Continuity-Engine on `POST /api/profile/social-import`: blind-hashed contacts only, max 500 per event | `SOCIAL_IMPORT.md` |

## No third-party servers
Calls connect directly when they can (LAN, IPv6, hole punching) and otherwise relay through another Omni node. Nothing depends on hosted TURN/STUN. The relay has caps (allocations, kbps, MB per day) and refuses to relay to loopback, link-local or multicast addresses; LAN and carrier-NAT targets need `OMNI_RELAY_ALLOW_PRIVATE=1`.

**Not yet:** TURN over TCP/TLS (for networks that block UDP), and field tests on real phones and carrier networks.

Split out of [kiroba/The-Omniverse](https://github.com/kiroba/The-Omniverse) (`desktop` branch) with full history, minus tracked `*.db` files. Formerly named The-Omni-Hub.
