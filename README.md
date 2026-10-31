# Omni-Daemon

The networked protocol layer of The Omniverse. One daemon runs on each device. It keeps the signed event log, syncs with peers (negentropy catch-up), offers remote access over Tailscale, runs the test-credits ledger, and handles OmniCast signaling. Apps only ever talk to their own device's daemon.

- Daemon and engines: `Admin/` (docs in `Admin/docs/`)
- Tests: `python3 -m unittest tests.<name>`, run from the repo root

Split out of [kiroba/The-Omniverse](https://github.com/kiroba/The-Omniverse) (`desktop` branch) with full history, minus tracked `*.db` files. Formerly named The-Omni-Hub.
