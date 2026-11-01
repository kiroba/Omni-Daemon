# Social profile import (daemon v8.23.0)

`POST :5000/api/profile/social-import` accepts the `PROFILE_SOCIAL_IMPORT` event that
[kiroba/Continuity-Engine](https://github.com/kiroba/Continuity-Engine)
`integrations/continuity_social_import_engine.py` produces (`import_external_profile()`, sent with
`post_to_daemon()`). It is a **person-signed event**, handled like profiles and comments: the person's
Ed25519 key signs, the hub verifies, stores the event as signed, and relays it to peers. Same gate as the
other `:5000` paths (loopback, or remote hub with the bearer token).

## Envelope

The canonical signed event (see `SIGNED_EVENTS.md`): exactly `action`, `author_pubkey`, `previous_hash`,
`ts` (int ms), `payload`, `signature`, optional `event_id`. Unknown envelope fields → 400.

## Payload (exactly these keys)

```json
{"import_event_id": "<uuid4>", "platform_source": "X_TWITTER", "external_handle": "@alice_creator",
 "imported_bio": "Digital artist", "total_contacts_imported": 2,
 "contact_blind_hashes": [{"display_name": "Bob Smith", "blind_hash": "<64 hex>", "platform": "X_TWITTER"}],
 "fanbase_bootstrap_status": "PENDING_P2P_MATCHING"}
```

| Field | Rule |
| --- | --- |
| `import_event_id` | lowercase UUID4 |
| `platform_source` | 1-32 chars, upper case, trimmed |
| `external_handle` | 1-100 chars, trimmed, no control characters |
| `imported_bio` | 0-1000 chars, trimmed, newlines allowed |
| `contact_blind_hashes` | list of at most 500 `{display_name (0-64 chars, trimmed), blind_hash (64 lowercase hex), platform}`; each `platform`, upper-cased, must equal `platform_source` |
| `total_contacts_imported` | must equal the list length |
| `fanbase_bootstrap_status` | `PENDING_P2P_MATCHING` (the only value the engine emits) |

`blind_hash` is `sha256("OMNI_SALT_2026:" + identifier.strip().lower())`; a raw email or phone number is
rejected. Body cap 128 KB (413 above it). A 500-contact import with ASCII names fits; very long
non-ASCII names can hit the body cap first, so split large imports.

## Answers

200 `{"status": "SUCCESS", "event_id", "action", "author_pubkey", ...}` (replay → `"result": "ALREADY_EXISTS"`),
400 schema, 401 signature, 413 body too large, 429 rate limit (`OMNI_USER_RATE_MAX` per key per minute,
shared with profile updates, comments and reactions). `POST /api/events/append` refuses the action (403).

## Not built

- No read endpoint for imports yet; they show in `GET /api/events/feed`.
- Friend matching (`match_contacts_and_bootstrap_fanbase`) and fan-link events are not handled by the hub.
- The salt is a fixed constant, so blind hashes of phone numbers are guessable by brute force; they hide
  identifiers from casual reading, not from a determined peer.

## Tests

`tests/test_social_import.py`: validator accepts/rejects, contact cap, registration; live daemon accepts a
signed import and dedupes a replay, rejects tampered/forged signatures (401), malformed payload and envelope
(400), an oversized body (413), and refuses `/api/events/append` (403). Set `CONTINUITY_ENGINE_DIR` to a
Continuity-Engine checkout to also sign and post with that repo's own engine.
