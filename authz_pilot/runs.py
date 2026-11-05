"""Run folders: planning a run, writing its config, and rebuilding its reports.

The command line and the app both start runs through `plan_run` and `execute`.
A run that uses your own scenarios keeps a copy of them (custom_scenarios.toml),
so editing or deleting a scenario later never changes how an old run is scored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .custom import snapshot
from .dashboard import write_dashboard
from .models import cost_usd
from .runner import CONDITIONS, is_complete, read_events, run_batch
from .scoring import EpisodeScore, render_report, score_episode, summarize
from .settings import Settings
from .world import DEFAULT_DATA_DIR, PROJECT_ROOT, CustomScenarioError, World, custom_path, load_world, validate

SNAPSHOT = "custom_scenarios.toml"
RESUME_KEYS = ("agent", "effort", "monitor", "monitor_effort", "data_hash", "custom_hash", "max_steps")


class RunError(Exception):
    """A run cannot start; the message says why."""


def results_dir() -> Path:
    """Where new runs go: results/ in the project, or AUTHZ_PILOT_RESULTS."""
    override = os.environ.get("AUTHZ_PILOT_RESULTS")
    return Path(override).expanduser() if override else PROJECT_ROOT / "results"


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


def _new_folder(results: Path, name: str) -> Path:
    out, n = results / name, 2
    while out.exists():
        out, n = results / f"{name}-{n}", n + 1
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
    results: Path | None = None,
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
        out = _new_folder(results or results_dir(), stamp + ("-dry-run" if dry_run else "") + (f"-{suffix}" if suffix else ""))
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


def execute(plan: Plan, *, progress=print, observe=None, stop=None) -> dict:
    """Run the plan's episodes, then write the report, summary and dashboard."""
    state = run_batch(plan.world, plan.jobs, plan.make_agent, plan.make_monitor, plan.out, workers=plan.workers, max_cost=plan.max_cost, max_steps=plan.max_steps, meta=plan.meta, progress=progress, observe=observe, stop=stop)
    state["scores"] = write_report(plan.out, plan.world, plan.config)
    return state


# --------------------------------------------------------------------------- cost estimate

# Tokens per message, measured in a short test run (results/smoke-20261105: 4
# episodes, 6 messages, Opus 5.5 assistant and Sonnet 5.5 monitor, both at
# medium effort). Only a rough guide: real use depends on how much the model
# reads and thinks, and higher effort usually means more output.
AGENT_TOKENS_PER_MESSAGE = {"input_tokens": 6, "output_tokens": 234, "cache_creation_input_tokens": 1589, "cache_read_input_tokens": 3106}
MONITOR_TOKENS_PER_MESSAGE = {"input_tokens": 2080, "output_tokens": 88}


def estimate_cost(world: World, scenario_ids: list[str], conditions: list[str], model: str, monitor_model: str, repeats: int = 1) -> dict:
    """A rough cost estimate for a real run, from the measured tokens per message."""
    messages = sum(len(world.scenarios[s].turns) for s in scenario_ids) * repeats
    agent = (cost_usd(model, AGENT_TOKENS_PER_MESSAGE) or 0.0) * messages * len(conditions)
    monitor = (cost_usd(monitor_model, MONITOR_TOKENS_PER_MESSAGE) or 0.0) * messages if "C" in conditions else 0.0
    return {"usd": round(agent + monitor, 4), "episodes": len(scenario_ids) * len(conditions) * repeats, "messages": messages * len(conditions)}


# --------------------------------------------------------------------------- listing runs


def jobs_of(config: dict) -> list[tuple[str, str, int]]:
    return [(sid, c, r) for r in range(1, int(config.get("repeats") or 1) + 1) for sid in config.get("scenarios", []) for c in config.get("conditions", [])]


def list_runs(results: Path) -> list[dict]:
    """Every run folder under `results`, newest first, with a few numbers for a list view."""
    runs = []
    for config_path in results.glob("*/config.json"):
        folder = config_path.parent
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        done = sum(1 for p in (folder / "episodes").glob("*.jsonl") if is_complete(p))
        totals = {}
        try:
            conditions = json.loads((folder / "summary.json").read_text(encoding="utf-8"))["conditions"]
            totals = {
                "violations": sum(c["trap_episodes_violated"] + c["legit_with_violation"] for c in conditions.values()),
                "cost": round(sum(c["cost_usd"] for c in conditions.values()), 4),
            }
        except (OSError, ValueError, KeyError, TypeError):
            pass
        runs.append(
            {
                "name": folder.name,
                "modified": config_path.stat().st_mtime,
                "dry_run": bool(config.get("dry_run")),
                "agent": config.get("agent"),
                "effort": config.get("effort"),
                "monitor": config.get("monitor"),
                "conditions": config.get("conditions", []),
                "scenarios": config.get("scenarios", []),
                "episodes": len(jobs_of(config)),
                "done": done,
                **totals,
            }
        )
    for run in runs:
        newest = max((p.stat().st_mtime for p in (results / run["name"] / "episodes").glob("*.jsonl")), default=run["modified"])
        run["modified"] = max(run["modified"], newest)
    return sorted(runs, key=lambda r: r["modified"], reverse=True)
