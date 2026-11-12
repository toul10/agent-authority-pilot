"""Local stand-ins for the OpenAI Responses API, the Gemini API and xAI's Responses API. They never
contact OpenAI, Google or xAI.

Each accepts one key and answers 400 to what the real API is documented to reject, plus a few
rules of the pilot's own (marked "pilot rule"). Above all, they check that the pilot sends back
what they handed out exactly: the output items of OpenAI and xAI (encrypted reasoning, call ids,
message phase) and Gemini's model turns (thought signatures), whole, unchanged and in order.

By default the assistant's first answer to each new message calls list_documents, and its
answer to the tool result is a short text, so every episode exercises tool calls and replays.
Tests can queue their own answers, or errors as (status, body).

To try the app with them, without real keys:

    python tests/mock_providers.py --port 8798
    AUTHZ_PILOT_OPENAI_BASE_URL=http://127.0.0.1:8798/openai/v1 OPENAI_API_KEY=sk-mock-openai-0000000000000000 \\
    AUTHZ_PILOT_GEMINI_BASE_URL=http://127.0.0.1:8798/gemini/v1beta GEMINI_API_KEY=AIzaMockGemini00000000000000000000000 \\
    AUTHZ_PILOT_XAI_BASE_URL=http://127.0.0.1:8798/xai/v1 XAI_API_KEY=xai-mock-0000000000000000000000000000 \\
        python -m authz_pilot app
"""  # noqa: E501 (the shell example has to stay copy-pasteable)

from __future__ import annotations

import argparse
import contextlib
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPENAI_KEY = "sk-mock-openai-0000000000000000"
GEMINI_KEY = "AIzaMockGemini00000000000000000000000"
XAI_KEY = "xai-mock-0000000000000000000000000000"

# Written down here on their own (not read from the app's model table). Effort values from the
# OpenAI model pages; thinking levels from the Gemini thinking guide.
FIVE = {"low", "medium", "high", "xhigh", "max"}
OPENAI_MODELS = {
    "gpt-6-astra": FIVE,
    "gpt-6.1-sol": FIVE,
    "gpt-6.1-terra": FIVE | {"none"},
    "gpt-6-luna": FIVE | {"none"},
}
GEMINI_MODELS = {
    "gemini-4-argon": ("Gemini 4 Argon", {"LOW", "MEDIUM", "HIGH"}),
    "gemini-3.8-flash": ("Gemini 3.8 Flash", {"LOW", "MEDIUM", "HIGH"}),
    "gemini-3.5-flash-lite": ("Gemini 3.5 Flash-Lite", {"MINIMAL", "LOW", "MEDIUM", "HIGH"}),
}
REPLY = "Mock reply: this text comes from a local mock API, not from a model."
ALLOW = {"decision": "allow", "reason": "Mock monitor: nothing to block."}


def canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def strict_schema_problem(schema: dict, where: str) -> str | None:
    """Strict mode: every object lists all its properties as required and allows no others."""
    if schema.get("type") == "object":
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is not False:
            return f"{where}: additionalProperties must be false in strict mode"
        if sorted(schema.get("required") or []) != sorted(props):
            return f"{where}: every property must be required in strict mode"
        for name, sub in props.items():
            problem = strict_schema_problem(sub, f"{where}.{name}")
            if problem:
                return problem
    return None


class _Mock:
    """A threaded HTTP server that records every request and answers through `answer`."""

    prefix = ""

    def __init__(self, *, responses: list | None = None, port: int = 0, strict: bool = True):
        self.strict = strict
        self.responses = list(responses or [])
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, payload: dict, headers: dict | None = None) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                body = None
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                with outer.lock:
                    outer.requests.append(
                        {"method": method, "path": self.path, "body": body, "headers": dict(self.headers)}
                    )
                    status, payload, headers = outer.answer(
                        method, self.path, body, {k.lower(): v for k, v in self.headers.items()}
                    )
                self._send(status, payload, headers)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}{self.prefix}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def queued(self):
        """The next queued answer: a 200 body, or (status, body) for an error."""
        if not self.responses:
            return None
        item = self.responses.pop(0)
        return item if isinstance(item, tuple) else (200, item)


# --------------------------------------------------------------------------- OpenAI


class MockOpenAI(_Mock):
    """POST /v1/responses and GET /v1/models/{id}."""

    prefix = "/v1"

    def __init__(self, *, key: str | None = OPENAI_KEY, **kwargs):
        super().__init__(**kwargs)
        self.key = key
        self.handed: list[list[dict]] = []  # the output of every response, in order
        self.item_ids: dict[str, int] = {}  # output item id -> index in handed
        self.n = 0

    def _error(
        self,
        status: int,
        message: str,
        code: str | None = None,
        param: str | None = None,
        kind: str = "invalid_request_error",
    ):
        return (
            status,
            {"error": {"message": message, "type": kind, "param": param, "code": code}},
            {"x-request-id": f"req_mock_{len(self.requests)}"},
        )

    def answer(self, method, path, body, headers):
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return self._error(401, "Missing bearer authentication in header")
        if self.key is not None and auth[len("Bearer ") :] != self.key:
            return self._error(401, "Incorrect API key provided.", code="invalid_api_key")
        path = path.split("?")[0]
        if method == "GET":
            m = re.fullmatch(r"/v1/models/([\w.\-]+)", path)
            if m and m.group(1) in OPENAI_MODELS:
                return 200, {"id": m.group(1), "object": "model", "created": 1767225600, "owned_by": "openai"}, {}
            return self._error(
                404,
                f"The model `{m.group(1) if m else path}` does not exist or you do not have access to it.",
                code="model_not_found",
            )
        if path != "/v1/responses":
            return self._error(404, "Not found.")
        problem = self.rejection(body) if self.strict else None
        if problem:
            return self._error(400, problem[0], param=problem[1])
        queued = self.queued()
        if queued and queued[0] != 200:
            status, payload = queued
            return status, payload, {"x-request-id": f"req_mock_{len(self.requests)}"}
        payload = queued[1] if queued else self.default(body)
        self.remember(payload.get("output") or [])
        return 200, payload, {"x-request-id": f"req_mock_{len(self.requests)}"}

    def remember(self, output: list[dict]) -> None:
        self.handed.append(output)
        for item in output:
            if item.get("id"):
                self.item_ids[item["id"]] = len(self.handed) - 1

    def rejection(self, body: dict) -> tuple[str, str | None] | None:
        """(message, param) for what the real API, or a pilot rule, rejects."""
        model = body.get("model")
        if model not in OPENAI_MODELS:
            return f"The model `{model}` does not exist.", "model"
        if "previous_response_id" in body:
            return "pilot rule: no previous_response_id; the pilot sends the whole conversation", "previous_response_id"
        if body.get("store") is not False:
            return "pilot rule: store must be false", "store"
        effort = (body.get("reasoning") or {}).get("effort")
        if effort is not None and effort not in OPENAI_MODELS[model]:
            return f"Unsupported value: '{effort}' is not supported with the '{model}' model.", "reasoning.effort"
        for i, tool in enumerate(body.get("tools") or []):
            if tool.get("type") != "function" or not tool.get("name"):
                return "Each tool needs type function and a name.", f"tools[{i}]"
            if tool.get("strict") is True:
                problem = strict_schema_problem(tool.get("parameters") or {}, f"tools[{i}].parameters")
                if problem:
                    return problem, f"tools[{i}].parameters"
        fmt = ((body.get("text") or {}).get("format")) or {}
        if fmt.get("type") == "json_schema":
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fmt.get("name") or ""):
                return "text.format.name must be a-z, A-Z, 0-9, _ or -, up to 64 characters.", "text.format.name"
            if fmt.get("strict") is True:
                problem = strict_schema_problem(fmt.get("schema") or {}, "text.format.schema")
                if problem:
                    return problem, "text.format.schema"
        return self.input_problem(body.get("input"))

    def input_problem(self, items) -> tuple[str, str | None] | None:
        if not isinstance(items, list):
            return None
        calls: dict[str, int] = {}
        answered: set[str] = set()
        i = 0
        while i < len(items):
            item = items[i]
            kind = item.get("type") or ("message" if "role" in item else None)
            item_id = item.get("id")
            if kind in ("reasoning", "function_call") or (kind == "message" and item.get("role") == "assistant"):
                if item_id not in self.item_ids:
                    return f"Item '{item_id}' of type '{kind}' was not produced by this API.", f"input[{i}]"
                output = self.handed[self.item_ids[item_id]]
                if output and output[0].get("id") != item_id:
                    return f"Item '{item_id}' must come with the items before it in its response.", f"input[{i}]"
                run = items[i : i + len(output)]
                if [canon(x) for x in run] != [canon(x) for x in output]:
                    return (
                        f"Items from a response must be sent back whole, unchanged and in order (from '{item_id}').",
                        f"input[{i}]",
                    )
                for x in output:
                    if x.get("type") == "function_call":
                        calls[x["call_id"]] = i
                i += len(output)
                continue
            if kind == "function_call_output":
                if item.get("call_id") not in calls:
                    return (
                        f"No tool call found for function call output with call_id {item.get('call_id')}.",
                        f"input[{i}]",
                    )
                answered.add(item["call_id"])
            elif kind == "message":
                problem = self.role_problem(item.get("role"))
                if problem:
                    return problem, f"input[{i}].role"
                pending = [c for c in calls if c not in answered]
                if pending:
                    return f"No tool output found for function call {pending[0]}.", f"input[{i}]"
            else:
                return f"Invalid input item type '{kind}'.", f"input[{i}]"
            i += 1
        pending = [c for c in calls if c not in answered]
        return (f"No tool output found for function call {pending[0]}.", "input") if pending else None

    def role_problem(self, role) -> str | None:
        """For an input message the pilot wrote (the model's own messages are checked as output items)."""
        return None if role in ("user", "developer", "system") else f"Invalid role '{role}'."

    def default(self, body: dict) -> dict:
        self.n += 1
        n = self.n
        reasoning = {
            "type": "reasoning",
            "id": f"rs_mock{n}",
            "summary": [],
            "encrypted_content": secrets.token_urlsafe(24),
        }
        if (body.get("reasoning") or {}).get("summary"):
            reasoning["summary"] = [{"type": "summary_text", "text": "Mock reasoning summary."}]
        fmt = ((body.get("text") or {}).get("format")) or {}
        last = (body.get("input") or [{}])[-1]
        if fmt.get("type") == "json_schema":
            output = [reasoning, self.message(n, json.dumps(ALLOW))]
        elif last.get("type") == "function_call_output":
            output = [reasoning, self.message(n, REPLY)]
        else:
            output = [
                reasoning,
                {
                    "type": "function_call",
                    "id": f"fc_mock{n}",
                    "call_id": f"call_mock{n}",
                    "name": "list_documents",
                    "arguments": "{}",
                    "status": "completed",
                },
            ]
        return response(body.get("model", ""), output, n)

    @staticmethod
    def message(n: int, text: str) -> dict:
        return {
            "type": "message",
            "id": f"msg_mock{n}",
            "role": "assistant",
            "status": "completed",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }


def response(model: str, output: list, n: int = 0, *, status: str = "completed", incomplete: str | None = None) -> dict:
    return {
        "id": f"resp_mock{n}",
        "object": "response",
        "status": status,
        "model": f"{model}-2026-10-01",
        "output": output,
        "error": None,
        "incomplete_details": {"reason": incomplete} if incomplete else None,
        "usage": {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 300},
            "output_tokens": 120,
            "output_tokens_details": {"reasoning_tokens": 80},
            "total_tokens": 1120,
        },
    }


# --------------------------------------------------------------------------- Gemini


class MockGemini(_Mock):
    """POST /v1beta/models/{model}:generateContent and GET /v1beta/models/{model}."""

    prefix = "/v1beta"

    def __init__(self, *, key: str | None = GEMINI_KEY, call_ids: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.key = key
        self.call_ids = call_ids  # whether functionCall parts carry an id (the API may leave it out)
        self.handed: set[str] = set()  # every model turn handed out, as canonical JSON
        self.n = 0

    @staticmethod
    def _error(code: int, status: str, message: str, reason: str | None = None):
        err = {"code": code, "message": message, "status": status}
        if reason:
            err["details"] = [
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason, "domain": "googleapis.com"}
            ]
        return code, {"error": err}, {}

    def answer(self, method, path, body, headers):
        if "key=" in path.partition("?")[2]:
            return self._error(
                400, "INVALID_ARGUMENT", "pilot rule: the key goes in the x-goog-api-key header, never in the URL"
            )
        key = headers.get("x-goog-api-key")
        if not key:
            return self._error(
                403,
                "PERMISSION_DENIED",
                "Method doesn't allow unregistered callers (callers without established identity).",
            )
        if self.key is not None and key != self.key:
            return self._error(
                400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.", "API_KEY_INVALID"
            )
        path = path.split("?")[0]
        if method == "GET":
            m = re.fullmatch(r"/v1beta/models/([\w.\-]+)", path)
            if m and m.group(1) in GEMINI_MODELS:
                name = m.group(1)
                return (
                    200,
                    {
                        "name": f"models/{name}",
                        "baseModelId": name,
                        "version": "001",
                        "displayName": GEMINI_MODELS[name][0],
                        "inputTokenLimit": 1048576,
                        "outputTokenLimit": 65536,
                        "supportedGenerationMethods": ["generateContent", "countTokens"],
                        "thinking": True,
                    },
                    {},
                )
            return self._error(
                404, "NOT_FOUND", f"models/{m.group(1) if m else path} is not found for API version v1beta."
            )
        m = re.fullmatch(r"/v1beta/models/([\w.\-]+):generateContent", path)
        if not m or m.group(1) not in GEMINI_MODELS:
            return self._error(404, "NOT_FOUND", f"{path} is not found.")
        model = m.group(1)
        problem = self.rejection(model, body) if self.strict else None
        if problem:
            return self._error(400, "INVALID_ARGUMENT", problem)
        queued = self.queued()
        if queued and queued[0] != 200:
            return queued[0], queued[1], {}
        payload = queued[1] if queued else self.default(model, body)
        for c in payload.get("candidates") or []:
            if (c.get("content") or {}).get("parts"):
                self.handed.add(canon(c["content"]))
        return 200, payload, {}

    def rejection(self, model: str, body: dict) -> str | None:
        config = body.get("generationConfig") or {}
        for name in ("temperature", "topP", "topK", "candidateCount"):
            if name in config:
                return f"generationConfig.{name} is not supported on {model}"
        thinking = config.get("thinkingConfig") or {}
        if "thinkingBudget" in thinking:
            return "pilot rule: the pilot sets thinkingLevel, not thinkingBudget"
        level = thinking.get("thinkingLevel")
        if level is not None and level not in GEMINI_MODELS[model][1]:
            return f"thinking_level {level} is not supported by {model}"
        mode = (((body.get("toolConfig") or {}).get("functionCallingConfig")) or {}).get("mode", "AUTO")
        if mode not in ("AUTO", "ANY", "NONE", "VALIDATED"):
            return f"function_calling_config.mode {mode} is invalid"
        if (((body.get("toolConfig") or {}).get("functionCallingConfig")) or {}).get(
            "allowedFunctionNames"
        ) and mode not in ("ANY", "VALIDATED"):
            return "allowed_function_names can only be set in ANY or VALIDATED mode"
        for tool in body.get("tools") or []:
            for d in tool.get("functionDeclarations") or []:
                if not d.get("name") or ("parameters" in d and "parametersJsonSchema" in d):
                    return (
                        "each function declaration needs a name and at most one of parameters and parametersJsonSchema"
                    )
        contents = body.get("contents") or []
        if not contents:
            return "contents is not specified"
        previous = None
        for i, c in enumerate(contents):
            role, parts = c.get("role"), c.get("parts") or []
            if role not in ("user", "model"):
                return f"contents[{i}].role must be user or model, not {role!r}"
            if not parts:
                return f"contents[{i}].parts must not be empty"
            if role == "model" and canon(c) not in self.handed:
                return (
                    f"contents[{i}]: a model turn must be sent back exactly as it was returned, "
                    "thought signatures included"
                )
            responses = [p["functionResponse"] for p in parts if "functionResponse" in p]
            if responses:
                calls = (
                    [p["functionCall"] for p in (previous or {}).get("parts", []) if "functionCall" in p]
                    if (previous or {}).get("role") == "model"
                    else []
                )
                if [r.get("name") for r in responses] != [fc.get("name") for fc in calls]:
                    return (
                        f"contents[{i}]: function responses must answer the function calls of the turn before, in order"
                    )
                for r, fc in zip(responses, calls, strict=True):
                    if fc.get("id") and r.get("id") != fc["id"]:
                        return f"contents[{i}]: function response for {fc['name']} must carry the call's id"
                    if not isinstance(r.get("response"), dict):
                        return f"contents[{i}]: function_response.response must be an object"
            previous = c
        if contents[-1]["role"] != "user":
            return "the last turn must be from the user"
        return None

    def default(self, model: str, body: dict) -> dict:
        self.n += 1
        n = self.n
        sig = secrets.token_urlsafe(24)
        allowed = (
            (((body.get("toolConfig") or {}).get("functionCallingConfig")) or {}).get("allowedFunctionNames")
        ) or []
        last = (body.get("contents") or [{}])[-1].get("parts") or []
        if allowed:
            parts = [{"functionCall": {"name": allowed[0], "args": ALLOW}, "thoughtSignature": sig}]
        elif any("text" in p for p in last) and not any("functionResponse" in p for p in last):
            call = {"name": "list_documents", "args": {}}
            if self.call_ids:
                call["id"] = f"fc-mock-{n}"
            parts = [
                {"text": "Mock thought summary.", "thought": True},
                {"functionCall": call, "thoughtSignature": sig},
            ]
        else:
            parts = [{"text": REPLY, "thoughtSignature": sig}]  # a signature can sit on a text part
        return candidate(model, parts, n=n)


def candidate(model: str, parts: list, finish: str = "STOP", n: int = 0) -> dict:
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish, "index": 0}],
        "usageMetadata": {
            "promptTokenCount": 1000,
            "cachedContentTokenCount": 400,
            "candidatesTokenCount": 50,
            "thoughtsTokenCount": 70,
            "totalTokenCount": 1120,
        },
        "modelVersion": f"{model}-001",
        "responseId": f"mock-response-{n}",
    }


# --------------------------------------------------------------------------- xAI

# Written down here on their own, from docs.x.ai (checked 2026-11-12): the reasoning efforts each model
# takes (its model page and the reasoning guide), and its price per million tokens (input, cached input,
# output), for the cost that xAI reports with each answer.
XAI_MODELS = {
    "grok-4.7": ({"low", "medium", "high", "xhigh"}, (2.00, 0.50, 6.00)),
    "grok-4.6": ({"low", "medium", "high", "xhigh"}, (2.00, 0.50, 6.00)),
    "grok-4.5": ({"low", "medium", "high", "xhigh"}, (2.00, 0.30, 6.00)),  # the guide: xhigh counts as high
    "grok-4.3": ({"none", "low", "medium", "high", "xhigh"}, (1.25, 0.20, 2.50)),
}
# xAI's error bodies are {"code": <the status, described>, "error": <the message>}.
XAI_CODES = {
    400: "Client specified an invalid argument",
    401: "Unauthenticated",
    403: "The caller does not have permission to execute the specified operation",
    404: "Some requested entity was not found",
    429: "Some resource has been exhausted",
}


def xai_schema_problem(schema, where: str) -> str | None:
    """What xAI's structured outputs guide says gets a 400: enum or anyOf without variants, a property
    schema of true or false, minContains or maxContains, and items as an array."""
    if not isinstance(schema, dict):
        return None
    for name in ("enum", "anyOf"):
        if name in schema and not schema[name]:
            return f"{where}.{name} needs at least one variant"
    for name in ("minContains", "maxContains"):
        if name in schema:
            return f"{where}: {name} is not supported"
    if isinstance(schema.get("items"), list):
        return f"{where}.items must be one schema; use prefixItems for a tuple"
    subs = [(f"{where}.properties.{k}", v) for k, v in (schema.get("properties") or {}).items()]
    for path, sub in [*subs, (f"{where}.items", schema.get("items"))]:
        if isinstance(sub, bool):
            return f"{path}: a schema cannot be true or false"
        problem = xai_schema_problem(sub, path)
        if problem:
            return problem
    return None


class MockXAI(MockOpenAI):
    """POST /v1/responses, GET /v1/models/{id} and GET /v1/api-key, as on xAI's API. Earlier output
    items must come back whole, unchanged and in order, as the OpenAI mock checks."""

    def __init__(
        self,
        *,
        key: str | None = None,
        blocked: bool = False,
        reasoning_inside: bool = False,
        model_name: str | None = None,
        **kwargs,
    ):
        super().__init__(key=key or XAI_KEY, **kwargs)
        self.blocked = blocked  # GET /api-key reports the key as blocked
        self.reasoning_inside = reasoning_inside  # usage counts reasoning inside output_tokens, as OpenAI's does
        self.model_name = model_name  # what answers call the model; the requested id when None
        self.usages: list[dict] = []  # the usage of every answer, in order

    def _error(self, status: int, message: str, code=None, param=None, kind=None):
        return status, {"code": XAI_CODES.get(status, "Unknown"), "error": message}, {}

    def answer(self, method, path, body, headers):
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return self._error(401, "No authorization header was provided.")
        given = auth[len("Bearer ") :]
        if self.key is not None and given != self.key:
            return self._error(
                400,
                f"Incorrect API key provided: {given[:2]}***{given[-2:]}. You can obtain an API key from "
                "https://console.x.ai.",
            )
        path = path.split("?")[0]
        if method == "GET":
            if path == "/v1/api-key":
                info = {"api_key_id": "mock", "name": "mock", "redacted_api_key": "xai-…0000", "team_id": "mock"}
                flags = {"api_key_blocked": self.blocked, "api_key_disabled": False, "team_blocked": False}
                return 200, info | flags, {}
            m = re.fullmatch(r"/v1/models/([\w.\-]+)", path)
            if m and m.group(1) in XAI_MODELS:
                return 200, {"id": m.group(1), "object": "model", "created": 1767225600, "owned_by": "xai"}, {}
            name = m.group(1) if m else path
            return self._error(404, f"The model {name} does not exist or your team does not have access to it.")
        if path != "/v1/responses":
            return self._error(404, "Not found.")
        problem = self.rejection(body) if self.strict else None
        if problem:
            return self._error(400, problem[0])
        queued = self.queued()
        if queued and queued[0] != 200:
            return queued[0], queued[1], {}
        payload = queued[1] if queued else self.default(body)
        self.remember(payload.get("output") or [])
        self.usages.append(payload.get("usage") or {})
        return 200, payload, {}

    def rejection(self, body: dict) -> tuple[str, str | None] | None:
        model = body.get("model")
        if model not in XAI_MODELS:
            return f"The model {model} does not exist or your team does not have access to it.", "model"
        if "previous_response_id" in body:
            return "pilot rule: no previous_response_id; the pilot sends the whole conversation", "previous_response_id"
        if body.get("store") is not False:
            return "pilot rule: store must be false", "store"
        for name in ("presence_penalty", "frequency_penalty", "stop"):
            if name in body:
                return f"{name} cannot be used with reasoning models.", name
        effort = (body.get("reasoning") or {}).get("effort")
        if effort is not None and effort not in XAI_MODELS[model][0]:
            return f"reasoning.effort {effort!r} is not supported by {model}.", "reasoning.effort"
        if body.get("tools") and "reasoning.encrypted_content" not in (body.get("include") or []):
            return (
                "pilot rule: include reasoning.encrypted_content, so earlier reasoning can come back with store false",
                "include",
            )
        for i, tool in enumerate(body.get("tools") or []):
            if tool.get("type") != "function" or not tool.get("name"):
                return "Each tool needs type function and a name.", f"tools[{i}]"
            problem = xai_schema_problem(tool.get("parameters") or {}, f"tools[{i}].parameters")
            if problem:
                return problem, f"tools[{i}].parameters"
        fmt = ((body.get("text") or {}).get("format")) or {}
        if fmt.get("type") == "json_schema":
            if not isinstance(fmt.get("schema"), dict):
                return "text.format.schema is required for json_schema.", "text.format.schema"
            problem = xai_schema_problem(fmt["schema"], "text.format.schema")
            if problem:
                return problem, "text.format.schema"
        return self.input_problem(body.get("input"))

    def role_problem(self, role) -> str | None:
        if role == "developer":
            return "pilot rule: xAI's docs name the roles system, user and assistant, not developer"
        return None if role in ("user", "system") else f"Invalid role '{role}'."

    def default(self, body: dict) -> dict:
        self.n += 1
        n = self.n
        model = body.get("model", "")
        # The summary always comes back; the encrypted reasoning from Grok 4.7 always, else when asked for.
        reasoning: dict = {
            "type": "reasoning",
            "id": f"rs_mock{n}",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": "Mock reasoning summary."}],
        }
        if model == "grok-4.7" or "reasoning.encrypted_content" in (body.get("include") or []):
            reasoning["encrypted_content"] = secrets.token_urlsafe(24)
        fmt = ((body.get("text") or {}).get("format")) or {}
        last = (body.get("input") or [{}])[-1]
        if fmt.get("type") == "json_schema":
            output = [reasoning, self.message(n, json.dumps(ALLOW))]
        elif last.get("type") == "function_call_output":
            output = [reasoning, self.message(n, REPLY)]
        else:
            output = [
                reasoning,
                {
                    "type": "function_call",
                    "id": f"fc_mock{n}",
                    "call_id": f"call_mock{n}",
                    "name": "list_documents",
                    "arguments": "{}",
                    "status": "completed",
                },
            ]
        return xai_response(model, output, n, reasoning_inside=self.reasoning_inside, name=self.model_name)

    @staticmethod
    def message(n: int, text: str) -> dict:
        return {
            "type": "message",
            "id": f"msg_mock{n}",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "logprobs": [], "annotations": []}],
        }


def xai_response(
    model: str, output: list, n: int = 0, *, reasoning_inside: bool = False, name: str | None = None
) -> dict:
    """An answer whose usage has the shape of xAI's example: cached tokens inside input_tokens, reasoning
    apart from output_tokens (or inside, to test both), and the cost xAI reports, in USD ticks (1e-10 USD)."""
    inp, cached, out, reasoning = 1000, 600, 40, 80
    price_in, price_cached, price_out = XAI_MODELS[model][1] if model in XAI_MODELS else (0.0, 0.0, 0.0)
    usd = ((inp - cached) * price_in + cached * price_cached + (out + reasoning) * price_out) / 1e6
    return {
        "id": f"resp_mock{n}",
        "object": "response",
        "status": "completed",
        "model": name or model,
        "output": output,
        "error": None,
        "incomplete_details": None,
        "usage": {
            "input_tokens": inp,
            "input_tokens_details": {"cached_tokens": cached},
            "output_tokens": out + reasoning if reasoning_inside else out,
            "output_tokens_details": {"reasoning_tokens": reasoning},
            "total_tokens": inp + out + reasoning,
            "cost_in_usd_ticks": round(usd * 1e10),
        },
    }


class MockProviders:
    """The mocks on one port, under /openai, /gemini and /xai, for trying the app by hand."""

    def __init__(self, port: int):
        self.openai, self.gemini, self.xai = MockOpenAI(), MockGemini(), MockXAI()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self, method):
                which, _, rest = self.path.lstrip("/").partition("/")
                mock = {"openai": outer.openai, "gemini": outer.gemini, "xai": outer.xai}.get(which)
                body = None
                if method == "POST":
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if mock is None:
                    status, payload, headers = 404, {"error": {"message": "not found"}}, {}
                else:
                    with mock.lock:
                        mock.requests.append(
                            {"method": method, "path": "/" + rest, "body": body, "headers": dict(self.headers)}
                        )
                        status, payload, headers = mock.answer(
                            method, "/" + rest, body, {k.lower(): v for k, v in self.headers.items()}
                        )
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8798)
    port = parser.parse_args().port
    mocks = MockProviders(port)
    print(f"Mock OpenAI API on http://127.0.0.1:{port}/openai/v1 (key {OPENAI_KEY})")
    print(f"Mock Gemini API on http://127.0.0.1:{port}/gemini/v1beta (key {GEMINI_KEY})")
    print(f"Mock xAI API on http://127.0.0.1:{port}/xai/v1 (key {XAI_KEY}). Ctrl-C stops them.")
    with contextlib.suppress(KeyboardInterrupt):
        mocks.server.serve_forever()
