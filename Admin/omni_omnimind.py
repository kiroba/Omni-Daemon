"""omni_omnimind.py - OmniMind world library for Omni Node (daemon v8.26.0).

First version of the "world library": the shared, signed, versioned store of lore, places, NPCs,
quests, items and asset references that OmniMind builds as the world grows. Like the LPE module
(omni_lpe.py) nothing here is a table of truth. Every change is a signed event in the node's normal
event log, and the state is rebuilt by replaying those events in (signed ts, event_id) order, so two
nodes holding the same events (and the same owner list) compute the same world.

Events (all payloads carry "schema": 1):

  omnimind_world_entry  publisher-signed  lore / place / npc / quest / item. Versioned: version n > 1
                                          must name the event id of version n-1 as "parent", so every
                                          entry is a hash-linked history. Counted only when signed by a
                                          publisher (an OMNI_OMNIMIND_OWNERS key or this node).
  omnimind_asset_ref    publisher-signed  sha256 + kind + prompt + model + license + source node of a
                                          file in the source node's local CAS dir (file transfer
                                          between nodes is NOT built yet).
  omnimind_request      person-signed     "need an NPC / lore / asset X for context Y".
  omnimind_proposal     node-signed       a full node's draft answer to a request (generator, safety
                                          result, retrieval ids). Never part of the world by itself.
  omnimind_review       publisher-signed  reject a proposal, or keep / remove a flagged world entry.
  omnimind_flag         person-signed     anyone's report of an entry, asset, proposal or request.
  omnimind_signal       person-signed     opt-in play signal: pick / finish / skip / lose_to / report.
                                          No free text.
  omnimind_capability   node-signed       role (mini/full), llm, gpu. Latest per node wins.
  omnimind_checkpoint   publisher-signed  root hash over the world heads up to a signed ts.

Roles: OMNI_OMNIMIND_ROLE=full (desktop) fulfils requests; anything else is "mini" (phones), which
stores, relays, serves and signs, but never generates.

Generators (full nodes): the built-in deterministic template/remix generator (always on, no ML, includes
the Zodiac procedural NPC generator) and an optional OpenAI-compatible local LLM (Ollama / llama.cpp)
at OMNI_OMNIMIND_LLM_URL. Heavy image generation never runs here: a request with needs_gpu waits until
some node advertises gpu capability, and the GPU worker itself is not built yet.

Human Feed Policy: every generated NPC and asset, and every world entry, carries
"feed_injectable": false. The daemon refuses to store any non-omnimind event whose payload carries that
tag, and its human event feed leaves omnimind events out (see the hook block in
omni_live_daemon_server.py and Admin/docs/OMNIMIND_WORLD.md).

Lazy-loaded by omni_live_daemon_server.py on the first /api/omnimind/* (or /api/v1/omnimind/*) request.
Standard library only (the daemon verifies signatures before events reach this module), so it runs
under Chaquopy on Android.
"""
import colorsys
import hashlib
import json
import math
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = 1
GENERATOR_VERSION = "0.1.0"

WORLD = "omnimind_world_entry"
ASSET = "omnimind_asset_ref"
REQUEST = "omnimind_request"
PROPOSAL = "omnimind_proposal"
REVIEW = "omnimind_review"
FLAG = "omnimind_flag"
SIGNAL = "omnimind_signal"
CAPABILITY = "omnimind_capability"
CHECKPOINT = "omnimind_checkpoint"
ALL_ACTIONS = (WORLD, ASSET, REQUEST, PROPOSAL, REVIEW, FLAG, SIGNAL, CAPABILITY, CHECKPOINT)
PUBLISHER_ACTIONS = (WORLD, ASSET, REVIEW, CHECKPOINT)

ENTRY_KINDS = ("lore", "place", "npc", "quest", "item")
REQUEST_KINDS = ENTRY_KINDS + ("asset",)
ASSET_KINDS = ("image", "svg", "model3d", "audio", "composite", "palette")
LICENSES = ("CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "omni-generated", "owner-only")
RARITIES = ("common", "uncommon", "rare", "epic", "legendary")
SIGNAL_KINDS = ("pick", "finish", "skip", "lose_to", "report")
SIGNAL_CONSENT = "omnimind-signals-v0"
FLAG_REASONS = ("unsafe", "offensive", "copyright", "spam", "broken", "off_canon", "other")
REVIEW_DECISIONS = ("reject", "keep", "remove")
REVIEW_REASONS = ("unsafe", "low_quality", "duplicate", "off_canon", "copyright", "other")
ROLES = ("mini", "full")

TS_WINDOW_MS = 600_000          # signed ts of a POST must be within 10 min of the node clock
DAY_MS = 86_400_000
LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200
FLAG_HIDE_DEFAULT = 3           # distinct flaggers that hide an entry until an owner keeps it
MAX_PER_PASS_DEFAULT = 5        # requests a full node fulfils per worker pass
CHECKPOINT_EVERY_DEFAULT = 25   # new world heads between this node's checkpoints
ZERO_HASH = "0x" + "0" * 64

_PUBKEY_RE = re.compile(r"[0-9a-f]{64}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_EVENT_ID_RE = re.compile(r"0x[0-9a-f]{64}")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,47}")
_TAG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_ASSET_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_HEX_COLOR_RE = re.compile(r"#[0-9a-f]{6}")

LOCK = threading.RLock()   # worker pass / approve / reject: replay + checks + store is one section


class OmniMindError(Exception):
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


def _bool(v, name: str) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"'{name}' must be true or false")
    return v


def _text(v, name: str, max_len: int, min_len: int = 0, newlines: bool = False) -> str:
    if not isinstance(v, str):
        raise ValueError(f"'{name}' must be a string")
    bad = [ch for ch in v if ch < " " and not (newlines and ch == "\n")]
    if bad or v != v.strip():
        raise ValueError(f"'{name}' has control characters or surrounding spaces; send it trimmed")
    if not min_len <= len(v) <= max_len:
        raise ValueError(f"'{name}' must be {min_len}-{max_len} characters")
    return v


def _slug(v, name: str) -> str:
    if not isinstance(v, str) or not _SLUG_RE.fullmatch(v):
        raise ValueError(f"'{name}' must be 2-48 chars of a-z 0-9 _ - (starting with a letter or digit)")
    return v


def _pubkey(v, name: str) -> str:
    if not isinstance(v, str) or not _PUBKEY_RE.fullmatch(v):
        raise ValueError(f"'{name}' must be a 64-char lowercase hex Ed25519 public key")
    return v


def _event_id(v, name: str) -> str:
    if not isinstance(v, str) or not _EVENT_ID_RE.fullmatch(v):
        raise ValueError(f"'{name}' must be an event id (0x + 64 lowercase hex)")
    return v


def _enum(v, name: str, options) -> str:
    if v not in options or not isinstance(v, str):
        raise ValueError(f"'{name}' must be one of: {', '.join(options)}")
    return v


def _list(v, name: str, max_n: int, item, unique: bool = True) -> list:
    if not isinstance(v, list) or len(v) > max_n:
        raise ValueError(f"'{name}' must be a list of at most {max_n} items")
    out = [item(x, f"{name}[]") for x in v]
    if unique and len({json.dumps(x, sort_keys=True) for x in out}) != len(out):
        raise ValueError(f"'{name}' items must be unique")
    return out


def _tag(v, name: str) -> str:
    if not isinstance(v, str) or not _TAG_RE.fullmatch(v):
        raise ValueError(f"'{name}' must be 1-32 chars of a-z 0-9 _ -")
    return v


def _sha(v, name: str) -> str:
    if not isinstance(v, str) or not _SHA_RE.fullmatch(v):
        raise ValueError(f"'{name}' must be a sha256 (64 lowercase hex)")
    return v


def _fields(p, required: set, optional: set = frozenset()):
    if not isinstance(p, dict):
        raise ValueError("payload must be an object")
    unknown, missing = set(p) - required - optional, required - set(p)
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"missing field(s): {', '.join(sorted(missing))}")
    if "schema" in required and (p["schema"] != SCHEMA or isinstance(p["schema"], bool)):
        raise ValueError(f"'schema' must be {SCHEMA}")


def _not_feed(p):
    if p.get("feed_injectable") is not False:
        raise ValueError("'feed_injectable' must be false (Human Feed Policy: generated content never "
                         "enters the human feed)")


def _layer(v, name: str) -> dict:
    _fields(v, {"layer", "asset_id", "z", "tint", "opacity"})
    _tag(v["layer"], f"{name}.layer")
    if not isinstance(v["asset_id"], str) or not _ASSET_ID_RE.fullmatch(v["asset_id"]):
        raise ValueError(f"'{name}.asset_id' must be 1-64 chars of a-z 0-9 _ . -")
    _int(v["z"], f"{name}.z", 0, 15)
    if not isinstance(v["tint"], str) or not _HEX_COLOR_RE.fullmatch(v["tint"]):
        raise ValueError(f"'{name}.tint' must be #rrggbb (lowercase)")
    _int(v["opacity"], f"{name}.opacity", 0, 100)     # percent; integers only, so every language signs the same bytes
    return v


def _stats(v, name: str) -> dict:
    _fields(v, {"power", "agility", "wit", "resolve"})
    for k in v:
        _int(v[k], f"{name}.{k}", 1, 10)
    return v


ATTRS = {
    "lore": {"era": lambda v, n: _text(v, n, 40, 1), "canon": _bool},
    "place": {"region": lambda v, n: _text(v, n, 60, 1), "biome": _tag,
              "coords": lambda v, n: _list(v, n, 2, lambda x, m: _int(x, m, -1_000_000, 1_000_000), unique=False)},
    "npc": {"persona": _tag, "archetype": _tag, "traits": lambda v, n: _list(v, n, 8, _tag),
            "difficulty": lambda v, n: _int(v, n, 1, 10), "home": _slug, "trait_hash": _sha,
            "layers": lambda v, n: _list(v, n, 8, _layer), "stats": _stats,
            "seed": lambda v, n: _int(v, n, 0, 2 ** 53)},
    "quest": {"giver": _slug, "steps": lambda v, n: _list(v, n, 10, lambda x, m: _text(x, m, 160, 1), unique=False),
              "reward": _slug, "difficulty": lambda v, n: _int(v, n, 1, 10), "place": _slug},
    "item": {"rarity": lambda v, n: _enum(v, n, RARITIES), "slot": _tag, "power": lambda v, n: _int(v, n, 0, 100)},
}
SOURCE_FIELDS = {"request_id": _event_id, "proposal_id": _event_id,
                 "generator": lambda v, n: _text(v, n, 40, 1), "model": lambda v, n: _text(v, n, 80, 1)}


def _source(v, name: str = "source") -> dict:
    _fields(v, set(), set(SOURCE_FIELDS))
    for k, fn in SOURCE_FIELDS.items():
        if k in v:
            fn(v[k], f"{name}.{k}")
    return v


def validate_world_entry(p) -> dict:
    _fields(p, {"schema", "kind", "id", "version", "parent", "title", "summary", "feed_injectable"},
            {"body", "tags", "refs", "assets", "attrs", "source"})
    _not_feed(p)
    kind = _enum(p["kind"], "kind", ENTRY_KINDS)
    _slug(p["id"], "id")
    v = _int(p["version"], "version", 1, 1_000_000)
    if v == 1 and p["parent"] is not None:
        raise ValueError("'parent' must be null for version 1")
    if v > 1:
        _event_id(p["parent"], "parent")
    _text(p["title"], "title", 80, 1)
    _text(p["summary"], "summary", 500, 1, newlines=True)
    if "body" in p:
        _text(p["body"], "body", 4000, 1, newlines=True)
    if "tags" in p:
        _list(p["tags"], "tags", 12, _tag)
    if "refs" in p:
        _list(p["refs"], "refs", 16, _slug)
    if "assets" in p:
        _list(p["assets"], "assets", 8, _sha)
    if "attrs" in p:
        a = p["attrs"]
        allowed = ATTRS[kind]
        _fields(a, set(), set(allowed))
        for k, fn in allowed.items():
            if k in a:
                fn(a[k], f"attrs.{k}")
    if "source" in p:
        _source(p["source"])
    return p


def validate_asset_ref(p) -> dict:
    _fields(p, {"schema", "sha256", "kind", "size", "prompt", "model", "license", "source_node", "feed_injectable"},
            {"name", "mime", "parents", "tags", "source"})
    _not_feed(p)
    _sha(p["sha256"], "sha256")
    _enum(p["kind"], "kind", ASSET_KINDS)
    _int(p["size"], "size", 1, 512 * 1024 * 1024)
    _text(p["prompt"], "prompt", 500, 0, newlines=True)
    _text(p["model"], "model", 80, 1)
    _enum(p["license"], "license", LICENSES)
    _pubkey(p["source_node"], "source_node")
    if "name" in p:
        _slug(p["name"], "name")
    if "mime" in p:
        if not isinstance(p["mime"], str) or not re.fullmatch(r"[a-z0-9.+-]{1,40}/[a-z0-9.+-]{1,60}", p["mime"]):
            raise ValueError("'mime' must look like type/subtype")
    if "parents" in p:
        _list(p["parents"], "parents", 8, _sha)
    if "tags" in p:
        _list(p["tags"], "tags", 12, _tag)
    if "source" in p:
        _source(p["source"])
    return p


def validate_request(p) -> dict:
    _fields(p, {"schema", "kind", "need"}, {"context", "tags", "extends", "needs_gpu", "sign", "seed", "asset_kind"})
    kind = _enum(p["kind"], "kind", REQUEST_KINDS)
    _text(p["need"], "need", 200, 1)
    if "context" in p:
        _text(p["context"], "context", 500, 1, newlines=True)
    if "tags" in p:
        _list(p["tags"], "tags", 8, _tag)
    if "extends" in p:
        if kind == "asset":
            raise ValueError("'extends' is for world entries, not assets")
        _slug(p["extends"], "extends")
    if "needs_gpu" in p:
        _bool(p["needs_gpu"], "needs_gpu")
    if "sign" in p:
        if kind != "npc":
            raise ValueError("'sign' is only for kind 'npc'")
        _enum(p["sign"], "sign", ZODIAC_SIGNS)
    if "seed" in p:
        _int(p["seed"], "seed", 0, 2 ** 53)
    if "asset_kind" in p:
        if kind != "asset":
            raise ValueError("'asset_kind' is only for kind 'asset'")
        _enum(p["asset_kind"], "asset_kind", ASSET_KINDS)
    return p


def validate_proposal(p) -> dict:
    _fields(p, {"schema", "request_id", "publish_action", "draft", "generator", "safety", "feed_injectable"},
            {"retrieval", "notes"})
    _not_feed(p)
    _event_id(p["request_id"], "request_id")
    act = _enum(p["publish_action"], "publish_action", (WORLD, ASSET))
    PUBLISH_VALIDATORS[act](p["draft"])
    g = p["generator"]
    _fields(g, {"name", "model", "version"})
    _text(g["name"], "generator.name", 40, 1)
    _text(g["model"], "generator.model", 80, 1)
    _text(g["version"], "generator.version", 20, 1)
    s = p["safety"]
    _fields(s, {"clean", "hits"})
    _bool(s["clean"], "safety.clean")
    _list(s["hits"], "safety.hits", 20, lambda v, n: _text(v, n, 60, 1))
    if s["clean"] != (not s["hits"]):
        raise ValueError("'safety.clean' must be true exactly when there are no hits")
    if "retrieval" in p:
        _list(p["retrieval"], "retrieval", 8, _slug)
    if "notes" in p:
        _list(p["notes"], "notes", 8, lambda v, n: _text(v, n, 200, 1))
    return p


def validate_review(p) -> dict:
    _fields(p, {"schema", "target", "decision"}, {"reason"})
    _event_id(p["target"], "target")
    _enum(p["decision"], "decision", REVIEW_DECISIONS)
    if "reason" in p:
        _enum(p["reason"], "reason", REVIEW_REASONS)
    return p


def validate_flag(p) -> dict:
    _fields(p, {"schema", "target", "reason"})
    _event_id(p["target"], "target")
    _enum(p["reason"], "reason", FLAG_REASONS)
    return p


def validate_signal(p) -> dict:
    _fields(p, {"schema", "target", "signal", "consent"})
    _slug(p["target"], "target")
    _enum(p["signal"], "signal", SIGNAL_KINDS)
    if p["consent"] != SIGNAL_CONSENT:
        raise ValueError(f"'consent' must be \"{SIGNAL_CONSENT}\" (signals are opt-in)")
    return p


def validate_capability(p) -> dict:
    _fields(p, {"schema", "role", "llm", "gpu"}, {"generators"})
    _enum(p["role"], "role", ROLES)
    _bool(p["llm"], "llm")
    _bool(p["gpu"], "gpu")
    if "generators" in p:
        _list(p["generators"], "generators", 8, _tag)
    return p


def validate_checkpoint(p) -> dict:
    _fields(p, {"schema", "root", "count", "upto_ts"})
    _sha(p["root"], "root")
    _int(p["count"], "count", 0, 10_000_000)
    _int(p["upto_ts"], "upto_ts", 0, 2 ** 53)
    return p


PUBLISH_VALIDATORS = {WORLD: validate_world_entry, ASSET: validate_asset_ref}
VALIDATORS = {WORLD: validate_world_entry, ASSET: validate_asset_ref, REQUEST: validate_request,
              PROPOSAL: validate_proposal, REVIEW: validate_review, FLAG: validate_flag,
              SIGNAL: validate_signal, CAPABILITY: validate_capability, CHECKPOINT: validate_checkpoint}


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Guardrails: safety pass ------------------------------------------------------------------------
# A small built-in blocklist, matched on normalised words (lowercase, common leetspeak folded, punctuation
# collapsed). It is a floor, not a moderation system: extend it with OMNI_OMNIMIND_BLOCKLIST_FILE (one
# term per line, "#" comments). Generated text must also stay inside the schema limits, carry no links
# and no contact details.
BLOCKLIST = {
    "sexual_minors": ("child porn", "cp link", "underage sex", "loli porn", "minor nude"),
    "sexual": ("porn", "porno", "nsfw", "nude", "nudes", "hentai", "xxx", "rape", "molest"),
    "self_harm": ("kill yourself", "kys", "suicide method", "how to self harm", "cut yourself"),
    "violent_extremism": ("heil hitler", "white power", "gas the", "ethnic cleansing", "school shooting",
                          "mass shooting", "pipe bomb", "how to make a bomb", "build a bomb"),
    "hate": ("subhuman", "untermensch", "race war"),
    "drugs": ("cook meth", "buy cocaine", "buy heroin", "fentanyl recipe"),
    "scam": ("seed phrase", "private key", "send me crypto", "wire me", "gift card code"),
}
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
_LINK_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|io|gg|xyz|ru|cn)\b)", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![0-9])(\+?[0-9][0-9 ().-]{8,}[0-9])(?![0-9])")
_extra_terms = None


def _norm(text: str) -> str:
    """Lowercase, leetspeak folded, punctuation collapsed, and spelled-out runs joined ("n.s.f.w" -> "nsfw")."""
    words = re.sub(r"[^a-z0-9]+", " ", text.lower().translate(_LEET)).split()
    out, run = [], ""
    for w in words:
        if len(w) == 1:
            run += w
            continue
        if run:
            out.append(run)
            run = ""
        out.append(w)
    if run:
        out.append(run)
    return " " + " ".join(out) + " "


def extra_blocklist() -> tuple:
    global _extra_terms
    if _extra_terms is None:
        terms = []
        path = os.environ.get("OMNI_OMNIMIND_BLOCKLIST_FILE", "").strip()
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.split("#", 1)[0].strip()
                        if line:
                            terms.append(_norm(line).strip())
            except OSError:
                terms = []
        _extra_terms = tuple(t for t in terms if t)
    return _extra_terms


def safety_hits(text: str) -> list:
    """Sorted unique hit codes for one piece of text ([] = clean)."""
    hits = set()
    n = _norm(text)
    for cat, terms in BLOCKLIST.items():
        if any(f" {t} " in n for t in terms):
            hits.add(f"blocklist:{cat}")
    if any(f" {t} " in n for t in extra_blocklist()):
        hits.add("blocklist:custom")
    if _LINK_RE.search(text):
        hits.add("link")
    if _EMAIL_RE.search(text):
        hits.add("pii:email")
    if _PHONE_RE.search(text):
        hits.add("pii:phone")
    return sorted(hits)


def _texts_of(obj, out: list, depth: int = 0):
    if depth > 6:
        return
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in sorted(obj.items()):
            if k not in ("source", "trait_hash", "sha256", "parent", "source_node", "assets", "parents"):
                _texts_of(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _texts_of(v, out, depth + 1)


def safety_pass(action: str, payload: dict) -> dict:
    """Blocklist + links + contact details + the schema's own limits. {"clean": bool, "hits": [...]}."""
    hits = set()
    try:
        PUBLISH_VALIDATORS.get(action, VALIDATORS.get(action))(payload)
    except (ValueError, KeyError, TypeError) as e:
        hits.add("schema:" + str(e)[:50])
    texts = []
    _texts_of(payload, texts)
    for t in texts:
        hits.update(safety_hits(t))
    hits = sorted(hits)[:20]
    return {"clean": not hits, "hits": hits}


# --- Zodiac procedural NPC generator (deterministic, no ML) -----------------------------------------
ZODIAC_SIGNS = ("aries", "taurus", "gemini", "cancer", "leo", "virgo", "libra", "scorpio", "sagittarius",
                "capricorn", "aquarius", "pisces")
ZODIAC = {
    "aries": ("fire", "cardinal", "vanguard", ("bold", "restless", "direct"), "#d7263d", (8, 7, 4, 6)),
    "taurus": ("earth", "fixed", "warden", ("steady", "patient", "stubborn"), "#6a994e", (7, 3, 5, 9)),
    "gemini": ("air", "mutable", "trickster", ("curious", "witty", "fickle"), "#f4d35e", (4, 7, 9, 3)),
    "cancer": ("water", "cardinal", "keeper", ("loyal", "guarded", "tender"), "#9fb8ad", (5, 4, 6, 8)),
    "leo": ("fire", "fixed", "champion", ("proud", "generous", "dramatic"), "#f19c1f", (8, 5, 5, 7)),
    "virgo": ("earth", "mutable", "artificer", ("precise", "modest", "critical"), "#8d7b68", (4, 6, 9, 6)),
    "libra": ("air", "cardinal", "envoy", ("fair", "charming", "indecisive"), "#e8a0bf", (4, 6, 8, 5)),
    "scorpio": ("water", "fixed", "shade", ("intense", "secretive", "relentless"), "#5c1a33", (7, 6, 7, 8)),
    "sagittarius": ("fire", "mutable", "ranger", ("free", "blunt", "hopeful"), "#7b2cbf", (6, 9, 6, 5)),
    "capricorn": ("earth", "cardinal", "strategist", ("disciplined", "dry", "ambitious"), "#3d405b", (6, 4, 8, 9)),
    "aquarius": ("air", "fixed", "visionary", ("inventive", "aloof", "humane"), "#2ec4b6", (4, 5, 9, 6)),
    "pisces": ("water", "mutable", "mystic", ("dreamy", "kind", "elusive"), "#5e60ce", (3, 6, 8, 6)),
}
LAYER_ORDER = ("body", "palette", "outfit", "accessory", "aura")
_LAYER_OPTIONS = {
    "body": {"fire": ("body_lithe", "body_broad", "body_tall"), "earth": ("body_stout", "body_broad", "body_tall"),
             "air": ("body_lithe", "body_slim", "body_tall"), "water": ("body_slim", "body_lithe", "body_stout")},
    "palette": {"fire": ("pal_ember", "pal_sunforge", "pal_cinder"), "earth": ("pal_moss", "pal_clay", "pal_granite"),
                "air": ("pal_sky", "pal_dawn", "pal_storm"), "water": ("pal_tide", "pal_abyss", "pal_mist")},
    "outfit": {"cardinal": ("fit_plate", "fit_coat", "fit_uniform"), "fixed": ("fit_robe", "fit_mail", "fit_cloak"),
               "mutable": ("fit_leathers", "fit_wraps", "fit_patchwork")},
    "accessory": ("acc_horns", "acc_circlet", "acc_mask", "acc_lantern", "acc_scarf", "acc_bracers",
                  "acc_quiver", "acc_tome", "acc_pendant", "acc_goggles", "acc_shell", "acc_feather"),
    "aura": {"fire": ("aura_flare", "aura_halo"), "earth": ("aura_dust", "aura_bloom"),
             "air": ("aura_breeze", "aura_spark"), "water": ("aura_ripple", "aura_fog")},
}
_SYLLABLES = ("ka", "ri", "zo", "mel", "tha", "vin", "or", "sae", "lun", "dra", "quo", "bel", "nyx", "ira",
              "cor", "fen", "ael", "mor", "sil", "tav")


def _pick(seed_key: str, options):
    h = int.from_bytes(hashlib.sha256(seed_key.encode("utf-8")).digest()[:8], "big")
    return options[h % len(options)]


def _shift_hex(hex_color: str, seed_key: str, max_deg: float = 40.0) -> str:
    """Deterministic hue/lightness nudge of #rrggbb."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    raw = int.from_bytes(hashlib.sha256(seed_key.encode("utf-8")).digest()[:4], "big")
    h = (h + ((raw % 2001) / 1000.0 - 1.0) * max_deg / 360.0) % 1.0
    l = min(0.85, max(0.15, l + (((raw >> 12) % 201) / 1000.0 - 0.1)))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def zodiac_sign(value) -> str:
    """'Scorpio' / 'scorpio' -> 'scorpio'. Raises ValueError for anything else."""
    s = str(value or "").strip().lower()
    if s not in ZODIAC:
        raise ValueError("'sign' must be one of: " + ", ".join(x.capitalize() for x in ZODIAC_SIGNS))
    return s


def zodiac_npc(sign: str, seed: int = 0) -> dict:
    """NPC manifest for one sign + seed: a normalized 5-layer stack (body, palette, outfit, accessory,
    aura; fixed order, z 0..4, lowercase ids, #rrggbb tints) and trait_hash = sha256 over the canonical
    JSON of the traits. Pure: the same (sign, seed) always gives byte-identical output."""
    sign = zodiac_sign(sign)
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 2 ** 53:
        raise ValueError("'seed' must be an integer from 0 to 2^53")
    element, modality, archetype, temperament, base_color, base_stats = ZODIAC[sign]
    key = f"{sign}|{seed}"
    stats = {}
    for i, name in enumerate(("power", "agility", "wit", "resolve")):
        nudge = int(_pick(f"{key}|stat|{name}", ("-1", "0", "0", "1")))
        stats[name] = max(1, min(10, base_stats[i] + nudge))
    traits = {"sign": sign, "element": element, "modality": modality, "archetype": archetype,
              "temperament": list(temperament), "stats": stats,
              "quirk": _pick(f"{key}|quirk", ("hums_old_songs", "collects_keys", "counts_stars", "never_sits",
                                               "talks_to_birds", "keeps_ledgers", "laughs_at_storms",
                                               "trades_riddles"))}
    trait_hash = sha256_hex(canonical(traits))
    layers = []
    for z, layer in enumerate(LAYER_ORDER):
        opts = _LAYER_OPTIONS[layer]
        if isinstance(opts, dict):
            opts = opts[element] if layer != "outfit" else opts[modality]
        asset_id = _pick(f"{key}|{layer}", opts)
        tint = _shift_hex(base_color, f"{key}|{layer}|tint")
        opacity = 80 if layer == "aura" else 100
        layers.append({"layer": layer, "asset_id": asset_id, "z": z, "tint": tint, "opacity": opacity})
    name = "".join(_pick(f"{key}|name|{i}", _SYLLABLES) for i in range(2 + int(_pick(f"{key}|len", "01"))))
    manifest = {"schema": SCHEMA, "kind": "npc_manifest", "generator": "omnimind-zodiac",
                "generator_version": GENERATOR_VERSION, "sign": sign, "seed": seed,
                "name": name.capitalize(), "traits": traits, "trait_hash": trait_hash, "layers": layers,
                "feed_injectable": False}
    manifest["manifest_hash"] = sha256_hex(canonical(manifest))
    return manifest


def layers_normalized(layers) -> bool:
    try:
        _list(layers, "layers", 8, _layer)
    except ValueError:
        return False
    return [x["layer"] for x in layers] == list(LAYER_ORDER) and [x["z"] for x in layers] == list(range(5))


# --- Replay -------------------------------------------------------------------------------------------
def _valid_events(events) -> list:
    """Valid omnimind events, deduplicated by event_id, in (signed ts, event_id) order."""
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


def _new_stats() -> dict:
    return {k: 0 for k in SIGNAL_KINDS}


def replay(events, node_id: str, owners=(), flag_hide: int = FLAG_HIDE_DEFAULT) -> dict:
    """Rebuilds the world library from signature-verified events. Pure and deterministic: the same
    events, node id and owner list give the same state, in any order and with duplicates."""
    publishers = set(owners or ()) | {node_id}
    entries, by_event, history = {}, {}, {}
    assets, requests, proposals = {}, {}, {}
    reviews, flags, capabilities, checkpoints = {}, {}, {}, []
    signals, signal_seen = {}, set()
    hidden = {}       # entry id -> {"removed": bool, "flaggers": set, "owner_flag": bool}
    ignored = {"not_publisher": 0, "version_conflict": 0, "asset_duplicate": 0, "flag_duplicate": 0,
               "signal_duplicate": 0, "proposal_orphan": 0}
    for e in _valid_events(events):
        act, author, ts, p, eid = e["action"], e["author_pubkey"], e["ts"], e["payload"], e["event_id"]
        if act in PUBLISHER_ACTIONS and author not in publishers:
            ignored["not_publisher"] += 1
            continue
        if act == WORLD:
            head = entries.get(p["id"])
            ok = (p["version"] == 1 and head is None) or (
                head is not None and p["version"] == head["version"] + 1 and p["parent"] == head["event_id"]
                and p["kind"] == head["kind"])
            if not ok:
                ignored["version_conflict"] += 1
                continue
            rec = {"event_id": eid, "author": author, "ts": ts, "id": p["id"], "kind": p["kind"],
                   "version": p["version"], "parent": p["parent"], "payload": p}
            entries[p["id"]] = rec
            by_event[eid] = rec
            history.setdefault(p["id"], []).append(rec)
        elif act == ASSET:
            if p["sha256"] in assets:
                ignored["asset_duplicate"] += 1
                continue
            assets[p["sha256"]] = {"event_id": eid, "author": author, "ts": ts, "payload": p}
            by_event[eid] = {"asset": p["sha256"]}
        elif act == REQUEST:
            requests[eid] = {"event_id": eid, "author": author, "ts": ts, "payload": p}
        elif act == PROPOSAL:
            proposals[eid] = {"event_id": eid, "author": author, "ts": ts, "payload": p}
        elif act == REVIEW:
            reviews[eid] = {"event_id": eid, "author": author, "ts": ts, "payload": p}
            target = by_event.get(p["target"])
            if target and "id" in target:
                h = hidden.setdefault(target["id"], {"removed": False, "flaggers": set(), "owner_flag": False})
                if p["decision"] == "remove":
                    h["removed"] = True
                elif p["decision"] == "keep":
                    h.update(removed=False, flaggers=set(), owner_flag=False)
        elif act == FLAG:
            key = (author, p["target"])
            if key in flags:
                ignored["flag_duplicate"] += 1
                continue
            flags[key] = {"event_id": eid, "ts": ts, "reason": p["reason"], "target": p["target"], "author": author}
            target = by_event.get(p["target"])
            if target and "id" in target:
                h = hidden.setdefault(target["id"], {"removed": False, "flaggers": set(), "owner_flag": False})
                h["flaggers"].add(author)
                if author in publishers:
                    h["owner_flag"] = True
        elif act == SIGNAL:
            key = (author, p["target"], p["signal"], ts // DAY_MS)
            if key in signal_seen:
                ignored["signal_duplicate"] += 1     # one per person, target, kind and UTC day
                continue
            signal_seen.add(key)
            st = signals.setdefault(p["target"], {"counts": _new_stats(), "people": set()})
            st["counts"][p["signal"]] += 1
            st["people"].add(author)
        elif act == CAPABILITY:
            capabilities[author] = {"ts": ts, "event_id": eid, **{k: p[k] for k in ("role", "llm", "gpu")},
                                    "generators": list(p.get("generators", []))}
        elif act == CHECKPOINT:
            checkpoints.append({"event_id": eid, "author": author, "ts": ts, **{k: p[k] for k in ("root", "count", "upto_ts")}})

    # Links that don't depend on order: proposals -> requests, publications -> proposals.
    published, rejected = {}, {}
    for rec in list(history.values()):
        for r in rec:
            pid = r["payload"].get("source", {}).get("proposal_id")
            if pid:
                published.setdefault(pid, r["event_id"])
    for a in assets.values():
        pid = a["payload"].get("source", {}).get("proposal_id")
        if pid:
            published.setdefault(pid, a["event_id"])
    for rv in sorted(reviews.values(), key=lambda r: (r["ts"], r["event_id"])):
        if rv["payload"]["decision"] == "reject":
            rejected.setdefault(rv["payload"]["target"], rv["event_id"])
    for pid in list(proposals):
        if proposals[pid]["payload"]["request_id"] not in requests:
            ignored["proposal_orphan"] += 1          # its request hasn't reached this node (yet)
    flag_counts = {}
    for (author, target), f in flags.items():
        flag_counts.setdefault(target, set()).add(author)
    hide = {}
    for eid_, h in hidden.items():
        reason = None
        if h["removed"]:
            reason = "removed_by_owner"
        elif h["owner_flag"]:
            reason = "flagged_by_owner"
        elif len(h["flaggers"]) >= flag_hide:
            reason = "flagged"
        if reason:
            hide[eid_] = reason
    gpu_nodes = sorted(k for k, c in capabilities.items() if c["gpu"])
    full_nodes = sorted(k for k, c in capabilities.items() if c["role"] == "full")
    return {"entries": entries, "by_event": by_event, "history": history, "assets": assets,
            "requests": requests, "proposals": proposals, "reviews": reviews, "flags": flags,
            "flag_counts": flag_counts, "signals": signals, "capabilities": capabilities,
            "checkpoints": checkpoints, "published": published, "rejected": rejected, "hidden": hide,
            "gpu_nodes": gpu_nodes, "full_nodes": full_nodes, "publishers": sorted(publishers),
            "ignored": ignored, "flag_hide": flag_hide}


def world_root(state: dict, upto_ts: int = None) -> tuple:
    """(root sha256, count) over the world heads as of upto_ts (inclusive; None = now)."""
    heads = []
    for eid_, hist in sorted(state["history"].items()):
        vis = [r for r in hist if upto_ts is None or r["ts"] <= upto_ts]
        if vis:
            heads.append([eid_, vis[-1]["version"], vis[-1]["event_id"]])
    return sha256_hex(canonical(heads)), len(heads)


def signal_view(state: dict, target: str) -> dict:
    st = state["signals"].get(target)
    if not st:
        return {"counts": _new_stats(), "people": 0, "pick_rate": None, "finish_rate": None,
                "skip_rate": None, "loss_rate": None}
    c = st["counts"]
    total = sum(c.values())
    shown = c["pick"] + c["skip"]
    return {"counts": dict(c), "people": len(st["people"]),
            "pick_rate": round(c["pick"] / shown, 4) if shown else None,
            "finish_rate": round(c["finish"] / c["pick"], 4) if c["pick"] else None,
            "skip_rate": round(c["skip"] / shown, 4) if shown else None,
            "loss_rate": round(c["lose_to"] / total, 4) if total else None}


def tuned_difficulty(state: dict, target: str, current: int) -> tuple:
    """Signals -> difficulty (v0 learning: counting, not training). Players losing to an NPC most of the
    time lowers it, players almost never losing raises it; needs at least 5 signals from 3 people."""
    st = state["signals"].get(target)
    if not st or sum(st["counts"].values()) < 5 or len(st["people"]) < 3:
        return current, None
    v = signal_view(state, target)
    if v["loss_rate"] is not None and v["loss_rate"] >= 0.6:
        return max(1, current - 1), f"loss_rate {v['loss_rate']} >= 0.6: easier"
    if v["loss_rate"] is not None and v["loss_rate"] <= 0.15 and st["counts"]["finish"] >= 3:
        return min(10, current + 1), f"loss_rate {v['loss_rate']} <= 0.15: harder"
    return current, None


def vary_candidates(state: dict, limit: int = 5) -> list:
    """NPCs players skip most (>= 3 people, skip_rate >= 0.5): the ones worth a new variant first."""
    out = []
    for eid_, rec in state["entries"].items():
        if rec["kind"] != "npc" or eid_ in state["hidden"]:
            continue
        st = state["signals"].get(eid_)
        if not st or len(st["people"]) < 3:
            continue
        v = signal_view(state, eid_)
        if v["skip_rate"] is not None and v["skip_rate"] >= 0.5:
            out.append((-v["skip_rate"], eid_))
    return [e for _, e in sorted(out)[:limit]]


def request_status(state: dict, rid: str) -> tuple:
    """(status, [proposal ids]). open / proposed / published / rejected / waiting_gpu / gpu_available /
    flagged."""
    req = state["requests"][rid]
    props = sorted((p for p in state["proposals"].values() if p["payload"]["request_id"] == rid),
                   key=lambda p: (p["ts"], p["event_id"]))
    pids = [p["event_id"] for p in props]
    if any(pid in state["published"] for pid in pids):
        return "published", pids
    flaggers = state["flag_counts"].get(rid, set())
    if len(flaggers) >= state["flag_hide"] or flaggers & set(state["publishers"]):
        return "flagged", pids
    open_ = [pid for pid in pids if pid not in state["rejected"]]
    if open_:
        return "proposed", pids
    if pids:
        return "rejected", pids
    if req["payload"].get("needs_gpu"):
        return ("gpu_available" if state["gpu_nodes"] else "waiting_gpu"), pids
    return "open", pids


# --- Retrieval: TF-IDF over world heads (stdlib) ------------------------------------------------------
_STOP = frozenset("a an the and or of to in on for with at by from is are was were be this that it its as "
                  "into their them they he she his her who what which need needs want some any".split())


def tokens(text: str) -> list:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 1 and t not in _STOP]


def _doc_tokens(p: dict) -> list:
    toks = tokens(p["title"]) * 2 + tokens(" ".join(p.get("tags", []))) * 2 + tokens(p["summary"])
    toks += tokens(p.get("body", "")) + tokens(p["id"].replace("-", " ").replace("_", " "))
    a = p.get("attrs", {})
    toks += tokens(" ".join(str(a.get(k, "")) for k in ("persona", "archetype", "biome", "region", "era")))
    toks += tokens(" ".join(a.get("traits", [])))
    return toks


def retrieve(state: dict, query: str, kind: str = None, limit: int = 8, include_hidden: bool = False) -> list:
    """[(score, entry id)] best first. Cosine similarity of TF-IDF vectors; deterministic ties by id."""
    docs = {k: _doc_tokens(r["payload"]) for k, r in state["entries"].items()
            if (include_hidden or k not in state["hidden"]) and (kind is None or r["kind"] == kind)}
    q = tokens(query)
    if not docs or not q:
        return []
    n = len(docs)
    df = {}
    for toks in docs.values():
        for t in set(toks):
            df[t] = df.get(t, 0) + 1

    def vec(toks):
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        return {t: (1 + math.log(c)) * (math.log((n + 1) / (df.get(t, 0) + 1)) + 1) for t, c in tf.items()}

    qv = vec(q)
    qn = math.sqrt(sum(v * v for v in qv.values())) or 1.0
    scored = []
    for k, toks in docs.items():
        dv = vec(toks)
        dot = sum(qv[t] * dv.get(t, 0.0) for t in qv)
        if dot <= 0:
            continue
        dn = math.sqrt(sum(v * v for v in dv.values())) or 1.0
        scored.append((round(dot / (qn * dn), 6), k))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[:limit]


# --- Generators -----------------------------------------------------------------------------------------
def _slugify(text: str, suffix: str) -> str:
    base = "-".join(re.findall(r"[a-z0-9]+", text.lower()))[:36].strip("-") or "entry"
    if not base[0].isalnum():
        base = "e" + base
    return f"{base}-{suffix}"[:48]


def _request_query(req: dict) -> str:
    p = req["payload"]
    return " ".join([p["need"], p.get("context", ""), " ".join(p.get("tags", []))])


def _base_draft(state: dict, req: dict, kind: str, title: str) -> dict:
    """id / version / parent: a new entry, or the next version of 'extends'."""
    p = req["payload"]
    ext = p.get("extends")
    if ext and ext in state["entries"] and state["entries"][ext]["kind"] == kind:
        head = state["entries"][ext]
        return {"schema": SCHEMA, "kind": kind, "id": ext, "version": head["version"] + 1,
                "parent": head["event_id"], "feed_injectable": False}
    return {"schema": SCHEMA, "kind": kind, "id": _slugify(title, req["event_id"][2:8]), "version": 1,
            "parent": None, "feed_injectable": False}


def _seed_of(req: dict) -> int:
    p = req["payload"]
    if "seed" in p:
        return p["seed"]
    return int(req["event_id"][2:15], 16)


def _choose(key: str, options):
    return _pick(key, tuple(options)) if options else None


def _remix_sources(state: dict, req: dict, kind: str = None, limit: int = 4) -> list:
    """Retrieved entry ids; among equally relevant ones, finished-often entries come first and
    skipped-often ones last (signals feed the remix)."""
    hits = retrieve(state, _request_query(req), kind, limit=8)

    def pref(item):
        score, eid_ = item
        v = signal_view(state, eid_)
        bonus = (v["finish_rate"] or 0) * 0.05 - (v["skip_rate"] or 0) * 0.05
        return (-(score + bonus), eid_)

    return [e for _, e in sorted(hits, key=pref)[:limit]]


_LORE_FRAMES = ("Before the {a} there was {b}, and the elders still argue which came first.",
                "Travellers who pass {a} speak of {b} in low voices.",
                "The songs of {a} remember {b} differently from the books.",
                "Nobody alive has seen {b}, yet every map of {a} leaves room for it.")
_PLACE_BIOMES = ("forest", "coast", "desert", "tundra", "marsh", "highlands", "caverns", "city")
_QUEST_VERBS = ("Recover", "Escort", "Map", "Deliver", "Defend", "Unmask", "Rebuild", "Silence")
_ITEM_FORMS = ("charm", "blade", "lantern", "map", "ring", "drum", "cloak", "compass")


def template_generate(state: dict, req: dict, node_id: str, cas_dir: str = None) -> tuple:
    """Built-in deterministic generator. (publish_action, draft, retrieval ids, notes). No ML: it remixes
    world entries and offline personas (Zodiac), and recolours or combines asset refs."""
    p = req["payload"]
    kind = p["kind"]
    seed = _seed_of(req)
    key = f"{req['event_id']}|{seed}"
    src = {"request_id": req["event_id"], "generator": "template-remix", "model": f"omnimind-remix-{GENERATOR_VERSION}"}
    notes = []
    if kind == "asset":
        return asset_remix(state, req, node_id, cas_dir, seed)
    sources = _remix_sources(state, req, None)
    titles = [state["entries"][s]["payload"]["title"] for s in sources]
    need = p["need"]
    tags = list(dict.fromkeys(p.get("tags", [])))[:8]
    if kind == "npc":
        sign = p.get("sign")
        ext = p.get("extends")
        if not sign and ext in state["entries"]:
            sign = state["entries"][ext]["payload"].get("attrs", {}).get("persona")
        if sign not in ZODIAC:
            sign = _pick(f"{key}|sign", ZODIAC_SIGNS)
        m = zodiac_npc(sign, seed)
        title = f"{m['name']} the {m['traits']['archetype'].capitalize()}"
        draft = _base_draft(state, req, "npc", title)
        difficulty = max(1, min(10, round(sum(m["traits"]["stats"].values()) / 4)))
        if draft["version"] > 1:
            prev = state["entries"][draft["id"]]["payload"].get("attrs", {})
            title = state["entries"][draft["id"]]["payload"]["title"]
            difficulty, why = tuned_difficulty(state, draft["id"], prev.get("difficulty", difficulty))
            if why:
                notes.append("signals: " + why)
        homes = [s for s in sources if state["entries"][s]["kind"] == "place"]
        summary = (f"A {', '.join(m['traits']['temperament'])} {m['traits']['archetype']} born under "
                   f"{sign.capitalize()} ({m['traits']['element']}, {m['traits']['modality']}). "
                   f"Known for one habit: {m['traits']['quirk'].replace('_', ' ')}.")
        if titles:
            summary += f" Drawn from {', '.join(titles[:2])}."
        attrs = {"persona": sign, "archetype": m["traits"]["archetype"],
                 "traits": list(m["traits"]["temperament"]), "difficulty": difficulty,
                 "trait_hash": m["trait_hash"], "layers": m["layers"], "stats": m["traits"]["stats"], "seed": seed}
        if homes:
            attrs["home"] = homes[0]
        draft.update(title=title, summary=summary[:500], tags=list(dict.fromkeys(tags + [sign, "npc"]))[:12],
                     attrs=attrs, source=src)
        if sources:
            draft["refs"] = sources[:16]
        return WORLD, draft, sources, notes
    a = titles[0] if titles else need
    b = titles[1] if len(titles) > 1 else need
    if kind == "lore":
        title = need[:80].rstrip(" .")
        body = _choose(f"{key}|frame", _LORE_FRAMES).format(a=a, b=b)
        if len(titles) > 2:
            body += f" Some tie it to {titles[2]}."
        draft = _base_draft(state, req, "lore", title)
        draft.update(title=title, summary=f"{need.rstrip('.')}. {body}"[:500], body=body,
                     attrs={"era": _choose(f"{key}|era", ("First Age", "Long Quiet", "Age of Tides", "Now")),
                            "canon": False})
    elif kind == "place":
        title = need[:80].rstrip(" .")
        biome = _choose(f"{key}|biome", _PLACE_BIOMES)
        draft = _base_draft(state, req, "place", title)
        draft.update(title=title, summary=f"A {biome} place: {need.rstrip('.')}. Near {a}."[:500],
                     attrs={"biome": biome, "region": (a[:60] or "Unmapped")})
    elif kind == "quest":
        npcs = [s for s in sources if state["entries"][s]["kind"] == "npc"] or \
               [k for k in sorted(state["entries"]) if state["entries"][k]["kind"] == "npc"
                and k not in state["hidden"]][:4]
        places = [s for s in sources if state["entries"][s]["kind"] == "place"]
        verb = _choose(f"{key}|verb", _QUEST_VERBS)
        title = f"{verb}: {need}"[:80].rstrip(" .")
        steps = [f"Hear the request{(' from ' + state['entries'][npcs[0]]['payload']['title']) if npcs else ''}.",
                 f"{verb} what was asked: {need[:100]}.",
                 f"Return{(' to ' + state['entries'][places[0]]['payload']['title']) if places else ''} and report."]
        difficulty = 3 + int(_pick(f"{key}|diff", "0123"))
        if npcs:
            difficulty, why = tuned_difficulty(state, npcs[0], difficulty)
            if why:
                notes.append("signals: " + why)
        attrs = {"steps": [s[:160] for s in steps], "difficulty": difficulty}
        if npcs:
            attrs["giver"] = npcs[0]
        if places:
            attrs["place"] = places[0]
        draft = _base_draft(state, req, "quest", title)
        draft.update(title=title, summary=f"{need.rstrip('.')}. A quest built from {a}."[:500], attrs=attrs)
    else:   # item
        form = _choose(f"{key}|form", _ITEM_FORMS)
        rarity = _choose(f"{key}|rarity", RARITIES[:4])
        title = need[:80].rstrip(" .")
        draft = _base_draft(state, req, "item", title)
        draft.update(title=title, summary=f"A {rarity} {form}. {need.rstrip('.')}. Said to echo {a}."[:500],
                     attrs={"rarity": rarity, "power": 10 + RARITIES.index(rarity) * 20 + int(_pick(f"{key}|p", "0123456789"))})
    draft["tags"] = list(dict.fromkeys(tags + [kind]))[:12]
    if sources:
        draft["refs"] = sources[:16]
    draft["source"] = src
    return WORLD, draft, sources, notes


def cas_path(cas_dir: str, sha: str) -> str:
    return os.path.join(cas_dir, sha[:2], sha)


def cas_put(cas_dir: str, data: bytes) -> str:
    sha = sha256_hex(data)
    path = cas_path(cas_dir, sha)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    return sha


def cas_has(cas_dir: str, sha: str) -> bool:
    return bool(cas_dir) and os.path.isfile(cas_path(cas_dir, sha))


def recolour_svg(svg: bytes, key: str) -> bytes:
    text = svg.decode("utf-8")

    def sub(m):
        c = m.group(0).lower()
        if len(c) == 4:
            c = "#" + "".join(ch * 2 for ch in c[1:])
        return _shift_hex(c, f"{key}|{c}", 90.0)

    return re.sub(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", sub, text).encode("utf-8")


def asset_remix(state: dict, req: dict, node_id: str, cas_dir: str, seed: int) -> tuple:
    """Recolour an SVG asset ref, or combine refs into a composite manifest, or (no assets yet) emit a
    palette. Writes the new file to this node's CAS dir and proposes an omnimind_asset_ref."""
    if not cas_dir:
        raise OmniMindError(503, "no CAS dir on this node")
    p = req["payload"]
    key = f"{req['event_id']}|{seed}"
    q = set(tokens(_request_query(req)))
    visible = [(sha, a) for sha, a in sorted(state["assets"].items())]
    ranked = sorted(visible, key=lambda x: (-len(q & set(tokens(" ".join(x[1]["payload"].get("tags", []))
                                                                  + " " + x[1]["payload"]["prompt"]))), x[0]))
    want = p.get("asset_kind")
    svgs = [(s, a) for s, a in ranked if a["payload"]["kind"] == "svg" and cas_has(cas_dir, s)]
    parents, notes = [], []
    if svgs and want in (None, "svg"):
        sha0, a0 = svgs[0]
        with open(cas_path(cas_dir, sha0), "rb") as f:
            data = recolour_svg(f.read(), key)
        kind, mime, parents = "svg", "image/svg+xml", [sha0]
        notes.append(f"recoloured {sha0[:12]}")
    elif ranked and want in (None, "composite"):
        picks = [s for s, _ in ranked[:3]]
        layers = [{"sha256": s, "z": i, "tint": _shift_hex("#888888", f"{key}|{s}")} for i, s in enumerate(picks)]
        data = canonical({"schema": SCHEMA, "kind": "composite", "layers": layers, "feed_injectable": False})
        kind, mime, parents = "composite", "application/json", picks
        notes.append("combined " + ", ".join(s[:12] for s in picks))
    else:
        sign = _pick(f"{key}|sign", ZODIAC_SIGNS)
        base = ZODIAC[sign][4]
        colours = [_shift_hex(base, f"{key}|pal|{i}", 120.0) for i in range(5)]
        data = canonical({"schema": SCHEMA, "kind": "palette", "sign": sign, "colors": colours,
                          "feed_injectable": False})
        kind, mime = "palette", "application/json"
        notes.append(f"palette from {sign}")
    sha = cas_put(cas_dir, data)
    draft = {"schema": SCHEMA, "sha256": sha, "kind": kind, "size": len(data), "mime": mime,
             "prompt": p["need"], "model": f"omnimind-remix-{GENERATOR_VERSION}", "license": "omni-generated",
             "source_node": node_id, "feed_injectable": False,
             "tags": list(dict.fromkeys(p.get("tags", []) + ["generated"]))[:12],
             "source": {"request_id": req["event_id"], "generator": "template-remix",
                        "model": f"omnimind-remix-{GENERATOR_VERSION}"}}
    if parents:
        draft["parents"] = parents
    return ASSET, draft, [], notes


# --- Optional local LLM (OpenAI-compatible: Ollama / llama.cpp) ---------------------------------------
LLM_KEYS = ("title", "summary", "body", "tags", "attrs")


def _llm_url(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return u + "/chat/completions"
    return u + "/v1/chat/completions"


def _post_json(url: str, body: dict, timeout: float) -> dict:
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never proxy loopback
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read(4 * 1024 * 1024).decode("utf-8"))


def extract_json(text: str):
    """Mechanical repair: code fences, prose around the object, a trailing comma. Returns dict or None."""
    if not isinstance(text, str):
        return None
    t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.I)
    start = t.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = t[start:i + 1]
                for cand in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
                    try:
                        obj = json.loads(cand)
                        return obj if isinstance(obj, dict) else None
                    except ValueError:
                        continue
                return None
    return None


def _repair_fields(obj: dict, kind: str) -> dict:
    out = {k: obj[k] for k in LLM_KEYS if k in obj}
    for k in ("title", "summary", "body"):
        if isinstance(out.get(k), str):
            out[k] = re.sub(r"[\x00-\x09\x0b-\x1f]", " ", out[k]).strip()
            if not out[k]:
                out.pop(k)
    if "tags" in out:
        tags = out["tags"] if isinstance(out["tags"], list) else []
        norm = []
        for t in tags:
            s = "-".join(re.findall(r"[a-z0-9]+", str(t).lower()))[:32]
            if s and s not in norm:
                norm.append(s)
        out["tags"] = norm[:12]
    if "attrs" in out:
        if isinstance(out["attrs"], dict):
            out["attrs"] = {k: v for k, v in out["attrs"].items() if k in ATTRS[kind] and k not in ("layers", "trait_hash", "seed")}
        else:
            out.pop("attrs")
    return out


def _llm_prompt(state: dict, req: dict, kind: str, sources: list) -> list:
    p = req["payload"]
    ctx = [{"id": s, "kind": state["entries"][s]["kind"], "title": state["entries"][s]["payload"]["title"],
            "summary": state["entries"][s]["payload"]["summary"][:300],
            "tags": state["entries"][s]["payload"].get("tags", [])} for s in sources]
    attr_keys = ", ".join(k for k in ATTRS[kind] if k not in ("layers", "trait_hash", "seed"))
    system = ("You write content for a shared fantasy game world. Stay consistent with the world entries "
              "you are given. Family-friendly, no links, no real people, no contact details. Reply with ONE "
              "JSON object and nothing else, with keys: title (<=80 chars), summary (<=500 chars), body "
              f"(optional, <=1500 chars), tags (list of short lowercase words), attrs (object; allowed keys "
              f"for a {kind}: {attr_keys}).")
    hints = {}
    for s in sources:
        v = signal_view(state, s)
        if sum(v["counts"].values()):
            hints[s] = {k: v[k] for k in ("finish_rate", "skip_rate", "loss_rate")}
    user = {"kind": kind, "need": p["need"], "context": p.get("context", ""), "tags": p.get("tags", []),
            "world_entries": ctx, "player_signals": hints}
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]


def llm_generate(state: dict, req: dict, url: str, model: str, timeout: float = 60.0) -> tuple:
    """(WORLD, draft, retrieval, notes) or raises OmniMindError(502) when the model's output can't be
    repaired into a valid entry after one retry (reject). Assets never go to the LLM."""
    kind = req["payload"]["kind"]
    if kind == "asset":
        raise OmniMindError(400, "assets are not generated by the text LLM")
    sources = _remix_sources(state, req, None, limit=6)
    messages = _llm_prompt(state, req, kind, sources)
    title_hint = req["payload"]["need"]
    last_err = "no output"
    for attempt in range(2):
        try:
            resp = _post_json(_llm_url(url), {"model": model, "messages": messages, "temperature": 0.7,
                                              "stream": False, "response_format": {"type": "json_object"}}, timeout)
            content = resp["choices"][0]["message"]["content"]
        except (OSError, ValueError, KeyError, IndexError, TypeError, urllib.error.URLError) as e:
            raise OmniMindError(502, f"LLM call failed: {type(e).__name__}: {str(e)[:120]}")
        obj = extract_json(content)
        if obj is None:
            last_err = "output was not a JSON object"
        else:
            fields = _repair_fields(obj, kind)
            draft = _base_draft(state, req, kind, fields.get("title") or title_hint)
            if draft["version"] > 1:
                fields["title"] = state["entries"][draft["id"]]["payload"]["title"]
            draft.update(fields)
            draft["tags"] = list(dict.fromkeys(draft.get("tags", []) + [kind]))[:12]
            if sources:
                draft["refs"] = sources[:16]
            draft["source"] = {"request_id": req["event_id"], "generator": "llm", "model": model[:80]}
            try:
                validate_world_entry(draft)
                return WORLD, draft, sources, ([f"repaired after retry"] if attempt else [])
            except ValueError as e:
                last_err = str(e)
        messages = messages + [{"role": "assistant", "content": str(content)[:2000]},
                               {"role": "user", "content": f"That was invalid: {last_err[:200]}. Reply again "
                                                           "with only the JSON object, within the limits."}]
    raise OmniMindError(502, f"LLM output rejected: {last_err[:200]}")


# --- Worker (full nodes) ---------------------------------------------------------------------------
# ctx (built by the daemon): node_id, role, owners (set), autopublish (bool), llm_url, llm_model,
# llm_timeout, gpu (bool), cas_dir, flag_hide, max_per_pass, checkpoint_every, events() -> verified
# omnimind events, now_ms(), parse_signed(data, validators) -> event (ValueError -> 400,
# PermissionError -> 401), store_person(data, validators) -> (resp, status), store_node(action, payload)
# -> (resp, status).

def _state(ctx) -> dict:
    return replay(ctx.events(), ctx.node_id, ctx.owners, ctx.flag_hide)


def generators_of(ctx) -> list:
    return ["template-remix", "zodiac"] + (["llm"] if ctx.llm_url else [])


def advertise(ctx, st: dict = None) -> bool:
    """Publishes this node's omnimind_capability when it differs from the last one it signed."""
    st = st or _state(ctx)
    want = {"role": ctx.role, "llm": bool(ctx.llm_url), "gpu": bool(ctx.gpu)}
    have = st["capabilities"].get(ctx.node_id)
    if have and all(have[k] == v for k, v in want.items()):
        return False
    ctx.store_node(CAPABILITY, dict(want, schema=SCHEMA, generators=generators_of(ctx)))
    return True


def generate(ctx, st: dict, req: dict) -> tuple:
    """Picks the backend: LLM when configured and the request is text (falls back to the template
    generator if the LLM call fails or its output is rejected), template otherwise."""
    notes = []
    p = req["payload"]
    use_llm = ctx.llm_url and p["kind"] in ENTRY_KINDS and not (p["kind"] == "npc" and "sign" in p)
    if use_llm:
        try:
            act, draft, retrieval, n = llm_generate(st, req, ctx.llm_url, ctx.llm_model, ctx.llm_timeout)
            return act, draft, retrieval, notes + n, {"name": "llm", "model": ctx.llm_model[:80], "version": GENERATOR_VERSION}
        except OmniMindError as e:
            notes.append(("llm fallback: " + e.message)[:200])
    act, draft, retrieval, n = template_generate(st, req, ctx.node_id, ctx.cas_dir)
    name = "zodiac" if draft.get("kind") == "npc" else "template-remix"
    return act, draft, retrieval, notes + n, {"name": name, "model": f"omnimind-remix-{GENERATOR_VERSION}",
                                              "version": GENERATOR_VERSION}


def _publish_payload(proposal: dict) -> dict:
    d = json.loads(json.dumps(proposal["payload"]["draft"]))
    d["source"] = dict(d.get("source", {}), proposal_id=proposal["event_id"])
    return d


def _check_publishable(st: dict, action: str, payload: dict):
    if action == WORLD:
        head = st["entries"].get(payload["id"])
        if payload["version"] == 1 and head is not None:
            raise OmniMindError(409, f"entry '{payload['id']}' already exists; propose a new version instead")
        if payload["version"] > 1 and (head is None or head["event_id"] != payload["parent"]
                                       or head["version"] + 1 != payload["version"]):
            raise OmniMindError(409, "stale version: the entry moved on since this was proposed",
                                head=head and {"version": head["version"], "event_id": head["event_id"]})
    elif payload["sha256"] in st["assets"]:
        raise OmniMindError(409, "that asset is already in the library")


def worker_pass(ctx) -> dict:
    """One fulfilment pass on a full node: advertise capability, answer open requests (oldest first,
    at most max_per_pass) with proposals, autopublish clean ones when enabled, then checkpoint."""
    if ctx.role != "full":
        raise OmniMindError(409, "this node is a mini node (set OMNI_OMNIMIND_ROLE=full to fulfil requests)")
    out = {"proposed": [], "autopublished": [], "skipped": {}, "errors": [], "checkpoint": None}
    with LOCK:
        st = _state(ctx)
        out["advertised"] = advertise(ctx, st)
        mine = {p["payload"]["request_id"] for p in st["proposals"].values() if p["author"] == ctx.node_id}
        todo = []
        for rid, req in sorted(st["requests"].items(), key=lambda kv: (kv[1]["ts"], kv[0])):
            status, _ = request_status(st, rid)
            if status != "open":
                out["skipped"][status] = out["skipped"].get(status, 0) + 1
                continue
            if rid in mine:
                continue
            todo.append(req)
        for req in todo[:ctx.max_per_pass]:
            try:
                act, draft, retrieval, notes, gen = generate(ctx, st, req)
                safety = safety_pass(act, draft)
                if not safety["clean"] and any(h.startswith("schema:") for h in safety["hits"]):
                    raise OmniMindError(500, "generator produced an invalid draft: " + ", ".join(safety["hits"]))
                payload = {"schema": SCHEMA, "request_id": req["event_id"], "publish_action": act, "draft": draft,
                           "generator": gen, "safety": safety, "feed_injectable": False}
                if retrieval:
                    payload["retrieval"] = retrieval[:8]
                if notes:
                    payload["notes"] = [n[:200] for n in notes[:8]]
                validate_proposal(payload)
                resp, status = ctx.store_node(PROPOSAL, payload)
                if status != 200:
                    raise OmniMindError(status, resp.get("message", "store failed"))
                pid = resp["event_id"]
                out["proposed"].append(pid)
                if ctx.autopublish and safety["clean"]:
                    st = _state(ctx)
                    prop = st["proposals"].get(pid)
                    if prop is not None:
                        pub = _publish_payload(prop)
                        _check_publishable(st, act, pub)
                        r2, s2 = ctx.store_node(act, pub)
                        if s2 == 200:
                            out["autopublished"].append(r2["event_id"])
                st = _state(ctx)
            except OmniMindError as e:
                out["errors"].append({"request_id": req["event_id"], "message": e.message[:200]})
            except Exception as e:   # one bad request must not stop the pass
                out["errors"].append({"request_id": req["event_id"], "message": f"{type(e).__name__}: {str(e)[:160]}"})
        out["checkpoint"] = maybe_checkpoint(ctx, st)
    return out


def maybe_checkpoint(ctx, st: dict):
    mine = [c for c in st["checkpoints"] if c["author"] == ctx.node_id]
    root, count = world_root(st)
    if count == 0:
        return None
    last = mine[-1] if mine else None
    if last and (last["root"] == root or count - last["count"] < ctx.checkpoint_every):
        return None
    upto = max(r["ts"] for h in st["history"].values() for r in h)
    resp, status = ctx.store_node(CHECKPOINT, {"schema": SCHEMA, "root": root, "count": count, "upto_ts": upto})
    return resp.get("event_id") if status == 200 else None


# --- Views --------------------------------------------------------------------------------------------
def entry_view(st: dict, eid_: str, full: bool = False) -> dict:
    rec = st["entries"][eid_]
    p = rec["payload"]
    v = {"id": eid_, "kind": p["kind"], "title": p["title"], "summary": p["summary"], "tags": p.get("tags", []),
         "version": rec["version"], "event_id": rec["event_id"], "parent": rec["parent"], "author": rec["author"],
         "ts": rec["ts"], "feed_injectable": False, "hidden": st["hidden"].get(eid_),
         "flags": len(st["flag_counts"].get(rec["event_id"], set())), "signals": signal_view(st, eid_)}
    if full:
        v["entry"] = p
        v["history"] = [{"version": r["version"], "event_id": r["event_id"], "parent": r["parent"], "ts": r["ts"],
                         "author": r["author"], "proposal_id": r["payload"].get("source", {}).get("proposal_id")}
                        for r in st["history"][eid_]]
    return v


def asset_view(st: dict, sha: str, cas_dir: str = None) -> dict:
    a = st["assets"][sha]
    return {"sha256": sha, "event_id": a["event_id"], "author": a["author"], "ts": a["ts"],
            "asset": a["payload"], "local_file": cas_has(cas_dir, sha), "feed_injectable": False,
            "flags": len(st["flag_counts"].get(a["event_id"], set()))}


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


def _limit(q: dict) -> int:
    if "limit" not in q:
        return LIST_DEFAULT_LIMIT
    if not re.fullmatch(r"[0-9]{1,4}", q["limit"]):
        raise ValueError("'limit' must be a positive integer")
    return max(1, min(int(q["limit"]), LIST_MAX_LIMIT))


def _err(e: Exception) -> tuple:
    if isinstance(e, OmniMindError):
        return e.response()
    if isinstance(e, PermissionError):
        return {"status": "ERROR", "message": str(e)}, 401
    return {"status": "ERROR", "message": str(e)}, 400


def _latest_checkpoint(st: dict) -> dict:
    if not st["checkpoints"]:
        return None
    c = st["checkpoints"][-1]
    root, count = world_root(st, c["upto_ts"])
    return dict(c, verified=(root == c["root"] and count == c["count"]))


def get_world(ctx, query: str) -> tuple:
    try:
        q = _query(query, {"q", "kind", "tag", "limit", "include_hidden"})
        limit = _limit(q)
        kind = q.get("kind") or None
        if kind and kind not in ENTRY_KINDS + ("asset",):
            raise ValueError("'kind' must be one of: " + ", ".join(ENTRY_KINDS + ("asset",)))
        tag = q.get("tag") or None
        if tag:
            _tag(tag, "tag")
        include_hidden = q.get("include_hidden") in ("1", "true")
        text = q.get("q", "")
        if len(text) > 200:
            raise ValueError("'q' is longer than 200 characters")
    except ValueError as e:
        return _err(e)
    st = _state(ctx)
    root, count = world_root(st)
    base = {"status": "OK", "node_id": ctx.node_id, "role": ctx.role, "world_root": root, "entries_total": count,
            "checkpoint": _latest_checkpoint(st)}
    if kind == "asset":
        shas = sorted(st["assets"], key=lambda s: (-st["assets"][s]["ts"], s))
        if tag:
            shas = [s for s in shas if tag in st["assets"][s]["payload"].get("tags", [])]
        return dict(base, kind="asset", count=min(limit, len(shas)), has_more=len(shas) > limit,
                    assets=[asset_view(st, s, ctx.cas_dir) for s in shas[:limit]]), 200
    if text.strip():
        hits = retrieve(st, text, kind, limit=LIST_MAX_LIMIT, include_hidden=include_hidden)
        ids = [(score, k) for score, k in hits if not tag or tag in st["entries"][k]["payload"].get("tags", [])]
        items = [dict(entry_view(st, k), score=score) for score, k in ids[:limit]]
        return dict(base, query=text, count=len(items), has_more=len(ids) > limit, entries=items), 200
    ids = [k for k, r in st["entries"].items() if (kind is None or r["kind"] == kind)
           and (include_hidden or k not in st["hidden"]) and (not tag or tag in r["payload"].get("tags", []))]
    ids.sort(key=lambda k: (-st["entries"][k]["ts"], k))
    return dict(base, count=min(limit, len(ids)), has_more=len(ids) > limit,
                entries=[entry_view(st, k) for k in ids[:limit]]), 200


def get_world_item(ctx, item: str, query: str) -> tuple:
    try:
        _query(query, set())
    except ValueError as e:
        return _err(e)
    st = _state(ctx)
    if _SHA_RE.fullmatch(item):
        if item not in st["assets"]:
            return {"status": "ERROR", "message": "no asset with that sha256 on this node"}, 404
        return {"status": "OK", "asset": asset_view(st, item, ctx.cas_dir)}, 200
    if _EVENT_ID_RE.fullmatch(item):
        rec = st["by_event"].get(item)
        if rec is None:
            return {"status": "ERROR", "message": "no world entry or asset with that event id"}, 404
        if "asset" in rec:
            return {"status": "OK", "asset": asset_view(st, rec["asset"], ctx.cas_dir)}, 200
        v = entry_view(st, rec["id"], full=True)
        v["requested_version"] = rec["version"]
        v["requested_entry"] = rec["payload"]
        return {"status": "OK", "entry": v}, 200
    if not _SLUG_RE.fullmatch(item) or item not in st["entries"]:
        return {"status": "ERROR", "message": "no world entry with that id on this node"}, 404
    return {"status": "OK", "entry": entry_view(st, item, full=True)}, 200


def request_view(st: dict, rid: str) -> dict:
    r = st["requests"][rid]
    status, pids = request_status(st, rid)
    return {"request_id": rid, "author": r["author"], "ts": r["ts"], "request": r["payload"], "status": status,
            "proposals": [{"proposal_id": pid, "author": st["proposals"][pid]["author"],
                           "generator": st["proposals"][pid]["payload"]["generator"]["name"],
                           "published": st["published"].get(pid), "rejected": pid in st["rejected"]}
                          for pid in pids]}


def get_requests(ctx, query: str) -> tuple:
    try:
        q = _query(query, {"status", "author", "limit"})
        limit = _limit(q)
        want = q.get("status") or None
        if want and want not in ("open", "proposed", "published", "rejected", "waiting_gpu", "gpu_available", "flagged"):
            raise ValueError("'status' is not a request status")
        author = _pubkey(q["author"], "author") if q.get("author") else None
    except ValueError as e:
        return _err(e)
    st = _state(ctx)
    rids = sorted(st["requests"], key=lambda k: (-st["requests"][k]["ts"], k))
    views = [request_view(st, k) for k in rids if not author or st["requests"][k]["author"] == author]
    if want:
        views = [v for v in views if v["status"] == want]
    node = {"node_id": ctx.node_id, "role": ctx.role, "llm": bool(ctx.llm_url), "autopublish": bool(ctx.autopublish),
            "full_nodes": len(st["full_nodes"]), "gpu_nodes": len(st["gpu_nodes"])}
    return {"status": "OK", "node": node, "count": min(limit, len(views)), "has_more": len(views) > limit,
            "requests": views[:limit]}, 200


def get_review(ctx, query: str) -> tuple:
    """Loopback only (the daemon keeps it out of REMOTE_GET_PATHS): what an owner has to decide."""
    try:
        q = _query(query, {"limit"})
        limit = _limit(q)
    except ValueError as e:
        return _err(e)
    st = _state(ctx)
    now = ctx.now_ms()
    queue = []
    for pid, prop in sorted(st["proposals"].items(), key=lambda kv: (kv[1]["ts"], kv[0])):
        if pid in st["published"] or pid in st["rejected"] or prop["payload"]["request_id"] not in st["requests"]:
            continue
        p = prop["payload"]
        flaggers = st["flag_counts"].get(pid, set())
        why = []
        if not p["safety"]["clean"]:
            why.append("safety: " + ", ".join(p["safety"]["hits"]))
        if flaggers:
            why.append(f"flagged by {len(flaggers)}")
        if not ctx.autopublish:
            why.append("autopublish off (review everything)")
        pub = _publish_payload(prop)
        stale = None
        try:
            _check_publishable(st, p["publish_action"], pub)
        except OmniMindError as e:
            stale = e.message
        queue.append({"proposal_id": pid, "proposer": prop["author"], "ts": prop["ts"],
                      "request": request_view(st, p["request_id"])["request"], "request_id": p["request_id"],
                      "publish_action": p["publish_action"], "draft": p["draft"], "generator": p["generator"],
                      "safety": p["safety"], "notes": p.get("notes", []), "flags": len(flaggers),
                      "flagged": bool(flaggers) or not p["safety"]["clean"], "why_in_queue": why, "stale": stale,
                      "to_sign": {"action": p["publish_action"], "payload": pub, "previous_hash": ZERO_HASH,
                                  "ts": now, "author_pubkey": "<owner key>"}})
    flagged = []
    for eid_, reason in sorted(st["hidden"].items()):
        if eid_ in st["entries"]:
            flagged.append(dict(entry_view(st, eid_), hidden=reason))
    for eid_, rec in sorted(st["entries"].items()):
        if eid_ not in st["hidden"] and st["flag_counts"].get(rec["event_id"]):
            flagged.append(entry_view(st, eid_))
    return {"status": "OK", "autopublish": bool(ctx.autopublish), "owners": sorted(ctx.owners),
            "count": min(limit, len(queue)), "has_more": len(queue) > limit, "proposals": queue[:limit],
            "flagged_entries": flagged[:limit], "ignored": st["ignored"]}, 200


def _signed(ctx, data, validators: dict) -> dict:
    ev = ctx.parse_signed(data, validators)
    now = ctx.now_ms()
    if abs(ev["ts"] - now) > TS_WINDOW_MS:
        raise OmniMindError(400, f"'ts' must be within {TS_WINDOW_MS // 60000} minutes of the node clock", node_ts=now)
    return ev


def post_request(ctx, data) -> tuple:
    try:
        ev = _signed(ctx, data, {REQUEST: validate_request})
        hits = sorted({h for t in [ev["payload"]["need"], ev["payload"].get("context", "")] for h in safety_hits(t)})
        if hits:
            raise OmniMindError(422, "request refused by the safety pass", hits=hits)
    except Exception as e:
        return _err(e)
    resp, status = ctx.store_person(data, {REQUEST: validate_request})
    if status == 200:
        resp = dict(resp, request_status="waiting_gpu" if ev["payload"].get("needs_gpu") else "open")
    return resp, status


def post_signal(ctx, data) -> tuple:
    try:
        _signed(ctx, data, {SIGNAL: validate_signal})
    except Exception as e:
        return _err(e)
    return ctx.store_person(data, {SIGNAL: validate_signal})


def post_flag(ctx, data) -> tuple:
    try:
        _signed(ctx, data, {FLAG: validate_flag})
    except Exception as e:
        return _err(e)
    return ctx.store_person(data, {FLAG: validate_flag})


def _target_record(st: dict, body: dict) -> tuple:
    """('proposal' | 'entry', record) for {"proposal_id"} or {"target"} (event id, or entry id)."""
    if "proposal_id" in body:
        pid = _event_id(body["proposal_id"], "proposal_id")
        if pid not in st["proposals"]:
            raise OmniMindError(404, "no such proposal on this node")
        return "proposal", st["proposals"][pid]
    t = body.get("target")
    if isinstance(t, str) and _SLUG_RE.fullmatch(t) and t in st["entries"]:
        return "entry", st["entries"][t]
    if isinstance(t, str) and _EVENT_ID_RE.fullmatch(t):
        if t in st["proposals"]:
            return "proposal", st["proposals"][t]
        rec = st["by_event"].get(t)
        if rec and "id" in rec:
            return "entry", st["entries"][rec["id"]]
    raise OmniMindError(404, "send 'proposal_id', or 'target' (a world entry id or event id) known to this node")


def _owner_signed(ctx, data, validators: dict) -> dict:
    ev = _signed(ctx, data, validators)
    if ev["author_pubkey"] not in ctx.owners:
        raise OmniMindError(403, "only an owner key (OMNI_OMNIMIND_OWNERS) can approve or reject")
    return ev


def post_approve(ctx, data) -> tuple:
    """Owner-only, loopback (the daemon keeps it out of REMOTE_POST_PATHS). Either
      {"proposal_id"}         - this node signs the publication (needs 'self' in OMNI_OMNIMIND_OWNERS),
      {"target"}              - keep a flagged / removed world entry (this node signs the review), or
      a signed envelope       - an owner key's own omnimind_world_entry / omnimind_asset_ref (payload =
                                the queue's to_sign.payload) or omnimind_review {decision: keep}."""
    with LOCK:
        try:
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            st = _state(ctx)
            if "signature" in data:
                act = data.get("action")
                if act in (WORLD, ASSET):
                    ev = _owner_signed(ctx, data, {act: PUBLISH_VALIDATORS[act]})
                    pid = ev["payload"].get("source", {}).get("proposal_id")
                    if not pid or pid not in st["proposals"]:
                        raise OmniMindError(404, "payload.source.proposal_id must name a proposal on this node")
                    if pid in st["published"] or pid in st["rejected"]:
                        raise OmniMindError(409, "that proposal was already decided")
                    if st["proposals"][pid]["payload"]["publish_action"] != act:
                        raise OmniMindError(400, f"that proposal publishes {st['proposals'][pid]['payload']['publish_action']}")
                    _check_publishable(st, act, ev["payload"])
                    safety = safety_pass(act, ev["payload"])
                    resp, status = ctx.store_person(data, {act: PUBLISH_VALIDATORS[act]})
                    return (dict(resp, decision="published", proposal_id=pid, safety=safety), status)
                if act == REVIEW:
                    ev = _owner_signed(ctx, data, {REVIEW: validate_review})
                    if ev["payload"]["decision"] != "keep":
                        raise OmniMindError(400, "use /api/omnimind/reject for reject / remove")
                    resp, status = ctx.store_person(data, {REVIEW: validate_review})
                    return dict(resp, decision="keep"), status
                raise ValueError(f"signed body must be {WORLD}, {ASSET} or {REVIEW}")
            if ctx.node_id not in ctx.owners:
                raise OmniMindError(403, "this node's key is not an owner: add 'self' to OMNI_OMNIMIND_OWNERS, or "
                                         "send an owner-signed envelope")
            unknown = set(data) - {"proposal_id", "target"}
            if unknown:
                raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
            what, rec = _target_record(st, data)
            if what == "proposal":
                pid = rec["event_id"]
                if pid in st["published"]:
                    raise OmniMindError(409, "that proposal is already published", event_id=st["published"][pid])
                if pid in st["rejected"]:
                    raise OmniMindError(409, "that proposal was rejected")
                act = rec["payload"]["publish_action"]
                pub = _publish_payload(rec)
                _check_publishable(st, act, pub)
                resp, status = ctx.store_node(act, pub)
                return (dict(resp, decision="published", proposal_id=pid, safety=rec["payload"]["safety"]), status)
            resp, status = ctx.store_node(REVIEW, {"schema": SCHEMA, "target": rec["event_id"], "decision": "keep"})
            return dict(resp, decision="keep", entry=rec["id"]), status
        except Exception as e:
            return _err(e)


def post_reject(ctx, data) -> tuple:
    """Owner-only, loopback. {"proposal_id"|"target", "reason"?} signed by this node (needs 'self' in
    owners), or an owner-signed omnimind_review envelope. A world entry target is removed (hidden)."""
    with LOCK:
        try:
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            st = _state(ctx)
            if "signature" in data:
                ev = _owner_signed(ctx, data, {REVIEW: validate_review})
                if ev["payload"]["decision"] == "keep":
                    raise OmniMindError(400, "use /api/omnimind/approve for keep")
                resp, status = ctx.store_person(data, {REVIEW: validate_review})
                return dict(resp, decision=ev["payload"]["decision"]), status
            if ctx.node_id not in ctx.owners:
                raise OmniMindError(403, "this node's key is not an owner: add 'self' to OMNI_OMNIMIND_OWNERS, or "
                                         "send an owner-signed envelope")
            unknown = set(data) - {"proposal_id", "target", "reason"}
            if unknown:
                raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
            what, rec = _target_record(st, data)
            payload = {"schema": SCHEMA, "target": rec["event_id"],
                       "decision": "reject" if what == "proposal" else "remove"}
            if "reason" in data:
                payload["reason"] = _enum(data["reason"], "reason", REVIEW_REASONS)
            if what == "proposal" and (rec["event_id"] in st["published"] or rec["event_id"] in st["rejected"]):
                raise OmniMindError(409, "that proposal was already decided")
            resp, status = ctx.store_node(REVIEW, payload)
            return dict(resp, decision=payload["decision"]), status
        except Exception as e:
            return _err(e)


def post_fulfil(ctx, data) -> tuple:
    """Loopback only: run one worker pass now (full nodes). Body: {}."""
    if data not in ({}, None):
        return {"status": "ERROR", "message": "send an empty JSON object"}, 400
    try:
        return dict(worker_pass(ctx), status="OK"), 200
    except Exception as e:
        return _err(e)


def get_generate_asset(ctx, query: str) -> tuple:
    """GET /api/v1/omnimind/generate-asset?sign=Scorpio&seed=N: the Zodiac NPC manifest, computed
    locally (no network, nothing written to the chain). To get it into the world, sign an
    omnimind_request {kind: "npc", sign, seed} instead."""
    try:
        q = _query(query, {"sign", "seed"})
        seed = 0
        if "seed" in q:
            if not re.fullmatch(r"[0-9]{1,16}", q["seed"]):
                raise ValueError("'seed' must be a non-negative integer")
            seed = int(q["seed"])
        manifest = zodiac_npc(zodiac_sign(q.get("sign", "")), seed)
    except ValueError as e:
        return {"status": "error", "message": str(e)}, 400
    return {"status": "success", "data": manifest}, 200


GET_ROUTES = {"/api/omnimind/world": get_world, "/api/omnimind/requests": get_requests,
              "/api/omnimind/review": get_review, "/api/v1/omnimind/generate-asset": get_generate_asset}
POST_ROUTES = {"/api/omnimind/request": post_request, "/api/omnimind/signal": post_signal,
               "/api/omnimind/flag": post_flag, "/api/omnimind/approve": post_approve,
               "/api/omnimind/reject": post_reject, "/api/omnimind/fulfil": post_fulfil}
