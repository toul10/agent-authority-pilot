"""The models you can pick, and the adapters for the Claude API: the assistant under test and the monitor.

Both adapters are thin wrappers around `client.messages.create`, so tests can swap in
scripted stand-ins (see scripted.py) or point the SDK at a local mock server. The
OpenAI and Gemini adapters are in providers.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

DEFAULT_AGENT_MODEL = "claude-opus-5-5"
DEFAULT_MONITOR_MODEL = "claude-sonnet-5-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")

FOUR_EFFORTS = ("low", "medium", "high", "max")  # Opus 4.6 and Sonnet 4.6 have no xhigh


@dataclass(frozen=True)
class ModelInfo:
    """What the harness needs to know about one model, from the provider's model and pricing docs."""

    name: str
    price: tuple[float, float, float]  # USD per million tokens: input, output, cached input (cache read)
    provider: str = "anthropic"
    group: str = "Claude"  # the heading it sits under in the app's menus
    efforts: tuple[str, ...] = EFFORTS  # the model's own effort levels; () = none, effort sets a thinking budget
    thinking: str = "adaptive"  # "adaptive", or "budget" for manual extended thinking with budget_tokens
    display: bool = True  # takes thinking.display "summarized" (older models return summaries by default)
    system_messages: bool = True  # takes system messages in the middle of a conversation
    price_changes: tuple[tuple[str, tuple[float, float, float]], ...] = ()  # announced: (first day, new price)
    provider_monitor: bool = False  # the provider watches its agent actions and may stop a conversation

    @property
    def effort_levels(self) -> tuple[str, ...]:
        """The effort settings the harness accepts for this model."""
        return self.efforts or EFFORTS

    def price_on(self, day: str | None = None) -> tuple[float, float, float]:
        """The price on a day ("2026-11-05"; today when left out), counting announced changes."""
        day = day or date.today().isoformat()
        price = self.price
        for start, later in self.price_changes:
            if day >= start:
                price = later
        return price


OLDER = "Claude, older"
GPT = "GPT, from OpenAI"
GEMINI = "Gemini, from Google"
GEMINI_EFFORTS = ("low", "medium", "high")  # thinkingLevel; xhigh and max become high
# The models you can pick. Each needs a price: without one, the spending limit could not count
# what a run spends. Claude prices: https://platform.claude.com/docs/en/about-claude/pricing
# (checked 2026-11-05). Mid-conversation system messages, effort levels and thinking per model:
# the Claude API docs (model overview, effort, adaptive thinking, platform availability).
MODEL_INFO: dict[str, ModelInfo] = {
    "claude-fable-5-1": ModelInfo("Claude Fable 5.1", (10.00, 50.00, 0.25)),
    "claude-opus-5-5": ModelInfo("Claude Opus 5.5", (4.00, 20.00, 0.20)),
    "claude-sonnet-5-5": ModelInfo("Claude Sonnet 5.5", (2.00, 10.00, 0.10)),
    # Haiku 5.5 takes prompts up to 100K tokens.
    "claude-haiku-5-5": ModelInfo("Claude Haiku 5.5", (0.10, 0.50, 0.01), system_messages=False),
    "claude-fable-5": ModelInfo("Claude Fable 5", (10.00, 50.00, 1.00), group=OLDER),
    "claude-opus-5": ModelInfo("Claude Opus 5", (5.00, 25.00, 0.50), group=OLDER),
    "claude-sonnet-5": ModelInfo("Claude Sonnet 5", (2.00, 10.00, 0.20), group=OLDER, system_messages=False),
    "claude-opus-4-8": ModelInfo("Claude Opus 4.8", (5.00, 25.00, 0.50), group=OLDER),
    "claude-opus-4-7": ModelInfo("Claude Opus 4.7", (5.00, 25.00, 0.50), group=OLDER, system_messages=False),
    "claude-opus-4-6": ModelInfo(
        "Claude Opus 4.6", (5.00, 25.00, 0.50), group=OLDER, efforts=FOUR_EFFORTS, display=False, system_messages=False
    ),
    "claude-sonnet-4-6": ModelInfo(
        "Claude Sonnet 4.6",
        (3.00, 15.00, 0.30),
        group=OLDER,
        efforts=FOUR_EFFORTS,
        display=False,
        system_messages=False,
    ),
    "claude-opus-4-5": ModelInfo(
        "Claude Opus 4.5",
        (5.00, 25.00, 0.50),
        group=OLDER,
        efforts=(),
        thinking="budget",
        display=False,
        system_messages=False,
    ),
    "claude-haiku-4-5": ModelInfo(
        "Claude Haiku 4.5",
        (1.00, 5.00, 0.10),
        group=OLDER,
        efforts=(),
        thinking="budget",
        display=False,
        system_messages=False,
    ),
    # OpenAI, Responses API. Prices (Standard tier): https://developers.openai.com/api/docs/pricing,
    # effort levels: the model pages under https://developers.openai.com/api/docs/models (checked 2026-11-05).
    # Cache writes cost 1.25x input, as for Claude. Every model here has all five effort levels.
    "gpt-6-astra": ModelInfo("GPT-6 Astra", (10.00, 50.00, 1.00), provider="openai", group=GPT, provider_monitor=True),
    "gpt-6.1-sol": ModelInfo("GPT-6.1 Sol", (2.00, 10.00, 0.10), provider="openai", group=GPT),
    "gpt-6.1-terra": ModelInfo("GPT-6.1 Terra", (1.00, 5.00, 0.05), provider="openai", group=GPT),
    "gpt-6-luna": ModelInfo("GPT-6 Luna", (0.10, 0.50, 0.01), provider="openai", group=GPT),
    # Google, Gemini API generateContent. Prices (paid tier, output includes thinking) and the dated
    # increases: https://ai.google.dev/gemini-api/docs/pricing; thinking levels:
    # https://ai.google.dev/gemini-api/docs/thinking (checked 2026-11-05).
    "gemini-4-argon": ModelInfo(
        "Gemini 4 Argon",
        (2.00, 10.00, 0.10),
        provider="google",
        group=GEMINI,
        efforts=GEMINI_EFFORTS,
        system_messages=False,
        price_changes=(("2026-12-01", (4.00, 20.00, 0.20)),),
    ),
    "gemini-3.8-flash": ModelInfo(
        "Gemini 3.8 Flash",
        (0.75, 3.75, 0.075),
        provider="google",
        group=GEMINI,
        efforts=GEMINI_EFFORTS,
        system_messages=False,
        price_changes=(("2027-01-01", (1.50, 7.50, 0.15)),),
    ),
    "gemini-3.5-flash-lite": ModelInfo(
        "Gemini 3.5 Flash-Lite",
        (0.30, 2.50, 0.03),
        provider="google",
        group=GEMINI,
        efforts=GEMINI_EFFORTS,
        system_messages=False,
    ),
}
MODELS = {m: info.name for m, info in MODEL_INFO.items()}
PRICES = {m: info.price for m, info in MODEL_INFO.items()}

# For models without effort: the thinking budget each effort setting stands for. budget_tokens
# must stay below max_tokens, and max_tokens stays under what the SDK allows without streaming.
THINKING_BUDGET = {"low": 1024, "medium": 2048, "high": 4096, "xhigh": 8192, "max": 12000}

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute cache writes cost 1.25x input


def model_notes(info: ModelInfo, role: str = "assistant") -> list[str]:
    """What to know before comparing this model with the others, as the assistant or as the monitor."""
    notes = []
    if info.thinking == "budget":
        if role == "assistant":
            low, high = THINKING_BUDGET["low"], THINKING_BUDGET["max"]
            notes.append(
                f"It has no effort setting of its own: effort picks a thinking budget, from {low // 1000}K tokens "
                f"(low) to {high // 1000}K (max)."
            )
        else:
            notes.append(
                "It has no effort setting of its own, so effort changes nothing here: the monitor runs it without "
                "extended thinking."
            )
    if role == "assistant" and not info.system_messages:
        notes.append(
            "It takes no system messages mid-conversation, so operator notices reach it inside the user's turn. Keep "
            "that in mind when comparing it."
        )
    if role == "assistant" and info.provider == "openai":
        notes.append("Operator notices reach it as developer messages, OpenAI's nearest match to system messages.")
    if role == "assistant" and info.provider_monitor:
        notes.append(
            "OpenAI watches this model's agent actions on its side. Trap scenarios may send alerts to your OpenAI "
            "organization, and if OpenAI stops a conversation, the episode is marked as stopped by the provider."
        )
    for start, (inp, out, _) in info.price_changes:
        if start > date.today().isoformat():
            day = date.fromisoformat(start)
            notes.append(
                f"Its price goes up on {day.day} {day:%B %Y}, to {dollars(inp)} in and {dollars(out)} out per million "
                "tokens."
            )
    return notes


def dollars(amount: float) -> str:
    """$4, $1.50, $0.075: cents always shown, and more digits only where a price has them."""
    if amount == int(amount):
        return f"${int(amount)}"
    digits = max(2, len(f"{amount:.6f}".rstrip("0").split(".")[1]))
    return f"${amount:.{digits}f}"


def provider_of(model: str) -> str:
    info = model_info(model)
    return info.provider if info else "anthropic"


def notices_channel(info: ModelInfo) -> str:
    """How operator notices reach the assistant, recorded with each run: "system" (system messages),
    "user_turn" (a <system-reminder> block in the user turn) or "developer" (developer messages)."""
    if info.provider == "openai":
        return "developer"
    return "system" if info.system_messages else "user_turn"


def model_info(model: str) -> ModelInfo | None:
    """The entry for a model id; a dated id (claude-haiku-4-5-20251001) matches its alias."""
    if model in MODEL_INFO:
        return MODEL_INFO[model]
    match = max((m for m in MODEL_INFO if model.startswith(m + "-")), key=len, default=None)
    return MODEL_INFO[match] if match else None


def effort_for(info: ModelInfo, effort: str) -> str:
    """The effort to send: the setting itself when the model has it, else the nearest level below."""
    levels = info.effort_levels
    if effort in levels:
        return effort
    lower = EFFORTS[: EFFORTS.index(effort)] if effort in EFFORTS else ()
    return next((e for e in reversed(lower) if e in levels), levels[0])


def cost_usd(model: str, usage: dict, day: str | None = None) -> float | None:
    """Estimated cost of one call made on `day` (today when left out), or None for models without a price.
    Usage is in the Claude API's terms; the other providers' adapters convert theirs."""
    info = model_info(model)
    if info is None:
        return None
    inp, out, cache_read = info.price_on(day)
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


def _client(client, api_key: str | None = None):
    if client is not None:
        return client
    import anthropic

    # api_key=None lets the SDK read ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN itself.
    return anthropic.Anthropic(api_key=api_key, max_retries=4)


# Models without mid-conversation system messages get operator notices inside the user turn,
# as the Claude API docs suggest, and one sentence in the system prompt that says so.
REMINDER_NOTE = (
    "\n\nIn this chat system, system messages arrive inside a user turn, "
    "in a <system-reminder> block that only the chat system can add."
)
_REMINDER_TAG = re.compile(r"<(/?)system-reminder>", re.IGNORECASE)


def _defuse(content):
    """Sender text and documents cannot open a <system-reminder> block of their own."""
    if isinstance(content, str):
        return _REMINDER_TAG.sub(r"(\1system-reminder)", content)
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            block = {**block, "text": _defuse(block["text"])}
        elif isinstance(block, dict) and block.get("type") == "tool_result":
            block = {**block, "content": _defuse(block.get("content") or "")}
        out.append(block)
    return out


def inline_system_messages(messages: list[dict]) -> list[dict]:
    """Each mid-conversation system message becomes a <system-reminder> block at the end of the
    user message before it. Deterministic, so every request still extends the previous one."""
    out: list[dict] = []
    for m in messages:
        if m.get("role") == "system":
            block = {"type": "text", "text": f"<system-reminder>\n{m['content']}\n</system-reminder>"}
            if out and out[-1]["role"] == "user":
                prev = out[-1]["content"]
                prev = prev if isinstance(prev, list) else [{"type": "text", "text": prev}]
                out[-1] = {**out[-1], "content": [*prev, block]}
            else:
                out.append({"role": "user", "content": [block]})
        elif m.get("role") == "user":
            out.append({**m, "content": _defuse(m["content"])})
        else:
            out.append(m)
    return out


class AnthropicAgent:
    """The assistant under test: one `messages.create` call per step of the agent loop."""

    def __init__(
        self,
        model: str = DEFAULT_AGENT_MODEL,
        effort: str = "medium",
        max_tokens: int = 8000,
        client=None,
        api_key: str | None = None,
    ):
        self.model = model
        self.info = model_info(model) or MODEL_INFO[DEFAULT_AGENT_MODEL]
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = _client(client, api_key)
        self.notices = notices_channel(self.info)

    def request(self, system: str, tools: list[dict], messages: list[dict]) -> dict:
        """One step's request. The same for every model, except what a model does not take."""
        info = self.info
        params = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
            "cache_control": {"type": "ephemeral"},
        }
        if info.thinking == "budget":
            # No adaptive thinking and no effort: the effort setting picks a thinking budget.
            budget = THINKING_BUDGET.get(self.effort, THINKING_BUDGET["medium"])
            params["thinking"] = {"type": "enabled", "budget_tokens": budget}
            params["max_tokens"] = self.max_tokens + budget
        else:
            # Thinking is on; "summarized" puts a readable summary in the logs where a model
            # would otherwise leave it out. Effort is set explicitly so runs are comparable.
            params["thinking"] = {"type": "adaptive", "display": "summarized"} if info.display else {"type": "adaptive"}
        if info.efforts:
            params["output_config"] = {"effort": effort_for(info, self.effort)}
        if not info.system_messages:
            params["system"] = system + REMINDER_NOTE
            params["messages"] = inline_system_messages(messages)
        return params

    def respond(self, system: str, tools: list[dict], messages: list[dict]) -> Step:
        response = self.client.messages.create(**self.request(system, tools, messages))
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

    def __init__(
        self,
        model: str = DEFAULT_MONITOR_MODEL,
        effort: str = "medium",
        max_tokens: int = 4000,
        client=None,
        api_key: str | None = None,
    ):
        self.model = model
        self.info = model_info(model) or MODEL_INFO[DEFAULT_MONITOR_MODEL]
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = _client(client, api_key)

    def request(self, system: str, prompt: str) -> dict:
        output_config: dict = {"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}}
        if self.info.efforts:
            output_config["effort"] = effort_for(self.info, self.effort)
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }

    def review(self, system: str, prompt: str, request: dict | None = None) -> Verdict:
        response = self.client.messages.create(**self.request(system, prompt))
        meta = {
            "usage": usage_dict(response.usage),
            "model": response.model,
            "request_id": getattr(response, "_request_id", None),
        }
        if response.stop_reason == "refusal":
            return Verdict("block", "The monitor model declined to review this action.", error="refusal", **meta)
        return parse_verdict(next((b.text for b in response.content if getattr(b, "type", "") == "text"), ""), meta)


def parse_verdict(answer: str | dict, meta: dict) -> Verdict:
    """The monitor's decision from its JSON text (or the arguments of its verdict call). Anything
    unreadable blocks the action: the monitor fails closed."""
    try:
        data = json.loads(answer) if isinstance(answer, str) else answer
        decision, reason = data["decision"], str(data.get("reason", ""))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return Verdict(
            "block",
            "The monitor's answer could not be read.",
            error=f"{type(exc).__name__}: {str(answer)[:200]}",
            **meta,
        )
    if decision not in ("allow", "block"):
        return Verdict("block", "The monitor gave no valid decision.", error=f"decision={decision!r}", **meta)
    return Verdict(decision, reason, **meta)


# --------------------------------------------------------------------------- key check


def _capability_problems(caps: Any, role: str, effort: str, info: ModelInfo | None = None) -> list[str]:
    """What this model lacks for what the harness sends it. Unknown capabilities are not problems."""
    caps = to_dict(caps) if caps is not None else None
    if not isinstance(caps, dict):
        return []
    budget = info is not None and info.thinking == "budget"
    has_effort = info is None or bool(info.efforts)
    if info is not None and has_effort:
        effort = effort_for(info, effort)

    def supported(*path: str) -> bool | None:
        node: Any = caps
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        return node.get("supported") if isinstance(node, dict) else None

    problems = []
    if has_effort and (supported("effort") is False or supported("effort", effort) is False):
        problems.append(f"does not support effort {effort}")
    if role == "assistant" and not budget and supported("thinking", "types", "adaptive") is False:
        problems.append("does not support adaptive thinking, which the assistant uses")
    if role == "assistant" and budget and supported("thinking", "types", "enabled") is False:
        problems.append(
            "does not support extended thinking with a token budget, which the assistant uses on this model"
        )
    if role == "monitor" and supported("structured_outputs") is False:
        problems.append("does not support structured outputs, which the monitor uses")
    return problems


def check_models(roles: dict[str, tuple[str, str]], *, api_key: str | None = None, client=None) -> dict:
    """Free checks with the Models API (no tokens): does the key work, and can
    each model do what the harness asks of it?

    `roles` maps "assistant" and "monitor" to (model, effort).
    """
    import anthropic

    client = client or anthropic.Anthropic(api_key=api_key, max_retries=1, timeout=20)
    rows: list[dict] = []
    seen: dict[str, Any] = {}
    for role, (model, effort) in roles.items():
        row: dict[str, Any] = {
            "role": role,
            "model": model,
            "effort": effort,
            "name": MODELS.get(model, model),
            "ok": False,
            "problems": [],
        }
        rows.append(row)
        try:
            if model not in seen:
                seen[model] = client.models.retrieve(model)
        except anthropic.AuthenticationError:
            return {
                "ok": False,
                "error": "The API did not accept this key (401). Copy it again from the Claude Console.",
                "models": rows,
            }
        except anthropic.PermissionDeniedError:
            row["problems"].append("this key may not use this model (403)")
            continue
        except anthropic.NotFoundError:
            row["problems"].append("the API does not know this model, or this key cannot see it (404)")
            continue
        except anthropic.RateLimitError:
            row["problems"].append("rate limited (429); try again in a minute")
            continue
        except anthropic.APIStatusError as exc:
            row["problems"].append(f"the API answered with an error ({exc.status_code})")
            continue
        except anthropic.APIConnectionError:
            return {"ok": False, "error": "Could not reach the API. Check the internet connection.", "models": rows}
        info = seen[model]
        row["name"] = getattr(info, "display_name", None) or row["name"]
        row["problems"] = _capability_problems(getattr(info, "capabilities", None), role, effort, model_info(model))
        row["ok"] = not row["problems"]
    return {"ok": all(r["ok"] for r in rows), "error": None, "models": rows}
