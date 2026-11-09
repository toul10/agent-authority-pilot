"""A local web app: quick start, settings, your own scenarios, and results.

    python -m authz_pilot app        # opens http://127.0.0.1:8765/

The server listens on 127.0.0.1 only, so other computers cannot reach it.
Other web pages cannot use it either:

- every API call needs a token that this app's page receives when it loads;
- the Host header must name this server, against DNS rebinding;
- the pages may not be framed by other sites, against clickjacking;
- the API key never goes back to the browser, only a masked form of it;
- a run that calls Claude starts only when the request carries
  `confirm_spend: true`, which the page sends after you confirm the cost.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import threading
import time
import tomllib
import traceback
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .additions import COUNTS as ADD_COUNTS, LIMITS as ADD_LIMITS, additions_toml, check_additions, clean_additions, load_additions_raw, save_additions, to_form
from .custom import COUNTS, LIMITS, check_scenario, clean_form, dump, load_raw, next_id, remove, upsert
from .dashboard import SHORT, build_payload, outcome, render_dashboard
from .models import EFFORTS, MODEL_INFO, model_notes, provider_of
from .providers import BASE_ENV, check_models
from .runner import CONDITIONS, episode_path, is_complete, read_events
from .runs import RunError, estimate_cost, execute, jobs_of, list_runs, load_run, plan_run, providers_needed, results_dir
from .scoring import score_episode
from .settings import LIMITS as SETTING_LIMITS
from .tools import ACTION_NOTES, REPLY_NOTE, SIDE_EFFECT_TOOLS, TOOLS
from .settings import PROVIDERS, describe_source, env_key, key_path, load_settings, mask, parse_settings, remove_key, resolve_key, save_key, save_settings, settings_path
from .world import BAIT_TYPES, CUSTOM_ID, DEFAULT_DATA_DIR, EXPECT_TYPES, TRAP_TYPES, CustomScenarioError, CustomWorldError, World, custom_path, custom_world_path, load_world

WEB = Path(__file__).parent / "web"
STATIC = {
    "app.js": "text/javascript; charset=utf-8",
    "theme.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "report.js": "text/javascript; charset=utf-8",
    "report.css": "text/css; charset=utf-8",
    "fonts/commissioner.woff2": "font/woff2",
}
MAX_BODY = 8 * 1024 * 1024  # far above any real form (see COUNTS in additions.py)
RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")
QUICK_START = ["L01", "T05"]  # one legit request and one trap, one message each
API_NAMES = {"anthropic": "Claude API", "openai": "OpenAI API", "google": "Gemini API"}
FAMILIES = {"anthropic": "Claude", "openai": "GPT", "google": "Gemini"}  # what the run button says it runs

APP_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
API_CSP = "default-src 'none'; frame-ancestors 'none'"


def script_hashes(html: str) -> list[str]:
    """CSP hashes of the inline scripts that run (not the JSON data block)."""
    found = re.findall(r"<script>(.*?)</script>", html, re.S)
    return ["'sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode() + "'" for s in found]


def dashboard_csp(html: str) -> str:
    # The run's dashboard is shown inside the app, so only this server may frame it.
    return f"default-src 'none'; script-src {' '.join(script_hashes(html))}; style-src 'unsafe-inline'; img-src data:; font-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def resume_command(plan) -> str:
    """The command that finishes an interrupted run from the command line."""
    c = plan.config
    parts = ["python -m authz_pilot run", "--out", shlex.quote(str(plan.out)), "--scenarios", ",".join(c["scenarios"]), "--conditions", ",".join(c["conditions"]), "--repeats", str(c["repeats"]), "--max-steps", str(c["max_steps"])]
    if c.get("dry_run"):
        parts.append("--dry-run")
    else:
        parts += ["--model", c["agent"], "--effort", c["effort"]]
        if c.get("monitor"):
            parts += ["--monitor-model", c["monitor"], "--monitor-effort", c["monitor_effort"]]
    return " ".join(parts)


def _key(job: tuple[str, str, int]) -> str:
    return f"{job[0]}_{job[1]}_{job[2]}"


def _strings(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ApiError(400, f"{where}: expected a list of strings.")
    return value


class LiveRun:
    """A run this app started: its plan, live episodes and progress."""

    def __init__(self, plan, dry_run: bool):
        self.plan = plan
        self.name = plan.out.name
        self.dry_run = dry_run
        self.status = "running"  # then done, stopped or failed
        self.error = ""
        self.log: list[str] = []
        self.live: dict[str, object] = {}
        self.stop = threading.Event()
        self.result: dict = {}
        self.started = time.time()

    def observe(self, job, episode) -> None:
        self.live[_key(job)] = episode

    def progress(self, line: str) -> None:
        self.log.append(line)
        del self.log[:-100]


class App:
    def __init__(self, *, results: Path | None = None, data_dir: Path = DEFAULT_DATA_DIR, custom: Path | None = None, custom_world: Path | None = None):
        self.results = Path(results or results_dir())
        self.data_dir = Path(data_dir)
        self.custom = Path(custom or custom_path())
        self.custom_world = Path(custom_world or custom_world_path())
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self.lock = threading.Lock()  # starting runs
        self.custom_lock = threading.Lock()  # writing your scenarios or your additions, and copying them into a run
        self.current: LiveRun | None = None
        self.rows: dict[tuple, dict] = {}  # scored episode rows, by (path, mtime, size)

    def bind(self, port: int) -> None:
        self.port = port
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    # ------------------------------------------------------------------ worlds

    def company_world(self) -> World:
        """The company as your own scenarios see it: as published, plus your additions. If your
        additions cannot be read, the company as published, with the reason in additions_error."""
        return load_world(self.data_dir, custom_world=self.custom_world, strict=False)

    def published_with_mine(self) -> World:
        """The company as published, with your scenarios when their file can be read."""
        try:
            return load_world(self.data_dir, custom=self.custom)
        except CustomScenarioError:
            return load_world(self.data_dir)

    def full_world(self) -> World:
        try:
            return load_world(self.data_dir, custom=self.custom, custom_world=self.custom_world, strict=False)
        except CustomScenarioError as exc:
            raise ApiError(400, f"Your scenarios file cannot be read, so fix it by hand first: {exc}") from exc

    # ------------------------------------------------------------------ routes

    ROUTES = [
        ("GET", r"/api/status", "status"),
        ("GET", r"/api/world", "world_info"),
        ("GET", r"/api/scenarios", "scenarios"),
        ("POST", r"/api/world/check", "check_world"),
        ("POST", r"/api/world", "save_world"),
        ("POST", r"/api/scenarios/check", "check"),
        ("POST", r"/api/scenarios", "save"),
        ("DELETE", r"/api/scenarios/([^/]+)", "delete_scenario"),
        ("POST", r"/api/key", "set_key"),
        ("DELETE", r"/api/key(?:/(anthropic|openai|google))?", "delete_key"),
        ("POST", r"/api/key/test", "test_key"),
        ("POST", r"/api/settings", "set_settings"),
        ("POST", r"/api/estimate", "estimate"),
        ("GET", r"/api/runs", "runs"),
        ("POST", r"/api/runs", "start_run"),
        ("GET", r"/api/runs/([^/]+)", "run_view"),
        ("POST", r"/api/runs/([^/]+)/stop", "stop_run"),
        ("GET", r"/api/runs/([^/]+)/report", "run_report"),
    ]

    def route(self, method: str, path: str, body: dict):
        for m, pattern, name in self.ROUTES:
            match = re.fullmatch(pattern, path)
            if match and m == method:
                return getattr(self, name)(body, *match.groups())
        raise ApiError(404, "Unknown API call.")

    # ------------------------------------------------------------------ status, key, settings

    def key_status(self, provider: str = "anthropic") -> dict:
        key, source = resolve_key(provider)
        token = provider == "anthropic" and bool(os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        return {
            "present": bool(key) or token,
            "source": source,
            "where": describe_source(source, provider),
            "masked": mask(key),
            "saved": key_path(provider).is_file(),
            "saved_file": str(key_path(provider)),
            "env": env_key(provider)[1] or PROVIDERS[provider].env[0],
        }

    def keys_needed(self, settings, conditions: list[str]) -> list[dict]:
        """The keys a real run with these settings uses, one per provider, for the confirmation."""
        out = []
        for provider, roles in providers_needed(settings, conditions).items():
            k = self.key_status(provider)
            out.append({"provider": provider, "name": PROVIDERS[provider].name, "api": API_NAMES[provider], "roles": roles, "present": k["present"], "masked": k["masked"]})
        return out

    def status(self, body: dict) -> dict:
        current = self.current
        return {
            "version": __version__,
            "key": self.key_status(),
            "keys": {p: self.key_status(p) for p in PROVIDERS},
            "providers": {p: {"name": v.name, "console": v.console, "api": API_NAMES[p], "family": FAMILIES[p]} for p, v in PROVIDERS.items()},
            "api_base": os.environ.get("ANTHROPIC_BASE_URL") or None,
            "api_bases": {p: os.environ[env] for p, env in BASE_ENV.items() if os.environ.get(env)},
            "settings": asdict(load_settings()),
            "limits": {k: list(v) for k, v in SETTING_LIMITS.items()},
            "models": [{"id": m, "name": i.name, "price": list(i.price_on()), "provider": i.provider, "group": i.group, "efforts": list(i.effort_levels), "notes": {r: model_notes(i, r) for r in ("assistant", "monitor")}} for m, i in MODEL_INFO.items()],
            "efforts": list(EFFORTS),
            "paths": {"settings": str(settings_path()), "key": str(key_path()), "keys": {p: str(key_path(p)) for p in PROVIDERS}, "custom": str(self.custom), "custom_world": str(self.custom_world), "results": str(self.results)},
            "run": {"name": current.name, "status": current.status} if current else None,
            "quick_start": QUICK_START,
        }

    def set_key(self, body: dict) -> dict:
        key, provider = body.get("key"), body.get("provider", "anthropic")
        if provider not in PROVIDERS:
            raise ApiError(400, "Unknown provider.")
        if not isinstance(key, str):
            raise ApiError(400, "Paste a key first.")
        try:
            save_key(key, provider)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        return {"key": self.key_status(provider)}

    def delete_key(self, body: dict, provider: str | None = None) -> dict:
        provider = provider or "anthropic"
        removed = remove_key(provider)
        return {"removed": removed, "key": self.key_status(provider)}

    def test_key(self, body: dict) -> dict:
        s = load_settings()
        roles = {"assistant": (s.model, s.effort), "monitor": (s.monitor_model, s.monitor_effort)}
        providers = list(dict.fromkeys(provider_of(m) for m, _ in roles.values()))
        if not any(self.key_status(p)["present"] for p in providers):
            raise ApiError(400, "No API key yet. Paste one first.")
        result = check_models(roles, {p: resolve_key(p)[0] for p in providers})
        return result | {"key": self.key_status(), "keys": {p: self.key_status(p) for p in PROVIDERS}}

    def set_settings(self, body: dict) -> dict:
        settings, problems = parse_settings(body, load_settings())
        if problems:
            raise ApiError(400, "Some settings are not valid.", fields=problems)
        save_settings(settings)
        return {"settings": asdict(settings)}

    # ------------------------------------------------------------------ scenarios

    def world_info(self, body: dict) -> dict:
        """The company as your own scenarios see it, each part marked `yours` if it is one of your
        additions, and your additions again as the Company page's form edits them."""
        w = self.company_world()
        try:
            raw, raw_error = load_additions_raw(self.custom_world), ""
        except CustomWorldError as exc:
            raw, raw_error = {}, str(exc)
        actions = [{"name": t["name"], "does": ACTION_NOTES[t["name"]][0], "check": ACTION_NOTES[t["name"]][1], "reviewed": t["name"] in SIDE_EFFECT_TOOLS} for t in TOOLS]
        actions.append({"name": "reply", "does": REPLY_NOTE[0], "check": REPLY_NOTE[1], "reviewed": True})
        return {
            "company": w.company.get("name", ""),
            "company_description": w.company.get("description", ""),
            "actions": actions,
            "roles": {k: {"label": r["label"], "read": r["read"], "approve": r["approve"], "yours": w.is_yours("roles", k)} for k, r in w.roles.items()},
            "people": [{"id": p.id, "name": p.name, "role": p.role, "role_label": w.role_label(p), "email": p.email, "yours": w.is_yours("people", p.id)} for p in w.people.values()],
            "contacts": [c | {"yours": w.is_yours("contacts", k)} for k, c in w.contacts.items()],
            "documents": [{"id": d.id, "title": d.title, "classification": d.classification, "canaries": list(d.canaries), "body": d.body, "yours": w.is_yours("documents", d.id)} for d in w.documents.values()],
            "requests": [{"id": r.id, "type": r.type, "document": r.document, "yours": w.is_yours("requests", r.id)} for r in w.requests.values()],
            "classifications": w.classifications(),
            "request_types": w.request_types(),
            "request_prefixes": dict(w.company.get("request_prefixes", {})),
            "additions": to_form(raw),
            "additions_error": raw_error or w.additions_error,
            "additions_readable": not raw_error,  # if not, the page will not save over the file
            "additions_path": str(self.custom_world),
            "additions_limits": {"limits": ADD_LIMITS, "counts": ADD_COUNTS},
            "trap_types": list(TRAP_TYPES),
            "expect_types": list(EXPECT_TYPES),
            "bait_types": list(BAIT_TYPES),
            "conditions": CONDITIONS,
            "short": SHORT,
            "limits": LIMITS,
            "counts": COUNTS,
        }

    def scenarios(self, body: dict) -> dict:
        builtin = tomllib.loads((self.data_dir / "scenarios.toml").read_text(encoding="utf-8"))["scenario"]
        try:
            custom, error = load_raw(self.custom), None
        except CustomScenarioError as exc:
            custom, error = [], f"Your scenarios file cannot be read, so fix it by hand first: {exc}"
        return {"builtin": builtin, "custom": custom, "error": error}

    def _existing_id(self, raws: list[dict], sid) -> str:
        if not (isinstance(sid, str) and CUSTOM_ID.match(sid) and any(r.get("id") == sid for r in raws)):
            raise ApiError(404, f"There is no scenario of yours with the id {sid!r}.")
        return sid

    # ------------------------------------------------------------------ your additions to the company

    def _additions_toml(self, raw: dict) -> str:
        try:
            return additions_toml(raw)
        except ValueError:
            return ""

    def check_world(self, body: dict) -> dict:
        world = self.published_with_mine()
        raw, problems = clean_additions(body.get("form"), world)
        result = {"problems": problems, "warnings": []} if problems else check_additions(world, raw)
        return result | {"toml": self._additions_toml(raw), "form": to_form(raw)}

    def save_world(self, body: dict) -> dict:
        with self.custom_lock:
            try:
                load_additions_raw(self.custom_world)
            except CustomWorldError as exc:  # never save over what someone wrote by hand
                raise ApiError(400, f"Your additions file cannot be read, so fix it by hand first: {exc}") from exc
            world = self.published_with_mine()
            raw, problems = clean_additions(body.get("form"), world)
            if problems:
                raise ApiError(400, "Fix these first.", problems=problems)
            result = check_additions(world, raw)
            if result["problems"]:
                raise ApiError(400, "Fix these first.", problems=result["problems"])
            save_additions(self.custom_world, raw)
        return result | {"toml": additions_toml(raw), "additions": to_form(raw)}

    # ------------------------------------------------------------------ your scenarios

    def check(self, body: dict) -> dict:
        world = self.company_world()
        try:
            raws = load_raw(self.custom)
        except CustomScenarioError:
            raws = []
        sid = body.get("id")
        sid = sid if isinstance(sid, str) and CUSTOM_ID.match(sid) else next_id(raws, world)
        raw, problems = clean_form(body.get("form"), sid)
        if problems:
            return {"id": sid, "problems": problems, "warnings": [], "coverage": None, "toml": dump([raw]) if raw else ""}
        return {"id": sid, **check_scenario(world, raw), "toml": dump([raw])}

    def save(self, body: dict) -> dict:
        world = self.company_world()
        with self.custom_lock:
            try:
                raws = load_raw(self.custom)
            except CustomScenarioError as exc:
                raise ApiError(400, f"Your scenarios file cannot be read, so fix it by hand first: {exc}") from exc
            sid = self._existing_id(raws, body["id"]) if body.get("id") is not None else next_id(raws, world)
            raw, problems = clean_form(body.get("form"), sid)
            if problems:
                raise ApiError(400, "Fix these first.", problems=problems)
            result = check_scenario(world, raw)
            if result["problems"]:
                raise ApiError(400, "Fix these first.", problems=result["problems"])
            upsert(self.custom, raw)
        return {"id": sid, **result, "toml": dump([raw]), "scenario": raw}

    def delete_scenario(self, body: dict, sid: str) -> dict:
        with self.custom_lock:
            try:
                raws = load_raw(self.custom)
            except CustomScenarioError as exc:
                raise ApiError(400, f"Your scenarios file cannot be read, so fix it by hand first: {exc}") from exc
            remove(self.custom, self._existing_id(raws, sid))
        return {"deleted": sid}

    # ------------------------------------------------------------------ runs

    def _run_inputs(self, body: dict, world: World):
        ids = _strings(body.get("scenarios"), "scenarios")
        conditions = _strings(body.get("conditions"), "conditions")
        if not ids:
            raise ApiError(400, "Choose at least one scenario.")
        unknown = [s for s in ids if s not in world.scenarios]
        if unknown:
            raise ApiError(400, f"Unknown scenario ids: {', '.join(unknown)}")
        if not conditions or any(c not in CONDITIONS for c in conditions):
            raise ApiError(400, "Choose at least one of the conditions A, B and C.")
        conditions = [c for c in CONDITIONS if c in conditions]  # A, B, C order, no repeats
        repeats = body.get("repeats", 1)
        if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 10:
            raise ApiError(400, "Repeats is a whole number from 1 to 10.")
        overrides = {k: body[k] for k in ("model", "effort", "monitor_model", "monitor_effort") if body.get(k) is not None}
        settings, problems = parse_settings(overrides, load_settings())
        if problems:
            raise ApiError(400, "Some model choices are not valid.", fields=problems)
        return list(dict.fromkeys(ids)), conditions, repeats, settings

    def estimate(self, body: dict) -> dict:
        world = self.full_world()
        ids, conditions, repeats, settings = self._run_inputs(body, world)
        return estimate_cost(world, ids, conditions, settings.model, settings.monitor_model, repeats) | {"max_cost": settings.max_cost, "keys": self.keys_needed(settings, conditions)}

    def start_run(self, body: dict) -> dict:
        world = self.full_world()
        ids, conditions, repeats, settings = self._run_inputs(body, world)
        mode = body.get("mode")
        if mode not in ("dry", "claude"):  # "claude": any real model, whichever its provider
            raise ApiError(400, "Choose the scripted assistant (free) or a real model.")
        keys = None
        if mode == "claude":
            if body.get("confirm_spend") is not True:
                raise ApiError(400, "A run with a real model costs money, so it needs your confirmation first.")
            keys = {}
            for k in self.keys_needed(settings, conditions):
                if not k["present"]:
                    roles = " and ".join(k["roles"])
                    raise ApiError(400, f"Add your {k['name']} API key first, in Settings: the {roles} model{'s run' if len(k['roles']) > 1 else ' runs'} on the {k['api']}.", missing_key=k["provider"])
                keys[k["provider"]] = resolve_key(k["provider"])[0]
        label = "-".join(ids) if len(ids) <= 3 else f"{len(ids)}-scenarios"
        with self.lock:
            if self.current and self.current.status == "running":
                raise ApiError(409, f"Run {self.current.name} is still going. Wait for it, or stop it.", current=self.current.name)
            try:
                with self.custom_lock:  # the run copies your scenarios and additions as they are now
                    plan = plan_run(scenario_ids=ids, conditions=conditions, repeats=repeats, settings=settings, dry_run=mode == "dry", data_dir=self.data_dir, custom=self.custom, custom_world=self.custom_world, api_keys=keys, label=label, results=self.results)
            except RunError as exc:
                raise ApiError(400, str(exc)) from None
            run = LiveRun(plan, dry_run=mode == "dry")
            self.current = run
            threading.Thread(target=self._execute, args=(run,), name=f"run-{run.name}", daemon=True).start()
        return {"name": run.name}

    def _execute(self, run: LiveRun) -> None:
        try:
            state = execute(run.plan, progress=run.progress, observe=run.observe, stop=run.stop)
            run.result = {k: state[k] for k in ("ran", "skipped", "over_budget", "cancelled", "spent")}
            run.status = "stopped" if run.stop.is_set() else "done"
        except Exception as exc:  # shown in the page; details in the terminal
            traceback.print_exc()
            run.error = f"{type(exc).__name__}: {exc}"
            run.status = "failed"

    def stop_run(self, body: dict, name: str) -> dict:
        run = self.current
        if not run or run.name != name:
            raise ApiError(404, "That run is not going.")
        if run.status == "running":
            run.stop.set()
        return {"name": name, "status": run.status, "stopping": run.stop.is_set()}

    def runs(self, body: dict) -> dict:
        current = self.current
        return {"runs": list_runs(self.results), "current": {"name": current.name, "status": current.status} if current else None}

    def run_folder(self, name: str) -> Path:
        folder = self.results / name
        if not RUN_NAME.match(name) or not (folder / "config.json").is_file() or folder.resolve().parent != self.results.resolve():
            raise ApiError(404, "There is no run with that name.")
        return folder

    def _row(self, folder: Path, world: World, job: tuple[str, str, int], live: dict) -> dict:
        sid, condition, repeat = job
        key = _key(job)
        row = {"key": key, "scenario": sid, "condition": condition, "repeat": repeat}
        path = episode_path(folder, sid, condition, repeat)
        if is_complete(path):
            stat = path.stat()
            cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
            if cache_key not in self.rows:
                score = score_episode(read_events(path), world)
                self.rows[cache_key] = {
                    "state": "done",
                    "outcome": outcome(score),
                    "kind": score.kind,
                    "completed": score.completed,
                    "violations": sorted({v["type"] for v in score.violations}),
                    "attempted": len(score.attempted),
                    "stopped_by": sorted({a["blocked_by"] for a in score.attempted if a.get("blocked_by")}),
                    "false_blocks": len(score.false_blocks),
                    "stopped": score.stopped,
                    "cost": round(score.cost, 5),
                }
            return row | self.rows[cache_key]
        episode = live.get(key)
        if episode is not None:
            events = list(episode.events)
            turns = [e for e in events if e["event"] == "turn_start"]
            actions = [e for e in events if e["event"] == "tool_call"]
            return row | {"state": "running", "message": len(turns), "messages": len(world.scenarios[sid].turns), "actions": len(actions)}
        return row | {"state": "waiting"}

    def run_view(self, body: dict, name: str) -> dict:
        folder = self.run_folder(name)
        run = self.current if self.current and self.current.name == name and self.current.plan.out.resolve() == folder.resolve() else None
        if run:
            config, world = run.plan.config, run.plan.world
        else:
            config, world, _ = load_run(folder)
        rows = [self._row(folder, world, job, run.live if run else {}) for job in jobs_of(config)]
        done = sum(r["state"] == "done" for r in rows)
        status = run.status if run else ("finished" if done == len(rows) else "incomplete")
        return {
            "name": name,
            "status": status,
            "error": run.error if run else "",
            "stopping": bool(run and run.stop.is_set() and run.status == "running"),
            "config": {k: v for k, v in config.items() if k != "data_dir"},
            "conditions": {c: CONDITIONS[c] for c in config.get("conditions", [])},
            "scenarios": {s: {"title": world.scenarios[s].title, "kind": world.scenarios[s].kind, "trap": world.scenarios[s].trap, "custom": world.scenarios[s].custom} for s in config.get("scenarios", []) if s in world.scenarios},
            "rows": rows,
            "done": done,
            "spent": round(sum(r.get("cost", 0) for r in rows), 5),
            "max_cost": run.plan.max_cost if run else None,
            "result": run.result if run else {},
            "log": run.log[-10:] if run else [],
        }

    def payload(self, name: str) -> dict:
        """What the run page shows: every finished episode, scored, with its conversation."""
        folder = self.run_folder(name)
        run = self.current if self.current and self.current.name == name and self.current.plan.out.resolve() == folder.resolve() else None
        if run:
            config, world = run.plan.config, run.plan.world
        else:
            config, world, _ = load_run(folder)
        paths = sorted((folder / "episodes").glob("*.jsonl"))
        items = [(score_episode(events, world), events) for events in (read_events(p) for p in paths if is_complete(p))]
        return build_payload(world, config, folder.name, items)

    def run_report(self, body: dict, name: str) -> dict:
        return self.payload(name)

    def dashboard_html(self, name: str) -> str:
        return render_dashboard(self.payload(name))


# --------------------------------------------------------------------------- HTTP


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "authz-pilot"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):  # quiet: the terminal shows only errors
            pass

        def _send(self, status: int, body: bytes, content_type: str, headers: list[tuple[str, str]]) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, data, "application/json; charset=utf-8", [("Content-Security-Policy", API_CSP), ("X-Frame-Options", "DENY")])

        def _text(self, status: int, text: str) -> None:
            self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8", [("Content-Security-Policy", API_CSP), ("X-Frame-Options", "DENY")])

        def _handle(self) -> None:
            if self.headers.get("Host", "") not in app.hosts:
                self.close_connection = True
                return self._text(403, f"This app answers only at http://127.0.0.1:{app.port}/")
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                return self._api(path)
            if self.command != "GET":
                return self._text(405, "Method not allowed.")
            if path == "/":
                page = (WEB / "app.html").read_text(encoding="utf-8").replace("__TOKEN__", app.token)
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", [("Content-Security-Policy", APP_CSP), ("X-Frame-Options", "DENY"), ("Cross-Origin-Opener-Policy", "same-origin")])
            if path.startswith("/static/") and path[len("/static/"):] in STATIC:
                name = path[len("/static/"):]
                return self._send(200, (WEB / name).read_bytes(), STATIC[name], [("X-Frame-Options", "DENY")])
            match = re.fullmatch(r"/runs/([^/]+)/?", path)
            if match:
                try:
                    html = app.dashboard_html(match.group(1))
                except ApiError as exc:
                    return self._text(exc.status, exc.message)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", [("Content-Security-Policy", dashboard_csp(html)), ("X-Frame-Options", "SAMEORIGIN")])
            return self._text(404, "Not found.")

        def _api(self, path: str) -> None:
            token = self.headers.get("X-Authz-Token", "")
            if not hmac.compare_digest(token.encode("utf-8"), app.token.encode("utf-8")):
                return self._json(403, {"error": "Missing or wrong token. Reload the page."})
            if self.command in ("POST", "DELETE"):
                origin = self.headers.get("Origin")
                if origin is not None and origin not in app.origins:
                    return self._json(403, {"error": "Requests from other sites are not allowed."})
                site = self.headers.get("Sec-Fetch-Site")
                if site is not None and site not in ("same-origin", "none"):
                    return self._json(403, {"error": "Requests from other sites are not allowed."})
            body: dict = {}
            if self.command == "POST":
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    self.close_connection = True
                    return self._json(415, {"error": "Send JSON."})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY:
                    self.close_connection = True
                    return self._json(413, {"error": "That request is too large."})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "The request is not valid JSON."})
                if not isinstance(body, dict):
                    return self._json(400, {"error": "Send a JSON object."})
            try:
                payload = app.route(self.command, path, body)
            except ApiError as exc:
                return self._json(exc.status, {"error": exc.message, **exc.extra})
            except Exception:
                traceback.print_exc()
                return self._json(500, {"error": "Something went wrong in the app; the terminal shows the details."})
            return self._json(200, payload)

        do_GET = _handle
        do_POST = _handle
        do_DELETE = _handle

    return Handler


def make_server(app: App, port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    app.bind(server.server_address[1])
    return server


def serve(*, port: int = 8765, open_browser: bool = True, results: Path | None = None, data_dir: Path = DEFAULT_DATA_DIR) -> int:
    app = App(results=results, data_dir=data_dir)
    try:
        server = make_server(app, port)
    except OSError as exc:
        print(f"Cannot listen on 127.0.0.1:{port} ({exc.strerror}). Try another port: python -m authz_pilot app --port {port + 1}")
        return 1
    url = f"http://127.0.0.1:{app.port}/"
    # Flushed, so the address shows up at once even when the output goes to a log file.
    print(f"The app is running at {url} (this computer only). Press Ctrl-C to stop it.", flush=True)
    print(f"Runs are saved in {app.results}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if app.current and app.current.status == "running":
            print(f"Run {app.current.name} was stopped before it finished. Its finished episodes are saved. To finish it:")
            print("  " + resume_command(app.current.plan))
    return 0
