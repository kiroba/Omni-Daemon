"""
Peer-to-peer update cache (daemon v8.30.0)
==========================================
Keeps the newest verified Omniverse update manifest and the release files it lists, so paired devices
can pass updates to each other instead of each one downloading from GitHub.

Trust model (matches kiroba/Omni-Core-UI lib/src/omni_ota_engine.dart, OmniOta.manifestTrusted):
  * The manifest is `update-manifest.json` from kiroba/The-Omniverse GitHub Releases:
      {"tag": "0.1.24", "build": 1029, "transport": "github",
       "apps": {"<slug>": {"url": "https://...apk", "sha256": "<64 hex>"}, ...},
       "signature": "<128 hex>"}
  * `signature` is Ed25519 over the canonical JSON of the manifest minus "signature":
    json.dumps(obj, sort_keys=True, separators=(",", ":")) (ensure_ascii on), UTF-8. That is what the
    release workflow signs and what the Dart canonicalJson() reproduces byte for byte.
  * The public key is pinned: PINNED_OTA_PUBKEY is the key the release workflow derives from the
    OMNI_OTA_SIGNING_KEY secret and bakes into every APK as OMNI_OTA_PUBKEY (logged by the release run
    as "OTA manifests will be signature-pinned to <hex>"). OMNI_UPDATE_PUBKEY overrides it (tests, a
    fork with its own key).
  * A manifest is accepted only if the signature verifies and its `build` is >= the held one (no
    downgrade). Unsigned manifests are refused unless OMNI_UPDATE_ALLOW_UNSIGNED=1.
  * Files are stored by SHA-256 and only after the bytes hash to the manifest entry (temp file, then an
    atomic rename). A peer is never trusted for content: the hash is what makes a peer safe.

Only the standard library + `cryptography`.
"""
import hashlib
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PINNED_OTA_PUBKEY = "4f23da25e30a7a9e797a58e01641fa98ad78ed46ff213f43de68dbe9723e9e11"
DEFAULT_MANIFEST_URL = "https://github.com/kiroba/The-Omniverse/releases/latest/download/update-manifest.json"

SHA_RE = re.compile(r"[0-9a-f]{64}")
_HEX_RE = re.compile(r"[0-9a-f]+")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
MANIFEST_MAX_BYTES = 256 * 1024
MAX_APPS = 64
CHUNK = 64 * 1024
PART_MAX_AGE_S = 7 * 86400


class ManifestError(ValueError):
    """The manifest is malformed, unsigned (and unsigned isn't allowed) or its signature is bad."""


class PermanentFetchError(Exception):
    """The source answered in a way retrying can't fix (404, other 4xx)."""


# ---------------------------------------------------------------------------------------- settings

def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def parse_mode(value=None) -> str:
    """OMNI_UPDATES: on | off | auto (default; auto = on)."""
    v = (os.environ.get("OMNI_UPDATES", "") if value is None else value).strip().lower() or "auto"
    if v not in ("on", "off", "auto"):
        raise ValueError(f"OMNI_UPDATES={v!r} is invalid: use on, off or auto")
    return "off" if v == "off" else "on"


def pinned_pubkey(value=None) -> str:
    v = (os.environ.get("OMNI_UPDATE_PUBKEY", "") if value is None else value).strip().lower()
    v = v or PINNED_OTA_PUBKEY
    if len(v) != 64 or not _HEX_RE.fullmatch(v):
        raise ValueError("OMNI_UPDATE_PUBKEY must be 64 hex chars (a raw Ed25519 public key)")
    return v


def settings(env=None) -> dict:
    env = os.environ if env is None else env

    def num(name, default, cast=float, lo=0):
        try:
            return max(lo, cast(str(env.get(name, "")).strip() or default))
        except ValueError:
            return default
    apps = [a.strip() for a in str(env.get("OMNI_UPDATE_APPS", "")).split(",") if a.strip()]
    return {
        "mode": parse_mode(env.get("OMNI_UPDATES", "")),
        "cache_dir": Path(str(env.get("OMNI_UPDATE_CACHE_DIR", "")).strip() or (Path.home() / ".omni" / "updates")),
        "cap_bytes": int(num("OMNI_UPDATE_CACHE_MB", 1024) * 1024 * 1024),
        "pubkey": pinned_pubkey(env.get("OMNI_UPDATE_PUBKEY", "")),
        "allow_unsigned": _truthy(env.get("OMNI_UPDATE_ALLOW_UNSIGNED")),
        "manifest_url": str(env.get("OMNI_UPDATE_MANIFEST_URL", "")).strip() or DEFAULT_MANIFEST_URL,
        "github_interval_s": num("OMNI_UPDATE_GITHUB_INTERVAL_MIN", 30) * 60.0,
        "peer_interval_s": num("OMNI_UPDATE_PEER_INTERVAL_S", 60),
        "timeout_s": num("OMNI_UPDATE_TIMEOUT_S", 30, lo=1),
        "wifi_only": _truthy(env.get("OMNI_UPDATE_WIFI_ONLY")),
        "apps": apps or None,       # None = every app in the manifest counts as this node's
    }


def network_allows_download(wifi_only: bool) -> bool:
    """Hook for OMNI_UPDATE_WIFI_ONLY. Desktop: always True (no metered-network signal here). The Android
    shell can replace this function (or set OMNI_UPDATE_METERED=1 while on mobile data)."""
    if not wifi_only:
        return True
    return not _truthy(os.environ.get("OMNI_UPDATE_METERED"))


# ---------------------------------------------------------------------------------------- manifest

def canonical_bytes(obj) -> bytes:
    """Python twin of Omni-Core-UI canonicalJson(): sort_keys, no spaces, ensure_ascii."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_manifest(m) -> dict:
    """Shape check. Returns m unchanged (the signature covers the exact object). Raises ManifestError."""
    if not isinstance(m, dict):
        raise ManifestError("manifest must be a JSON object")
    if not isinstance(m.get("tag"), str) or not m["tag"]:
        raise ManifestError("manifest 'tag' must be a non-empty string")
    b = m.get("build")
    if not isinstance(b, int) or isinstance(b, bool) or b < 0:
        raise ManifestError("manifest 'build' must be a non-negative integer")
    apps = m.get("apps")
    if not isinstance(apps, dict) or len(apps) > MAX_APPS:
        raise ManifestError("manifest 'apps' must be an object")
    for slug, e in apps.items():
        if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
            raise ManifestError(f"bad app slug {slug!r}")
        if not isinstance(e, dict):
            raise ManifestError(f"apps[{slug!r}] must be an object")
        url, sha = e.get("url"), e.get("sha256")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise ManifestError(f"apps[{slug!r}].url must be an http(s) URL")
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise ManifestError(f"apps[{slug!r}].sha256 must be 64 lowercase hex chars")
    sig = m.get("signature")
    if sig is not None and (not isinstance(sig, str) or len(sig) != 128 or not _HEX_RE.fullmatch(sig)):
        raise ManifestError("manifest 'signature' must be 128 lowercase hex chars")
    return m


def verify_manifest(m, pubkey_hex: str, allow_unsigned: bool = False) -> str:
    """Returns "signed" or "unsigned" (only with allow_unsigned). Raises ManifestError otherwise."""
    validate_manifest(m)
    sig = m.get("signature")
    if sig is None:
        if allow_unsigned:
            return "unsigned"
        raise ManifestError("unsigned manifest refused (set OMNI_UPDATE_ALLOW_UNSIGNED=1 to accept)")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    unsigned = {k: v for k, v in m.items() if k != "signature"}
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(bytes.fromhex(sig),
                                                                            canonical_bytes(unsigned))
    except (InvalidSignature, ValueError):
        raise ManifestError("manifest signature does not verify against the pinned key")
    return "signed"


def parse_manifest_bytes(raw: bytes) -> dict:
    if raw is None or len(raw) > MANIFEST_MAX_BYTES:
        raise ManifestError("manifest missing or too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ManifestError("manifest is not valid UTF-8 JSON")


def manifest_hashes(m, apps=None) -> dict:
    """{sha256: [slugs]} for the manifest's apps (only `apps` when given)."""
    out = {}
    for slug, e in ((m or {}).get("apps") or {}).items():
        if apps is None or slug in apps:
            out.setdefault(e["sha256"], []).append(slug)
    return out


def manifest_url_for(m, sha: str):
    for e in ((m or {}).get("apps") or {}).values():
        if e.get("sha256") == sha:
            return e.get("url")
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _write_atomic(path: Path, data: bytes):
    tmp = path.with_name(path.name + f".tmp{os.getpid()}.{threading.get_ident()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------- downloads

def _status(resp) -> int:
    return int(getattr(resp, "status", None) or getattr(resp, "code", 0) or 0)


def _close(resp):
    for obj in (resp, getattr(resp, "_omni_conn", None)):
        try:
            if obj is not None:
                obj.close()
        except Exception:
            pass


def parse_content_range(value):
    """'bytes a-b/T' -> (a, b, T or None). None when unparseable."""
    m = re.fullmatch(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*", str(value or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), (None if m.group(3) == "*" else int(m.group(3)))


def download_resume(open_fn, part: Path, attempts: int = 3, on_start=None, on_bytes=None,
                    backoff=lambda n: min(2.0 * n, 5.0)) -> int:
    """Downloads into `part`, resuming with Range from whatever is already there.

    open_fn(start) -> an HTTP response (urllib / http.client), asked for bytes start- when start > 0.
    on_start(total_or_None, offset): the body starts at `offset` (0 = from scratch) of `total` bytes.
    on_bytes(have): `have` bytes of the file are on disk (flushed).
    Returns the final size. Raises PermanentFetchError (404/4xx) or the last error after `attempts`."""
    last = None
    for i in range(max(1, attempts)):
        if i:
            time.sleep(backoff(i))
        have = part.stat().st_size if part.exists() else 0
        try:
            resp = open_fn(have)
        except PermanentFetchError:
            raise
        except urllib.error.HTTPError as e:
            resp = e
        except (OSError, http.client.HTTPException, ValueError) as e:
            last = e
            continue
        try:
            code = _status(resp)
            if code == 416 and have > 0:
                return have          # asked past the end: what we have may already be the whole file
            if code == 404:
                raise PermanentFetchError("not found (404)")
            if 400 <= code < 500 and code not in (408, 429):
                raise PermanentFetchError(f"HTTP {code}")
            if code not in (200, 206):
                last = OSError(f"HTTP {code}")
                continue
            if code == 206 and have > 0:
                cr = parse_content_range(resp.headers.get("Content-Range"))
                if cr is None or cr[0] != have:
                    last = OSError("bad Content-Range on resume")
                    part.unlink(missing_ok=True)
                    continue
                total, mode = cr[2], "ab"
            else:
                have, mode = 0, "wb"
                cl = resp.headers.get("Content-Length")
                total = int(cl) if cl and str(cl).isdigit() else None
                if code == 206:     # a range we didn't ask for: treat as an error
                    last = OSError("unexpected 206")
                    continue
            if on_start:
                on_start(total, have)
            with open(part, mode) as f:
                while True:
                    buf = resp.read(CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    f.flush()
                    have += len(buf)
                    if on_bytes:
                        on_bytes(have)
            if total is not None and have < total:
                last = OSError(f"cut short ({have} of {total} bytes)")
                continue
            return have
        except PermanentFetchError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as e:
            last = e
        finally:
            _close(resp)
    raise last if last else OSError("download failed")


def url_opener(url: str, timeout: float, opener=None):
    """open_fn for download_resume over plain urllib (follows GitHub's redirect to its CDN; Range is kept)."""
    op = opener or urllib.request.build_opener()

    def open_fn(start: int):
        h = {"User-Agent": "omni-daemon-updates/1"}
        if start > 0:
            h["Range"] = f"bytes={start}-"
        return op.open(urllib.request.Request(url, headers=h), timeout=timeout)
    return open_fn


class FetchJob:
    """One file being fetched into the cache. Readers stream it while it downloads (stream_job)."""

    def __init__(self, sha: str):
        self.sha = sha
        self.cond = threading.Condition()
        self.total = None
        self.written = 0
        self.started = False
        self.done = False
        self.ok = False
        self.gen = 0             # bumped when the partial file is thrown away (hash mismatch)
        self.error = None
        self.source = None
        self.tried = []

    def snapshot(self) -> dict:
        with self.cond:
            return {"sha256": self.sha, "written": self.written, "total": self.total, "source": self.source,
                    "done": self.done, "ok": self.ok, "error": self.error}


# ---------------------------------------------------------------------------------------- the cache

class UpdateCache:
    def __init__(self, root: Path, cap_bytes: int, pubkey: str, allow_unsigned: bool = False, apps=None):
        self.root = Path(root)
        self.files_dir = self.root / "files"
        self.part_dir = self.root / "partial"
        self.manifest_path = self.root / "manifest.json"
        self.meta_path = self.root / "manifest.meta.json"
        self.cap = int(cap_bytes)
        self.pubkey = pubkey
        self.allow_unsigned = bool(allow_unsigned)
        self.apps = set(apps) if apps else None
        self.lock = threading.RLock()
        self.jobs = {}
        self.manifest = None
        self.meta = {}
        self.last_error = None
        for d in (self.root, self.files_dir, self.part_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._load()
        self._sweep_parts()

    # -- manifest
    def _load(self):
        try:
            m = parse_manifest_bytes(self.manifest_path.read_bytes())
            trust = verify_manifest(m, self.pubkey, self.allow_unsigned)    # re-check: the pin may have changed
            try:
                meta = json.loads(self.meta_path.read_text("utf-8"))
            except (OSError, ValueError):
                meta = {}
            self.manifest, self.meta = m, dict(meta, trust=trust)
        except FileNotFoundError:
            pass
        except (OSError, ManifestError) as e:
            self.last_error = f"stored manifest ignored: {e}"

    def held_build(self) -> int:
        with self.lock:
            return self.manifest["build"] if self.manifest else -1

    def offer_manifest(self, m, source: str) -> str:
        """Verifies and maybe adopts a manifest. Returns "updated" (newer, now held), "same" (same build,
        kept the held one) or raises ManifestError (bad/unsigned/downgrade)."""
        trust = verify_manifest(m, self.pubkey, self.allow_unsigned)
        with self.lock:
            held = self.manifest["build"] if self.manifest else -1
            if m["build"] < held:
                raise ManifestError(f"downgrade refused: build {m['build']} < held {held}")
            if m["build"] == held:
                return "same"
            meta = {"source": source, "received_at": int(time.time()), "trust": trust}
            _write_atomic(self.manifest_path, json.dumps(m, indent=2).encode("utf-8"))
            _write_atomic(self.meta_path, json.dumps(meta).encode("utf-8"))
            self.manifest, self.meta = m, meta
            return "updated"

    def manifest_view(self):
        with self.lock:
            if not self.manifest:
                return None
            return {"tag": self.manifest["tag"], "build": self.manifest["build"],
                    "apps": sorted(self.manifest["apps"]), **self.meta}

    def protected(self) -> dict:
        with self.lock:
            return manifest_hashes(self.manifest, self.apps)

    # -- files
    def path_for(self, sha: str) -> Path:
        if not SHA_RE.fullmatch(sha or ""):
            raise ValueError("bad sha256")
        return self.files_dir / sha

    def part_for(self, sha: str) -> Path:
        return self.part_dir / f"{self.path_for(sha).name}.part"

    def has(self, sha: str) -> bool:
        try:
            return self.path_for(sha).is_file()
        except ValueError:
            return False

    def touch(self, sha: str):
        try:
            os.utime(self.path_for(sha), None)
        except (OSError, ValueError):
            pass

    def entries(self) -> list:
        out = []
        for p in self.files_dir.iterdir():
            if p.is_file() and SHA_RE.fullmatch(p.name):
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append({"sha256": p.name, "size": st.st_size, "last_used": int(st.st_mtime)})
        return out

    def size_bytes(self) -> int:
        return sum(e["size"] for e in self.entries())

    def commit(self, part: Path, sha: str) -> bool:
        """Moves a downloaded file into the cache iff it hashes to `sha`. A mismatch deletes it."""
        if sha256_file(part) != sha:
            part.unlink(missing_ok=True)
            return False
        os.replace(part, self.path_for(sha))
        self.evict(keep=sha)
        return True

    def evict(self, keep: str = None) -> list:
        """LRU eviction down to the cap. Files the held manifest lists for this node's apps go last."""
        removed = []
        with self.lock:
            ents = self.entries()
            total = sum(e["size"] for e in ents)
            if total <= self.cap:
                return removed
            prot = self.protected()
            order = sorted(ents, key=lambda e: (e["sha256"] in prot, e["last_used"]))
            for e in order:
                if total <= self.cap:
                    break
                if e["sha256"] == keep:
                    continue
                try:
                    os.remove(self.files_dir / e["sha256"])
                except OSError:
                    continue        # in use (Windows) or gone; try the next one
                total -= e["size"]
                removed.append(e["sha256"])
        return removed

    def _sweep_parts(self):
        now = time.time()
        for p in self.part_dir.iterdir():
            try:
                if now - p.stat().st_mtime > PART_MAX_AGE_S:
                    p.unlink()
            except OSError:
                pass

    # -- fetching
    def job_for(self, sha: str, sources) -> FetchJob:
        """The running fetch for sha, or a new one started on `sources` = [(name, open_fn), ...]."""
        with self.lock:
            j = self.jobs.get(sha)
            if j is not None and not j.done:
                return j
            j = FetchJob(sha)
            self.jobs[sha] = j
        threading.Thread(target=self._run_job, args=(j, list(sources)), name=f"omni-update-{sha[:8]}",
                         daemon=True).start()
        return j

    def _run_job(self, j: FetchJob, sources):
        part = self.part_for(j.sha)

        def on_start(total, offset):
            with j.cond:
                if total is not None:
                    j.total = total
                j.written = offset
                j.started = True
                j.cond.notify_all()

        def on_bytes(have):
            with j.cond:
                j.written = have
                j.cond.notify_all()

        for name, open_fn in sources:
            with j.cond:
                j.source = name
            try:
                download_resume(open_fn, part, on_start=on_start, on_bytes=on_bytes)
            except Exception as e:  # noqa: BLE001
                # 404 / 4xx (PermanentFetchError) or out of retries: the partial stays for the next source's Range
                j.tried.append({"source": name, "result": "failed", "detail": f"{type(e).__name__}: {e}"})
                continue
            with j.cond:            # readers open/read/close under this lock, so the rename never races them
                ok = self.commit(part, j.sha) if part.exists() else False
                if ok:
                    j.ok, j.done = True, True
                    j.written = self.path_for(j.sha).stat().st_size
                    j.total = j.written
                    j.tried.append({"source": name, "result": "ok"})
                    j.cond.notify_all()
                    return
                j.gen += 1
                j.written, j.total = 0, None
                j.tried.append({"source": name, "result": "hash mismatch (not cached)"})
                self.last_error = f"{j.sha[:12]}: hash mismatch from {name}; not cached"
                j.cond.notify_all()
        with j.cond:
            j.done, j.ok = True, False
            j.error = "; ".join(f"{t['source']}: {t['result']}" + (f" ({t['detail']})" if t.get("detail") else "")
                                for t in j.tried) or "no source has this file"
            self.last_error = f"{j.sha[:12]}: {j.error}"
            j.cond.notify_all()

    def wait_started(self, j: FetchJob, timeout: float) -> bool:
        """Waits until the job knows where its body starts (or finished). False on timeout."""
        deadline = time.time() + timeout
        with j.cond:
            while not (j.started or j.done):
                left = deadline - time.time()
                if left <= 0:
                    return False
                j.cond.wait(min(left, 1.0))
            return True

    def stream_job(self, j: FetchJob, start: int, write, idle_timeout: float = 60.0) -> int:
        """Writes the job's bytes from `start` as they arrive. Raises IOError when the job fails, the
        partial file is discarded (hash mismatch), or nothing new arrives for idle_timeout."""
        pos = start
        with j.cond:
            gen = j.gen
        while True:
            with j.cond:
                deadline = time.time() + idle_timeout
                while j.gen == gen and not j.done and j.written <= pos:
                    left = deadline - time.time()
                    if left <= 0:
                        raise IOError("update download stalled")
                    j.cond.wait(min(left, 1.0))
                if j.gen != gen or (j.done and not j.ok):
                    raise IOError(j.error or "download discarded (hash mismatch)")
                if j.done and j.ok and pos >= j.written:
                    return pos
                avail = j.written - pos
                path = self.path_for(j.sha) if (j.done and j.ok) else self.part_for(j.sha)
                with open(path, "rb") as f:
                    f.seek(pos)
                    buf = f.read(min(CHUNK * 4, avail))
            if not buf:
                raise IOError("partial file shrank")
            write(buf)
            pos += len(buf)

    def downloads(self) -> list:
        with self.lock:
            jobs = list(self.jobs.values())
        return [j.snapshot() for j in jobs if not j.done]


# ---------------------------------------------------------------------------------------- HTTP helpers

def parse_range(value, size: int):
    """'bytes=a-b' | 'bytes=a-' | 'bytes=-n' -> (start, end_inclusive). None = no/ignored Range (serve all).
    Raises ValueError when unsatisfiable (-> 416)."""
    if not value:
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", str(value))
    if not m or (not m.group(1) and not m.group(2)):
        return None                     # multi-range or junk: serve the whole file (allowed by RFC 9110)
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:
        n = int(m.group(2))
        if n == 0:
            raise ValueError("unsatisfiable")
        start, end = max(0, size - n), size - 1
    end = min(end, size - 1)
    if start >= size or start > end:
        raise ValueError("unsatisfiable")
    return start, end


def send_file(handler, path: Path, range_header, content_type="application/octet-stream", extra=None):
    """Serves a complete file with Range/206/416 support on a BaseHTTPRequestHandler."""
    size = path.stat().st_size
    try:
        rng = parse_range(range_header, size)
    except ValueError:
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{size}")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    start, end = rng if rng else (0, size - 1)
    length = max(0, end - start + 1)
    handler.send_response(206 if rng else 200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(length))
    if rng:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    for k, v in (extra or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    with open(path, "rb") as f:
        f.seek(start)
        left = length
        while left > 0:
            buf = f.read(min(CHUNK * 4, left))
            if not buf:
                break
            handler.wfile.write(buf)
            left -= len(buf)
