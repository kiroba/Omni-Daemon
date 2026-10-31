"""Run every tests/test_*.py suite, each in its own process (daemon suites spawn subprocesses and
rewrite ports, so isolating them keeps one failure from cascading).

Run from the repo root:  python tests/run_all.py   (exit code 1 if any suite fails)
Optional: python tests/run_all.py radio catchup   -> only suites whose name contains a word.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv):
    suites = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py"))
    if argv:
        suites = [s for s in suites if any(w in s for w in argv)]
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env["NO_PROXY"] = ",".join(filter(None, [env.get("NO_PROXY"), "127.0.0.1,localhost,::1"]))
    failed = []
    for name in suites:
        t = time.time()
        r = subprocess.run([sys.executable, "-m", "unittest", f"tests.{name}"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=600)
        if r.returncode == 5:  # "NO TESTS RAN": a plain script-style suite (asserts at top level)
            r = subprocess.run([sys.executable, str(ROOT / "tests" / f"{name}.py")], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=600)
        tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
        status = "ok  " if r.returncode == 0 else "FAIL"
        print(f"{status} {name:32s} {time.time() - t:5.1f}s  {tail[0]}", flush=True)
        if r.returncode != 0:
            failed.append(name)
            print(r.stdout[-4000:], r.stderr[-4000:], sep="\n", flush=True)
    print(f"\n{len(suites) - len(failed)}/{len(suites)} suites passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
