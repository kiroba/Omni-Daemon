# Internet without Tailscale (daemon v8.21.0)

## What works
- **Any node that can dial a reachable node is on the mesh.** A phone behind carrier NAT dials out to a
  reachable peer (a friend whose router forwarded the port, your PC, a small server). It says a signed
  `POST /p2p/hello`, then catch-up sync pulls and pushes over that outgoing connection. The reachable
  peer never has to dial back.
- **OmniCast signaling crosses nodes.** `POST /api/signal/send` is queued locally and copied (one hop) to
  every dialable peer's `/p2p/signal/send`. While an app long-polls `/api/signal/poll`, its daemon runs
  the same signed poll on each peer (`/p2p/signal/poll`) and queues what comes back. So a caller and a
  callee on different phones meet through any peer both can reach, or directly on a LAN.
- **The node tells you if it's reachable.** Every 10 min (`OMNI_WAN_INTERVAL`): STUN finds the public
  IPv4, UPnP asks the home router to forward TCP 5005 (`OMNI_UPNP=0` turns this off), a global IPv6 is
  noted, and a peer outside the network is asked to dial back (`/p2p/dialback`). See `wan` in
  node-health: `public_addr`, `reachable` (true / false / null = not checked yet), `upnp`, `nat`, `detail`.
  Set `OMNI_PUBLIC_ADDR=host:port` when you forwarded the port yourself or run behind a DNS name.

## What doesn't (yet)
- Two nodes that are **both** behind carrier NAT, with no reachable peer between them, can't sync
  (that needs UDP hole punching plus a transport over UDP). Calls between them need TURN for media
  (`OMNI_TURN_URLS` + `OMNI_TURN_SECRET` on a reachable node).

## Security
- Peer endpoints need the mesh header. `/p2p/hello` must be signed by the caller's node key (5 min ts
  window) and, with `OMNI_TRUSTED_KEYS_FILE`, by a trusted key. Dial-back only connects to the caller's
  own address, once per address and port every 30 s. Signaling messages stay signed end to end and
  are never written to disk.
- UPnP exposes :5005 to the internet. Use a private `OMNI_MESH_ID` plus `OMNI_TRUSTED_KEYS_FILE`
  for anything beyond testing.


## v8.22.0: self-hosted relay
Media for calls between two carrier-NAT phones can now go through any reachable node's built-in TURN.
See [RELAY.md](RELAY.md).
