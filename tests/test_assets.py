"""Content-addressed KickBack asset endpoint tests."""
import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ADMIN = Path(__file__).resolve().parent.parent / "Admin"
sys.path.insert(0, str(ADMIN))
_TMP = Path(tempfile.mkdtemp(prefix="omni_asset_test_"))
os.environ["HOME"] = str(_TMP)
os.environ["OMNI_MESH_ID"] = "asset-test"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.chdir(_TMP)

daemon = importlib.import_module("omni_live_daemon_server")


class AssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.ContinuityIPCHandler)
        cls.p2p = ThreadingHTTPServer(("127.0.0.1", 0), daemon.P2PSyncHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        threading.Thread(target=cls.p2p.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.p2p_base = f"http://127.0.0.1:{cls.p2p.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.p2p.shutdown()
        cls.p2p.server_close()

    def setUp(self):
        self.work = _TMP / self._testMethodName
        self.work.mkdir(exist_ok=True)
        os.chdir(self.work)

    def request(self, method, path, body=b"", headers=None, base=None):
        req = urllib.request.Request((base or self.base) + path, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def test_upload_and_download_by_sha256(self):
        import hashlib

        body = b"fake image bytes"
        sha = hashlib.sha256(body).hexdigest()
        status, response, _ = self.request(
            "POST", f"/api/assets/{sha}", body, {"Content-Type": "image/png"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(response)["sha256"], sha)

        status, response, headers = self.request("GET", f"/api/assets/{sha}")
        self.assertEqual(status, 200)
        self.assertEqual(response, body)
        self.assertEqual(headers.get("X-Omni-Asset-Sha256"), sha)

    def test_wrong_hash_is_rejected(self):
        status, response, _ = self.request(
            "POST", f"/api/assets/{'0' * 64}", b"not that hash", {"Content-Type": "image/png"}
        )
        self.assertEqual(status, 400)
        self.assertIn("SHA-256", json.loads(response)["message"])

    def test_friend_event_schema(self):
        target = "a" * 64
        daemon.validate_kickback_friend_event({
            "action": "KICKBACK_FRIEND",
            "payload": {"schema": 1, "target_pubkey": target, "state": "pending"},
        })
        with self.assertRaisesRegex(ValueError, "state"):
            daemon.validate_kickback_friend_event({
                "action": "KICKBACK_FRIEND",
                "payload": {"schema": 1, "target_pubkey": target, "state": "bogus"},
            })

    def test_peer_asset_routes(self):
        import hashlib

        body = b"peer image bytes"
        sha = hashlib.sha256(body).hexdigest()
        headers = {"Content-Type": "application/octet-stream", daemon.MESH_HEADER: daemon.STATE.mesh_id}
        status, response, _ = self.request("POST", f"/p2p/assets/{sha}", body, headers, self.p2p_base)
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(response)["sha256"], sha)
        status, response, _ = self.request("GET", f"/p2p/assets/{sha}", b"", {daemon.MESH_HEADER: daemon.STATE.mesh_id}, self.p2p_base)
        self.assertEqual(status, 200)
        self.assertEqual(response, body)


if __name__ == "__main__":
    unittest.main()
