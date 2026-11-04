# Peer update cache: sharing app updates between paired devices (daemon v8.30.0)

Every Omniverse app updates from `update-manifest.json` on kiroba/The-Omniverse GitHub Releases
(`OmniOta` + `OtaDownloader` in kiroba/Omni-Core-UI). With this release the daemon keeps a verified copy of
that manifest and of the release files it lists, so a paired device can get an update from a device next to
it, and only one device on the LAN needs to download it from GitHub.

Code: `Admin/omni_updates.py` (manifest checks, cache, resumable downloads, Range helpers) and the "peer
update cache" section of `Admin/omni_live_daemon_server.py` (HTTP glue). Tests: `tests/test_peer_updates.py`.
Nothing new to install: Ed25519 comes from `cryptography`, everything else is the standard library.

## Trust model

The daemon checks a manifest exactly the way `OmniOta.manifestTrusted` does in the apps:

* **Format** (what `.github/workflows/release.yml` writes):
  `{"tag": "0.1.24", "build": 1029, "transport": "github", "apps": {"<slug>": {"url": "...", "sha256": "<64 hex>"}}, "signature": "<128 hex>"}`.
* **Signature:** Ed25519 over the canonical JSON of the manifest without `signature`:
  `json.dumps(obj, sort_keys=True, separators=(",", ":"))` (ASCII escapes on), UTF-8. That is what the
  release workflow signs and what Omni-Core-UI's `canonicalJson()` reproduces.
* **Pinned key:** `4f23da25e30a7a9e797a58e01641fa98ad78ed46ff213f43de68dbe9723e9e11`. This is the public key the
  release workflow derives from the `OMNI_OTA_SIGNING_KEY` secret and bakes into every APK as
  `--dart-define=OMNI_OTA_PUBKEY` (the 0.1.24 release run logs "OTA manifests will be signature-pinned to
  4f23da25…"). `OMNI_UPDATE_PUBKEY` overrides it (tests, a fork with its own key). If the signing key is ever
  rotated, the daemon's pin has to change with the apps' pin.
* **No downgrade:** a manifest is adopted only when its `build` is higher than the one held; the same build is
  accepted and the held copy is kept; a lower build is refused (`downgrade refused` in `sources_tried`).
* **Unsigned:** refused by default. The live 0.1.24 manifest is signed, so nothing needs the opt-in today.
  `OMNI_UPDATE_ALLOW_UNSIGNED=1` accepts a manifest with *no* `signature` (a bad signature is always refused).
  Use it only for a private fork that has no signing key.
* **Files** are stored under their SHA-256 and only after the downloaded bytes hash to the manifest entry
  (temp file in `partial/`, then an atomic rename into `files/`). A mismatch deletes the temp file; nothing is
  cached. Peers are never trusted for content: the signed manifest says what the hash must be.
* A peer never fetches anything for another peer. `:5005` serves only what is already in the cache.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `OMNI_UPDATES` | `auto` | `on`, `off` or `auto` (= on). Off: `/api/updates/*` answers 409 (status still works), `:5005` answers 404 inside a session, node-health shows `"enabled": false`. |
| `OMNI_UPDATE_CACHE_DIR` | `~/.omni/updates` | Cache root: `manifest.json`, `manifest.meta.json`, `files/<sha256>`, `partial/<sha256>.part`. |
| `OMNI_UPDATE_CACHE_MB` | `1024` | Size cap for `files/`. LRU eviction (reading a file counts as use). Files the held manifest lists for this node's apps are evicted last, only when nothing else can go. The file just downloaded is never the one evicted. |
| `OMNI_UPDATE_APPS` | all apps in the manifest | Comma-separated app slugs this node keeps (protects them from eviction). |
| `OMNI_UPDATE_PUBKEY` | the pinned release key | 64-hex Ed25519 public key. |
| `OMNI_UPDATE_ALLOW_UNSIGNED` | off | Accept manifests without a signature (see above). |
| `OMNI_UPDATE_MANIFEST_URL` | `https://github.com/kiroba/The-Omniverse/releases/latest/download/update-manifest.json` | Where GitHub's manifest is read from (tests point it at a local server). |
| `OMNI_UPDATE_GITHUB_INTERVAL_MIN` | `30` | GitHub is asked for the manifest at most this often (`?refresh=1` forces it). |
| `OMNI_UPDATE_PEER_INTERVAL_S` | `60` | Paired peers are asked for theirs at most this often. |
| `OMNI_UPDATE_TIMEOUT_S` | `30` | Socket timeout for manifest and file downloads. Downloads retry 3 times per source and resume with `Range`. |
| `OMNI_UPDATE_WIFI_ONLY` | off | Hook for "download from GitHub only on Wi-Fi". On desktop it's a no-op (no metered-network signal); `OMNI_UPDATE_METERED=1` makes it skip GitHub (peers on the LAN are still used). The Android shell can set that variable or replace `omni_updates.network_allows_download`. |

## Endpoints

### Loopback app port `:5000`

Same gate as every other `:5000` path: loopback, plus `X-Omni-Local-Token` (or `Authorization: Bearer <local
token>`) when the node runs with `OMNI_LOCAL_TOKEN=1`. **Not** in the remote-hub allowlists: a remote caller gets
403 "loopback only".

| Call | Answer |
|---|---|
| `GET /api/updates/manifest[?refresh=1]` | Refreshes from paired peers and GitHub (rate-limited as above), then returns the newest verified manifest **exactly as signed** (200, the manifest JSON itself, so an app can re-verify it with its own pinned key). Headers: `X-Omni-Update-Source` (`github`, `peer <host>:<port>`), `X-Omni-Update-Trust` (`signed`/`unsigned`). 404 `{"message": "no verified update manifest yet", "sources_tried": [...]}` when no source had one. |
| `GET /api/updates/file/<sha256>` | The file. From the cache when present (`X-Omni-Update-Source: cache`). Otherwise, only for a hash the held manifest lists: fetched from a paired peer that has it, else from the manifest's GitHub URL, and streamed to the caller while it downloads; it enters the cache only after the hash matches. `Range` (`bytes=a-`, `a-b`, `-n`) gets 206/416. If the hash turns out wrong the stream is cut short (the Core-UI downloader then fails its own SHA-256 check and retries). 400 bad id; 404 not cached and not in the manifest; 502 every source failed (`tried`); 503 no source allowed; 504 no source answered within 40 s. |
| `GET /api/updates/status` | `enabled`, `mode`, `cache_dir`, `cache_bytes`, `cap_bytes`, `files` (`sha256`, `size`, `last_used`, `apps`), `manifest` (`tag`, `build`, `apps`, `source`, `received_at`, `trust`), `pinned_pubkey`, `allow_unsigned`, `wifi_only`, `sources_tried` (last refresh: `source`, `result` = `updated`/`same`/`rejected`/`unreachable`/`skipped`, `detail`), `last_refresh`, `downloads` in progress, `paired_peers`, `last_error`. |

node-health (`:9100`) carries `"updates": {"enabled", "mode", "manifest_build", "manifest_tag",
"manifest_source", "trust", "cache_bytes", "cache_files", "cap_bytes", "downloads", "last_error"}` and lists
`"peer_updates"` in `features`.

### Peer port `:5005`

| Call | Answer |
|---|---|
| `GET /p2p/updates/manifest` | The held manifest as signed (200), or 404. |
| `GET /p2p/updates/file/<sha256>` | The cached file with `Range`/206/416, or 404 when it isn't cached (never fetched onward). |

Only inside a signed quiet-mesh session (the `X-Omni-Session` header from `/p2p/auth`, MAC over
`GET`, the path and an empty body; see [QUIET_MESH.md](QUIET_MESH.md)), i.e. paired devices (or an
`OMNI_TRUSTED_KEYS_FILE` peer that completed the signed hello). No session, a bad one or a replayed counter:
the connection is closed with zero bytes and counts toward the per-IP block, like every other quiet path.
A node that isn't quiet (nobody paired, or `OMNI_QUIET_MESH=off`) answers 403 "update sharing is only for
paired devices". Any other GET on `:5005` is 404 (silent in quiet mode).

The client side uses the same session code as catch-up (`client_session`), dialing paired devices where they
were last seen.

## For app developers

* Put `http://127.0.0.1:5000/api/updates/manifest` ahead of GitHub in `OmniOta.manifestSources`: the body is the
  signed manifest, so `manifestTrusted` works unchanged.
* Download through `http://127.0.0.1:5000/api/updates/file/<sha256 from the manifest>` instead of the entry's
  GitHub `url`. `OtaDownloader` already resumes with Range and checks the SHA-256.
* Today `OmniOta.fetchManifest` sends no local token, so on Omni Node (which runs with `OMNI_LOCAL_TOKEN=1`) these
  calls need the Core-UI change to send it. That change is not part of this PR.

## Tested in the sandbox (`tests/test_peer_updates.py`, 25 tests)

* The real 0.1.24 manifest verifies against the pinned key; one changed field or another key fails it.
* Unsigned refused unless opted in; a bad signature refused even then; shape checks.
* Cache: newer adopted, same build kept, downgrade and bad signature refused, the stored manifest re-verified
  on start (a changed pin drops it); only matching hashes cached; LRU eviction keeps manifest files when it can.
* Range resume: a download cut off half-way resumes with `Range: bytes=N-`; 404 stops at once.
* Three real daemons plus a fake GitHub with a test key (A and B paired, C with updates off, N never paired):
  signed manifest accepted, bad signature and wrong key rejected, downgrade rejected; file from GitHub cached
  on A; loopback Range 206/416; with GitHub down B gets the manifest and the file from A (GitHub never asked
  for it); a file whose bytes don't match is not cached and leaves no partial; a paired session on `:5005`
  gets the manifest and a 206 range, a missing file is 404 (no onward fetch), a replayed counter gets silence;
  a stranger or forged session gets zero bytes; an unpaired node answers 403; `OMNI_UPDATES=off`; the paths
  are not in the remote-hub allowlists.
* Also run by hand against the real GitHub release: the daemon accepted the live 0.1.24 manifest as `signed`
  and fetched `phaser-studio` (18.1 MB) through GitHub's redirect, hash matched, served it again with Range.

## Not tested yet (honest list)

* Real phones and real Wi-Fi: only loopback daemons in the sandbox.
* Android / Chaquopy storage: `~/.omni/updates` resolves under the app's files dir there (as `~/.omni` does for
  the other stores), but it hasn't been run on a device, and nothing yet sets `OMNI_UPDATE_METERED` from the
  Android shell. Large caches may want `OMNI_UPDATE_CACHE_MB` lower on phones.
* Windows: CI runs the suite on windows-latest. Serving a file while eviction removes it is handled (the
  remove fails and eviction moves on), but it hasn't been run on Keith's PC.
* Streaming a file that is still downloading to a caller that asks for a middle range: that caller waits for
  the download to finish, then gets the range from the cache (tested only for open-ended ranges while streaming).
* The daemon doesn't prefetch: a file is fetched the first time an app asks for it.
* No apps use these endpoints yet (Omni-Core-UI change needed, see above).
