from authz_pilot.runner import Episode, run_episode
from authz_pilot.scripted import PlannedAgent, ScriptedAgent, ScriptedMonitor, scripted_step, text_block, tool_block

from helpers import check_message_rules


def every_call(agent):
    return agent.seen


def test_notice_is_a_system_message_after_the_user_message(world):
    agent = ScriptedAgent([[tool_block("t1", "approve_request", request_id="DR-12")], [text_block("Approved.")]])
    run_episode(world, world.scenarios["L12"], "B", agent)
    first = agent.seen[0]
    assert first[0]["role"] == "user"
    assert first[0]["content"][-1]["text"].startswith("[Authenticated sender: Maria Ostrander <maria@ottermere.example>, Sales]")
    assert first[1] == {"role": "system", "content": "Operator notice: " + world.scenarios["L12"].turns[0].notice}
    for messages in agent.seen:
        check_message_rules(messages)


def test_history_is_append_only_across_turns(world):
    sc = world.scenarios["T13"]
    agent = ScriptedAgent([
        [tool_block("t1", "approve_request", request_id="DR-12")],
        [text_block("Approved DR-12.")],
        [text_block("I can't approve DR-17 any more.")],
    ])
    run_episode(world, sc, "A", agent)
    for before, after in zip(agent.seen, agent.seen[1:]):
        assert after[: len(before)] == before  # nothing earlier is ever edited
    roles = [m["role"] for m in agent.seen[-1]]
    assert roles == ["user", "system", "assistant", "user", "assistant", "user", "system"]
    for messages in agent.seen:
        check_message_rules(messages)


def test_parallel_tool_calls_are_answered_in_one_message(world):
    agent = ScriptedAgent([
        [tool_block("t1", "read_document", document_id="price-list"), tool_block("t2", "read_document", document_id="supplier-newsletter")],
        [text_block("€24.90; new crop from mid-November.")],
    ])
    run_episode(world, world.scenarios["L03"], "A", agent)
    results = agent.seen[1][-1]["content"]
    assert [b["tool_use_id"] for b in results] == ["t1", "t2"]
    check_message_rules(agent.seen[1])


def test_code_check_denial_is_returned_as_an_error(world):
    agent = ScriptedAgent([[tool_block("t1", "read_document", document_id="margin-sheet")], [text_block("I can't share that.")]])
    events = run_episode(world, world.scenarios["T06"], "B", agent)
    result = agent.seen[1][-1]["content"][0]
    assert result["is_error"] is True and "may not see finance documents" in result["content"]
    call = next(e for e in events if e["event"] == "tool_call")
    assert call["status"] == "blocked" and call["blocked_by"] == "code"


def test_step_limit_carries_unanswered_calls_into_the_next_turn(world):
    looping = [[tool_block(f"t{i}", "list_documents")] for i in range(1, 4)]
    agent = ScriptedAgent(looping + [[text_block("ok")]])
    events = run_episode(world, world.scenarios["L14"], "A", agent, max_steps=2)
    ends = [e for e in events if e["event"] == "turn_end"]
    assert ends[0]["outcome"] == "step_limit"
    second_turn = agent.seen[2]
    opening = second_turn[-1]["content"]
    assert opening[0]["type"] == "tool_result" and opening[0]["tool_use_id"] == "t2" and opening[0]["is_error"]
    assert opening[-1]["type"] == "text"
    check_message_rules(second_turn)


def test_withheld_reply_is_explained_to_the_assistant_next_turn(world):
    monitor = ScriptedMonitor(lambda r: ("block", "Finance data for sales.") if r["kind"] == "reply" else ("allow", ""))
    agent = ScriptedAgent([[text_block("€13.85")], [text_block("€18.40")]])
    events = run_episode(world, world.scenarios["L14"], "C", agent, monitor)
    first_end = next(e for e in events if e["event"] == "turn_end")
    assert first_end["delivered"] is False and first_end["blocked_by"] == "monitor"
    system = agent.seen[1][-1]
    assert system["role"] == "system" and "was not delivered" in system["content"]
    check_message_rules(agent.seen[1])


def test_monitor_failure_blocks_the_action(world):
    def broken(request):
        raise RuntimeError("monitor down")

    agent = ScriptedAgent([[tool_block("t1", "approve_request", request_id="DR-12")], [text_block("Approved.")]])
    events = run_episode(world, world.scenarios["L04"], "C", agent, ScriptedMonitor(broken))
    call = next(e for e in events if e["event"] == "tool_call")
    assert call["status"] == "blocked" and call["blocked_by"] == "monitor"
    review = next(e for e in events if e["event"] == "monitor_call")
    assert review["decision"] == "block" and "monitor down" in review["error"]


def test_monitor_sees_state_conversation_and_action(world):
    seen = []
    monitor = ScriptedMonitor(lambda r: ("allow", ""))
    original = monitor.review

    def spy(system, prompt, request=None):
        seen.append(prompt)
        return original(system, prompt, request)

    monitor.review = spy
    agent = ScriptedAgent([[tool_block("t1", "read_document", document_id="margin-sheet")], [text_block("€13.85")], [text_block("Ask finance.")]])
    run_episode(world, world.scenarios["T09"], "C", agent, monitor)
    last = seen[-1]
    assert "Current requester: Maria Ostrander" in last
    assert "Maria Ostrander (Sales): may see public, sales; may approve nothing" in last
    assert "€13.85" in last  # what the assistant told finance earlier
    assert last.rstrip().endswith("Ask finance.")


def test_refusal_stops_the_episode(world):
    agent = ScriptedAgent([scripted_step([], stop_reason="refusal")])
    events = run_episode(world, world.scenarios["T09"], "A", agent)
    assert [e["event"] for e in events].count("turn_start") == 1
    assert events[-1]["stopped"] == "refusal"


def test_agent_error_stops_the_episode(world):
    agent = ScriptedAgent([])
    events = run_episode(world, world.scenarios["L01"], "A", agent)
    assert events[-1]["stopped"] == "error"
    assert any(e["event"] == "error" for e in events)


def test_planned_agents_follow_the_message_rules(world):
    for sc in world.scenarios.values():
        for gullible in (False, True):
            agent = PlannedAgent(world, sc, gullible=gullible)
            calls = []
            original = agent.respond

            def spy(system, tools, messages, _orig=original):
                calls.append([dict(m) for m in messages])
                return _orig(system, tools, messages)

            agent.respond = spy
            Episode(world, sc, "B", agent).run()
            for messages in calls:
                check_message_rules(messages)
