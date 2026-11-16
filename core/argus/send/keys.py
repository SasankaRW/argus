"""Device keys (Ed25519), the session key and the per-request MAC."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


class DeviceKey:
    """This device's key pair: made on first start, the private half stays in the agent's data dir."""

    def __init__(self, path: Path):
        if path.exists():
            self._key = Ed25519PrivateKey.from_private_bytes(unb64(path.read_text(encoding="ascii").strip()))
        else:
            self._key = Ed25519PrivateKey.generate()
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = self._key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                          serialization.NoEncryption())
            path.write_text(b64(raw), encoding="ascii")
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
        self.pub = b64(self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    def sign(self, data: bytes) -> str:
        return b64(self._key.sign(data))


def verify(pub: str, data: bytes, sig: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(unb64(pub)).verify(unb64(sig), data)
        return True
    except (InvalidSignature, ValueError):
        return False


class Ephemeral:
    """A fresh X25519 key pair for one hello: a sniffer sees only the public halves and cannot compute K."""

    def __init__(self):
        self._key = X25519PrivateKey.generate()
        self.pub = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def shared(self, peer_pub: bytes) -> bytes:
        return self._key.exchange(X25519PublicKey.from_public_bytes(peer_pub))


def session_key(shared: bytes, nonce_c: bytes, nonce_s: bytes, pub_c: str, pub_s: str) -> bytes:
    """K = HKDF-SHA256(X25519 secret, salt=nonce_c||nonce_s, info="argus-send v1"||client pubkey||server pubkey)."""
    info = b"argus-send v1" + unb64(pub_c) + unb64(pub_s)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=nonce_c + nonce_s, info=info).derive(shared)


def request_mac(key: bytes, method: str, path: str, query: str, body_sha256: str, seq: str | int = "") -> str:
    msg = "|".join((method.upper(), path, query, body_sha256, str(seq))).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()
