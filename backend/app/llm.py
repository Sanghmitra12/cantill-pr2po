"""Optional LLM access (Anthropic Messages API over httpx).  Everything works without it: callers fall back to rules."""
from __future__ import annotations

import json
import os
import re

import httpx

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def complete(system: str, user: str, max_tokens: int = 6000, timeout: float = 90.0) -> str:
    r = httpx.post(
        os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]},
        timeout=timeout,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []))


def extract_json(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(raw)
