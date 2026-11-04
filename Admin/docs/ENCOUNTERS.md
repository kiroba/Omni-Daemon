# Encounters (daemon v8.29.0, pre-alpha)

"StreetPass"-style meetings: two Omni nodes on the same network and the same mesh, both opted in, swap a
small signed card with a display name the first time they meet, remember when they met, and then sync
their feeds. Everything runs over the LAN path the daemon already has (UDP `:9101` beacons, TCP `:5005`).

**Off by default.** Nothing new is sent or stored until the owner turns it on with a display name.

## Privacy model (Keith's rule)

No personal or identifying data except a display name, and the display name crosses only on a first
in-person meeting.

| | Sent | Stored (this device only) |
| --- | --- | --- |
| Beacon | `enc`: 128-bit random id from `secrets.token_hex`, rotated every 15 min, only while encounters are on | - |
| Handshake | Card-key proof (`card_pubkey`, `ts`, `sig`), no name | - |
| First meeting | Card: `{schema: 1, display_name, card_pubkey, ts, sig}`, only to a peer that proved its key and doesn't know us yet | The peer's card, `first_met`, `last_met` (ms), `meet_count` |
| Later meetings | Proofs only (no name) | `last_met`; `meet_count` +1 after a 30-minute gap |

Never sent or stored in an encounter record: IP address, port, location, device model, phone number,
email, contacts, or the node key. The store is `~/.omni/encounters.json` (owner-only, override with
`OMNI_ENCOUNTERS_FILE`); the card key is a **separate** Ed25519 key in
`~/.omni/encounter_card_ed25519.key`, so a card can't be matched to the node id that signs events.

- Display names are NFC-normalised, control/format/private-use characters (zero-width, bidi overrides)
  are dropped, whitespace is collapsed, 1-32 characters. A name with `@` or 7+ digits is refused, so it
  can't carry an email address or phone number. A received card must already be in that form.
- Cards are not chain events: they never enter the event DB, never relay or sync onward (gossip,
  catch-up), and never appear in `/api/events/feed`.
- Peer addresses are needed to run a handshake and the catch-up. They live in memory only (rate-limit
  tables) and are gone on restart.
- `/p2p/encounter` keeps the existing gates: mesh header (403 otherwise), discovered/known peers only,
  8 KB body cap, and the beacon itself must pass the mesh and signature/allowlist checks first.
- A client proof is signed over the server's current beacon id, so a captured proof can't be replayed to
  another node; the server's proof is signed over the client's card key.

### Trade-off: the beacon still carries `node_id`

Open beacons have carried the node's Ed25519 public key (`node_id`) since v8.9: the beacon signature, the
trusted-keys allowlist (`OMNI_TRUSTED_KEYS_FILE`) and peer bookkeeping need it. Removing it would break
discovery for every existing node, so v8.29.0 leaves it in. That means anyone on the same Wi-Fi can still
recognise a node across `enc` rotations by `node_id`. The rotating `enc` id adds no new linkability, and the
encounter card uses a different key, but true unlinkability needs the quiet mesh (v8.24, beacons are only
an HMAC token) or a future beacon format. Encounters do not run in quiet mode yet (no open beacons there).

## API (loopback `:5000`, plus the local token when `OMNI_LOCAL_TOKEN=1`; never over remote hub)

| Call | Body / answer |
| --- | --- |
| `GET /api/encounters` | `{enabled, display_name, count, encounters: [{id, display_name, card_pubkey, first_met, last_met, meet_count}], handshakes: {ok, failed, cards_received}, healing: {runs, rate_limited, last_at, last_stored, last_pushed, last_ok}}` |
| `POST /api/encounters/settings` | `{"enabled": true, "display_name": "Kiro"}` (either key). Turning on needs a name. 400 on a bad name or unknown key |
| `DELETE /api/encounters/<id>` | Forget one (`id` = 16 hex). 404 if unknown |
| `DELETE /api/encounters` | Forget everyone: `{"forgotten": n}` |

DELETE is not listed in the CORS preflight answer, so a web page can't send it. node-health lists
`"encounters"` in `features`.

Peer endpoint: `POST :5005/p2p/encounter`. Round 1 `{schema, hello}` → `{hello, need_card}`. Round 2
(only when a card is missing on either side) `{schema, hello, card?, want_card}` → `{hello, need_card, card?}`.
A node with encounters off answers 404 and stores nothing.

## Swarm healing

After every successful handshake (a new meeting, a rotated id, or a known peer coming back after a gap) the
node runs the existing negentropy catch-up (`/p2p/reconcile` + `/p2p/events`, v8.8) with that one peer:
pull what we lack (every event re-verified), push what they lack. At most once per peer per 5 minutes; it
shares the catch-up lock with the periodic run. New posts keep flowing through the existing gossip relay;
there is no new feed format.

## Settings (env, mostly for tests)

| Env | Default | |
| --- | --- | --- |
| `OMNI_ENCOUNTER_ROTATE_S` | 900 | beacon `enc` rotation |
| `OMNI_ENCOUNTER_CATCHUP_GAP_S` | 300 | healing rate limit per peer |
| `OMNI_ENCOUNTER_RETRY_S` | 60 | min gap between handshake attempts per address |
| `OMNI_ENCOUNTER_MEET_GAP_S` | 1800 | gap that counts as a new meeting |
| `OMNI_OPEN_BEACON_TARGETS` | - | extra unicast `host:udpport` targets for open beacons (labs, tests, Wi-Fi that drops broadcast) |

## Not done yet

- **No Bluetooth (BLE).** LAN / same network only. BLE encounters belong in a later Omni-Node (Kotlin) PR
  that hands peers to the daemon.
- **Untested on real phones.** Verified with two daemons on one machine over loopback
  (`tests/test_encounters.py`); not on Android, real Wi-Fi, or client-isolating access points.
- No app UI yet (Omni Node / KickBack need a toggle, a name field and a "people you met" list).
- Not in quiet mode (see the trade-off above).
- No blocking list: forgetting someone means the card comes back if you meet again while encounters are on.
