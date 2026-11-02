# OmniMind world library: daemon v8.26.0

**Status: first version (pre-alpha).** This is the shared, signed, versioned library of lore, places,
NPCs, quests, items and asset references that OmniMind builds as the world grows. The logic is in
`Admin/omni_omnimind.py`. The daemon imports it on the first `/api/omnimind/*` (or
`/api/v1/omnimind/*`) request. It uses the standard library only, so it runs under Chaquopy on Android.
Hosting cost stays at $0: everything runs on the nodes people already run, and nothing calls a paid API.

## Architecture: mini and full nodes

| Role | Set by | What it does |
|------|--------|--------------|
| **mini** (default, phones) | nothing | Stores, verifies, relays and serves world events. Takes person-signed requests, signals and flags from apps. Serves the local Zodiac NPC generator. **Never generates.** |
| **full** (desktops) | `OMNI_OMNIMIND_ROLE=full` | Everything a mini node does, plus a worker that picks up open requests (from any node, through the existing sync), answers them with proposals, and publishes approved ones. |

```
phone app ──signed omnimind_request──▶ mini node (phone) ──catch-up sync──▶ full node (desktop)
                                                                              │ generator
                                                                              ▼
phone app ◀── world entry ◀── catch-up sync ◀── omnimind_world_entry ◀── approve (owner) ◀── omnimind_proposal
```

There are no OmniMind tables. Every change is a signed event in the node's normal event log, and the
state is rebuilt by replaying those events in `(signed ts, event_id)` order, the same way the credits
ledger and the LPE work. Two nodes holding the same events **and the same owner list** compute the same
world (`world_root` matches). Events sync over the existing `/p2p/sync` relay and catch-up paths.
**No new ports.** `/api/events/append` refuses every `omnimind_*` action.

The full node's worker runs one pass every `OMNI_OMNIMIND_POLL_S` seconds (default 30, first pass after
`OMNI_OMNIMIND_STARTUP_DELAY`, default 5). Each pass:
1. publishes this node's `omnimind_capability` if it changed,
2. answers up to `OMNI_OMNIMIND_MAX_PER_PASS` (default 5) **open** requests, oldest first, with one
   `omnimind_proposal` each (a request that already has a proposal from any node is skipped, so two full
   nodes rarely both answer; if they race, the owner sees two proposals and picks one),
3. autopublishes clean proposals when autopublish is on (see Guardrails),
4. signs an `omnimind_checkpoint` every `OMNI_OMNIMIND_CHECKPOINT_EVERY` (default 25) new world heads.

`POST /api/omnimind/fulfil` (loopback only) runs one pass now.

## Events

All payloads carry `"schema": 1`. Field sets are exact (unknown fields are refused).

| Event (`action`) | Signed by | Payload | Replay rule |
|---|---|---|---|
| `omnimind_world_entry` | publisher | `kind` (lore/place/npc/quest/item), `id` (slug), `version`, `parent`, `title`, `summary`, `feed_injectable: false`; optional `body`, `tags[]`, `refs[]` (entry ids), `assets[]` (sha256), `attrs` (per kind, below), `source` (`request_id`, `proposal_id`, `generator`, `model`) | Counted only if the author is a **publisher**. Version 1 has `parent: null`; version n must name the event id of the current version n-1 of the same id and kind. A second version n on the same parent (a fork) loses to the earlier signed one. |
| `omnimind_asset_ref` | publisher | `sha256`, `kind` (image/svg/model3d/audio/composite/palette), `size`, `prompt`, `model`, `license` (CC0-1.0, CC-BY-4.0, CC-BY-SA-4.0, omni-generated, owner-only), `source_node`, `feed_injectable: false`; optional `name`, `mime`, `parents[]`, `tags[]`, `source` | First ref per sha256 wins. Points at a file in the source node's CAS dir. |
| `omnimind_request` | person | `kind` (the five entry kinds or `asset`), `need` (≤200); optional `context` (≤500), `tags[]`, `extends` (entry id: ask for its next version), `needs_gpu`, `sign` + `seed` (npc), `asset_kind` (asset) | Status is derived: open → proposed → published / rejected; `waiting_gpu` / `gpu_available` for needs_gpu; `flagged`. |
| `omnimind_proposal` | node | `request_id`, `publish_action`, `draft` (a full world entry or asset ref payload), `generator` {name, model, version}, `safety` {clean, hits[]}, `feed_injectable: false`; optional `retrieval[]`, `notes[]` | Never part of the world by itself. |
| `omnimind_review` | publisher | `target` (event id), `decision` (reject / keep / remove), optional `reason` | `reject` closes a proposal. `remove` hides an entry, `keep` un-hides it and clears its flags. |
| `omnimind_flag` | person | `target` (event id), `reason` (unsafe, offensive, copyright, spam, broken, off_canon, other) | One per person per target. |
| `omnimind_signal` | person | `target` (entry id), `signal` (pick, finish, skip, lose_to, report), `consent: "omnimind-signals-v0"` | One per person, target, signal and UTC day. **No free text.** |
| `omnimind_capability` | node | `role`, `llm`, `gpu`, optional `generators[]` | Latest per node. |
| `omnimind_checkpoint` | publisher | `root`, `count`, `upto_ts` | `verified` when replaying the heads up to `upto_ts` gives the same root. |

**Publishers** are the keys in `OMNI_OMNIMIND_OWNERS` plus this node itself. A world entry, asset ref,
review or checkpoint signed by anyone else is stored and relayed but **not counted** on this node.

Entry `attrs` by kind (all optional):
- lore: `era`, `canon`
- place: `region`, `biome`, `coords` [x, y]
- npc: `persona`, `archetype`, `traits[]`, `difficulty` 1-10, `home`, `trait_hash`, `layers[]`, `stats` {power, agility, wit, resolve}, `seed`
- quest: `giver`, `steps[]`, `reward`, `difficulty` 1-10, `place`
- item: `rarity`, `slot`, `power` 0-100

Content addressing: the event id is the sha256 of the canonical signed event, so every version of
every entry has a stable hash, and `parent` links make the history tamper-evident. `world_root` is the
sha256 of the canonical JSON `[[id, version, event_id], ...]` over all heads, sorted by id.

## Generators (full nodes)

1. **Template / remix (built in, always on, no ML, deterministic).** Seeded by the request's event id
   (or its `seed`), so the same request always gives the same draft.
   - Retrieves the most relevant world entries (TF-IDF, below) and remixes their titles, places and NPCs
     into lore, places, quests and items.
   - **Zodiac procedural NPCs.** For each of the 12 signs, a manifest with a normalized layer stack
     (`body`, `palette`, `outfit`, `accessory`, `aura`, always in that order with `z` 0-4, lowercase ids,
     `#rrggbb` tints, integer opacity percent), each layer picked deterministically from the sign plus
     the seed. `trait_hash` = sha256 of the canonical JSON of the traits (sign, element, modality,
     archetype, temperament, stats, quirk). The same sign and seed always give byte-identical output.
   - **Assets.** Recolours an SVG asset found in the local CAS dir (deterministic hue shift), or combines
     up to three asset refs into a `composite` JSON manifest, or (when there are none) emits a `palette`.
     The new file is written to the CAS dir and proposed as an `omnimind_asset_ref`.
2. **Local LLM (optional, off by default).** Set `OMNI_OMNIMIND_LLM_URL` to an OpenAI-compatible server,
   e.g. Ollama `http://127.0.0.1:11434` or llama.cpp `http://127.0.0.1:8080`, and optionally
   `OMNI_OMNIMIND_LLM_MODEL` (default `llama3.2`) and `OMNI_OMNIMIND_LLM_TIMEOUT` (seconds, default 60).
   - Context: the 6 most relevant world entries by TF-IDF retrieval, plus their play-signal rates.
   - Output must be one JSON object (`response_format: json_object` is requested). **Repair:** code
     fences, prose around the object and trailing commas are stripped; tags are normalised to slugs;
     unknown keys and attrs are dropped. The node fills `kind`, `id`, `version`, `parent` and `source`
     itself. Then the draft is schema-validated. If it fails, the model gets one retry with the error. If
     that fails too, the output is **rejected** and the node falls back to the template generator (the
     proposal's `notes` say so).
   - Assets and Zodiac NPC requests (with `sign`) never go to the LLM.
3. **Heavy image generation is not run in the daemon.** Keith's PC is iGPU-only, and an image model would
   make the daemon heavy and slow. A request with `needs_gpu: true` is never answered by the generators
   above. It shows `waiting_gpu` until some node advertises `gpu: true` (`OMNI_OMNIMIND_GPU=1` in its
   `omnimind_capability`), then `gpu_available`. The GPU worker that would answer it is **not built**.

**Retrieval** is plain TF-IDF in the standard library: tokens from title (×2), tags (×2), summary, body,
id and a few attrs; idf = ln((N+1)/(df+1)) + 1; cosine similarity; ties broken by id. Among equally
relevant entries, the remix prefers ones players finish and avoids ones they skip.

## Guardrails

- **Safety pass** on every generated draft and on request text: a built-in blocklist (sexual content and
  minors, self-harm, violent extremism, hate, drugs, scams), matched on normalised words (lowercase,
  leetspeak folded, punctuation collapsed, spelled-out letters joined), plus links, email addresses and
  phone numbers, plus the schema's own length and field limits. Extend the list with
  `OMNI_OMNIMIND_BLOCKLIST_FILE` (one term per line, `#` comments). It is a floor, not a moderation
  system. A request that fails it is refused (422). A proposal that fails it is kept, marked `flagged`
  with its hits, and never autopublished.
- **Review everything by default.** Results are published as `omnimind_proposal` only. Getting into the
  world needs an owner's approval. `OMNI_OMNIMIND_AUTOPUBLISH=1` lets this node publish a proposal
  itself, **only** when its safety pass is clean; everything else still waits in the review queue.
  Autopublish is a proposal Keith hasn't confirmed, so it stays off unless set.
- **Owners.** `OMNI_OMNIMIND_OWNERS` is a comma-separated list of 64-hex keys and/or `self` (this node's
  key). Default `self`, so on Keith's PC, loopback approval works out of the box. Add a desktop node's
  key on a phone so the phone counts that desktop's publications. Bad values are ignored with a warning.
- **Flags.** Anyone can sign an `omnimind_flag`. An entry with `OMNI_OMNIMIND_FLAG_HIDE` (default 3)
  distinct flaggers, or one flag from a publisher, is hidden from `/world` and listed in the review queue
  until an owner keeps it (or removes it).
- **Code is never generated or run.** OmniMind writes content (JSON) only. Code changes still go through
  Keith as PRs.

## Human Feed Policy

Generated content never enters the human social feed.
- Every world entry, asset ref and proposal must carry `"feed_injectable": false` (schema-required), and
  so does every Zodiac manifest.
- **Writes:** the daemon refuses (403) any non-`omnimind_*` event whose payload carries
  `"feed_injectable": false` anywhere, including inside a JSON string, whatever path it came in on:
  `/api/events/append`, `/api/social/*`, `/api/profile/*`, remote apps, and peers over `/p2p/sync`.
- **Reads:** `/api/events/feed` leaves out `omnimind_*` events and tagged payloads (pagination still
  fills each page). Asking for an `omnimind_*` type explicitly (`?type=omnimind_request`) still works,
  for tools; that is not the social feed.

## Learning, v0 (what it honestly means)

There is **no model training** and no fine-tuning. "Learning" in v0 is counting:
- Apps send opt-in, person-signed `omnimind_signal` events (pick, finish, skip, lose_to, report) with no
  free text. The consent string is part of the signed payload, so an event can't exist without it.
- Replay aggregates them per entry: counts, distinct people, pick / finish / skip / loss rates (one per
  person, target, signal and UTC day).
- Those numbers feed the generators:
  - **Difficulty:** when a new version of an NPC (or a quest from that NPC) is generated, a loss rate
    ≥ 0.6 lowers difficulty by 1, and a loss rate ≤ 0.15 with ≥ 3 finishes raises it by 1. This needs
    at least 5 signals from 3 people.
  - **What to vary:** NPCs with a skip rate ≥ 0.5 from ≥ 3 people are the `vary_candidates`; remix
    prefers often-finished entries over often-skipped ones.
  - **LLM context:** the signal rates of the retrieved entries go into the prompt.
- Signals are public, signed events: anyone holding them can see which key sent what. Apps should say so
  when asking for opt-in.

## API (:5000, same gate as the LPE endpoints)

Loopback is open as before (plus the local token on phones). Remote callers need the remote hub on and
the bearer token, and only the "remote" paths below.

| Method & path | Remote? | Body / query | Answer |
|---|---|---|---|
| `GET /api/omnimind/world` | yes | `q` (TF-IDF search), `kind` (or `asset`), `tag`, `limit` (≤200), `include_hidden=1` | `world_root`, `entries_total`, latest `checkpoint` (+`verified`), `entries[]` (or `assets[]`) with signals and flag counts |
| `GET /api/omnimind/world/{id}` | yes | `{id}` = entry id, event id (any version) or asset sha256 | entry with full payload and version `history`, or the asset (with `local_file`) |
| `POST /api/omnimind/request` | yes | signed `omnimind_request` envelope | stored event id + `request_status`; 401 bad signature, 400 schema / ts more than 10 min off, 422 safety |
| `GET /api/omnimind/requests` | yes | `status`, `author`, `limit` | requests with status and proposals, plus `node` (role, llm, autopublish, full/gpu node counts) |
| `POST /api/omnimind/signal` | yes | signed `omnimind_signal` envelope | stored event id |
| `POST /api/omnimind/flag` | yes | signed `omnimind_flag` envelope | stored event id |
| `GET /api/omnimind/review` | **loopback** | `limit` | open proposals with draft, safety, flags, `why_in_queue`, `stale`, and `to_sign` (the exact payload an owner key signs); `flagged_entries` |
| `POST /api/omnimind/approve` | **loopback** | `{"proposal_id"}` (node signs; needs `self` in owners), `{"target"}` (keep a flagged entry), or an owner-signed `omnimind_world_entry` / `omnimind_asset_ref` (payload = `to_sign.payload`) / `omnimind_review` keep | 200 published / kept; 403 not an owner; 409 already decided or stale version |
| `POST /api/omnimind/reject` | **loopback** | `{"proposal_id"\|"target", "reason"?}` or an owner-signed `omnimind_review` | 200 rejected / removed |
| `POST /api/omnimind/fulfil` | **loopback** | `{}` | one worker pass now (409 on a mini node) |
| `GET /api/v1/omnimind/generate-asset` | yes | `sign` (e.g. `Scorpio`), `seed` (default 0) | `{"status":"success","data": manifest}`; 400 bad sign or seed |

`GET /api/v1/omnimind/generate-asset` is the endpoint Plaza Brawler's "OmniMind generate-asset" label
refers to. It runs fully locally with no network and **writes nothing to the chain**. To put that NPC
into the world, sign an `omnimind_request` `{kind: "npc", sign, seed}`. A full node answers it with the
same manifest (same `trait_hash` and layers) as a proposal.

The existing `/api/omnimind/telemetry` cache (the OmniMind agent's, on `:9100`) is unchanged.

Signed envelopes are the canonical Omni envelope (`action, author_pubkey, payload, previous_hash, ts`)
with the author's Ed25519 signature, the same as `/api/lpe/*` and `/api/profile/update`. The node's own
key can't sign person events.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `OMNI_OMNIMIND_ROLE` | mini | `full` = fulfil requests |
| `OMNI_OMNIMIND_OWNERS` | `self` | publisher keys (64-hex, comma-separated) and/or `self` |
| `OMNI_OMNIMIND_AUTOPUBLISH` | off | `1` = publish clean proposals without review |
| `OMNI_OMNIMIND_LLM_URL` / `_MODEL` / `_TIMEOUT` | off / llama3.2 / 60 | OpenAI-compatible local LLM |
| `OMNI_OMNIMIND_GPU` | off | `1` = advertise gpu capability (no GPU worker yet) |
| `OMNI_OMNIMIND_CAS_DIR` | `~/.omni/omnimind/cas` | local content-addressed files (`<sha[:2]>/<sha>`) |
| `OMNI_OMNIMIND_FLAG_HIDE` | 3 | distinct flaggers that hide an entry |
| `OMNI_OMNIMIND_POLL_S` / `_STARTUP_DELAY` | 30 / 5 | worker pass interval and first delay (s) |
| `OMNI_OMNIMIND_MAX_PER_PASS` | 5 | requests answered per pass |
| `OMNI_OMNIMIND_CHECKPOINT_EVERY` | 25 | new heads between checkpoints |
| `OMNI_OMNIMIND_BLOCKLIST_FILE` | none | extra blocklist terms |

## Not built yet

- **Asset file transfer between nodes.** An `omnimind_asset_ref` syncs, but the file stays in the source
  node's CAS dir. Moving the bytes (pull by sha256 over the P2P port, with size caps) is a separate step.
- **GPU worker.** `needs_gpu` requests are queued and visible; nothing generates images, 3D or audio.
- **Phone-side mini UI.** No app screen yet for requests, review, signals opt-in or flags. The API is
  ready for it.
- **Model fine-tuning or training of any kind.** v0 learning is counting signals (above).
- **Play AI-content reporting UI in apps.** Google Play's AI-generated content policy needs an in-app
  way to report generated content. `omnimind_flag` is the backend for it; the app button isn't built.
- Per-sign art for the Zodiac layer ids (`body_lithe`, `pal_ember`, ...): the ids are stable, and the
  art behind them belongs to the apps / asset library.
- A distributed claim on requests: two full nodes can still both answer one request in a race.
