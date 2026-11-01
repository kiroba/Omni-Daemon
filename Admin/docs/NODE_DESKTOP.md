# Omni Node on Windows, macOS and Linux

The same screen as Omni Node on Android, for the computer that runs your daemon.

```
python Admin/omni_node_desktop.py            # or double-click Admin/omni_node_desktop.pyw on Windows
pip install pystray pillow                   # optional: tray icon (status colour, Open / Pause / Quit)
```

| Section | Same as Android | Desktop difference |
| --- | --- | --- |
| Network switch + status card | Same words and colours (`omni_node_words.py`, pinned by `tests/test_node_desktop.py`) | Attaches to a daemon that's already running instead of starting a second one |
| Connect apps | Shows the node token with the same instructions | Asks you to confirm instead of a screen lock |
| Internet | Your address and whether others can reach you (node-health `wan`) | |
| Relay calls | Same switch and status line | On by default, no limit unless you pick one (1, 5 or 20 GB a day) |
| Peers | Add a friend's address, your phone or a server; re-handed to the daemon every 60 s | |
| Background | | "Start when I sign in": per-user Run key on Windows, LaunchAgent on macOS, autostart file on Linux. No admin needed |

The app starts the daemon with `OMNI_LOCAL_TOKEN=1` and `PYTHONUTF8=1`, exactly like the Android app, and logs it to `~/.omni/node_desktop_daemon.log`. Settings live in `~/.omni/node_desktop.json`.

With a tray icon, closing the window keeps the node running (like closing the Android app). Without one, closing the window quits and stops the node it started.

Changing a status word? Change it in Omni Node's `NodeStatus.kt` / `MainActivity.kt` and in `omni_node_words.py` together. The test fails if they drift.
