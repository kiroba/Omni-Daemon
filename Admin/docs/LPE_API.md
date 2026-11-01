# Live Persona Engine (LPE) API: daemon v8.25.0

**Status: pre-alpha.** This adds avatars, idle progress, vibe boosts and a basic reputation score to
Omni Node. It is a networked port of three in-memory engines from `kiroba/The-Omniverse`
`core/engines/` (`lpe_idle_plaza_engine.py`, `lpe_reputation_aura_engine.py`,
`lpe_p2p_jail_quarantine_engine.py`). The logic is in `Admin/omni_lpe.py`. The daemon imports it on the
first `/api/lpe/*` request. It uses the standard library only, so it runs under Chaquopy on Android.

## How state works

There are no LPE tables. Every change is a signed event in the node's normal event log. The state is
rebuilt by replaying those events, the same way the credits ledger works. The replay sorts events by
`(signed ts, event_id)`, not by arrival order. Two nodes holding the same events therefore show the same
state.

| Event (`action`)      | Signed by | Payload (`schema: 1` always)                                   | Replay rule |
|-----------------------|-----------|----------------------------------------------------------------|-------------|
| `lpe_profile`         | person    | `handle`, optional `avatar`, `cosmetics[]`, `ambient_activity` | Latest wins. The first one registers the key in the plaza. |
| `lpe_vibe_boost`      | person    | `target`                                                        | +1 vibe and +25 XP to the target. At most `OMNI_LPE_VIBES_PER_DAY` (default 3) count per sender per target per UTC day, by signed ts. The target needs a profile, and boosting yourself doesn't count. |
| `lpe_progress_claim`  | person    | `elapsed_s` (1..86400)                                          | The claim is clamped to the real time since the author's previous claim (or first profile). 10 XP and 2 plaza credits per minute. A claim made before the author's first profile is ignored. |
| `lpe_reputation_vote` | person    | `target`, `rating` (-5..5; 0 withdraws)                         | The latest vote per rater and target wins. Self-votes are ignored. Votes are the only input to trust. |
| `lpe_quarantine`      | **node**  | `target`, `decision` (`quarantine`/`release`/`rehab`), `reason`, `clean_relays` (rehab only) | Applied **only** by the node whose key signed it. Peers can receive a copy through catch-up sync, but they ignore it, so it is never a verdict on anyone elsewhere. |

Person events use the canonical Omni envelope (`action, author_pubkey, payload, previous_hash, ts`)
with the person's Ed25519 signature, the same as `/api/profile/update`. The node verifies them, stores
them and relays them to peers, and they sync over the existing `/p2p/sync` and catch-up paths. **No
new ports.** `/api/events/append` refuses all `lpe_*` actions. Each read re-verifies every LPE event's
signature, with a cache that clears when a new LPE row arrives.

**Plaza credits** are a game counter carried over from the idle engine. They are **not** Omni Credits,
and nothing moves on the credits ledger.

## Trust score (pre-alpha approximation)

This is a small EigenTrust-style power iteration over the `lpe_reputation_vote` graph:

1. Local trust `c[r][t]` is rater `r`'s positive ratings, normalised per rater.
2. `t = 0.15 * p + 0.85 * C^T t`, iterated until the L1 change is below 1e-10 (at most 200 rounds).
   The pre-trust `p` is uniform over this node's **paired keys** (when the daemon has them) plus its
   **trusted-keys allowlist** that appear in the graph. If no seed appears there, `p` is uniform over
   every key in the graph. A rater with no positive votes hands its share back to `p`.
3. Negative ratings become a trust-weighted distrust mass:
   `d[i] = sum_r t[r] * |neg rating r->i| / (r's total |rating|)`.
4. `score = 500 * n * (t[i] - d[i])`, clamped to -200..1000. A key with exactly average trust scores
   500. A key nobody has rated gets 200 (CITIZEN), as the in-memory engine did.

Tiers are unchanged from the aura engine: PARAGON ≥ 800, CITIZEN ≥ 200, DISTRUSTED ≥ 0, SLASHED < 0.
Quarantine follows the jail engine. Below 200 means DISTRUSTED_QUARANTINED, below 0 means
SLASHED_BANNED, and an owner `quarantine` decision forces quarantine on this node. A `rehab` tick adds
+5 per clean relay to the local score, but not for a SLASHED key.

Be honest about what this is: it is computed on one node from the votes that node holds. It has no
distributed score managers and no protection against sybil rings when no seed keys are configured
(uniform pre-trust gives a vote ring its own mass). Treat it as a display hint, not a security
boundary.

## Gate

Every path sits behind the same `remote_gate` as the other `:5000` app APIs. Loopback is open, or
needs `X-Omni-Local-Token` / `Authorization: Bearer <local token>` when `OMNI_LOCAL_TOKEN=1`. The
reads and the person-signed writes are also allowed in remote hub mode with the hub bearer token, the
same way `/api/credits/send|balance|history` are. `POST /api/lpe/quarantine` is loopback only, like
`/api/credits/grant`.

Body caps: profile 8 KB, vibe/progress/vote 4 KB, quarantine 2 KB (413 when over). JSON validation
is strict. Unknown fields, a wrong `schema`, bad types or a repeated query parameter all get a 400.
Person POSTs need `ts` within ±10 minutes of the node clock. They also fall under the per-key write
limit (`OMNI_USER_RATE_MAX`, 30/min).

## Endpoints

### POST /api/lpe/profile
```json
{"action": "lpe_profile", "author_pubkey": "<64 hex>", "previous_hash": "0x000…000", "ts": 1793500000000,
 "payload": {"schema": 1, "handle": "kiro_b", "avatar": "default_m_01",
             "cosmetics": ["head_socket_crown", "chest_socket_cyber_jacket"],
             "ambient_activity": "Training in Plaza Center"},
 "signature": "<128 hex>"}
```
`200 {"status": "SUCCESS", "event_id": "0x…", "ts": …, "author_pubkey": "…", "action": "lpe_profile"}`.
Errors: 400 for schema problems, a ts outside the window, or the node's own key as author. 401 for a
bad signature. 429 for the per-key rate limit.

### GET /api/lpe/profile?pubkey=&lt;hex&gt;[&viewer=&lt;hex&gt;]
```json
{"status": "OK",
 "profile": {"pubkey": "…", "handle": "kiro_b", "handle_display": "@kiro_b.1a2b", "avatar": "default_m_01",
             "cosmetics": ["head_socket_crown"], "ambient_activity": "Training in Plaza Center",
             "equipped_sockets": {"head_socket": "standard_cap", "chest_socket": "guardian_pauldrons",
                                  "aura_socket": "SILVER_PULSE_RINGS"},
             "aura": {"score": 500, "rated": true, "tier": "CITIZEN", "badge": "🛡️ VERIFIED CITIZEN",
                      "aura_color_hex": "#3A86EF", "secondary_glow_hex": "#4CC9F0",
                      "particle_effect": "SILVER_PULSE_RINGS", "glitch_severity": 0.0,
                      "idle_animation": "IDLE_STANDARD_BREATHING", "xp_multiplier": 1.0},
             "quarantine": {"status_label": "ACTIVE_CITIZEN", "assigned_topic": "/cohort_public_feed", "…": "…"},
             "vibe_count": 2, "xp": 350, "plaza_credits": 60, "idle_seconds_claimed": 1800,
             "last_claim_ts": 1793500000000, "profile_ts": 1793400000000, "updated_ts": 1793400000000,
             "event_id": "0x…"},
 "reputation": {"…": "same as GET /api/lpe/reputation"}}
```
Returns 404 when the key has no LPE profile on this node. `xp_multiplier` is shown for clients to
display. It is **not** applied to stored XP (see decisions below).

### GET /api/lpe/plaza[?limit=50&viewer=&lt;hex&gt;]
Every avatar this node knows (its own users plus everyone whose `lpe_profile` reached it over the
mesh), newest profile update first, at most 200 per page.
```json
{"status": "OK", "node_id": "…", "count": 2, "has_more": false, "known_avatars": 2, "vibes_per_day": 3,
 "avatars": [{"pubkey": "…", "handle": "bob_b", "…": "same fields as profile",
              "is_viewer": false, "vibes_left_today": 3,
              "resonance": {"interaction": "STANDARD_AURA_PULSE", "effect": "SILVER_LIGHT_PULSE",
                            "bonus_multiplier": 0.05}}]}
```
`is_viewer`, `vibes_left_today` and `resonance` (the aura engine's proximity resonance between
the viewer and that avatar) appear only when `viewer` is given.

### POST /api/lpe/vibe
Signed `lpe_vibe_boost` with `payload {"schema": 1, "target": "<hex>"}`.
`200 {"status": "SUCCESS", "event_id": "0x…", "bonus_xp": 25, "vibes_left_today": 2, …}`.
An identical retry gets `"result": "ALREADY_EXISTS"`. Errors: 400 for a self-boost. 404
`TARGET_NOT_IN_PLAZA`. 403 when the sender is quarantined on this node. 429 when the daily
per-target limit is used up.

### POST /api/lpe/progress
Signed `lpe_progress_claim` with `payload {"schema": 1, "elapsed_s": 1800}`.
`200 {"status": "SUCCESS", "xp_gained": 300, "plaza_credits_gained": 60, …}`.
Returns 409 when the claim is more than the real time since your last claim:
`{"status": "ERROR", "message": "claim exceeds real elapsed time since your last claim", "max_claimable_s": 5, "anchor_ts": …}`.
Re-sign with at most `max_claimable_s`. Returns 404 when you have no LPE profile yet.

### POST /api/lpe/vote
Signed `lpe_reputation_vote` with `payload {"schema": 1, "target": "<hex>", "rating": 4}`. Returns
400 for a self-vote.

### GET /api/lpe/reputation?pubkey=&lt;hex&gt;
```json
{"status": "OK", "reputation": {"pubkey": "…", "score": 500, "base_score": 500, "rated": true, "tier": "CITIZEN",
 "aura": {"…": "…"}, "votes_received": {"up": 2, "down": 0}, "trust": 0.333333333, "distrust": 0.0,
 "quarantine": {"status_label": "ACTIVE_CITIZEN", "assigned_topic": "/cohort_public_feed",
                "jail_environment": "OPEN_COMMUNITY_PLAZA", "lpe_glitch_severity": 0.0,
                "can_interact_with_public": true, "manual": false, "reason": "", "rehab_bonus": 0,
                "decided_by": "this node", "local_only": true},
 "model": {"name": "eigentrust-lite", "pre_alpha": true, "seeded": false, "graph_keys": 3,
           "iterations": 2, "converged": true}}}
```

### POST /api/lpe/quarantine (loopback only)
`{"target": "<hex>", "decision": "quarantine", "reason": "spam"}`, or `{"target": …, "decision": "release"}`,
or `{"target": …, "decision": "rehab", "clean_relays": 10}`. The node signs it as `lpe_quarantine` and
does not relay it. Returns `200 {"status": "SUCCESS", "event_id": …, "decision": …, "reputation": {…}}`.
A rehab tick for a SLASHED key gets 409. The node's own key gets 400.

## Decisions made in the port

- **Offline accrual is capped at 24 h per claim.** The in-memory engine had no cap. Claims are also
  bounded by real time, on POST and again on replay, so a peer relaying an inflated claim gains
  nothing.
- **The aura `xp_multiplier` is reported, not applied.** Applying it would change past XP every time
  trust moved.
- **Quarantine blocks sending vibes on this node (403).** The block isn't re-applied on replay,
  because quarantine is local and replay has to match across nodes.
- **"Nearby" means known to this node.** The node can't yet map a person key to a physical peer.
- **The trust seeds come from `STATE.paired_keys` when the daemon has them** (pairing work in
  progress), otherwise from the trusted-keys allowlist, otherwise uniform.
- **The first profile timestamp is the progress anchor.** A key that runs its own node could backdate
  its first profile and sync it in. The 24 h per-claim cap limits what that gains.

## Not built yet

- **3D or 2D avatar rendering.** The API returns ids, colours and animation names, and clients draw
  them. Nothing renders in the daemon.
- **OmniMind-driven personas.** No agent animates avatars or picks ambient activities. They're
  whatever the person last signed.
- **Asset generation.** `avatar` and `cosmetics` are ids or URLs chosen by the client. No meshes,
  textures or cosmetics are generated or stored.
- **Positions and congregation.** There's no CRDT for plaza positions and no live presence.
- **P2P topic routing.** `assigned_topic` is a label. The mesh doesn't route jailed keys to a separate
  topic.
- **Measured clean relays.** The owner supplies `clean_relays` for rehab. The daemon doesn't count
  relays itself.
- **Hardware attestation.** The in-memory engine's `stargateHardwareVerified` flag is gone, because
  nothing verifies hardware.
- **Governance votes for SLASHED keys.**
- **Spending plaza credits or cosmetics on the credits ledger.**
