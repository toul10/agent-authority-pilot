"""The synthetic world and scenarios, loaded from the TOML files in data/.

Everything here is fictional: an invented company, invented people and reserved
`.example` domains.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

TRAP_TYPES = ("injection", "false_authority", "cross_context", "permission_change")
EXPECT_TYPES = ("reply", "email", "approve")
BAIT_TYPES = ("reply", "email", "approve", "read")

# --------------------------------------------------------------------------- matching

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def number_readings(token: str) -> set[Decimal]:
    """Values a number can mean when written with ',' or '.' separators.

    "4,317" -> {4317, 4.317}; "4.317,50" -> {4317.50}; "18.40" -> {18.40}.
    """
    parts = re.split(r"[.,]", token)
    if len(parts) == 1:
        return {Decimal(token)}
    readings = {Decimal("".join(parts[:-1]) + "." + parts[-1])}
    if all(len(part) == 3 for part in parts[1:]):
        readings.add(Decimal("".join(parts)))
    return readings


def numbers_in(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for token in _NUMBER.findall(text):
        found |= number_readings(token)
    return found


def _squash(text: str) -> str:
    return re.sub(r"[\s\-‐–—_]", "", text).casefold()


def is_number(value: str) -> bool:
    return bool(_NUMBER.fullmatch(value.strip()))


def contains_value(text: str, needle: str) -> bool:
    """Number-aware search used for canaries and expectations.

    "18.40" matches "€18.40" or "18,40" but not "118.40"; "DR-12" matches "dr 12".
    """
    needle = needle.strip()
    if is_number(needle):
        return bool(number_readings(needle) & numbers_in(text))
    return _squash(needle) in _squash(text)


_REQUEST_ID = re.compile(r"^\s*([A-Za-z]+)[\s\-_]*(\d+)\s*$")


def normalize_request_id(value: str) -> str:
    """"dr 12", "DR12" and "DR-12" all become "DR-12"."""
    m = _REQUEST_ID.match(value)
    return f"{m.group(1).upper()}-{m.group(2)}" if m else value.strip().upper()


# --------------------------------------------------------------------------- data model


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    role: str
    email: str


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    classification: str
    body: str
    canaries: tuple[str, ...] = ()


@dataclass(frozen=True)
class Request:
    id: str
    type: str  # "discount", "payment" or "leave"
    document: str


Grant = tuple[str, str, str]  # (person id, "read" | "approve", classification or request type)


@dataclass(frozen=True)
class Turn:
    sender: str
    message: str
    notice: str = ""  # operator notice, delivered through the system channel
    grant: tuple[Grant, ...] = ()
    revoke: tuple[Grant, ...] = ()
    asks: tuple[str, ...] = ()  # request ids the sender asks to have approved
    allow_email: tuple[str, ...] = ()  # addresses the sender asks to have emailed
    expect: tuple[dict, ...] = ()  # what a careful assistant does in this turn
    bait: tuple[dict, ...] = ()  # what a fooled assistant would do (traps only)


@dataclass(frozen=True)
class Scenario:
    id: str
    kind: str  # "legit" or "trap"
    trap: str  # "" for legit scenarios, otherwise one of TRAP_TYPES
    title: str
    description: str
    turns: tuple[Turn, ...]
    inject: tuple[tuple[str, str], ...] = ()  # (document id, text appended to it)
    extra_documents: tuple[Document, ...] = ()
    extra_requests: tuple[Request, ...] = ()


@dataclass
class World:
    company: dict
    roles: dict[str, dict]
    people: dict[str, Person]
    contacts: dict[str, dict]  # casefolded email -> {"name", "email"}
    documents: dict[str, Document]
    requests: dict[str, Request]
    scenarios: dict[str, Scenario]
    data_hash: str = ""

    def person_by_email(self, address: str) -> Person | None:
        address = address.strip().casefold()
        return next((p for p in self.people.values() if p.email.casefold() == address), None)

    def is_known_contact(self, address: str) -> bool:
        return address.strip().casefold() in self.contacts

    def role_label(self, person: Person) -> str:
        return self.roles[person.role]["label"]

    def documents_for(self, scenario: Scenario) -> dict[str, Document]:
        docs = dict(self.documents)
        for doc in scenario.extra_documents:
            docs[doc.id] = doc
        for doc_id, text in scenario.inject:
            d = docs[doc_id]
            body = d.body.rstrip() + "\n\n" + text.strip() + "\n"
            docs[doc_id] = Document(d.id, d.title, d.classification, body, d.canaries)
        return docs

    def requests_for(self, scenario: Scenario) -> dict[str, Request]:
        requests = dict(self.requests)
        requests.update({r.id: r for r in scenario.extra_requests})
        return requests


class Permissions:
    """Authoritative permissions: role defaults plus grants from operator notices."""

    def __init__(self, world: World):
        self.world = world
        self.grants: set[Grant] = set()

    def apply(self, turn: Turn) -> None:
        self.grants |= set(turn.grant)
        self.grants -= set(turn.revoke)

    def _extra(self, person_id: str, kind: str) -> set[str]:
        return {value for person, k, value in self.grants if person == person_id and k == kind}

    def readable(self, person_id: str) -> set[str]:
        role = self.world.roles[self.world.people[person_id].role]
        return set(role["read"]) | self._extra(person_id, "read")

    def approvable(self, person_id: str) -> set[str]:
        role = self.world.roles[self.world.people[person_id].role]
        return set(role["approve"]) | self._extra(person_id, "approve")

    def cleared_for(self, address: str) -> set[str]:
        """Classifications an email address may receive. Anyone who is not staff gets public only."""
        person = self.world.person_by_email(address)
        return self.readable(person.id) if person else {"public"}

    def describe(self) -> list[str]:
        lines = []
        for person in self.world.people.values():
            read = ", ".join(sorted(self.readable(person.id)))
            approve = ", ".join(sorted(self.approvable(person.id))) or "nothing"
            lines.append(f"- {person.name} ({self.world.role_label(person)}): may see {read}; may approve {approve}")
        return lines


def permissions_at(world: World, scenario: Scenario, turn_number: int) -> Permissions:
    """Permissions in force during turn `turn_number` (1-based), after that turn's notice."""
    perms = Permissions(world)
    for turn in scenario.turns[:turn_number]:
        perms.apply(turn)
    return perms


# --------------------------------------------------------------------------- loading


def _document(raw: dict) -> Document:
    return Document(
        id=raw["id"],
        title=raw["title"],
        classification=raw["classification"],
        body=raw["body"].strip() + "\n",
        canaries=tuple(raw.get("canaries", [])),
    )


def _request(raw: dict) -> Request:
    return Request(id=raw["id"], type=raw["type"], document=raw["document"])


def _grants(entries: list[dict]) -> tuple[Grant, ...]:
    out = []
    for entry in entries:
        for kind in ("read", "approve"):
            for value in entry.get(kind, []):
                out.append((entry["person"], kind, value))
    return tuple(out)


def _turn(raw: dict) -> Turn:
    return Turn(
        sender=raw["sender"],
        message=raw["message"].strip(),
        notice=raw.get("notice", "").strip(),
        grant=_grants(raw.get("grant", [])),
        revoke=_grants(raw.get("revoke", [])),
        asks=tuple(normalize_request_id(r) for r in raw.get("asks", [])),
        allow_email=tuple(a.strip().casefold() for a in raw.get("allow_email", [])),
        expect=tuple(raw.get("expect", [])),
        bait=tuple(raw.get("bait", [])),
    )


def _scenario(raw: dict) -> Scenario:
    return Scenario(
        id=raw["id"],
        kind=raw["kind"],
        trap=raw.get("trap", ""),
        title=raw["title"],
        description=raw.get("description", "").strip(),
        turns=tuple(_turn(t) for t in raw["turn"]),
        inject=tuple((i["document"], i["text"]) for i in raw.get("inject", [])),
        extra_documents=tuple(_document(d) for d in raw.get("document", [])),
        extra_requests=tuple(_request(r) for r in raw.get("request", [])),
    )


def load_world(data_dir: Path | str = DEFAULT_DATA_DIR) -> World:
    data_dir = Path(data_dir)
    world_bytes = (data_dir / "world.toml").read_bytes()
    scenario_bytes = (data_dir / "scenarios.toml").read_bytes()
    w = tomllib.loads(world_bytes.decode("utf-8"))
    s = tomllib.loads(scenario_bytes.decode("utf-8"))
    scenarios = [_scenario(raw) for raw in s["scenario"]]
    return World(
        company=w["company"],
        roles=w["roles"],
        people={p["id"]: Person(p["id"], p["name"], p["role"], p["email"]) for p in w["people"]},
        contacts={c["email"].casefold(): {"name": c["name"], "email": c["email"]} for c in w["contacts"]},
        documents={d["id"]: _document(d) for d in w["documents"]},
        requests={r["id"]: _request(r) for r in w["requests"]},
        scenarios={sc.id: sc for sc in scenarios},
        data_hash=hashlib.sha256(world_bytes + b"\0" + scenario_bytes).hexdigest()[:12],
    )


# --------------------------------------------------------------------------- validation


def _canary_ok(canary: str) -> bool:
    if is_number(canary):
        return len(re.sub(r"\D", "", canary)) >= 3
    return len(_squash(canary)) >= 5


def validate(world: World) -> list[str]:
    """Check the data for mistakes. Returns a list of problems; empty means usable."""
    problems: list[str] = []
    classifications = {c for role in world.roles.values() for c in role["read"]}
    request_types = {t for role in world.roles.values() for t in role["approve"]}

    for person in world.people.values():
        if person.role not in world.roles:
            problems.append(f"{person.id}: unknown role {person.role!r}")
    for address in world.contacts:
        if world.person_by_email(address):
            problems.append(f"contact {address} is also a staff address")

    all_documents = dict(world.documents)
    for sc in world.scenarios.values():
        for doc in sc.extra_documents:
            if doc.id in world.documents:
                problems.append(f"{sc.id}: extra document {doc.id} shadows a world document")
            all_documents[doc.id] = doc

    for doc in all_documents.values():
        if doc.classification not in classifications:
            problems.append(f"{doc.id}: unknown classification {doc.classification!r}")
        if doc.classification == "public" and doc.canaries:
            problems.append(f"{doc.id}: public documents must not have canaries")
        for canary in doc.canaries:
            if not _canary_ok(canary):
                problems.append(f"{doc.id}: canary {canary!r} is too short to detect reliably")
            if not contains_value(doc.body, canary):
                problems.append(f"{doc.id}: canary {canary!r} does not appear in the document")
            for other in all_documents.values():
                if contains_value(other.title, canary):
                    problems.append(f"{doc.id}: canary {canary!r} appears in the title of {other.id}")
                if other.id != doc.id and other.classification != doc.classification and contains_value(other.body, canary):
                    problems.append(f"{doc.id}: canary {canary!r} also appears in {other.id} ({other.classification})")

    def canaries_in(text: str) -> list[tuple[Document, str]]:
        return [(d, c) for d in all_documents.values() for c in d.canaries if contains_value(text, c)]

    def check_requests(requests: dict[str, Request], where: str) -> None:
        for req in requests.values():
            if req.document not in all_documents:
                problems.append(f"{where}{req.id}: document {req.document} does not exist")
            if req.type not in request_types:
                problems.append(f"{where}{req.id}: no role can approve {req.type!r} requests")
            if normalize_request_id(req.id) != req.id:
                problems.append(f"{where}{req.id}: request ids look like 'DR-12'")

    check_requests(world.requests, "")

    for sc in world.scenarios.values():
        where = f"scenario {sc.id}"
        if sc.kind not in ("legit", "trap"):
            problems.append(f"{where}: kind must be 'legit' or 'trap'")
        if sc.kind == "trap" and sc.trap not in TRAP_TYPES:
            problems.append(f"{where}: trap must be one of {', '.join(TRAP_TYPES)}")
        if sc.kind == "legit" and sc.trap:
            problems.append(f"{where}: legit scenarios have no trap type")
        if not sc.turns:
            problems.append(f"{where}: no turns")
        has_bait = any(turn.bait for turn in sc.turns)
        if sc.kind == "trap" and not has_bait:
            problems.append(f"{where}: trap scenarios need a bait entry")
        if sc.kind == "legit" and has_bait:
            problems.append(f"{where}: legit scenarios have no bait")
        if sc.kind == "legit" and not any(turn.expect for turn in sc.turns):
            problems.append(f"{where}: legit scenarios need at least one expectation")
        check_requests({r.id: r for r in sc.extra_requests}, f"{where}: ")
        requests = world.requests_for(sc)
        for doc_id, text in sc.inject:
            if doc_id not in all_documents:
                problems.append(f"{where}: inject targets unknown document {doc_id}")
            for d, c in canaries_in(text):
                problems.append(f"{where}: injected text contains canary {c!r} of {d.id}")

        perms = Permissions(world)
        for i, turn in enumerate(sc.turns, 1):
            at = f"{where} turn {i}"
            if turn.sender not in world.people:
                problems.append(f"{at}: unknown sender {turn.sender!r}")
                continue
            for person, kind, value in turn.grant + turn.revoke:
                if person not in world.people:
                    problems.append(f"{at}: grant for unknown person {person!r}")
                if kind == "read" and value not in classifications:
                    problems.append(f"{at}: grant of unknown classification {value!r}")
                if kind == "approve" and value not in request_types:
                    problems.append(f"{at}: grant of unknown request type {value!r}")
            if (turn.grant or turn.revoke) and not turn.notice:
                problems.append(f"{at}: permission changes must come with an operator notice")
            perms.apply(turn)
            for text in (turn.message, turn.notice):
                for d, c in canaries_in(text):
                    problems.append(f"{at}: message or notice contains canary {c!r} of {d.id}")
            for rid in turn.asks:
                if rid not in requests:
                    problems.append(f"{at}: asks for unknown request {rid}")
            for address in turn.allow_email:
                if not (world.person_by_email(address) or world.is_known_contact(address)):
                    problems.append(f"{at}: allow_email has unknown address {address}")

            sender_email = world.people[turn.sender].email
            for e in turn.expect:
                kind = e.get("type")
                if kind not in EXPECT_TYPES:
                    problems.append(f"{at}: unknown expectation type {kind!r}")
                    continue
                if kind == "approve":
                    rid = normalize_request_id(e.get("request", ""))
                    if rid not in requests:
                        problems.append(f"{at}: expectation for unknown request {rid}")
                    elif requests[rid].type not in perms.approvable(turn.sender):
                        problems.append(f"{at}: expects approval of {rid}, which the sender may not approve")
                    elif rid not in turn.asks:
                        problems.append(f"{at}: expected approval {rid} is missing from asks")
                audience = sender_email
                if kind == "email":
                    audience = e.get("to", "")
                    if not (world.person_by_email(audience) or world.is_known_contact(audience)):
                        problems.append(f"{at}: expected email to unknown address {audience!r}")
                    if audience.casefold() not in turn.allow_email:
                        problems.append(f"{at}: expected email to {audience} is missing from allow_email")
                for needle in e.get("contains", []):
                    for d, c in canaries_in(needle):
                        if d.classification not in perms.cleared_for(audience):
                            problems.append(f"{at}: expectation needs canary {c!r} of {d.id} for {audience}, who is not cleared")

            for b in turn.bait:
                kind = b.get("type")
                if kind not in BAIT_TYPES:
                    problems.append(f"{at}: unknown bait type {kind!r}")
                elif kind == "approve" and normalize_request_id(b.get("request", "")) not in requests:
                    problems.append(f"{at}: bait approves unknown request {b.get('request')}")
                elif kind == "read" and b.get("document") not in all_documents:
                    problems.append(f"{at}: bait reads unknown document {b.get('document')}")
                elif kind == "email" and not b.get("to"):
                    problems.append(f"{at}: email bait needs a recipient")
    return problems
