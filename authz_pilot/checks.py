"""Consistency checks that run the scenarios with scripted agents (no API calls).

- The oracle does exactly what each turn expects. It must complete every
  expectation without a violation, and the code checks must never block it.
  If they do, the scenario contradicts the policy.
- The gullible agent also takes the bait. It must commit at least one violation
  in every trap under condition A; otherwise the scorer could not detect the trap.
  Running it under condition B shows which traps code checks can stop at all.
"""

from __future__ import annotations

from .runner import run_episode
from .scoring import score_episode
from .scripted import PlannedAgent
from .world import World


def _types(items: list[dict]) -> list[str]:
    return sorted({v["type"] for v in items})


def scripted_checks(world: World, ids: list[str] | None = None) -> tuple[list[str], list[dict]]:
    """Returns (problems, rows showing what code checks stop for the gullible agent).

    `ids` limits the checks to some scenarios; by default every scenario is checked.
    """
    problems: list[str] = []
    coverage: list[dict] = []
    for sc in world.scenarios.values():
        if ids is not None and sc.id not in ids:
            continue
        for condition in ("A", "B"):
            score = score_episode(run_episode(world, sc, condition, PlannedAgent(world, sc)), world)
            if score.violations:
                problems.append(f"{sc.id} ({condition}): doing what the scenario expects commits {_types(score.violations)}")
            if score.attempted or score.false_blocks:
                problems.append(f"{sc.id} ({condition}): a code check blocks what the scenario expects")
            missed = [e for e in score.expectations if not e["met"]]
            if missed:
                problems.append(f"{sc.id} ({condition}): expectations cannot be met: {missed}")
        if sc.kind != "trap":
            continue
        a = score_episode(run_episode(world, sc, "A", PlannedAgent(world, sc, gullible=True)), world)
        b = score_episode(run_episode(world, sc, "B", PlannedAgent(world, sc, gullible=True)), world)
        if not a.violations:
            problems.append(f"{sc.id}: taking the bait commits no violation, so the trap cannot be detected")
        coverage.append(
            {
                "scenario": sc.id,
                "trap": sc.trap,
                "violations_without_checks": _types(a.violations),
                "still_effective_with_code_checks": _types(b.violations),
                "stopped_by_code_checks": _types(b.attempted),
            }
        )
    return problems, coverage
