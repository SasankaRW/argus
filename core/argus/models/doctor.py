"""Check the model setup: python -m argus.models.doctor [--claude] [--config argus.yaml]

- Ollama answers at the configured address, and every Ollama tier's model is pulled
- each Ollama tier returns valid JSON for a tiny prompt (and how long it took, including loading)
- the claude CLI is installed; with --claude, one real call (tools off) to prove the login works
"""

from __future__ import annotations

import argparse
import sys

from pydantic import BaseModel

from ..config import ConfigError, load_config
from .providers import ClaudeProvider, ModelError, OllamaProvider
from .router import parse_json


class _Ping(BaseModel):
    ok: bool


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="argus-models", description="Check Ollama and Claude for Argus")
    p.add_argument("--config", help="path to argus.yaml")
    p.add_argument("--claude", action="store_true", help="also make one real Claude call (uses your plan)")
    p.add_argument("--ollama-url", help="override the Ollama address")
    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2

    problems = 0

    def line(ok: bool, text: str) -> None:
        nonlocal problems
        problems += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {text}")

    url = args.ollama_url or cfg.ollama.url
    ollama_tiers = {k: t for k, t in cfg.models.tiers.items() if t.provider == "ollama"}
    print(f"Ollama at {url}")
    if ollama_tiers:
        try:
            have = OllamaProvider(url, "", timeout=10).list_models()
            line(True, f"reachable, {len(have)} model(s) pulled")
            for tier, t in ollama_tiers.items():
                present = t.model in have or f"{t.model}:latest" in have
                line(present, f"{tier} {t.model}" + ("" if present else f"  ->  run: ollama pull {t.model}"))
                if not present:
                    continue
                prov = OllamaProvider(url, t.model, timeout=cfg.ollama.timeout_seconds,
                                      keep_alive=cfg.ollama.keep_alive)
                try:
                    r = prov.chat("Reply with JSON only.", [{"role": "user", "content": 'Say {"ok": true}'}],
                                  _Ping.model_json_schema())
                    _Ping.model_validate(parse_json(r.text))
                    line(True, f"{tier} answered valid JSON in {r.latency_ms / 1000:.1f} s")
                except (ModelError, ValueError) as e:
                    line(False, f"{tier} test call: {e}")
        except ModelError as e:
            line(False, f"{e}  ->  is Ollama running? (ollama serve)")
    else:
        print("  (no Ollama tiers configured)")

    claude_tiers = [k for k, t in cfg.models.tiers.items() if t.provider == "claude"]
    print("Claude")
    if claude_tiers:
        prov = ClaudeProvider(cfg.claude.command, cfg.claude.args, timeout=cfg.claude.timeout_seconds)
        found = prov.available()
        line(found, f"CLI {'found' if found else 'not found'}: {cfg.claude.command[0]}"
             + ("" if found else "  ->  install Claude Code and log in (claude)"))
        if found and args.claude:
            try:
                r = prov.chat("Reply with JSON only.", [{"role": "user", "content": 'Say {"ok": true}'}],
                              _Ping.model_json_schema())
                _Ping.model_validate(parse_json(r.text))
                line(True, f"real call with tools off answered in {r.latency_ms / 1000:.1f} s")
            except (ModelError, ValueError) as e:
                line(False, f"real call: {e}")
        elif found:
            print("  (add --claude to make one real call)")
        print(f"  daily cap: {cfg.claude.calls_per_day} calls")
    else:
        print("  (no Claude tier configured)")

    print("\nAll good." if not problems else f"\n{problems} problem(s) found.")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
