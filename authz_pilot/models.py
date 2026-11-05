"""Model adapters for the Claude API: the assistant under test and the monitor.

Both are thin wrappers around `client.messages.create`, so tests can swap in
scripted stand-ins (see scripted.py) or point the SDK at a local mock server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

DEFAULT_AGENT_MODEL = "claude-opus-5-5"
DEFAULT_MONITOR_MODEL = "claude-sonnet-5-5"

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

# USD per million tokens: (input, output, cache read). 5-minute cache writes cost 1.25x input.
# Check https://platform.claude.com/docs/en/about-claude/pricing before relying on these.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),  # prompts up to 100K tokens
}
CACHE_WRITE_MULTIPLIER = 1.25


def cost_usd(model: str, usage: dict) -> float | None:
    """Estimated cost of one call, or None for models without a price entry."""
    price = next((p for name, p in PRICES.items() if model.startswith(name)), None)
    if price is None:
        return None
    inp, out, cache_read = price
    total = (
        usage.get("input_tokens", 0) * inp
        + usage.get("cache_creation_input_tokens", 0) * inp * CACHE_WRITE_MULTIPLIER
        + usage.get("cache_read_input_tokens", 0) * cache_read
        + usage.get("output_tokens", 0) * out
    )
    return total / 1_000_000


def to_dict(obj: Any) -> Any:
    if obj is None or isinstance(obj, (dict, str, int, float, bool)):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(obj)


def usage_dict(usage: Any) -> dict:
    return {f: int(getattr(usage, f, 0) or 0) for f in USAGE_FIELDS}


def _loggable(block: dict) -> dict:
    """Drop opaque signatures from logs; keep everything a reader needs."""
    block = dict(block)
    if block.get("type") == "thinking" and "signature" in block:
        block["signature"] = f"<{len(block['signature'] or '')} chars>"
    if block.get("type") == "redacted_thinking" and "data" in block:
        block["data"] = f"<{len(block['data'] or '')} chars>"
    return block


@dataclass
class Step:
    """One assistant response."""

    content: list  # passed back to the API unchanged, as the next assistant message
    blocks: list[dict]  # the same content as plain dicts
    stop_reason: str
    stop_details: dict | None = None
    usage: dict = field(default_factory=dict)
    model: str = ""
    request_id: str | None = None

    @property
    def texts(self) -> list[str]:
        return [b.get("text", "") for b in self.blocks if b.get("type") == "text"]

    @property
    def tool_calls(self) -> list[dict]:
        return [b for b in self.blocks if b.get("type") == "tool_use"]

    def log(self) -> dict:
        return {
            "stop_reason": self.stop_reason,
            "stop_details": self.stop_details,
            "usage": self.usage,
            "model": self.model,
            "request_id": self.request_id,
            "content": [_loggable(b) for b in self.blocks],
        }


@dataclass
class Verdict:
    decision: str  # "allow" or "block"
    reason: str
    usage: dict = field(default_factory=dict)
    model: str = ""
    request_id: str | None = None
    error: str | None = None


def _client(client):
    if client is not None:
        return client
    import anthropic

    return anthropic.Anthropic(max_retries=4)


class AnthropicAgent:
    """The assistant under test: one `messages.create` call per step of the agent loop."""

    def __init__(self, model: str = DEFAULT_AGENT_MODEL, effort: str = "medium", max_tokens: int = 8000, client=None):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = _client(client)

    def respond(self, system: str, tools: list[dict], messages: list[dict]) -> Step:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            # Thinking is always on for Claude Opus 5.5; "summarized" puts a readable
            # summary in the logs. Effort is set explicitly so runs are comparable.
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
        )
        stop_details = getattr(response, "stop_details", None)
        return Step(
            content=list(response.content),
            blocks=[to_dict(b) for b in response.content],
            stop_reason=response.stop_reason,
            stop_details=to_dict(stop_details) if stop_details else None,
            usage=usage_dict(response.usage),
            model=response.model,
            request_id=getattr(response, "_request_id", None),
        )


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["allow", "block"]},
        "reason": {"type": "string"},
    },
    "required": ["decision", "reason"],
    "additionalProperties": False,
}


class AnthropicMonitor:
    """Reviews one proposed action and returns allow/block as structured JSON."""

    def __init__(self, model: str = DEFAULT_MONITOR_MODEL, effort: str = "medium", max_tokens: int = 4000, client=None):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = _client(client)

    def review(self, system: str, prompt: str, request: dict | None = None) -> Verdict:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
            },
        )
        meta = {
            "usage": usage_dict(response.usage),
            "model": response.model,
            "request_id": getattr(response, "_request_id", None),
        }
        if response.stop_reason == "refusal":
            return Verdict("block", "The monitor model declined to review this action.", error="refusal", **meta)
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
            decision, reason = data["decision"], str(data.get("reason", ""))
        except (ValueError, KeyError, TypeError) as exc:
            return Verdict("block", "The monitor's answer could not be read.", error=f"{type(exc).__name__}: {text[:200]}", **meta)
        if decision not in ("allow", "block"):
            return Verdict("block", "The monitor gave no valid decision.", error=f"decision={decision!r}", **meta)
        return Verdict(decision, reason, **meta)
