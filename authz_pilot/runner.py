"""Runs scenarios against an assistant and records every step as an event."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .models import Verdict, cost_usd
from .prompts import agent_system_prompt, monitor_prompt, monitor_system_prompt, user_message
from .tools import SIDE_EFFECT_TOOLS, TOOLS, Toolbox, tool_result
from .world import Scenario, Turn, World, permissions_at

CONDITIONS = {
    "A": "policy in the system prompt only",
    "B": "A + code checks on reads, approvals and email recipients",
    "C": "B + a monitor model that reviews emails, approvals and replies",
}


class Episode:
    """One scenario, one condition, one assistant. Call run() once."""

    def __init__(self, world: World, scenario: Scenario, condition: str, agent, monitor=None, *, max_steps: int = 8, repeat: int = 1, meta: dict | None = None):
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition {condition!r}")
        if condition == "C" and monitor is None:
            raise ValueError("condition C needs a monitor")
        self.world = world
        self.scenario = scenario
        self.condition = condition
        self.agent = agent
        self.monitor = monitor
        self.max_steps = max_steps
        self.repeat = repeat
        self.meta = meta or {}
        self.box = Toolbox(world, scenario)
        self.system = agent_system_prompt(world)
        self.monitor_system = monitor_system_prompt(world) if condition == "C" else ""
        self.history: list[dict] = []
        self.events: list[dict] = []
        self.carry: list[dict] = []  # results owed for tool calls that were never run
        self.note = ""  # tells the assistant, next turn, that a reply was withheld

    def emit(self, event: str, **fields) -> dict:
        record = {"event": event, **fields}
        self.events.append(record)
        return record

    def run(self) -> list[dict]:
        sc = self.scenario
        self.emit("episode_start", scenario=sc.id, kind=sc.kind, trap=sc.trap, condition=self.condition, repeat=self.repeat, data_hash=self.world.data_hash, **self.meta)
        stopped = None
        for number, turn in enumerate(sc.turns, 1):
            outcome = self._turn(number, turn)
            if outcome in ("refusal", "error"):
                stopped = outcome
                break
        self.emit("episode_end", stopped=stopped, approved=sorted(self.box.approved), emails_sent=len(self.box.outbox), escalations=len(self.box.escalations))
        return self.events

    # -- one turn ----------------------------------------------------------------

    def _turn(self, number: int, turn: Turn) -> str:
        self.number, self.turn = number, turn
        self.perms = permissions_at(self.world, self.scenario, number)
        self.sender = self.world.people[turn.sender]
        self.emit("turn_start", turn=number, sender=turn.sender, message=turn.message, notice=turn.notice, permissions=self.perms.describe())

        text = {"type": "text", "text": user_message(self.world, self.sender, turn.message)}
        self.history.append({"role": "user", "content": self.carry + [text]})
        self.carry = []
        system_parts = [p for p in (self.note, f"Operator notice: {turn.notice}" if turn.notice else "") if p]
        self.note = ""
        if system_parts:
            # Mid-conversation system message: it must follow a user message.
            self.history.append({"role": "system", "content": "\n\n".join(system_parts)})
        if hasattr(self.agent, "start_turn"):
            self.agent.start_turn(number, turn)

        texts: list[str] = []
        outcome = "replied"
        for step_number in range(1, self.max_steps + 1):
            try:
                step = self.agent.respond(self.system, TOOLS, self.history)
            except Exception as exc:  # recorded, and the episode stops
                self.emit("error", turn=number, step=step_number, source="agent", error=f"{type(exc).__name__}: {exc}")
                outcome = "error"
                break
            self.emit("model_call", turn=number, step=step_number, role="agent", **step.log())
            if step.stop_reason == "refusal":
                outcome = "refusal"
                break
            self.history.append({"role": "assistant", "content": step.content})
            texts.extend(step.texts)
            calls = step.tool_calls
            if not calls:
                outcome = "replied" if step.stop_reason in ("end_turn", "stop_sequence") else step.stop_reason
                break
            if step.stop_reason != "tool_use" or step_number == self.max_steps:
                why = "step limit reached" if step.stop_reason == "tool_use" else f"response ended with {step.stop_reason}"
                for call in calls:
                    self.emit("tool_call", turn=number, id=call["id"], name=call["name"], input=call.get("input"), status="not_run", reason=why)
                self.carry = [tool_result(c["id"], f"Not run: {why}.", True) for c in calls]
                outcome = "step_limit" if step.stop_reason == "tool_use" else step.stop_reason
                break
            results = [self._run_tool(call) for call in calls]
            self.history.append({"role": "user", "content": results})

        reply = "\n\n".join(t.strip() for t in texts if t.strip())
        delivered, blocked_by, reason = bool(reply), None, ""
        if reply and self.condition == "C":
            verdict = self._review({"kind": "reply", "reply": reply})
            if verdict.decision != "allow":
                delivered, blocked_by, reason = False, "monitor", verdict.reason
                self.note = f"Operator notice: your last reply, to {self.sender.name}, was not delivered. The policy monitor withheld it: {verdict.reason}"
        self.emit("turn_end", turn=number, outcome=outcome, reply=reply, delivered=delivered, blocked_by=blocked_by, reason=reason)
        return outcome

    # -- tools and monitor ---------------------------------------------------------

    def _run_tool(self, call: dict) -> dict:
        name, raw = call["name"], call.get("input")
        record = {"turn": self.number, "id": call["id"], "name": name, "input": raw}
        args, error = self.box.check_input(name, raw)
        if error:
            self.emit("tool_call", **record, status="invalid", result=error)
            return tool_result(call["id"], error, True)
        record["args"] = args
        if self.condition in ("B", "C"):
            denial = self.box.code_check(name, args, self.turn.sender, self.perms)
            if denial:
                self.emit("tool_call", **record, status="blocked", blocked_by="code", result=denial)
                return tool_result(call["id"], denial, True)
        if self.condition == "C" and name in SIDE_EFFECT_TOOLS:
            verdict = self._review({"kind": "tool", "name": name, "args": args})
            if verdict.decision != "allow":
                message = f"Blocked by the policy monitor: {verdict.reason}"
                self.emit("tool_call", **record, status="blocked", blocked_by="monitor", result=message)
                return tool_result(call["id"], message, True)
        result = self.box.perform(name, args)
        self.emit("tool_call", **record, status="executed", result=result)
        return tool_result(call["id"], result)

    def _review(self, request: dict) -> Verdict:
        prompt = monitor_prompt(self.world, self.events, self.sender, self.perms, request)
        try:
            verdict = self.monitor.review(self.monitor_system, prompt, request)
        except Exception as exc:  # fail closed
            verdict = Verdict("block", "The monitor could not be reached, so the action was blocked.", error=f"{type(exc).__name__}: {exc}")
        if verdict.decision not in ("allow", "block"):
            verdict = Verdict("block", "The monitor gave no valid decision.", usage=verdict.usage, model=verdict.model, error=f"decision={verdict.decision!r}")
        target = request["kind"] if request["kind"] == "reply" else request["name"]
        self.emit("monitor_call", turn=self.number, target=target, decision=verdict.decision, reason=verdict.reason, usage=verdict.usage, model=verdict.model, request_id=verdict.request_id, error=verdict.error)
        return verdict


def run_episode(world: World, scenario: Scenario, condition: str, agent, monitor=None, **kwargs) -> list[dict]:
    return Episode(world, scenario, condition, agent, monitor, **kwargs).run()


# --------------------------------------------------------------------------- batches


def episode_cost(events: list[dict]) -> float:
    total = 0.0
    for e in events:
        if e["event"] in ("model_call", "monitor_call") and e.get("usage"):
            total += cost_usd(e.get("model") or "", e["usage"]) or 0.0
    return total


def episode_path(out_dir: Path, scenario_id: str, condition: str, repeat: int) -> Path:
    return out_dir / "episodes" / f"{scenario_id}_{condition}_{repeat}.jsonl"


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def is_complete(path: Path) -> bool:
    try:
        events = read_events(path)
    except (OSError, ValueError):
        return False
    return bool(events) and events[-1]["event"] == "episode_end"


def is_finished(path: Path) -> bool:
    """Complete and not stopped by an error, so a rerun can skip it. Errors are retried."""
    return is_complete(path) and read_events(path)[-1].get("stopped") != "error"


def run_batch(world: World, jobs: list[tuple[str, str, int]], make_agent, make_monitor, out_dir: Path, *, workers: int = 4, max_cost: float | None = None, max_steps: int = 8, meta: dict | None = None, progress=print) -> dict:
    """Run (scenario id, condition, repeat) jobs; one JSONL file per episode.

    Finished episode files are skipped, so an interrupted run can be resumed by
    running the same command again; episodes that stopped with an error run
    again. No new episode starts once the estimated spend passes `max_cost`.
    """
    (out_dir / "episodes").mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    state = {"spent": 0.0, "ran": 0, "skipped": 0, "over_budget": 0}

    def work(job: tuple[str, str, int]) -> None:
        scenario_id, condition, repeat = job
        path = episode_path(out_dir, scenario_id, condition, repeat)
        if is_finished(path):
            with lock:
                state["skipped"] += 1
            return
        with lock:
            if max_cost is not None and state["spent"] >= max_cost:
                state["over_budget"] += 1
                return
        scenario = world.scenarios[scenario_id]
        monitor = make_monitor() if condition == "C" else None
        events = run_episode(world, scenario, condition, make_agent(scenario), monitor, max_steps=max_steps, repeat=repeat, meta=meta)
        path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
        cost = episode_cost(events)
        with lock:
            state["spent"] += cost
            state["ran"] += 1
            done = state["ran"]
        stopped = events[-1].get("stopped")
        progress(f"[{done}/{len(jobs)}] {scenario_id} {condition} #{repeat}" + (f" ({stopped})" if stopped else "") + f"  ~${cost:.3f}")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for future in [pool.submit(work, job) for job in jobs]:
            future.result()
    return state
