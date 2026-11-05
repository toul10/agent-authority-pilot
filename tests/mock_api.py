"""A local stand-in for the Claude API. It never contacts Anthropic.

It answers GET /v1/models/{id} and POST /v1/messages, and accepts only one key.
Tests start it in a thread. To try the app or the CLI without a real key:

    python tests/mock_api.py --port 8799
    ANTHROPIC_BASE_URL=http://127.0.0.1:8799 ANTHROPIC_API_KEY=sk-ant-mock-0000000000000000 \\
        python -m authz_pilot key test
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOCK_KEY = "sk-ant-mock-0000000000000000"

CAPABLE = {
    "effort": {"supported": True, **{level: {"supported": True} for level in ("low", "medium", "high", "xhigh", "max")}},
    "thinking": {"supported": True, "types": {"adaptive": {"supported": True}, "enabled": {"supported": False}}},
    "structured_outputs": {"supported": True},
}
NAMES = {
    "claude-fable-5-1": "Claude Fable 5.1",
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5",
}


def model_info(model_id: str, capabilities: dict | None = None) -> dict:
    return {
        "id": model_id,
        "type": "model",
        "display_name": NAMES.get(model_id, model_id),
        "created_at": "2026-01-01T00:00:00Z",
        "capabilities": CAPABLE if capabilities is None else capabilities,
    }


def message(content: list, stop_reason: str, model: str = "claude-opus-5-5") -> dict:
    return {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }


def sent_key(headers: dict) -> str | None:
    """The API key a request carried: SDK 2.x sends it as a bearer token, older clients as x-api-key."""
    lowered = {k.lower(): v for k, v in headers.items()}
    auth = lowered.get("authorization", "")
    return auth[len("Bearer "):] if auth.startswith("Bearer ") else lowered.get("x-api-key")


def default_reply(body: dict) -> dict:
    """The monitor allows; the assistant answers in one short text."""
    model = body.get("model", "claude-opus-5-5")
    if (body.get("output_config") or {}).get("format"):
        text = json.dumps({"decision": "allow", "reason": "Mock monitor: nothing to block."})
    else:
        text = "Mock reply: this text comes from the local mock API, not from Claude."
    return message([{"type": "text", "text": text}], "end_turn", model)


class MockAPI:
    def __init__(self, *, key: str | None = MOCK_KEY, responses: list | None = None, models: dict | None = None, port: int = 0):
        self.key = key
        self.responses = list(responses or [])
        self.models = dict(models) if models is not None else {m: model_info(m) for m in NAMES}
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("request-id", f"req_mock_{len(outer.requests)}")
                self.end_headers()
                self.wfile.write(data)

            def _error(self, status: int, kind: str, text: str) -> None:
                self._send(status, {"type": "error", "error": {"type": kind, "message": text}})

            def _authorised(self) -> bool:
                return outer.key is None or sent_key(dict(self.headers)) == outer.key

            def do_GET(self):
                outer.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
                if not self._authorised():
                    return self._error(401, "authentication_error", "invalid API key")
                m = re.fullmatch(r"/v1/models/([\w.\-]+)", self.path.split("?")[0])
                if m and m.group(1) in outer.models:
                    return self._send(200, outer.models[m.group(1)])
                self._error(404, "not_found_error", "not found")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"method": "POST", "path": self.path, "body": body, "headers": dict(self.headers)})
                if not self._authorised():
                    return self._error(401, "authentication_error", "invalid API key")
                if self.path.split("?")[0] != "/v1/messages":
                    return self._error(404, "not_found_error", "not found")
                self._send(200, outer.responses.pop(0) if outer.responses else default_reply(body))

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> MockAPI:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8799)
    port = parser.parse_args().port
    with MockAPI(port=port) as api:
        print(f"Mock Claude API on {api.url} (key {MOCK_KEY}). Ctrl-C stops it.")
        try:
            api.thread.join()
        except KeyboardInterrupt:
            pass
