"""The OpenAI and Gemini adapters against strict local mocks: requests the APIs accept, replays
that are exact, usage in the Claude API's terms, and every way an answer can end."""

import functools
import json

import pytest
from mock_providers import GEMINI_KEY, OPENAI_KEY, MockGemini, MockOpenAI, candidate, response

from authz_pilot.dashboard import outcome
from authz_pilot.models import MODEL_INFO, cost_usd, model_info
from authz_pilot.providers import (
    GeminiAgent,
    GeminiMonitor,
    OpenAIAgent,
    OpenAIMonitor,
    ProviderError,
    call_json,
    check_models,
    gemini_contents,
    gemini_usage,
    openai_usage,
)
from authz_pilot.runner import run_episode
from authz_pilot.scoring import score_episode
from authz_pilot.tools import TOOLS

GPT = [m for m, i in MODEL_INFO.items() if i.provider == "openai"]
GEMINI = [m for m, i in MODEL_INFO.items() if i.provider == "google"]


@pytest.fixture
def openai_api(monkeypatch):
    with MockOpenAI() as api:
        monkeypatch.setenv("AUTHZ_PILOT_OPENAI_BASE_URL", api.url)
        yield api


@pytest.fixture
def gemini_api(monkeypatch):
    with MockGemini() as api:
        monkeypatch.setenv("AUTHZ_PILOT_GEMINI_BASE_URL", api.url)
        yield api


def problems(events):
    return [e for e in events if e["event"] == "error"] + [
        e for e in events if e["event"] == "monitor_call" and e.get("error")
    ]


def posts(api):
    return [r["body"] for r in api.requests if r["method"] == "POST"]


# --------------------------------------------------------------------------- whole episodes


@pytest.mark.parametrize("effort", ["low", "max"])
@pytest.mark.parametrize("model", GPT)
def test_each_gpt_model_runs_an_episode_with_a_notice_and_the_monitor(world, openai_api, model, effort):
    # T13 has an operator notice and two messages; the mock answers each message with a tool call first.
    events = run_episode(
        world,
        world.scenarios["T13"],
        "C",
        OpenAIAgent(model, effort, api_key=OPENAI_KEY),
        OpenAIMonitor(model, effort, api_key=OPENAI_KEY),
    )
    assert not problems(events), problems(events)
    agent = [b for b in posts(openai_api) if "tools" in b]
    assert len(agent) >= 4  # two messages, each a tool call and then a reply: every replay was accepted
    assert any(i.get("role") == "developer" and "Operator notice" in i["content"] for i in agent[-1]["input"])
    assert all(b["reasoning"]["effort"] == effort and b["store"] is False for b in agent)
    calls = [e for e in events if e["event"] == "tool_call"]
    # The call_id, not the fc_ item id.
    assert calls and all(c["status"] == "executed" and c["id"].startswith("call_") for c in calls)
    assert any(e["event"] == "model_call" and any(b["type"] == "thinking" for b in e["content"]) for e in events)


@pytest.mark.parametrize("effort", ["low", "xhigh"])
@pytest.mark.parametrize("model", GEMINI)
def test_each_gemini_model_runs_an_episode_with_a_notice_and_the_monitor(world, gemini_api, model, effort):
    events = run_episode(
        world,
        world.scenarios["T13"],
        "C",
        GeminiAgent(model, effort, api_key=GEMINI_KEY),
        GeminiMonitor(model, effort, api_key=GEMINI_KEY),
    )
    assert not problems(events), problems(events)
    agent = [b for b in posts(gemini_api) if b["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"]
    assert len(agent) >= 4
    level = {"low": "LOW", "xhigh": "HIGH"}[effort]  # Gemini's highest level is high
    assert all(b["generationConfig"]["thinkingConfig"]["thinkingLevel"] == level for b in agent)
    last = json.dumps(agent[-1]["contents"])
    assert "<system-reminder>" in last and "Operator notice" in last  # inside the user turn
    assert "only the chat system can add" in agent[-1]["systemInstruction"]["parts"][0]["text"]
    assert all("key=" not in r["path"] for r in gemini_api.requests)  # the key never goes in the URL
    monitor = [b for b in posts(gemini_api) if b["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"]
    assert monitor and all(e["decision"] == "allow" for e in events if e["event"] == "monitor_call")


def test_gemini_call_ids_are_echoed_when_the_api_sends_them(world, monkeypatch):
    with MockGemini(call_ids=True) as api:
        monkeypatch.setenv("AUTHZ_PILOT_GEMINI_BASE_URL", api.url)
        events = run_episode(world, world.scenarios["L01"], "A", GeminiAgent("gemini-3.8-flash", api_key=GEMINI_KEY))
    assert not problems(events)
    replies = [
        p["functionResponse"] for b in posts(api) for c in b["contents"] for p in c["parts"] if "functionResponse" in p
    ]
    assert replies and all(r["id"].startswith("fc-mock-") and r["name"] == "list_documents" for r in replies)


def test_a_gpt_assistant_with_a_gemini_monitor(world, openai_api, gemini_api):
    events = run_episode(
        world,
        world.scenarios["L01"],
        "C",
        OpenAIAgent("gpt-6.1-sol", api_key=OPENAI_KEY),
        GeminiMonitor("gemini-3.8-flash", api_key=GEMINI_KEY),
    )
    assert not problems(events)
    assert [e["model"] for e in events if e["event"] == "monitor_call"] == ["gemini-3.8-flash-001"] * len(
        [e for e in events if e["event"] == "monitor_call"]
    )


# --------------------------------------------------------------------------- the mocks are strict


def test_the_openai_mock_rejects_what_the_api_rejects(openai_api):
    agent = OpenAIAgent("gpt-6.1-sol", api_key=OPENAI_KEY)
    history = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    first = agent.respond("system", TOOLS, history)
    call = first.tool_calls[0]
    tampered = [dict(i) for i in first.content]
    tampered[0]["encrypted_content"] = "changed"
    with pytest.raises(ProviderError, match="whole, unchanged and in order"):
        agent.respond(
            "system",
            TOOLS,
            [
                *history,
                {"role": "assistant", "content": tampered},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "[]"}]},
            ],
        )
    item_id = next(i["id"] for i in first.content if i["type"] == "function_call")
    with pytest.raises(ProviderError, match="No tool call found"):
        agent.respond(
            "system",
            TOOLS,
            [
                *history,
                {"role": "assistant", "content": first.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": item_id, "content": "[]"}]},
            ],
        )
    with pytest.raises(ProviderError, match="not supported"):  # the harness never sends none: its lowest level is low
        call_json(
            "openai",
            "POST",
            f"{openai_api.url}/responses",
            {"Authorization": f"Bearer {OPENAI_KEY}"},
            {"model": "gpt-6-astra", "input": "hi", "store": False, "reasoning": {"effort": "none"}},
            timeout=5,
            retries=0,
        )


def test_the_gemini_mock_rejects_what_the_api_rejects(gemini_api):
    agent = GeminiAgent("gemini-4-argon", api_key=GEMINI_KEY)
    history = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    first = agent.respond("system", TOOLS, history)
    turn = json.loads(json.dumps(first.content))
    turn[0]["content"]["parts"][1]["thoughtSignature"] = "changed"
    call = first.tool_calls[0]
    with pytest.raises(ProviderError, match="exactly as it was returned"):
        agent.respond(
            "system",
            TOOLS,
            [
                *history,
                {"role": "assistant", "content": turn},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "[]"}]},
            ],
        )
    with pytest.raises(ProviderError, match="last turn must be from the user"):
        agent.respond("system", TOOLS, [*history, {"role": "assistant", "content": first.content}])
    with pytest.raises(ProviderError, match="never in the URL"):
        call_json("google", "GET", f"{gemini_api.url}/models/gemini-4-argon?key={GEMINI_KEY}", {}, timeout=5, retries=0)


# --------------------------------------------------------------------------- usage, prices, outcomes


def test_usage_is_converted_to_the_claude_api_terms():
    # OpenAI: input_tokens includes cached reads and cache writes; output includes reasoning.
    u = openai_usage(
        {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 300},
            "output_tokens": 120,
        }
    )
    assert u == {
        "input_tokens": 100,
        "output_tokens": 120,
        "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 600,
    }
    # $2 in, $10 out, $0.10 cached, writes at 1.25 x input: 100*2 + 300*2.5 + 600*0.10 + 120*10, per million
    assert cost_usd("gpt-6.1-sol-2026-10-01", u) == pytest.approx((200 + 750 + 60 + 1200) / 1e6)
    # Gemini: promptTokenCount includes cached tokens; thinking is billed as output.
    g = gemini_usage(
        {"promptTokenCount": 1000, "cachedContentTokenCount": 400, "candidatesTokenCount": 50, "thoughtsTokenCount": 70}
    )
    assert g == {
        "input_tokens": 600,
        "output_tokens": 120,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 400,
    }


def test_announced_price_changes_apply_from_their_day():
    million = {"input_tokens": 1_000_000}
    assert cost_usd("gemini-4-argon", million, "2026-11-30") == 2.0
    assert cost_usd("gemini-4-argon-001", million, "2026-12-01") == 4.0
    assert (
        cost_usd("gemini-3.8-flash", million, "2026-12-31") == 0.75
        and cost_usd("gemini-3.8-flash", million, "2027-01-01") == 1.5
    )
    assert model_info("gpt-6-luna").price_on("2030-01-01") == (0.10, 0.50, 0.01)


def test_openai_misalignment_stop_is_its_own_outcome(world, openai_api):
    stop = (
        403,
        {
            "error": {
                "message": "This conversation was stopped by misalignment monitoring.",
                "type": "invalid_request_error",
                "param": None,
                "code": "misalignment_policy_violation",
            }
        },
    )
    openai_api.responses = [stop]
    events = run_episode(world, world.scenarios["T05"], "A", OpenAIAgent("gpt-6-astra", api_key=OPENAI_KEY))
    assert events[-1]["stopped"] == "provider_stop"
    call = next(e for e in events if e["event"] == "model_call")
    assert call["stop_details"]["code"] == "misalignment_policy_violation"
    assert outcome(score_episode(events, world)) == "provider"
    assert len(posts(openai_api)) == 1  # a 403 is never retried


@pytest.mark.parametrize(
    ("answer", "stop"),
    [
        (lambda: response("gpt-6.1-sol", [], status="incomplete", incomplete="content_filter"), "refusal"),
        (
            lambda: response(
                "gpt-6.1-sol",
                [
                    {
                        "type": "message",
                        "id": "msg_x",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "refusal", "refusal": "I can't help with that."}],
                    }
                ],
            ),
            "refusal",
        ),
        (
            lambda: response(
                "gpt-6.1-sol",
                [{"type": "reasoning", "id": "rs_x", "summary": [], "encrypted_content": "e"}],
                status="incomplete",
                incomplete="max_output_tokens",
            ),
            "max_tokens",
        ),
    ],
)
def test_openai_answers_that_end_without_a_reply(openai_api, answer, stop):
    openai_api.responses = [answer()]
    step = OpenAIAgent("gpt-6.1-sol", api_key=OPENAI_KEY).respond("system", TOOLS, [{"role": "user", "content": "hi"}])
    assert step.stop_reason == stop and not step.texts


@pytest.mark.parametrize(
    ("answer", "stop"),
    [
        (
            {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}, "usageMetadata": {"promptTokenCount": 10}},
            "refusal",
        ),
        (candidate("gemini-3.8-flash", [], finish="SAFETY"), "refusal"),
        (
            candidate("gemini-3.8-flash", [{"text": "partial", "thoughtSignature": "s"}], finish="MAX_TOKENS"),
            "max_tokens",
        ),
        (candidate("gemini-3.8-flash", [], finish="MALFORMED_FUNCTION_CALL"), "malformed_function_call"),
    ],
)
def test_gemini_answers_that_end_without_a_tool_call(gemini_api, answer, stop):
    gemini_api.responses = [answer]
    step = GeminiAgent("gemini-3.8-flash", api_key=GEMINI_KEY).respond(
        "system", TOOLS, [{"role": "user", "content": "hi"}]
    )
    assert step.stop_reason == stop


def test_an_empty_gemini_turn_is_not_sent_back(world, gemini_api):
    # A malformed call ends the message with nothing to replay; the next message still gets a valid request.
    gemini_api.responses = [candidate("gemini-3.8-flash", [], finish="MALFORMED_FUNCTION_CALL")]
    events = run_episode(world, world.scenarios["T13"], "A", GeminiAgent("gemini-3.8-flash", api_key=GEMINI_KEY))
    assert not problems(events)
    assert next(e["outcome"] for e in events if e["event"] == "turn_end") == "malformed_function_call"
    later = posts(gemini_api)[1]["contents"]
    assert [c["role"] for c in later][:1] == ["user"] and all(c["parts"] for c in later)


def test_gemini_contents_merge_two_user_turns_in_a_row():
    contents = gemini_contents(
        [{"role": "user", "content": "one"}, {"role": "assistant", "content": []}, {"role": "user", "content": "two"}]
    )
    assert contents == [{"role": "user", "parts": [{"text": "one"}, {"text": "two"}]}]


# --------------------------------------------------------------------------- retries and summaries


def test_retries_follow_retry_after_but_never_repeat_a_final_answer(openai_api):
    waits = []
    call = functools.partial(call_json, sleep=waits.append)
    busy = (
        429,
        {"error": {"message": "Rate limit reached.", "type": "requests", "param": None, "code": "rate_limit_exceeded"}},
    )
    openai_api.responses = [
        busy,
        (
            503,
            {"error": {"message": "overloaded", "type": "server_error", "param": None, "code": "server_is_overloaded"}},
        ),
    ]
    step = OpenAIAgent("gpt-6-luna", api_key=OPENAI_KEY, call=call).respond(
        "system", TOOLS, [{"role": "user", "content": "hi"}]
    )
    assert step.tool_calls and len(waits) == 2
    broke = (
        429,
        {
            "error": {
                "message": "You exceeded your current quota.",
                "type": "insufficient_quota",
                "param": None,
                "code": "insufficient_quota",
            }
        },
    )
    openai_api.responses = [broke]
    with pytest.raises(ProviderError, match="insufficient_quota"):
        OpenAIAgent("gpt-6-luna", api_key=OPENAI_KEY, call=call).respond(
            "system", TOOLS, [{"role": "user", "content": "hi"}]
        )
    assert len(waits) == 2  # no wait: a used-up quota does not come back by waiting


def test_without_reasoning_summaries_the_assistant_carries_on(openai_api):
    refused = (
        400,
        {
            "error": {
                "message": "Your organization must be verified to generate reasoning summaries.",
                "type": "invalid_request_error",
                "param": "reasoning.summary",
                "code": "unsupported_value",
            }
        },
    )
    openai_api.responses = [refused]
    agent = OpenAIAgent("gpt-6.1-sol", api_key=OPENAI_KEY)
    assert agent.respond("system", TOOLS, [{"role": "user", "content": "hi"}]).tool_calls
    agent.respond("system", TOOLS, [{"role": "user", "content": "again"}])
    sent = posts(openai_api)
    assert "summary" in sent[0]["reasoning"] and all("summary" not in b["reasoning"] for b in sent[1:])


# --------------------------------------------------------------------------- the free key check


def test_key_check_looks_the_models_up_for_free(openai_api, gemini_api):
    result = check_models(
        {"assistant": ("gpt-6.1-sol", "high"), "monitor": ("gemini-3.8-flash", "medium")},
        {"openai": OPENAI_KEY, "google": GEMINI_KEY},
    )
    assert result["ok"] and [r["name"] for r in result["models"]] == ["GPT-6.1 Sol", "Gemini 3.8 Flash"]
    assert not posts(openai_api) and not posts(gemini_api)  # lookups only, no tokens
    bad = check_models(
        {"assistant": ("gpt-6.1-sol", "high"), "monitor": ("gpt-6-luna", "low")},
        {"openai": "sk-wrong-key-0000000000000000"},
    )
    assert not bad["ok"] and "did not accept this key" in bad["error"]
    bad = check_models(
        {"assistant": ("gemini-4-argon", "high"), "monitor": ("gemini-4-argon", "high")},
        {"google": "AIzaWrongKey000000000000000000000000"},
    )
    assert "Google API did not accept this key" in bad["error"]
    missing = check_models(
        {"assistant": ("gpt-6-luna", "low"), "monitor": ("gemini-3.8-flash", "low")}, {"openai": OPENAI_KEY}
    )
    assert missing["models"][0]["ok"] and missing["models"][1]["problems"] == ["no Google API key yet: add one above"]
