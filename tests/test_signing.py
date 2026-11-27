import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parent.parent / 'Admin'))
"""Run: python test_signing.py  (no daemon needed)"""
import tempfile
from pathlib import Path

from omni_signing import (load_or_create_key, pubkey_hex, sign_event, verify_event,
                          sign_lpe_profile, verify_lpe_profile)

tmp = Path(tempfile.mkdtemp())

# 1. Key survives a restart
a1 = load_or_create_key(tmp / "a.key")
a2 = load_or_create_key(tmp / "a.key")
assert pubkey_hex(a1) == pubkey_hex(a2), "key changed across restart"
print("[pass] same identity after restart:", pubkey_hex(a1)[:16], "...")

alice = a1
mallory = load_or_create_key(tmp / "m.key")

event = {"action": "CHAT_MESSAGE", "author_pubkey": pubkey_hex(alice),
         "payload": {"text": "hello from node A"}, "previous_hash": "0x" + "0" * 64,
         "timestamp": 1761590000.25}
event["signature"] = sign_event(alice, event)

# 2. Real event verifies
assert verify_event(event)
print("[pass] genuine event verifies")

# 3. Tampered payload fails
t = dict(event, payload={"text": "send me your keys"})
assert not verify_event(t)
print("[pass] edited payload rejected")

# 4. Forged author: Mallory claims to be Alice, signs with her own key
forged = dict(event, payload={"text": "i am alice, trust me"})
forged["signature"] = sign_event(mallory, forged)
assert not verify_event(forged)
print("[pass] forged author rejected")

# 5. Missing / garbage signature fails cleanly (no crash)
assert not verify_event({k: v for k, v in event.items() if k != "signature"})
assert not verify_event(dict(event, signature="zz"))
print("[pass] missing or malformed signature rejected")

# 6. LPE profile attestation (R-19): the daemon signs a profile view with its node key
profile = {"pubkey": pubkey_hex(alice), "handle": "alice", "event_id": "0x" + "11" * 32}
reputation = {"score": 500, "tier": "CITIZEN"}
resp = {"status": "OK", "profile": profile, "reputation": reputation,
        "sign": sign_lpe_profile(alice, profile, reputation, ts=1761590000000)}
assert verify_lpe_profile(resp)
print("[pass] daemon-attested profile verifies")

# 7. Tampered profile / reputation / signer / unsigned all fail
assert not verify_lpe_profile(dict(resp, profile=dict(profile, handle="mallory")))
assert not verify_lpe_profile(dict(resp, reputation=dict(reputation, score=9999)))
assert not verify_lpe_profile(dict(resp, sign=dict(resp["sign"], signer=pubkey_hex(mallory))))
assert not verify_lpe_profile({"status": "OK", "profile": profile, "reputation": reputation})
print("[pass] tampered or unsigned profile rejected")
print("ALL 7 PASSED")
