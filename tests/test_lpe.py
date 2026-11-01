"""Daemon v8.25.0: Live Persona Engine (Admin/omni_lpe.py + /api/lpe/* on :5000).

Covers replay determinism, the progress-claim cap, the vibe rate limit, forged/unsigned events being
refused, the trust score converging on a small graph, the remote/local-token gate, and two daemons
syncing LPE events over the existing catch-up path.

Run: python3 -m unittest tests.test_lpe
"""
import ast
import json
import random
import secrets
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daemon_harness import ADMIN, Daemon, Key, daemon_module, http, now_ms  # noqa: E402

import omni_lpe as lpe  # noqa: E402  (daemon_harness put Admin/ on sys.path)

ZERO = "0x" + "0" * 64
NODE = "f" * 64


def pk(i: int) -> str:
    return f"{i:064x}"


_n = [0]


def ev(action, author, ts, payload) -> dict:
    """An already-verified event as the daemon hands it to omni_lpe.replay()."""
    _n[0] += 1
    return {"event_id": "0x%064x" % _n[0], "action": action, "author_pubkey": author, "ts": ts,
            "payload": dict(payload, schema=1)}


def signed(key: Key, action: str, payload: dict, ts: int = None, prev: str = ZERO) -> dict:
    return key.sign_event({"action": action, "author_pubkey": key.pub, "payload": dict(payload, schema=1),
                           "previous_hash": prev, "ts": now_ms() if ts is None else ts})


def canon(state: dict) -> str:
    def fix(o):
        if isinstance(o, dict):
            return {(json.dumps(k) if isinstance(k, tuple) else k): fix(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [fix(x) for x in o]
        return o
    return json.dumps(fix(state), sort_keys=True)


class Unit(unittest.TestCase):
    def test_stdlib_only(self):
        """Chaquopy: omni_lpe imports nothing outside the standard library."""
        tree = ast.parse((ADMIN / "omni_lpe.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertTrue(names <= {"hashlib", "json", "math", "re", "threading", "urllib"}, names)

    def test_android_packager_finds_the_module(self):
        """Omni-Node-Daemon's fetch_daemon.sh copies modules found by grep -E '^(import|from) omni_...'."""
        import re
        text = (ADMIN / "omni_live_daemon_server.py").read_text(encoding="utf-8")
        self.assertIn("omni_lpe", re.findall(r"^(?:import|from) (omni_[a-z0-9_]+)", text, re.M))

    def test_validators_strict(self):
        lpe.validate_profile({"schema": 1, "handle": "kiro_b", "avatar": "default_01",
                              "cosmetics": ["head_socket_crown"], "ambient_activity": "Training"})
        bad = [
            (lpe.validate_profile, {"schema": 1, "handle": "@kiro"}),
            (lpe.validate_profile, {"schema": 1, "handle": "kiro", "extra": 1}),
            (lpe.validate_profile, {"schema": 2, "handle": "kiro"}),
            (lpe.validate_profile, {"schema": True, "handle": "kiro"}),
            (lpe.validate_profile, {"schema": 1, "handle": "kiro", "cosmetics": ["a"] * 13}),
            (lpe.validate_profile, {"schema": 1, "handle": "kiro", "cosmetics": ["a", "a"]}),
            (lpe.validate_profile, {"schema": 1, "handle": "kiro", "ambient_activity": " x"}),
            (lpe.validate_profile, {"schema": 1, "handle": "kiro", "avatar": "x" * 201}),
            (lpe.validate_vibe, {"schema": 1, "target": "nope"}),
            (lpe.validate_progress, {"schema": 1, "elapsed_s": 0}),
            (lpe.validate_progress, {"schema": 1, "elapsed_s": lpe.MAX_CLAIM_S + 1}),
            (lpe.validate_progress, {"schema": 1, "elapsed_s": 1.5}),
            (lpe.validate_vote, {"schema": 1, "target": pk(1), "rating": 6}),
            (lpe.validate_vote, {"schema": 1, "target": pk(1), "rating": True}),
            (lpe.validate_quarantine, {"schema": 1, "target": pk(1), "decision": "rehab"}),
            (lpe.validate_quarantine, {"schema": 1, "target": pk(1), "decision": "ban"}),
            (lpe.validate_vote, []),
        ]
        for fn, p in bad:
            with self.assertRaises(ValueError, msg=repr(p)):
                fn(p)

    def _sample_events(self):
        t0 = 1_800_000_000_000
        a, b, c = pk(1), pk(2), pk(3)
        evs = [ev(lpe.PROFILE, k, t0 + i, {"handle": f"user_{i}"}) for i, k in enumerate((a, b, c))]
        evs += [ev(lpe.PROGRESS, a, t0 + 600_000, {"elapsed_s": 300}),
                ev(lpe.PROGRESS, a, t0 + 900_000, {"elapsed_s": 300}),
                ev(lpe.VIBE, b, t0 + 1000, {"target": a}), ev(lpe.VIBE, c, t0 + 2000, {"target": a}),
                ev(lpe.VOTE, a, t0 + 10, {"target": b, "rating": 5}), ev(lpe.VOTE, b, t0 + 11, {"target": a, "rating": 4}),
                ev(lpe.VOTE, c, t0 + 12, {"target": a, "rating": -2}), ev(lpe.VOTE, a, t0 + 13, {"target": c, "rating": 1}),
                ev(lpe.QUARANTINE, NODE, t0 + 14, {"target": c, "decision": "quarantine", "reason": "spam"})]
        return evs

    def test_replay_is_deterministic_in_any_order(self):
        evs = self._sample_events()
        ref = canon(lpe.replay(evs, NODE))
        rng = random.Random(7)
        for _ in range(20):
            shuffled = list(evs)
            rng.shuffle(shuffled)
            self.assertEqual(canon(lpe.replay(shuffled + shuffled[:3], NODE)), ref)   # duplicates too

    def test_progress_claims_bounded_by_real_time(self):
        t0, a = 1_800_000_000_000, pk(1)
        st = lpe.replay([ev(lpe.PROGRESS, a, t0 - 1, {"elapsed_s": 100}),           # before profile: ignored
                         ev(lpe.PROFILE, a, t0, {"handle": "alice"}),
                         ev(lpe.PROGRESS, a, t0 + 60_000, {"elapsed_s": 3600}),     # claims 1h after 1 min
                         ev(lpe.PROGRESS, a, t0 + 60_000, {"elapsed_s": 50}),       # same instant: 0 s left
                         ev(lpe.PROGRESS, a, t0 + 180_000, {"elapsed_s": 120})], NODE)
        p = st["progress"][a]
        self.assertEqual(p["claimed_s"], 60 + 120)
        self.assertEqual(p["xp"], 10 + 20)                  # BASE_XP_PER_MINUTE = 10
        self.assertEqual(p["plaza_credits"], 2 + 4)
        self.assertEqual(st["ignored"]["claim_no_profile"], 1)

    def test_vibe_rate_limit_per_sender_target_day(self):
        t0 = (1_800_000_000_000 // lpe.DAY_MS) * lpe.DAY_MS
        a, b = pk(1), pk(2)
        evs = [ev(lpe.PROFILE, a, t0, {"handle": "alice"}), ev(lpe.PROFILE, b, t0, {"handle": "bob"})]
        evs += [ev(lpe.VIBE, b, t0 + 1000 + i, {"target": a}) for i in range(5)]          # day 0: 3 count
        evs += [ev(lpe.VIBE, b, t0 + lpe.DAY_MS + i, {"target": a}) for i in range(2)]    # day 1: 2 count
        evs += [ev(lpe.VIBE, a, t0 + 5, {"target": a}), ev(lpe.VIBE, a, t0 + 6, {"target": pk(9)})]
        st = lpe.replay(evs, NODE)
        self.assertEqual(st["vibes"][a]["count"], 5)
        self.assertEqual(st["ignored"]["vibe_rate"], 2)
        self.assertEqual(st["ignored"]["vibe_self"], 1)
        self.assertEqual(st["ignored"]["vibe_no_target"], 1)
        view = lpe.avatar_view(st, a)
        self.assertEqual(view["xp"], 5 * lpe.VIBE_BONUS_XP)

    def test_quarantine_only_from_own_node(self):
        t0, a = 1_800_000_000_000, pk(1)
        evs = [ev(lpe.PROFILE, a, t0, {"handle": "alice"}),
               ev(lpe.QUARANTINE, pk(77), t0 + 1, {"target": a, "decision": "quarantine"})]
        st = lpe.replay(evs, NODE)
        self.assertFalse(lpe.reputation_view(st, a)["quarantine"]["manual"])
        self.assertEqual(st["ignored"]["quarantine_foreign"], 1)
        st = lpe.replay(evs + [ev(lpe.QUARANTINE, NODE, t0 + 2, {"target": a, "decision": "quarantine"})], NODE)
        self.assertEqual(lpe.reputation_view(st, a)["quarantine"]["status_label"], "DISTRUSTED_QUARANTINED")

    def test_trust_converges_on_small_graph(self):
        honest = [pk(i) for i in range(1, 5)]
        sybils = [pk(i) for i in range(10, 13)]
        votes = {}
        for i in honest:
            for j in honest:
                if i != j:
                    votes[(i, j)] = 4
        for i in sybils:                               # a sybil ring vouching only for itself
            for j in sybils:
                if i != j:
                    votes[(i, j)] = 5
        votes[(honest[0], honest[3])] = 5
        votes[(honest[1], sybils[0])] = -5             # an honest key flags one sybil
        r = lpe.compute_trust(votes, seeds=set(honest))
        self.assertTrue(r["converged"])
        self.assertTrue(r["seeded"])
        self.assertLess(r["iterations"], lpe.TRUST_MAX_ITER)
        self.assertAlmostEqual(sum(r["trust"].values()), 1.0, places=9)
        for h in honest:
            self.assertGreaterEqual(r["scores"][h], 200, h)        # CITIZEN or better
            for s in sybils:
                self.assertGreater(r["scores"][h], r["scores"][s])
        self.assertLess(r["scores"][sybils[0]], 0)                  # flagged sybil: SLASHED
        self.assertEqual(lpe.reputation_tier(r["scores"][sybils[0]])[0], lpe.TIER_SLASHED)
        self.assertEqual(r, lpe.compute_trust(dict(reversed(list(votes.items()))), seeds=set(honest)))
        # No seeds -> uniform pre-trust; a symmetric ring scores 500 each (exactly average)
        ring = {(pk(1), pk(2)): 3, (pk(2), pk(3)): 3, (pk(3), pk(1)): 3}
        r2 = lpe.compute_trust(ring)
        self.assertFalse(r2["seeded"])
        self.assertEqual(set(r2["scores"].values()), {500})
        self.assertEqual(lpe.compute_trust({})["scores"], {})

    def test_tiers_and_resonance_match_the_engines(self):
        self.assertEqual([lpe.reputation_tier(s)[0] for s in (950, 500, 120, -150)],
                         ["PARAGON", "CITIZEN", "DISTRUSTED", "SLASHED"])
        self.assertEqual(lpe.proximity_resonance(950, 900)["bonus_multiplier"], 0.10)
        self.assertEqual(lpe.proximity_resonance(950, 120)["interaction"], "DEFENSIVE_BARRIER")
        self.assertEqual(lpe.proximity_resonance(500, 900)["bonus_multiplier"], 0.05)
        self.assertEqual(lpe.quarantine_status(-50, False)["status_label"], "SLASHED_BANNED")
        self.assertEqual(lpe.quarantine_status(220, False)["assigned_topic"], lpe.TOPIC_PUBLIC_FEED)

    def test_daemon_registration_and_gate_sets(self):
        d = daemon_module()
        self.assertEqual(d.VERSION, "8.25.0")
        for p in ("/api/lpe/profile", "/api/lpe/plaza", "/api/lpe/reputation"):
            self.assertIn(p, d.USER_GET_ROUTES)
            self.assertIn(p, d.REMOTE_GET_PATHS)
        for p in ("/api/lpe/profile", "/api/lpe/vibe", "/api/lpe/progress", "/api/lpe/vote"):
            self.assertIn(p, d.USER_POST_ROUTES)
            self.assertIn(p, d.REMOTE_POST_PATHS)
        self.assertIn("/api/lpe/quarantine", d.USER_POST_ROUTES)
        self.assertNotIn("/api/lpe/quarantine", d.REMOTE_POST_PATHS)      # loopback only, like credits/grant
        for a in lpe.ALL_ACTIONS:
            self.assertIn(a, d.RESERVED_USER_ACTIONS)
        self.assertEqual(set(d.LPE_ACTIONS), set(lpe.ALL_ACTIONS))
        self.assertIsNone(d._LPE_MODULE, "omni_lpe must not load until the first /api/lpe request")


class LiveLpe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="omni_lpe_"))
        cls.d = Daemon(cls.tmp, "lpe", env={"OMNI_USER_RATE_MAX": "200"}).start()
        cls.alice, cls.bob, cls.carol = Key(), Key(), Key()
        for k, h in ((cls.alice, "alice"), (cls.bob, "bob_b"), (cls.carol, "carol")):
            code, r = cls.d.post("/api/lpe/profile", signed(k, "lpe_profile", {"handle": h}, ts=now_ms() - 120_000))
            assert code == 200, r

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_profile_roundtrip(self):
        code, r = self.d.post("/api/lpe/profile", signed(self.alice, "lpe_profile", {
            "handle": "alice", "avatar": "default_f_02", "cosmetics": ["head_socket_crown"],
            "ambient_activity": "Meditating by the fountain"}))
        self.assertEqual(code, 200, r)
        code, r = self.d.get("/api/lpe/profile", {"pubkey": self.alice.pub})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["profile"]["cosmetics"], ["head_socket_crown"])
        self.assertEqual(r["profile"]["handle_display"], "@alice." + self.alice.pub[:4])
        self.assertIn(r["profile"]["aura"]["tier"], ("CITIZEN", "PARAGON", "DISTRUSTED", "SLASHED"))
        self.assertEqual(self.d.get("/api/lpe/profile", {"pubkey": "0" * 64})[0], 404)
        self.assertEqual(self.d.get("/api/lpe/profile", {"pubkey": "zz"})[0], 400)
        self.assertEqual(self.d.get("/api/lpe/profile", {"pubkey": self.alice.pub, "x": "1"})[0], 400)

    def test_forged_unsigned_and_raw_append_rejected(self):
        good = signed(self.alice, "lpe_reputation_vote", {"target": self.bob.pub, "rating": 3})
        forged = dict(good, payload=dict(good["payload"], rating=5))              # tampered after signing
        forged.pop("event_id")
        self.assertEqual(self.d.post("/api/lpe/vote", forged)[0], 401)
        other = signed(self.carol, "lpe_reputation_vote", {"target": self.bob.pub, "rating": 3})
        stolen = dict(good, signature=other["signature"])                          # someone else's signature
        stolen.pop("event_id")
        self.assertEqual(self.d.post("/api/lpe/vote", stolen)[0], 401)
        unsigned = {k: v for k, v in good.items() if k not in ("signature", "event_id")}
        self.assertEqual(self.d.post("/api/lpe/vote", unsigned)[0], 400)
        stale = signed(self.alice, "lpe_reputation_vote", {"target": self.bob.pub, "rating": 3}, ts=now_ms() - 3_600_000)
        self.assertEqual(self.d.post("/api/lpe/vote", stale)[0], 400)
        self.assertEqual(self.d.post("/api/lpe/vote", dict(good, extra=1))[0], 400)
        code, r = self.d.post("/api/events/append", {"action": "lpe_profile", "payload": {"schema": 1, "handle": "evil"}})
        self.assertEqual(code, 403, r)
        code, r = http(f"{self.d.ipc}/api/lpe/vote", raw=b"{" + b" " * 5000 + b"}")
        self.assertEqual(code, 413, r)
        self.assertEqual(http(f"{self.d.ipc}/api/lpe/vote", raw=b"not json")[0], 400)

    def test_progress_claim_cap(self):
        k = Key()
        t0 = now_ms() - 120_000
        self.assertEqual(self.d.post("/api/lpe/profile", signed(k, "lpe_profile", {"handle": "idler"}, ts=t0))[0], 200)
        t1 = now_ms()
        code, r = self.d.post("/api/lpe/progress", signed(k, "lpe_progress_claim", {"elapsed_s": 60}, ts=t1))
        self.assertEqual(code, 200, r)
        self.assertEqual(r["xp_gained"], 10)
        code, r = self.d.post("/api/lpe/progress", signed(k, "lpe_progress_claim", {"elapsed_s": 600}, ts=t1 + 5000))
        self.assertEqual(code, 409, r)
        self.assertEqual(r["max_claimable_s"], 5)
        code, r = self.d.post("/api/lpe/progress", signed(k, "lpe_progress_claim", {"elapsed_s": 5}, ts=t1 + 5000))
        self.assertEqual(code, 200, r)
        code, r = self.d.get("/api/lpe/profile", {"pubkey": k.pub})
        self.assertEqual(r["profile"]["idle_seconds_claimed"], 65)
        nobody = Key()
        self.assertEqual(self.d.post("/api/lpe/progress", signed(nobody, "lpe_progress_claim", {"elapsed_s": 1}))[0], 404)

    def test_vibe_rate_limit_and_idempotent_retry(self):
        sender = Key()
        self.assertEqual(self.d.post("/api/lpe/profile", signed(sender, "lpe_profile", {"handle": "fan"}))[0], 200)
        t = now_ms()
        first = signed(sender, "lpe_vibe_boost", {"target": self.carol.pub}, ts=t)
        code, r = self.d.post("/api/lpe/vibe", first)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["vibes_left_today"], 2)
        self.assertEqual(self.d.post("/api/lpe/vibe", first)[1].get("result"), "ALREADY_EXISTS")
        for i in (1, 2):
            code, r = self.d.post("/api/lpe/vibe", signed(sender, "lpe_vibe_boost", {"target": self.carol.pub}, ts=t + i))
            self.assertEqual(code, 200, r)
        code, r = self.d.post("/api/lpe/vibe", signed(sender, "lpe_vibe_boost", {"target": self.carol.pub}, ts=t + 3))
        self.assertEqual(code, 429, r)
        self.assertEqual(self.d.post("/api/lpe/vibe", signed(sender, "lpe_vibe_boost", {"target": sender.pub}))[0], 400)
        self.assertEqual(self.d.post("/api/lpe/vibe", signed(sender, "lpe_vibe_boost", {"target": "1" * 64}))[0], 404)
        code, r = self.d.get("/api/lpe/plaza", {"viewer": sender.pub})
        self.assertEqual(code, 200, r)
        carol = next(a for a in r["avatars"] if a["pubkey"] == self.carol.pub)
        self.assertGreaterEqual(carol["vibe_count"], 3)
        self.assertEqual(carol["vibes_left_today"], 0)
        self.assertIn("resonance", carol)

    def test_votes_reputation_and_local_quarantine(self):
        for a, b in ((self.alice, self.bob), (self.bob, self.alice), (self.carol, self.alice)):
            code, r = self.d.post("/api/lpe/vote", signed(a, "lpe_reputation_vote", {"target": b.pub, "rating": 4}))
            self.assertEqual(code, 200, r)
        self.assertEqual(self.d.post("/api/lpe/vote", signed(self.alice, "lpe_reputation_vote",
                                                              {"target": self.alice.pub, "rating": 5}))[0], 400)
        code, r = self.d.get("/api/lpe/reputation", {"pubkey": self.alice.pub})
        self.assertEqual(code, 200, r)
        rep = r["reputation"]
        self.assertTrue(rep["rated"])
        self.assertTrue(rep["model"]["pre_alpha"])
        self.assertTrue(rep["model"]["converged"])
        self.assertGreaterEqual(rep["votes_received"]["up"], 2)
        code, r = self.d.post("/api/lpe/quarantine", {"target": self.bob.pub, "decision": "quarantine", "reason": "test"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["reputation"]["quarantine"]["status_label"], "DISTRUSTED_QUARANTINED")
        code, r = self.d.post("/api/lpe/vibe", signed(self.bob, "lpe_vibe_boost", {"target": self.alice.pub}))
        self.assertEqual(code, 403, r)
        self.assertEqual(self.d.post("/api/lpe/quarantine", {"target": self.bob.pub, "decision": "release"})[0], 200)
        self.assertEqual(self.d.post("/api/lpe/quarantine", {"target": self.bob.pub, "decision": "ban"})[0], 400)
        code, r = self.d.post("/api/lpe/vibe", signed(self.bob, "lpe_vibe_boost", {"target": self.alice.pub}))
        self.assertEqual(code, 200, r)


class LocalTokenGate(unittest.TestCase):
    def test_lpe_endpoints_need_the_local_token(self):
        root = Path(tempfile.mkdtemp(prefix="omni_lpe_tok_"))
        tok_file = root / "local_token"
        tok = secrets.token_urlsafe(32)
        tok_file.write_text(tok + "\n", encoding="utf-8")
        d = Daemon(root, "lpetok", env={"OMNI_LOCAL_TOKEN": "1", "OMNI_LOCAL_TOKEN_FILE": str(tok_file)})
        d.start_headers = {"X-Omni-Local-Token": tok}
        try:
            d.start()
            self.assertEqual(d.get("/api/lpe/plaza")[0], 401)
            k = Key()
            self.assertEqual(d.post("/api/lpe/profile", signed(k, "lpe_profile", {"handle": "tok"}))[0], 401)
            code, r = d.get("/api/lpe/plaza", headers=d.start_headers)
            self.assertEqual(code, 200, r)
            code, r = d.get("/api/lpe/plaza", headers={"Authorization": "Bearer " + tok})
            self.assertEqual(code, 200, r)
        finally:
            d.stop()
            shutil.rmtree(root, ignore_errors=True)


class TwoNodeSync(unittest.TestCase):
    """LPE events written on node A reach node B over the existing catch-up sync (no new ports), and
    B replays them to the same state. A's quarantine decision syncs as data but never applies on B."""

    def test_two_daemons_sync_lpe(self):
        root = Path(tempfile.mkdtemp(prefix="omni_lpe_mesh_"))
        a = Daemon(root, "lpea", env={"OMNI_MESH_ID": "lpe-mesh"})
        b = Daemon(root, "lpeb", env={"OMNI_MESH_ID": "lpe-mesh",
                                      "OMNI_SYNC_PEERS": f"127.0.0.1:{a.ports['OMNI_P2P_PORT']}"})
        try:
            a.start()
            b.start()
            u1, u2 = Key(), Key()
            t = now_ms() - 60_000
            for k, h in ((u1, "mesh_one"), (u2, "mesh_two")):
                self.assertEqual(a.post("/api/lpe/profile", signed(k, "lpe_profile", {"handle": h}, ts=t))[0], 200)
            self.assertEqual(a.post("/api/lpe/vibe", signed(u1, "lpe_vibe_boost", {"target": u2.pub}))[0], 200)
            self.assertEqual(a.post("/api/lpe/vote", signed(u1, "lpe_reputation_vote", {"target": u2.pub, "rating": 5}))[0], 200)
            self.assertEqual(a.post("/api/lpe/progress", signed(u2, "lpe_progress_claim", {"elapsed_s": 30}))[0], 200)
            self.assertEqual(a.post("/api/lpe/quarantine", {"target": u1.pub, "decision": "quarantine"})[0], 200)
            deadline = time.time() + 30
            while True:
                code, rb = b.get("/api/lpe/plaza")
                if code == 200 and rb["known_avatars"] == 2 or time.time() > deadline:
                    break
                b.post("/api/sync/catchup", {})
                time.sleep(0.5)
            self.assertEqual(rb["known_avatars"], 2, rb)
            _, ra = a.get("/api/lpe/plaza")
            strip = lambda r: {x["pubkey"]: {k: v for k, v in x.items() if k != "quarantine"} for x in r["avatars"]}
            self.assertEqual(strip(ra), strip(rb))
            two = next(x for x in rb["avatars"] if x["pubkey"] == u2.pub)
            self.assertEqual(two["vibe_count"], 1)
            self.assertEqual(two["idle_seconds_claimed"], 30)
            qa = a.get("/api/lpe/reputation", {"pubkey": u1.pub})[1]["reputation"]["quarantine"]
            qb = b.get("/api/lpe/reputation", {"pubkey": u1.pub})[1]["reputation"]["quarantine"]
            self.assertTrue(qa["manual"])
            self.assertFalse(qb["manual"], "A's local quarantine decision must not apply on B")
            self.assertEqual(a.get("/api/lpe/reputation", {"pubkey": u2.pub})[1]["reputation"]["score"],
                             b.get("/api/lpe/reputation", {"pubkey": u2.pub})[1]["reputation"]["score"])
        finally:
            a.stop()
            b.stop()
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
