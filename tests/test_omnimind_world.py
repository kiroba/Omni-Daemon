"""Daemon v8.26.0: OmniMind world library (Admin/omni_omnimind.py + /api/omnimind/* on :5000).

Covers schema validation, replay determinism, the Zodiac NPC generator, the safety pass, signal
aggregation, the LLM backend against a local fake OpenAI-compatible server, the request -> proposal ->
approve / reject flow, autopublish off and on, the Human Feed Policy, forged events being refused, and
two daemons (a mini "phone" node and a full "desktop" node) syncing a request and its fulfilment.

Run: python3 -m unittest tests.test_omnimind_world
"""
import ast
import hashlib
import json
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daemon_harness import ADMIN, Daemon, Key, daemon_module, now_ms  # noqa: E402
from daemon_version import DAEMON_VERSION  # noqa: E402

import omni_omnimind as om  # noqa: E402  (daemon_harness put Admin/ on sys.path)

ZERO = "0x" + "0" * 64
NODE = "f" * 64
OWNER = "e" * 64


def pk(i: int) -> str:
    return f"{i:064x}"


_n = [0]


def ev(action, author, ts, payload, eid=None) -> dict:
    """An already-verified event as the daemon hands it to omni_omnimind.replay()."""
    _n[0] += 1
    return {"event_id": eid or "0x%064x" % (10_000 + _n[0]), "action": action, "author_pubkey": author, "ts": ts,
            "payload": dict(payload, schema=1)}


def signed(key: Key, action: str, payload: dict, ts: int = None, prev: str = ZERO) -> dict:
    return key.sign_event({"action": action, "author_pubkey": key.pub, "payload": dict(payload, schema=1),
                           "previous_hash": prev, "ts": now_ms() if ts is None else ts})


def entry(eid, version=1, parent=None, kind="npc", title=None, **extra) -> dict:
    p = {"kind": kind, "id": eid, "version": version, "parent": parent, "title": title or eid.replace("-", " "),
         "summary": f"About {eid}.", "feed_injectable": False}
    p.update(extra)
    return p


def canon(obj) -> str:
    def fix(o):
        if isinstance(o, dict):
            return {(json.dumps(k) if isinstance(k, tuple) else str(k)): fix(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [fix(x) for x in o]
        if isinstance(o, set):
            return sorted(fix(x) for x in o)
        return o
    return json.dumps(fix(obj), sort_keys=True)


# --- Fake OpenAI-compatible LLM (Ollama / llama.cpp shape) ----------------------------------------------
class FakeLLM:
    """Answers /v1/chat/completions. The reply depends on the 'need' in the user message:
    'fenced' -> fenced JSON with a trailing comma, 'retry' -> garbage first then valid JSON,
    'garbage' -> never JSON, 'link' -> valid JSON whose text has a link (fails the safety pass)."""

    def __init__(self):
        self.calls = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append((self.path, body))
                user = json.loads(body["messages"][1]["content"])
                need = user["need"]
                retry_round = len(body["messages"]) > 2
                good = {"title": f"Tale of {need.split()[-1].capitalize()}", "summary": "An old story the river tells.",
                        "tags": ["River Lore", "old"], "attrs": {"era": "First Age", "bogus": 1}, "extra": "dropped"}
                if "fenced" in need:
                    content = "Here you go:\n```json\n" + json.dumps(good)[:-1] + ",}\n```\nEnjoy!"
                elif "retry" in need and not retry_round:
                    content = "I cannot comply with JSON today."
                elif "garbage" in need:
                    content = "{not json at all"
                elif "link" in need:
                    content = json.dumps(dict(good, summary="Read more at https://example.com/lore now."))
                else:
                    content = json.dumps(good)
                out = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def req_event(need, kind="lore", **extra):
    e = ev(om.REQUEST, pk(7), 1_800_000_000_000, dict({"kind": kind, "need": need}, **extra))
    return e


# --- Unit tests -------------------------------------------------------------------------------------------
class Unit(unittest.TestCase):
    def test_stdlib_only(self):
        """Chaquopy: omni_omnimind imports nothing outside the standard library."""
        tree = ast.parse((ADMIN / "omni_omnimind.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertTrue(names <= {"colorsys", "hashlib", "json", "math", "os", "re", "threading", "urllib"}, names)

    def test_android_packager_finds_the_module(self):
        text = (ADMIN / "omni_live_daemon_server.py").read_text(encoding="utf-8")
        self.assertIn("omni_omnimind", re.findall(r"^(?:import|from) (omni_[a-z0-9_]+)", text, re.M))

    def test_schema_validation(self):
        om.validate_world_entry(dict(entry("ember-warden", tags=["fire"], attrs={"difficulty": 4, "persona": "aries"}), schema=1))
        om.validate_world_entry(dict(entry("old-song", 2, "0x" + "a" * 64, kind="lore", attrs={"era": "Now"}), schema=1))
        om.validate_asset_ref({"schema": 1, "sha256": "a" * 64, "kind": "svg", "size": 10, "prompt": "", "model": "m",
                               "license": "CC0-1.0", "source_node": NODE, "feed_injectable": False})
        om.validate_request({"schema": 1, "kind": "npc", "need": "a guard", "sign": "leo", "seed": 3})
        om.validate_signal({"schema": 1, "target": "ember-warden", "signal": "lose_to", "consent": om.SIGNAL_CONSENT})
        good = dict(entry("x-1"), schema=1)
        bad = [
            (om.validate_world_entry, dict(good, feed_injectable=True)),
            (om.validate_world_entry, {k: v for k, v in good.items() if k != "feed_injectable"}),
            (om.validate_world_entry, dict(good, version=2)),                       # v2 needs a parent
            (om.validate_world_entry, dict(good, parent="0x" + "a" * 64)),          # v1 has no parent
            (om.validate_world_entry, dict(good, kind="dragon")),
            (om.validate_world_entry, dict(good, id="Bad Id")),
            (om.validate_world_entry, dict(good, title="x" * 81)),
            (om.validate_world_entry, dict(good, attrs={"rarity": "rare"})),        # item attr on an npc
            (om.validate_world_entry, dict(good, attrs={"difficulty": 11})),
            (om.validate_world_entry, dict(good, schema=2)),
            (om.validate_world_entry, dict(good, extra=1)),
            (om.validate_world_entry, dict(good, attrs={"layers": [{"layer": "body", "asset_id": "b", "z": 0,
                                                                     "tint": "#FFFFFF", "opacity": 100}]})),
            (om.validate_request, {"schema": 1, "kind": "lore", "need": "x", "sign": "leo"}),
            (om.validate_request, {"schema": 1, "kind": "lore", "need": ""}),
            (om.validate_request, {"schema": 1, "kind": "lore", "need": "x", "needs_gpu": "yes"}),
            (om.validate_signal, {"schema": 1, "target": "a-b", "signal": "pick", "consent": "yes"}),
            (om.validate_signal, {"schema": 1, "target": "a-b", "signal": "love", "consent": om.SIGNAL_CONSENT}),
            (om.validate_signal, {"schema": 1, "target": "a-b", "signal": "pick", "consent": om.SIGNAL_CONSENT, "note": "hi"}),
            (om.validate_flag, {"schema": 1, "target": "nope", "reason": "spam"}),
            (om.validate_review, {"schema": 1, "target": "0x" + "a" * 64, "decision": "approve"}),
            (om.validate_asset_ref, {"schema": 1, "sha256": "a" * 64, "kind": "svg", "size": 10, "prompt": "",
                                     "model": "m", "license": "WTFPL", "source_node": NODE, "feed_injectable": False}),
        ]
        for fn, p in bad:
            with self.assertRaises(ValueError, msg=repr(p)):
                fn(p)

    def test_zodiac_all_signs(self):
        self.assertEqual(len(om.ZODIAC_SIGNS), 12)
        for sign in om.ZODIAC_SIGNS:
            for seed in (0, 1, 42, 2 ** 40):
                m = om.zodiac_npc(sign.capitalize(), seed)
                self.assertRegex(m["trait_hash"], r"^[0-9a-f]{64}$")
                self.assertEqual(m["trait_hash"], hashlib.sha256(om.canonical(m["traits"])).hexdigest())
                self.assertTrue(om.layers_normalized(m["layers"]), m["layers"])
                self.assertEqual([x["layer"] for x in m["layers"]], ["body", "palette", "outfit", "accessory", "aura"])
                self.assertIs(m["feed_injectable"], False)
                self.assertEqual(m, om.zodiac_npc(sign, seed))                      # same seed -> same output
                body = {k: v for k, v in m.items() if k != "manifest_hash"}
                self.assertEqual(m["manifest_hash"], hashlib.sha256(om.canonical(body)).hexdigest())
            hashes = {json.dumps(om.zodiac_npc(sign, s)["layers"]) + om.zodiac_npc(sign, s)["trait_hash"] for s in range(20)}
            self.assertGreater(len(hashes), 1, f"{sign}: seeds should vary the NPC")
        with self.assertRaises(ValueError):
            om.zodiac_npc("ophiuchus", 1)
        with self.assertRaises(ValueError):
            om.zodiac_npc("leo", -1)

    def test_safety_pass(self):
        self.assertEqual(om.safety_hits("A brave knight guards the dawn gate."), [])
        self.assertIn("blocklist:self_harm", om.safety_hits("just KYS"))
        self.assertIn("blocklist:sexual", om.safety_hits("n.s.f.w content"))
        self.assertIn("blocklist:sexual", om.safety_hits("p0rn"))                    # leetspeak folded
        self.assertIn("link", om.safety_hits("see www.example.com"))
        self.assertIn("pii:email", om.safety_hits("mail me a@b.co"))
        self.assertIn("pii:phone", om.safety_hits("call 314-555-0199"))
        self.assertEqual(om.safety_hits("The scorpion has a sting."), [])           # no substring false positives
        clean = om.safety_pass(om.WORLD, dict(entry("knight-1"), schema=1))
        self.assertEqual(clean, {"clean": True, "hits": []})
        dirty = om.safety_pass(om.WORLD, dict(entry("knight-1", body="buy cocaine at www.x.io"), schema=1))
        self.assertFalse(dirty["clean"])
        self.assertIn("link", dirty["hits"])
        self.assertTrue(any(h.startswith("schema:") for h in om.safety_pass(om.WORLD, dict(entry("k-1"), schema=1, title="x" * 99))["hits"]))

    def _sample(self):
        t0 = 1_800_000_000_000
        e1 = ev(om.WORLD, OWNER, t0, entry("river-song", kind="lore"), eid="0x" + "1" * 64)
        e2 = ev(om.WORLD, OWNER, t0 + 10, entry("river-song", 2, e1["event_id"], kind="lore", title="River song v2"))
        e3 = ev(om.WORLD, NODE, t0 + 20, entry("ember-warden", attrs={"difficulty": 5}))
        fork = ev(om.WORLD, OWNER, t0 + 30, entry("river-song", 2, e1["event_id"], kind="lore", title="fork"))
        stranger = ev(om.WORLD, pk(9), t0 + 5, entry("evil-twin"))
        req = ev(om.REQUEST, pk(7), t0 + 1, {"kind": "npc", "need": "a guard"})
        prop = ev(om.PROPOSAL, NODE, t0 + 2, {"request_id": req["event_id"], "publish_action": om.WORLD,
                                              "draft": dict(entry("guard-1"), schema=1),
                                              "generator": {"name": "zodiac", "model": "m", "version": "0.1.0"},
                                              "safety": {"clean": True, "hits": []}, "feed_injectable": False})
        sigs = [ev(om.SIGNAL, pk(20 + i), t0 + 40 + i, {"target": "ember-warden", "signal": s, "consent": om.SIGNAL_CONSENT})
                for i, s in enumerate(["pick", "lose_to", "lose_to", "lose_to", "skip", "pick"])]
        flags = [ev(om.FLAG, pk(30 + i), t0 + 60 + i, {"target": e3["event_id"], "reason": "spam"}) for i in range(2)]
        cap = ev(om.CAPABILITY, pk(50), t0, {"role": "full", "llm": False, "gpu": True})
        return [e1, e2, e3, fork, stranger, req, prop, cap] + sigs + flags

    def test_replay_is_deterministic_in_any_order(self):
        evs = self._sample()
        ref = canon(om.replay(evs, NODE, {OWNER}))
        rng = random.Random(11)
        for _ in range(20):
            shuffled = list(evs)
            rng.shuffle(shuffled)
            self.assertEqual(canon(om.replay(shuffled + shuffled[:4], NODE, {OWNER})), ref)
        st = om.replay(evs, NODE, {OWNER})
        self.assertEqual(st["entries"]["river-song"]["version"], 2)
        self.assertEqual(st["entries"]["river-song"]["payload"]["title"], "River song v2")   # earlier ts wins the fork
        self.assertEqual(st["ignored"]["version_conflict"], 1)
        self.assertNotIn("evil-twin", st["entries"])                     # not a publisher
        self.assertEqual(st["ignored"]["not_publisher"], 1)
        self.assertEqual([r["version"] for r in st["history"]["river-song"]], [1, 2])
        self.assertEqual(st["history"]["river-song"][1]["parent"], st["history"]["river-song"][0]["event_id"])
        self.assertEqual(st["gpu_nodes"], [pk(50)])
        root1, n1 = om.world_root(st)
        self.assertEqual(n1, 2)
        self.assertEqual(om.world_root(om.replay(list(reversed(evs)), NODE, {OWNER})), (root1, n1))
        # Without the owner, the owner's entries don't count on this node.
        self.assertNotIn("river-song", om.replay(evs, NODE, set())["entries"])

    def test_signals_aggregation(self):
        st = om.replay(self._sample(), NODE, {OWNER})
        v = om.signal_view(st, "ember-warden")
        self.assertEqual(v["counts"], {"pick": 2, "finish": 0, "skip": 1, "lose_to": 3, "report": 0})
        self.assertEqual(v["people"], 6)
        self.assertEqual(v["loss_rate"], 0.5)
        t0 = 1_800_000_000_000
        dup = [ev(om.SIGNAL, pk(1), t0 + i, {"target": "x-1", "signal": "pick", "consent": om.SIGNAL_CONSENT}) for i in range(5)]
        dup.append(ev(om.SIGNAL, pk(1), t0 + om.DAY_MS, {"target": "x-1", "signal": "pick", "consent": om.SIGNAL_CONSENT}))
        st2 = om.replay(dup, NODE)
        self.assertEqual(st2["signals"]["x-1"]["counts"]["pick"], 2)          # one per person/target/kind/day
        self.assertEqual(st2["ignored"]["signal_duplicate"], 4)
        # Difficulty tuning: mostly losing -> easier; never losing with finishes -> harder.
        lose = [ev(om.SIGNAL, pk(60 + i), t0, {"target": "boss-1", "signal": "lose_to", "consent": om.SIGNAL_CONSENT}) for i in range(4)]
        lose.append(ev(om.SIGNAL, pk(70), t0, {"target": "boss-1", "signal": "pick", "consent": om.SIGNAL_CONSENT}))
        self.assertEqual(om.tuned_difficulty(om.replay(lose, NODE), "boss-1", 6)[0], 5)
        win = [ev(om.SIGNAL, pk(80 + i), t0, {"target": "boss-1", "signal": s, "consent": om.SIGNAL_CONSENT})
               for i, s in enumerate(["finish", "finish", "finish", "pick", "pick", "pick"])]
        self.assertEqual(om.tuned_difficulty(om.replay(win, NODE), "boss-1", 6)[0], 7)
        self.assertEqual(om.tuned_difficulty(om.replay(win[:2], NODE), "boss-1", 6), (6, None))   # too few signals
        skip = [ev(om.WORLD, NODE, t0, entry("dull-npc"))] + [
            ev(om.SIGNAL, pk(90 + i), t0 + 1, {"target": "dull-npc", "signal": "skip", "consent": om.SIGNAL_CONSENT}) for i in range(3)]
        self.assertEqual(om.vary_candidates(om.replay(skip, NODE)), ["dull-npc"])

    def test_flags_hide_and_keep(self):
        evs = self._sample()
        st = om.replay(evs, NODE, {OWNER}, flag_hide=3)
        self.assertNotIn("ember-warden", st["hidden"])                     # 2 flags < 3
        third = ev(om.FLAG, pk(39), 1_800_000_000_100, {"target": evs[2]["event_id"], "reason": "spam"})
        st = om.replay(evs + [third], NODE, {OWNER}, flag_hide=3)
        self.assertEqual(st["hidden"]["ember-warden"], "flagged")
        keep = ev(om.REVIEW, OWNER, 1_800_000_000_200, {"target": evs[2]["event_id"], "decision": "keep"})
        self.assertNotIn("ember-warden", om.replay(evs + [third, keep], NODE, {OWNER}, flag_hide=3)["hidden"])
        fake_keep = ev(om.REVIEW, pk(99), 1_800_000_000_200, {"target": evs[2]["event_id"], "decision": "keep"})
        self.assertIn("ember-warden", om.replay(evs + [third, fake_keep], NODE, {OWNER}, flag_hide=3)["hidden"])

    def test_retrieval_tfidf(self):
        t0 = 1_800_000_000_000
        evs = [ev(om.WORLD, NODE, t0, entry("salt-marsh", kind="place", title="The Salt Marsh", tags=["marsh", "coast"])),
               ev(om.WORLD, NODE, t0, entry("ember-forge", kind="place", title="Ember Forge", tags=["fire"])),
               ev(om.WORLD, NODE, t0, entry("marsh-witch", title="Witch of the Marsh", tags=["marsh"]))]
        st = om.replay(evs, NODE)
        hits = om.retrieve(st, "a hermit living in the salt marsh")
        self.assertEqual(hits[0][1], "salt-marsh")
        self.assertEqual({h[1] for h in hits}, {"salt-marsh", "marsh-witch"})
        self.assertEqual(om.retrieve(st, "marsh", kind="npc")[0][1], "marsh-witch")
        self.assertEqual(om.retrieve(st, "zzz"), [])

    def test_template_generator_is_deterministic(self):
        st = om.replay(self._sample(), NODE, {OWNER})
        tmp = tempfile.mkdtemp(prefix="omni_cas_")
        try:
            for kind, extra in (("lore", {}), ("place", {}), ("quest", {}), ("item", {}), ("npc", {"sign": "scorpio", "seed": 9}),
                                ("npc", {}), ("asset", {})):
                r = req_event(f"something for {kind} near the river", kind, **extra)
                st2 = dict(st, requests=dict(st["requests"], **{r["event_id"]: {"event_id": r["event_id"], "author": pk(7),
                                                                                 "ts": r["ts"], "payload": r["payload"]}}))
                a = om.template_generate(st2, st2["requests"][r["event_id"]], NODE, tmp)
                b = om.template_generate(st2, st2["requests"][r["event_id"]], NODE, tmp)
                self.assertEqual(a, b, kind)
                om.PUBLISH_VALIDATORS[a[0]](a[1])
                self.assertIs(a[1]["feed_injectable"], False)
                if kind == "npc" and extra:
                    m = om.zodiac_npc("scorpio", 9)
                    self.assertEqual(a[1]["attrs"]["trait_hash"], m["trait_hash"])
                    self.assertEqual(a[1]["attrs"]["layers"], m["layers"])
                if kind == "asset":
                    self.assertTrue(om.cas_has(tmp, a[1]["sha256"]))
                    self.assertEqual(a[1]["source_node"], NODE)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_svg_recolour(self):
        svg = b'<svg><rect fill="#ff0000"/><circle stroke="#0f0"/></svg>'
        out = om.recolour_svg(svg, "k")
        self.assertEqual(out, om.recolour_svg(svg, "k"))
        self.assertNotEqual(out, svg)
        self.assertTrue(out.startswith(b"<svg><rect fill=\"#"))

    def test_extract_json_repair(self):
        self.assertEqual(om.extract_json('```json\n{"a": [1,2,],}\n```'), {"a": [1, 2]})
        self.assertEqual(om.extract_json('sure: {"a": "}"} bye'), {"a": "}"})
        self.assertIsNone(om.extract_json("no json"))
        self.assertIsNone(om.extract_json("[1, 2]"))

    def test_llm_backend_with_fake_server(self):
        fake = FakeLLM()
        try:
            st = om.replay(self._sample(), NODE, {OWNER})

            def run(need):
                r = req_event(need)
                rec = {"event_id": r["event_id"], "author": pk(7), "ts": r["ts"], "payload": r["payload"]}
                return om.llm_generate(st, rec, fake.url, "fake-model", 10)

            act, draft, retrieval, notes = run("a story about the river")
            self.assertEqual(act, om.WORLD)
            om.validate_world_entry(draft)
            self.assertEqual(draft["tags"], ["river-lore", "old", "lore"])         # repaired to slugs
            self.assertEqual(draft["attrs"], {"era": "First Age"})                  # unknown attr dropped
            self.assertNotIn("extra", draft)
            self.assertEqual(draft["source"]["generator"], "llm")
            self.assertIn("river-song", retrieval)                                   # retrieval filled the context
            path, body = fake.calls[-1]
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertIn("river-song", body["messages"][1]["content"])
            om.validate_world_entry(run("fenced please")[1])
            act, draft, _, notes = run("retry once")
            self.assertEqual(notes, ["repaired after retry"])
            with self.assertRaises(om.OmniMindError) as cm:
                run("garbage forever")
            self.assertEqual(cm.exception.status, 502)
            with self.assertRaises(om.OmniMindError):
                om.llm_generate(st, {"event_id": "0x" + "9" * 64, "payload": {"kind": "lore", "need": "x"}},
                                "http://127.0.0.1:9", "m", 2)                          # nothing listening
        finally:
            fake.close()

    def test_daemon_registration_and_gate_sets(self):
        d = daemon_module()
        self.assertEqual(d.VERSION, DAEMON_VERSION)
        for p in ("/api/omnimind/world", "/api/omnimind/requests", "/api/v1/omnimind/generate-asset"):
            self.assertIn(p, d.USER_GET_ROUTES)
            self.assertIn(p, d.REMOTE_GET_PATHS)
        self.assertIn("/api/omnimind/world/ember-warden", d.USER_GET_ROUTES)
        self.assertIn("/api/omnimind/world/ember-warden", d.REMOTE_GET_PATHS)
        self.assertNotIn("/api/omnimind/world/a/b", d.USER_GET_ROUTES)
        self.assertIn("/api/omnimind/review", d.USER_GET_ROUTES)
        self.assertNotIn("/api/omnimind/review", d.REMOTE_GET_PATHS)                  # loopback only
        for p in ("/api/omnimind/request", "/api/omnimind/signal", "/api/omnimind/flag"):
            self.assertIn(p, d.REMOTE_POST_PATHS)
        for p in ("/api/omnimind/approve", "/api/omnimind/reject", "/api/omnimind/fulfil"):
            self.assertIn(p, d.USER_POST_ROUTES)
            self.assertNotIn(p, d.REMOTE_POST_PATHS)
        self.assertNotIn("/api/omnimind/telemetry", d.USER_GET_ROUTES)                # telemetry cache untouched
        for a in om.ALL_ACTIONS:
            self.assertIn(a, d.RESERVED_USER_ACTIONS)
        self.assertEqual(set(d.OMNIMIND_ACTIONS), set(om.ALL_ACTIONS))
        self.assertEqual(d.OMNIMIND_ROLE, "mini")
        self.assertFalse(d.OMNIMIND_AUTOPUBLISH)                                      # default: review everything
        self.assertIsNone(d._OMNIMIND_MODULE, "omni_omnimind must not load until the first request")
        self.assertTrue(d.feed_blocked({"post": {"npc": {"feed_injectable": False}}}))
        self.assertTrue(d.feed_blocked({"content": json.dumps({"feed_injectable": False})}))
        self.assertFalse(d.feed_blocked({"content": "hello", "feed_injectable": True}))


# --- Live daemon tests ---------------------------------------------------------------------------------
def _fulfil(d):
    code, r = d.post("/api/omnimind/fulfil", {})
    assert code == 200, r
    return r


class LiveOmniMind(unittest.TestCase):
    """One full node, owners = self, autopublish off (the default: review everything)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="omni_omnimind_"))
        cls.d = Daemon(cls.tmp, "om", env={"OMNI_USER_RATE_MAX": "500", "OMNI_OMNIMIND_ROLE": "full",
                                            "OMNI_OMNIMIND_POLL_S": "3600", "OMNI_OMNIMIND_STARTUP_DELAY": "3600",
                                            "OMNI_OMNIMIND_CAS_DIR": str(cls.tmp / "cas")}).start()
        cls.alice, cls.bob = Key(), Key()

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def request(self, key, need, kind="lore", **extra):
        code, r = self.d.post("/api/omnimind/request", signed(key, om.REQUEST, dict({"kind": kind, "need": need}, **extra)))
        self.assertEqual(code, 200, r)
        return r["event_id"]

    def status(self, rid):
        return next(x["status"] for x in self.d.get("/api/omnimind/requests", {"limit": "200"})[1]["requests"]
                    if x["request_id"] == rid)

    def proposal_for(self, rid):
        _, q = self.d.get("/api/omnimind/review")
        return next(p for p in q["proposals"] if p["request_id"] == rid)

    def test_generate_asset_alias_is_local_and_writes_nothing(self):
        before = self.d.event_count()
        code, r = self.d.get("/api/v1/omnimind/generate-asset", {"sign": "Scorpio", "seed": "7"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["data"], om.zodiac_npc("scorpio", 7))
        self.assertIs(r["data"]["feed_injectable"], False)
        self.assertEqual(self.d.get("/api/v1/omnimind/generate-asset", {"sign": "Ophiuchus"})[0], 400)
        self.assertEqual(self.d.get("/api/v1/omnimind/generate-asset", {"sign": "leo", "seed": "-1"})[0], 400)
        self.assertEqual(self.d.event_count(), before, "generate-asset must not write to the chain")

    def test_request_proposal_approve_flow(self):
        rid = self.request(self.alice, "the founding of the lantern city", tags=["lantern"])
        self.assertIn(rid, [x["request_id"] for x in self.d.get("/api/omnimind/requests", {"status": "open"})[1]["requests"]])
        out = _fulfil(self.d)
        self.assertGreaterEqual(len(out["proposed"]), 1, out)
        self.assertEqual(out["autopublished"], [])
        prop = self.proposal_for(rid)
        self.assertIn("autopublish off (review everything)", prop["why_in_queue"])
        self.assertEqual(prop["generator"]["name"], "template-remix")
        self.assertEqual(self.status(rid), "proposed")
        self.assertEqual(_fulfil(self.d)["proposed"], [], "a request is answered once")
        code, r = self.d.post("/api/omnimind/approve", {"proposal_id": prop["proposal_id"]})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["decision"], "published")
        self.assertEqual(self.d.post("/api/omnimind/approve", {"proposal_id": prop["proposal_id"]})[0], 409)
        eid = prop["draft"]["id"]
        code, w = self.d.get(f"/api/omnimind/world/{eid}")
        self.assertEqual(code, 200, w)
        self.assertEqual(w["entry"]["entry"]["source"]["proposal_id"], prop["proposal_id"])
        self.assertEqual(w["entry"]["history"][0]["version"], 1)
        self.assertEqual(self.d.get(f"/api/omnimind/world/{r['event_id']}")[1]["entry"]["id"], eid)
        code, s = self.d.get("/api/omnimind/world", {"q": "lantern city"})
        self.assertEqual(s["entries"][0]["id"], eid)
        self.assertEqual(self.status(rid), "published")
        self.assertNotIn(prop["proposal_id"], [p["proposal_id"] for p in self.d.get("/api/omnimind/review")[1]["proposals"]])
        # A new version extends it: parent = the published event.
        rid2 = self.request(self.alice, "the lantern city after the flood", extends=eid)
        _fulfil(self.d)
        p2 = self.proposal_for(rid2)
        self.assertEqual((p2["draft"]["id"], p2["draft"]["version"], p2["draft"]["parent"]), (eid, 2, r["event_id"]))
        self.assertEqual(self.d.post("/api/omnimind/approve", {"proposal_id": p2["proposal_id"]})[0], 200)
        hist = self.d.get(f"/api/omnimind/world/{eid}")[1]["entry"]["history"]
        self.assertEqual([h["version"] for h in hist], [1, 2])
        self.assertEqual(hist[1]["parent"], hist[0]["event_id"])

    def test_reject_flow(self):
        rid = self.request(self.bob, "a cursed bell", kind="item")
        _fulfil(self.d)
        prop = self.proposal_for(rid)
        code, r = self.d.post("/api/omnimind/reject", {"proposal_id": prop["proposal_id"], "reason": "low_quality"})
        self.assertEqual(code, 200, r)
        self.assertEqual(self.d.post("/api/omnimind/approve", {"proposal_id": prop["proposal_id"]})[0], 409)
        status = next(x for x in self.d.get("/api/omnimind/requests")[1]["requests"] if x["request_id"] == rid)["status"]
        self.assertEqual(status, "rejected")

    def test_zodiac_npc_request_and_asset_request(self):
        rid = self.request(self.alice, "a guard for the north gate", kind="npc", sign="leo", seed=5)
        _fulfil(self.d)
        prop = self.proposal_for(rid)
        self.assertEqual(prop["generator"]["name"], "zodiac")
        self.assertEqual(prop["draft"]["attrs"]["trait_hash"], om.zodiac_npc("leo", 5)["trait_hash"])
        self.assertIs(prop["draft"]["feed_injectable"], False)
        rid = self.request(self.alice, "a palette for the tide temple", kind="asset")
        _fulfil(self.d)
        prop = self.proposal_for(rid)
        self.assertEqual(prop["publish_action"], om.ASSET)
        self.assertEqual(self.d.post("/api/omnimind/approve", {"proposal_id": prop["proposal_id"]})[0], 200)
        code, a = self.d.get("/api/omnimind/world", {"kind": "asset"})
        sha = prop["draft"]["sha256"]
        mine = next(x for x in a["assets"] if x["sha256"] == sha)
        self.assertTrue(mine["local_file"])
        self.assertEqual(mine["asset"]["source_node"], self.d.node_id)
        self.assertEqual(self.d.get(f"/api/omnimind/world/{sha}")[0], 200)

    def test_signals_flags_and_keep(self):
        rid = self.request(self.alice, "a duelist who never loses", kind="npc", sign="aries", seed=1)
        _fulfil(self.d)
        prop = self.proposal_for(rid)
        code, r = self.d.post("/api/omnimind/approve", {"proposal_id": prop["proposal_id"]})
        self.assertEqual(code, 200, r)
        eid, ev_id = prop["draft"]["id"], r["event_id"]
        players = [Key() for _ in range(3)]
        for i, k in enumerate(players):
            for s in (("pick", "lose_to") if i else ("lose_to",)):     # 5 signals, 3 losses: loss_rate 0.6
                code, x = self.d.post("/api/omnimind/signal", signed(k, om.SIGNAL, {"target": eid, "signal": s,
                                                                                    "consent": om.SIGNAL_CONSENT}))
                self.assertEqual(code, 200, x)
        self.assertEqual(self.d.post("/api/omnimind/signal", signed(players[0], om.SIGNAL, {"target": eid, "signal": "pick"}))[0], 400)
        sig = self.d.get(f"/api/omnimind/world/{eid}")[1]["entry"]["signals"]
        self.assertEqual((sig["counts"]["lose_to"], sig["people"]), (3, 3))
        for k in players:
            self.assertEqual(self.d.post("/api/omnimind/flag", signed(k, om.FLAG, {"target": ev_id, "reason": "spam"}))[0], 200)
        ids = [e["id"] for e in self.d.get("/api/omnimind/world", {"limit": "200"})[1]["entries"]]
        self.assertNotIn(eid, ids, "3 flags hide an entry")
        review = self.d.get("/api/omnimind/review")[1]
        self.assertIn(eid, [e["id"] for e in review["flagged_entries"]])
        self.assertEqual(self.d.post("/api/omnimind/approve", {"target": eid})[0], 200)       # owner keeps it
        ids = [e["id"] for e in self.d.get("/api/omnimind/world", {"limit": "200"})[1]["entries"]]
        self.assertIn(eid, ids)
        # Signals feed generation: players lose to it -> its next version is easier.
        rid2 = self.request(self.alice, "the duelist, a year later", kind="npc", extends=eid)
        _fulfil(self.d)
        p2 = self.proposal_for(rid2)
        self.assertEqual(p2["draft"]["attrs"]["difficulty"], prop["draft"]["attrs"]["difficulty"] - 1)
        self.assertTrue(any(n.startswith("signals:") for n in p2["notes"]))

    def test_forged_and_refused(self):
        good = signed(self.alice, om.REQUEST, {"kind": "lore", "need": "a forged tale"})
        forged = dict(good, payload=dict(good["payload"], need="something else"))
        self.assertEqual(self.d.post("/api/omnimind/request", forged)[0], 401)
        self.assertEqual(self.d.post("/api/omnimind/request", dict(good, signature="0" * 128))[0], 401)
        self.assertEqual(self.d.post("/api/omnimind/request", signed(self.alice, om.REQUEST, {"kind": "lore", "need": "x"},
                                                                     ts=now_ms() - 3_600_000))[0], 400)
        code, r = self.d.post("/api/omnimind/request", signed(self.alice, om.REQUEST, {"kind": "lore", "need": "how to make a bomb"}))
        self.assertEqual(code, 422, r)
        for act, p in ((om.WORLD, dict(entry("sneaky-1"), schema=1)), (om.PROPOSAL, {"schema": 1}), (om.REQUEST, {"schema": 1})):
            code, r = self.d.post("/api/events/append", {"action": act, "payload": p})
            self.assertEqual(code, 403, r)
        # A person who is not an owner can't publish or review, even with a valid signature.
        rid = self.request(self.bob, "a fox spirit of the hills", kind="npc")
        _fulfil(self.d)
        prop = self.proposal_for(rid)
        env = signed(self.bob, om.WORLD, prop["to_sign"]["payload"])
        code, r = self.d.post("/api/omnimind/approve", env)
        self.assertEqual(code, 403, r)
        code, r = self.d.post("/api/omnimind/reject", signed(self.bob, om.REVIEW, {"target": prop["proposal_id"], "decision": "reject"}))
        self.assertEqual(code, 403, r)

    def test_human_feed_policy(self):
        self.request(self.alice, "feed policy check")
        code, r = self.d.post("/api/events/append", {"action": "post", "payload": {"text": "look", "npc": om.zodiac_npc("leo", 1)}})
        self.assertEqual(code, 403, r)
        self.assertIn("Human Feed Policy", r["message"])
        code, r = self.d.post("/api/events/append", {"action": "post", "payload": {"text": json.dumps({"feed_injectable": False})}})
        self.assertEqual(code, 403, r)
        self.assertEqual(self.d.post("/api/events/append", {"action": "post", "payload": {"text": "a human post"}})[0], 200)
        _, feed = self.d.get("/api/events/feed", {"limit": "200"})
        acts = [e["action"] for e in feed["events"]]
        self.assertIn("post", acts)
        self.assertFalse([a for a in acts if a.startswith("omnimind_")], acts)
        _, tools = self.d.get("/api/events/feed", {"type": "omnimind_request"})
        self.assertTrue(tools["events"], "explicit omnimind types stay readable for tools")


class AutopublishAndLlm(unittest.TestCase):
    """Full node with OMNI_OMNIMIND_AUTOPUBLISH=1, the fake LLM, and a gpu capability advertised."""

    def test_autopublish_on_llm_and_gpu_queue(self):
        root = Path(tempfile.mkdtemp(prefix="omni_omnimind_auto_"))
        fake = FakeLLM()
        d = Daemon(root, "omauto", env={"OMNI_OMNIMIND_ROLE": "full", "OMNI_OMNIMIND_AUTOPUBLISH": "1",
                                         "OMNI_OMNIMIND_LLM_URL": fake.url, "OMNI_OMNIMIND_LLM_MODEL": "fake-model",
                                         "OMNI_OMNIMIND_POLL_S": "3600", "OMNI_OMNIMIND_STARTUP_DELAY": "3600",
                                         "OMNI_OMNIMIND_CAS_DIR": str(root / "cas"), "OMNI_OMNIMIND_CHECKPOINT_EVERY": "1"})
        try:
            d.start()
            k = Key()
            ids = {}
            for need, extra in (("a song of the river", {}), ("a link to lore", {}), ("garbage forever", {}),
                                ("a 3d statue of the sun king", {"needs_gpu": True})):
                code, r = d.post("/api/omnimind/request", signed(k, om.REQUEST, dict({"kind": "lore", "need": need}, **extra)))
                self.assertEqual(code, 200, r)
                ids[need] = r["event_id"]
            out = _fulfil(d)
            self.assertEqual(len(out["proposed"]), 3, out)
            self.assertEqual(len(out["autopublished"]), 2, out)       # clean LLM output + template fallback
            self.assertTrue(out["checkpoint"])
            reqs = {x["request_id"]: x for x in d.get("/api/omnimind/requests")[1]["requests"]}
            self.assertEqual(reqs[ids["a song of the river"]]["status"], "published")
            self.assertEqual(reqs[ids["a song of the river"]]["proposals"][0]["generator"], "llm")
            self.assertEqual(reqs[ids["a link to lore"]]["status"], "proposed")        # safety hit: stays in review
            self.assertEqual(reqs[ids["garbage forever"]]["status"], "published")
            self.assertEqual(reqs[ids["garbage forever"]]["proposals"][0]["generator"], "template-remix")
            self.assertEqual(reqs[ids["a 3d statue of the sun king"]]["status"], "waiting_gpu")
            q = d.get("/api/omnimind/review")[1]
            flagged = next(p for p in q["proposals"] if p["request_id"] == ids["a link to lore"])
            self.assertTrue(flagged["flagged"])
            self.assertIn("link", flagged["safety"]["hits"])
            w = d.get("/api/omnimind/world")[1]
            self.assertEqual(w["entries_total"], 2)
            self.assertTrue(w["checkpoint"]["verified"])
            self.assertEqual(w["checkpoint"]["root"], w["world_root"])
            self.assertTrue(any(c[0] == "/v1/chat/completions" for c in fake.calls))
        finally:
            d.stop()
        # Same node, restarted advertising a GPU: the needs_gpu request becomes claimable, still not run here.
        d.env["OMNI_OMNIMIND_GPU"] = "1"
        try:
            d.start()
            _fulfil(d)
            reqs = {x["request_id"]: x for x in d.get("/api/omnimind/requests")[1]["requests"]}
            self.assertEqual(reqs[ids["a 3d statue of the sun king"]]["status"], "gpu_available")
            self.assertEqual(reqs[ids["a 3d statue of the sun king"]]["proposals"], [])
            self.assertEqual(d.get("/api/omnimind/requests")[1]["node"]["gpu_nodes"], 1)
        finally:
            d.stop()
            fake.close()
            shutil.rmtree(root, ignore_errors=True)


class OwnerKeys(unittest.TestCase):
    def test_owner_signed_approval_and_non_owner_node(self):
        root = Path(tempfile.mkdtemp(prefix="omni_omnimind_owner_"))
        owner = Key()
        d = Daemon(root, "omown", env={"OMNI_OMNIMIND_ROLE": "full", "OMNI_OMNIMIND_OWNERS": owner.pub,
                                        "OMNI_OMNIMIND_POLL_S": "3600", "OMNI_OMNIMIND_STARTUP_DELAY": "3600",
                                        "OMNI_OMNIMIND_CAS_DIR": str(root / "cas")})
        try:
            d.start()
            user = Key()
            code, r = d.post("/api/omnimind/request", signed(user, om.REQUEST, {"kind": "place", "need": "a quiet harbour"}))
            self.assertEqual(code, 200, r)
            _fulfil(d)
            prop = d.get("/api/omnimind/review")[1]["proposals"][0]
            code, x = d.post("/api/omnimind/approve", {"proposal_id": prop["proposal_id"]})
            self.assertEqual(code, 403, x)                                  # node key isn't an owner here
            code, x = d.post("/api/omnimind/approve", signed(owner, prop["to_sign"]["action"], prop["to_sign"]["payload"]))
            self.assertEqual(code, 200, x)
            w = d.get(f"/api/omnimind/world/{prop['draft']['id']}")[1]["entry"]
            self.assertEqual(w["author"], owner.pub)
        finally:
            d.stop()
            shutil.rmtree(root, ignore_errors=True)


class TwoNodeSync(unittest.TestCase):
    """A mini node (phone) takes a request; the full node (desktop) pulls it over the existing catch-up
    sync, fulfils it and publishes; the mini node gets the proposal and the world entry back."""

    def test_two_daemons_sync_request_and_fulfilment(self):
        root = Path(tempfile.mkdtemp(prefix="omni_omnimind_mesh_"))
        phone = Daemon(root, "ommini", env={"OMNI_MESH_ID": "om-mesh"})
        desk = Daemon(root, "omfull", env={"OMNI_MESH_ID": "om-mesh", "OMNI_OMNIMIND_ROLE": "full",
                                            "OMNI_OMNIMIND_POLL_S": "3600", "OMNI_OMNIMIND_STARTUP_DELAY": "3600",
                                            "OMNI_OMNIMIND_CAS_DIR": str(root / "cas"),
                                            "OMNI_SYNC_PEERS": f"127.0.0.1:{phone.ports['OMNI_P2P_PORT']}"})
        try:
            desk.start()
            phone.env["OMNI_OMNIMIND_OWNERS"] = f"self,{desk.node_id}"     # the phone trusts the desktop's publications
            phone.start()
            k = Key()
            code, r = phone.post("/api/omnimind/request", signed(k, om.REQUEST, {"kind": "npc", "need": "a ferryman",
                                                                                 "sign": "pisces", "seed": 3}))
            self.assertEqual(code, 200, r)
            rid = r["event_id"]
            self.assertEqual(phone.post("/api/omnimind/fulfil", {})[0], 409, "mini nodes never generate")

            def until(fn, what):
                deadline = time.time() + 30
                while time.time() < deadline:
                    desk.post("/api/sync/catchup", {})
                    v = fn()
                    if v:
                        return v
                    time.sleep(0.5)
                self.fail(what)

            until(lambda: any(x["request_id"] == rid for x in desk.get("/api/omnimind/requests")[1]["requests"]),
                  "request did not reach the full node")
            out = _fulfil(desk)
            self.assertEqual(len(out["proposed"]), 1, out)
            st = until(lambda: next((x["status"] for x in phone.get("/api/omnimind/requests")[1]["requests"]
                                     if x["request_id"] == rid and x["status"] == "proposed"), None),
                       "proposal did not reach the phone")
            self.assertEqual(st, "proposed")
            prop = desk.get("/api/omnimind/review")[1]["proposals"][0]
            code, x = desk.post("/api/omnimind/approve", {"proposal_id": prop["proposal_id"]})
            self.assertEqual(code, 200, x)
            eid = prop["draft"]["id"]
            until(lambda: phone.get(f"/api/omnimind/world/{eid}")[0] == 200, "world entry did not reach the phone")
            a, b = phone.get("/api/omnimind/world")[1], desk.get("/api/omnimind/world")[1]
            self.assertEqual(a["world_root"], b["world_root"])
            self.assertEqual(phone.get("/api/omnimind/world/" + eid)[1]["entry"]["entry"]["attrs"]["trait_hash"],
                             om.zodiac_npc("pisces", 3)["trait_hash"])
        finally:
            phone.stop()
            desk.stop()
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
