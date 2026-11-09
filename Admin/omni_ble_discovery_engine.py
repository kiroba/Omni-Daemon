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
==============================================================================
"""

import threading
import time

OMNI_BLE_SERVICE_UUID = 0xFFA0          # matches NodeRadio SERVICE_UUID 0000FFA0-...
KEY_PREFIX_BYTES = 16                   # service-data = first 16 bytes of the node pubkey
NEARBY_TTL_S = 90.0                     # NodeRadio BLE_FRESH_MS: a sighting stays "nearby" 90 s
MAX_NEARBY = 256                        # hard bound, oldest evicted first
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


if __name__ == "__main__":
    demo = "b0ad355fd5e6aa11" + "22" * 24
    frame = encode_service_data(demo)
    print(f"service data ({len(frame)} bytes): {frame.hex()}")
    print(f"decoded prefix: {decode_service_data(frame)}")
    reg = BleNearbyRegistry(own_node_id=demo)
    print("own sighting refused:", not reg.note_service_data(frame))
    print("peer sighting kept:", reg.note("aa" * 16))
    print(reg.snapshot())
