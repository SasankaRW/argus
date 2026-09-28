"""ULID generation: 26-character, time-sortable IDs that are safe to create on any machine."""

from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id() -> str:
    """Return a new ULID. IDs from this process always sort in creation order, even if the clock steps back."""
    global _last_ms, _last_rand
    with _lock:
        ms = int(time.time() * 1000)
        if ms <= _last_ms:  # same millisecond, or the clock went backwards: keep counting from the last id
            ms = _last_ms
            _last_rand += 1
            if _last_rand >= 1 << 80:  # random part used up: borrow the next millisecond
                ms += 1
                _last_rand = int.from_bytes(os.urandom(10), "big") >> 1
        else:
            _last_rand = int.from_bytes(os.urandom(10), "big") >> 1  # top bit clear leaves room to count up
        _last_ms = ms
        return _encode(ms, 10) + _encode(_last_rand, 16)
