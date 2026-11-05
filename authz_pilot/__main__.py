"""Command line: app, key, validate, run, report, dashboard, show.

    python -m authz_pilot app              # the local web app: quick start, settings, your scenarios, results
    python -m authz_pilot key set          # paste your Anthropic API key (hidden); key test checks it for free
    python -m authz_pilot key set --provider openai    # or google: keys for GPT and Gemini models
    python -m authz_pilot validate
    python -m authz_pilot run --dry-run
    python -m authz_pilot run --scenarios L01,T09 --conditions A
    python -m authz_pilot report results/<run>
    python -m authz_pilot dashboard results/<run>
    python -m authz_pilot show results/<run>/episodes/T09_A_1.jsonl
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import webbrowser
from pathlib import Path

from .checks import scripted_checks
from .models import EFFORTS, MODELS, provider_of
from .providers import check_models
from .prompts import render_transcript
from .runner import CONDITIONS, is_complete, read_events
from .runs import RunError, execute, load_run, pick_scenarios, plan_run, problems_for_run, providers_needed, write_report
from .scoring import score_episode
from .settings import PROVIDERS, describe_source, env_key, load_settings, mask, parse_settings, remove_key, resolve_key, save_key
from .world import DEFAULT_DATA_DIR, CustomScenarioError, custom_path, load_world, validate


def _world(data_dir) -> object | None:
    try:
        return load_world(data_dir, custom=custom_path())
    except CustomScenarioError as exc:
        print(f"Your scenarios cannot be read: {exc}")
        return None


def _load_run(run_dir: Path):
    config, world, warnings = load_run(run_dir)
    for warning in warnings:
        print(f"Warning: {warning}.")
    return config, world


def cmd_validate(args) -> int:
    world = _world(args.data)
    if world is None:
        return 1
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
    builtin = [s for s in world.scenarios.values() if not s.custom]
    legit = sum(s.kind == "legit" for s in builtin)
    mine = len(world.scenarios) - len(builtin)
    print(f"OK: {len(builtin)} scenarios ({legit} legit, {len(builtin) - legit} traps)" + (f" and {mine} of your own" if mine else "") + f", data {world.data_hash}.")
    print("\nWhat code checks (condition B) can stop when an agent takes every bait (scripted, no model):\n")
    print("| Trap | Type | Violations without checks | Still take effect with code checks |")
    print("|---|---|---|---|")
    for row in coverage:
        still = ", ".join(row["still_effective_with_code_checks"]) or "none"
        print(f"| {row['scenario']} | {row['trap']} | {', '.join(row['violations_without_checks'])} | {still} |")
    stopped = sum(not row["still_effective_with_code_checks"] for row in coverage)
    print(f"\nCode checks stop every violation in {stopped} of {len(coverage)} traps.")
    return 0


def cmd_run(args) -> int:
    # Flags override the saved settings (key set / the app's Settings page).
    flags = {"model": args.model, "effort": args.effort, "monitor_model": args.monitor_model, "monitor_effort": args.monitor_effort, "max_steps": args.max_steps, "workers": args.workers, "max_cost": args.max_cost}
    s, bad = parse_settings({k: v for k, v in flags.items() if v is not None}, load_settings())
    if bad:
        raise SystemExit("; ".join(f"--{k.replace('_', '-')}: {v}" for k, v in bad.items()))
    conditions = [c.strip().upper() for c in args.conditions.split(",") if c.strip()]
    if not conditions or any(c not in CONDITIONS for c in conditions):
        raise SystemExit(f"--conditions takes a comma-separated subset of {', '.join(CONDITIONS)}")
    world = _world(args.data)
    if world is None:
        return 1
    try:
        scenario_ids = pick_scenarios(world, args.scenarios)
    except RunError as exc:
        raise SystemExit(str(exc)) from None
    problems = problems_for_run(world, scenario_ids)
    if problems:
        print("Data problems; run `python -m authz_pilot validate`:")
        print("\n".join(f"- {p}" for p in problems))
        return 1

    keys = None
    if not args.dry_run:
        keys, found = {}, []
        for provider, roles in providers_needed(s, conditions).items():
            p = PROVIDERS[provider]
            key, source = resolve_key(provider)
            if not key and not (provider == "anthropic" and os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                flag = "" if provider == "anthropic" else f" --provider {provider}"
                print(f"No {p.name} API key found, and the {' and '.join(roles)} model runs on it. Save one (the input is hidden):")
                print(f"  python -m authz_pilot key set{flag}")
                print(f"or set {p.env[0]}. On macOS a Keychain item works too:")
                print(f'  security add-generic-password -a "$USER" -s {p.keychain} -U -w')
                print("To try everything without a key: python -m authz_pilot run --dry-run")
                return 2
            keys[provider] = key
            found.append(f"{p.name} key: {describe_source(source, provider)}" + (f" ({mask(key)})" if key else ""))
        use_monitor = "C" in conditions
        episodes = len(scenario_ids) * len(conditions) * args.repeats
        print(f"About to run {episodes} episodes: {len(scenario_ids)} scenarios × conditions {','.join(conditions)} × {args.repeats} repeat(s).")
        print(f"Assistant: {s.model} (effort {s.effort})" + (f"; monitor: {s.monitor_model} (effort {s.monitor_effort})" if use_monitor else ""))
        print("\n".join(found))
        print(f"No new episode starts once the estimated spend passes ${s.max_cost:.2f}.")
        if not args.yes:
            if not sys.stdin.isatty():
                print("Not a terminal; add --yes to confirm.")
                return 2
            if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return 1

    try:
        plan = plan_run(scenario_ids=scenario_ids, conditions=conditions, repeats=args.repeats, settings=s, dry_run=args.dry_run, scripted_agent=args.scripted_agent, out=args.out, data_dir=Path(args.data), api_keys=keys)
    except RunError as exc:
        print(exc)
        return 1
    state = execute(plan, progress=(lambda _: None) if args.dry_run else print)
    out, scores = plan.out, state["scores"]
    print((out / "report.md").read_text(encoding="utf-8"))
    print(f"Episodes run: {state['ran']}, already done: {state['skipped']}, not started (budget): {state['over_budget']}. Estimated spend: ${state['spent']:.2f}.")
    errors = sum(sc.stopped == "error" for sc in scores)
    monitor_errors = sum(sc.monitor_errors for sc in scores)
    if errors:
        print(f"Warning: {errors} episode(s) stopped with an error; run the same command again to retry them.")
    if monitor_errors:
        print(f"Warning: the monitor failed {monitor_errors} time(s); those actions were blocked. Check the monitor_call events.")
    print(f"Report: {out / 'report.md'}")
    print(f"Dashboard: {out / 'dashboard.html'}  (python -m authz_pilot dashboard {out})")
    return 0


def cmd_key(args) -> int:
    provider = args.provider
    p = PROVIDERS[provider]
    if args.action == "set":
        key = getpass.getpass(f"Paste your {p.name} API key (it is not shown): ").strip()
        try:
            path = save_key(key, provider)
        except ValueError as exc:
            print(exc)
            return 1
        print(f"Saved in {path}, readable only by your user.")
        env, name = env_key(provider)
        if env:
            print(f"Note: {name} is set in this shell, and it is used instead of the saved key.")
        print("Check it for free: python -m authz_pilot key test")
        return 0
    if args.action == "remove":
        print("Removed the saved key." if remove_key(provider) else "There was no saved key.")
        key, source = resolve_key(provider)
        if key:
            print(f"Still in use: {describe_source(source, provider)} ({mask(key)}).")
        return 0
    if args.action == "status":
        keys = {name: resolve_key(name) for name in PROVIDERS}
        for name, (key, source) in keys.items():
            print(f"{PROVIDERS[name].name} key: {describe_source(source, name)}" + (f" ({mask(key)})" if key else ""))
        return 0 if keys[provider][0] else 1

    # test: the models in the settings, with the key of each one's provider
    s = load_settings()
    roles = {"assistant": (s.model, s.effort), "monitor": (s.monitor_model, s.monitor_effort)}
    keys = {}
    for name in dict.fromkeys(provider_of(m) for m, _ in roles.values()):
        key, source = resolve_key(name)
        print(f"{PROVIDERS[name].name} key: {describe_source(source, name)}" + (f" ({mask(key)})" if key else ""))
        keys[name] = key
    if not any(keys.values()) and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print("Save one first: python -m authz_pilot key set" + ("" if "anthropic" in keys else f" --provider {next(iter(keys))}"))
        return 2
    result = check_models(roles, keys)
    if result["error"]:
        print(result["error"])
        return 1
    for row in result["models"]:
        status = "ok  " if row["ok"] else "FAIL"
        problems = "" if row["ok"] else ": " + "; ".join(row["problems"])
        print(f"{status} {row['role']:<9} {row['name']} ({row['model']}), effort {row['effort']}{problems}")
    print("The key works, and both models can do what the harness needs." if result["ok"] else "Pick other models or efforts in the settings, then test again.")
    print("These checks are free: they look the models up, and use no tokens.")
    return 0 if result["ok"] else 1


def cmd_report(args) -> int:
    config, world = _load_run(args.run_dir)
    scores = write_report(args.run_dir, world, config)
    print((args.run_dir / "report.md").read_text(encoding="utf-8"))
    print(f"Scored {len(scores)} episodes.")
    return 0


def cmd_dashboard(args) -> int:
    config, world = _load_run(args.run_dir)
    scores = write_report(args.run_dir, world, config)
    path = (args.run_dir / "dashboard.html").resolve()
    print(f"Dashboard for {len(scores)} episodes: {path}")
    if not args.no_open:
        webbrowser.open(path.as_uri())
    return 0


def cmd_show(args) -> int:
    events = read_events(args.episode)
    run_dir = args.episode.parent.parent
    if (run_dir / "config.json").exists():
        _, world = _load_run(run_dir)
    else:
        world = _world(DEFAULT_DATA_DIR)
        if world is None:
            return 1
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


def cmd_app(args) -> int:
    from .app import serve

    return serve(port=args.port, open_browser=not args.no_open, results=args.results, data_dir=Path(args.data))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m authz_pilot", description="Agent authority pilot")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("app", help="open the local web app: quick start, settings, your own scenarios and results")
    a.add_argument("--port", type=int, default=8765)
    a.add_argument("--no-open", action="store_true", help="start the server without opening a browser")
    a.add_argument("--results", type=Path, help="where runs are saved (default: results/ in the project)")
    a.add_argument("--data", default=str(DEFAULT_DATA_DIR))
    a.set_defaults(func=cmd_app)

    k = sub.add_parser("key", help="save, check or remove an API key (Anthropic, OpenAI or Google)")
    k.add_argument("action", choices=["set", "test", "status", "remove"], help="set: paste it (hidden); test: free check of the keys and the chosen models; status: where each key comes from; remove: delete the saved key")
    k.add_argument("--provider", choices=list(PROVIDERS), default="anthropic", help="whose key to set or remove (default: anthropic)")
    k.set_defaults(func=cmd_key)

    v = sub.add_parser("validate", help="check the data and run the scripted checks (no API calls)")
    v.add_argument("--data", default=str(DEFAULT_DATA_DIR))
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("run", help="run scenarios against a real model (Claude, GPT or Gemini), or --dry-run with a scripted agent")
    r.add_argument("--conditions", default="A,B,C", help="comma-separated subset of A,B,C")
    r.add_argument("--scenarios", default="all", help="all (the 30 built-in ones), legit, trap, custom (yours), or comma-separated ids")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--model", choices=list(MODELS), help="the assistant (default: the saved setting, else claude-opus-5-5)")
    r.add_argument("--effort", choices=EFFORTS, help="default: the saved setting, else medium")
    r.add_argument("--monitor-model", choices=list(MODELS), help="the monitor in condition C (default: the saved setting, else claude-sonnet-5-5)")
    r.add_argument("--monitor-effort", choices=EFFORTS, help="default: the saved setting, else medium")
    r.add_argument("--max-steps", type=int, help="model calls allowed per message (default 8)")
    r.add_argument("--workers", type=int, help="episodes run in parallel (default 4)")
    r.add_argument("--max-cost", type=float, help="USD; no new episode starts after the estimate passes this (default 20)")
    r.add_argument("--out", type=Path, help="run directory (default: results/<timestamp>)")
    r.add_argument("--dry-run", action="store_true", help="use a scripted agent; no API key needed")
    r.add_argument("--scripted-agent", choices=["gullible", "oracle"], default="gullible", help="which scripted agent --dry-run uses")
    r.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    r.add_argument("--data", default=str(DEFAULT_DATA_DIR))
    r.set_defaults(func=cmd_run)

    rep = sub.add_parser("report", help="rebuild report.md, summary.json and dashboard.html from a run directory")
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
    # Output sent to a file on Windows uses the local code page, which lacks some characters that
    # model replies and transcripts contain. Replace those instead of stopping with an error.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    raise SystemExit(main())
