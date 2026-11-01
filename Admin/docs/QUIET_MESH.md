# Quiet mesh: pairing, private beacons, stealth entry (daemon v8.24.0)

Before 8.24 every node shouted on the LAN: UDP `:9101` beacons went to `255.255.255.255` with the node's
public key (its id), mesh name and port in the clear, and anyone could talk to `:5005` (it answered with
JSON errors, a `Server:` banner and, on `/p2p/hello`, the node id and version). Quiet mesh changes that
once you pair a device.

Code: `Admin/omni_quiet_mesh.py` (crypto, store, gate, interfaces) and the "quiet mesh" section of
`Admin/omni_live_daemon_server.py` (HTTP glue). Tests: `tests/test_quiet_mesh.py`. Nothing new to
install: X25519, AES-GCM and Ed25519 come from `cryptography`, which the daemon already needs (also under
Chaquopy on Android); everything else is the standard library.

## Modes: `OMNI_QUIET_MESH`

| Value | Behaviour |
|---|---|
| `auto` (default) | Exactly the pre-8.24 behaviour until the first pairing; quiet from then on. Remove every paired device and it goes back. |
| `on` | Quiet even with nobody paired: no beacons at all, `:5005` silent to everyone (pairing still works while a code shows). |
| `off` | The pre-8.24 daemon. `/api/pair/*` answers 409, the paired-devices file is ignored. |

## Pairing

All on the loopback app port (`:5000`), never in the remote-hub allowlist, and behind the local token
when the node runs with `OMNI_LOCAL_TOKEN=1` (Omni Node on Android and desktop always do).

| Call | What it does |
|---|---|
| `POST /api/pair/offer` `{"ttl_s": 300}` | New offer (replaces any open one): 6-digit `code`, `payload` (`omni://pair?p=...`, for a QR or for pasting), `addresses`, `expires_in` (60-600 s, default 300), `attempts_left` (5). |
| `GET /api/pair/offer` | The open offer, or `{"active": false, "ended": "paired"/"locked"/"expired"/"none"}`. |
| `POST /api/pair/cancel` | Close the offer. |
| `POST /api/pair/accept` `{"code": "123456"}` or `{"payload": "omni://pair?p=..."}`, optional `"host"`, `"port"` | Pair with a device showing a code. With a code and no address it listens up to 15 s for that device's "pairing open" beacon. 200 `{"peer": {...}}`; 403 "That code didn't match." (+ `attempts_left`); 410 too many wrong codes; 404 nobody nearby is showing a code; 502 couldn't reach it. Blocks up to ~25 s. |
| `GET /api/pair/list` | `{"quiet": {...}, "peers": [{"pubkey", "tag", "name", "address", "paired_at", "last_seen", "online"}]}`. Never the mesh key. |
| `POST /api/pair/remove` `{"pubkey": "..."}` | Forget a device (this side only; the other side keeps trying and is ignored). |

node-health (`:9100`) carries `"quiet": {"mode", "active", "paired", "online", "offer_open", "offer_expires_in", "counts"}`.

**The exchange** (over `:5005`, path `/p2p/pair`, reachable only while an offer is open):

1. B → A: `{step 1, mode: code|qr, eb (X25519), nb}`. A answers `{sid, ea, na, salt, iters}`.
2. Both derive `secret`: PBKDF2-HMAC-SHA256(code, salt, 200,000 rounds) for a typed code, or
   HMAC(mesh key from the QR, salt, code) for a scanned/pasted payload. `K = HMAC(secret, X25519(eb, ea) || transcript)`.
3. B → A: `{step 2, pk_B, port, name, Ed25519 sig over the transcript, MAC_K("B", ...)}`. A checks the MAC
   first. Wrong: 403 with attempts left; the 5th wrong code locks the offer (410) and counts toward the
   IP block below. Right: A stores B and answers `{pk_A, port, name, sig, MAC_K("A", ...), AES-GCM_K(mesh key)}`.
4. B checks A's MAC (A knows the code too), A's signature, for QR mode that A's key is the one in the QR,
   decrypts the mesh key and stores A.

Each pairing gets a fresh 32-byte mesh key. Stored in `~/.omni/paired_peers.json` (`OMNI_PAIRED_PEERS_FILE`
to move it): pubkey, mesh key, name, last address. Owner-only: `0600` on POSIX, an owner-only ACL via
`icacls` on Windows (same helper as the token files). An unreadable file is renamed `.bad`, never
overwritten silently.

**Paired keys as the allowlist.** Paired devices are the only peers a quiet node talks to, so the store
is the peer allowlist. When `OMNI_TRUSTED_KEYS_FILE` is also set, paired node keys are added to that
event-author allowlist (so their node-signed events are accepted). When it isn't set, event authors are
still not restricted (phones' person keys sign posts; restricting authors to node keys would drop them).

## Quiet beacons

Every 3 s, per paired mesh key, 48 bytes: `nonce(16) || HMAC-SHA256(beacon key, epoch(8, big-endian) || nonce)`,
where `beacon key = HMAC(mesh key, "omni-beacon-v1")` and `epoch = unix time // 300`. No node id, pubkey,
mesh name, version or port. Receivers try each paired key for the current epoch and one either side; a
match from IP X makes the node open a signed session to X on the port that device gave when pairing.
A beacon alone never makes a peer live: only a completed session does, so a replayed beacon gets nobody in.

Sent to the directed broadcast address of every private IPv4 interface only (10/8, 172.16/12, 192.168/16),
never to `255.255.255.255`, Tailscale/CGNAT (100.64/10), link-local or loopback. Linux and Android read
each interface's netmask; Windows and macOS use the host's addresses with an assumed /24.
`OMNI_QUIET_BEACON_TARGETS="host:udpport,..."` adds unicast targets (labs, a tailnet, the tests).

While an offer is open the node also sends `{"type": "OMNI_PAIR_OPEN", "port": P}` (no key, code or id)
so a device typing the code can find it. Open (pre-8.24) beacons from other nodes are ignored in quiet mode.

## Stealth entry on :5005

In quiet mode:

* Every new connection first passes a per-IP token bucket (burst 30, 5/s; `OMNI_P2P_RATE_BURST`,
  `OMNI_P2P_RATE_PER_S`) and the block list. Refused connections are closed before a byte is read or written.
* `/p2p/auth` is the signed challenge-response (below). Anything else needs a valid session header.
* A caller that fails either (wrong path, no/bad session, bad hello, replayed counter, malformed HTTP,
  GET, OPTIONS...) gets the connection closed with **zero bytes**: no status line, no `Server:` banner,
  no version, no node id. 5 failures in 5 min block that IP for 10 min (`OMNI_P2P_FAIL_LIMIT`, `OMNI_P2P_BLOCK_S`).
* `/p2p/pair` answers only while an offer is open.

**Challenge-response** (`POST /p2p/auth`, two steps, ephemeral X25519 on both sides):

1. Client: `{nc, ec, ts, m: [...]}`, where `m` holds `HMAC(auth key, "hello", nc, ec, ts)` for each of its
   paired keys, padded with random filler to a multiple of 8 and shuffled (so it doesn't show how many
   pairings it has, or which). `ts` must be within 2 min and `nc` unseen. An `OMNI_TRUSTED_KEYS_FILE` peer
   without a pairing signs the hello instead (`pk` + Ed25519 sig; this shows its pubkey on the wire).
   Server finds the matching key or closes silently. It answers `{sid, ns, es, m: HMAC(auth key, "server", transcript)}`.
2. Client: `{sid, sig}`: its node key signs the transcript (nonce from the server included). Server checks
   it against the paired key that matched and answers `{sig}`: its own node key over the transcript (the
   server proof the client checks against the key it paired with).

Session key = `HMAC(HMAC(mesh key, "session", X25519 shared), transcript)`, 10 minutes. Each request then
carries `X-Omni-Session: <sid>.<counter>.<HMAC(session key, sid, counter, method, path, sha256(body))>`,
bound to the client's IP; a counter is accepted once. With a valid session the mesh header and the
known-peer gate are not needed; events inside are still hash- and signature-checked like before.

Clients use this automatically for every peer call (live relay, catch-up, hello/dial-back, signal
forwarding). Paired devices are dialed at the address they were last seen at, so pairing over Tailscale
or a reachable public address keeps working without beacons.

## What it does not do (honest list)

* **The port is still open.** A scanner sees that something accepts TCP on 5005 and UDP on 9101 (and on
  Android/desktop, a host sending 48-byte UDP packets every 3 s to the broadcast address). It can't tell
  it's an Omni node from a banner, or which node, but "unknown service that closes at once" is itself a
  fingerprint.
* **The code exchange is not a PAKE.** A typed 6-digit code is stretched with PBKDF2 (200k rounds) and
  only 5 guesses are allowed online. But someone who actively intercepts the exchange (for example ARP
  spoofing on your Wi-Fi) while the code shows, posing as the device showing it, receives a MAC they can
  attack offline: 10^6 codes x 200k rounds. To use it they must finish within the ~15 s the typing device
  waits, which takes a lot of compute, but it is possible. A passive listener learns nothing (ephemeral
  X25519). Scanning or pasting the QR payload uses its 32-byte key instead and doesn't have this weakness,
  so prefer it.
* The "pairing open" beacon shows that some Omni node is pairing at that IP while the code shows.
* Traffic after authentication is plain HTTP: the session MAC stops tampering and injection, but a LAN
  sniffer can read the (already signed, already public-to-the-mesh) events being synced, and see the
  IPs talking. Encrypting the body is a later step.
* Removing a device only deletes it on this side; the other device must remove it too.
* Not tested yet on real phones, real Wi-Fi networks or Windows broadcast routing (CI covers Windows for
  the daemon and the desktop window).
