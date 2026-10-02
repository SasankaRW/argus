"""Model calls for workers: providers (Ollama, Claude CLI) and the tier router with checks and escalation."""

from .providers import ClaudeProvider, ModelError, ModelTimeout, ModelUnavailable, OllamaProvider, Reply
from .router import Answer, EscalationExhausted, Router, parse_json

__all__ = [
    "Answer",
    "ClaudeProvider",
    "EscalationExhausted",
    "ModelError",
    "ModelTimeout",
    "ModelUnavailable",
    "OllamaProvider",
    "Reply",
    "Router",
    "build_providers",
    "parse_json",
]


def build_providers(cfg: dict, *, ollama_url: str | None = None) -> dict:
    """Providers for every tier in the worker config argusd hands out at registration.

    `ollama_url` overrides the server's address (the worker on the PC reaches Ollama at 127.0.0.1 while the
    laptop knows it by its LAN address).
    """
    out = {}
    o, c = cfg.get("ollama", {}), cfg.get("claude", {})
    for tier, t in cfg.get("tiers", {}).items():
        if t["provider"] == "ollama":
            out[tier] = OllamaProvider(ollama_url or o.get("url", "http://127.0.0.1:11434"), t["model"],
                                       timeout=o.get("timeout_seconds", 120), keep_alive=o.get("keep_alive", "10m"),
                                       num_ctx=o.get("num_ctx", 8192))
        elif t["provider"] == "claude":
            p = ClaudeProvider(c.get("command", ["claude"]), c.get("args", []),
                               timeout=c.get("timeout_seconds", 300), model=t.get("model"))
            if p.available():
                out[tier] = p
    return out
