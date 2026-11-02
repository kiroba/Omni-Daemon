# Omni Node: one shared core, thin platform shells (daemon v8.27.0, Node 0.5.0)

Omni Node is one app on every platform. It has the same behaviour, the same words, the same features and
the same version number everywhere. Only the code that has to touch the operating system differs.

```
            omni_node_words.py   every word and colour (the only copy)
                    │
            omni_node_core.py    NodeCore: ui_state() + do(action, args), stdlib only
           ┌────────┼──────────────────────────┬───────────────────────────────┐
  Android shell     │ desktop shell            │ daemon loopback routes        │ iOS shell
  (Kotlin, calls    │ (Tk, in-process:         │ GET  /api/node/ui             │ (not built:
  the core through  │ omni_node_desktop.py)    │ POST /api/node/action         │ see below)
  Chaquopy)         │                          │ POST /api/wan/config          │
```

Until v8.26.0, Android (`NodeStatus.kt`, `NodePairing.kt`, `NodeRelay.kt`, `MainActivity.kt`) and the
desktop window (`omni_node_desktop.py`) each had their own copy of the status logic, the words, the
Network switch, Connect apps, the Relay calls policy, Peers, Pair a device, Internet and the version
line, and the two copies drifted apart. Now each of those exists once, in the core.

## The core (`Admin/omni_node_core.py`)

| Call | I/O? | What it does |
| --- | --- | --- |
| `NodeCore(env=..., settings=..., client=...)` | no | One per process. `env` holds the platform facts (below). |
| `refresh()` | yes, loopback | Reads node-health, the paired devices and the open pairing code. Safe in any process. |
| `duties(force_peers=False)` | yes, loopback | Only the process that owns the node runs this. It applies the Relay calls policy (`POST /api/relay/config`) and the router-port switch (`POST /api/wan/config`), re-hands added peers every 60 s (`POST /api/sync/peers`), checks for a new Omni Node once a day, and writes `~/.omni/node_runtime.json` so other processes see peer and relay states. It returns `{line, node_id, relay_active, network_on}`, which Android uses for the notification and Wi-Fi lock. |
| `sync()` | yes | `refresh()` + `duties()`. The desktop window calls it every 3 s. |
| `ui_state()` | **no** | Everything the screen shows. Safe on a UI thread. |
| `do(action, args)` | may block | Runs one action and returns `{ok, toast, effects, ...}`. `pair_accept` can take up to 45 s and `check_updates` up to 8 s, so call it off the UI thread. |

### `ui_state()` shape (schema `core: 1`)

`platform`, `node_version`, `daemon_version`, `version_line`, `title`, `subtitle`, `words` (every fixed
string, by name, for buttons and dialogs), `colors`, plus one block per section:

- `network {label, on}`
- `status {line, color, detail, node_id, node_line, tooltip}`
- `connect {button, hint}`
- `radios {line, color, ask_permission}`: phones only. It is null when the shell sends no `radios` env.
- `internet {title, line, color, address, switch, on, hint}`
- `relay {section, title, on, hint, line, color, cap_mb, cap_title, caps:[{mb,label}]}`
- `peers {title, hint, button, items:[{key,label,color}]}`
- `pairing {title, hint, quiet_line, quiet_color, offer:{code,expiry,payload,howto,warning}|null, paired:[{pubkey,name,line,color,confirm}], none}`
- `update {title, button, line, color, url, get, newer}`: the update banner
- `background {title, label, on}`: shown only when the shell can start the node at sign-in
- `toasts`: one-off messages, such as "Paired. Your nodes now find each other quietly." Each is handed out once.

### Actions (`do`)

`network {on}`, `token`, `copy_token`, `copy_node_key`, `copy_address`, `add_peer {text}`,
`remove_peer {key}`, `relay {on}`, `relay_cap {mb}`, `internet {on}`, `pair_offer`, `pair_cancel`,
`copy_pair_text`, `pair_accept {text}`, `pair_remove {pubkey}`, `check_updates`,
`start_at_login {on, error}`.

A result can include `copy` (text for the clipboard; `sensitive: true` for the token and pairing text),
`url`, `token`/`howto`/`confirm` (Connect apps), and `effects`. Effects are for the shell to carry out:

| Effect | Shell does |
| --- | --- |
| `start_node` / `stop_node` | Starts or stops the node process. Android uses the foreground service; desktop uses the child daemon. |
| `settings_changed` | Tells the process that owns the node to run `duties()` now. Android sends an intent to the `:daemon` service. On desktop it's the same process, so nothing extra is needed. |
| `set_autostart` | Reserved. Today the desktop shell changes the OS setting first, then reports with `start_at_login {on, error}`. |

### The env adapter

Each shell describes its platform in a small dict. Every key is optional:

| Key | Who sends it | Used for |
| --- | --- | --- |
| `platform` | all | `android`, `ios`, `windows`, `macos` or `linux`. Phones get Tap/Long-press wording, phone relay rules, 250 MB/1 GB/5 GB caps and relay off by default. Desktops get Click wording, 1/5/20 GB/no-limit caps and relay on by default. |
| `charging`, `unmetered`, `battery_saver` | Android | Relay calls policy: a phone relays only while charging on unmetered Wi-Fi with battery saver off. |
| `radios {missing, status}` | Android | The Nearby radios line. `status` is NodeRadio's `radio_status.json`. |
| `autostart_available` | desktop | Shows the Background section ("Start when I sign in"). |
| `attached` | desktop | A daemon this window didn't start is answering, so the card says so. |
| `node_error` | Android service, desktop | Why the node process stopped. Shown as "Problem: ...". |
| `default_network_on` | Android | The old Android switch value, for the first run of the shared settings. |
| `can_stop_node` | daemon routes | `False`: the Network switch belongs to the app that runs the node. |
| `auto_update_check` | all | Daily release check. On by default; `OMNI_NODE_AUTO_UPDATE=0` turns it off too (CI does this). |
| `os` | Android | Kept for reports; not shown. |

### Settings: one file, every platform

`~/.omni/node_settings.json` holds `network_on`, `relay_on`, `relay_mb_day`, `internet_on`, `peers`,
`start_at_login` and `update`. On Android, `HOME` is the app's files dir. The file is re-read whenever it
changes, and every write is a read-modify-write, so Android's screen process and its `:daemon` service
(or the desktop window and the daemon's `/api/node/*`) never undo each other's changes. On first run the
core carries over the old copies: desktop `~/.omni/node_desktop.json`, and Android `peers.txt` +
`relay.json`. Android's `BootReceiver` reads `network_on` straight from the same file (no Python at boot).

### What changed in behaviour

- **One relay policy.** The phone rule (charging + unmetered Wi-Fi) now also respects battery saver, and the core decides it for every platform.
- **Internet switch** ("Open a port on my router"). This new switch flips the daemon's UPnP live through `POST /api/wan/config`, and the setting is passed as `OMNI_UPNP` when the node starts. When the switch is off, the node doesn't map a port. A mapping the router already made isn't removed; it expires on the router's own timer.
- **Check for updates** (new). It reads `https://api.github.com/repos/kiroba/Omni-Node-Daemon/releases`, takes the newest non-draft release (pre-releases count), and compares it with the Node version. The check runs on demand and once a day. It is unauthenticated: GitHub sees the device's IP, and the limit is 60 requests an hour. `OMNI_NODE_UPDATE_URL` points it elsewhere (the tests use a local fake).
- **The same Node version everywhere.** The version comes from `OMNI_NODE_VERSION`, else a `NODE_VERSION` file next to the core, else `dev`. The Omni-Node-Daemon release writes `NODE_VERSION` from its single `VERSION` file into the APK and into every desktop zip, so they match.
- **Unified wording.** Where the two copies had drifted, one wording was kept: "Paste it **into** the app's Hub settings", "Only reachable on this **network** so far", and peer lines with ● syncing / ○ waiting.

## The daemon routes (loopback + local token)

| Route | Body | Answer |
| --- | --- | --- |
| `GET /api/node/ui` | | `ui_state()` after a `refresh()`. Here `status` is the status card, not `"OK"`. |
| `POST /api/node/action` | `{"action": "...", "args": {...}}` | `do()` result plus `status: OK/ERROR`. `network` and `start_at_login` return 409, because they belong to the app that runs the node. An unknown action returns 400. |
| `POST /api/wan/config` | `{"upnp": true\|false}` | `{status, upnp}`. Returns 503 before the internet check has started. |

None of these routes is in `REMOTE_*_PATHS`, so a request from off the box gets 403. When `OMNI_LOCAL_TOKEN=1`, a loopback caller without the token gets 401. They live in one marked block in `omni_live_daemon_server.py` ("Omni Node shared core hooks"). The core is imported lazily, so the block names `omni_node_core` and `omni_node_words` in a packaging hint string for `fetch_daemon.sh`.

## What stays per platform

| | Android (Kotlin) | Desktop (Tk) | iOS (not built) |
| --- | --- | --- | --- |
| Run the node | Foreground service `connectedDevice` in its own `:daemon` process, Chaquopy | Child process `omni_live_daemon_server.py`, or attach to one already running | Only while the app is open (see below) |
| Start by itself | `BootReceiver` (boot + app update) | Run key / LaunchAgent / autostart file | No |
| Background extras | Notification, battery-optimisation prompt, multicast lock, Wi-Fi lock while relaying | Tray icon (pystray) | n/a |
| Platform facts | Charging, unmetered Wi-Fi, battery saver, radio permissions → `env` | Attached / child exit reason → `env` | Low Power Mode, Wi-Fi |
| Radios | BLE + Wi-Fi Aware (`NodeRadio.kt`), which hands found peers to the daemon itself | none | Multipeer/BLE (later) |
| Secrets UI | BiometricPrompt / device credential before showing the token | Confirm dialog | Face ID |
| Clipboard | `IS_SENSITIVE` flag for the token and pairing text | Tk clipboard | `UIPasteboard` with expiry |

Every word these shells show comes from `ui_state()["words"]` or a section block. That includes the
notification channel and battery prompt on Android, and the tray menu on desktop. Android caches the last
words in `~/.omni/node_words.json`, so the service notification can be labelled before Python starts.

## Parity checks (CI)

- Omni-Daemon `tests/test_node_core.py::PackagingAndParity` fails if `omni_node_desktop.py` contains a string literal that spells out Node words (any `omni_node_words` string of 10+ characters, or a status phrase such as "Connected to" or "Quiet mode"). It also pins the packaging hint and checks the core is stdlib-only.
- Omni-Node-Daemon `scripts/check_parity.py` (job `parity`) does the same for every `.kt` file, against the `omni_node_words.py` that `fetch_daemon.sh` packaged.
- `OneStateEveryPlatform` in the same test shows that the same node gives the same status line, colour, detail and words on Android and desktop.

To change a word, change it in `omni_node_words.py`. There is nothing to keep in sync by hand.

## Releases: one tag, every platform

Omni-Node-Daemon holds the single `VERSION` (for example `0.5.0`). One `v*` tag there (for example
`v0.5.0-prealpha`, which must start with `v<VERSION>`) builds the following, from one Omni-Daemon commit
resolved once at the start of the run:

- `app-debug.apk` (Android, with `NODE_VERSION` packaged)
- `omni-node-windows-x64.zip`, built the same way as The-Omniverse `omni-node-windows.yml`: Python sources, `Start Omni Node.cmd`, smoke-tested on windows-latest
- `omni-node-linux.tar.gz` and `omni-node-macos.tar.gz`: the same sources plus `start-omni-node.sh`, smoke-tested on ubuntu-latest and macos-latest

A final `publish` job checks that all four files are present and that each desktop archive's
`NODE_VERSION` matches `VERSION`. Only then does it create the release, because releases are immutable.

## iOS notes (not built)

An iOS Omni Node cannot be always-on. iOS suspends apps a few seconds after they leave the foreground.
There is no general-purpose background service, the closest equivalent to Android's foreground service is
not allowed for this use, and `BGTaskScheduler` gives short, OS-chosen windows (minutes, at most a few
times a day). A phone running the iPhone app is a node only while the app is open, or briefly during a
background fetch or a push-triggered wake. Planned shape when it's built:

- The same core, embedded with a Python runtime (for example BeeWare's Python-Apple-support / Briefcase, or a PythonKit host), or later a Rust port of the core with the same `ui_state` schema. SwiftUI renders `ui_state()`; `env` sends `platform: "ios"`, Low Power Mode and Wi-Fi.
- Relay calls stays off on iOS, because the app can't be relied on to be running. The phone uses another node (a home PC or an Android phone) through Add peer or pairing.
- No sign-in start and no Background section (`autostart_available` stays false).
- App Store review: the token screen needs Face ID / passcode, and local network access needs the `NSLocalNetworkUsageDescription` prompt.

None of this exists yet; nothing in the repos claims iOS support.
