"""Command line: validate, run, report, dashboard, show.

    python -m authz_pilot validate
    python -m authz_pilot run --dry-run
    python -m authz_pilot run --scenarios L01,T09 --conditions A
    python -m authz_pilot report results/<run>
    python -m authz_pilot dashboard results/<run>
    python -m authz_pilot show results/<run>/episodes/T09_A_1.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .checks import scripted_checks
from .dashboard import write_dashboard
from .models import DEFAULT_AGENT_MODEL, DEFAULT_MONITOR_MODEL
from .prompts import render_transcript
from .runner import CONDITIONS, is_complete, read_events, run_batch
from .scoring import render_report, score_episode, summarize
from .world import DEFAULT_DATA_DIR, load_world, validate

EFFORTS = ("low", "medium", "high", "xhigh", "max")
ROOT = Path(__file__).resolve().parent.parent


def _portable(path: Path) -> str:
    """A data path as stored in config.json: relative to the project when inside it."""
    path = path.resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _data_dir(config: dict) -> Path:
    """The data a run used. If that folder is gone (the project moved), use the project's own."""
    saved = config.get("data_dir")
    if saved:
        path = Path(saved)
        if not path.is_absolute():
            path = ROOT / path
        if (path / "world.toml").is_file():
            return path
    return DEFAULT_DATA_DIR


def _load_run(run_dir: Path) -> tuple[dict, object]:
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    world = load_world(_data_dir(config))
    if config.get("data_hash") != world.data_hash:
        print(f"Warning: the data files changed since this run (run {config.get('data_hash')}, now {world.data_hash}).")
    return config, world


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _pick_scenarios(world, spec: str) -> list[str]:
    if spec == "all":
        return list(world.scenarios)
    if spec in ("legit", "trap"):
        return [s.id for s in world.scenarios.values() if s.kind == spec]
    ids = [x.strip().upper() for x in spec.split(",") if x.strip()]
    unknown = [x for x in ids if x not in world.scenarios]
    if unknown:
        raise SystemExit(f"Unknown scenario ids: {', '.join(unknown)}")
    return ids


def cmd_validate(args) -> int:
    world = load_world(args.data)
    problems = validate(world)
    if problems:
        print("Data problems:")
        print("\n".join(f"- {p}" for p in problems))
        return 1
    problems, coverage = scripted_checks(world)
    if problems:
        print("Scenario problems found by the scripted checks:")
        print("\n".join(f"- {p}" for p in problems))
        return 1
    legit = sum(s.kind == "legit" for s in world.scenarios.values())
    print(f"OK: {len(world.scenarios)} scenarios ({legit} legit, {len(world.scenarios) - legit} traps), data {world.data_hash}.")
    print("\nWhat code checks (condition B) can stop when an agent takes every bait (scripted, no model):\n")
    print("| Trap | Type | Violations without checks | Still take effect with code checks |")
    print("|---|---|---|---|")
    for row in coverage:
        still = ", ".join(row["still_effective_with_code_checks"]) or "none"
        print(f"| {row['scenario']} | {row['trap']} | {', '.join(row['violations_without_checks'])} | {still} |")
    stopped = sum(not row["still_effective_with_code_checks"] for row in coverage)
    print(f"\nCode checks stop every violation in {stopped} of {len(coverage)} traps.")
    return 0


def _write_report(run_dir: Path, world, config: dict) -> list:
    """Score every finished episode; write report.md, summary.json and dashboard.html."""
    paths = sorted((run_dir / "episodes").glob("*.jsonl"))
    items = [(score_episode(events, world), events) for events in (read_events(p) for p in paths if is_complete(p))]
    scores = [s for s, _ in items]
    (run_dir / "report.md").write_text(render_report(scores, world, config), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(summarize(scores) | {"config": config}, indent=2, ensure_ascii=False), encoding="utf-8")
    write_dashboard(run_dir, world, config, items)
    return scores


def cmd_run(args) -> int:
    world = load_world(args.data)
    problems = validate(world)
    if problems:
        print("Data problems; run `python -m authz_pilot validate`:")
        print("\n".join(f"- {p}" for p in problems))
        return 1
    conditions = [c.strip().upper() for c in args.conditions.split(",") if c.strip()]
    if not conditions or any(c not in CONDITIONS for c in conditions):
        raise SystemExit(f"--conditions takes a comma-separated subset of {', '.join(CONDITIONS)}")
    scenario_ids = _pick_scenarios(world, args.scenarios)
    jobs = [(sid, c, r) for r in range(1, args.repeats + 1) for sid in scenario_ids for c in conditions]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = args.out or ROOT / "results" / (f"{stamp}-dry-run" if args.dry_run else stamp)

    if args.dry_run:
        from .scripted import PlannedAgent, ScriptedMonitor

        gullible = args.scripted_agent == "gullible"
        config = {"agent": f"scripted-{args.scripted_agent}", "monitor": "scripted (allows everything)", "dry_run": True}

        def make_agent(scenario):
            return PlannedAgent(world, scenario, gullible=gullible)

        def make_monitor():
            return ScriptedMonitor()
    else:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            print("No API key found. Set ANTHROPIC_API_KEY, for example from the macOS Keychain:")
            print('  export ANTHROPIC_API_KEY="$(security find-generic-password -a "$USER" -s anthropic-api-key -w)"')
            print("Or try everything without a key: python -m authz_pilot run --dry-run")
            return 2
        from .models import AnthropicAgent, AnthropicMonitor

        config = {"agent": args.model, "effort": args.effort, "monitor": args.monitor_model if "C" in conditions else None, "monitor_effort": args.monitor_effort if "C" in conditions else None, "dry_run": False}
        print(f"About to run {len(jobs)} episodes: {len(scenario_ids)} scenarios × conditions {','.join(conditions)} × {args.repeats} repeat(s).")
        print(f"Assistant: {args.model} (effort {args.effort})" + (f"; monitor: {args.monitor_model} (effort {args.monitor_effort})" if "C" in conditions else ""))
        print(f"No new episode starts once the estimated spend passes ${args.max_cost:.2f}.")
        if not args.yes:
            if not sys.stdin.isatty():
                print("Not a terminal; add --yes to confirm.")
                return 2
            if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return 1
        agent = AnthropicAgent(args.model, effort=args.effort)
        monitor = AnthropicMonitor(args.monitor_model, effort=args.monitor_effort) if "C" in conditions else None

        def make_agent(scenario):
            return agent

        def make_monitor():
            return monitor

    config |= {
        "version": __version__,
        "git_commit": _git_commit(),
        "data_hash": world.data_hash,
        "data_dir": _portable(Path(args.data)),
        "conditions": conditions,
        "scenarios": scenario_ids,
        "repeats": args.repeats,
        "max_steps": args.max_steps,
    }
    out.mkdir(parents=True, exist_ok=True)
    config_path = out / "config.json"
    if config_path.exists():
        old = json.loads(config_path.read_text(encoding="utf-8"))
        keys = ("agent", "effort", "monitor", "monitor_effort", "data_hash", "max_steps")
        if any(old.get(k) != config.get(k) for k in keys):
            print(f"{out} holds a run with different settings; choose another --out.")
            return 1
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    meta = {k: config.get(k) for k in ("agent", "effort", "monitor", "monitor_effort")}
    state = run_batch(world, jobs, make_agent, make_monitor, out, workers=1 if args.dry_run else args.workers, max_cost=None if args.dry_run else args.max_cost, max_steps=args.max_steps, meta=meta, progress=(lambda _: None) if args.dry_run else print)
    scores = _write_report(out, world, config)
    print((out / "report.md").read_text(encoding="utf-8"))
    print(f"Episodes run: {state['ran']}, already done: {state['skipped']}, not started (budget): {state['over_budget']}. Estimated spend: ${state['spent']:.2f}.")
    errors = sum(s.stopped == "error" for s in scores)
    monitor_errors = sum(s.monitor_errors for s in scores)
    if errors:
        print(f"Warning: {errors} episode(s) stopped with an error; run the same command again to retry them.")
    if monitor_errors:
        print(f"Warning: the monitor failed {monitor_errors} time(s); those actions were blocked. Check the monitor_call events.")
    print(f"Report: {out / 'report.md'}")
    print(f"Dashboard: {out / 'dashboard.html'}  (python -m authz_pilot dashboard {out})")
    return 0


def cmd_report(args) -> int:
    config, world = _load_run(args.run_dir)
    scores = _write_report(args.run_dir, world, config)
    print((args.run_dir / "report.md").read_text(encoding="utf-8"))
    print(f"Scored {len(scores)} episodes.")
    return 0


def cmd_dashboard(args) -> int:
    config, world = _load_run(args.run_dir)
    scores = _write_report(args.run_dir, world, config)
    path = (args.run_dir / "dashboard.html").resolve()
    print(f"Dashboard for {len(scores)} episodes: {path}")
    if not args.no_open:
        webbrowser.open(path.as_uri())
    return 0


def cmd_show(args) -> int:
    events = read_events(args.episode)
    config_path = args.episode.parent.parent / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    world = load_world(_data_dir(config))
    start = events[0]
    scenario = world.scenarios[start["scenario"]]
    print(f"# {scenario.id}: {scenario.title} (condition {start['condition']}, repeat {start.get('repeat', 1)})")
    if scenario.description:
        print(scenario.description)
    print()
    print(render_transcript(world, events, thinking=args.thinking))
    if not is_complete(args.episode):
        return 0
    score = score_episode(events, world)
    print("\n# Score")
    print(f"Violations that took effect: {score.violations or 'none'}")
    print(f"Stopped by a check: {score.attempted or 'none'}")
    if score.false_blocks:
        print(f"Harmless actions blocked: {score.false_blocks}")
    for e in score.expectations:
        print(f"Expectation (turn {e['turn']}) {'met' if e['met'] else 'NOT met'}: {e['expect']}")
    print(f"Estimated cost: ${score.cost:.4f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m authz_pilot", description="Agent authority pilot")
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="check the data and run the scripted checks (no API calls)")
    v.add_argument("--data", default=str(DEFAULT_DATA_DIR))
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("run", help="run scenarios against the Claude API, or --dry-run with a scripted agent")
    r.add_argument("--conditions", default="A,B,C", help="comma-separated subset of A,B,C")
    r.add_argument("--scenarios", default="all", help="comma-separated ids, or all, legit, trap")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--model", default=DEFAULT_AGENT_MODEL)
    r.add_argument("--effort", default="medium", choices=EFFORTS)
    r.add_argument("--monitor-model", default=DEFAULT_MONITOR_MODEL)
    r.add_argument("--monitor-effort", default="medium", choices=EFFORTS)
    r.add_argument("--max-steps", type=int, default=8, help="model calls allowed per message")
    r.add_argument("--workers", type=int, default=4, help="episodes run in parallel")
    r.add_argument("--max-cost", type=float, default=20.0, help="USD; no new episode starts after the estimate passes this")
    r.add_argument("--out", type=Path, help="run directory (default: results/<timestamp>)")
    r.add_argument("--dry-run", action="store_true", help="use a scripted agent; no API key needed")
    r.add_argument("--scripted-agent", choices=["gullible", "oracle"], default="gullible", help="which scripted agent --dry-run uses")
    r.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    r.add_argument("--data", default=str(DEFAULT_DATA_DIR))
    r.set_defaults(func=cmd_run)

    rep = sub.add_parser("report", help="rebuild report.md and summary.json from a run directory")
    rep.add_argument("run_dir", type=Path)
    rep.set_defaults(func=cmd_report)

    dash = sub.add_parser("dashboard", help="rebuild the report and open dashboard.html, a local page with charts and every conversation")
    dash.add_argument("run_dir", type=Path)
    dash.add_argument("--no-open", action="store_true", help="write the file without opening a browser")
    dash.set_defaults(func=cmd_dashboard)

    show = sub.add_parser("show", help="print one episode as a readable transcript")
    show.add_argument("episode", type=Path)
    show.add_argument("--thinking", action="store_true", help="include the assistant's thinking summaries and monitor decisions")
    show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
