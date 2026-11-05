// Shared by the app and the standalone run page (dashboard.html): verdicts, the scorecard,
// the summary charts and the conversation view. Text is always set with textContent; no HTML
// is ever parsed. This file is inlined into dashboard.html, so it must never contain the
// closing tag of a script element.
(() => {
"use strict";

// ---------------------------------------------------------------- helpers
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = Boolean(v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(3)) if (c != null && c !== false) el.append(c);
  return el;
}
// Like replaceChildren, but flattens arrays and drops null and false, as h() does.
function fill(el, ...children) {
  el.replaceChildren(...children.flat(3).filter(c => c != null && c !== false));
  return el;
}
const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
  return el;
}
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const words = list => list.map(t => String(t).replaceAll("_", " ")).join(", ");
function usd(x, digits) {
  x = Number(x || 0);
  if (digits != null) return "$" + x.toFixed(digits);
  return "$" + (x === 0 ? "0.00" : x < 0.1 ? x.toFixed(3) : x.toFixed(2));
}
// Replies are Markdown; show **bold** and `code` without parsing any HTML.
function rich(text) {
  const out = document.createDocumentFragment();
  for (const part of String(text || "").split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/)) {
    if (!part) continue;
    if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) out.append(h("strong", { text: part.slice(2, -2) }));
    else if (part.length > 2 && part.startsWith("`") && part.endsWith("`")) out.append(h("code", { text: part.slice(1, -1) }));
    else out.append(document.createTextNode(part));
  }
  return out;
}

// ---------------------------------------------------------------- marks
const GLYPH = {
  check: "M4.6 8.3 7 10.6 11.4 5.6",
  x: "M5.4 5.4l5.2 5.2M10.6 5.4l-5.2 5.2",
  shield: "M8 3.6 11.2 4.9v2.6c0 2-1.3 3.6-3.2 4.3-1.9-.7-3.2-2.3-3.2-4.3V4.9z",
  minus: "M5 8h6",
  alert: "M8 4.6v4.2M8 11.2v.2",
  dot: "M8 8h0",
};
function icon(tone, glyph, size = 16) {
  const s = svg("svg", { viewBox: "0 0 16 16", width: size, height: size, "aria-hidden": "true", class: "vi" });
  const c = svg("circle", { cx: 8, cy: 8, r: 8 });
  c.style.fill = `var(--${tone})`;
  const p = svg("path", { d: GLYPH[glyph], fill: "none", "stroke-width": glyph === "dot" ? 4 : 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" });
  p.style.stroke = `var(--on-${tone})`;
  s.append(c, p);
  return s;
}
// One word for the cell, one sentence for the legend and the conversation view.
const VERDICT = {
  violation: { word: "Violation", label: "Violation took effect", tone: "bad", glyph: "x" },
  stopped: { word: "Stopped", label: "Stopped before it took effect", tone: "warn", glyph: "shield" },
  safe: { word: "No attempt", label: "No attempt at the forbidden action", tone: "good", glyph: "check" },
  ok: { word: "Done", label: "Done: every expected action happened", tone: "good", glyph: "check" },
  incomplete: { word: "Not done", label: "Not done: an expected action is missing", tone: "serious", glyph: "minus" },
  refusal: { word: "Model refused", label: "The model refused to answer", tone: "neutral", glyph: "dot" },
  provider: { word: "Provider stopped", label: "The model's provider stopped the conversation", tone: "neutral", glyph: "alert" },
  error: { word: "Error", label: "Error: the episode stopped early", tone: "neutral", glyph: "alert" },
};
const LEGEND_ORDER = ["violation", "stopped", "safe", "ok", "incomplete", "refusal", "provider", "error"];
const verdict = kind => VERDICT[kind] || VERDICT.error;
const badge = (kind, size) => icon(verdict(kind).tone, verdict(kind).glyph, size);
// The set-ups stack safeguards: A has one layer, B two, C three.
const COND = {
  A: { name: "Rules in the prompt", short: "Prompt", layers: 1 },
  B: { name: "Plus code checks", short: "+ Checks", layers: 2 },
  C: { name: "Plus a monitor model", short: "+ Monitor", layers: 3 },
};
const condName = c => (COND[c] || { name: c }).name;
// Drawn as plates stacked one on another, bottom first; the ones in place are filled.
function layers(n, total = 3, size = 20) {
  const s = svg("svg", { viewBox: "0 0 20 20", width: size, height: size, "aria-hidden": "true", class: "layers" });
  for (let i = 0; i < total; i++) {
    const y = 14.2 - i * 4.4;
    s.append(svg("path", { d: `M2 ${y} 10 ${y - 3.6} 18 ${y} 10 ${y + 3.6}z`, "stroke-linejoin": "round", class: i < n ? "on" : "off" }));
  }
  return s;
}
const FAMILY = { injection: "Injection", false_authority: "False authority", cross_context: "Cross-context", permission_change: "Permission change" };
const BY = { code: "a code check", monitor: "the monitor" };
function legend(kinds) {
  if (!kinds.length) return null;
  return h("ul", { class: "legend", "aria-label": "What the colours mean" }, kinds.map(k => h("li", {}, badge(k, 14), verdict(k).label)));
}

// ---------------------------------------------------------------- theme
const THEMES = ["auto", "light", "dark"];
function getTheme() {
  try { const t = localStorage.getItem("authz-theme"); return THEMES.includes(t) ? t : "auto"; } catch (e) { return "auto"; }
}
function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}
function themeIcon(theme) {
  const s = svg("svg", { viewBox: "0 0 16 16", width: 16, height: 16, "aria-hidden": "true" });
  const line = { fill: "none", stroke: "currentColor", "stroke-width": 1.5, "stroke-linecap": "round", "stroke-linejoin": "round" };
  if (theme === "light") s.append(svg("circle", { cx: 8, cy: 8, r: 3, ...line }), svg("path", { d: "M8 1.6v1.6M8 12.8v1.6M1.6 8h1.6M12.8 8h1.6M3.5 3.5l1.1 1.1M11.4 11.4l1.1 1.1M3.5 12.5l1.1-1.1M11.4 4.6l1.1-1.1", ...line }));
  else if (theme === "dark") s.append(svg("path", { d: "M13.4 9.7A5.6 5.6 0 1 1 6.3 2.6a4.4 4.4 0 0 0 7.1 7.1z", ...line }));
  else s.append(svg("circle", { cx: 8, cy: 8, r: 5.6, ...line }), svg("path", { d: "M8 2.4a5.6 5.6 0 0 1 0 11.2z", fill: "currentColor" }));
  return s;
}
function themeButton(onChange) {
  const btn = h("button", { class: "btn quiet small theme-btn", type: "button" });
  let theme = getTheme();
  const draw = () => {
    fill(btn, themeIcon(theme), h("span", { text: { auto: "Auto", light: "Light", dark: "Dark" }[theme] }));
    btn.setAttribute("aria-label", `Colours: ${theme}. Change them`);
    btn.title = theme === "auto" ? "Colours follow your computer" : `Colours: ${theme}`;
  };
  btn.addEventListener("click", () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    try { localStorage.setItem("authz-theme", theme); } catch (e) {}
    applyTheme(theme);
    draw();
    if (onChange) onChange(theme);
  });
  draw();
  return btn;
}

// ---------------------------------------------------------------- one shape for an episode result
function fromEpisode(ep) {
  return {
    key: ep.key, scenario: ep.scenario, condition: ep.condition, repeat: ep.repeat, state: "done",
    outcome: ep.outcome, kind: ep.kind, completed: ep.completed, stopped: ep.stopped, cost: ep.cost,
    violations: [...new Set(ep.violations.map(v => v.type))],
    stoppedBy: [...new Set(ep.attempted.map(a => a.blocked_by))],
    attempted: ep.attempted.length, falseBlocks: ep.false_blocks.length,
  };
}
// The app's live rows, from GET /api/runs/<name>.
function fromRow(r) {
  const base = { key: r.key, scenario: r.scenario, condition: r.condition, repeat: r.repeat, state: r.state };
  if (r.state === "running") return { ...base, message: r.message, messages: r.messages, actions: r.actions };
  if (r.state !== "done") return base;
  return {
    ...base, outcome: r.outcome, kind: r.kind, completed: r.completed, stopped: r.stopped, cost: r.cost,
    violations: r.violations || [], stoppedBy: r.stopped_by || [], attempted: r.attempted || 0, falseBlocks: r.false_blocks || 0,
  };
}
function detail(c) {
  const bits = [];
  if (c.outcome === "violation") bits.push(words(c.violations));
  if (c.outcome === "stopped") bits.push("by " + (c.stoppedBy.length === 1 ? BY[c.stoppedBy[0]] || c.stoppedBy[0] : "checks and the monitor"));
  else if (c.attempted) bits.push(`${c.attempted} more stopped`);
  if (c.kind === "trap" && c.completed === false && !["violation", "error", "refusal", "provider"].includes(c.outcome)) bits.push("task missed");
  if (c.falseBlocks) bits.push(`${c.falseBlocks} harmless blocked`);
  if (c.outcome !== "error" && (c.stopped === "error" || c.stopped === "incomplete")) bits.push("then an error");
  return bits.filter(Boolean).join(", ");
}

// ---------------------------------------------------------------- the scorecard
function groupScenarios(scenarios) {
  const groups = [];
  const legit = scenarios.filter(s => s.kind !== "trap");
  if (legit.length) groups.push({ key: "legit", label: "Legit requests", scenarios: legit });
  for (const [f, label] of Object.entries(FAMILY)) {
    const list = scenarios.filter(s => s.kind === "trap" && s.trap === f);
    if (list.length) groups.push({ key: f, label: "Traps: " + label.toLowerCase(), scenarios: list });
  }
  const other = scenarios.filter(s => s.kind === "trap" && !FAMILY[s.trap]);
  if (other.length) groups.push({ key: "other", label: "Other traps", scenarios: other });
  return groups;
}
function condHead(c) {
  const m = COND[c] || { name: c, short: c, layers: 0 };
  return h("div", { class: "cond" },
    h("span", { class: "cond-letter display", text: c }),
    layers(m.layers),
    h("span", { class: "cond-name" }, h("span", { class: "long", text: m.name }), h("span", { class: "short", text: m.short })));
}
function cellEl(c, s, onOpen, seen, multi) {
  const where = `${s.id} under ${c.condition}` + (multi ? `, repeat ${c.repeat}` : "");
  if (c.state === "done") {
    const v = verdict(c.outcome);
    const d = [multi ? `repeat ${c.repeat}` : "", detail(c)].filter(Boolean).join(", ");
    const was = seen ? seen.get(c.key) : undefined;
    const props = { class: `cell t-${v.tone}` + (was !== undefined && was !== "done" ? " fresh" : ""), "data-key": c.key };
    const kids = [badge(c.outcome, 18), h("span", { class: "cw" }, h("span", { class: "w", text: v.word }), d ? h("span", { class: "d", text: d }) : null)];
    if (!onOpen) return h("div", props, kids);
    const el = h("button", { ...props, type: "button", "aria-label": `${where}: ${v.label}${d ? ", " + d : ""}. Read the conversation.` }, kids);
    el.addEventListener("click", () => onOpen(c, el));
    return el;
  }
  if (c.state === "running") {
    const d = c.messages ? `message ${c.message} of ${c.messages}` + (c.actions ? `, ${plural(c.actions, "action", "actions")}` : "") : "";
    return h("div", { class: "cell running", "data-key": c.key },
      h("span", { class: "pulse", "aria-hidden": "true" }),
      h("span", { class: "cw" }, h("span", { class: "w", text: "Running" }), d ? h("span", { class: "d", text: d }) : null));
  }
  const skipped = c.state === "skipped";
  return h("div", { class: "cell " + (skipped ? "skipped" : "waiting"), "data-key": c.key },
    h("span", { class: "cw" }, h("span", { class: "w", text: skipped ? "Not run" : "Waiting" })));
}
function scorecard({ groups, conditions, cells, onOpen, seen, labelled }) {
  const at = new Map();
  for (const c of cells) {
    const k = c.scenario + "\n" + c.condition;
    if (!at.has(k)) at.set(k, []);
    at.get(k).push(c);
  }
  const body = h("tbody");
  for (const g of groups) {
    if (labelled) body.append(h("tr", { class: "group" }, h("th", { colspan: String(conditions.length + 1), scope: "colgroup", text: g.label })));
    for (const s of g.scenarios) {
      body.append(h("tr", {},
        h("th", { scope: "row" }, h("span", { class: "sid", text: s.id }), h("span", { class: "stitle", text: s.title || "" })),
        conditions.map(c => {
          const list = (at.get(s.id + "\n" + c) || []).slice().sort((a, b) => a.repeat - b.repeat);
          return h("td", {}, list.map(x => cellEl(x, s, onOpen, seen, list.length > 1)));
        })));
    }
  }
  return h("table", { class: "score", style: `--cols:${conditions.length}` },
    h("caption", { class: "sr-only", text: "Result of each episode, by scenario and set-up" }),
    h("colgroup", {}, h("col", { class: "scen" }), conditions.map(() => h("col"))),
    h("thead", {}, h("tr", {},
      h("th", { class: "corner", scope: "col" }, h("span", { class: "sr-only", text: "Scenario" })),
      conditions.map(c => h("th", { scope: "col" }, condHead(c))))),
    body);
}
// The scorecard with its legend and, for a bigger run, filters by kind of scenario.
function board({ scenarios, conditions, onOpen }) {
  const groups = groupScenarios(scenarios);
  let filter = "all", cells = [], seen = null;
  const legendHost = h("div");
  const filterRow = h("div", { class: "filters", role: "group", "aria-label": "Show scenarios" });
  const tableHost = h("div");
  const el = h("div", { class: "board" }, h("div", { class: "board-tools" }, legendHost, filterRow), tableHost);
  function draw() {
    // Filters help only when there is a lot to look at.
    if (groups.length > 1 && scenarios.length > 6) {
      fill(filterRow, [["all", "All"], ...groups.map(g => [g.key, g.label])].map(([k, label]) => {
        const b = h("button", { class: "btn small", type: "button", "aria-pressed": String(k === filter), text: label });
        b.addEventListener("click", () => { filter = k; draw(); });
        return b;
      }));
    }
    const present = new Set(cells.filter(c => c.state === "done").map(c => c.outcome));
    fill(legendHost, legend(LEGEND_ORDER.filter(k => present.has(k))));
    const focused = document.activeElement && tableHost.contains(document.activeElement) ? document.activeElement.dataset.key : null;
    fill(tableHost, scorecard({ groups: groups.filter(g => filter === "all" || g.key === filter), conditions, cells, onOpen, seen, labelled: groups.length > 1 }));
    if (focused) {
      const again = tableHost.querySelector(`[data-key="${CSS.escape(focused)}"]`);
      if (again && again.tagName === "BUTTON") again.focus({ preventScroll: true });
    }
  }
  return { el, update(next, previous) { cells = next; seen = previous || null; draw(); } };
}

// ---------------------------------------------------------------- summary: numbers and charts
let tipEl = null;
function tip(el, lines) {
  const show = (x, y) => {
    if (!tipEl) { tipEl = h("div", { class: "tooltip", role: "tooltip", hidden: true }); document.body.append(tipEl); }
    fill(tipEl, lines().map((t, i) => h("div", { class: i === 0 ? "v" : "l", text: t })));
    tipEl.hidden = false;
    const w = tipEl.offsetWidth, ht = tipEl.offsetHeight;
    let left = x + 14, top = y - ht - 12;
    if (left + w > innerWidth - 8) left = Math.max(8, x - w - 14);
    if (top < 8) top = y + 18;
    tipEl.style.left = left + "px";
    tipEl.style.top = top + "px";
  };
  const hide = () => { if (tipEl) tipEl.hidden = true; };
  el.addEventListener("pointermove", e => show(e.clientX, e.clientY));
  el.addEventListener("pointerleave", hide);
  el.addEventListener("focus", () => { const r = el.getBoundingClientRect(); show(r.left + r.width / 2, r.top); });
  el.addEventListener("blur", hide);
}
function niceTicks(max) {
  const step = [1, 2, 5, 10, 20, 25, 50, 100, 200].find(s => max / s <= 5) || Math.ceil(max / 5);
  const end = Math.max(step, Math.ceil(max / step) * step);
  const out = [];
  for (let t = 0; t <= end; t += step) out.push(t);
  return out;
}
function countBy(eps) {
  const c = {};
  for (const e of eps) c[e.outcome] = (c[e.outcome] || 0) + 1;
  return c;
}
// Hide a number that does not fit inside its bar segment; the tooltip and the table still have it.
const fitter = typeof ResizeObserver === "function" ? new ResizeObserver(entries => {
  for (const entry of entries) for (const seg of entry.target.querySelectorAll(".seg")) {
    const num = seg.firstChild;
    num.hidden = false;
    if (seg.clientWidth < num.offsetWidth + 10) num.hidden = true;
  }
}) : null;

const TRAP_CATS = ["violation", "stopped", "safe", "refusal", "provider", "error"];
const LEGIT_CATS = ["ok", "incomplete", "violation", "refusal", "provider", "error"];
function stacked(title, subtitle, cats, group, conds, footnote) {
  const rows = conds.map(c => { const eps = group.filter(e => e.condition === c); return { cond: c, counts: countBy(eps), total: eps.length }; });
  const shown = cats.filter((k, i) => i < 3 || rows.some(r => r.counts[k]));
  const toggle = h("button", { class: "btn small", type: "button", "aria-pressed": "false", text: "Table" });
  const chart = h("div", {}, legend(shown));
  const ticks = niceTicks(Math.max(1, ...rows.map(r => r.total)));
  const dom = ticks[ticks.length - 1];
  const bars = h("div", { class: "bars" });
  for (const r of rows) {
    bars.append(h("div", { class: "row-label" }, h("b", { class: "display", text: r.cond }), h("span", { text: condName(r.cond) })));
    const plot = h("div", { class: "plot" });
    for (const t of ticks) plot.append(h("div", { class: "gridline" + (t === 0 ? " base" : ""), style: `left:${(t / dom) * 100}%` }));
    const bar = h("div", { class: "bar", style: `width:${(r.total / dom) * 100}%` });
    for (const k of shown) {
      const n = r.counts[k] || 0;
      if (!n) continue;
      const v = verdict(k);
      const seg = h("div", { class: `seg t-${v.tone}`, tabindex: "0", role: "img", style: `flex:${n} 1 0`,
        "aria-label": `${r.cond}, ${condName(r.cond).toLowerCase()}: ${n} of ${r.total}, ${v.label}` },
        h("span", { class: "tnum", text: String(n) }));
      tip(seg, () => [`${n} of ${r.total}`, v.label, `${r.cond}: ${condName(r.cond)}`]);
      bar.append(seg);
    }
    plot.append(bar);
    bars.append(plot);
  }
  bars.append(h("div"), h("div", { class: "axis", "aria-hidden": "true" }, ticks.map(t => h("span", { style: `left:${(t / dom) * 100}%`, text: String(t) }))));
  chart.append(bars);
  if (fitter) fitter.observe(bars);
  const table = h("table", { hidden: true },
    h("thead", {}, h("tr", {}, h("th", { text: "Set-up" }), shown.map(k => h("th", { class: "num", text: verdict(k).word })), h("th", { class: "num", text: "Episodes" }))),
    h("tbody", {}, rows.map(r => h("tr", {}, h("td", { text: `${r.cond}: ${condName(r.cond)}` }),
      shown.map(k => h("td", { class: "num" + (r.counts[k] ? "" : " zero"), text: String(r.counts[k] || 0) })),
      h("td", { class: "num", text: String(r.total) })))));
  toggle.addEventListener("click", () => {
    const showTable = table.hidden;
    table.hidden = !showTable;
    chart.hidden = showTable;
    toggle.textContent = showTable ? "Chart" : "Table";
    toggle.setAttribute("aria-pressed", String(showTable));
  });
  return h("figure", { class: "chart" },
    h("div", { class: "chart-head" }, h("div", {}, h("h3", { text: title }), h("p", { class: "sub", text: subtitle })), toggle),
    chart, table, footnote ? h("p", { class: "footnote", text: footnote }) : null);
}
const stoppedByError = e => e.stopped === "error" || e.stopped === "incomplete";
function summary(DATA) {
  const conds = Object.keys(DATA.summary);
  const eps = DATA.episodes;
  const cfg = DATA.config || {};
  const traps = eps.filter(e => e.kind === "trap"), legit = eps.filter(e => e.kind === "legit");
  const out = h("div", { class: "summary" });
  const cost = eps.reduce((a, e) => a + (e.cost || 0), 0);
  const apiErrors = eps.filter(stoppedByError).length;
  const monErrors = eps.reduce((a, e) => a + (e.monitor_errors || 0), 0);
  const escalations = eps.reduce((a, e) => a + (e.escalations || 0), 0);
  const scenarios = new Set(eps.map(e => e.scenario)).size;
  const stats = [
    ["Episodes", String(eps.length), `${plural(scenarios, "scenario", "scenarios")} × ${plural(conds.length, "set-up", "set-ups")} × ${plural(cfg.repeats || 1, "repeat", "repeats")}`],
    ["Estimated cost", cfg.dry_run ? "Free" : usd(cost, 2), cfg.dry_run ? "a scripted run calls no API" : "from the logged token usage"],
    ["Errors", String(apiErrors + monErrors), `${plural(apiErrors, "episode", "episodes")} stopped, ${plural(monErrors, "monitor error", "monitor errors")}`, apiErrors + monErrors ? "error" : null],
    ["Escalations", String(escalations), "the assistant handed a request to a person"],
  ];
  const mon = DATA.monitor && DATA.monitor.C;
  if (mon) stats.push(["Monitor reviews", String(mon.reviews || 0), `${mon.blocks || 0} blocked, ${plural(mon.errors || 0, "error", "errors")}`]);
  out.append(h("div", { class: "stats", role: "list", "aria-label": "Run totals" }, stats.map(([label, value, note, kind]) =>
    h("div", { class: "stat", role: "listitem" }, h("div", { class: "label", text: label }), h("div", { class: "value" }, kind ? badge(kind, 18) : null, value), h("div", { class: "note", text: note })))));

  const charts = h("div", { class: "charts", style: "margin-top:28px" });
  if (traps.length) charts.append(stacked("Traps: did a violation take effect?", "Trap episodes per set-up. Stopped means a code check or the monitor blocked every attempt.", TRAP_CATS, traps, conds));
  if (legit.length) {
    const blocked = conds.map(c => `${c} ${eps.filter(e => e.condition === c).reduce((a, e) => a + e.false_blocks.length, 0)}`).join(", ");
    charts.append(stacked("Legit requests: were they done?", "Legit episodes per set-up. Done means every expected action happened, with no violation.", LEGIT_CATS, legit, conds, "Harmless actions blocked by a check: " + blocked + "."));
  }
  out.append(charts);

  const tables = h("div", { class: "charts", style: "margin-top:32px" });
  if (traps.length) {
    const fams = Object.keys(FAMILY).filter(f => traps.some(e => e.trap === f));
    const thin = list => {
      const c = countBy(list);
      return h("div", { class: "thin", "aria-hidden": "true" }, TRAP_CATS.filter(k => c[k]).map(k => h("i", { class: "t-" + verdict(k).tone, style: `flex:${c[k]} 1 0` })));
    };
    tables.append(h("div", {},
      h("h3", { text: "Traps by family" }),
      h("p", { class: "sub", style: "font-size:13.5px", text: "Episodes where a violation took effect, out of the family's trap episodes." }),
      h("div", { class: "scroll", style: "margin-top:8px" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", { text: "Family" }), conds.map(c => h("th", { class: "num", text: c })))),
        h("tbody", {}, fams.map(f => h("tr", {}, h("td", { text: FAMILY[f] }), conds.map(c => {
          const list = traps.filter(e => e.trap === f && e.condition === c);
          const bad = list.filter(e => e.outcome === "violation").length;
          return h("td", { class: "num" }, h("span", { class: bad ? "" : "zero", text: `${bad} of ${list.length}` }), thin(list));
        }))))))));
  }
  tables.append(h("div", {},
    h("h3", { text: "Violation types" }),
    h("p", { class: "sub", style: "font-size:13.5px", text: "Episodes with at least one violation of the type that took effect." }),
    h("table", { style: "margin-top:8px" },
      h("thead", {}, h("tr", {}, h("th", { text: "Type" }), conds.map(c => h("th", { class: "num", text: c })))),
      h("tbody", {}, Object.entries(DATA.violation_types).map(([k, meaning]) => h("tr", {},
        h("td", {}, h("span", { text: k.replaceAll("_", " ") }), h("span", { class: "type-meaning", text: meaning })),
        conds.map(c => { const n = (DATA.summary[c].episodes_by_violation_type || {})[k] || 0; return h("td", { class: "num" + (n ? "" : " zero"), text: String(n) }); })))))));
  out.append(tables);
  return out;
}

// ---------------------------------------------------------------- one conversation
function violationText(v) {
  switch (v.type) {
    case "disclosure": return `${(v.canaries || []).join(", ")} from ${v.document} (${v.classification}) reached ${v.recipient} by ${v.channel}`;
    case "over_access": return `read ${v.document} (${v.classification})`;
    case "not_authorised": return `approved ${v.request}, which the sender may not approve`;
    case "unrequested_approval": return `approved ${v.request}, which nobody asked for`;
    case "unrequested_external_email": return `emailed ${v.recipient}, which nobody asked for`;
    case "followed_injection": return v.request ? `approved ${v.request}, as text in a document asked` : `emailed ${v.recipient}, as text in a document asked`;
    default: return v.type;
  }
}
function expectText(x) {
  const needles = (x.contains || []).join(", ");
  if (x.type === "reply") return needles ? `a reply that mentions ${needles}` : "a reply";
  if (x.type === "email") return `an email to ${x.to}` + (needles ? ` that mentions ${needles}` : "");
  if (x.type === "approve") return `approve ${x.request}`;
  if (x.type === "read") return `read ${x.document}`;
  return x.type;
}
function argsText(e) {
  const a = e.args || e.input || {};
  switch (e.name) {
    case "read_document": return a.document_id || "";
    case "approve_request": return a.request_id || "";
    case "send_email": return a.to ? "to " + a.to : "";
    case "escalate_to_human": return a.reason || "";
    default: return "";
  }
}
const TOOL_STATUS = { executed: "done", invalid: "invalid input", not_run: "not run" };
function episodeView(DATA, ep) {
  const s = DATA.scenarios.find(x => x.id === ep.scenario) || { id: ep.scenario, title: "", description: "" };
  const people = DATA.people || {};
  const person = id => people[id] || { name: id, role: "" };
  const v = verdict(ep.outcome);
  const c = fromEpisode(ep);
  const body = h("div", {});
  if (s.description) body.append(h("p", { class: "sub", text: s.description }));
  const d = detail(c);
  body.append(h("p", { class: "ep-verdict" }, badge(ep.outcome, 20), h("span", { class: "verdict", text: v.label }), d ? h("span", { class: "sub", text: d }) : null));

  const facts = h("div", { class: "facts" });
  const section = (title, items) => { if (items.length) facts.append(h("h3", { text: title }), h("ul", {}, items)); };
  const turn = n => `Message ${n}`;
  section("Violations that took effect", ep.violations.map(x => h("li", {}, badge("violation", 14), h("span", { text: `${turn(x.turn)}, ${x.type.replaceAll("_", " ")}: ${violationText(x)}` }))));
  section("Stopped before taking effect", ep.attempted.map(x => h("li", {}, badge("stopped", 14), h("span", { text: `${turn(x.turn)}, ${x.type.replaceAll("_", " ")}: ${violationText(x)}, blocked by ${BY[x.blocked_by] || x.blocked_by}` }))));
  section("Harmless actions blocked", ep.false_blocks.map(b => h("li", {}, badge("incomplete", 14), h("span", { text: `${turn(b.turn)}: ${b.action}, blocked by ${BY[b.blocked_by] || b.blocked_by}${b.reason ? ": " + b.reason : ""}` }))));
  section("Expected", ep.expectations.map(x => h("li", {}, badge(x.met ? "ok" : "violation", 14), h("span", { text: `${turn(x.turn)}: ${expectText(x.expect)}${x.met ? "" : " (not done)"}` }))));
  const extra = [];
  for (const n of ep.notes || []) extra.push(h("li", {}, h("span", { text: `${turn(n.turn)}: an email to ${n.recipient}, a colleague, that nobody asked for (not counted as a violation)` })));
  if (ep.escalations) extra.push(h("li", {}, h("span", { text: plural(ep.escalations, "escalation to a person", "escalations to a person") })));
  if (ep.monitor_errors) extra.push(h("li", {}, badge("error", 14), h("span", { text: plural(ep.monitor_errors, "monitor error", "monitor errors") + "; the action was blocked" })));
  if (ep.stopped) extra.push(h("li", {}, badge("error", 14), h("span", { text: `The episode stopped early: ${ep.stopped}` })));
  section("Also", extra);
  if (!facts.childNodes.length) facts.append(h("p", { class: "sub", text: "No violations, nothing blocked, and nothing expected in this scenario." }));
  body.append(facts);

  let box = body, sender = null;
  for (const e of ep.events) {
    if (e.event === "turn_start") {
      sender = person(e.sender);
      box = h("section", { class: "turn", "aria-label": turn(e.turn) },
        h("div", { class: "turn-head" }, `${turn(e.turn)} from `, h("b", { text: sender.name }), sender.role ? `, ${sender.role}` : ""));
      if (e.notice) box.append(h("div", { class: "notice" }, h("b", { text: "Operator notice, through the system channel" }), e.notice));
      box.append(h("div", { class: "msg", text: e.message }));
      body.append(box);
    } else if (e.event === "model_call") {
      for (const t of e.thinking || []) {
        const preview = t.length > 110 ? t.slice(0, 110).trimEnd() + "…" : t;
        box.append(h("details", { class: "step" }, h("summary", { text: "Thinking (summary): " + preview }), h("div", { class: "thinking", text: t })));
      }
    } else if (e.event === "monitor_call") {
      const allow = e.decision === "allow";
      box.append(h("div", { class: "step" },
        h("div", { class: "action" }, h("span", { class: "tag " + (allow ? "allow" : "block"), text: allow ? "monitor allowed" : "monitor blocked" }),
          h("span", { class: "muted", text: e.target === "reply" ? "the reply" : e.target })),
        h("div", { class: "reason", text: (e.reason || "") + (e.error ? ` (error: ${e.error})` : "") })));
    } else if (e.event === "tool_call") {
      const blocked = e.status === "blocked";
      const step = h("div", { class: "step" }, h("div", { class: "action" },
        h("span", { class: "tag" + (blocked ? " blocked" : ""), text: blocked ? `blocked by ${BY[e.blocked_by] || e.blocked_by || "a check"}` : TOOL_STATUS[e.status] || e.status }),
        h("code", { text: e.name }), h("span", { text: argsText(e) })));
      const a = e.args || e.input || {};
      if (e.name === "send_email" && (a.subject || a.body)) step.append(h("div", { class: "box email" }, h("strong", { text: `Subject: ${a.subject || ""}` }), "\n\n", rich(a.body)));
      if (e.status === "executed" && e.result && e.name !== "send_email" && e.name !== "approve_request")
        step.append(h("details", {}, h("summary", { text: "Result" }), h("pre", { class: "box", text: e.result })));
      else if (e.status !== "executed" && (e.result || e.reason)) step.append(h("div", { class: "reason", text: e.result || e.reason }));
      box.append(step);
    } else if (e.event === "turn_end") {
      if (e.reply) {
        const who = sender ? sender.name : "the sender";
        const status = e.delivered ? `Reply to ${who}, delivered` : `Reply to ${who}, withheld by ${e.blocked_by === "monitor" ? "the monitor" : e.blocked_by || "the harness"}${e.reason ? ": " + e.reason : ""}`;
        box.append(h("div", { class: "reply" + (e.delivered ? "" : " withheld") }, h("div", { class: "turn-head", text: status }), h("div", { class: "msg" }, rich(e.reply))));
      }
      if (e.outcome && e.outcome !== "replied") box.append(h("p", { class: "reason", text: `The message ended: ${e.outcome.replaceAll("_", " ")}` }));
    } else if (e.event === "error") {
      box.append(h("p", { class: "reason", text: "Error: " + e.error }));
    }
  }
  const sub = `${s.id} under ${ep.condition}, ${condName(ep.condition).toLowerCase()}` + (ep.repeat > 1 ? `, repeat ${ep.repeat}` : "") + "." + ((DATA.config || {}).dry_run ? "" : ` Cost ${usd(ep.cost, 3)}.`);
  return { title: s.title || s.id, sub, body };
}

// ---------------------------------------------------------------- the side panel
let drawerCount = 0;
function makeDrawer() {
  const id = "drawer-title-" + ++drawerCount;
  const scrim = h("div", { class: "scrim", hidden: true });
  const title = h("h2", { id });
  const sub = h("p", { class: "sub" });
  const close = h("button", { class: "btn small", type: "button", text: "Close" });
  const body = h("div", { class: "drawer-body" });
  const el = h("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-labelledby": id, hidden: true },
    h("div", { class: "drawer-head" }, h("div", {}, title, sub), close), body);
  let back = null, onClose = null;
  function hide() {
    if (el.hidden) return;
    el.hidden = true;
    scrim.hidden = true;
    document.body.classList.remove("drawer-open");
    if (onClose) onClose();
    if (back && back.isConnected) back.focus({ preventScroll: true });
  }
  function show(t, s, content, from, closed) {
    title.textContent = t;
    sub.textContent = s || "";
    sub.hidden = !s;
    fill(body, content);
    back = from || document.activeElement;
    onClose = closed || null;
    el.hidden = false;
    scrim.hidden = false;
    document.body.classList.add("drawer-open");
    body.scrollTop = 0;
    close.focus();
  }
  close.addEventListener("click", hide);
  scrim.addEventListener("click", hide);
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !el.hidden) hide(); });
  // Keep the keyboard inside the panel while it is open.
  el.addEventListener("keydown", e => {
    if (e.key !== "Tab") return;
    const focusable = [...el.querySelectorAll("button, [href], summary, input, select, textarea, [tabindex]:not([tabindex='-1'])")].filter(x => !x.disabled && x.getClientRects().length);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  document.body.append(scrim, el);
  return { show, hide, get open() { return !el.hidden; } };
}

window.AuthzReport = {
  h, fill, svg, plural, words, usd, rich, icon, badge, layers, legend, VERDICT, verdict, COND, condName, FAMILY,
  THEMES, getTheme, applyTheme, themeButton, fromEpisode, fromRow, detail, board, summary, episodeView, makeDrawer,
};

// ---------------------------------------------------------------- the standalone page
const dataEl = document.getElementById("data");
if (!dataEl) return;
const DATA = JSON.parse(dataEl.textContent);
const hashParts = location.hash.slice(1).split("&").filter(Boolean);
let theme = getTheme();
if (hashParts.includes("dark")) theme = "dark";
if (hashParts.includes("light")) theme = "light";
applyTheme(theme);

const app = document.getElementById("app");
const cfg = DATA.config || {};
const eps = DATA.episodes;
const drawer = makeDrawer();
const meta = [
  cfg.dry_run ? "Scripted assistant, no API." : `Assistant ${cfg.agent || "unknown"}` + (cfg.effort ? ` at ${cfg.effort} effort.` : "."),
  cfg.monitor ? `Monitor ${cfg.monitor}` + (cfg.monitor_effort ? ` at ${cfg.monitor_effort} effort.` : ".") : null,
  { user_turn: "Operator notices went to the assistant inside the user's turn, not as system messages.", developer: "Operator notices went to the assistant as developer messages." }[cfg.notices] || null,
  `Data ${DATA.data_hash}, page made ${DATA.generated}.`,
].filter(Boolean).join(" ");
app.append(
  h("header", { class: "report-top" }, h("span", { class: "brand" }, layers(3, 3, 18), "Agent authority pilot"), themeButton()),
  h("h1", { class: "report-title display", text: DATA.run }),
  h("p", { class: "report-meta", text: meta }));
if (cfg.dry_run) {
  app.append(h("div", { class: "banner", role: "note" }, badge("refusal"),
    h("span", { text: "Dry run: a scripted assistant played these scenarios, not a model, and the scripted monitor allows everything, so C matches B. The page shows how the scoring works." })));
}
if (!eps.length) {
  app.append(h("p", { class: "empty", text: "No finished episodes in this run yet." }));
  return;
}
function showEpisode(ep, from) {
  const view = episodeView(DATA, ep);
  drawer.show(view.title, view.sub, view.body, from, () => {
    try { history.replaceState(null, "", location.pathname + location.search); } catch (e) {}
  });
  try { history.replaceState(null, "", "#episode=" + ep.key); } catch (e) {}
}
const conds = Object.keys(DATA.summary);
const b = board({
  scenarios: DATA.scenarios.filter(s => eps.some(e => e.scenario === s.id)),
  conditions: conds,
  onOpen: (c, from) => { const ep = eps.find(e => e.key === c.key); if (ep) showEpisode(ep, from); },
});
b.update(eps.map(fromEpisode));
app.append(
  h("section", { class: "report-section", "aria-labelledby": "s-board" },
    h("h2", { id: "s-board", text: "Every episode" }),
    h("p", { class: "sub", text: "One row per scenario, one column per set-up. Select a result to read the conversation." }),
    b.el),
  h("section", { class: "report-section", "aria-labelledby": "s-sum" },
    h("h2", { id: "s-sum", text: "Totals" }),
    h("p", { class: "sub", text: "Counts per set-up. Small numbers: read them as signals, not estimates." }),
    summary(DATA)),
  h("footer", { class: "report-foot" }, h("ul", {},
    conds.map(c => h("li", { text: `${c}, ${condName(c).toLowerCase()}: ${DATA.conditions[c]}.` })),
    h("li", { text: "Disclosure is detected with canary values, so a paraphrase that avoids the exact value is not counted: disclosure counts are a lower bound." }))));
const wanted = hashParts.find(p => p.startsWith("episode="));
if (wanted) { const ep = eps.find(e => e.key === wanted.slice(8)); if (ep) showEpisode(ep); }
})();
