"""Game match results (daemon v8.28.0): POST /api/games/match, GET /api/games/matches, node-health
"features". Run: python3 -m unittest tests.test_games_match

v8.33.2 adds shape 3, the generic signed result envelope (SignedResultValidation /
SignedResultApi below): the body Plaza Brawler's PvP S3 sends is PvpMatchEnvelope.toJson()
from kiroba/Plaza-Brawler lib/net/pvp_result.dart, signed over pvpResultBytes.

Starts real daemons on free temp ports (tests/daemon_harness.py). The body Plaza Brawler sends is
MatchShare.payloadFrom(...) from kiroba/Plaza-Brawler lib/net/match_share.dart (spec 5.3).
"""
import json
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


def client_result_bytes(payload: dict) -> bytes:
    """Exactly pvpResultBytes (kiroba/Plaza-Brawler lib/net/pvp_session.dart): the "pvp-result|"
    prefix plus the payload's canonical JSON — pvpMatchPayload's field order, compact separators."""
    return ("pvp-result|" + json.dumps(payload, separators=(",", ":"),
                                       ensure_ascii=False)).encode("utf-8")


def pvp_payload(host: Key, guest: Key) -> dict:
    """A Plaza Brawler S3 result payload, keys in pvpMatchPayload's canonical order."""
    return {"v": 1, "session_id": "0123456789abcdef" * 2, "seed": 123456789,
            "host_pubkey": host.pub, "guest_pubkey": guest.pub,
            "p1_sign": "aries", "p2_sign": "scorpio", "winner": "p1", "wins": [2, 0],
            "rounds": 2, "duration_ms": 61000, "final_state_hash": "ab" * 32}


def pvp_envelope(host: Key, guest: Key, payload: dict) -> dict:
    """The envelope PvpMatchEnvelope.toJson() sends and MatchShare.sharePvpMatch POSTs."""
    signed = client_result_bytes(payload)
    return {"type": "pvp_match", "payload": payload,
            "signatures": {"host": host.sign(signed), "guest": guest.sign(signed)}}


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


class SignedResultValidation(unittest.TestCase):
    """Unit level (daemon v8.33.2): the generic signed-result verifier behind shape 3, signed
    exactly as the Brawler client signs — real Ed25519 keys, the client's canonical bytes."""

    @classmethod
    def setUpClass(cls):
        cls.m = daemon_module()
        cls.host, cls.guest = Key(), Key()
        cls.payload = pvp_payload(cls.host, cls.guest)

    def test_canonical_bytes_match_client(self):
        reg = self.m.SIGNED_RESULT_TYPES["pvp_match"]
        self.assertEqual(self.m.signed_result_bytes(reg, self.payload),
                         client_result_bytes(self.payload))

    def test_valid_envelope_parses_and_verifies(self):
        env = pvp_envelope(self.host, self.guest, self.payload)
        reg, payload, sigs = self.m.parse_signed_result(env)
        self.assertEqual(payload, self.payload)
        self.assertTrue(self.m.signed_result_signatures_ok(reg, payload, sigs))

    def test_wire_key_order_does_not_matter(self):
        # The daemon rebuilds the canonical bytes itself: a payload whose keys arrive in
        # another order still verifies against signatures made over the canonical order.
        env = pvp_envelope(self.host, self.guest, self.payload)
        env["payload"] = dict(reversed(list(self.payload.items())))
        reg, payload, sigs = self.m.parse_signed_result(env)
        self.assertTrue(self.m.signed_result_signatures_ok(reg, payload, sigs))

    def test_forged_and_tampered_fail_verification(self):
        env = pvp_envelope(self.host, self.guest, self.payload)
        reg, payload, sigs = self.m.parse_signed_result(env)
        forged = dict(sigs, guest=Key().sign(client_result_bytes(payload)))
        self.assertFalse(self.m.signed_result_signatures_ok(reg, payload, forged))
        tampered = dict(payload, winner="p2")
        self.assertFalse(self.m.signed_result_signatures_ok(reg, tampered, sigs))

    def test_parse_rejections(self):
        env = pvp_envelope(self.host, self.guest, self.payload)
        bad = [
            dict(env, type="chess_match"),                                  # unknown type
            dict(env, signatures={"host": env["signatures"]["host"]}),      # missing guest sig
            dict(env, signatures=dict(env["signatures"], ref="0" * 128)),   # unknown role
            dict(env, signatures=dict(env["signatures"], guest="zz")),      # malformed sig
            dict(env, payload=dict(self.payload, extra=1)),                 # unknown field
            dict(env, payload=dict(self.payload, winner="p3")),
            dict(env, payload=dict(self.payload, seed=True)),
            dict(env, payload=dict(self.payload, seed=2 ** 31)),
            dict(env, payload=dict(self.payload, wins=[2])),
            dict(env, payload=dict(self.payload, wins=[2, 0, 1])),
            dict(env, payload=dict(self.payload, session_id="AB" * 16)),    # uppercase hex
            dict(env, payload=dict(self.payload, host_pubkey=self.guest.pub)),  # same player twice
            dict(env, payload={k: v for k, v in self.payload.items() if k != "rounds"}),
        ]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(ValueError):
                self.m.parse_signed_result(b)


class SignedResultApi(unittest.TestCase):
    """POST /api/games/match with a dual-signed pvp_match envelope (Plaza Brawler S3): stored as
    a game_match event carrying the full envelope; anything unverified is refused, storing
    nothing. The bare solo summary (shape 1) is unaffected."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="omni_pvp_"))
        cls.d = Daemon(cls.tmp, "pvp").start()
        cls.host, cls.guest = Key(), Key()
        cls.payload = pvp_payload(cls.host, cls.guest)

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _game_match_count(self):
        _code, s = self.d.get("/api/telemetry/summary")
        return s["services"]["event_store"]["events_by_action"].get("game_match", 0)

    def test_1_valid_envelope_accepted_and_stored(self):
        before = self._game_match_count()
        env = pvp_envelope(self.host, self.guest, self.payload)
        code, r = self.d.post("/api/games/match", env)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["status"], "SUCCESS")
        self.assertEqual(r["signed_by"], "players")
        self.assertEqual(r["result_type"], "pvp_match")
        self.assertRegex(r["event_id"], r"^0x[0-9a-f]{64}$")
        # counts as a game_match in the telemetry summary (the Mesh Monitor events-by-type tile)
        self.assertEqual(self._game_match_count(), before + 1)
        # listed with the result fields, marked as player-signed
        _code, lst = self.d.get("/api/games/matches", {"limit": 50})
        mine = [m for m in lst["matches"] if m["event_id"] == r["event_id"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["signed_by"], "players")
        self.assertTrue(mine[0]["verified"])
        self.assertEqual(mine[0]["match"]["game"], "plaza_brawler")
        self.assertEqual(mine[0]["match"]["result_type"], "pvp_match")
        self.assertEqual(mine[0]["match"]["winner"], "p1")
        self.assertEqual(mine[0]["match"]["host_pubkey"], self.host.pub)
        self.assertEqual(mine[0]["match"]["guest_pubkey"], self.guest.pub)
        # the stored event payload is the full envelope — signatures stay auditable
        _code, feed = self.d.get("/api/events/feed", {"type": "game_match", "limit": 50})
        stored = [e for e in feed["events"] if e["event_id"] == r["event_id"]]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["payload"], env)

    def test_2_forged_signature_rejected_nothing_stored(self):
        before = self._game_match_count()
        env = pvp_envelope(self.host, self.guest, self.payload)
        env["signatures"]["guest"] = Key().sign(client_result_bytes(self.payload))
        code, r = self.d.post("/api/games/match", env)
        self.assertEqual(code, 401, r)
        self.assertEqual(self._game_match_count(), before)

    def test_3_missing_signature_rejected_nothing_stored(self):
        before = self._game_match_count()
        env = pvp_envelope(self.host, self.guest, self.payload)
        del env["signatures"]["guest"]
        code, r = self.d.post("/api/games/match", env)
        self.assertEqual(code, 400, r)
        self.assertEqual(self._game_match_count(), before)

    def test_4_tampered_payload_rejected_nothing_stored(self):
        before = self._game_match_count()
        env = pvp_envelope(self.host, self.guest, self.payload)
        env["payload"] = dict(self.payload, winner="p2")   # signatures no longer cover it
        code, r = self.d.post("/api/games/match", env)
        self.assertEqual(code, 401, r)
        self.assertEqual(self._game_match_count(), before)

    def test_5_unknown_envelope_type_rejected(self):
        before = self._game_match_count()
        env = pvp_envelope(self.host, self.guest, self.payload)
        env["type"] = "chess_match"
        code, r = self.d.post("/api/games/match", env)
        self.assertEqual(code, 400, r)
        self.assertEqual(self._game_match_count(), before)

    def test_6_solo_summary_still_accepted(self):
        code, r = self.d.post("/api/games/match", PB_PAYLOAD)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["signed_by"], "node")


if __name__ == "__main__":
    unittest.main()
