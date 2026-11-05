"""A self-contained HTML dashboard for one run.

`python -m authz_pilot dashboard results/<run>` writes `dashboard.html` next to
the report. It is a single file with the data embedded: open it in a browser,
no server and no network needed.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .runner import CONDITIONS
from .scoring import VIOLATIONS, EpisodeScore, summarize
from .world import World

WEB = Path(__file__).parent / "web"
SHORT = {"A": "Prompt only", "B": "+ Code checks", "C": "+ Monitor"}


def outcome(score: EpisodeScore) -> str:
    """One label per episode, used for colour and grouping.

    A violation that took effect wins over everything else, including an error
    later in the episode, so the page never shows fewer violations than the report.
    """
    if score.violations:
        return "violation"
    if score.stopped in ("error", "incomplete"):
        return "error"
    if score.stopped == "refusal":
        return "refusal"
    if score.kind == "trap":
        return "stopped" if score.attempted else "safe"
    return "ok" if score.completed else "incomplete"


_KEEP = {
    "turn_start": ("turn", "sender", "message", "notice", "permissions"),
    "tool_call": ("turn", "id", "name", "input", "args", "status", "blocked_by", "result", "reason"),
    "monitor_call": ("turn", "target", "decision", "reason", "error", "model"),
    "turn_end": ("turn", "outcome", "reply", "delivered", "blocked_by", "reason"),
    "error": ("turn", "error"),
}


def _slim(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        kind = e["event"]
        if kind == "model_call":
            thinking = [b["thinking"] for b in e.get("content", []) if b.get("type") == "thinking" and b.get("thinking")]
            out.append({"event": kind, "turn": e.get("turn"), "step": e.get("step"), "stop_reason": e.get("stop_reason"), "thinking": thinking})
        elif kind in _KEEP:
            out.append({"event": kind} | {k: e.get(k) for k in _KEEP[kind] if e.get(k) is not None})
    return out


def build_payload(world: World, config: dict, run_name: str, items: list[tuple[EpisodeScore, list[dict]]]) -> dict:
    scores = [s for s, _ in items]
    summary = summarize(scores)["conditions"]
    monitor: dict[str, Counter] = {}
    episodes = []
    for score, events in items:
        reviews = [e for e in events if e["event"] == "monitor_call"]
        if reviews:
            m = monitor.setdefault(score.condition, Counter())
            m.update(reviews=len(reviews), blocks=sum(r["decision"] != "allow" for r in reviews), errors=sum(bool(r.get("error")) for r in reviews))
        episodes.append(
            {
                "key": f"{score.scenario}_{score.condition}_{score.repeat}",
                "scenario": score.scenario,
                "condition": score.condition,
                "repeat": score.repeat,
                "kind": score.kind,
                "trap": score.trap,
                "outcome": outcome(score),
                "completed": score.completed,
                "violations": score.violations,
                "attempted": score.attempted,
                "false_blocks": score.false_blocks,
                "expectations": score.expectations,
                "notes": score.notes,
                "escalations": score.escalations,
                "monitor_errors": score.monitor_errors,
                "stopped": score.stopped,
                "cost": round(score.cost, 5),
                "events": _slim(events),
            }
        )
    return {
        "run": run_name,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "config": {k: v for k, v in config.items() if k != "data_dir"},  # no local paths in the page
        "data_hash": world.data_hash,
        "conditions": {c: CONDITIONS[c] for c in summary},
        "short": {c: SHORT.get(c, c) for c in summary},
        "summary": summary,
        "monitor": {c: dict(v) for c, v in monitor.items()},
        "violation_types": VIOLATIONS,
        "people": {p.id: {"name": p.name, "role": world.role_label(p)} for p in world.people.values()},
        "scenarios": [
            {"id": s.id, "kind": s.kind, "trap": s.trap, "title": s.title, "description": s.description}
            for s in world.scenarios.values()
        ],
        "episodes": episodes,
    }


def _json_for_script(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    # Keep the data inert inside <script type="application/json">.
    return text.replace("</", "<\\/").replace("<!--", "<\\u0021--").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _page() -> str:
    """The page around the data: the shared report styles and script, inlined so the file stands alone."""
    css = (WEB / "report.css").read_text(encoding="utf-8")
    js = (WEB / "report.js").read_text(encoding="utf-8")
    # An inline script ends at the first "</script", so the shared script may never contain one.
    if "</script" in js.lower() or "__DATA__" in css + js:
        raise ValueError("report.js or report.css contains text that would break the page")
    return TEMPLATE.replace("__REPORT_CSS__", css).replace("__REPORT_JS__", js)


def render_dashboard(payload: dict) -> str:
    head, tail = _page().split("__DATA__")
    return head + _json_for_script(payload) + tail


def write_dashboard(run_dir: Path, world: World, config: dict, items: list[tuple[EpisodeScore, list[dict]]]) -> Path:
    path = run_dir / "dashboard.html"
    path.write_text(render_dashboard(build_payload(world, config, run_dir.name, items)), encoding="utf-8")
    return path


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent authority pilot</title>
<style>
__REPORT_CSS__</style>
</head>
<body>
<div class="page" id="app"></div>
<div class="scrim" hidden></div>
<aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title" hidden>
  <div class="drawer-head">
    <div><h2 id="drawer-title"></h2><p class="sub" id="drawer-sub"></p></div>
    <button class="btn" type="button" id="drawer-close">Close</button>
  </div>
  <div class="drawer-body" id="drawer-body"></div>
</aside>
<div class="tooltip" role="tooltip" hidden></div>
<script id="data" type="application/json">__DATA__</script>
<script>
__REPORT_JS__</script>
</body>
</html>
"""
