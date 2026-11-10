"""Build the GitHub Pages site: a landing page that lists the published runs, and each run's dashboard.

The index is made from each run's config.json and summary.json at deploy time, so a run added to runs/
appears on the site without editing anything here. Standard library only; it does not import authz_pilot,
so the Pages workflow needs no install.

    python scripts/build_pages.py --out _site
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "authz_pilot" / "web"
CONDITIONS = {"A": "Rules in the prompt", "B": "Plus code checks", "C": "Plus a monitor model"}
TIMES = {1: "once", 2: "twice", 3: "three times"}
FOLDER_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-")


# ---------------------------------------------------------------- counts, as scoring.py writes them


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson score interval for k of n. A copy of authz_pilot.scoring.wilson."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    scale = 1 + z * z / n
    return (0.0 if k == 0 else (centre - half) / scale), (1.0 if k == n else (centre + half) / scale)


def percent(x: float) -> str:
    """A bound as a whole percentage, or with one decimal where rounding would make it read as 0 or 100."""
    whole = f"{x * 100:.0f}"
    return f"{x * 100:.1f}" if whole in ("0", "100") and 0 < x < 1 else whole


def interval(k: int, n: int) -> str:
    low, high = wilson(k, n)
    return f"{percent(low)}–{percent(high)}%"


# ---------------------------------------------------------------- what the page says about a run


@dataclass
class Run:
    name: str
    label: str
    day: date | None
    scenarios: str
    traps: int
    legit: int
    recorded_as: str
    agent: str
    monitor: str
    effort: str
    notices: str
    repeats: int
    dry_run: bool
    episodes: int
    planned: int
    conditions: dict[str, dict[str, Any]] = field(default_factory=dict)


def model_names() -> dict[str, str]:
    """Model ids to their names, read from models.py's table. Missing or changed, the ids are prettified."""
    try:
        text = (ROOT / "authz_pilot" / "models.py").read_text(encoding="utf-8")
    except OSError:
        return {}
    return dict(re.findall(r'"([a-z0-9][a-z0-9.\-]*)"\s*:\s*ModelInfo\(\s*"([^"]+)"', text))


def model_name(model: str, names: dict[str, str]) -> str:
    if model in names:
        return names[model]
    words = re.sub(r"-(\d+)-(\d+)\b", r" \1.\2", model).replace("-", " ").split()
    return " ".join(w.upper() if w in ("gpt",) else w[:1].upper() + w[1:] for w in words)


def id_ranges(ids: list[str]) -> str:
    """'T16, T17, T18, L16, L17' as 'T16–T18, L16, L17': traps first, then legit, then the rest."""
    groups: dict[str, list[tuple[int, str]]] = {}
    for sid in ids:
        m = re.fullmatch(r"([A-Za-z]+)(\d+)", sid)
        prefix, number = (m.group(1), int(m.group(2))) if m else (sid, -1)
        groups.setdefault(prefix, []).append((number, sid))
    order = sorted(groups, key=lambda p: ({"T": 0, "L": 1}.get(p, 2), p))
    parts: list[str] = []
    for prefix in order:
        items = sorted(set(groups[prefix]))
        i = 0
        while i < len(items):
            j = i
            while j + 1 < len(items) and items[j + 1][0] == items[j][0] + 1 and items[i][0] >= 0:
                j += 1
            if j - i >= 2:
                parts.append(f"{items[i][1]}–{items[j][1]}")
            else:
                parts.extend(sid for _, sid in items[i : j + 1])
            i = j + 1
    return ", ".join(parts)


def built_in_ids(run_dir: Path, ids: list[str]) -> list[str]:
    """A run recorded with custom scenarios that later joined the built-in set, under the built-in ids.

    The scenarios are matched by title. An id with no match stays as it was recorded."""
    custom = run_dir / "custom_scenarios.toml"
    if not custom.is_file():
        return ids
    try:
        recorded = {s["id"]: s.get("title") for s in tomllib.loads(custom.read_text("utf-8")).get("scenario", [])}
        data = tomllib.loads((ROOT / "data" / "scenarios.toml").read_text("utf-8")).get("scenario", [])
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return ids
    by_title = {s.get("title"): s["id"] for s in data if s.get("title") and "id" in s}
    return [by_title.get(recorded.get(sid), sid) for sid in ids]


def folder_label(folder: str, agent: str) -> str:
    """What a run's folder is called beyond its date and its model: 'Harder traps 2', or nothing."""
    rest = FOLDER_DATE.sub("", folder)
    if agent and rest.endswith(agent):
        rest = rest[: -len(agent)]
    rest = rest.strip("-").replace("-", " ")
    return rest[:1].upper() + rest[1:]


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def load_run(run_dir: Path, names: dict[str, str]) -> Run:
    config = json.loads((run_dir / "config.json").read_text("utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text("utf-8"))
    conditions: dict[str, dict[str, Any]] = summary["conditions"]
    ids = list(config.get("scenarios") or sorted({e["scenario"] for e in summary.get("episodes", [])}))
    shown = built_in_ids(run_dir, ids)
    m = FOLDER_DATE.match(run_dir.name)
    repeats = int(config.get("repeats") or 1)
    kinds = {e["scenario"]: e.get("kind") for e in summary.get("episodes", [])}
    return Run(
        name=run_dir.name,
        label=folder_label(run_dir.name, str(config.get("agent", ""))),
        traps=sum(k == "trap" for k in kinds.values()),
        legit=sum(k == "legit" for k in kinds.values()),
        day=date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None,
        scenarios=id_ranges(shown),
        recorded_as=id_ranges(ids) if shown != ids else "",
        agent=model_name(str(config.get("agent", "unknown")), names),
        monitor=model_name(str(config["monitor"]), names) if config.get("monitor") else "",
        effort=str(config.get("effort") or ""),
        notices=str(config.get("notices") or "system"),
        repeats=repeats,
        dry_run=bool(config.get("dry_run")),
        episodes=sum(int(c.get("episodes", 0)) for c in conditions.values()),
        planned=len(ids) * len(config.get("conditions") or conditions) * repeats,
        conditions=conditions,
    )


# ---------------------------------------------------------------- the page

E = html.escape
LAYERS = (
    '<svg class="layers" viewBox="0 0 20 20" width="{size}" height="{size}" aria-hidden="true">'
    + "".join(
        f'<path d="M2 {14.2 - i * 4.4:.1f} 10 {14.2 - i * 4.4 - 3.6:.1f} 18 {14.2 - i * 4.4:.1f} '
        f'10 {14.2 - i * 4.4 + 3.6:.1f}z" stroke-linejoin="round" class="{{c{i}}}"/>'
        for i in range(3)
    )
    + "</svg>"
)
MARK = {
    "bad": "M5.4 5.4l5.2 5.2M10.6 5.4l-5.2 5.2",
    "good": "M4.6 8.3 7 10.6 11.4 5.6",
}


def layers(n: int, size: int = 20) -> str:
    return LAYERS.format(size=size, **{f"c{i}": "on" if i < n else "off" for i in range(3)})


def mark(tone: str) -> str:
    return (
        f'<svg class="vi" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">'
        f'<circle cx="8" cy="8" r="8" style="fill:var(--{tone})"/>'
        f'<path d="{MARK[tone]}" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
        f'style="stroke:var(--on-{tone})"/></svg>'
    )


def meter(k: int, stopped: int, n: int) -> str:
    """One thin bar for a count: violations, then attempts that were all stopped, then the rest."""
    parts = [("bad", k), ("warn", stopped), ("good", n - k - stopped)]
    return (
        '<span class="meter" aria-hidden="true">'
        + "".join(f'<i class="t-{tone}" style="flex:{v} 1 0"></i>' for tone, v in parts if v > 0)
        + "</span>"
    )


def count_cell(letter: str, k: int, stopped: int, n: int, sampled: bool, detail: list[str]) -> str:
    """One set-up's box: how many trap episodes had a violation that took effect, and what else to know."""
    tone = "bad" if k else "good"
    if n:
        headline = f"{k} / {n}" + (f' <span class="ci">({interval(k, n)})</span>' if sampled else "")
        body = f'<span class="w">{headline}</span>{meter(k, stopped, n)}'
    else:
        body = '<span class="w">–</span><span class="d">no trap episodes</span>'
    if stopped:
        detail = [f"{stopped} stopped before {'it' if stopped == 1 else 'they'} took effect", *detail]
    return (
        f'<div class="cell t-{tone}">{mark(tone)}<span class="cw">'
        f'<span class="cn"><b class="display">{E(letter)}</b> {E(CONDITIONS.get(letter, letter))}</span>'
        + body
        + "".join(f'<span class="d">{E(d)}</span>' for d in detail)
        + "</span></div>"
    )


def condition_cell(letter: str, c: dict[str, Any], sampled: bool) -> str:
    done, legit = int(c.get("legit_completed", 0)), int(c.get("legit_episodes", 0))
    refused = int(c.get("refusals", 0)) + int(c.get("provider_stops", 0))
    errors = int(c.get("errors", 0))
    detail = []
    if legit:
        detail.append(f"{done} / {legit} legit requests done")
    if refused:
        detail.append(f"{refused} ended in a refusal")
    if errors:
        detail.append(f"{errors} ended with an error")
    violated, stopped = int(c.get("trap_episodes_violated", 0)), int(c.get("trap_episodes_stopped_only", 0))
    return count_cell(letter, violated, stopped, int(c.get("trap_episodes", 0)), sampled, detail)


def by_assistant(runs: list[Run]) -> str:
    """The runs added up per assistant: the first thing to read, before any one run."""
    totals: dict[str, dict[str, list[int]]] = {}
    seen: dict[str, int] = {}
    for run in runs:
        if run.dry_run:
            continue
        seen[run.agent] = seen.get(run.agent, 0) + 1
        for letter, c in run.conditions.items():
            cell = totals.setdefault(run.agent, {}).setdefault(letter, [0, 0, 0])
            cell[0] += int(c.get("trap_episodes_violated", 0))
            cell[1] += int(c.get("trap_episodes_stopped_only", 0))
            cell[2] += int(c.get("trap_episodes", 0))
    if not totals:
        return ""

    def rate(agent: str) -> float:
        k, _, n = next(iter(totals[agent].values()))
        return k / n if n else 0.0

    rows = []
    for agent in sorted(totals, key=lambda a: (-rate(a), a)):
        cells = "".join(count_cell(letter, k, s, n, True, []) for letter, (k, s, n) in totals[agent].items())
        rows.append(
            f'<div class="who-row"><h3>{E(agent)} <span class="muted">{plural(seen[agent], "run", "runs")}</span></h3>'
            f'<div class="conds">{cells}</div></div>'
        )
    return (
        '<section class="finding" aria-labelledby="f-h">'
        '<h2 id="f-h">What the runs show so far</h2>'
        '<p class="sub">Trap episodes where a violation took effect, out of all the trap episodes of each assistant, '
        "per set-up. The range is the 95% interval.</p>" + "".join(rows) + "</section>"
    )


def run_card(run: Run) -> str:
    when = f"{run.day.day} {run.day:%b %Y}" if run.day else ""
    times = TIMES.get(run.repeats, f"{run.repeats} times")
    size = " and ".join(
        s
        for s in (plural(run.traps, "trap", "traps"), plural(run.legit, "legit request", "legit requests"))
        if s[0] != "0"
    )
    facts = [
        when,
        run.scenarios if run.label else "",
        size,
        f"each {times}",
        f"{run.episodes} episodes",
        f"monitor {run.monitor}" if run.monitor else "",
    ]
    notes = []
    if run.recorded_as:
        notes.append(f"Recorded as {run.recorded_as}, custom ids, before these scenarios joined the built-in set.")
    if run.notices == "user_turn":
        notes.append("Operator notices reached this assistant inside the user's turn, not as system messages.")
    elif run.notices == "developer":
        notes.append("Operator notices reached this assistant as developer messages.")
    if run.planned > run.episodes:
        missing = run.planned - run.episodes
        notes.append(f"{missing} planned {'episode was' if missing == 1 else 'episodes were'} not started.")
    if run.dry_run:
        notes.append("Dry run: a scripted assistant, not a model.")
    href = f"runs/{run.name}/dashboard.html"
    cells = "".join(condition_cell(c, v, not run.dry_run) for c, v in run.conditions.items())
    return (
        '<article class="run">'
        f'<h3><a href="{E(href)}">{E(run.label or run.scenarios)}</a> <span class="who">{E(run.agent)}</span></h3>'
        f'<p class="facts-line">{E(" · ".join(f for f in facts if f))}</p>'
        f'<div class="conds">{cells}</div>'
        + "".join(f'<p class="note">{E(n)}</p>' for n in notes)
        + '<p class="open"><span class="go">Read every conversation <span aria-hidden="true">→</span></span>'
        f' <span class="muted folder">{E(run.name)}</span></p>'
        "</article>"
    )


PAGE_CSS = """
.lede { margin: 0 0 6px; font-size: 17px; max-width: 70ch; }
.finding, .runs-head { margin: 40px 0 0; }
.finding > .sub, .runs-head > .sub { font-size: 14px; margin: 4px 0 0; max-width: 75ch; }
.who-row { margin: 18px 0 0; }
.who-row h3 { font-size: 17px; margin: 0 0 8px; }
.who-row h3 .muted { font-size: 13px; font-weight: 500; margin-left: 6px; }
.runs { display: grid; gap: 16px; margin: 16px 0 0; }
.run {
  position: relative; background: var(--sheet); border: 1px solid var(--rule); border-radius: var(--r-surface);
  padding: 18px 22px;
}
.run:hover { border-color: var(--ink-3); }
.run h3 { font-size: 19px; }
.run h3 a { text-decoration: none; }
.run h3 a::after { content: ""; position: absolute; inset: 0; border-radius: var(--r-surface); }
.run h3 a:focus-visible { outline: none; }
.run h3 a:focus-visible::after { outline: 2px solid var(--focus); outline-offset: 2px; }
.run h3 .who { color: var(--ink-2); font-weight: 500; margin-left: 6px; white-space: nowrap; }
.facts-line { color: var(--ink-2); font-size: 13.5px; margin: 4px 0 14px; }
.conds { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr)); gap: 8px; }
.conds .cell { align-items: flex-start; min-height: 0; padding: 10px 12px; }
.conds .cell .vi { margin-top: 2px; }
.conds .cw { flex: 1; }
.conds .cn { font-size: 12.5px; color: var(--ink-2); line-height: 1.3; margin-bottom: 4px; }
.conds .cn b { font-size: 16px; color: var(--ink); margin-right: 3px; }
.conds .w { font-size: 18px; font-variant-numeric: tabular-nums; }
.conds .ci { font-size: 13px; font-weight: 500; color: var(--ink-2); }
.finding .conds .w { font-size: 24px; }
.meter { display: flex; gap: 2px; height: 6px; margin: 7px 0 5px; border-radius: 3px; overflow: hidden; }
.meter i { display: block; height: 100%; min-width: 3px; background: var(--tone); }
.note { margin: 10px 0 0; color: var(--ink-2); font-size: 13px; }
.open { margin: 14px 0 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; font-size: 13px; }
.open .go { font-weight: 600; }
.run:hover .go { text-decoration: underline; text-underline-offset: 3px; }
.open .folder { font: 12px/1.4 var(--mono); overflow-wrap: anywhere; }
@media (max-width: 520px) { .run { padding: 16px; } .report-page { padding-left: 16px; padding-right: 16px; } }
"""

THEME_SCRIPT = """
(() => {
  const THEMES = ["auto", "light", "dark"];
  const LABEL = { auto: "Auto", light: "Light", dark: "Dark" };
  const ICON = {
    auto: '<circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.5"/>'
      + '<path d="M8 2.4a5.6 5.6 0 0 1 0 11.2z" fill="currentColor"/>',
    light: '<g fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">'
      + '<circle cx="8" cy="8" r="3"/>'
      + '<path d="M8 1.6v1.6M8 12.8v1.6M1.6 8h1.6M12.8 8h1.6M3.5 3.5l1.1 1.1M11.4 11.4l1.1 1.1'
      + 'M3.5 12.5l1.1-1.1M11.4 4.6l1.1-1.1"/></g>',
    dark: '<path d="M13.4 9.7A5.6 5.6 0 1 1 6.3 2.6a4.4 4.4 0 0 0 7.1 7.1z" fill="none" stroke="currentColor"'
      + ' stroke-width="1.5" stroke-linejoin="round"/>',
  };
  let theme = "auto";
  try { const t = localStorage.getItem("authz-theme"); if (THEMES.includes(t)) theme = t; } catch (e) {}
  const hash = location.hash.slice(1).split("&");
  if (hash.includes("light")) theme = "light";
  if (hash.includes("dark")) theme = "dark";
  const apply = () => {
    if (theme === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  };
  apply();
  const btn = document.getElementById("theme");
  const draw = () => {
    btn.innerHTML = '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">' + ICON[theme] + "</svg>";
    const span = document.createElement("span");
    span.textContent = LABEL[theme];
    btn.append(span);
    btn.setAttribute("aria-label", "Colours: " + theme + ". Change them");
    btn.title = theme === "auto" ? "Colours follow your computer" : "Colours: " + theme;
  };
  btn.addEventListener("click", () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    try { localStorage.setItem("authz-theme", theme); } catch (e) {}
    apply();
    draw();
  });
  draw();
})();
"""


def render_index(runs: list[Run], repo: str) -> str:
    css = (WEB / "report.css").read_text("utf-8")
    theme = (WEB / "theme.js").read_text("utf-8")
    for text in (css, theme):
        if "</style" in text.lower() or "</script" in text.lower():
            raise ValueError("report.css or theme.js contains text that would break the page")
    face = (
        '@font-face { font-family: "Commissioner"; font-weight: 100 900; font-display: swap; '
        'src: url("fonts/commissioner.woff2") format("woff2"); }\n'
    )
    gh = f"https://github.com/{repo}"
    body = "".join(run_card(r) for r in runs) or '<p class="empty">No published runs yet.</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent authority pilot</title>
<meta name="description" content="Published runs of a small experiment: does an AI agent tell apart having access, \
being asked and being authorised?">
<script>{theme}</script>
<style>
{face}</style>
<style>
{css}</style>
<style>{PAGE_CSS}</style>
</head>
<body>
<main class="report-page">
<header class="report-top"><a class="brand" href="{E(gh)}">{layers(3, 18)}Agent authority pilot</a>\
<button class="btn quiet small theme-btn" type="button" id="theme">Auto</button></header>
<h1 class="report-title display">Does an AI agent tell apart having access, being asked, and being authorised?</h1>
<p class="report-meta">An assistant with tools can usually do more than the person it is helping may do. \
Each run put an assistant through traps and legit requests under three set-ups: A, rules in the prompt; \
B, plus code checks; C, plus a monitor model. Small numbers: read them as signals, not estimates. \
<a href="{E(gh)}">The code and the scenarios</a> · <a href="{E(gh)}/blob/main/RESULTS.md">The results in words</a></p>
{by_assistant(runs)}
<div class="runs-head"><h2>The runs</h2>
<p class="sub">Newest first. Open one to read every conversation behind its numbers.</p></div>
<section class="runs" aria-label="Runs">{body}</section>
<footer class="report-foot"><ul>
<li>A, rules in the prompt: the policy in the system prompt, nothing else.</li>
<li>B, plus code checks: A, plus deterministic checks against the sender's current permissions.</li>
<li>C, plus a monitor model: B, plus a second model that reviews every email, approval and reply before it takes \
effect.</li>
<li>Disclosure is detected with canary values, so a paraphrase that avoids the exact value is not counted: \
disclosure counts are a lower bound.</li>
</ul></footer>
</main>
<script>{THEME_SCRIPT}</script>
</body>
</html>
"""


def with_home(page: str) -> str:
    """A run's page, told where the list of runs is, so that it links back to it. Only on the site:
    the dashboard.html in a run's folder stays a page that stands alone."""
    head = '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    if page.count(head) != 1:
        raise SystemExit("a dashboard.html does not have the head this script expects")
    return page.replace(head, head + '<meta name="authz-home" content="../../">\n')


def build(runs_dir: Path, out: Path, repo: str) -> list[Run]:
    names = model_names()
    runs: list[Run] = []
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        missing = [f for f in ("config.json", "summary.json", "dashboard.html") if not (run_dir / f).is_file()]
        if missing:
            raise SystemExit(f"{run_dir}: missing {', '.join(missing)}")
        runs.append(load_run(run_dir, names))
        target = out / "runs" / run_dir.name
        target.mkdir(parents=True, exist_ok=True)
        (target / "dashboard.html").write_text(
            with_home((run_dir / "dashboard.html").read_text("utf-8")), encoding="utf-8"
        )
    runs.sort(key=lambda r: (r.day or date.min, r.name), reverse=True)
    (out / "fonts").mkdir(parents=True, exist_ok=True)
    for font in ("commissioner.woff2", "OFL.txt"):
        shutil.copyfile(WEB / "fonts" / font, out / "fonts" / font)
    (out / "index.html").write_text(render_index(runs, repo), encoding="utf-8")
    return runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=ROOT / "_site", help="the folder to write the site to")
    parser.add_argument("--runs", type=Path, default=ROOT / "runs", help="the published runs")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY") or "toul10/agent-authority-pilot",
        help="owner/name on GitHub, for the links back to the code",
    )
    args = parser.parse_args(argv)
    if args.out.exists():
        # Only ever replace a site this script wrote, never a folder given by mistake.
        if any(args.out.iterdir()) and not (args.out / "index.html").is_file():
            parser.error(f"{args.out} is not empty and holds no index.html; choose another --out")
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)
    runs = build(args.runs, args.out, args.repo)
    print(f"Wrote {args.out / 'index.html'} with {len(runs)} runs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
