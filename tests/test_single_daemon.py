"""Issue #6: only one daemon implementation.

In the split Omni-Daemon repo the canonical daemon is Admin/omni_live_daemon_server.py. The old
monorepo redirect shim (backend_services/omni-ipc-daemon/...) belongs to The-Omniverse and is not
carried here, so this suite only checks that the canonical file exists and that no second full
daemon copy has crept back in.

Run: python -m unittest tests.test_single_daemon -v
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = ROOT / "Admin" / "omni_live_daemon_server.py"


class SingleDaemonTests(unittest.TestCase):
    def test_canonical_daemon_exists(self):
        self.assertTrue(CANONICAL.is_file(), CANONICAL)
        self.assertIn("ThreadingHTTPServer", CANONICAL.read_text(encoding="utf-8"))

    def test_no_other_full_daemon_copies(self):
        copies = [p for p in ROOT.rglob("omni_live_daemon_server.py")
                  if ".git" not in p.parts and p != CANONICAL
                  and "ThreadingHTTPServer" in p.read_text(encoding="utf-8", errors="ignore")]
        self.assertEqual(copies, [], f"extra daemon copies: {copies}")

    def test_no_monorepo_paths(self):
        """The split repo must not depend on monorepo-only folders."""
        for name in ("backend_services", "omni_core", "packages", "mobile"):
            self.assertFalse((ROOT / name).exists(), f"monorepo folder {name}/ reintroduced")


if __name__ == "__main__":
    unittest.main(verbosity=2)
