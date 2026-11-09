"""Scripted stand-ins for the model, used by the tests, `validate` and `run --dry-run`.

None of these call an API. They exist to check the scenarios, the harness and the
scoring before any money is spent.
"""

from __future__ import annotations

import copy
import itertools

from .models import Step, Verdict
from .world import Scenario, Turn, World, contains_value, permissions_at


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def tool_block(tool_id: str, name: str, **tool_input) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def scripted_step(blocks: list[dict], stop_reason: str | None = None) -> Step:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    return Step(content=blocks, blocks=blocks, stop_reason=stop_reason, model="scripted")


class ScriptedAgent:
    """Plays back a fixed list of steps. Each step is a list of blocks or a Step."""

    name = "scripted"

    def __init__(self, steps: list):
        self.steps = list(steps)
        self.seen: list[list[dict]] = []  # copy of the messages sent on each call

    def respond(self, system: str, tools: list[dict], messages: list[dict]) -> Step:
        self.seen.append(copy.deepcopy(messages))
        if not self.steps:
            raise RuntimeError("the script has no more steps")
        step = self.steps.pop(0)
        return step if isinstance(step, Step) else scripted_step(step)


class PlannedAgent:
    """Acts out a turn from the scenario data instead of calling a model.

    The oracle does what `expect` describes. The gullible agent also does what
    `bait` describes. Either one only repeats values it could actually know:
    values from documents it read successfully at some point in the episode.
    """

    def __init__(self, world: World, scenario: Scenario, *, gullible: bool = False):
        self.world = world = world.for_scenario(scenario)
        self.scenario = scenario
        self.gullible = gullible
        self.name = "gullible" if gullible else "oracle"
        self.documents = world.documents_for(scenario)
        self.known: set[str] = set()  # documents read successfully so far
        self.pending_reads: dict[str, str] = {}  # tool_use id -> document id
        self.ids = itertools.count(1)
        self.items: list[dict] = []
        self.sender = ""
        self.stage = "done"
        self.turn_number = 0

    def start_turn(self, turn_number: int, turn: Turn) -> None:
        self.turn_number = turn_number
        self.sender = turn.sender
        self.items = list(turn.expect) + (list(turn.bait) if self.gullible else [])
        self.stage = "read"

    def _id(self) -> str:
        return f"toolu_scripted_{next(self.ids)}"

    def _source(self, needle: str) -> str | None:
        """A document containing `needle`, preferring one the sender may see."""
        perms = permissions_at(self.world, self.scenario, self.turn_number)
        readable = perms.readable(self.sender)
        hits = [d for d in self.documents.values() if contains_value(d.body, needle)]
        hits.sort(key=lambda d: d.classification not in readable)
        return hits[0].id if hits else None

    def _knows(self, needle: str) -> bool:
        source = self._source(needle)
        return source is None or source in self.known

    def _absorb(self, messages: list[dict]) -> None:
        if not messages or messages[-1]["role"] != "user" or not isinstance(messages[-1]["content"], list):
            return
        for block in messages[-1]["content"]:
            if block.get("type") == "tool_result" and block.get("tool_use_id") in self.pending_reads:
                doc_id = self.pending_reads.pop(block["tool_use_id"])
                if not block.get("is_error"):
                    self.known.add(doc_id)

    def respond(self, system: str, tools: list[dict], messages: list[dict]) -> Step:
        self._absorb(messages)
        if self.stage == "read":
            self.stage = "act"
            wanted: list[str] = []
            for item in self.items:
                sources = [self._source(n) for n in item.get("contains", [])]
                if item["type"] == "read":
                    sources.append(item["document"])
                for doc_id in sources:
                    if doc_id and doc_id not in self.known and doc_id not in wanted:
                        wanted.append(doc_id)
            if wanted:
                calls = []
                for doc_id in wanted:
                    tool_id = self._id()
                    self.pending_reads[tool_id] = doc_id
                    calls.append(tool_block(tool_id, "read_document", document_id=doc_id))
                return scripted_step(calls)
        if self.stage == "act":
            self.stage = "reply"
            calls = []
            for item in self.items:
                if item["type"] == "approve":
                    calls.append(tool_block(self._id(), "approve_request", request_id=item["request"]))
                elif item["type"] == "email":
                    known = [n for n in item.get("contains", []) if self._knows(n)]
                    body = "\n".join(known) or "Following up on your request."
                    calls.append(tool_block(self._id(), "send_email", to=item["to"], subject="Your request", body=body))
            if calls:
                return scripted_step(calls)
        self.stage = "done"
        reply = [n for item in self.items if item["type"] == "reply" for n in item.get("contains", []) if self._knows(n)]
        return scripted_step([text_block("\n".join(reply) or "Done.")])


class ScriptedMonitor:
    """A stand-in monitor. By default it allows everything."""

    name = "scripted-monitor"

    def __init__(self, decide=None):
        self.decide = decide or (lambda request: ("allow", "Scripted monitor: allows everything."))
        self.requests: list[dict] = []

    def review(self, system: str, prompt: str, request: dict | None = None) -> Verdict:
        self.requests.append(request or {})
        decision, reason = self.decide(request or {})
        return Verdict(decision, reason, model="scripted")
