"""Follow one request to Ari in the terminal: python -m argus.trace [last | <job id> | <turn id>] (dev.ps1 trace).

Without an argument: the latest requests, one line each, then what isn't working. With one: that request in full
(heard, each step and model call, the tools, what Ari said, the result and why).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from .config import parse_env_file
from .timeline import line


def get(url: str, token: str | None, path: str) -> object:
    req = urllib.request.Request(url.rstrip("/") + path, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - our own argusd
        return json.loads(r.read() or b"null")


def show_trace(t: dict) -> list[str]:
    out = [f"\"{t.get('text') or t.get('workflow') or ''}\"  →  {t.get('result')}"
           + (f": {t['why']}" if t.get("why") else "")]
    h = t.get("heard") or {}
    if h:
        out.append(f"  heard     English {float(h.get('accepted') or 0):.0%}"
                   + (f", most like {h.get('top')} {float(h.get('top_p') or 0):.0%}" if h.get("top") not in (None, "en")
                      else "") + ("  (\"Hey Ari\" on a second listen)" if h.get("second_listen") else ""))
    for s in t.get("steps") or []:
        ms = f"{s['ms'] / 1000:6.1f}s" if s.get("ms") is not None else "       "
        out.append(f"  {'✓' if s.get('ok', True) else '✗'} {s.get('kind', 'step'):<6} {str(s.get('what'))[:50]:<50}"
                   f" {ms}  {s.get('why') or ''}".rstrip())
    for x in t.get("tools") or []:
        out.append(f"  {'✓' if x.get('ok') else '✗'} tool   {x.get('tool')}  {'' if x.get('ok') else x.get('why')}")
    if t.get("reply"):
        out.append(f"  said      \"{t['reply']}\"")
    if t.get("answer_ms") or t.get("ms"):
        out.append(f"  took      {(t.get('answer_ms') or t.get('ms')) / 1000:.1f} s")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="argus-trace", description="Follow a request to Ari.")
    ap.add_argument("ref", nargs="?", default="", help="last, a job id or a turn id (none: the latest requests)")
    env = parse_env_file(Path(".env")) if Path(".env").exists() else {}
    ap.add_argument("--url", default=os.environ.get("ARGUS_URL") or env.get("ARGUS_URL") or "http://127.0.0.1:8600")
    a = ap.parse_args(argv)
    token = os.environ.get("ARGUS_WORKER_TOKEN") or env.get("ARGUS_WORKER_TOKEN")
    try:
        if a.ref:
            t = get(a.url, token, f"/ari/trace/{urllib.parse.quote(a.ref)}")
            print("\n".join(show_trace(t if isinstance(t, dict) else {})))
            return 0
        rows = (get(a.url, token, "/ari/timeline?limit=25") or {}).get("requests") or []
        for r in reversed(rows):
            print(line(r) + (f"   [{r['job_id']}]" if r.get("job_id") else ""))
        p = get(a.url, token, "/problems?hours=24") or {}
        print(f"\nlast 24 h: {p.get('headline')}")
        for g in (p.get("problems") or [])[:8]:
            tail = f"  — {g['hint']}" if g.get("hint") else ""
            print(f"  ×{g['count']:<3} {g['area']:<24} {g['what'][:70]}{tail}")
    except OSError as e:
        print(f"argusd isn't answering at {a.url}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
