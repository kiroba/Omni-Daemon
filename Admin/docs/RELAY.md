# Built-in relay: STUN + TURN for OmniCast (daemon v8.22.0)

Every Omni node can relay call media for the mesh. Nothing is hosted by a third party: no Cloudflare,
no Google, and no eturnal needed (eturnal via `OMNI_TURN_URLS` + `OMNI_TURN_SECRET` still works).

## How a call finds a path
1. **Direct** (host / IPv6 candidates). Most US carriers give phones IPv6. Some filter inbound.
2. **Hole punching** via STUN. WebRTC does this on its own. The STUN server is now an Omni node.
3. **Relay** through a reachable node's TURN. Only used when 1 and 2 fail (both sides behind strict NAT).

`ice_transport_policy` is `all`, so relay is a fallback, not forced. With external TURN env set it
stays `relay`, as before.

## Who relays
| Node | Default | Notes |
|---|---|---|
| Desktop daemon | **on** | `OMNI_RELAY=off` to disable |
| Android (Omni Node) | **off** | Settings > Relay calls. Runs only on Wi-Fi + charging, with caps |

`OMNI_RELAY=on|off|auto` (auto = on unless Android).

## Ports
- UDP `OMNI_RELAY_PORT` (3478): STUN + TURN control
- UDP `OMNI_RELAY_PORTS` (49160-49200): relayed addresses

On a home network the daemon asks the router (UPnP) to forward them (`OMNI_UPNP=0` turns that off).
Otherwise forward them by hand. For the relay to work from the internet, `node-health.relay.public`
must show an address. Set `OMNI_RELAY_PUBLIC_IP` if STUN gets it wrong.

## Credentials
TURN REST: username `<expiry>:<app pubkey>`, password `base64(HMAC-SHA1(secret, username))`, 1 hour.
The secret lives at `~/.omni/relay_secret` (owner-only; `OMNI_RELAY_SECRET` overrides) and never
leaves the node. Credentials are only minted for an app that signed an `ice-config` request.

## Peers' relays
`GET /api/signal/ice-config` now returns this node's relay plus up to 4 peers' relays. For each peer the
node forwards the app's own signed ice-config query to `POST :5005/p2p/relay/cred` (same mesh header +
known-peer gate as signaling), and the peer answers with a credential for that app key. So a phone
whose node only knows your PC still gets your PC's TURN.

## Safety limits
- Peers on loopback, link-local, multicast or unspecified addresses: always refused (403).
- Private and carrier-NAT ranges (RFC 1918, 100.64/10): refused unless `OMNI_RELAY_ALLOW_PRIVATE=1`.
  This keeps the relay from becoming a doorway into the host or its LAN. Turn it on for Tailscale testers.
- `OMNI_RELAY_MAX_ALLOC` (16) allocations, 4 per credential.
- `OMNI_RELAY_MAX_KBPS` (2500) per allocation; excess packets are dropped.
- `OMNI_RELAY_MAX_MB_DAY` (0 = unlimited); when spent, new allocations get 486.

## Loopback API (local token when `OMNI_LOCAL_TOKEN=1`)
- `GET :5000/api/relay/status`
- `POST :5000/api/relay/config` `{"enabled": bool, "max_kbps": int, "max_mb_day": int, "allow_private": bool}` (all optional)
- `node-health` has a `relay` block: enabled, port, public, allocations, mb_today, caps, upnp, stats.

## Not implemented yet
TURN over TCP/TLS (some networks block UDP), IPv6 relayed addresses, EVEN-PORT / RESERVATION-TOKEN.

## Verified
- 19 tests in `tests/test_turn_relay.py`, including the RFC 5769 integrity vector and a two-daemon
  peer-relay test.
- Interop with an independent TURN client (aioice): allocate, permission, channel data both ways,
  refresh-to-zero.
- **Not yet tested on real networks or phones.**
