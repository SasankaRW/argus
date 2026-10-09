"""Ari's voice: the voice server (voice_server.py, Chatterbox on the PC's GPU) as argusd and the listener see it.

There is one voice, and the server owns it (the clip it sounds like is set there, not per request). When it can't
speak (the PC is off, the server is still loading, it failed) Ari shows the words and stays silent; it never
switches to a second voice.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("argus.voice")


class Expressive:
    """A WAV for a text, or None when the server can't make one (not running, still loading, failed)."""

    def __init__(self, url: str, timeout: float = 30, token: str | None = None):
        self.url, self.timeout = url.rstrip("/"), timeout
        self.headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
        self._down_until = 0.0
        self.ok_at = 0.0  # when it last made a sentence (a hiccup right after that is retried, not swapped)

    def recent(self, seconds: float = 120) -> bool:
        return time.time() - self.ok_at < seconds

    @property
    def remote(self) -> bool:
        """On another machine (the PC's, seen from the laptop), not this one."""
        import urllib.parse

        return (urllib.parse.urlsplit(self.url).hostname or "") not in ("127.0.0.1", "localhost", "::1")

    def warm(self, tries: int = 24, wait: float = 5.0) -> bool:
        """Say one short word to the server (waiting for it to come up): its model, the voice to sound like and the
        GPU are ready before the first real sentence, which would otherwise pay for all that."""
        import json
        import urllib.request

        body = json.dumps({"text": "Hi."}).encode()
        for i in range(tries):
            try:
                req = urllib.request.Request(self.url + "/say", data=body, method="POST", headers=self.headers)
                with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - our own local service
                    r.read()
                log.info("expressive voice warmed up")
                self.ok_at = time.time()
                return True
            except Exception as e:  # noqa: BLE001 - not up yet
                if i == tries - 1:
                    log.info("expressive voice not warmed up", extra={"error": str(e)[:120]})
                time.sleep(wait)
        return False

    def say(self, text: str, timeout: float | None = None, force: bool = False) -> bytes | None:
        import json
        import urllib.request

        if time.time() < self._down_until and not force:
            return None
        body = json.dumps({"text": text}).encode()
        req = urllib.request.Request(self.url + "/say", data=body, method="POST", headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:  # noqa: S310 - our own service
                wav = r.read()
            self.ok_at = time.time()
            self._down_until = 0.0
            return wav
        except Exception as e:  # noqa: BLE001 - down or failed: silent for a short while, then try again
            self._down_until = time.time() + 15
            log.warning("ari's voice unavailable, showing text", extra={"error": str(e)[:200]})
            return None
