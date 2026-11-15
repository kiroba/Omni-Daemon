# Remote tester guide: reach Keith's node through the mesh

_Rewritten Nov 15, 2026. The earlier version of this guide routed testers to one shared hub
through a third-party overlay VPN, with accounts on that service. That method is retired: the
mesh does it itself now, and no tester needs an account with anyone but the Omniverse._

## The model now

There is no shared hub to borrow. **Each tester runs their own Omni Node**, and their node syncs
with Keith's node, which is the reachable one (it sits at his home on a PC that stays on). A
tester's node dials out to Keith's node, they exchange a signed hello, and catch-up sync pulls
and pushes until both stores hold the same signed events. KickBack on the tester's phone talks
to the tester's **own** node — posts are signed by the tester's own key, travel node-to-node,
and land on Keith's node (and every other node they meet) by sync, not by logging into his hub.

How the daemon side works (rendezvous, dial-out, reachability checks) is in [WAN.md](WAN.md).
How a remote hub on one device is configured is in [REMOTE_HUB.md](REMOTE_HUB.md).

## Ports

| Port | Used by testers? | What it is |
|---|---|---|
| TCP 5005 | **Yes** | Node-to-node P2P sync. The tester's node dials Keith's node here; Keith's router must let it in (UPnP or a manual forward). |
| TCP 5000 | Own device only | App API between KickBack and the tester's own node, on the same phone. Never crosses the internet. |
| TCP 9100 | Own device only | Telemetry between the apps and the tester's own node. |
| UDP 9101 | No | LAN discovery broadcast. Doesn't leave the local network. |

---

## A. Keith: one-time setup

### 1. Run the reachable node
Run Omni Node on the home PC and leave it on. In the node app, check the **Internet** line: it
shows the node's public address (e.g. `203.0.113.10:5005`) and whether the router lets people
in. The daemon asks the router to forward TCP 5005 by UPnP (`OMNI_UPNP=0` turns that off); if
UPnP is off or blocked, forward TCP 5005 to the PC by hand in the router settings.

### 2. Share the address — privately
Send each tester the node's current public address, privately, the way you'd send a password.
Two cautions:

- **Residential addresses change.** If testers suddenly can't connect, the address probably
  changed; read the new one off the node's Internet line and re-send it.
- The address alone is not a key: a stranger who dials it still has to pass the signed hello,
  and with a private mesh (below) their events go nowhere. But don't post it publicly either.

### 3. Optional: make it a private test mesh
For an invited-only round, run Keith's node with a private `OMNI_MESH_ID` plus
`OMNI_TRUSTED_KEYS_FILE` holding each tester's node key. Only trusted keys sync. Give testers
the mesh id privately along with the address.

### 4. Revoke one tester
Remove their peer entry, and if the round is private-mesh, take their key out of the
trusted-keys file and restart the node. Their node stops converging with yours from that
point; events they already synced stay in the shared history (signed events are the record —
that part is by design). Rotating the mesh id and re-inviting the remaining testers is the
hard reset.

---

## B. Tester steps (Android 8.0 or newer)

1. **Install Omni Node** from the link Keith sent (the current release). Open it and turn the
   **Network** switch on. Allow notifications and "keep running in background".
2. **Add Keith's node as a peer.** In the node's **Peers** section, use **Add peer** and enter
   the public address Keith sent you. Within a few seconds the peer should show as connected /
   nearby, and sync counters start moving.
3. **Install KickBack** from the same release. Open it and read and accept the **Tester
   agreement** (pre-alpha consent screen).
4. **Point KickBack at your own node.** In Omni Node, tap **Connect apps**, unlock, and **Copy
   token**. In KickBack, open **Hub settings** (gear), choose **Remote hub**, type `127.0.0.1`,
   and paste the token. (It says "remote" but the address is your own phone — the token is your
   own node's, and Keith never sees it.)
5. **Post and react.** Make a post in KickBack and react to one. Your node carries it to Keith's
   node on the next sync — give it a minute.
6. **Report bugs** from the app's **Report a bug** sheet: describe what happened, then tap
   **Report on GitHub** (opens a prefilled issue) or **Email instead**. App version, device model
   and Android version are added for you. Don't include passwords, tokens, or private messages.

Keep your node token to yourself, same as any password.

---

## C. Troubleshooting

| What you see | Meaning | Fix |
|---|---|---|
| Peer never connects | No path to Keith's node. | Keith's PC is off or asleep, his daemon isn't running, the address changed (ask Keith for the current one), or his router isn't forwarding 5005. |
| Peer connects, nothing syncs | The signed hello or the mesh gate refused. | If it's a private-mesh round: your node key isn't in Keith's trusted list, or your mesh id differs. Ask Keith to check. |
| KickBack says **unauthorized** | KickBack's token doesn't match your own node. | Re-copy the token from your node's **Connect apps** and paste it again exactly (no spaces). |
| KickBack says **unreachable** | Your own node isn't answering. | Open Omni Node, check the Network switch is on and the status card is green/teal. |

**Android older than 8.0 is not supported.** The APKs are built with `minSdk 26` (Android 8.0);
older phones can't install them.

## Status (honest)

Node-to-node rendezvous over the internet is proven between Keith's own phone and home PC
(Nov 14, 2026: the phone, on mobile data miles from home, reached the PC node, reconciles
served with zero errors, and a relayed post crossed). The first run with an outside tester's
node is the next field test; if the peer step fails for a real tester, this guide gets the fix
written into it.
