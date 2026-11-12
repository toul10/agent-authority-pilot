"""The OpenAI, Gemini and xAI adapters against strict local mocks: requests the APIs accept,
replays that are exact, usage in the Claude API's terms, and every way an answer can end."""

import functools
import json

import pytest
from mock_providers import (
    GEMINI_KEY,
    OPENAI_KEY,
    XAI_KEY,
    MockGemini,
    MockOpenAI,
    MockXAI,
    candidate,
    response,
)

from authz_pilot.dashboard import outcome
from authz_pilot.models import MODEL_INFO, VERDICT_SCHEMA, cost_usd, model_info
from authz_pilot.providers import (
    GeminiAgent,
    GeminiMonitor,
    GrokAgent,
    GrokMonitor,
    OpenAIAgent,
    OpenAIMonitor,
    ProviderError,
    call_json,
    check_models,
    gemini_contents,
    gemini_usage,
    openai_usage,
    xai_usage,
)
from authz_pilot.runner import run_episode
from authz_pilot.scoring import score_episode
from authz_pilot.tools import TOOLS

GPT = [m for m, i in MODEL_INFO.items() if i.provider == "openai"]
GEMINI = [m for m, i in MODEL_INFO.items() if i.provider == "google"]
GROK = [m for m, i in MODEL_INFO.items() if i.provider == "xai"]


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


@pytest.fixture
def xai_api(monkeypatch):
    with MockXAI() as api:
        monkeypatch.setenv("AUTHZ_PILOT_XAI_BASE_URL", api.url)
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


@pytest.mark.parametrize("effort", ["low", "max"])
@pytest.mark.parametrize("model", GROK)
def test_each_grok_model_runs_an_episode_with_a_notice_and_the_monitor(world, xai_api, model, effort):
    events = run_episode(
        world,
        world.scenarios["T13"],
        "C",
        GrokAgent(model, effort, api_key=XAI_KEY),
        GrokMonitor(model, effort, api_key=XAI_KEY),
    )
    assert not problems(events), problems(events)
    sent = posts(xai_api)
    agent = [b for b in sent if "tools" in b]
    assert len(agent) >= 4  # two messages, each a tool call and then a reply: every replay was accepted
    # The notice goes as a system message after the conversation has started, never as a developer message.
    last = agent[-1]["input"]
    at = next(i for i, item in enumerate(last) if item.get("role") == "system" and "Operator notice" in item["content"])
    assert at > 0 and not any(item.get("role") == "developer" for b in sent for item in b["input"])
    expected = {"low": "low", "max": "high" if model == "grok-4.5" else "xhigh"}[effort]  # Grok has no max
    assert all(b["reasoning"] == {"effort": expected} and b["store"] is False for b in sent)
    assert all(b["include"] == ["reasoning.encrypted_content"] and "strict" not in b["tools"][0] for b in agent)
    calls = [e for e in events if e["event"] == "tool_call"]
    assert calls and all(c["status"] == "executed" and c["id"].startswith("call_") for c in calls)
    assert any(e["event"] == "model_call" and any(b["type"] == "thinking" for b in e["content"]) for e in events)
    reviews = [e for e in events if e["event"] == "monitor_call"]
    assert reviews and all(e["decision"] == "allow" and e["model"] == model for e in reviews)


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


def test_the_xai_mock_rejects_what_the_api_rejects(xai_api):
    agent = GrokAgent("grok-4.6", api_key=XAI_KEY)
    history = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    first = agent.respond("system", TOOLS, history)
    call = first.tool_calls[0]
    tampered = [dict(i) for i in first.content]
    tampered[0]["encrypted_content"] = "changed"
    with pytest.raises(ProviderError, match="whole, unchanged and in order") as err:
        agent.respond(
            "system",
            TOOLS,
            [
                *history,
                {"role": "assistant", "content": tampered},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "[]"}]},
            ],
        )
    # xAI's error body: {"code": <the status, described>, "error": <the message>}
    assert err.value.status == 400 and err.value.code == "Client specified an invalid argument"

    def post(body: dict) -> None:
        call_json(
            "xai",
            "POST",
            f"{xai_api.url}/responses",
            {"Authorization": f"Bearer {XAI_KEY}"},
            {"model": "grok-4.7", "input": "hi", "store": False} | body,
            timeout=5,
            retries=0,
        )

    with pytest.raises(ProviderError, match="not developer"):
        post({"input": [{"role": "developer", "content": "Operator notice: hi"}]})
    with pytest.raises(ProviderError, match=r"not supported by grok-4\.7"):  # the harness maps max to xhigh
        post({"reasoning": {"effort": "max"}})
    with pytest.raises(ProviderError, match="cannot be used with reasoning models"):
        post({"presence_penalty": 0.5})
    with pytest.raises(ProviderError, match=r"include reasoning\.encrypted_content"):
        post({"tools": [{"type": "function", "name": "f", "parameters": {"type": "object", "properties": {}}}]})
    with pytest.raises(ProviderError, match="needs at least one variant"):
        post({"text": {"format": {"type": "json_schema", "name": "v", "schema": {"type": "string", "enum": []}}}})
    # What the pilot sends passes xAI's schema rules.
    post({"text": {"format": {"type": "json_schema", "name": "verdict", "schema": VERDICT_SCHEMA, "strict": True}}})


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
    # xAI: cached reads are inside input_tokens; its example counts reasoning apart from output_tokens
    # (32 + 9 + 110 = 151 total), OpenAI inside. total_tokens tells which, so both come out the same.
    apart = {
        "input_tokens": 32,
        "input_tokens_details": {"cached_tokens": 8},
        "output_tokens": 9,
        "output_tokens_details": {"reasoning_tokens": 110},
        "total_tokens": 151,
    }
    x = xai_usage(apart)
    assert x == {
        "input_tokens": 24,
        "output_tokens": 119,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 8,
    }
    assert xai_usage(apart | {"output_tokens": 119}) == x
    assert xai_usage({k: v for k, v in apart.items() if k != "total_tokens"}) == x  # without a total: as the example


@pytest.mark.parametrize("inside", [False, True])
def test_a_grok_call_costs_what_xai_reports(monkeypatch, inside):
    with MockXAI(reasoning_inside=inside) as api:
        monkeypatch.setenv("AUTHZ_PILOT_XAI_BASE_URL", api.url)
        step = GrokAgent("grok-4.7", api_key=XAI_KEY).respond("system", TOOLS, [{"role": "user", "content": "hi"}])
        review = GrokMonitor("grok-4.3", api_key=XAI_KEY).review("system", "review this")
    # xAI's own cost of each call, in USD ticks (1e-10 USD), from the mock's own copy of the prices.
    assert cost_usd(step.model, step.usage) == pytest.approx(api.usages[0]["cost_in_usd_ticks"] / 1e10)
    assert cost_usd(review.model, review.usage) == pytest.approx(api.usages[1]["cost_in_usd_ticks"] / 1e10)
    assert review.decision == "allow"


@pytest.mark.parametrize(("name", "logged"), [("grok-4.3-20261101", "grok-4.3-20261101"), ("grok-x", "grok-4.3")])
def test_a_grok_answer_under_a_name_without_a_price_still_counts(monkeypatch, name, logged):
    # A name that matches no price would make the call free, and the spending limit would never see it.
    with MockXAI(model_name=name) as api:
        monkeypatch.setenv("AUTHZ_PILOT_XAI_BASE_URL", api.url)
        step = GrokAgent("grok-4.3", api_key=XAI_KEY).respond("system", TOOLS, [{"role": "user", "content": "hi"}])
        review = GrokMonitor("grok-4.3", api_key=XAI_KEY).review("system", "review this")
    assert step.model == review.model == logged
    assert cost_usd(step.model, step.usage) > 0 and cost_usd(review.model, review.usage) > 0


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


def test_xai_retries_a_rate_limit_but_not_used_up_credits(xai_api):
    waits = []
    call = functools.partial(call_json, sleep=waits.append)
    busy = (429, {"code": "Some resource has been exhausted", "error": "Rate limit reached for grok-4.7."})
    xai_api.responses = [busy]
    agent = GrokAgent("grok-4.7", api_key=XAI_KEY, call=call)
    assert agent.respond("system", TOOLS, [{"role": "user", "content": "hi"}]).tool_calls and len(waits) == 1
    spent = (
        429,
        {
            "code": "Some resource has been exhausted",
            "error": "Your team has either used all available credits or reached its monthly spending limit.",
        },
    )
    xai_api.responses = [spent]
    with pytest.raises(ProviderError, match="spending limit") as err:
        agent.respond("system", TOOLS, [{"role": "user", "content": "hi"}])
    assert err.value.status == 429 and len(waits) == 1  # no wait: credits do not come back by waiting


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


def test_the_grok_key_check_is_free_and_reads_the_key_state(xai_api, monkeypatch):
    roles = {"assistant": ("grok-4.7", "high"), "monitor": ("grok-4.3", "low")}
    result = check_models(roles, {"xai": XAI_KEY})
    assert result["ok"] and [r["name"] for r in result["models"]] == ["Grok 4.7", "Grok 4.3"]
    assert [(r["method"], r["path"]) for r in xai_api.requests] == [
        ("GET", "/v1/api-key"),
        ("GET", "/v1/models/grok-4.7"),
        ("GET", "/v1/models/grok-4.3"),
    ]  # lookups only, no tokens
    wrong = check_models(roles, {"xai": "xai-wrong-key-0000000000000000000"})
    assert not wrong["ok"] and "xAI API did not accept this key (400)" in wrong["error"]
    with MockXAI(blocked=True) as blocked_api:
        monkeypatch.setenv("AUTHZ_PILOT_XAI_BASE_URL", blocked_api.url)
        blocked = check_models(roles, {"xai": XAI_KEY})
    assert not blocked["ok"] and "the key is blocked" in blocked["error"]
    missing = check_models(roles, {})
    assert [r["problems"] for r in missing["models"]] == [["no xAI API key yet: add one above"]] * 2
