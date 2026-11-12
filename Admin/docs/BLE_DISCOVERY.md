# BLE discovery (daemon v8.32.0)

Proximity discovery over Bluetooth Low Energy: which Omni nodes are physically
near this one. Engine: `Admin/omni_ble_discovery_engine.py` (v2.0.0).

## Wire format — the one already in the field

The Omni Node Android app (`NodeRadio.kt`) advertises BLE service UUID **0xFFA0**
whose service-data value is the **first 16 bytes of the node's Ed25519 public
key**, and scans for the same. The daemon engine encodes/decodes exactly that
(`encode_service_data` / `decode_service_data`, `key_prefix_hex` mirrors
NodeRadio's `keyPrefix()`), so daemon and every phone build see each other.

## Flow

Phone radios are unreachable from Python on Android, so the radio layer reports
what it hears:

`POST :5000/api/ble/nearby` (loopback + local token only; never served over the
remote hub)
```json
{"seen": ["<32 hex key prefix>", "..."]}
```
At most 64 prefixes per report. The daemon keeps them in an in-memory registry
(90 s freshness — the same window NodeRadio uses — bounded at 256, oldest
evicted). Own advertisements and malformed prefixes are refused and counted.

Read it back in **node-health** (`:9100` `GET /api/v1/telemetry/node-health`):

```json
"ble": {"nearby": 1, "prefixes": ["aaaa..."], "last_seen_ago_s": 3.2,
        "reports": 5, "rejected": 1}
```

and in the **telemetry summary** as protocol `ble_discovery` (nearby count +
report/refusal counters); the summary's `capabilities.ble_discovery` state is
`wired`. Node-health also lists `ble_discovery` in `features`.

## Trust and privacy

- A sighting is a **hint, never a peer**: it carries no address, proves nothing,
  and never creates a sync relationship. Peers are still made only by signed
  beacons, quiet-mesh pairing, addressed radio peers (see RADIO_PEERS.md), or
  WAN dial-out. BLE's job is to say "a node is near — try Wi-Fi Aware / LAN /
  pairing now."
- The registry holds **only key prefixes and times** — the same disclosure as
  the beacon prefixes the summary already prints. Nothing about proximity is
  written to disk (in-memory, pruned on read), per the no-logs posture.

## Introductions: the BLE-met record (v8.33.0)

A completed GATT exchange is more than a sighting. The peer hands over an
**introduction**: its full public key, mesh, display name, current endpoints,
and a signature over the `omni.p2p.hello.v1` bytes (`hello_bytes(node_id,
mesh_id, ts)` — no new crypto format). The radio layer hands it to
`POST :5000/api/ble/met` (loopback + local token). The daemon verifies the
signature, the mesh and the allowlist (same checks as `/p2p/hello`), and only
then:

- stores the meeting in `~/.omni/ble_met.json` — persistent across restarts,
  **not** on the 120 s radio-peer TTL (a meeting is a durable fact, like a
  pairing; this is the one BLE artifact written to disk, and it holds only
  what the peer signed an introduction for: public key, name, endpoints);
- registers every dialable endpoint at once via the radio-peer path
  (transport `ble`), so catch-up syncs on the next wake; and re-registers
  them at startup;
- keeps the stored endpoints in the catch-up dial set for 24 h after the
  meeting, so when an IP path appears later the two nodes sync without
  re-doing the introduction.

The signature covers the identity (node_id / mesh / ts, 5-minute window),
not the endpoints: endpoints stay dial hints, and the sync they trigger is
itself signature-gated end to end. Node-health's `ble` block gains a `met`
summary (count + key prefixes); the telemetry `ble_discovery` counts gain
`met` / `met_accepted`.

## Tests

`tests/test_ble_discovery.py` — wire format vs NodeRadio, own/junk refusal,
expiry and bounds, plus a live-daemon pass: report → node-health → summary,
asserting `active_peers` stays 0 for a sighting.

`tests/test_ble_met.py` — the met store (persistence, dial window, bounds)
plus a live-daemon pass: a verified introduction registers a peer and
persists; a bad signature, a wrong mesh and a stale hello do not.

## Not here

- The desktop daemon has no BLE stack and no server path; its registry simply
  stays empty. BLE is a phone/edge transport. (The met endpoint exists there
  too, but nothing reports to it without a radio layer.)
- NodeRadio reports sightings to `/api/ble/nearby` since Omni-Node-Daemon
  PR #14. The GATT introduction exchange itself (connectable advertising +
  reading the peer's introduction blob) is the remaining app-side piece.
