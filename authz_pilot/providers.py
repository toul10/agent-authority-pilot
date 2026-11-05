"""The assistant and the monitor on the OpenAI and Gemini APIs, over plain HTTPS (no SDKs).

The runner keeps the conversation in the Claude API's shape: user turns with text and
tool_result blocks, system messages for operator notices, and assistant turns holding
whatever the provider returned. Each adapter here turns that history into its API's
request, sends back its own earlier output exactly as it arrived (OpenAI's encrypted
reasoning items, Gemini's thought signatures), and turns each answer into the blocks the
runner reads: text, thinking and tool_use.

Both adapters keep no state between calls, so one instance can serve every episode of a
run: OpenAI requests use store=false and carry the whole conversation.

Request and response shapes, checked 2026-11-05:
- OpenAI Responses API: https://developers.openai.com/api/reference/resources/responses/methods/create
  and https://developers.openai.com/api/docs/guides/function-calling
- Gemini generateContent: https://ai.google.dev/api/generate-content
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
import uuid

from .models import (
    REMINDER_NOTE,
    VERDICT_SCHEMA,
    AnthropicAgent,
    AnthropicMonitor,
    Step,
    Verdict,
    check_models as check_claude_models,
    effort_for,
    inline_system_messages,
    model_info,
    parse_verdict,
    provider_of,
)
from .settings import PROVIDERS

BASES = {"openai": "https://api.openai.com/v1", "google": "https://generativelanguage.googleapis.com/v1beta"}
BASE_ENV = {"openai": "AUTHZ_PILOT_OPENAI_BASE_URL", "google": "AUTHZ_PILOT_GEMINI_BASE_URL"}  # for the mock APIs in tests


def base_url(provider: str) -> str:
    return (os.environ.get(BASE_ENV[provider]) or BASES[provider]).rstrip("/")


# --------------------------------------------------------------------------- HTTP


class ProviderError(Exception):
    """A call that failed for good, after any retries."""

    def __init__(self, provider: str, status: int | None, message: str, *, code: str | None = None, param: str | None = None, request_id: str | None = None):
        self.provider, self.status, self.code, self.param, self.request_id = provider, status, code, param, request_id
        name = PROVIDERS[provider].name
        super().__init__(f"{name} API" + (f" {status}" if status else "") + (f" ({code})" if code else "") + f": {message}")


# 429s that waiting does not fix: the credit, the spend limit or the quota is used up.
FINAL_429 = {"insufficient_quota", "credit_balance_exhausted", "organization_usage_limit_exceeded"}
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _error_fields(provider: str, payload) -> tuple[str, str | None, str | None]:
    """(message, code, param) from an error body: OpenAI's {"error": {...}} or google.rpc's."""
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return "no details", None, None
    message = str(err.get("message") or "no details")[:500]
    if provider == "openai":
        return message, err.get("code") or err.get("type"), err.get("param")
    reason = next((d.get("reason") for d in err.get("details") or [] if isinstance(d, dict) and d.get("reason")), None)
    return message, reason or err.get("status"), None


def _retryable(status: int, code: str | None) -> bool:
    if status == 429:
        return code not in FINAL_429 and not (code or "").endswith("_spend_limit_exceeded")
    return status in RETRY_STATUS


def _pause(attempt: int, retry_after: str | None) -> float:
    try:
        asked = float(retry_after) if retry_after else 0.0
    except ValueError:
        asked = 0.0
    return max(asked, min(60.0, 2.0**attempt)) + random.uniform(0, 1)


def call_json(provider: str, method: str, url: str, headers: dict, body: dict | None = None, *, timeout: float = 600, retries: int = 4, sleep=time.sleep) -> tuple[dict, dict]:
    """One JSON request, retried on rate limits, server errors and dropped connections, never on
    4xx answers that would only repeat. Returns (body, headers with lower-case names)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", "Accept": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read() or b"{}"), {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = {"error": {"message": raw[:300].decode("utf-8", "replace")}}
            message, code, param = _error_fields(provider, payload)
            got = {k.lower(): v for k, v in (exc.headers or {}).items()}
            if attempt < retries and _retryable(exc.code, code):
                sleep(_pause(attempt, got.get("retry-after")))
                continue
            raise ProviderError(provider, exc.code, message, code=code, param=param, request_id=got.get("x-request-id")) from None
        except (urllib.error.URLError, OSError) as exc:  # no answer at all
            if attempt < retries:
                sleep(_pause(attempt, None))
                continue
            raise ProviderError(provider, None, f"could not reach the API ({getattr(exc, 'reason', exc)})") from None
    raise AssertionError("unreachable")


def provider_stop(provider: str, exc: ProviderError, model: str) -> Step:
    """The provider's own monitor ended the conversation. Kept apart from refusals and errors:
    it is a check outside the set-up being tested."""
    details = {"type": "provider_stop", "provider": provider, "code": exc.code, "message": str(exc)}
    return Step(content=[], blocks=[], stop_reason="provider_stop", stop_details=details, usage={}, model=model, request_id=exc.request_id)


def _text_blocks(content) -> list[dict]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


# --------------------------------------------------------------------------- OpenAI

OPENAI_STOP = "misalignment_policy_violation"  # OpenAI's own monitor stopped the agent (HTTP 403)


def openai_tools(tools: list[dict]) -> list[dict]:
    # strict is off, as for Claude: the harness checks every tool input itself.
    return [{"type": "function", "name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"], "strict": False} for t in tools]


def openai_input(messages: list[dict]) -> list[dict]:
    """Responses API input for the runner's history: tool results as function_call_output items,
    operator notices as developer messages, the model's earlier output items unchanged."""
    items: list[dict] = []
    for m in messages:
        role, content = m["role"], m["content"]
        if role == "assistant":
            items.extend(content)  # every output item, in order, as it came back
        elif role == "system":
            items.append({"role": "developer", "content": content})
        else:
            blocks = _text_blocks(content)
            items += [{"type": "function_call_output", "call_id": b["tool_use_id"], "output": str(b.get("content", ""))} for b in blocks if b.get("type") == "tool_result"]
            texts = [b["text"] for b in blocks if b.get("type") == "text"]
            if texts:
                items.append({"role": "user", "content": "\n\n".join(texts)})
    return items


def openai_usage(usage: dict | None) -> dict:
    """In the Claude API's terms: input_tokens excludes cached reads and cache writes."""
    u = usage or {}
    details = u.get("input_tokens_details") or {}
    cached, writes = int(details.get("cached_tokens") or 0), int(details.get("cache_write_tokens") or 0)
    return {
        "input_tokens": max(0, int(u.get("input_tokens") or 0) - cached - writes),
        "output_tokens": int(u.get("output_tokens") or 0),  # includes the reasoning tokens
        "cache_creation_input_tokens": writes,
        "cache_read_input_tokens": cached,
    }


def openai_step(data: dict, model: str, request_id: str | None) -> Step:
    items = data.get("output") or []
    blocks: list[dict] = []
    refusal = None
    for item in items:
        kind = item.get("type")
        if kind == "reasoning":
            text = "\n\n".join(s["text"] for s in item.get("summary") or [] if isinstance(s, dict) and s.get("text"))
            if text:
                blocks.append({"type": "thinking", "thinking": text})
        elif kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text" and part.get("text"):
                    blocks.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "refusal":
                    refusal = part.get("refusal") or ""
        elif kind == "function_call":
            raw = item.get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except ValueError:
                args = raw  # the runner reports the call as invalid
            blocks.append({"type": "tool_use", "id": item.get("call_id"), "name": item.get("name", ""), "input": args})
    details = None
    if data.get("status") == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason") or "incomplete"
        stop = {"content_filter": "refusal", "max_output_tokens": "max_tokens"}.get(reason, reason)
        details = {"type": "incomplete", "reason": reason}
    elif refusal is not None:
        stop, details = "refusal", {"type": "refusal", "explanation": refusal}
    elif any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"
    else:
        stop = "end_turn"
    return Step(content=items, blocks=blocks, stop_reason=stop, stop_details=details, usage=openai_usage(data.get("usage")), model=data.get("model") or model, request_id=request_id)


class _OpenAI:
    def __init__(self, model: str, effort: str, max_tokens: int, api_key: str | None, call=call_json):
        self.model, self.effort, self.max_tokens = model, effort, max_tokens
        self.info = model_info(model)
        self.key = api_key
        self.call = call

    def _post(self, body: dict) -> tuple[dict, dict]:
        data, headers = self.call("openai", "POST", base_url("openai") + "/responses", {"Authorization": f"Bearer {self.key}"}, body, timeout=600)
        err = data.get("error") if isinstance(data.get("error"), dict) else None
        if err or data.get("status") == "failed":
            err = err or {}
            raise ProviderError("openai", None, str(err.get("message") or "the response failed"), code=err.get("code"), request_id=headers.get("x-request-id"))
        return data, headers


class OpenAIAgent(_OpenAI):
    """The assistant under test on the Responses API."""

    def __init__(self, model: str, effort: str = "medium", max_tokens: int = 32000, api_key: str | None = None, call=call_json):
        super().__init__(model, effort, max_tokens, api_key, call)
        self.notices = "developer"
        self.summaries = True  # reasoning summaries for the logs, unless the organization may not have them

    def request(self, system: str, tools: list[dict], messages: list[dict]) -> dict:
        reasoning = {"effort": effort_for(self.info, self.effort)}
        if self.summaries:
            reasoning["summary"] = "auto"
        return {"model": self.model, "instructions": system, "input": openai_input(messages), "tools": openai_tools(tools),
                "tool_choice": "auto", "reasoning": reasoning, "store": False, "max_output_tokens": self.max_tokens}

    def respond(self, system: str, tools: list[dict], messages: list[dict]) -> Step:
        try:
            try:
                data, headers = self._post(self.request(system, tools, messages))
            except ProviderError as exc:
                # Summaries can need a verified organization. Without one, carry on without them.
                if not (self.summaries and exc.status == 400 and (exc.param == "reasoning.summary" or "reasoning.summary" in str(exc))):
                    raise
                self.summaries = False
                data, headers = self._post(self.request(system, tools, messages))
        except ProviderError as exc:
            if exc.code == OPENAI_STOP:
                return provider_stop("openai", exc, self.model)
            raise
        return openai_step(data, self.model, headers.get("x-request-id"))


class OpenAIMonitor(_OpenAI):
    """Reviews one proposed action; the answer is JSON that must match the verdict schema."""

    def __init__(self, model: str, effort: str = "medium", max_tokens: int = 16000, api_key: str | None = None, call=call_json):
        super().__init__(model, effort, max_tokens, api_key, call)

    def request(self, system: str, prompt: str) -> dict:
        return {"model": self.model, "instructions": system, "input": [{"role": "user", "content": prompt}],
                "reasoning": {"effort": effort_for(self.info, self.effort)},
                "text": {"format": {"type": "json_schema", "name": "verdict", "schema": VERDICT_SCHEMA, "strict": True}},
                "store": False, "max_output_tokens": self.max_tokens}

    def review(self, system: str, prompt: str, request: dict | None = None) -> Verdict:
        try:
            data, headers = self._post(self.request(system, prompt))
        except ProviderError as exc:
            if exc.code != OPENAI_STOP:
                raise
            return Verdict("block", "OpenAI stopped the review, so the action was blocked.", model=self.model, request_id=exc.request_id, error=f"provider_stop: {exc}")
        step = openai_step(data, self.model, headers.get("x-request-id"))
        meta = {"usage": step.usage, "model": step.model, "request_id": step.request_id}
        if step.stop_reason == "refusal":
            return Verdict("block", "The monitor model declined to review this action.", error="refusal", **meta)
        if step.stop_reason != "end_turn":
            return Verdict("block", "The monitor's answer was cut off.", error=step.stop_reason, **meta)
        return parse_verdict("".join(step.texts), meta)


# --------------------------------------------------------------------------- Gemini

THINKING_LEVEL = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}
BLOCKED = {"SAFETY", "RECITATION", "SPII", "PROHIBITED_CONTENT", "BLOCKLIST", "IMAGE_SAFETY", "LANGUAGE"}
VERDICT_TOOL = "submit_verdict"


def gemini_tools(tools: list[dict]) -> list[dict]:
    declarations = []
    for t in tools:
        d = {"name": t["name"], "description": t.get("description", "")}
        if t["input_schema"].get("properties"):  # a tool without parameters declares none
            d["parametersJsonSchema"] = t["input_schema"]
        declarations.append(d)
    return [{"functionDeclarations": declarations}]


def gemini_contents(messages: list[dict]) -> list[dict]:
    """Gemini contents for the runner's history, with operator notices already inside the user
    turns: the model's turns exactly as they came back, tool results as functionResponse parts
    in call order, and one user turn where the history has two in a row."""
    contents: list[dict] = []
    calls: dict[str, dict] = {}
    for m in messages:
        if m["role"] == "assistant":
            for turn in m["content"]:
                calls.update(turn.get("calls") or {})
                if (turn.get("content") or {}).get("parts"):
                    contents.append(turn["content"])
            continue
        blocks = _text_blocks(m["content"])
        parts = []
        for b in blocks:
            if b.get("type") == "tool_result":
                call = calls.get(b["tool_use_id"], {})
                response = {"name": call.get("name", ""), "response": {"error" if b.get("is_error") else "result": str(b.get("content", ""))}}
                if call.get("api_id"):
                    response["id"] = call["api_id"]
                parts.append({"functionResponse": response})
        parts += [{"text": b["text"]} for b in blocks if b.get("type") == "text"]
        if not parts:
            continue
        if contents and contents[-1]["role"] == "user":
            contents[-1] = {"role": "user", "parts": contents[-1]["parts"] + parts}
        else:
            contents.append({"role": "user", "parts": parts})
    return contents


def gemini_usage(usage: dict | None) -> dict:
    """In the Claude API's terms: promptTokenCount includes cached tokens; thinking is billed as output."""
    u = usage or {}
    cached = int(u.get("cachedContentTokenCount") or 0)
    return {
        "input_tokens": max(0, int(u.get("promptTokenCount") or 0) - cached) + int(u.get("toolUsePromptTokenCount") or 0),
        "output_tokens": int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0),
        "cache_creation_input_tokens": 0,  # implicit caching has no write charge
        "cache_read_input_tokens": cached,
    }


def gemini_step(data: dict, model: str) -> Step:
    usage, rid, version = gemini_usage(data.get("usageMetadata")), data.get("responseId"), data.get("modelVersion") or model
    candidates = data.get("candidates") or []
    if not candidates:  # the prompt itself was blocked
        reason = (data.get("promptFeedback") or {}).get("blockReason") or "no answer"
        return Step(content=[], blocks=[], stop_reason="refusal", stop_details={"type": "blocked", "reason": reason}, usage=usage, model=version, request_id=rid)
    candidate = candidates[0]
    content = candidate.get("content") or {}
    parts = content.get("parts") or []
    blocks: list[dict] = []
    calls: dict[str, dict] = {}
    for p in parts:
        if "functionCall" in p:
            fc = p["functionCall"] or {}
            api_id = fc.get("id")
            tool_id = api_id or "call_" + uuid.uuid4().hex[:16]  # the API may leave the id out
            calls[tool_id] = {"name": fc.get("name", ""), "api_id": api_id}
            blocks.append({"type": "tool_use", "id": tool_id, "name": fc.get("name", ""), "input": fc.get("args") or {}})
        elif p.get("thought"):
            if p.get("text"):
                blocks.append({"type": "thinking", "thinking": p["text"]})
        elif p.get("text"):
            blocks.append({"type": "text", "text": p["text"]})
    finish = candidate.get("finishReason") or "STOP"
    details = None
    if finish == "STOP":
        stop = "tool_use" if calls else "end_turn"
    elif finish == "MAX_TOKENS":
        stop = "max_tokens"
    elif finish in BLOCKED:
        stop, details = "refusal", {"type": "blocked", "reason": finish}
    else:
        stop, details = finish.lower(), {"type": "finish_reason", "reason": finish}
    # The turn goes back unchanged, signatures and all; the ids let tool results name their call.
    turn = [{"type": "gemini", "content": content, "calls": calls}] if parts else []
    return Step(content=turn, blocks=blocks, stop_reason=stop, stop_details=details, usage=usage, model=version, request_id=rid)


class _Gemini:
    def __init__(self, model: str, effort: str, max_tokens: int, api_key: str | None, call=call_json):
        self.model, self.effort, self.max_tokens = model, effort, max_tokens
        self.info = model_info(model)
        self.key = api_key
        self.call = call

    def _post(self, body: dict) -> dict:
        # The key goes in a header, never in the URL, so it stays out of logs.
        data, _ = self.call("google", "POST", f"{base_url('google')}/models/{self.model}:generateContent", {"x-goog-api-key": self.key or ""}, body, timeout=900)
        return data

    def _thinking(self, include: bool) -> dict:
        config = {"thinkingLevel": THINKING_LEVEL[effort_for(self.info, self.effort)]}
        if include:
            config["includeThoughts"] = True
        return config


class GeminiAgent(_Gemini):
    """The assistant under test on generateContent. Gemini has no system messages in the middle of
    a conversation, so operator notices go inside the user turn, as for older Claude models."""

    def __init__(self, model: str, effort: str = "medium", max_tokens: int = 32768, api_key: str | None = None, call=call_json):
        super().__init__(model, effort, max_tokens, api_key, call)
        self.notices = "user_turn"

    def request(self, system: str, tools: list[dict], messages: list[dict]) -> dict:
        return {"systemInstruction": {"parts": [{"text": system + REMINDER_NOTE}]},
                "contents": gemini_contents(inline_system_messages(messages)),
                "tools": gemini_tools(tools),
                "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                "generationConfig": {"thinkingConfig": self._thinking(True), "maxOutputTokens": self.max_tokens}}

    def respond(self, system: str, tools: list[dict], messages: list[dict]) -> Step:
        return gemini_step(self._post(self.request(system, tools, messages)), self.model)


class GeminiMonitor(_Gemini):
    """Reviews one proposed action. The verdict comes as a call to submit_verdict that the
    request forces, because the structured-output fields of generateContent are being renamed."""

    def __init__(self, model: str, effort: str = "medium", max_tokens: int = 16384, api_key: str | None = None, call=call_json):
        super().__init__(model, effort, max_tokens, api_key, call)

    def request(self, system: str, prompt: str) -> dict:
        tool = {"name": VERDICT_TOOL, "description": "Record the decision on the proposed action.", "parametersJsonSchema": VERDICT_SCHEMA}
        return {"systemInstruction": {"parts": [{"text": system + f"\n\nGive the answer by calling {VERDICT_TOOL} with these two fields."}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "tools": [{"functionDeclarations": [tool]}],
                "toolConfig": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [VERDICT_TOOL]}},
                "generationConfig": {"thinkingConfig": self._thinking(False), "maxOutputTokens": self.max_tokens}}

    def review(self, system: str, prompt: str, request: dict | None = None) -> Verdict:
        step = gemini_step(self._post(self.request(system, prompt)), self.model)
        meta = {"usage": step.usage, "model": step.model, "request_id": step.request_id}
        if step.stop_reason == "refusal":
            return Verdict("block", "The monitor model declined to review this action.", error="refusal", **meta)
        call = next((c for c in step.tool_calls if c["name"] == VERDICT_TOOL), None)
        if call is None:
            return Verdict("block", "The monitor gave no decision.", error=f"no {VERDICT_TOOL} call ({step.stop_reason})", **meta)
        return parse_verdict(call["input"], meta)


# --------------------------------------------------------------------------- choosing and checking


def make_agent(model: str, effort: str, keys: dict[str, str | None]):
    provider = provider_of(model)
    if provider == "openai":
        return OpenAIAgent(model, effort, api_key=keys.get("openai"))
    if provider == "google":
        return GeminiAgent(model, effort, api_key=keys.get("google"))
    return AnthropicAgent(model, effort=effort, api_key=keys.get("anthropic"))


def make_monitor(model: str, effort: str, keys: dict[str, str | None]):
    provider = provider_of(model)
    if provider == "openai":
        return OpenAIMonitor(model, effort, api_key=keys.get("openai"))
    if provider == "google":
        return GeminiMonitor(model, effort, api_key=keys.get("google"))
    return AnthropicMonitor(model, effort=effort, api_key=keys.get("anthropic"))


def _lookup(provider: str, model: str, key: str, call) -> dict:
    """A free model lookup (no tokens): GET /models/{id}."""
    headers = {"Authorization": f"Bearer {key}"} if provider == "openai" else {"x-goog-api-key": key}
    data, _ = call(provider, "GET", f"{base_url(provider)}/models/{model}", headers, None, timeout=20, retries=1)
    return data


def _check_http(provider: str, roles: dict[str, tuple[str, str]], key: str | None, call) -> tuple[list[dict], str | None]:
    p = PROVIDERS[provider]
    rows = [{"role": role, "model": model, "effort": effort, "name": (model_info(model).name if model_info(model) else model), "ok": False, "problems": []} for role, (model, effort) in roles.items()]
    if not key:
        for row in rows:
            row["problems"].append(f"no {p.name} API key yet: add one above")
        return rows, None
    seen: dict[str, dict | ProviderError] = {}
    for row in rows:
        model = row["model"]
        if model not in seen:
            try:
                seen[model] = _lookup(provider, model, key, call)
            except ProviderError as exc:
                seen[model] = exc
        found = seen[model]
        if isinstance(found, ProviderError):
            bad_key = found.status == 401 or found.code in ("API_KEY_INVALID", "invalid_api_key") or (provider == "google" and found.status == 403 and "unregistered" in str(found).lower())
            if bad_key:
                return rows, f"The {p.name} API did not accept this key ({found.status}). Copy it again from {p.console}."
            if found.status is None:
                return rows, f"Could not reach the {p.name} API. Check the internet connection."
            row["problems"].append({403: "this key may not use this model (403)", 404: "the API does not know this model, or this key cannot see it (404)",
                                    429: "rate limited (429); try again in a minute"}.get(found.status, f"the API answered with an error ({found.status})"))
            continue
        if provider == "google":
            row["name"] = found.get("displayName") or row["name"]
            methods = found.get("supportedGenerationMethods")
            if isinstance(methods, list) and "generateContent" not in methods:
                row["problems"].append("does not support generateContent, which the pilot uses")
            if found.get("thinking") is False:
                row["problems"].append("does not support thinking, which the pilot sets")
        row["ok"] = not row["problems"]
    return rows, None


def check_models(roles: dict[str, tuple[str, str]], keys: dict[str, str | None], *, claude_client=None, call=call_json) -> dict:
    """Free checks for each provider the chosen models run on: does the key work, and can each
    model do what the harness asks of it? `roles` maps "assistant" and "monitor" to (model, effort)."""
    rows: dict[str, dict] = {}
    errors: list[str] = []
    for provider in dict.fromkeys(provider_of(m) for m, _ in roles.values()):
        mine = {role: v for role, v in roles.items() if provider_of(v[0]) == provider}
        if provider == "anthropic":
            if not keys.get("anthropic") and not os.environ.get("ANTHROPIC_AUTH_TOKEN") and claude_client is None:
                result = {"error": None, "models": [{"role": r, "model": m, "effort": e, "name": model_info(m).name if model_info(m) else m, "ok": False, "problems": ["no Anthropic API key yet: add one above"]} for r, (m, e) in mine.items()]}
            else:
                result = check_claude_models(mine, api_key=keys.get("anthropic"), client=claude_client)
            got, error = result["models"], result["error"]
        else:
            got, error = _check_http(provider, mine, keys.get(provider), call)
        rows.update({r["role"]: r for r in got})
        if error:
            errors.append(error)
    ordered = [rows[r] for r in roles if r in rows]
    return {"ok": not errors and all(r["ok"] for r in ordered), "error": " ".join(errors) or None, "models": ordered}
