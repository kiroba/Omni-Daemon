"""omni_lpe.py - Live Persona Engine (LPE) for Omni Node (daemon v8.25.0).

Ports the three in-memory engines from kiroba/The-Omniverse core/engines/ onto signed chain events:
  lpe_idle_plaza_engine.py         -> profiles (avatar, handle, cosmetics), idle progression, vibe boosts
  lpe_reputation_aura_engine.py    -> score -> tier / aura / gated sockets / proximity resonance
  lpe_p2p_jail_quarantine_engine.py -> quarantine status, topic routing labels, rehabilitation ticks

Nothing here is a table of truth. Every state change is a signed event in the node's event log, and
the state is rebuilt by replaying those events, the same way the credits ledger is (see
Admin/docs/LPE_API.md):

  lpe_profile          person-signed  avatar / handle / cosmetics / ambient activity (latest wins)
  lpe_vibe_boost       person-signed  sender -> target "like" (+25 XP), capped per sender/target/UTC day
  lpe_progress_claim   person-signed  idle progression; elapsed time bounded by real time since the
                                      author's previous claim (or first profile), on POST and on replay
  lpe_reputation_vote  person-signed  rating -5..+5 of another key (latest per rater/target wins);
                                      the only input to the trust score
  lpe_quarantine       NODE-signed    this node's own local decision about a key. Peers may receive a
                                      copy through sync, but a node only ever applies decisions signed
                                      by its own key, so it is never a verdict on anyone elsewhere.

Replay is deterministic: events are ordered by (signed ts, event_id), never by arrival order, so two
nodes holding the same events compute the same state.

Trust is a small EigenTrust-style power iteration over the vote graph (pre-alpha approximation, see
compute_trust). It is NOT the full EigenTrust protocol: no distributed computation, no score managers,
no protection beyond pre-trusted seeds against sybil clusters when no seeds are configured.

Lazy-loaded by omni_live_daemon_server.py on the first /api/lpe/* request. Standard library only (the
daemon verifies signatures before events reach this module), so it runs under Chaquopy on Android.
"""
import hashlib
import json
import math
import re
import threading
import urllib.parse

SCHEMA = 1

PROFILE = "lpe_profile"
VIBE = "lpe_vibe_boost"
PROGRESS = "lpe_progress_claim"
VOTE = "lpe_reputation_vote"
QUARANTINE = "lpe_quarantine"
PERSON_ACTIONS = (PROFILE, VIBE, PROGRESS, VOTE)
ALL_ACTIONS = PERSON_ACTIONS + (QUARANTINE,)

# Idle plaza (lpe_idle_plaza_engine.py constants, unchanged)
BASE_XP_PER_MINUTE = 10
BASE_CREDIT_RATE_PER_MIN = 2          # "plaza credits": a game counter, NOT Omni Credits (no ledger link)
VIBE_BONUS_XP = 25
# Additions for the networked version
MAX_CLAIM_S = 24 * 3600               # offline accrual cap per claim (the in-memory engine had none)
VIBES_PER_DAY_DEFAULT = 3             # per sender per target per UTC day
TS_WINDOW_MS = 600_000                # signed ts of a POST must be within 10 min of the hub clock
DAY_MS = 86_400_000

# Reputation tiers (lpe_reputation_aura_engine.py thresholds, score range -200..1000)
TIER_PARAGON, TIER_CITIZEN, TIER_DISTRUSTED, TIER_SLASHED = "PARAGON", "CITIZEN", "DISTRUSTED", "SLASHED"
SCORE_MIN, SCORE_MAX, SCORE_UNRATED = -200, 1000, 200
# Quarantine (lpe_p2p_jail_quarantine_engine.py)
SCORE_DISTRUSTED_THRESHOLD = 200
SCORE_SLASHED_THRESHOLD = 0
TOPIC_PUBLIC_FEED = "/cohort_public_feed"
TOPIC_JAIL_QUARANTINE = "/cohort_jail_quarantine"
REHAB_SCORE_PER_RELAY = 5
REHAB_MAX_RELAYS = 1000

# Trust iteration
TRUST_ALPHA = 0.15                    # weight of the pre-trust vector each round
TRUST_EPS = 1e-10
TRUST_MAX_ITER = 200

# Request limits (body caps are enforced by the daemon: LPE_POST_PATHS in omni_live_daemon_server.py)
PLAZA_DEFAULT_LIMIT = 50
PLAZA_MAX_LIMIT = 200
MAX_COSMETICS = 12

_PUBKEY_RE = re.compile(r"[0-9a-f]{64}")
_HANDLE_RE = re.compile(r"[a-z0-9_]{3,20}")
_COSMETIC_RE = re.compile(r"[a-z0-9_]{1,48}")
TEXT_FIELDS = {"avatar": 200, "ambient_activity": 80}

LOCK = threading.Lock()   # replay + limit check + store is one critical section (like CREDITS_LOCK)


class LpeError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra

    def response(self) -> tuple:
        return dict({"status": "ERROR", "message": self.message}, **self.extra), self.status


# --- Payload validation (strict: exact field sets, types, sizes) ----------------------------------
def _int(v, name: str, lo: int, hi: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError(f"'{name}' must be an integer from {lo} to {hi}")
    return v


def _pubkey(v, name: str) -> str:
    if not isinstance(v, str) or not _PUBKEY_RE.fullmatch(v):
        raise ValueError(f"'{name}' must be a 64-char lowercase hex Ed25519 public key")
    return v


def _text(v, name: str, max_len: int) -> str:
    if not isinstance(v, str):
        raise ValueError(f"'{name}' must be a string")
    if any(ch < " " for ch in v) or v != v.strip():
        raise ValueError(f"'{name}' has control characters or surrounding spaces; send it trimmed")
    if len(v) > max_len:
        raise ValueError(f"'{name}' is longer than {max_len} characters")
    return v


def _fields(p, required: set, optional: set = frozenset()):
    if not isinstance(p, dict):
        raise ValueError("payload must be an object")
    unknown, missing = set(p) - required - optional, required - set(p)
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"missing field(s): {', '.join(sorted(missing))}")
    if p["schema"] != SCHEMA or isinstance(p["schema"], bool):
        raise ValueError(f"'schema' must be {SCHEMA}")


def validate_profile(p) -> dict:
    _fields(p, {"schema", "handle"}, {"avatar", "cosmetics", "ambient_activity"})
    if not isinstance(p["handle"], str) or not _HANDLE_RE.fullmatch(p["handle"]):
        raise ValueError("'handle' must be 3-20 chars of a-z 0-9 _ (no @)")
    for k, n in TEXT_FIELDS.items():
        if k in p:
            _text(p[k], k, n)
    if "cosmetics" in p:
        c = p["cosmetics"]
        if not isinstance(c, list) or len(c) > MAX_COSMETICS:
            raise ValueError(f"'cosmetics' must be a list of at most {MAX_COSMETICS} ids")
        if any(not isinstance(x, str) or not _COSMETIC_RE.fullmatch(x) for x in c) or len(set(c)) != len(c):
            raise ValueError("'cosmetics' ids must be unique, 1-48 chars of a-z 0-9 _")
    return p


def validate_vibe(p) -> dict:
    _fields(p, {"schema", "target"})
    _pubkey(p["target"], "target")
    return p


def validate_progress(p) -> dict:
    _fields(p, {"schema", "elapsed_s"})
    _int(p["elapsed_s"], "elapsed_s", 1, MAX_CLAIM_S)
    return p


def validate_vote(p) -> dict:
    _fields(p, {"schema", "target", "rating"})
    _pubkey(p["target"], "target")
    _int(p["rating"], "rating", -5, 5)
    return p


QUARANTINE_DECISIONS = ("quarantine", "release", "rehab")


def validate_quarantine(p) -> dict:
    _fields(p, {"schema", "target", "decision"}, {"reason", "clean_relays"})
    _pubkey(p["target"], "target")
    if p["decision"] not in QUARANTINE_DECISIONS:
        raise ValueError(f"'decision' must be one of: {', '.join(QUARANTINE_DECISIONS)}")
    if "reason" in p:
        _text(p["reason"], "reason", 200)
    if p["decision"] == "rehab":
        if "clean_relays" not in p:
            raise ValueError("'clean_relays' is required for decision 'rehab'")
        _int(p["clean_relays"], "clean_relays", 1, REHAB_MAX_RELAYS)
    elif "clean_relays" in p:
        raise ValueError("'clean_relays' is only for decision 'rehab'")
    return p


VALIDATORS = {PROFILE: validate_profile, VIBE: validate_vibe, PROGRESS: validate_progress,
              VOTE: validate_vote, QUARANTINE: validate_quarantine}


# --- Trust: EigenTrust-style power iteration (pre-alpha approximation) -----------------------------
def compute_trust(votes: dict, seeds=None, alpha: float = TRUST_ALPHA, eps: float = TRUST_EPS,
                  max_iter: int = TRUST_MAX_ITER) -> dict:
    """votes: {(rater, target): rating -5..5}, latest per pair, no self-votes.

    Local trust c[r][t] = positive ratings of r, normalised per rater (EigenTrust drops negatives).
    Global trust t = (1 - alpha) * C^T t + alpha * p, iterated to convergence, where the pre-trust p is
    uniform over the seed keys that appear in the graph (this node's paired / trusted keys) or, if none
    do, uniform over every key in the graph. Raters with no positive votes hand their mass to p.

    Negative ratings are then applied as a separate, trust-weighted distrust mass:
        d[i] = sum_r t[r] * (-min(rating_ri, 0)) / sum_k |rating_rk|
    and the reported score is 500 * n * (t[i] - d[i]) clamped to -200..1000 (n = keys in the graph),
    so a key with exactly average trust and no distrust scores 500 (CITIZEN). A key nobody has rated
    gets the unrated default 200 (CITIZEN), as the in-memory engine did for new peers.

    Returns {"scores", "trust", "distrust", "iterations", "converged", "seeded", "nodes"}.
    """
    nodes = sorted({r for r, _ in votes} | {t for _, t in votes})
    out = {"scores": {}, "trust": {}, "distrust": {}, "iterations": 0, "converged": True,
           "seeded": False, "nodes": len(nodes)}
    if not nodes:
        return out
    n = len(nodes)
    seed_set = [k for k in nodes if seeds and k in seeds]
    out["seeded"] = bool(seed_set)
    base = seed_set or nodes
    p = {k: (1.0 / len(base) if k in set(base) else 0.0) for k in nodes}
    pos, absmass, rated = {}, {}, set()
    for (r, tgt), rating in sorted(votes.items()):
        if rating:
            rated.add(tgt)
            absmass[r] = absmass.get(r, 0) + abs(rating)
        if rating > 0:
            pos.setdefault(r, []).append((tgt, rating))
    t = dict(p)
    converged = False
    for it in range(1, max_iter + 1):
        new = {k: alpha * p[k] for k in nodes}
        for r in nodes:
            mass = (1.0 - alpha) * t[r]
            if not mass:
                continue
            edges = pos.get(r)
            if edges:
                total = float(sum(w for _, w in edges))
                for tgt, w in edges:
                    new[tgt] += mass * w / total
            else:
                for k in base:
                    new[k] += mass / len(base)
        delta = sum(abs(new[k] - t[k]) for k in nodes)
        t = new
        out["iterations"] = it
        if delta < eps:
            converged = True
            break
    out["converged"] = converged
    dist = {k: 0.0 for k in nodes}
    for (r, tgt), rating in votes.items():
        if rating < 0 and absmass.get(r):
            dist[tgt] += t[r] * (-rating) / absmass[r]
    for k in nodes:
        if k in rated:
            s = 500.0 * n * (t[k] - dist[k])
            out["scores"][k] = int(max(SCORE_MIN, min(SCORE_MAX, math.floor(s + 0.5))))
        out["trust"][k] = t[k]
        out["distrust"][k] = dist[k]
    return out


# --- Aura (lpe_reputation_aura_engine.py, unchanged mapping) ---------------------------------------
def reputation_tier(score: float) -> tuple:
    if score >= 800:
        return TIER_PARAGON, {"badge": "\U0001F451 PARAGON", "aura_color_hex": "#00FFCC",
                              "secondary_glow_hex": "#FFD700", "particle_effect": "GOLDEN_CELESTIAL_SPARKS",
                              "glitch_severity": 0.0, "idle_animation": "IDLE_HERO_STANCE", "xp_multiplier": 1.25}
    if score >= 200:
        return TIER_CITIZEN, {"badge": "\U0001F6E1\ufe0f VERIFIED CITIZEN", "aura_color_hex": "#3A86EF",
                              "secondary_glow_hex": "#4CC9F0", "particle_effect": "SILVER_PULSE_RINGS",
                              "glitch_severity": 0.0, "idle_animation": "IDLE_STANDARD_BREATHING",
                              "xp_multiplier": 1.0}
    if score >= 0:
        return TIER_DISTRUSTED, {"badge": "\u26a0\ufe0f LOW-TRUST / FLAGGED", "aura_color_hex": "#FFB703",
                                 "secondary_glow_hex": "#FF0055", "particle_effect": "STATIC_DISTORTION_FLICKER",
                                 "glitch_severity": 0.45, "idle_animation": "IDLE_DEFENSIVE_SHIELD_STANCE",
                                 "xp_multiplier": 0.5}
    return TIER_SLASHED, {"badge": "\U0001F6AB SLASHED / QUARANTINED", "aura_color_hex": "#FF0000",
                          "secondary_glow_hex": "#000000", "particle_effect": "PIXELATED_QUARANTINE_CAGE",
                          "glitch_severity": 0.9, "idle_animation": "IDLE_QUARANTINE_FROZEN", "xp_multiplier": 0.0}


def gated_sockets(score: float, tier_params: dict) -> dict:
    return {"head_socket": "paragon_crown" if score >= 800 else "standard_cap",
            "chest_socket": "guardian_pauldrons" if score >= 500 else "basic_shirt",
            "aura_socket": tier_params["particle_effect"]}


def proximity_resonance(score_a: float, score_b: float) -> dict:
    """calculate_peer_proximity_resonance() for two scores."""
    if score_a < 200 or score_b < 200:
        return {"interaction": "DEFENSIVE_BARRIER", "effect": "DEFENSIVE_GLITCH_SHIELD", "bonus_multiplier": 0.0}
    if score_a >= 800 and score_b >= 800:
        return {"interaction": "GOLDEN_TRUST_BEAM", "effect": "CELESTIAL_HARMONY_RESONANCE", "bonus_multiplier": 0.10}
    return {"interaction": "STANDARD_AURA_PULSE", "effect": "SILVER_LIGHT_PULSE", "bonus_multiplier": 0.05}


def quarantine_status(score: int, manual: bool) -> dict:
    """register_or_update_peer() status for a score (+ this node's manual decision)."""
    if score < SCORE_SLASHED_THRESHOLD:
        label, topic, env, glitch, public = "SLASHED_BANNED", TOPIC_JAIL_QUARANTINE, "MAXIMUM_SECURITY_SHADOW_CELL", 100.0, False
    elif score < SCORE_DISTRUSTED_THRESHOLD or manual:
        label, topic, env, glitch, public = ("DISTRUSTED_QUARANTINED", TOPIC_JAIL_QUARANTINE,
                                             "REHABILITATION_QUARANTINE_PLAZA", 50.0, False)
    else:
        label, topic, env, glitch, public = "ACTIVE_CITIZEN", TOPIC_PUBLIC_FEED, "OPEN_COMMUNITY_PLAZA", 0.0, True
    return {"status_label": label, "assigned_topic": topic, "jail_environment": env,
            "lpe_glitch_severity": glitch, "can_interact_with_public": public}


# --- Replay ---------------------------------------------------------------------------------------
def _sorted_events(events) -> list:
    """Valid LPE events, deduplicated by event_id, in (signed ts, event_id) order."""
    seen, out = set(), []
    for e in events:
        act, eid, ts = e.get("action"), e.get("event_id"), e.get("ts")
        if act not in VALIDATORS or eid in seen or not isinstance(ts, int) or isinstance(ts, bool):
            continue
        if not isinstance(e.get("author_pubkey"), str) or not _PUBKEY_RE.fullmatch(e["author_pubkey"]):
            continue
        try:
            VALIDATORS[act](e.get("payload"))
        except (ValueError, KeyError, TypeError):
            continue          # a peer can relay anything; invalid payloads are ignored on read
        seen.add(eid)
        out.append(e)
    out.sort(key=lambda e: (e["ts"], e["event_id"]))
    return out


def replay(events, node_id: str, seeds=None, vibes_per_day: int = VIBES_PER_DAY_DEFAULT) -> dict:
    """Rebuilds the whole LPE state from signature-verified events (dicts with event_id, action,
    author_pubkey, ts, payload). Pure and deterministic: same event set -> same state, any order."""
    profiles, progress, vibes_rx, vibe_day, votes, quarantine = {}, {}, {}, {}, {}, {}
    ignored = {"vibe_no_target": 0, "vibe_self": 0, "vibe_rate": 0, "claim_no_profile": 0,
               "claim_over_elapsed": 0, "vote_self": 0, "quarantine_foreign": 0}
    for e in _sorted_events(events):
        act, author, ts, p = e["action"], e["author_pubkey"], e["ts"], e["payload"]
        if act == PROFILE:
            prev = profiles.get(author)
            profiles[author] = {"handle": p["handle"], "avatar": p.get("avatar", ""),
                                "cosmetics": list(p.get("cosmetics", [])),
                                "ambient_activity": p.get("ambient_activity", ""),
                                "first_ts": prev["first_ts"] if prev else ts, "updated_ts": ts,
                                "event_id": e["event_id"]}
            progress.setdefault(author, {"anchor_ts": ts, "claimed_s": 0, "xp": 0, "plaza_credits": 0,
                                         "claims": 0, "last_claim_ts": None})
        elif act == PROGRESS:
            st = progress.get(author)
            if st is None:
                ignored["claim_no_profile"] += 1
                continue
            real_s = (ts - st["anchor_ts"]) // 1000
            if real_s <= 0:
                ignored["claim_over_elapsed"] += 1
                continue
            elapsed = min(p["elapsed_s"], real_s)
            if elapsed < p["elapsed_s"]:
                ignored["claim_over_elapsed"] += 1   # clamped: a client can't out-claim real time
            minutes = elapsed / 60.0
            st["xp"] += int(minutes * BASE_XP_PER_MINUTE)
            st["plaza_credits"] += int(minutes * BASE_CREDIT_RATE_PER_MIN)
            st["claimed_s"] += elapsed
            st["claims"] += 1
            st["anchor_ts"] = ts
            st["last_claim_ts"] = ts
        elif act == VIBE:
            tgt = p["target"]
            if tgt == author:
                ignored["vibe_self"] += 1
                continue
            prof = profiles.get(tgt)
            if prof is None or prof["first_ts"] > ts:
                ignored["vibe_no_target"] += 1      # TARGET_NOT_IN_PLAZA
                continue
            key = (author, tgt, ts // DAY_MS)
            if vibe_day.get(key, 0) >= vibes_per_day:
                ignored["vibe_rate"] += 1
                continue
            vibe_day[key] = vibe_day.get(key, 0) + 1
            rx = vibes_rx.setdefault(tgt, {"count": 0, "senders": {}})
            rx["count"] += 1
            rx["senders"][author] = rx["senders"].get(author, 0) + 1
        elif act == VOTE:
            if p["target"] == author:
                ignored["vote_self"] += 1
                continue
            votes[(author, p["target"])] = p["rating"]       # sorted order: latest signed vote wins
        elif act == QUARANTINE:
            if author != node_id:
                ignored["quarantine_foreign"] += 1          # another node's local decision: not ours
                continue
            q = quarantine.setdefault(p["target"], {"manual": False, "rehab_bonus": 0, "reason": "",
                                                    "decided_ts": None})
            if p["decision"] == "rehab":
                q["rehab_bonus"] += REHAB_SCORE_PER_RELAY * p["clean_relays"]
            else:
                q["manual"] = p["decision"] == "quarantine"
                q["reason"] = p.get("reason", "")
                q["decided_ts"] = ts
    trust = compute_trust(votes, seeds)
    return {"profiles": profiles, "progress": progress, "vibes": vibes_rx, "vibe_day": vibe_day,
            "votes": votes, "quarantine": quarantine, "trust": trust, "ignored": ignored,
            "vibes_per_day": vibes_per_day}


# --- Views ----------------------------------------------------------------------------------------
def score_of(state: dict, pk: str) -> tuple:
    """(score, rated, base_score). Rehab bonus applies unless the base score is SLASHED (the in-memory
    engine required a governance vote for slashed peers)."""
    base = state["trust"]["scores"].get(pk)
    rated = base is not None
    base = SCORE_UNRATED if base is None else base
    bonus = state["quarantine"].get(pk, {}).get("rehab_bonus", 0)
    score = base if base < SCORE_SLASHED_THRESHOLD else min(SCORE_MAX, base + bonus)
    return score, rated, base


def reputation_view(state: dict, pk: str) -> dict:
    score, rated, base = score_of(state, pk)
    tier, params = reputation_tier(score)
    up = sum(1 for (r, t), v in state["votes"].items() if t == pk and v > 0)
    down = sum(1 for (r, t), v in state["votes"].items() if t == pk and v < 0)
    q = state["quarantine"].get(pk, {})
    tr = state["trust"]
    return {"pubkey": pk, "score": score, "base_score": base, "rated": rated, "tier": tier, "aura": params,
            "votes_received": {"up": up, "down": down},
            "trust": round(tr["trust"].get(pk, 0.0), 9), "distrust": round(tr["distrust"].get(pk, 0.0), 9),
            "quarantine": dict(quarantine_status(score, bool(q.get("manual"))),
                               manual=bool(q.get("manual")), reason=q.get("reason", ""),
                               rehab_bonus=q.get("rehab_bonus", 0), decided_by="this node", local_only=True),
            "model": {"name": "eigentrust-lite", "pre_alpha": True, "seeded": tr["seeded"],
                      "graph_keys": tr["nodes"], "iterations": tr["iterations"], "converged": tr["converged"]}}


def handle_display(handle: str, pk: str) -> str:
    return f"@{handle}.{pk[:4]}"


def avatar_view(state: dict, pk: str, viewer: str = None, today: int = None) -> dict:
    prof = state["profiles"][pk]
    prog = state["progress"].get(pk, {})
    rx = state["vibes"].get(pk, {"count": 0})
    score, rated, _ = score_of(state, pk)
    tier, params = reputation_tier(score)
    q = state["quarantine"].get(pk, {})
    v = {"pubkey": pk, "handle": prof["handle"], "handle_display": handle_display(prof["handle"], pk),
         "avatar": prof["avatar"], "cosmetics": prof["cosmetics"], "ambient_activity": prof["ambient_activity"],
         "equipped_sockets": gated_sockets(score, params),
         "aura": {"score": score, "rated": rated, "tier": tier, **params},
         "quarantine": quarantine_status(score, bool(q.get("manual"))),
         "vibe_count": rx["count"],
         "xp": prog.get("xp", 0) + VIBE_BONUS_XP * rx["count"],
         "plaza_credits": prog.get("plaza_credits", 0), "idle_seconds_claimed": prog.get("claimed_s", 0),
         "last_claim_ts": prog.get("last_claim_ts"), "profile_ts": prof["first_ts"],
         "updated_ts": prof["updated_ts"], "event_id": prof["event_id"]}
    if viewer:
        v["is_viewer"] = viewer == pk
        if viewer != pk and today is not None:
            sent = state["vibe_day"].get((viewer, pk, today), 0)
            v["vibes_left_today"] = max(0, state["vibes_per_day"] - sent)
            vscore, _, _ = score_of(state, viewer)
            v["resonance"] = proximity_resonance(vscore, score)
    return v


# --- Endpoints ------------------------------------------------------------------------------------
# ctx (built by the daemon): node_id, events() -> verified LPE events, parse_signed(data, validators) ->
# event (raises ValueError -> 400, PermissionError -> 401), store_person(data, validators) -> (resp,
# status), store_node(action, payload) -> (resp, status), seeds() -> set or None, now_ms() -> int,
# vibes_per_day -> int.

def _query(query: str, allowed: set) -> dict:
    if len(query or "") > 2048:
        raise ValueError("query string too long")
    params = urllib.parse.parse_qs(query or "", keep_blank_values=True, strict_parsing=False)
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    if any(len(v) > 1 for v in params.values()):
        raise ValueError("each parameter may appear once")
    return {k: v[0] for k, v in params.items()}


def _state(ctx) -> dict:
    return replay(ctx.events(), ctx.node_id, ctx.seeds(), ctx.vibes_per_day)


def _err(e: Exception) -> tuple:
    if isinstance(e, LpeError):
        return e.response()
    if isinstance(e, PermissionError):
        return {"status": "ERROR", "message": str(e)}, 401
    return {"status": "ERROR", "message": str(e)}, 400


def get_profile(ctx, query: str) -> tuple:
    try:
        q = _query(query, {"pubkey", "viewer"})
        pk = _pubkey(q.get("pubkey", ""), "pubkey")
        viewer = _pubkey(q["viewer"], "viewer") if "viewer" in q else None
    except ValueError as e:
        return _err(e)
    st = _state(ctx)
    if pk not in st["profiles"]:
        return {"status": "ERROR", "message": "no LPE profile for that key on this node", "pubkey": pk}, 404
    return {"status": "OK", "profile": avatar_view(st, pk, viewer, ctx.now_ms() // DAY_MS),
            "reputation": reputation_view(st, pk)}, 200


def get_plaza(ctx, query: str) -> tuple:
    try:
        q = _query(query, {"limit", "viewer"})
        limit = PLAZA_DEFAULT_LIMIT
        if "limit" in q:
            if not re.fullmatch(r"[0-9]{1,4}", q["limit"]):
                raise ValueError("'limit' must be a positive integer")
            limit = max(1, min(int(q["limit"]), PLAZA_MAX_LIMIT))
        viewer = _pubkey(q["viewer"], "viewer") if "viewer" in q else None
    except ValueError as e:
        return _err(e)
    st = _state(ctx)
    today = ctx.now_ms() // DAY_MS
    keys = sorted(st["profiles"], key=lambda k: (-st["profiles"][k]["updated_ts"], k))
    avatars = [avatar_view(st, k, viewer, today) for k in keys[:limit]]
    return {"status": "OK", "node_id": ctx.node_id, "count": len(avatars), "has_more": len(keys) > limit,
            "known_avatars": len(keys), "vibes_per_day": st["vibes_per_day"], "avatars": avatars}, 200


def get_reputation(ctx, query: str) -> tuple:
    try:
        q = _query(query, {"pubkey"})
        pk = _pubkey(q.get("pubkey", ""), "pubkey")
    except ValueError as e:
        return _err(e)
    return {"status": "OK", "reputation": reputation_view(_state(ctx), pk)}, 200


def _signed(ctx, data, action: str) -> dict:
    ev = ctx.parse_signed(data, {action: VALIDATORS[action]})   # schema + Ed25519 signature
    now = ctx.now_ms()
    if abs(ev["ts"] - now) > TS_WINDOW_MS:
        raise LpeError(400, f"'ts' must be within {TS_WINDOW_MS // 60000} minutes of the node clock",
                       node_ts=now)
    return ev


def event_id_of(ev: dict) -> str:
    """Content id of a ts-stamped event: sha256 over omni_signing.canonical_event_bytes (same bytes)."""
    struct = {k: ev[k] for k in ("action", "author_pubkey", "payload", "previous_hash", "ts")}
    return "0x" + hashlib.sha256(json.dumps(struct, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _already_stored(ctx, ev: dict) -> bool:
    eid = event_id_of(ev)
    return any(e.get("event_id") == eid for e in ctx.events())


def _store(ctx, data, action: str, extra: dict = None) -> tuple:
    resp, status = ctx.store_person(data, {action: VALIDATORS[action]})
    if status == 200 and extra:
        resp = dict(resp, **extra)
    return resp, status


def post_profile(ctx, data) -> tuple:
    try:
        _signed(ctx, data, PROFILE)
    except Exception as e:
        return _err(e)
    return _store(ctx, data, PROFILE)


def post_vote(ctx, data) -> tuple:
    try:
        ev = _signed(ctx, data, VOTE)
        if ev["payload"]["target"] == ev["author_pubkey"]:
            raise LpeError(400, "you can't rate your own key")
    except Exception as e:
        return _err(e)
    return _store(ctx, data, VOTE)


def post_vibe(ctx, data) -> tuple:
    with LOCK:
        try:
            ev = _signed(ctx, data, VIBE)
            if _already_stored(ctx, ev):
                return ctx.store_person(data, {VIBE: VALIDATORS[VIBE]})   # identical retry: ALREADY_EXISTS
            sender, tgt = ev["author_pubkey"], ev["payload"]["target"]
            if tgt == sender:
                raise LpeError(400, "you can't vibe-boost yourself")
            st = _state(ctx)
            if tgt not in st["profiles"] or st["profiles"][tgt]["first_ts"] > ev["ts"]:
                raise LpeError(404, "TARGET_NOT_IN_PLAZA: that key has no LPE profile on this node")
            score, _, _ = score_of(st, sender)
            if not quarantine_status(score, st["quarantine"].get(sender, {}).get("manual", False))["can_interact_with_public"]:
                raise LpeError(403, "sender is quarantined on this node")
            used = st["vibe_day"].get((sender, tgt, ev["ts"] // DAY_MS), 0)
            if used >= st["vibes_per_day"]:
                raise LpeError(429, f"rate limited: {st['vibes_per_day']} vibe boosts per sender per target "
                                    "per UTC day", vibes_left_today=0)
        except Exception as e:
            return _err(e)
        return _store(ctx, data, VIBE, {"bonus_xp": VIBE_BONUS_XP,
                                        "vibes_left_today": st["vibes_per_day"] - used - 1})


def post_progress(ctx, data) -> tuple:
    with LOCK:
        try:
            ev = _signed(ctx, data, PROGRESS)
            if _already_stored(ctx, ev):
                return ctx.store_person(data, {PROGRESS: VALIDATORS[PROGRESS]})
            st = _state(ctx)
            prog = st["progress"].get(ev["author_pubkey"])
            if prog is None:
                raise LpeError(404, "post an LPE profile before claiming progress")
            real_s = (ev["ts"] - prog["anchor_ts"]) // 1000
            claim = ev["payload"]["elapsed_s"]
            if claim > real_s:
                raise LpeError(409, "claim exceeds real elapsed time since your last claim",
                               max_claimable_s=max(0, min(real_s, MAX_CLAIM_S)), anchor_ts=prog["anchor_ts"])
        except Exception as e:
            return _err(e)
        minutes = claim / 60.0
        return _store(ctx, data, PROGRESS, {"xp_gained": int(minutes * BASE_XP_PER_MINUTE),
                                            "plaza_credits_gained": int(minutes * BASE_CREDIT_RATE_PER_MIN)})


def post_quarantine(ctx, data) -> tuple:
    """Loopback-only owner decision. The NODE signs it; it only ever changes this node's view."""
    try:
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        payload = dict(data, schema=SCHEMA)
        validate_quarantine(payload)
        if payload["target"] == ctx.node_id:
            raise ValueError("a node can't quarantine its own key")
        if payload["decision"] == "rehab":
            st = _state(ctx)
            score, _, base = score_of(st, payload["target"])
            if base < SCORE_SLASHED_THRESHOLD:
                raise LpeError(409, "PERMANENT_BAN: a slashed key can't earn rehab ticks on this node")
    except Exception as e:
        return _err(e)
    with LOCK:
        resp, status = ctx.store_node(QUARANTINE, payload)
    if status == 200:
        st = _state(ctx)
        resp = {"status": "SUCCESS", "event_id": resp.get("event_id"), "decision": payload["decision"],
                "reputation": reputation_view(st, payload["target"])}
    return resp, status


GET_ROUTES = {"/api/lpe/profile": get_profile, "/api/lpe/plaza": get_plaza, "/api/lpe/reputation": get_reputation}
POST_ROUTES = {"/api/lpe/profile": post_profile, "/api/lpe/vibe": post_vibe, "/api/lpe/progress": post_progress,
               "/api/lpe/vote": post_vote, "/api/lpe/quarantine": post_quarantine}
