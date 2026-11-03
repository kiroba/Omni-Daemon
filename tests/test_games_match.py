"""Game match results (daemon v8.28.0): POST /api/games/match, GET /api/games/matches, node-health
"features". Run: python3 -m unittest tests.test_games_match

Starts real daemons on free temp ports (tests/daemon_harness.py). The body Plaza Brawler sends is
MatchShare.payloadFrom(...) from kiroba/Plaza-Brawler lib/net/match_share.dart (spec 5.3).
"""
import secrets
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daemon_harness import Daemon, Key, daemon_module, http, now_ms  # noqa: E402
from daemon_version import DAEMON_VERSION  # noqa: E402

# Exactly what MatchShare.payloadFrom builds (score is optional; Dart keeps null-valued keys).
PB_PAYLOAD = {"game": "plaza_brawler", "mode": "arcade", "p1_sign": "aries", "p2_sign": "scorpio",
              "p2_source": "omnimind", "winner": "p1", "rounds": 2, "score": 1200, "duration_ms": 61000,
              "app_version": "5.4.0"}
ZERO = "0x" + "0" * 64


def signed_match(key: Key, payload: dict, ts: int = None) -> dict:
    return key.sign_event({"action": "game_match", "author_pubkey": key.pub, "previous_hash": ZERO,
                           "ts": ts or now_ms(), "payload": payload})


class Validation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = daemon_module()

    def test_plaza_brawler_payload_accepted(self):
        out = self.m.validate_game_match(dict(PB_PAYLOAD))
        self.assertEqual(out, dict(PB_PAYLOAD, schema=1))

    def test_nulls_are_absent(self):
        p = dict(PB_PAYLOAD, p1_sign=None, p2_source=None, duration_ms=None)
        p.pop("score")
        out = self.m.validate_game_match(p)
        for k in ("p1_sign", "p2_source", "duration_ms", "score"):
            self.assertNotIn(k, out)

    def test_minimal(self):
        self.assertEqual(self.m.validate_game_match({"game": "plaza_brawler", "winner": "draw"}),
                         {"game": "plaza_brawler", "winner": "draw", "schema": 1})

    def test_ids_lowercased(self):
        self.assertEqual(self.m.validate_game_match({"game": "Plaza_Brawler", "winner": "p2"})["game"],
                         "plaza_brawler")

    def test_rejections(self):
        bad = [
            "not a dict",
            {"winner": "p1"},                                   # game required
            {"game": "plaza_brawler"},                          # winner required
            dict(PB_PAYLOAD, winner="p3"),
            dict(PB_PAYLOAD, rounds=-1),
            dict(PB_PAYLOAD, rounds=100),
            dict(PB_PAYLOAD, rounds=True),
            dict(PB_PAYLOAD, rounds="2"),
            dict(PB_PAYLOAD, duration_ms=1.5),
            dict(PB_PAYLOAD, score=10 ** 10),
            dict(PB_PAYLOAD, game="plaza brawler"),
            dict(PB_PAYLOAD, mode="x" * 33),
            dict(PB_PAYLOAD, app_version="v" * 33),
            dict(PB_PAYLOAD, schema=2),
            dict(PB_PAYLOAD, token="secret"),                   # unknown keys never stored
        ]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(ValueError):
                self.m.validate_game_match(b)


class GamesMatchApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="omni_games_"))
        cls.d = Daemon(cls.tmp, "games").start()

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_1_feature_flag_and_version(self):
        code, body = http(f"{self.d.tel}/api/v1/telemetry/node-health")
        self.assertEqual(code, 200)
        self.assertEqual(body["version"], DAEMON_VERSION)
        self.assertIn("games_match", body["features"])

    def test_2_post_list_newest_first(self):
        ids = []
        for i in range(3):
            code, r = self.d.post("/api/games/match", dict(PB_PAYLOAD, score=i))
            self.assertEqual(code, 200, r)
            self.assertEqual(r["status"], "SUCCESS")
            self.assertEqual(r["signed_by"], "node")
            self.assertRegex(r["event_id"], r"^0x[0-9a-f]{64}$")
            ids.append(r["event_id"])
        code, r = self.d.get("/api/games/matches", {"limit": 2})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["count"], 2)
        self.assertTrue(r["has_more"])
        self.assertEqual([m["event_id"] for m in r["matches"]], ids[::-1][:2])
        m = r["matches"][0]
        self.assertEqual(m["match"], dict(PB_PAYLOAD, score=2, schema=1))
        self.assertTrue(m["verified"])
        self.assertEqual(m["signed_by"], "node")
        self.assertEqual(m["author_pubkey"], self.d.node_id)
        code, r2 = self.d.get("/api/games/matches", {"limit": 2, "before": r["next_before"]})
        self.assertEqual(code, 200)
        self.assertIn(ids[0], [x["event_id"] for x in r2["matches"]])

    def test_3_game_filter(self):
        self.assertEqual(self.d.post("/api/games/match", {"game": "idle_plaza", "winner": "draw"})[0], 200)
        _, r = self.d.get("/api/games/matches", {"game": "idle_plaza"})
        self.assertGreaterEqual(r["count"], 1)
        self.assertTrue(all(m["match"]["game"] == "idle_plaza" for m in r["matches"]))
        _, r = self.d.get("/api/games/matches", {"game": "plaza_brawler"})
        self.assertTrue(all(m["match"]["game"] == "plaza_brawler" for m in r["matches"]))

    def test_4_kept_out_of_social_feed(self):
        self.assertEqual(self.d.post("/api/events/append", {"action": "post", "payload": {"text": "hi"}})[0], 200)
        self.assertEqual(self.d.post("/api/games/match", PB_PAYLOAD)[0], 200)
        _, feed = self.d.get("/api/events/feed", {"limit": 200})
        self.assertNotIn("game_match", [e["type"] for e in feed["events"]])
        self.assertIn("post", [e["type"] for e in feed["events"]])
        _, only = self.d.get("/api/events/feed", {"type": "game_match"})
        self.assertGreaterEqual(only["count"], 1)
        self.assertTrue(all(e["type"] == "game_match" for e in only["events"]))

    def test_5_append_cannot_forge_game_match(self):
        code, r = self.d.post("/api/events/append", {"action": "game_match", "payload": PB_PAYLOAD})
        self.assertEqual(code, 403, r)

    def test_6_bad_bodies(self):
        self.assertEqual(self.d.post("/api/games/match", {"game": "plaza_brawler"})[0], 400)
        self.assertEqual(self.d.post("/api/games/match", dict(PB_PAYLOAD, extra=1))[0], 400)
        self.assertEqual(self.d.post("/api/games/match", [1, 2])[0], 400)
        self.assertEqual(http(f"{self.d.ipc}/api/games/match", raw=b"{not json")[0], 400)
        big = dict(PB_PAYLOAD, app_version="x" * 5000)
        self.assertEqual(self.d.post("/api/games/match", big)[0], 413)
        self.assertEqual(self.d.get("/api/games/matches", {"limit": "x"})[0], 400)
        self.assertEqual(self.d.get("/api/games/matches", {"nope": "1"})[0], 400)
        self.assertEqual(self.d.get("/api/games/matches", {"game": "bad game"})[0], 400)

    def test_7_player_signed_envelope(self):
        k = Key()
        payload = dict(PB_PAYLOAD, schema=1)
        code, r = self.d.post("/api/games/match", signed_match(k, payload))
        self.assertEqual(code, 200, r)
        self.assertEqual(r["signed_by"], "player")
        _, lst = self.d.get("/api/games/matches", {"limit": 50})
        mine = [m for m in lst["matches"] if m["author_pubkey"] == k.pub]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["signed_by"], "other")
        self.assertEqual(mine[0]["match"], payload)
        # replay of the same signed event is a no-op
        ev = signed_match(k, payload, ts=now_ms() - 1000)
        self.assertEqual(self.d.post("/api/games/match", ev)[0], 200)
        code, r = self.d.post("/api/games/match", ev)
        self.assertEqual((code, r.get("result")), (200, "ALREADY_EXISTS"), r)

    def test_8_player_signed_rejections(self):
        k = Key()
        ev = signed_match(k, dict(PB_PAYLOAD, schema=1))
        tampered = dict(ev, payload=dict(ev["payload"], winner="p2"))
        tampered.pop("event_id")
        self.assertEqual(self.d.post("/api/games/match", tampered)[0], 401)
        # signature over a payload the node would normalise (no schema) is refused, not silently changed
        self.assertEqual(self.d.post("/api/games/match", signed_match(k, dict(PB_PAYLOAD)))[0], 400)
        self.assertEqual(self.d.post("/api/games/match", dict(ev, action="post"))[0], 400)


class GamesMatchLocalToken(unittest.TestCase):
    """The Android node app runs with OMNI_LOCAL_TOKEN=1: X-Omni-Local-Token or Bearer, like every :5000 path."""

    def test_token_required(self):
        root = Path(tempfile.mkdtemp(prefix="omni_games_tok_"))
        tok = secrets.token_urlsafe(32)
        f = root / "local_token"
        f.write_text(tok + "\n", encoding="utf-8")
        d = Daemon(root, "gtok", env={"OMNI_LOCAL_TOKEN": "1", "OMNI_LOCAL_TOKEN_FILE": str(f)})
        d.start_headers = {"X-Omni-Local-Token": tok}
        try:
            d.start()
            self.assertEqual(d.post("/api/games/match", PB_PAYLOAD)[0], 401)
            self.assertEqual(d.get("/api/games/matches")[0], 401)
            code, r = d.post("/api/games/match", PB_PAYLOAD, headers={"Authorization": f"Bearer {tok}"})
            self.assertEqual(code, 200, r)
            code, r = d.get("/api/games/matches", headers={"X-Omni-Local-Token": tok})
            self.assertEqual((code, r["count"]), (200, 1), r)
        finally:
            d.stop()
            shutil.rmtree(root, ignore_errors=True)


class GamesMatchSync(unittest.TestCase):
    """A match stored on node A reaches node B over the existing catch-up sync."""

    def test_two_daemons(self):
        root = Path(tempfile.mkdtemp(prefix="omni_games_mesh_"))
        a = Daemon(root, "ga", env={"OMNI_MESH_ID": "games-mesh"})
        b = Daemon(root, "gb", env={"OMNI_MESH_ID": "games-mesh",
                                    "OMNI_SYNC_PEERS": f"127.0.0.1:{a.ports['OMNI_P2P_PORT']}"})
        try:
            a.start()
            b.start()
            k = Key()
            code, r1 = a.post("/api/games/match", PB_PAYLOAD)
            self.assertEqual(code, 200, r1)
            code, r2 = a.post("/api/games/match", signed_match(k, dict(PB_PAYLOAD, schema=1)))
            self.assertEqual(code, 200, r2)
            deadline = time.time() + 30
            while True:
                _, rb = b.get("/api/games/matches")
                if rb.get("count") == 2 or time.time() > deadline:
                    break
                b.post("/api/sync/catchup", {})
                time.sleep(0.5)
            self.assertEqual(rb["count"], 2, rb)
            got = {m["event_id"]: m for m in rb["matches"]}
            self.assertEqual(got[r1["event_id"]]["author_pubkey"], a.node_id)
            self.assertEqual(got[r2["event_id"]]["author_pubkey"], k.pub)
            self.assertTrue(all(m["verified"] for m in rb["matches"]))
        finally:
            a.stop()
            b.stop()
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
