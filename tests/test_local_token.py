"""Local token (daemon v8.19.0): with OMNI_LOCAL_TOKEN=1 (the Android node app), loopback requests to
:5000 and :9100 need "X-Omni-Local-Token". Without the env var nothing changes (PC behaviour).

Run: python3 -m unittest tests.test_local_token
"""
import os
import secrets
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daemon_harness import Daemon, http  # noqa: E402

HDR = "X-Omni-Local-Token"


class LocalTokenRequired(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="omni_local_token_"))
        cls.token = secrets.token_urlsafe(32)
        tok_file = cls.root / "local_token"
        tok_file.write_text(cls.token + "\n", encoding="utf-8")
        cls.tok_file = tok_file
        cls.d = Daemon(cls.root, "lt", env={"OMNI_LOCAL_TOKEN": "1", "OMNI_LOCAL_TOKEN_FILE": str(tok_file)})
        cls.d.start_headers = {HDR: cls.token}
        cls.d.start()

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_ipc_without_token_is_401(self):
        code, body = self.d.get("/api/events/stats")
        self.assertEqual(code, 401, body)

    def test_ipc_wrong_token_is_401(self):
        code, _ = self.d.get("/api/events/stats", headers={HDR: "x" * 43})
        self.assertEqual(code, 401)

    def test_telemetry_without_token_is_401(self):
        code, _ = http(f"{self.d.tel}/api/v1/telemetry/node-health")
        self.assertEqual(code, 401)

    def test_ipc_with_token_works(self):
        code, body = self.d.get("/api/events/stats", headers={HDR: self.token})
        self.assertEqual(code, 200, body)
        self.assertIn("total_events", body)

    def test_post_with_token_reaches_handler(self):
        # A malformed body gets past the gate and is rejected by the handler (400), not the gate (401).
        code, _ = self.d.post("/api/events/append", {"nope": True}, headers={HDR: self.token})
        self.assertNotEqual(code, 401)
        code, _ = self.d.post("/api/events/append", {"nope": True})
        self.assertEqual(code, 401)

    def test_bearer_local_token_works(self):
        # v8.20.0: apps set to "Remote hub 127.0.0.1" send the token as a bearer header.
        code, body = self.d.get("/api/events/stats", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(code, 200, body)
        code, _ = http(f"{self.d.tel}/api/v1/telemetry/node-health",
                       headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(code, 200)

    def test_bearer_wrong_token_is_401(self):
        code, _ = self.d.get("/api/events/stats", headers={"Authorization": "Bearer " + "x" * 43})
        self.assertEqual(code, 401)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_token_file_is_owner_only(self):
        self.assertEqual(stat.S_IMODE(self.tok_file.stat().st_mode), 0o600)


class LocalTokenOffByDefault(unittest.TestCase):
    def test_loopback_open_without_env(self):
        root = Path(tempfile.mkdtemp(prefix="omni_local_token_off_"))
        d = Daemon(root, "off").start()
        try:
            code, body = d.get("/api/events/stats")
            self.assertEqual(code, 200, body)
            self.assertFalse((d.home / ".omni" / "local_token").exists())
        finally:
            d.stop()
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
