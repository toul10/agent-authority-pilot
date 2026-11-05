"""Shared helpers for the tests."""

from __future__ import annotations


def check_message_rules(messages: list[dict]) -> None:
    """Assert the ordering rules the Messages API enforces on a history."""
    assert messages, "empty history"
    assert messages[0]["role"] == "user", "the first message must be a user message"
    for i, m in enumerate(messages):
        if m["role"] == "system":
            assert i > 0 and messages[i - 1]["role"] == "user", f"system message {i} must follow a user message"
            assert i == len(messages) - 1 or messages[i + 1]["role"] == "assistant", f"system message {i} must be last or followed by an assistant message"
        if m["role"] == "assistant":
            calls = [b["id"] for b in _blocks(m) if b.get("type") == "tool_use"]
            if calls and i + 1 < len(messages):
                nxt = messages[i + 1]
                assert nxt["role"] == "user", f"tool calls in message {i} must be answered by a user message"
                answered = [b["tool_use_id"] for b in _blocks(nxt) if b.get("type") == "tool_result"]
                assert sorted(answered) == sorted(calls), f"message {i + 1} must answer every tool call of message {i}"
                first_other = next((j for j, b in enumerate(_blocks(nxt)) if b.get("type") != "tool_result"), len(_blocks(nxt)))
                assert all(b.get("type") != "tool_result" for b in _blocks(nxt)[first_other:]), "tool results must come first"


def _blocks(message: dict) -> list[dict]:
    content = message["content"]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b if isinstance(b, dict) else b.to_dict() for b in content]
