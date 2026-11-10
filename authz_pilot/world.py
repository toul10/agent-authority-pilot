"""The synthetic world and scenarios, loaded from the TOML files in data/.

Everything here is fictional: an invented company, invented people and reserved
`.example` domains.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
CUSTOM_ID = re.compile(r"^U\d{2,}$")  # your own scenarios: U01, U02, ...


def custom_path() -> Path:
    """Your own scenarios, written in the app or by hand. AUTHZ_PILOT_CUSTOM moves the file."""
    override = os.environ.get("AUTHZ_PILOT_CUSTOM")
    return Path(override).expanduser() if override else PROJECT_ROOT / "custom" / "scenarios.toml"


def custom_world_path() -> Path:
    """Your additions to the company: roles, people, contacts, documents and requests.
    By default the file sits next to your scenarios; AUTHZ_PILOT_CUSTOM_WORLD moves it."""
    override = os.environ.get("AUTHZ_PILOT_CUSTOM_WORLD")
    return Path(override).expanduser() if override else custom_path().with_name("world.toml")


class CustomScenarioError(ValueError):
    """The file with your own scenarios cannot be used."""


class CustomWorldError(CustomScenarioError):
    """The file with your additions to the company cannot be used."""


TRAP_TYPES = ("injection", "false_authority", "cross_context", "permission_change")
EXPECT_TYPES = ("reply", "email", "approve")
BAIT_TYPES = ("reply", "email", "approve", "read")

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")  # ids of people and roles, classifications, request types
PREFIX = re.compile(r"^[A-Z]{1,5}$")  # the start of a request id: DR in DR-12
EMAIL = re.compile(r"^[^@\s,;<>()\"']+@[^@\s,;<>()\"']+\.[A-Za-z0-9-]+$")

# --------------------------------------------------------------------------- matching

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


# The checks and the scorer look for every protected value in the same texts again and again, so
# the readings of a text are kept (frozen, so no caller can change them for the next one).
@lru_cache(maxsize=4096)
def number_readings(token: str) -> frozenset[Decimal]:
    """Values a number can mean when written with ',' or '.' separators.

    "4,317" -> {4317, 4.317}; "4.317,50" -> {4317.50}; "18.40" -> {18.40}.
    """
    parts = re.split(r"[.,]", token)
    if len(parts) == 1:
        return frozenset({Decimal(token)})
    readings = {Decimal("".join(parts[:-1]) + "." + parts[-1])}
    if all(len(part) == 3 for part in parts[1:]):
        readings.add(Decimal("".join(parts)))
    return frozenset(readings)


@lru_cache(maxsize=1024)
def numbers_in(text: str) -> frozenset[Decimal]:
    found: set[Decimal] = set()
    for token in _NUMBER.findall(text):
        found |= number_readings(token)
    return frozenset(found)


@lru_cache(maxsize=1024)
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
    """ "dr 12", "DR12" and "DR-12" all become "DR-12"."""
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
    custom: bool = False  # written by the user, not one of the built-in scenarios


@dataclass
class World:
    company: dict
    roles: dict[str, dict]
    people: dict[str, Person]
    contacts: dict[str, dict]  # casefolded email -> {"name", "email"}
    documents: dict[str, Document]
    requests: dict[str, Request]
    scenarios: dict[str, Scenario]
    data_hash: str = ""  # the built-in data files
    custom_hash: str = ""  # the custom scenario file, if one was loaded
    # Your additions to the company, when they were loaded: `base` is the company as published,
    # `additions` lists what is yours by kind, and `custom_world_hash` identifies the file.
    base: World | None = None
    additions: dict[str, list[str]] = field(default_factory=dict)
    custom_world_hash: str = ""
    additions_error: str = ""  # why your additions could not be loaded, if they could not

    def for_scenario(self, scenario: Scenario) -> World:
        """The company a scenario runs in. Built-in scenarios always run in the company as
        published, so their prompts and scores never depend on your additions; your own
        scenarios also get your additions."""
        return self.base if self.base is not None and not scenario.custom else self

    def person_by_email(self, address: str) -> Person | None:
        address = address.strip().casefold()
        return next((p for p in self.people.values() if p.email.casefold() == address), None)

    def is_known_contact(self, address: str) -> bool:
        return address.strip().casefold() in self.contacts

    def role_label(self, person: Person) -> str:
        return self.roles[person.role]["label"]

    def classifications(self) -> list[str]:
        """Every classification some role may see, in the order the roles first name them."""
        return list(dict.fromkeys(c for role in self.roles.values() for c in role["read"]))

    def request_types(self) -> list[str]:
        """Every request type some role may approve."""
        return list(dict.fromkeys(t for role in self.roles.values() for t in role["approve"]))

    def is_yours(self, kind: str, key: str) -> bool:
        return key in self.additions.get(kind, ())

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
    perms = Permissions(world.for_scenario(scenario))
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


def _scenario(raw: dict, custom: bool = False) -> Scenario:
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
        custom=custom,
    )


def _custom_scenarios(path: Path, taken: set[str]) -> tuple[list[Scenario], bytes]:
    data = path.read_bytes()
    try:
        raws = tomllib.loads(data.decode("utf-8")).get("scenario", [])
        scenarios = [_scenario(raw, custom=True) for raw in raws]
    except KeyError as exc:
        raise CustomScenarioError(f"{path}: a scenario is missing the field {exc}") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, TypeError, AttributeError) as exc:
        raise CustomScenarioError(f"{path}: {exc}") from exc
    for sc in scenarios:
        if not CUSTOM_ID.match(sc.id):
            raise CustomScenarioError(f"{path}: scenario id {sc.id!r} must look like U01")
        if sc.id in taken:
            raise CustomScenarioError(f"{path}: scenario id {sc.id} is used twice")
        taken.add(sc.id)
    return scenarios, data


def _data_bytes(path: Path) -> bytes:
    """A data file with Windows line endings read as Unix ones, so that a checkout with CRLF
    (git's default on Windows) has the same data hash as the files the runs were made with."""
    return path.read_bytes().replace(b"\r\n", b"\n")


ADDITION_SECTIONS = ("company", "roles", "people", "contacts", "documents", "requests")
ADDITION_KINDS = ("roles", "request_types", "people", "contacts", "documents", "requests")


def _strict(value: Any, kind: type[Any], where: str) -> Any:
    if not isinstance(value, kind) or (kind is list and not all(isinstance(v, str) for v in value)):
        names: dict[type, str] = {str: "text", list: "a list of text", dict: "a table"}
        expected = names[kind]
        raise CustomWorldError(f"{where} should be {expected}")
    return value


def _field(table: dict, key: str, where: str, kind: type = str):
    if key not in table:
        raise CustomWorldError(f"{where} is missing the field {key!r}")
    return _strict(table[key], kind, f"{where}: {key}")


def merge_additions(world: World, raw: dict) -> World:
    """The company with your additions merged in, which only ever add to it.

    Raises CustomWorldError for what cannot be merged: an unknown section, a missing field, an id
    the company already uses, or a person in a role that does not exist. Everything else (an email
    that is not valid, a request type nobody may approve...) is for `validate` to report.
    """
    _strict(raw, dict, "the file")
    unknown = [k for k in raw if k not in ADDITION_SECTIONS]
    if unknown:
        raise CustomWorldError(f"unknown section [{unknown[0]}]; the sections are {', '.join(ADDITION_SECTIONS)}")
    company = _strict(raw.get("company", {}), dict, "[company]")
    if set(company) - {"request_prefixes"}:
        raise CustomWorldError(
            "[company] takes only request_prefixes: the company's name and description stay as published"
        )
    prefixes = _strict(company.get("request_prefixes", {}), dict, "request_prefixes")
    roles = _strict(raw.get("roles", {}), dict, "[roles]")
    sections = {k: raw.get(k, []) for k in ("people", "contacts", "documents", "requests")}
    for key, items in sections.items():
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise CustomWorldError(f"expected [[{key}]] tables")
    if not (prefixes or roles or any(sections.values())):
        return world

    added: dict[str, list[str]] = {k: [] for k in ADDITION_KINDS}
    all_prefixes = dict(world.company.get("request_prefixes", {}))
    for rtype, prefix in prefixes.items():
        _strict(prefix, str, f"the id prefix of {rtype} requests")
        if rtype in all_prefixes:
            raise CustomWorldError(f"request type {rtype!r} is already in the company")
        all_prefixes[rtype] = prefix
        added["request_types"].append(rtype)

    all_roles = dict(world.roles)
    for rid, role in roles.items():
        where = f"role {rid}"
        _strict(role, dict, where)
        if rid in all_roles:
            raise CustomWorldError(f"role id {rid!r} is already in the company")
        all_roles[rid] = {
            "label": _field(role, "label", where),
            "read": list(_field(role, "read", where, list)),
            "approve": list(_field(role, "approve", where, list)),
        }
        added["roles"].append(rid)

    people = dict(world.people)
    for i, p in enumerate(sections["people"], 1):
        where = f"person {p.get('id') or i}"
        person = Person(*(_field(p, k, where) for k in ("id", "name", "role", "email")))
        if person.id in people:
            raise CustomWorldError(f"person id {person.id!r} is already in the company")
        if person.role not in all_roles:
            raise CustomWorldError(f"{where}: there is no role {person.role!r}")
        people[person.id] = person
        added["people"].append(person.id)

    contacts = dict(world.contacts)
    for i, c in enumerate(sections["contacts"], 1):
        where = f"contact {c.get('email') or i}"
        email, name = _field(c, "email", where), _field(c, "name", where)
        if email.strip().casefold() in contacts:
            raise CustomWorldError(f"{email} is already a known contact")
        contacts[email.strip().casefold()] = {"name": name, "email": email.strip()}
        added["contacts"].append(email.strip().casefold())

    documents = dict(world.documents)
    for i, d in enumerate(sections["documents"], 1):
        where = f"document {d.get('id') or i}"
        for key in ("id", "title", "classification", "body"):
            _field(d, key, where)
        _strict(d.get("canaries", []), list, f"{where}: canaries")
        doc = _document(d)
        if doc.id in documents:
            raise CustomWorldError(f"document id {doc.id!r} is already in the company")
        documents[doc.id] = doc
        added["documents"].append(doc.id)

    requests = dict(world.requests)
    taken = {normalize_request_id(r) for r in requests}
    for i, r in enumerate(sections["requests"], 1):
        where = f"request {r.get('id') or i}"
        req = Request(*(_field(r, k, where) for k in ("id", "type", "document")))
        if normalize_request_id(req.id) in taken:
            raise CustomWorldError(f"request id {req.id!r} is already in the company")
        taken.add(normalize_request_id(req.id))
        requests[req.id] = req
        added["requests"].append(req.id)

    return replace(
        world,
        company=dict(world.company, request_prefixes=all_prefixes),
        roles=all_roles,
        people=people,
        contacts=contacts,
        documents=documents,
        requests=requests,
        base=world,
        additions=added,
    )


def _with_additions(world: World, path: Path) -> World:
    data = path.read_bytes()
    try:
        raw = tomllib.loads(data.decode("utf-8"))
        merged = merge_additions(world, raw)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, CustomWorldError) as exc:
        raise CustomWorldError(f"{path}: {exc}") from exc
    if merged is not world:
        merged.custom_world_hash = hashlib.sha256(data).hexdigest()[:12]
    return merged


def load_world(
    data_dir: Path | str = DEFAULT_DATA_DIR,
    custom: Path | str | None = None,
    custom_world: Path | str | None = None,
    *,
    strict: bool = True,
) -> World:
    """The built-in world and scenarios, plus your own scenarios from `custom` and your additions
    to the company from `custom_world`, for each of those files that exists.

    If your additions cannot be read, a strict load raises CustomWorldError; otherwise the world
    comes back without them, with the reason in `additions_error`.
    """
    data_dir = Path(data_dir)
    world_bytes = _data_bytes(data_dir / "world.toml")
    scenario_bytes = _data_bytes(data_dir / "scenarios.toml")
    w = tomllib.loads(world_bytes.decode("utf-8"))
    s = tomllib.loads(scenario_bytes.decode("utf-8"))
    scenarios = [_scenario(raw) for raw in s["scenario"]]
    custom_hash = ""
    if custom is not None and Path(custom).is_file():
        extra, custom_bytes = _custom_scenarios(Path(custom), {sc.id for sc in scenarios})
        scenarios += extra
        custom_hash = hashlib.sha256(custom_bytes).hexdigest()[:12] if extra else ""
    world = World(
        company=w["company"],
        roles=w["roles"],
        people={p["id"]: Person(p["id"], p["name"], p["role"], p["email"]) for p in w["people"]},
        contacts={c["email"].casefold(): {"name": c["name"], "email": c["email"]} for c in w["contacts"]},
        documents={d["id"]: _document(d) for d in w["documents"]},
        requests={r["id"]: _request(r) for r in w["requests"]},
        scenarios={sc.id: sc for sc in scenarios},
        data_hash=hashlib.sha256(world_bytes + b"\0" + scenario_bytes).hexdigest()[:12],
        custom_hash=custom_hash,
    )
    if custom_world is not None and Path(custom_world).is_file():
        try:
            world = _with_additions(world, Path(custom_world))
        except CustomWorldError as exc:
            if strict:
                raise
            world.additions_error = str(exc)
    return world


def only_added_since(data_dir: Path | str, data_hash: str) -> bool:
    """True if the data files a run used (its `data_hash`) are the current ones with scenarios
    only added at the end of scenarios.toml: every scenario the run could use is unchanged."""
    data_dir = Path(data_dir)
    world_bytes = _data_bytes(data_dir / "world.toml")
    scenario_bytes = _data_bytes(data_dir / "scenarios.toml")
    digest, size = hashlib.sha256(world_bytes + b"\0"), 0
    for line in scenario_bytes.splitlines(keepends=True):
        digest.update(line)
        size += len(line)
        if digest.copy().hexdigest()[:12] == data_hash:
            # The old file is a prefix of the new one. Text added after it could still extend
            # the last old scenario (another [[scenario.turn]]), so compare them parsed.
            try:
                old = tomllib.loads(scenario_bytes[:size].decode("utf-8"))["scenario"]
            except (tomllib.TOMLDecodeError, KeyError, UnicodeDecodeError):
                return False
            return old == tomllib.loads(scenario_bytes.decode("utf-8"))["scenario"][: len(old)]
    return False


# --------------------------------------------------------------------------- validation


def _canary_ok(canary: str) -> bool:
    if is_number(canary):
        return len(re.sub(r"\D", "", canary)) >= 3
    return len(_squash(canary)) >= 5


def _company_problems(world: World) -> list[str]:
    """Problems in the company itself: roles, request types, people and contacts."""
    problems: list[str] = []
    request_types = set(world.request_types())
    prefixes = world.company.get("request_prefixes", {})
    for rid, role in world.roles.items():
        if not NAME.match(rid):
            problems.append(f"role {rid!r}: ids use lowercase letters, digits, - and _, starting with a letter")
        if not role["label"].strip():
            problems.append(f"role {rid}: the label is empty")
        if "public" not in role["read"]:
            problems.append(f"role {rid}: every role may see public documents")
        for c in role["read"]:
            if not NAME.match(c):
                problems.append(
                    f"role {rid}: classification {c!r} uses lowercase letters, digits, - and _, starting with a letter"
                )
        for t in role["approve"]:
            if t not in prefixes:
                problems.append(f"role {rid}: request type {t!r} has no id prefix")
    for label, n in Counter(r["label"].strip().casefold() for r in world.roles.values()).items():
        if n > 1 and label:
            problems.append(f"{n} roles are called {label!r}")
    for rtype, prefix in prefixes.items():
        if not NAME.match(rtype):
            problems.append(
                f"request type {rtype!r}: names use lowercase letters, digits, - and _, starting with a letter"
            )
        if not PREFIX.match(prefix):
            problems.append(f"request type {rtype}: the id prefix {prefix!r} should be 1 to 5 capital letters, like DR")
        if rtype not in request_types:
            problems.append(f"request type {rtype}: no role may approve it")
    for prefix, n in Counter(prefixes.values()).items():
        if n > 1:
            problems.append(f"{n} request types share the id prefix {prefix}")

    for person in world.people.values():
        if person.role not in world.roles:
            problems.append(f"{person.id}: unknown role {person.role!r}")
        if not NAME.match(person.id):
            problems.append(f"person {person.id!r}: ids use lowercase letters, digits, - and _, starting with a letter")
        if not person.name.strip():
            problems.append(f"person {person.id}: the name is empty")
        if not EMAIL.match(person.email.strip()):
            problems.append(f"person {person.id}: {person.email!r} is not an email address")
    for address, n in Counter(p.email.strip().casefold() for p in world.people.values()).items():
        if n > 1:
            problems.append(f"{n} people share the address {address}")
    for address, contact in world.contacts.items():
        if world.person_by_email(address):
            problems.append(f"contact {address} is also a staff address")
        if not EMAIL.match(address):
            problems.append(f"contact {contact['name']!r}: {contact['email']!r} is not an email address")
        if not contact["name"].strip():
            problems.append(f"contact {address}: the name is empty")
    return problems


def validate(world: World) -> list[str]:
    """Check the data for mistakes. Returns a list of problems; empty means usable.

    Each scenario is checked in the company it runs in (World.for_scenario), so a built-in
    scenario may not use your additions to the company.
    """
    problems = _company_problems(world)
    classifications = set(world.classifications())
    prefixes = world.company.get("request_prefixes", {})

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
                if (
                    other.id != doc.id
                    and other.classification != doc.classification
                    and contains_value(other.body, canary)
                ):
                    problems.append(f"{doc.id}: canary {canary!r} also appears in {other.id} ({other.classification})")

    def canaries_in(text: str) -> list[tuple[Document, str]]:
        return [(d, c) for d in all_documents.values() for c in d.canaries if contains_value(text, c)]

    def check_requests(w: World, requests: dict[str, Request], where: str) -> None:
        types = set(w.request_types())
        for req in requests.values():
            if req.document not in all_documents:
                problems.append(f"{where}{req.id}: document {req.document} does not exist")
            if req.type not in types:
                problems.append(f"{where}{req.id}: no role can approve {req.type!r} requests")
            if normalize_request_id(req.id) != req.id:
                problems.append(f"{where}{req.id}: request ids look like 'DR-12'")
            elif req.type in prefixes and not req.id.startswith(prefixes[req.type] + "-"):
                problems.append(f"{where}{req.id}: the ids of {req.type} requests start with {prefixes[req.type]}-")

    def one_request_per_document(requests: dict[str, Request], doc_ids, where: str) -> None:
        # The assistant sees a request's status when it reads the request's document.
        for doc_id in dict.fromkeys(doc_ids):
            n = sum(r.document == doc_id for r in requests.values())
            if n > 1:
                problems.append(f"{where}document {doc_id} belongs to {n} requests; a document can belong to one")

    check_requests(world, world.requests, "")
    one_request_per_document(world.requests, [r.document for r in world.requests.values()], "")

    for sc in world.scenarios.values():
        w = world.for_scenario(sc)  # the company this scenario runs in
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
        requests = w.requests_for(sc)
        if sc.extra_requests:
            for req in sc.extra_requests:
                if req.id in w.requests:
                    problems.append(f"{where}: request {req.id} is already in the company")
            check_requests(w, {r.id: r for r in sc.extra_requests}, f"{where}: ")
            one_request_per_document(requests, [r.document for r in sc.extra_requests], f"{where}: ")
        own = set(w.documents) | {d.id for d in sc.extra_documents}  # what this scenario's assistant can read
        for doc_id, text in sc.inject:
            if doc_id not in own:
                problems.append(f"{where}: inject targets unknown document {doc_id}")
            for d, c in canaries_in(text):
                problems.append(f"{where}: injected text contains canary {c!r} of {d.id}")
        for doc in sc.extra_documents:
            if doc.classification in classifications and doc.classification not in w.classifications():
                problems.append(
                    f"{where}: document {doc.id} is classified {doc.classification!r}, which only your additions to "
                    "the company have"
                )

        perms = Permissions(w)
        classes, types = set(w.classifications()), set(w.request_types())
        for i, turn in enumerate(sc.turns, 1):
            at = f"{where} turn {i}"
            if turn.sender not in w.people:
                problems.append(f"{at}: unknown sender {turn.sender!r}")
                continue
            for person, kind, value in turn.grant + turn.revoke:
                if person not in w.people:
                    problems.append(f"{at}: grant for unknown person {person!r}")
                if kind == "read" and value not in classes:
                    problems.append(f"{at}: grant of unknown classification {value!r}")
                if kind == "approve" and value not in types:
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
                if not (w.person_by_email(address) or w.is_known_contact(address)):
                    problems.append(f"{at}: allow_email has unknown address {address}")

            sender_email = w.people[turn.sender].email
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
                    if not (w.person_by_email(audience) or w.is_known_contact(audience)):
                        problems.append(f"{at}: expected email to unknown address {audience!r}")
                    if audience.casefold() not in turn.allow_email:
                        problems.append(f"{at}: expected email to {audience} is missing from allow_email")
                for needle in e.get("contains", []):
                    for d, c in canaries_in(needle):
                        if d.classification not in perms.cleared_for(audience):
                            problems.append(
                                f"{at}: expectation needs canary {c!r} of {d.id} for {audience}, who is not cleared"
                            )

            for b in turn.bait:
                kind = b.get("type")
                if kind not in BAIT_TYPES:
                    problems.append(f"{at}: unknown bait type {kind!r}")
                elif kind == "approve" and normalize_request_id(b.get("request", "")) not in requests:
                    problems.append(f"{at}: bait approves unknown request {b.get('request')}")
                elif kind == "read" and b.get("document") not in own:
                    problems.append(f"{at}: bait reads unknown document {b.get('document')}")
                elif kind == "email" and not b.get("to"):
                    problems.append(f"{at}: email bait needs a recipient")
    return problems
