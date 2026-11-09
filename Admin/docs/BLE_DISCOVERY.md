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

## Tests

`tests/test_ble_discovery.py` — wire format vs NodeRadio, own/junk refusal,
expiry and bounds, plus a live-daemon pass: report → node-health → summary,
asserting `active_peers` stays 0 for a sighting.

## Not here

- The desktop daemon has no BLE stack and no server path; its registry simply
  stays empty. BLE is a phone/edge transport.
- NodeRadio does not yet POST sightings to this endpoint (it keeps its own
  `radio_status.json` count for the node screen). Wiring that one call in the
  app is a small follow-up; the daemon side is complete and app-agnostic —
  any reporter with the local token can feed it.
