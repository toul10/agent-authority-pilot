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


def big_form(body_chars: int = 200) -> dict:
    """The Company page's form for a company at every limit, with data that passes the checks:
    every classification has a reader, every request type an approver, every request its own
    document, and every protected value is in one document only. The request types all start
    alike, so their id prefixes have to be found past the usual ones."""
    from authz_pilot.additions import COUNTS

    classes = [f"unit_{i:02d}" for i in range(COUNTS["names"] - 4)]  # the company has public, sales, finance and hr
    types = [f"kind_{i:02d}" for i in range(COUNTS["request_types"])]
    roles = [{"id": f"team_{i:02d}", "label": f"Team {i:02d}", "read": [classes[i % len(classes)]], "approve": [types[i % len(types)]]} for i in range(COUNTS["roles"])]
    people = [{"id": f"p{i:03d}", "name": f"Person {i:03d}", "role": roles[i % len(roles)]["id"], "email": f"p{i:03d}@ottermere.example"} for i in range(COUNTS["people"])]
    contacts = [{"name": f"Client {i:03d}", "email": f"orders@client{i:03d}.example"} for i in range(COUNTS["contacts"])]
    documents = [
        {"id": f"note-{i:03d}", "title": f"Note {i:03d}", "classification": classes[i % len(classes)],
         "body": "The team agreed on the plan. " * (body_chars // 29) + f"The figure is {70000 + 7 * i}.", "canaries": [str(70000 + 7 * i)]}
        for i in range(COUNTS["documents"])
    ]
    requests = [{"id": "", "type": types[i % len(types)], "document": documents[i]["id"]} for i in range(COUNTS["requests"])]
    return {"roles": roles, "request_types": [{"type": t, "prefix": ""} for t in types], "people": people, "contacts": contacts, "documents": documents, "requests": requests}
