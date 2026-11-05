"""Run folders: planning a run, writing its config, and rebuilding its reports.

The command line and the app both start runs through `plan_run` and `execute`.
A run that uses your own scenarios keeps a copy of them (custom_scenarios.toml),
so editing or deleting a scenario later never changes how an old run is scored.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .custom import snapshot
from .dashboard import write_dashboard
from .runner import CONDITIONS, is_complete, read_events, run_batch
from .scoring import EpisodeScore, render_report, score_episode, summarize
from .settings import Settings
from .world import DEFAULT_DATA_DIR, PROJECT_ROOT, CustomScenarioError, World, custom_path, load_world, validate

RESULTS_DIR = PROJECT_ROOT / "results"
SNAPSHOT = "custom_scenarios.toml"
RESUME_KEYS = ("agent", "effort", "monitor", "monitor_effort", "data_hash", "custom_hash", "max_steps")


class RunError(Exception):
    """A run cannot start; the message says why."""


def git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def portable(path: Path) -> str:
    """A data path as stored in config.json: relative to the project when inside it."""
    path = Path(path).resolve()
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def data_dir_of(config: dict) -> Path:
    """The data a run used. If that folder is gone (the project moved), use the project's own."""
    saved = config.get("data_dir")
    if saved:
        path = Path(saved)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if (path / "world.toml").is_file():
            return path
    return DEFAULT_DATA_DIR


def load_run(run_dir: Path) -> tuple[dict, World, list[str]]:
    """A run's config and world (with its own copy of any custom scenarios), plus warnings."""
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    snap = run_dir / SNAPSHOT
    world = load_world(data_dir_of(config), custom=snap if snap.is_file() else None)
    warnings = []
    if config.get("data_hash") != world.data_hash:
        warnings.append(f"the data files changed since this run (run {config.get('data_hash')}, now {world.data_hash})")
    return config, world, warnings


def write_report(run_dir: Path, world: World, config: dict) -> list[EpisodeScore]:
    """Score every finished episode; write report.md, summary.json and dashboard.html."""
    paths = sorted((run_dir / "episodes").glob("*.jsonl"))
    items = [(score_episode(events, world), events) for events in (read_events(p) for p in paths if is_complete(p))]
    scores = [s for s, _ in items]
    (run_dir / "report.md").write_text(render_report(scores, world, config), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(summarize(scores) | {"config": config}, indent=2, ensure_ascii=False), encoding="utf-8")
    write_dashboard(run_dir, world, config, items)
    return scores


def pick_scenarios(world: World, spec: str) -> list[str]:
    """`all`, `legit` and `trap` mean the built-in scenarios; `custom` means yours."""
    builtin = [s for s in world.scenarios.values() if not s.custom]
    if spec == "all":
        return [s.id for s in builtin]
    if spec in ("legit", "trap"):
        return [s.id for s in builtin if s.kind == spec]
    if spec == "custom":
        ids = [s.id for s in world.scenarios.values() if s.custom]
        if not ids:
            raise RunError("You have no scenarios of your own yet. Write one in the app: python -m authz_pilot app")
        return ids
    ids = [x.strip().upper() for x in spec.split(",") if x.strip()]
    unknown = [x for x in ids if x not in world.scenarios]
    if unknown:
        raise RunError(f"Unknown scenario ids: {', '.join(unknown)}")
    return ids


def problems_for_run(world: World, scenario_ids: list[str]) -> list[str]:
    """Data problems that stop a run. Custom scenarios that are not part of it do not count."""
    used = replace(world, scenarios={k: v for k, v in world.scenarios.items() if not v.custom or k in scenario_ids})
    return validate(used)


@dataclass
class Plan:
    out: Path
    world: World
    jobs: list[tuple[str, str, int]]
    config: dict
    make_agent: Callable
    make_monitor: Callable
    workers: int
    max_cost: float | None
    max_steps: int
    meta: dict = field(default_factory=dict)


def _new_folder(name: str) -> Path:
    out, n = RESULTS_DIR / name, 2
    while out.exists():
        out, n = RESULTS_DIR / f"{name}-{n}", n + 1
    return out


def plan_run(
    *,
    scenario_ids: list[str],
    conditions: list[str],
    settings: Settings,
    dry_run: bool,
    repeats: int = 1,
    scripted_agent: str = "gullible",
    out: Path | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    custom: Path | None = None,
    api_key: str | None = None,
    label: str = "",
) -> Plan:
    """Check the inputs, create the run folder with its config, and build the agents.

    Raises RunError with a readable message when the run cannot start.
    """
    if not conditions or any(c not in CONDITIONS for c in conditions):
        raise RunError(f"Conditions are a subset of {', '.join(CONDITIONS)}.")
    if repeats < 1:
        raise RunError("Repeats must be at least 1.")
    custom = custom or custom_path()
    try:
        live = load_world(data_dir, custom=custom)
    except CustomScenarioError as exc:
        raise RunError(f"Your scenarios cannot be read: {exc}") from exc
    unknown = [s for s in scenario_ids if s not in live.scenarios]
    if not scenario_ids or unknown:
        raise RunError(f"Unknown scenario ids: {', '.join(unknown)}" if unknown else "Choose at least one scenario.")
    problems = problems_for_run(live, scenario_ids)
    if problems:
        raise RunError("Data problems; run `python -m authz_pilot validate`:\n" + "\n".join(f"- {p}" for p in problems))

    custom_ids = [s for s in scenario_ids if live.scenarios[s].custom]
    snap = snapshot(custom, custom_ids) if custom_ids else None
    if dry_run:
        config = {"agent": f"scripted-{scripted_agent}", "monitor": "scripted (allows everything)", "dry_run": True}
    else:
        use_monitor = "C" in conditions
        config = {
            "agent": settings.model,
            "effort": settings.effort,
            "monitor": settings.monitor_model if use_monitor else None,
            "monitor_effort": settings.monitor_effort if use_monitor else None,
            "dry_run": False,
        }
    config |= {
        "version": __version__,
        "git_commit": git_commit(),
        "data_hash": live.data_hash,
        "custom_hash": hashlib.sha256(snap.encode("utf-8")).hexdigest()[:12] if snap else "",
        "data_dir": portable(data_dir),
        "conditions": conditions,
        "scenarios": scenario_ids,
        "repeats": repeats,
        "max_steps": settings.max_steps,
    }

    if out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        suffix = re.sub(r"[^A-Za-z0-9-]", "", label)[:40]
        out = _new_folder(stamp + ("-dry-run" if dry_run else "") + (f"-{suffix}" if suffix else ""))
    config_path = out / "config.json"
    if config_path.exists():
        old = json.loads(config_path.read_text(encoding="utf-8"))
        if any((old.get(k) or None) != (config.get(k) or None) for k in RESUME_KEYS):
            raise RunError(f"{out} holds a run with different settings or scenarios; choose another --out.")
    out.mkdir(parents=True, exist_ok=True)
    if snap:
        (out / SNAPSHOT).write_text(snap, encoding="utf-8")
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # What runs is the copy in the run folder, never the live custom file.
    world = load_world(data_dir, custom=out / SNAPSHOT if snap else None)
    jobs = [(sid, c, r) for r in range(1, repeats + 1) for sid in scenario_ids for c in conditions]
    if dry_run:
        from .scripted import PlannedAgent, ScriptedMonitor

        gullible = scripted_agent == "gullible"

        def make_agent(scenario):
            return PlannedAgent(world, scenario, gullible=gullible)

        def make_monitor():
            return ScriptedMonitor()

        workers, max_cost = 1, None
    else:
        from .models import AnthropicAgent, AnthropicMonitor

        agent = AnthropicAgent(settings.model, effort=settings.effort, api_key=api_key)
        monitor = AnthropicMonitor(settings.monitor_model, effort=settings.monitor_effort, api_key=api_key) if "C" in conditions else None

        def make_agent(scenario):
            return agent

        def make_monitor():
            return monitor

        workers, max_cost = settings.workers, settings.max_cost
    meta = {k: config.get(k) for k in ("agent", "effort", "monitor", "monitor_effort")}
    return Plan(out, world, jobs, config, make_agent, make_monitor, workers, max_cost, settings.max_steps, meta)


def execute(plan: Plan, *, progress=print, observe=None) -> dict:
    """Run the plan's episodes, then write the report, summary and dashboard."""
    state = run_batch(plan.world, plan.jobs, plan.make_agent, plan.make_monitor, plan.out, workers=plan.workers, max_cost=plan.max_cost, max_steps=plan.max_steps, meta=plan.meta, progress=progress, observe=observe)
    state["scores"] = write_report(plan.out, plan.world, plan.config)
    return state
