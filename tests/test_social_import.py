"""Daemon v8.23.0: PROFILE_SOCIAL_IMPORT events from kiroba/Continuity-Engine.

The event is built exactly like Continuity-Engine integrations/continuity_social_import_engine.py
import_external_profile() builds it (same payload keys, same SHA-256 blind hash, same canonical
Ed25519 envelope). Set CONTINUITY_ENGINE_DIR to a Continuity-Engine checkout to also sign with that
repo's own engine and post with its post_to_daemon() (skipped when unset).

Run: python3 -m unittest tests.test_social_import
"""
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daemon_harness import Daemon, Key, daemon_module, http, now_ms  # noqa: E402

ZERO = "0x" + "0" * 64
ACTION = "PROFILE_SOCIAL_IMPORT"
PATH = "/api/profile/social-import"
CE_DIR = os.environ.get("CONTINUITY_ENGINE_DIR", "").strip()


def blind_hash(identifier: str) -> str:
    """Continuity-Engine hash_contact_identifier()."""
    return hashlib.sha256(f"OMNI_SALT_2026:{identifier.strip().lower()}".encode("utf-8")).hexdigest()


def import_payload(platform="X_TWITTER", handle="@alice_creator", bio="Digital artist", contacts=None) -> dict:
    """Same shape as Continuity-Engine import_external_profile()'s payload."""
    contacts = contacts if contacts is not None else [
        {"name": "Bob Smith", "email": "bob@example.com"}, {"name": "Carol Danvers", "phone": "+15551234567"}]
    hashed = [{"display_name": c.get("name", "Omni Friend"),
               "blind_hash": blind_hash(c.get("email") or c.get("phone") or c.get("handle", "")),
               "platform": platform} for c in contacts]
    return {"import_event_id": str(uuid.uuid4()), "platform_source": platform.upper(), "external_handle": handle,
            "imported_bio": bio, "total_contacts_imported": len(hashed), "contact_blind_hashes": hashed,
            "fanbase_bootstrap_status": "PENDING_P2P_MATCHING"}


def signed(key: Key, payload: dict, action: str = ACTION, prev: str = ZERO) -> dict:
    return key.sign_event({"action": action, "author_pubkey": key.pub, "payload": payload,
                           "previous_hash": prev, "ts": now_ms()})


class Unit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = daemon_module()

    def test_valid_payloads(self):
        self.d.validate_social_import(import_payload())
        self.d.validate_social_import(import_payload(bio="", contacts=[]))
        self.d.validate_social_import(import_payload(platform="x_twitter", bio="two\nlines"))

    def test_rejects_malformed(self):
        good = import_payload()
        bads = []
        for mutate in (
            lambda p: p.update(extra=1),                                      # unknown field
            lambda p: p.pop("imported_bio"),                                  # missing field
            lambda p: p.update(import_event_id="not-a-uuid"),
            lambda p: p.update(platform_source="x_twitter"),                  # not upper case
            lambda p: p.update(external_handle=""),
            lambda p: p.update(external_handle=" @alice"),                    # untrimmed
            lambda p: p.update(imported_bio="x" * 1001),                      # too long
            lambda p: p.update(imported_bio="tab\there"),                     # control char
            lambda p: p.update(total_contacts_imported=5),                    # count mismatch
            lambda p: p.update(total_contacts_imported=True),
            lambda p: p.update(fanbase_bootstrap_status="DONE"),
            lambda p: p["contact_blind_hashes"][0].update(blind_hash="bob@example.com"),   # raw PII
            lambda p: p["contact_blind_hashes"][0].update(email="bob@example.com"),        # unknown contact key
            lambda p: p["contact_blind_hashes"][0].update(display_name="n" * 65),
            lambda p: p["contact_blind_hashes"][0].update(platform="INSTAGRAM"),
            lambda p: p.update(contact_blind_hashes="nope"),
        ):
            bad = copy.deepcopy(good)
            mutate(bad)
            bads.append(bad)
        bads.append([])
        for bad in bads:
            with self.assertRaises(ValueError, msg=json.dumps(bad)[:200]):
                self.d.validate_social_import(bad)

    def test_contact_cap(self):
        many = [{"email": f"u{i}@x.io"} for i in range(self.d.SOCIAL_IMPORT_MAX_CONTACTS + 1)]
        with self.assertRaises(ValueError):
            self.d.validate_social_import(import_payload(contacts=many))

    def test_registered_like_other_person_signed_actions(self):
        self.assertEqual(self.d.RESERVED_USER_ACTIONS[ACTION], PATH)
        self.assertIn(PATH, self.d.USER_POST_ROUTES)
        self.assertIn(PATH, self.d.REMOTE_POST_PATHS)


class LiveSocialImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="omni_social_import_"))
        cls.d = Daemon(cls.tmp, "socimport", env={"OMNI_USER_RATE_MAX": "50"}).start()

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _feed_actions(self):
        code, r = self.d.get("/api/events/feed", {"limit": 50})
        self.assertEqual(code, 200, r)
        return [(e["action"], e.get("author_pubkey"), e.get("verified")) for e in r["events"]]

    def test_accepts_signed_import_and_dedupes_replay(self):
        k = Key()
        ev = signed(k, import_payload())
        code, r = self.d.post(PATH, ev)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["status"], "SUCCESS")
        self.assertEqual(r["event_id"], ev["event_id"])
        self.assertEqual((r["action"], r["author_pubkey"]), (ACTION, k.pub))
        self.assertIn((ACTION, k.pub, True), self._feed_actions())
        code, r = self.d.post(PATH, ev)
        self.assertEqual(code, 200, r)
        self.assertEqual(r.get("result"), "ALREADY_EXISTS")

    def test_rejects_bad_signature(self):
        k = Key()
        ev = signed(k, import_payload())
        ev["payload"]["imported_bio"] = "tampered after signing"
        code, r = self.d.post(PATH, ev)
        self.assertEqual(code, 401, r)
        ev2 = signed(k, import_payload())
        ev2["signature"] = Key().sign(b"something else")
        self.assertEqual(self.d.post(PATH, ev2)[0], 401)

    def test_rejects_malformed_payload_and_envelope(self):
        k = Key()
        p = import_payload()
        p["contact_blind_hashes"][0]["blind_hash"] = "bob@example.com"
        code, r = self.d.post(PATH, signed(k, p))
        self.assertEqual(code, 400, r)
        self.assertEqual(self.d.post(PATH, signed(k, import_payload(), action="profile_update"))[0], 400)
        self.assertEqual(self.d.post(PATH, dict(signed(k, import_payload()), extra=1))[0], 400)

    def test_oversized_body_413(self):
        k = Key()
        p = import_payload(bio="")
        p["external_handle"] = "@" + "a" * (self.d_max() + 10)
        code, _ = self.d.post(PATH, signed(k, p))
        self.assertEqual(code, 413)

    def test_largest_ascii_import_fits(self):
        """500 contacts with 64-char names, 32-char platform and a 1000-char bio stay under the body cap."""
        plat = "P" * 32
        contacts = [{"name": f"{i:03d}" + "n" * 61, "email": f"u{i}@x.io"} for i in range(500)]
        p = import_payload(platform=plat, handle="@" + "h" * 99, bio="b" * 1000, contacts=contacts)
        ev = signed(Key(), p)
        self.assertLess(len(json.dumps(ev).encode()), self.d_max())
        code, r = self.d.post(PATH, ev)
        self.assertEqual(code, 200, r)

    def d_max(self):
        return daemon_module().SOCIAL_IMPORT_MAX_BYTES

    def test_generic_append_refuses_action(self):
        code, r = self.d.post("/api/events/append", signed(Key(), import_payload()))
        self.assertEqual(code, 403, r)
        self.assertIn(PATH, r["message"])


@unittest.skipUnless(CE_DIR, "set CONTINUITY_ENGINE_DIR to a kiroba/Continuity-Engine checkout")
class LiveWithContinuityEngine(unittest.TestCase):
    """Signs with Continuity-Engine's own engine and posts with its post_to_daemon()."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(CE_DIR) / "integrations"))
        import continuity_social_import_engine as ce
        cls.ce = ce
        cls.tmp = Path(tempfile.mkdtemp(prefix="omni_social_import_ce_"))
        cls.d = Daemon(cls.tmp, "socimportce").start()

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_engine_event_accepted_and_chained(self):
        eng = self.ce.ContinuitySocialImportEngine()
        ev = eng.import_external_profile("X_TWITTER", "@alice_creator", "Digital artist",
                                         [{"name": "Bob Smith", "email": "bob@example.com"},
                                          {"name": "Dave", "handle": "@dave_dev"}])
        code, r = self.ce.post_to_daemon(ev, self.d.ipc)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["event_id"], ev["event_id"])
        nxt = eng.import_external_profile("X_TWITTER", "@alice_creator", "v2", [], prev_hash=ev["event_id"])
        self.assertEqual(self.ce.post_to_daemon(nxt, self.d.ipc)[0], 200)
        bad = dict(ev, signature="0" * 128)
        self.assertEqual(self.ce.post_to_daemon(bad, self.d.ipc)[0], 401)


if __name__ == "__main__":
    unittest.main()
