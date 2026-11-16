"""The Python agent against the Android app's wire test vectors (docs/send-test-vectors.json)."""

from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from argus.send.keys import b64, request_mac, session_key, unb64, verify

V = json.loads((Path(__file__).parent.parent / "docs" / "send-test-vectors.json").read_text(encoding="utf-8"))
RAW = (serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def ed(seed: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(unb64(seed))


def test_keys_and_exchange_match():
    assert b64(ed(V["ed_seed_c"]).public_key().public_bytes(*RAW)) == V["pub_c"]
    assert b64(ed(V["ed_seed_s"]).public_key().public_bytes(*RAW)) == V["pub_s"]
    xc = X25519PrivateKey.from_private_bytes(unb64(V["x_priv_c"]))
    xs = X25519PrivateKey.from_private_bytes(unb64(V["x_priv_s"]))
    assert b64(xc.public_key().public_bytes(*RAW)) == V["eph_c"]
    shared = xc.exchange(X25519PublicKey.from_public_bytes(unb64(V["eph_s"])))
    assert b64(shared) == V["shared"] and shared == xs.exchange(X25519PublicKey.from_public_bytes(unb64(V["eph_c"])))


def test_session_key_signatures_and_mac_match():
    nc, ns = unb64(V["nonce_c"]), unb64(V["nonce_s"])
    k = session_key(unb64(V["shared"]), nc, ns, V["pub_c"], V["pub_s"])
    assert b64(k) == V["K"]
    assert k == session_key(unb64(V["shared"]), nc, ns, V["pub_s"], V["pub_c"])  # the pubkeys are sorted
    assert b64(ed(V["ed_seed_s"]).sign(nc + unb64(V["eph_s"]))) == V["sig_s"]
    assert b64(ed(V["ed_seed_c"]).sign(ns + unb64(V["eph_c"]))) == V["proof_c"]
    assert verify(V["pub_s"], nc + unb64(V["eph_s"]), V["sig_s"])
    assert verify(V["pub_c"], ns + unb64(V["eph_c"]), V["proof_c"])
    m = V["mac"]
    import hashlib

    assert hashlib.sha256(m["body"].encode()).hexdigest() == m["body_sha256"]
    assert request_mac(k, m["method"], m["path"], m["query"], m["body_sha256"], m["seq"]) == m["mac"]
