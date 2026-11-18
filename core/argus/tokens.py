"""Named API tokens, revocable one by one. One scope: full access (like the worker token), except managing tokens.

Only the SHA-256 of a token is stored. `check` is called on every request, so it looks the hash up in an in-memory
dict (hash -> name) that is rebuilt on create/revoke and at most every REFRESH seconds (another process may have
revoked a token). `last_used_at` is written at most once a minute per token.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time

from .db.store import Store

PREFIX = "ar_"  # lets log redaction recognise a token by shape
REFRESH = 30.0
TOUCH = 60.0


class TokenError(ValueError):
    pass


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _active(conn: sqlite3.Connection) -> dict[str, str]:
    return {h: n for h, n in conn.execute("SELECT hash, name FROM api_token WHERE revoked_at IS NULL")}


class Tokens:
    def __init__(self, store: Store):
        self.store = store
        self._map: dict[str, str] = {}
        self._loaded = float("-inf")
        self._touched: dict[str, float] = {}
        # A periodic re-read in `check` (a worker thread) must not overwrite a newer list from create/revoke with the
        # older one it read just before: each create/revoke bumps the generation, and a stale read is dropped.
        self._lock = threading.Lock()
        self._gen = 0

    async def _refresh(self) -> None:
        with self._lock:
            self._gen += 1
        fresh = await self.store.read(_active)
        with self._lock:
            self._gen += 1
            self._map = fresh
            self._loaded = time.monotonic()

    async def create(self, name: str) -> str:
        name = name.strip()
        if not name or len(name) > 60:
            raise TokenError("the name must be 1 to 60 characters")
        token = PREFIX + secrets.token_urlsafe(32)
        h, now = digest(token), time.time()

        def w(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT revoked_at FROM api_token WHERE name=?", (name,)).fetchone()
            if row is not None and row[0] is None:
                raise TokenError(f"a token named {name!r} already exists")
            conn.execute("DELETE FROM api_token WHERE name=?", (name,))  # a revoked name can be made again
            conn.execute("INSERT INTO api_token(name, hash, created_at) VALUES(?,?,?)", (name, h, now))

        await self.store.write(w)
        await self._refresh()
        return token

    async def list(self) -> list[dict]:
        def r(conn: sqlite3.Connection) -> list[dict]:
            rows = conn.execute("SELECT name, created_at, last_used_at, revoked_at FROM api_token ORDER BY id")
            return [{"name": n, "created_at": c, "last_used_at": u, "revoked_at": v} for n, c, u, v in rows]
        return await self.store.read(r)

    async def revoke(self, name: str) -> bool:
        name = name.strip()

        def w(conn: sqlite3.Connection) -> int:
            return conn.execute("UPDATE api_token SET revoked_at=? WHERE name=? AND revoked_at IS NULL",
                                (time.time(), name)).rowcount

        done = bool(await self.store.write(w))
        await self._refresh()
        return done

    def check(self, presented: str | None) -> str | None:
        """The token's name when it is a live named token, else None."""
        if not presented:
            return None
        if time.monotonic() - self._loaded > REFRESH:
            gen = self._gen
            try:
                fresh = self.store.read_sync(_active)
            except Exception:  # keep the last good list rather than fail every request
                fresh = None
            with self._lock:
                if fresh is not None and gen == self._gen:
                    self._map = fresh
                self._loaded = time.monotonic()
        h = digest(presented)
        name = self._map.get(h)
        if name is not None:
            self._touch(h)
        return name

    def _touch(self, h: str) -> None:
        now = time.monotonic()
        if now - self._touched.get(h, float("-inf")) < TOUCH:
            return
        self._touched[h] = now
        wall = time.time()
        try:
            self.store.submit(lambda c: c.execute("UPDATE api_token SET last_used_at=? WHERE hash=?", (wall, h)))
        except Exception:
            pass
