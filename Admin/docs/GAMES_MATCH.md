# Game match results (daemon v8.28.0)

Games on the phone (first: Plaza Brawler, spec 5.3) can hand a finished match to Omni Node. The node stores
it as a signed `game_match` chain event, relays it to peers and syncs it over catch-up like every other
event. Match results are not posts: `/api/events/feed` leaves them out unless you ask with
`?type=game_match`.

Same gate as every `:5000` path: open on loopback; with `OMNI_LOCAL_TOKEN=1` (the Android node app) send
`Authorization: Bearer <Omni Node token>` or `X-Omni-Local-Token: <token>`; from another device, remote hub
on + `Authorization: Bearer <hub token>` (both paths are remote-allowed).

## Is it there? (capability probe)

`GET :9100/api/v1/telemetry/node-health` now carries `"features": ["games_match"]`. Nodes before 8.28.0
have no `features` key (treat it as `[]`) and answer `404` to both paths below.

## POST /api/games/match

Body (max 4 KB), a bare summary, exactly what `MatchShare.payloadFrom` builds:

```json
{"game": "plaza_brawler", "mode": "arcade", "p1_sign": "aries", "p2_sign": "scorpio",
 "p2_source": "omnimind", "winner": "p1", "rounds": 2, "score": 1200, "duration_ms": 61000,
 "app_version": "5.4.0"}
```

| field | rule |
|---|---|
| `game` | **required**, 1-32 chars `a-z 0-9 _` (lowercased) |
| `winner` | **required**, `"p1"`, `"p2"` or `"draw"` |
| `mode`, `p1_sign`, `p2_sign`, `p1_source`, `p2_source` | optional, 1-32 chars `a-z 0-9 _` (lowercased) |
| `rounds` | optional int 0-99 |
| `score` | optional int 0-1,000,000,000 |
| `duration_ms` | optional int 0-86,400,000 |
| `app_version` | optional string, max 32 chars |
| `schema` | optional, must be `1` (added if missing) |

A key whose value is `null` is treated as absent. Any other key is refused (400), so nothing unexpected is
stored or gossiped. The stored payload is the cleaned summary plus `"schema": 1`. A bare summary is signed
by **this node's** key (the app is trusted because it passed the token gate).

Optional, player-signed: send the canonical signed envelope instead (same as `/api/profile/update`, see
SIGNED_EVENTS.md): `{"action": "game_match", "author_pubkey", "previous_hash", "ts", "payload",
"signature"[, "event_id"]}`. The node verifies the Ed25519 signature (401 if it fails), applies the per-key
rate limit (429) and stores the event as signed by the player. The signed `payload` must already be in the
clean form above (lowercase ids, no nulls, `"schema": 1`), because the node can't change signed bytes (400).

Responses:

- `200 {"status":"SUCCESS","event_id":"0x…","signed_by":"node"|"player","ts":<ms>}` (a replayed signed
  event adds `"result":"ALREADY_EXISTS"`)
- `400` bad body / field, `401` bad token or bad player signature, `411` empty body, `413` over 4 KB,
  `429` player key rate-limited, `404` on nodes older than 8.28.0.

Every bare POST is a new event (no dedupe), so an app should not retry a POST that got a 200.

## GET /api/games/matches?limit=&before=&game=

Newest first. `limit` default 20, max 200; `before` = an `id` cursor (use `next_before`); `game` filters by
game id. Only events whose signature re-verifies and whose payload passes the rules above are listed.

```json
{"status":"OK","node_id":"…","version":"8.28.0","limit":20,"count":1,"has_more":false,"next_before":null,
 "matches":[{"id":42,"event_id":"0x…","ts_ms":1762200000000,"author_pubkey":"…",
             "signed_by":"node","origin":"local","verified":true,
             "match":{"game":"plaza_brawler","mode":"arcade","winner":"p1","rounds":2,"schema":1, "…":"…"}}]}
```

`signed_by` here is `"node"` when this node signed it, else `"other"` (a player key or another node; see
`author_pubkey`). `origin` is `local` / `remote` as in the event feed.

`/api/events/append` refuses `action: "game_match"` (403): match events only come in through this endpoint.

Tests: `python3 -m unittest tests.test_games_match` (validation, API, local token, two-node catch-up).
