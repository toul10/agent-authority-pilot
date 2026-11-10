"""Runs the real API adapters against a local mock server: no network, no key."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
import pytest
from helpers import check_message_rules

from authz_pilot.models import AnthropicAgent, AnthropicMonitor
from authz_pilot.runner import run_episode
from authz_pilot.scoring import score_episode


def message(content, stop_reason, **extra):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": extra.pop("model", "claude-opus-5-5"),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_creation_input_tokens": 900,
            "cache_read_input_tokens": 0,
        },
        **extra,
    }


class MockAPI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append({"path": self.path, "body": json.loads(body), "headers": dict(self.headers)})
                payload = json.dumps(outer.responses.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("request-id", f"req_test_{len(outer.requests)}")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        host, port = self.server.server_address
        self.client = anthropic.Anthropic(api_key="test-key-not-real", base_url=f"http://{host}:{port}", max_retries=0)
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def test_agent_and_monitor_roundtrip(world):
    thinking = {
        "type": "thinking",
        "thinking": "Maria may approve discounts per the notice.",
        "signature": "sig-abc123",
    }
    responses = [
        message(
            [
                thinking,
                {"type": "tool_use", "id": "toolu_01", "name": "approve_request", "input": {"request_id": "DR-12"}},
            ],
            "tool_use",
        ),
        message(
            [{"type": "text", "text": '{"decision": "allow", "reason": "Requested and permitted."}'}],
            "end_turn",
            model="claude-sonnet-5-5",
        ),
        message([{"type": "text", "text": "Approved DR-12."}], "end_turn"),
        message(
            [{"type": "text", "text": '{"decision": "allow", "reason": "Nothing protected."}'}],
            "end_turn",
            model="claude-sonnet-5-5",
        ),
    ]
    with MockAPI(responses) as api:
        agent = AnthropicAgent(client=api.client)
        monitor = AnthropicMonitor(client=api.client)
        events = run_episode(world, world.scenarios["L12"], "C", agent, monitor)

    assert all(r["path"] == "/v1/messages" for r in api.requests)
    first, review, second, _ = (r["body"] for r in api.requests)

    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "medium"}
    assert first["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in first["tools"]] == [
        "list_documents",
        "read_document",
        "send_email",
        "approve_request",
        "escalate_to_human",
    ]
    assert "temperature" not in first and "tool_choice" not in first
    assert first["messages"][1]["role"] == "system" and first["messages"][1]["content"].startswith("Operator notice:")
    check_message_rules(first["messages"])

    assert review["model"] == "claude-sonnet-5-5"
    assert review["output_config"]["format"]["type"] == "json_schema"
    assert "approve_request DR-12" in review["messages"][0]["content"]

    # The assistant's turn goes back unchanged, thinking signature included.
    assistant = second["messages"][2]
    assert assistant["role"] == "assistant" and assistant["content"][0]["signature"] == "sig-abc123"
    assert second["messages"][3]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_01", "content": "Request DR-12 approved."}
    ]
    check_message_rules(second["messages"])

    call = next(e for e in events if e["event"] == "model_call")
    assert call["request_id"] == "req_test_1"
    assert call["usage"]["cache_creation_input_tokens"] == 900
    assert call["content"][0]["signature"] == "<10 chars>"  # signatures stay out of the logs
    reviews = [e for e in events if e["event"] == "monitor_call"]
    assert [r["decision"] for r in reviews] == ["allow", "allow"]
    score = score_episode(events, world)
    assert score.completed and score.violations == []
    assert score.cost > 0


def test_refusal_is_recorded(world):
    refusal = message([], "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "test"})
    with MockAPI([refusal]) as api:
        events = run_episode(world, world.scenarios["L01"], "A", AnthropicAgent(client=api.client))
    call = next(e for e in events if e["event"] == "model_call")
    assert call["stop_reason"] == "refusal" and call["stop_details"]["category"] == "cyber"
    assert events[-1]["stopped"] == "refusal"


@pytest.mark.parametrize("text", ["not json", '{"decision": "maybe", "reason": "?"}'])
def test_unreadable_monitor_answer_blocks(world, text):
    responses = [
        message(
            [{"type": "tool_use", "id": "toolu_01", "name": "approve_request", "input": {"request_id": "DR-12"}}],
            "tool_use",
        ),
        message([{"type": "text", "text": text}], "end_turn", model="claude-sonnet-5-5"),
        message([{"type": "text", "text": "The approval was blocked."}], "end_turn"),
        message(
            [{"type": "text", "text": '{"decision": "allow", "reason": "ok"}'}], "end_turn", model="claude-sonnet-5-5"
        ),
    ]
    with MockAPI(responses) as api:
        events = run_episode(
            world, world.scenarios["L04"], "C", AnthropicAgent(client=api.client), AnthropicMonitor(client=api.client)
        )
    call = next(e for e in events if e["event"] == "tool_call")
    assert call["status"] == "blocked" and call["blocked_by"] == "monitor"
