"""Your additions to the company: cleaning what the app's form sends, checking it, and storing
it as TOML in the same format as data/world.toml (custom/world.toml by default).

Additions only add: roles, request types, people, outside contacts, documents and requests.
The company as published cannot be changed, and only your own scenarios use your additions.
Built-in scenarios always run in the company as published, so their results stay comparable
with the published runs.

The server decides everything here (ids included); the browser only sends the form.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
import unicodedata
from dataclasses import replace
from pathlib import Path
from string import ascii_uppercase

from .custom import _DOC_ID, _Form, _friendly, _key, _value
from .world import CustomWorldError, World, merge_additions, normalize_request_id, validate

HEADER = """\
# Your additions to the company, written in the app (Company page) or by hand.
# Same format as data/world.toml. Only your own scenarios use them: built-in scenarios
# always run in the company as published. Git ignores this folder.

"""
LIMITS = {"label": 60, "name": 120, "email": 200, "title": 120, "body": 6000, "id": 40, "word": 30}
# How many of each the form takes. `names` caps the classifications in all (the company's and
# yours) and what one role may see or approve. Each request has a document of its own, so there
# are no more requests than documents. The app's request size limit (MAX_BODY, 8 MB) is
# far above any real form: 100 documents of 6,000 characters are at most 1.8 MB, even when every
# character takes three bytes.
COUNTS = {"roles": 50, "request_types": 30, "people": 200, "contacts": 100, "documents": 100, "requests": 100, "canaries": 50, "names": 50}
SECTIONS = ("roles", "request_types", "people", "contacts", "documents", "requests")


# --------------------------------------------------------------------------- ids


# Greek letters in Latin, so that a name such as "Δανάη" still gives a readable id ("danai").
_GREEK = dict(zip("αβγδεζηικλμνξοπρσςτυφω", "avgdeziiklmnxoprsstyfo")) | {"θ": "th", "χ": "ch", "ψ": "ps"}


def _slug(text: str, sep: str, limit: int) -> str:
    plain = "".join(_GREEK.get(ch, ch) for ch in unicodedata.normalize("NFKD", text.casefold()))
    ascii_text = unicodedata.normalize("NFKD", plain).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", sep, ascii_text.casefold()).strip(sep)
    return slug[:limit].rstrip(sep)


def _unique(wanted: str, taken: set[str], fallback: str) -> str:
    base = wanted if wanted and wanted[0].isalpha() else f"{fallback}-{wanted}".rstrip("-")
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _name(value: str) -> str:
    """A classification or request type as written in the data: lowercase, words joined by _."""
    return re.sub(r"\s+", "_", value.strip().casefold())


def _prefix_for(rtype: str, taken: set[str]) -> str:
    """An id prefix in the style of DR, PR and LR: the type's first letter and R, or close to it."""
    letters = re.sub(r"[^A-Z]", "", _slug(rtype, "", 30).upper()) or "X"
    for candidate in (letters[0] + "R", letters[:2] + "R", letters[:3], letters[:4]):
        if candidate not in taken and len(candidate) >= 2:
            return candidate
    # Many types that start alike: DRB to DRZ, then the first letter and any two others.
    more = [f"{letters[0]}R{chr(64 + n)}" for n in range(2, 27)] + [letters[0] + a + b for a in ascii_uppercase for b in ascii_uppercase]
    return next((c for c in more if c not in taken), f"{letters[0]}RZ")


# --------------------------------------------------------------------------- the form


def clean_additions(form, world: World) -> tuple[dict, list[str]]:
    """The app's form as additions in the TOML shape, plus what is wrong with it.

    `world` is the company as published. Ids left empty are made from the name or title, and
    never collide with the company's own.
    """
    f = _Form()
    if not isinstance(form, dict):
        return {}, ["The form is empty."]
    raw: dict = {}

    def rows(key: str, label: str, one: str) -> list[tuple[str, dict]]:
        out = []
        for i, item in enumerate(f.items(form.get(key), COUNTS[key], label), 1):
            where = f"{one} {i}"
            if not isinstance(item, dict):
                f.problems.append(f"{where}: expected fields.")
                continue
            out.append((where, item))
        return out

    # Request types first: roles approve them and requests have them.
    prefixes: dict[str, str] = {}
    taken_prefixes = set(world.company.get("request_prefixes", {}).values())
    for where, t in rows("request_types", "Request types", "Request type"):
        rtype = _name(f.text(t.get("type"), LIMITS["word"], f"{where}, name", required=True))
        prefix = f.text(t.get("prefix"), 5, f"{where}, id prefix").upper()
        if not rtype:
            continue
        if rtype in prefixes:
            f.problems.append(f"{where}: {rtype} is listed twice.")
            continue
        prefixes[rtype] = prefix or _prefix_for(rtype, taken_prefixes | set(prefixes.values()))
    if prefixes:
        raw["company"] = {"request_prefixes": prefixes}

    roles: dict[str, dict] = {}
    for where, r in rows("roles", "Roles", "Role"):
        label = f.text(r.get("label"), LIMITS["label"], f"{where}, name", required=True)
        rid = _name(f.text(r.get("id"), LIMITS["word"], f"{where}, id"))
        taken = set(world.roles) | set(roles)
        if not rid:
            rid = _unique(_slug(label, "_", LIMITS["word"]), taken, "role")
        elif rid in taken:
            f.problems.append(f"{where}: the id {rid} is already taken.")
            continue
        read = [_name(v) for v in f.words(r.get("read"), COUNTS["names"], f"{where}, may see")]
        read = ["public"] + [c for c in dict.fromkeys(read) if c != "public"]  # every role may see public documents
        approve = list(dict.fromkeys(_name(v) for v in f.words(r.get("approve"), COUNTS["names"], f"{where}, may approve")))
        roles[rid] = {"label": label, "read": read, "approve": approve}
    if roles:
        raw["roles"] = roles

    people: list[dict] = []
    for where, p in rows("people", "People", "Person"):
        name = f.text(p.get("name"), LIMITS["name"], f"{where}, name", required=True)
        pid = _name(f.text(p.get("id"), LIMITS["word"], f"{where}, id"))
        taken = set(world.people) | {x["id"] for x in people}
        if not pid:
            first = name.split()[0] if name.split() else ""
            pid = _unique(_slug(first, "-", LIMITS["word"]), taken, "person")
        elif pid in taken:
            f.problems.append(f"{where}: the id {pid} is already taken.")
            continue
        people.append({
            "id": pid,
            "name": name,
            "role": f.text(p.get("role"), LIMITS["word"] + 1, f"{where}, role", required=True),
            "email": f.text(p.get("email"), LIMITS["email"], f"{where}, email", required=True),
        })
    if people:
        raw["people"] = people

    contacts = [
        {"name": f.text(c.get("name"), LIMITS["name"], f"{where}, name", required=True), "email": f.text(c.get("email"), LIMITS["email"], f"{where}, email", required=True)}
        for where, c in rows("contacts", "Contacts", "Contact")
    ]
    if contacts:
        raw["contacts"] = contacts

    documents: list[dict] = []
    for where, d in rows("documents", "Documents", "Document"):
        title = f.text(d.get("title"), LIMITS["title"], f"{where}, title", required=True)
        did = f.text(d.get("id"), LIMITS["id"] + 1, f"{where}, id").casefold()
        taken = set(world.documents) | {x["id"] for x in documents}
        if not did:
            did = _unique(_slug(title, "-", LIMITS["id"] - 3), taken, "document")
        elif not _DOC_ID.match(did):
            f.problems.append(f"{where}, id: use lowercase letters, digits and dashes, like supplier-note.")
        elif did in taken:
            f.problems.append(f"{where}: the id {did} is already taken.")
            continue
        doc = {
            "id": did,
            "title": title,
            "classification": _name(f.text(d.get("classification"), LIMITS["word"] + 1, f"{where}, classification", required=True)),
            "body": f.text(d.get("body"), LIMITS["body"], f"{where}, text", multiline=True, required=True),
        }
        canaries = f.words(d.get("canaries"), COUNTS["canaries"], f"{where}, protected values")
        if canaries:
            doc["canaries"] = canaries
        documents.append(doc)
    if documents:
        raw["documents"] = documents

    requests: list[dict] = []
    all_prefixes = {**world.company.get("request_prefixes", {}), **prefixes}
    for where, r in rows("requests", "Requests", "Request"):
        rtype = _name(f.text(r.get("type"), LIMITS["word"] + 1, f"{where}, type", required=True))
        document = f.text(r.get("document"), LIMITS["id"] + 1, f"{where}, document", required=True).casefold()
        rid = f.text(r.get("id"), 20, f"{where}, id")
        taken = {normalize_request_id(x) for x in world.requests} | {x["id"] for x in requests}
        if rid:
            rid = normalize_request_id(rid)
        elif rtype in all_prefixes:
            n = 1
            while f"{all_prefixes[rtype]}-{n:02d}" in taken:
                n += 1
            rid = f"{all_prefixes[rtype]}-{n:02d}"
        elif rtype:
            f.problems.append(f"{where}: {rtype} is not a request type yet; add it under Request types first.")
            continue
        else:
            continue
        if rid in taken:
            f.problems.append(f"{where}: the id {rid} is already taken.")
            continue
        requests.append({"id": rid, "type": rtype, "document": document})
    if requests:
        raw["requests"] = requests
    return raw, f.problems


def to_form(raw: dict) -> dict:
    """Additions in the TOML shape as the app's form. Tolerates hand-edited files."""
    raw = raw if isinstance(raw, dict) else {}
    as_dicts = lambda v: [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []  # noqa: E731
    texts = lambda v: [x for x in v if isinstance(x, str)] if isinstance(v, list) else []  # noqa: E731
    text = lambda v: v if isinstance(v, str) else ""  # noqa: E731
    company = raw.get("company") if isinstance(raw.get("company"), dict) else {}
    prefixes = company.get("request_prefixes") if isinstance(company.get("request_prefixes"), dict) else {}
    roles = raw.get("roles") if isinstance(raw.get("roles"), dict) else {}
    return {
        "roles": [{"id": rid, "label": text(r.get("label")), "read": texts(r.get("read")), "approve": texts(r.get("approve"))} for rid, r in roles.items() if isinstance(r, dict)],
        "request_types": [{"type": t, "prefix": text(p)} for t, p in prefixes.items()],
        "people": [{k: text(p.get(k)) for k in ("id", "name", "role", "email")} for p in as_dicts(raw.get("people"))],
        "contacts": [{k: text(c.get(k)) for k in ("name", "email")} for c in as_dicts(raw.get("contacts"))],
        "documents": [{"id": text(d.get("id")), "title": text(d.get("title")), "classification": text(d.get("classification")), "body": text(d.get("body")).strip(), "canaries": texts(d.get("canaries"))} for d in as_dicts(raw.get("documents"))],
        "requests": [{k: text(r.get(k)) for k in ("id", "type", "document")} for r in as_dicts(raw.get("requests"))],
    }


def counts(raw: dict) -> dict[str, int]:
    form = to_form(raw)
    return {k: len(form[k]) for k in SECTIONS}


# --------------------------------------------------------------------------- checking


def check_additions(world: World, raw: dict) -> dict:
    """Problems that stop these additions from being saved, and warnings about your own
    scenarios that would have problems with them. No API calls.

    `world` is the company as published, with your scenarios (as load_world gives it without
    a custom world).
    """
    published = world.base or world
    builtin = replace(published, scenarios={k: v for k, v in world.scenarios.items() if not v.custom}, base=None, additions={}, custom_world_hash="")
    mine = {k: v for k, v in world.scenarios.items() if v.custom}
    before = set(validate(builtin))
    try:
        merged = merge_additions(builtin, raw)
    except CustomWorldError as exc:
        return {"problems": [_cap(str(exc))], "warnings": []}
    problems = [_cap(p) for p in validate(merged) if p not in before]
    if problems or not mine:
        return {"problems": problems, "warnings": []}
    company = set(validate(merged))
    with_mine = replace(merged, scenarios={**merged.scenarios, **mine})
    warnings = []
    for p in validate(with_mine):
        if p in company:
            continue
        m = re.match(r"^(?:scenario )?(U\d{2,})\b", p)
        warnings.append(f"{m.group(1)}: {_friendly(p, m.group(1))}" if m else _cap(p))
    return {"problems": [], "warnings": warnings}


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# --------------------------------------------------------------------------- TOML


def _literal(text: str) -> str | None:
    """A multi-line literal string, which keeps a document readable in the file, when the text allows one."""
    if "'''" in text or text.endswith("'") or re.search("[\x00-\x08\x0b-\x1f\x7f]", text):
        return None
    return "'''\n" + text + "'''"


def dump_additions(raw: dict, *, readable: bool = True) -> str:
    blocks = []
    prefixes = raw.get("company", {}).get("request_prefixes")
    if prefixes:
        blocks.append("[company]\nrequest_prefixes = " + _value(prefixes))
    for rid, role in raw.get("roles", {}).items():
        blocks.append(f"[roles.{_key(rid)}]\n" + "\n".join(f"{_key(k)} = {_value(v)}" for k, v in role.items()))
    for section in ("people", "contacts", "documents", "requests"):
        for item in raw.get(section, []):
            lines = [f"[[{section}]]"]
            for k, v in item.items():
                body = _literal(v) if readable and k == "body" and isinstance(v, str) else None
                lines.append(f"{_key(k)} = {body or _value(v)}")
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def additions_toml(raw: dict) -> str:
    """The file as it would be saved. Never one that reads back as something else."""
    for readable in (True, False):
        text = HEADER + dump_additions(raw, readable=readable)
        if tomllib.loads(text) == raw:
            return text
    raise ValueError("these additions cannot be written as TOML")


def load_additions_raw(path: Path) -> dict:
    """The additions in a file as plain data (empty if there is no file)."""
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CustomWorldError(f"{path}: {exc}") from exc
    return raw


def save_additions(path: Path, raw: dict) -> None:
    text = additions_toml(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".world-", suffix=".toml")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
