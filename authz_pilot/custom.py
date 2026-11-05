"""Your own scenarios: cleaning what the app's form sends, checking it, and
storing it as TOML in the same format as data/scenarios.toml.

The server decides everything here (ids included); the browser only sends the form.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path

from .checks import scripted_checks
from .world import BAIT_TYPES, EXPECT_TYPES, TRAP_TYPES, CustomScenarioError, World, _scenario, normalize_request_id, validate

HEADER = """\
# Your own scenarios, written in the app (python -m authz_pilot app) or by hand.
# Same format as data/scenarios.toml; ids look like U01. Git ignores this folder.

"""
LIMITS = {"title": 120, "description": 600, "message": 4000, "notice": 2000, "body": 20000, "short": 200, "id": 41}
COUNTS = {"turns": 10, "items": 10, "documents": 3, "canaries": 20, "grants": 6}
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f\ud800-\udfff]")  # control characters (not tab or newline), DEL, lone surrogates
_DOC_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


# --------------------------------------------------------------------------- TOML


def _string(value: str) -> str:
    # A JSON string is a valid TOML basic string, except that JSON leaves DEL raw.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _string(key)


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }" if value else "{}"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")


def dump(raws: list[dict]) -> str:
    """Scenarios as TOML: [[scenario]] blocks, with turns and documents as [[scenario.*]] tables."""
    blocks = []
    for raw in raws:
        lines = ["[[scenario]]"]
        tables = []
        for key, value in raw.items():
            if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                tables.append((key, value))
            else:
                lines.append(f"{_key(key)} = {_value(value)}")
        for key, items in tables:
            for item in items:
                lines += ["", f"[[scenario.{_key(key)}]]"]
                lines += [f"{_key(k)} = {_value(v)}" for k, v in item.items()]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def load_raw(path: Path) -> list[dict]:
    """The scenarios in a custom file as plain dicts (an empty list if there is no file)."""
    if not path.is_file():
        return []
    try:
        raws = tomllib.loads(path.read_text(encoding="utf-8")).get("scenario", [])
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CustomScenarioError(f"{path}: {exc}") from exc
    if not isinstance(raws, list) or not all(isinstance(r, dict) for r in raws):
        raise CustomScenarioError(f"{path}: expected [[scenario]] tables")
    return raws


def save_raw(path: Path, raws: list[dict]) -> None:
    text = HEADER + dump(raws)
    tomllib.loads(text)  # never write a file that could not be read back
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".scenarios-", suffix=".toml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def snapshot(path: Path, ids: list[str]) -> str | None:
    """TOML with just these custom scenarios as they are now; each run keeps a copy."""
    wanted = set(ids)
    raws = [r for r in load_raw(path) if r.get("id") in wanted]
    return dump(raws) if raws else None


def next_id(raws: list[dict], world: World) -> str:
    used = {r.get("id") for r in raws} | set(world.scenarios)
    n = 1
    while f"U{n:02d}" in used:
        n += 1
    return f"U{n:02d}"


# --------------------------------------------------------------------------- the form


class _Form:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def text(self, value, limit: int, where: str, *, multiline: bool = False, required: bool = False) -> str:
        if value is None:
            value = ""
        if not isinstance(value, str):
            self.problems.append(f"{where}: expected text.")
            return ""
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        if not multiline:
            value = " ".join(value.split("\n"))
        value = _UNSAFE.sub("", value).strip()
        if required and not value:
            self.problems.append(f"{where} is empty.")
        if len(value) > limit:
            self.problems.append(f"{where} is longer than {limit} characters.")
        return value[:limit]

    def items(self, value, limit: int, where: str) -> list:
        if value is None:
            return []
        if not isinstance(value, list):
            self.problems.append(f"{where}: expected a list.")
            return []
        if len(value) > limit:
            self.problems.append(f"{where}: at most {limit}.")
        return value[:limit]

    def words(self, value, limit: int, where: str) -> list[str]:
        """Short values such as protected values, addresses and request ids."""
        out = []
        for i, v in enumerate(self.items(value, limit, where), 1):
            text = self.text(v, LIMITS["short"], f"{where} #{i}")
            if text and text not in out:
                out.append(text)
        return out


def _actions(f: _Form, entries, where: str, label: str, types: tuple[str, ...]) -> list[dict]:
    out = []
    for j, e in enumerate(f.items(entries, COUNTS["items"], where), 1):
        at = f"{where}, {label} {j}"
        if not isinstance(e, dict) or e.get("type") not in types:
            f.problems.append(f"{at}: choose what happens.")
            continue
        kind = e["type"]
        entry: dict = {"type": kind}
        if kind == "email":
            entry["to"] = f.text(e.get("to"), LIMITS["short"], f"{at}, recipient", required=True).casefold()
        elif kind == "approve":
            entry["request"] = normalize_request_id(f.text(e.get("request"), 20, f"{at}, request", required=True))
        elif kind == "read":
            entry["document"] = f.text(e.get("document"), LIMITS["id"], f"{at}, document", required=True)
        if kind in ("reply", "email"):
            contains = f.words(e.get("contains"), COUNTS["items"], f"{at}, values")
            if contains:
                entry["contains"] = contains
        out.append(entry)
    return out


def clean_form(form, sid: str) -> tuple[dict, list[str]]:
    """The app's form as a scenario dict (the TOML shape), plus what is wrong with it."""
    f = _Form()
    if not isinstance(form, dict):
        return {}, ["The form is empty."]
    kind = form.get("kind")
    if kind not in ("legit", "trap"):
        f.problems.append("Choose whether this is a legit request or a trap.")
        kind = "legit"
    raw: dict = {"id": sid, "kind": kind}
    if kind == "trap":
        trap = form.get("trap")
        if trap not in TRAP_TYPES:
            f.problems.append("Choose the kind of trap.")
        raw["trap"] = trap if trap in TRAP_TYPES else TRAP_TYPES[0]
    raw["title"] = f.text(form.get("title"), LIMITS["title"], "Title", required=True)
    description = f.text(form.get("description"), LIMITS["description"], "Description", multiline=True)
    if description:
        raw["description"] = description

    documents = []
    for i, d in enumerate(f.items(form.get("document"), COUNTS["documents"], "Documents"), 1):
        where = f"Document {i}"
        if not isinstance(d, dict):
            f.problems.append(f"{where}: expected fields.")
            continue
        doc_id = f.text(d.get("id"), LIMITS["id"], f"{where}, id", required=True).lower()
        if doc_id and not _DOC_ID.match(doc_id):
            f.problems.append(f"{where}, id: use lowercase letters, digits and dashes, like supplier-note.")
        doc = {
            "id": doc_id,
            "title": f.text(d.get("title"), LIMITS["title"], f"{where}, title", required=True),
            "classification": f.text(d.get("classification"), 40, f"{where}, classification", required=True),
            "body": f.text(d.get("body"), LIMITS["body"], f"{where}, text", multiline=True, required=True),
        }
        canaries = f.words(d.get("canaries"), COUNTS["canaries"], f"{where}, protected values")
        if canaries:
            doc["canaries"] = canaries
        documents.append(doc)
    if documents:
        raw["document"] = documents

    turns = []
    raw_turns = f.items(form.get("turn"), COUNTS["turns"], "Messages")
    if not raw_turns:
        f.problems.append("Add at least one message.")
    for i, t in enumerate(raw_turns, 1):
        where = f"Message {i}"
        if not isinstance(t, dict):
            f.problems.append(f"{where}: expected fields.")
            continue
        turn: dict = {
            "sender": f.text(t.get("sender"), 31, f"{where}, sender", required=True),
            "message": f.text(t.get("message"), LIMITS["message"], f"{where}, text", multiline=True, required=True),
        }
        notice = f.text(t.get("notice"), LIMITS["notice"], f"{where}, operator notice", multiline=True)
        if notice:
            turn["notice"] = notice
        for key in ("grant", "revoke"):
            changes = []
            for j, g in enumerate(f.items(t.get(key), COUNTS["grants"], f"{where}, {key}"), 1):
                if not isinstance(g, dict):
                    continue
                entry = {"person": f.text(g.get("person"), 31, f"{where}, {key} {j}, person", required=True)}
                for right in ("read", "approve"):
                    values = f.words(g.get(right), 6, f"{where}, {key} {j}, {right}")
                    if values:
                        entry[right] = values
                if len(entry) == 1:
                    f.problems.append(f"{where}, {key} {j}: choose what may be read or approved.")
                    continue
                changes.append(entry)
            if changes:
                turn[key] = changes
        expect = _actions(f, t.get("expect"), where, "expected action", EXPECT_TYPES)
        bait = _actions(f, t.get("bait"), where, "bait", BAIT_TYPES) if kind == "trap" else []
        # What a careful assistant is expected to do was, by definition, asked for.
        asks = [normalize_request_id(r) for r in f.words(t.get("asks"), COUNTS["items"], f"{where}, asks to approve")]
        asks += [e["request"] for e in expect if e["type"] == "approve" and e["request"] not in asks]
        allow = [a.casefold() for a in f.words(t.get("allow_email"), COUNTS["items"], f"{where}, asks to email")]
        allow += [e["to"] for e in expect if e["type"] == "email" and e["to"] not in allow]
        if asks:
            turn["asks"] = asks
        if allow:
            turn["allow_email"] = allow
        if expect:
            turn["expect"] = expect
        if bait:
            turn["bait"] = bait
        turns.append(turn)
    raw["turn"] = turns
    return raw, f.problems


# --------------------------------------------------------------------------- checking


def _friendly(problem: str, sid: str) -> str:
    text = re.sub(rf"^scenario {re.escape(sid)}:? ?", "", problem)
    text = re.sub(r"^turn (\d+)", r"Message \1", text)
    text = re.sub(rf"^{re.escape(sid)} \(([ABC])\): ", r"Condition \1: ", text)
    text = re.sub(rf"^{re.escape(sid)}: ", "", text)
    return text[:1].upper() + text[1:]


def check_scenario(world: World, raw: dict) -> dict:
    """Problems that stop this scenario from being saved, warnings from the
    scripted checks, and what code checks would stop. No API calls."""
    sid = raw.get("id", "")
    try:
        scenario = _scenario(raw, custom=True)
    except (KeyError, TypeError, AttributeError) as exc:
        return {"problems": [f"The scenario is incomplete ({exc})."], "warnings": [], "coverage": None}
    base = replace(world, scenarios={k: v for k, v in world.scenarios.items() if k != sid})
    with_it = replace(base, scenarios={**base.scenarios, sid: scenario})
    before = set(validate(base))
    problems = [_friendly(p, sid) for p in validate(with_it) if p not in before]
    if problems:
        return {"problems": problems, "warnings": [], "coverage": None}
    warnings, coverage = scripted_checks(with_it, ids=[sid])
    return {"problems": [], "warnings": [_friendly(w, sid) for w in warnings], "coverage": coverage[0] if coverage else None}
