#!/usr/bin/env python3
"""
==============================================================================
OMNIVERSE BLE DISCOVERY ENGINE [v2.0.0] - daemon v8.32.0
==============================================================================
Proximity discovery over Bluetooth Low Energy, wired into the live daemon.

THE WIRE FORMAT IS THE ONE ALREADY IN THE FIELD. The Omni Node Android app
(NodeRadio.kt) advertises service UUID 0xFFA0 whose service-data value is the
first 16 bytes of the node's Ed25519 public key, and scans for the same.
This engine speaks exactly that format - the daemon and every phone build
see each other. (The v1 frame in this file predates NodeRadio, matched
nothing in the field, and is gone.)

Trust model, same as every other discovery surface in the daemon:
- A BLE sighting is a HINT, never a peer. It carries no address and proves
  nothing; it says "a node whose key starts with these 16 bytes was near".
- Peers are still made the usual ways only: signed UDP beacons, quiet-mesh
  pairing, radio peers with an address (Wi-Fi Aware), WAN dial-out.
- Privacy: the registry stores only the 16-byte key prefix (hex) and
  sighting times - the same exposure as the beacon pubkey prefixes the
  telemetry summary already prints. Own sightings are never recorded.

Who reports sightings: the phone's radio layer (NodeRadio) sees BLE
advertisements the Python daemon cannot reach on Android; it hands them to
the loopback endpoint POST :5000/api/ble/nearby with the local token.
Desktop daemons simply report an empty registry (no BLE stack, no server
code path) - BLE is a phone/edge transport, relays do not need it.

The registry is in-memory and bounded (MAX_NEARBY), pruned on read past
NEARBY_TTL_S - consistent with the daemon's no-logs posture: nothing about
proximity is ever written to disk.

Introductions (v8.33.0): a sighting stays a hint, but a completed GATT
exchange is more than a sighting - the peer hands over its full public key,
its mesh and name, its current endpoints, and a signed hello (the
omni.p2p.hello.v1 bytes). The daemon verifies that signature
(omni_live_daemon_server.ble_met_report) and only then records the meeting
in BleMetStore below: full key -> name + endpoints, written to disk
(~/.omni/ble_met.json, same posture as paired_peers.json) so a meeting
survives restarts and never expires on the 120 s radio-peer TTL. The stored
endpoints join the catch-up dial set for MET_DIAL_WINDOW_S after the
meeting, so when an IP path appears the two nodes sync without anyone
re-doing the introduction.
==============================================================================
"""

import json
import threading
import time
from pathlib import Path

OMNI_BLE_SERVICE_UUID = 0xFFA0          # matches NodeRadio SERVICE_UUID 0000FFA0-...
KEY_PREFIX_BYTES = 16                   # service-data = first 16 bytes of the node pubkey
NEARBY_TTL_S = 90.0                     # NodeRadio BLE_FRESH_MS: a sighting stays "nearby" 90 s
MAX_NEARBY = 256                        # hard bound, oldest evicted first
MAX_MET = 512                           # hard bound on stored introductions, least-recently-met evicted
MET_DIAL_WINDOW_S = 24 * 3600.0         # a met node's endpoints join catch-up dials for a day
_HEX = set("0123456789abcdef")


def key_prefix_hex(node_id: str) -> str:
    """First 16 bytes of a node id (public key hex) as 32 lowercase hex chars.
    Mirrors NodeRadio.keyPrefix(): non-hex characters are dropped, short
    keys are zero-padded on the right."""
    clean = "".join(c for c in str(node_id or "").lower() if c in _HEX)
    return clean[: KEY_PREFIX_BYTES * 2].ljust(KEY_PREFIX_BYTES * 2, "0")


def encode_service_data(node_id: str) -> bytes:
    """The exact bytes NodeRadio advertises for this node (16-byte key prefix)."""
    return bytes.fromhex(key_prefix_hex(node_id))


def decode_service_data(data) -> str | None:
    """The advertised key prefix from raw service data, or None when the data
    cannot be an Omni advertisement. NodeRadio sends exactly 16 bytes; longer
    values are accepted by prefix (some stacks append), shorter refused."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < KEY_PREFIX_BYTES:
        return None
    return bytes(data[:KEY_PREFIX_BYTES]).hex()


class BleNearbyRegistry:
    """In-memory sightings: key prefix -> last-seen seconds. Thread-safe."""

    def __init__(self, own_node_id: str = "", ttl_s: float = NEARBY_TTL_S, max_entries: int = MAX_NEARBY):
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._own = key_prefix_hex(own_node_id) if own_node_id else None
        self._ttl = float(ttl_s)
        self._max = int(max_entries)
        self.reports = 0          # accepted sighting reports since start
        self.rejected = 0         # refused reports (bad shape, own key, stale)

    def set_own(self, node_id: str):
        with self._lock:
            self._own = key_prefix_hex(node_id)

    def note(self, prefix_hex, ts: float = None) -> bool:
        """Record one sighting. Returns False (and counts a reject) for a
        malformed prefix or for this node's own advertisement."""
        if not isinstance(prefix_hex, str):
            self.rejected += 1
            return False
        p = prefix_hex.strip().lower()
        if len(p) != KEY_PREFIX_BYTES * 2 or any(c not in _HEX for c in p):
            self.rejected += 1
            return False
        with self._lock:
            if p == self._own:
                self.rejected += 1
                return False
            self._seen[p] = time.time() if ts is None else float(ts)
            self.reports += 1
            if len(self._seen) > self._max:      # evict oldest first
                for k in sorted(self._seen, key=self._seen.get)[: len(self._seen) - self._max]:
                    del self._seen[k]
            return True

    def note_service_data(self, data) -> bool:
        p = decode_service_data(data)
        return self.note(p) if p else False

    def _pruned(self, now: float) -> dict:
        return {k: v for k, v in self._seen.items() if now - v < self._ttl}

    def snapshot(self, now: float = None) -> dict:
        """node-health / telemetry shape: hashes only, freshest first."""
        now = time.time() if now is None else now
        with self._lock:
            fresh = self._pruned(now)
            self._seen = fresh
            items = sorted(fresh.items(), key=lambda kv: -kv[1])
            return {
                "nearby": len(items),
                "prefixes": [k for k, _ in items[:32]],
                "last_seen_ago_s": None if not items else round(max(0.0, now - items[0][1]), 1),
                "reports": self.reports,
                "rejected": self.rejected,
            }


class BleMetStore:
    """Persistent introductions verified over BLE: full node id -> record.

    Records are created ONLY from signature-verified introductions (the
    verification lives in the daemon server; this class just stores). A
    record holds the peer's mesh, display name and the endpoints it
    advertised at the meeting. Unlike radio peers there is no freshness
    TTL: a meeting is a durable fact, like a pairing. Endpoints are offered
    as dial targets for MET_DIAL_WINDOW_S after the last meeting - long
    enough for an IP path to appear, short enough that a stale address is
    not dialed forever.
    """

    def __init__(self, path=None, max_entries: int = MAX_MET):
        self._lock = threading.Lock()
        self._met: dict[str, dict] = {}
        self._max = int(max_entries)
        self._path: Path | None = None
        self.accepted = 0          # verified introductions recorded since start
        self.rejected = 0          # refused introductions since start (counted by the server)
        if path:
            self.bind(path)

    def bind(self, path):
        """Attach the on-disk store (called once at daemon startup)."""
        with self._lock:
            self._path = Path(path) if path else None
            self._load_locked()

    def _load_locked(self):
        if not self._path:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            rows = raw.get("met") if isinstance(raw, dict) else None
            for r in rows or []:
                if isinstance(r, dict) and isinstance(r.get("node_id"), str):
                    self._met[r["node_id"].lower()] = r
        except Exception:
            pass      # a corrupt store must never stop the daemon; meetings re-happen

    def _save_locked(self):
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            rows = sorted(self._met.values(), key=lambda r: r.get("last_met", 0))
            tmp.write_text(json.dumps({"version": 1, "met": rows}, indent=1), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            pass

    def record(self, node_id: str, mesh_id: str, name: str, endpoints: list, ts: float = None) -> dict:
        """Store (or refresh) one verified introduction. Returns the record."""
        now = time.time() if ts is None else float(ts)
        nid = str(node_id).lower()
        with self._lock:
            old = self._met.get(nid)
            rec = {"node_id": nid, "mesh_id": mesh_id, "name": name,
                   "endpoints": list(endpoints),
                   "first_met": old["first_met"] if old else now,
                   "last_met": now}
            self._met[nid] = rec
            self.accepted += 1
            if len(self._met) > self._max:      # evict least-recently-met first
                for k in sorted(self._met, key=lambda k: self._met[k]["last_met"])[: len(self._met) - self._max]:
                    del self._met[k]
            self._save_locked()
            return dict(rec)

    def get(self, node_id: str) -> dict | None:
        with self._lock:
            r = self._met.get(str(node_id).lower())
            return dict(r) if r else None

    def count(self) -> int:
        with self._lock:
            return len(self._met)

    def dial_targets(self, now: float = None, window_s: float = MET_DIAL_WINDOW_S) -> list:
        """[(host, port, node_id)] from meetings inside the dial window."""
        now = time.time() if now is None else now
        out = []
        with self._lock:
            for rec in self._met.values():
                if now - rec.get("last_met", 0) > window_s:
                    continue
                for ep in rec.get("endpoints") or []:
                    out.append((ep["host"], int(ep["port"]), rec["node_id"]))
        return out

    def snapshot(self) -> dict:
        """node-health / telemetry shape: key prefixes only, freshest first."""
        with self._lock:
            items = sorted(self._met.values(), key=lambda r: -r.get("last_met", 0))
            return {"count": len(items),
                    "prefixes": [key_prefix_hex(r["node_id"]) for r in items[:32]],
                    "accepted": self.accepted,
                    "rejected": self.rejected}


if __name__ == "__main__":
    demo = "b0ad355fd5e6aa11" + "22" * 24
    frame = encode_service_data(demo)
    print(f"service data ({len(frame)} bytes): {frame.hex()}")
    print(f"decoded prefix: {decode_service_data(frame)}")
    reg = BleNearbyRegistry(own_node_id=demo)
    print("own sighting refused:", not reg.note_service_data(frame))
    print("peer sighting kept:", reg.note("aa" * 16))
    print(reg.snapshot())
