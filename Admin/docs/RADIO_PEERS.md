# Radio peers: BLE and Wi-Fi Aware hand-off (daemon v8.18.0)

The daemon can't use phone radios. The **app** on the same device finds peers over BLE or Wi-Fi
Aware and hands each one to its local daemon. The daemon then syncs with it like any other peer
(live relay, plus negentropy catch-up right away).

App → local Omni daemon → other daemons.

## Endpoints (loopback only, never reachable over the remote hub)

`POST :5000/api/sync/peers`
```json
{"host": "fe80::1%aware_data0", "port": 5005, "transport": "wifi_aware", "node_hash": "ab12..."}
```
- `transport`: `ble`, `wifi_aware`, `lan` or `manual`
- `host`: IPv4, IPv6 (scope id allowed) or a hostname. IPv6 literals are bracketed in URLs automatically.
- A peer is kept for 120 s. Re-post to keep it, and at most 64 are kept.
- Answers `{"status":"OK","new":true,...}`. A new peer starts a catch-up run immediately.

`GET :5000/api/sync/peers`: radio peers (with age), static `OMNI_SYNC_PEERS`, and LAN beacon peers.

## What each radio gives
- **BLE**: proximity and identity only (node hash in the advert). There's no IP path, so BLE alone can't
  sync. It tells the app a peer is near so it can start Wi-Fi Aware or use the LAN.
- **Wi-Fi Aware**: discovery plus a data path (Android 10+) with an IPv6 link-local address. That
  address is what the app hands in.

## Manual test (one PC, two daemons)
`python -m unittest tests.test_radio_peers -v`

## IPv6 listener
`:5005` also listens on `[::]` (IPv6 only), so Wi-Fi Aware peers can connect. IPv4 is unchanged.
Turn it off with `OMNI_P2P_IPV6=0`.

## v8.18.1: peer traffic bypasses HTTP proxies

Relay and catch-up requests to peers now go through an opener with no proxy handler. Before this,
`urllib` sent them through `HTTP_PROXY`/`HTTPS_PROXY` (and, on Windows, the system proxy). Its
`NO_PROXY` matching also misses bracketed IPv6 hosts, so a Wi-Fi Aware peer such as
`[fe80::1%aware_data0]` was posted to the proxy and got a 404 back. `tests/test_mesh_sim.py` runs every
daemon with a dead `HTTP_PROXY` set to keep it that way.
