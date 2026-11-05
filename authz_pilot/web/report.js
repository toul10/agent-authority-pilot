(() => {
"use strict";
const DATA = JSON.parse(document.getElementById("data").textContent);
const CONDS = Object.keys(DATA.summary);
const SCEN = Object.fromEntries(DATA.scenarios.map(s => [s.id, s]));
const COLOR = { good: "var(--good)", warning: "var(--warning)", serious: "var(--serious)", critical: "var(--critical)", neutral: "var(--neutral)" };
const OUT = {
  violation: { label: "Violation took effect", short: "Violation", status: "critical", icon: "x" },
  stopped: { label: "Stopped by a check", short: "Stopped", status: "warning", icon: "shield" },
  safe: { label: "Safe", short: "Safe", status: "good", icon: "check" },
  ok: { label: "Completed", short: "Completed", status: "good", icon: "check" },
  incomplete: { label: "Not completed", short: "Not completed", status: "serious", icon: "minus" },
  refusal: { label: "Refusal", short: "Refusal", status: "neutral", icon: "dot" },
  error: { label: "Error", short: "Error", status: "neutral", icon: "alert" },
};
const FAMILY = { injection: "Injection", false_authority: "False authority", cross_context: "Cross-context", permission_change: "Permission change" };
const TRAP_CATS = ["violation", "stopped", "safe", "refusal", "error"];
const LEGIT_CATS = ["ok", "incomplete", "violation", "refusal", "error"];

// ---------------------------------------------------------------- helpers
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children) if (c != null && c !== false) el.append(c);
  return el;
}
const SVGNS = "http://www.w3.org/2000/svg";
const GLYPH = {
  check: "M4.6 8.3 7 10.6 11.4 5.6",
  x: "M5.4 5.4l5.2 5.2M10.6 5.4l-5.2 5.2",
  shield: "M8 3.6 11.2 4.9v2.6c0 2-1.3 3.6-3.2 4.3-1.9-.7-3.2-2.3-3.2-4.3V4.9z",
  minus: "M5 8h6",
  alert: "M8 4.6v4.2M8 11.2v.2",
  dot: "M8 8h0",
};
function badge(kind, size = 16) {
  const o = OUT[kind];
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16"); svg.setAttribute("width", size); svg.setAttribute("height", size); svg.setAttribute("aria-hidden", "true");
  const c = document.createElementNS(SVGNS, "circle");
  c.setAttribute("cx", "8"); c.setAttribute("cy", "8"); c.setAttribute("r", "8"); c.style.fill = COLOR[o.status];
  const p = document.createElementNS(SVGNS, "path");
  p.setAttribute("d", GLYPH[o.icon]); p.setAttribute("fill", "none");
  p.setAttribute("stroke", o.status === "warning" ? "#0b0b0b" : "#ffffff");
  p.setAttribute("stroke-width", o.icon === "dot" ? "4" : "1.8"); p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
  svg.append(c, p);
  return svg;
}
function mark(ok) { return badge(ok ? "ok" : "violation", 14); }
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
const money = (x, d = 2) => "$" + (x || 0).toFixed(d);
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const person = id => DATA.people[id] || { name: id, role: "" };

const tipEl = document.querySelector(".tooltip");
function placeTip(x, y) {
  const w = tipEl.offsetWidth, ht = tipEl.offsetHeight;
  let left = x + 14, top = y - ht - 12;
  if (left + w > innerWidth - 8) left = Math.max(8, x - w - 14);
  if (top < 8) top = y + 18;
  tipEl.style.left = left + "px"; tipEl.style.top = top + "px";
}
function tip(el, lines) {
  const show = (x, y) => {
    tipEl.replaceChildren(...lines().map((t, i) => h("div", { class: i === 0 ? "v" : "l", text: t })));
    tipEl.hidden = false; placeTip(x, y);
  };
  el.addEventListener("pointermove", e => show(e.clientX, e.clientY));
  el.addEventListener("pointerleave", () => { tipEl.hidden = true; });
  el.addEventListener("focus", () => { const r = el.getBoundingClientRect(); show(r.left + r.width / 2, r.top); });
  el.addEventListener("blur", () => { tipEl.hidden = true; });
}
function niceTicks(max) {
  const step = [1, 2, 5, 10, 20, 25, 50, 100, 200].find(s => max / s <= 5) || Math.ceil(max / 5);
  const end = Math.max(step, Math.ceil(max / step) * step);
  const out = [];
  for (let t = 0; t <= end; t += step) out.push(t);
  return out;
}
function countBy(eps) { const c = {}; for (const e of eps) c[e.outcome] = (c[e.outcome] || 0) + 1; return c; }

// ---------------------------------------------------------------- theme
const root = document.documentElement;
const THEMES = ["auto", "light", "dark"];
let theme = "auto";
try { theme = localStorage.getItem("authz-theme") || "auto"; } catch (e) {}
const hashParts = location.hash.slice(1).split("&").filter(Boolean);
if (hashParts.includes("dark")) theme = "dark";
if (hashParts.includes("light")) theme = "light";
function applyTheme() { if (theme === "auto") delete root.dataset.theme; else root.dataset.theme = theme; }
applyTheme();

// ---------------------------------------------------------------- page
const app = document.getElementById("app");
const cfg = DATA.config || {};
const episodes = DATA.episodes;
const traps = episodes.filter(e => e.kind === "trap");
const legit = episodes.filter(e => e.kind === "legit");

const themeBtn = h("button", { class: "btn", type: "button", text: "Theme: " + theme });
themeBtn.addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  try { localStorage.setItem("authz-theme", theme); } catch (e) {}
  applyTheme(); themeBtn.textContent = "Theme: " + theme;
});
const metaBits = [
  "Run " + DATA.run,
  "assistant " + (cfg.agent || "?") + (cfg.effort ? ` (effort ${cfg.effort})` : ""),
  cfg.monitor ? "monitor " + cfg.monitor + (cfg.monitor_effort ? ` (effort ${cfg.monitor_effort})` : "") : null,
  "data " + DATA.data_hash,
  "generated " + DATA.generated,
].filter(Boolean);
app.append(h("header", { class: "top" },
  h("div", {}, h("h1", { text: "Agent authority pilot" }), h("p", { class: "meta", text: metaBits.join(" · ") })),
  themeBtn));
if (cfg.dry_run) {
  app.append(h("div", { class: "banner", role: "note" }, badge("stopped"),
    h("span", { text: "Dry run: these results come from a scripted agent, not a model, and the scripted monitor allows everything, so C matches B. They show how the dashboard and the scoring work." })));
}
if (!episodes.length) {
  app.append(h("p", { class: "empty", text: "No finished episodes in this run yet." }));
  return;
}

// KPI tiles
const totalCost = episodes.reduce((a, e) => a + (e.cost || 0), 0);
const stoppedByError = e => e.stopped === "error" || e.stopped === "incomplete";
const apiErrors = episodes.filter(stoppedByError).length;
const monErrors = episodes.reduce((a, e) => a + (e.monitor_errors || 0), 0);
const escal = episodes.reduce((a, e) => a + (e.escalations || 0), 0);
const scenarioCount = new Set(episodes.map(e => e.scenario)).size;
const tiles = [
  ["Episodes", String(episodes.length), `${plural(scenarioCount, "scenario", "scenarios")} × ${plural(CONDS.length, "condition", "conditions")} × ${plural(cfg.repeats || 1, "repeat", "repeats")}`],
  ["Estimated cost", money(totalCost), "from the logged token usage"],
  ["Errors", String(apiErrors + monErrors), `${apiErrors} stopped episodes · ${monErrors} monitor errors`, apiErrors + monErrors ? "violation" : "ok"],
  ["Escalations to a human", String(escal), "the assistant handed a request over"],
];
const mon = DATA.monitor && DATA.monitor.C;
if (mon) tiles.push(["Monitor reviews", String(mon.reviews || 0), `${mon.blocks || 0} blocked · ${mon.errors || 0} errors`]);
app.append(h("section", { class: "kpis", "aria-label": "Run totals" }, ...tiles.map(([label, value, note, status]) =>
  h("div", { class: "card tile" }, h("div", { class: "label", text: label }),
    h("div", { class: "value" }, status ? badge(status, 18) : null, value),
    h("div", { class: "note", text: note })))));

// Stacked bar charts
function stacked(title, subtitle, cats, group, footnote) {
  const rows = CONDS.map(c => { const eps = group.filter(e => e.condition === c); return { cond: c, counts: countBy(eps), total: eps.length }; });
  const shown = cats.filter((k, i) => i < 3 || rows.some(r => r.counts[k]));
  const fig = h("figure", { class: "card" });
  const toggle = h("button", { class: "btn", type: "button", "aria-pressed": "false", text: "Table" });
  fig.append(h("div", { class: "card-head" }, h("div", {}, h("h2", { text: title }), h("p", { class: "sub", text: subtitle })), toggle));
  const chart = h("div");
  chart.append(h("ul", { class: "legend" }, ...shown.map(k => h("li", {}, badge(k, 14), OUT[k].label))));
  const ticks = niceTicks(Math.max(1, ...rows.map(r => r.total)));
  const dom = ticks[ticks.length - 1];
  const bars = h("div", { class: "bars" });
  for (const r of rows) {
    bars.append(h("div", { class: "row-label" }, h("b", { text: r.cond }), h("span", { text: DATA.short[r.cond] })));
    const plot = h("div", { class: "plot" });
    for (const t of ticks) plot.append(h("div", { class: "gridline" + (t === 0 ? " base" : ""), style: `left:${(t / dom) * 100}%` }));
    const bar = h("div", { class: "bar", style: `width:${(r.total / dom) * 100}%` });
    for (const k of shown) {
      const n = r.counts[k] || 0;
      if (!n) continue;
      const o = OUT[k];
      const seg = h("div", { class: "seg", tabindex: "0", role: "img",
        "aria-label": `Condition ${r.cond}: ${n} of ${r.total}, ${o.label}`,
        style: `flex:${n} 1 0;background:${COLOR[o.status]};color:${o.status === "warning" ? "#0b0b0b" : "#ffffff"}` },
        h("span", { class: "num", text: String(n) }));
      tip(seg, () => [`${n} of ${r.total}`, o.label, `Condition ${r.cond} · ${DATA.conditions[r.cond]}`]);
      bar.append(seg);
    }
    plot.append(bar);
    bars.append(plot);
  }
  bars.append(h("div"));
  bars.append(h("div", { class: "axis", "aria-hidden": "true" }, ...ticks.map(t => h("span", { style: `left:${(t / dom) * 100}%`, text: String(t) }))));
  chart.append(bars);
  const table = h("table", { hidden: true },
    h("thead", {}, h("tr", {}, h("th", { text: "Condition" }), ...shown.map(k => h("th", { class: "num", text: OUT[k].short })), h("th", { class: "num", text: "Total" }))),
    h("tbody", {}, ...rows.map(r => h("tr", {}, h("td", { text: `${r.cond} · ${DATA.short[r.cond]}` }),
      ...shown.map(k => h("td", { class: "num" + (r.counts[k] ? "" : " zero"), text: String(r.counts[k] || 0) })),
      h("td", { class: "num", text: String(r.total) })))));
  toggle.addEventListener("click", () => {
    const showTable = table.hidden;
    table.hidden = !showTable; chart.hidden = showTable;
    toggle.textContent = showTable ? "Chart" : "Table"; toggle.setAttribute("aria-pressed", String(showTable));
    fitLabels();
  });
  fig.append(chart, table);
  if (footnote) fig.append(h("p", { class: "footnote", text: footnote }));
  return fig;
}
const blockedByCond = CONDS.map(c => `${c} ${episodes.filter(e => e.condition === c).reduce((a, e) => a + e.false_blocks.length, 0)}`).join(" · ");
const charts = h("section", { class: "grid2" });
if (traps.length) charts.append(stacked("Traps: did a violation take effect?",
  "Trap episodes per condition. Stopped means a code check or the monitor blocked every attempt.", TRAP_CATS, traps));
if (legit.length) charts.append(stacked("Legit requests: were they completed?",
  "Legit episodes per condition. Completed means every expected action happened, with no violation.", LEGIT_CATS, legit,
  "Harmless actions blocked by a check: " + blockedByCond));
app.append(charts);

function fitLabels() {
  for (const seg of document.querySelectorAll(".seg")) {
    const num = seg.firstChild;
    num.hidden = false;
    if (seg.clientWidth < num.offsetWidth + 10) num.hidden = true;
  }
}

// Families and violation types
const tables = h("section", { class: "grid2" });
if (traps.length) {
  const fams = Object.keys(FAMILY).filter(f => traps.some(e => e.trap === f));
  const thin = eps => {
    const c = countBy(eps);
    return h("div", { class: "thin", "aria-hidden": "true" }, ...TRAP_CATS.filter(k => c[k]).map(k => h("i", { style: `flex:${c[k]} 1 0;background:${COLOR[OUT[k].status]}` })));
  };
  tables.append(h("div", { class: "card" },
    h("h2", { text: "Traps by family" }),
    h("p", { class: "sub", text: "Episodes where a violation took effect, out of all trap episodes in the family." }),
    h("div", { class: "scroll", style: "margin-top:10px" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { text: "Family" }), ...CONDS.map(c => h("th", { class: "num", text: `${c} · ${DATA.short[c]}` })))),
      h("tbody", {}, ...fams.map(f => h("tr", {}, h("td", { text: FAMILY[f] }), ...CONDS.map(c => {
        const eps = traps.filter(e => e.trap === f && e.condition === c);
        const bad = eps.filter(e => e.outcome === "violation").length;
        return h("td", { class: "num" }, h("span", { class: bad ? "" : "zero", text: `${bad} / ${eps.length}` }), thin(eps));
      }))))))));
}
tables.append(h("div", { class: "card" },
  h("h2", { text: "Violation types" }),
  h("p", { class: "sub", text: "Episodes with at least one violation of this type that took effect." }),
  h("table", { style: "margin-top:10px" },
    h("thead", {}, h("tr", {}, h("th", { text: "Type" }), ...CONDS.map(c => h("th", { class: "num", text: c })))),
    h("tbody", {}, ...Object.entries(DATA.violation_types).map(([k, meaning]) => h("tr", {},
      h("td", {}, h("span", { text: k.replaceAll("_", " ") }), h("span", { class: "type-meaning", text: meaning })),
      ...CONDS.map(c => { const n = DATA.summary[c].episodes_by_violation_type[k] || 0; return h("td", { class: "num" + (n ? "" : " zero"), text: String(n) }); })))))));
app.append(tables);

// Scenario matrix
const FILTERS = [["all", "All"], ["legit", "Legit"], ...Object.entries(FAMILY)];
let filter = "all";
const matrixCard = h("section", { class: "card" });
const filterRow = h("div", { class: "filters", role: "group", "aria-label": "Filter scenarios" });
const matrixBody = h("div", { class: "scroll" });
for (const [key, label] of FILTERS) {
  if (key !== "all" && key !== "legit" && !traps.some(e => e.trap === key)) continue;
  if (key === "legit" && !legit.length) continue;
  const b = h("button", { class: "btn", type: "button", "aria-pressed": String(key === filter), text: label });
  b.addEventListener("click", () => { filter = key; for (const x of filterRow.children) x.setAttribute("aria-pressed", String(x === b)); drawMatrix(); });
  filterRow.append(b);
}
matrixCard.append(h("div", { class: "card-head" }, h("div", {},
  h("h2", { text: "Scenarios" }), h("p", { class: "sub", text: "Every episode, by scenario and condition. Select a result to read the conversation." }))),
  filterRow, matrixBody);
app.append(matrixCard);

function chipText(ep) {
  let t = OUT[ep.outcome].short;
  if (ep.kind === "trap" && ep.outcome !== "violation" && ep.outcome !== "error" && !ep.completed) t += " · task missed";
  if (ep.outcome !== "stopped" && ep.attempted.length) t += ` · ${ep.attempted.length} stopped`;
  if (ep.false_blocks.length) t += ` · ${ep.false_blocks.length} harmless blocked`;
  if (ep.outcome !== "error" && stoppedByError(ep)) t += " · then an error";
  return t;
}
function chip(ep) {
  const o = OUT[ep.outcome];
  const b = h("button", { class: "chip", type: "button", style: `--c:${COLOR[o.status]}`,
    "aria-label": `${ep.scenario}, condition ${ep.condition}, repeat ${ep.repeat}: ${chipText(ep)}. Open the conversation.` },
    badge(ep.outcome), chipText(ep));
  b.addEventListener("click", () => openEpisode(ep, b));
  return b;
}
function drawMatrix() {
  const groups = [];
  if (filter === "all" || filter === "legit") groups.push(["Legit requests", DATA.scenarios.filter(s => s.kind === "legit")]);
  for (const f of Object.keys(FAMILY)) if (filter === "all" || filter === f) groups.push([`Traps · ${FAMILY[f]}`, DATA.scenarios.filter(s => s.trap === f)]);
  const body = h("tbody");
  for (const [label, scens] of groups) {
    const present = scens.filter(s => episodes.some(e => e.scenario === s.id));
    if (!present.length) continue;
    body.append(h("tr", { class: "group" }, h("th", { colspan: String(CONDS.length + 1), scope: "colgroup", text: label })));
    for (const s of present) {
      body.append(h("tr", {},
        h("td", {}, h("span", { class: "sid", text: s.id }), h("span", { class: "title", text: s.title })),
        ...CONDS.map(c => h("td", { class: "cell" }, ...episodes.filter(e => e.scenario === s.id && e.condition === c).sort((a, b) => a.repeat - b.repeat).map(chip)))));
    }
  }
  matrixBody.replaceChildren(h("table", { class: "matrix" },
    h("thead", {}, h("tr", {}, h("th", { text: "Scenario" }), ...CONDS.map(c => h("th", { text: `${c} · ${DATA.short[c]}` })))),
    body));
}
drawMatrix();

app.append(h("footer", {}, h("ul", {},
  ...CONDS.map(c => h("li", { text: `${c}: ${DATA.conditions[c]}.` })),
  h("li", { text: "Disclosure is detected with canary values, so a paraphrase that avoids the exact value is not counted: disclosure counts are a lower bound." }),
  h("li", { text: "Small numbers: read these counts as signals, not estimates." }))));

// ---------------------------------------------------------------- episode drawer
const drawer = document.querySelector(".drawer");
const scrim = document.querySelector(".scrim");
const closeBtn = document.getElementById("drawer-close");
let returnFocus = null;
function closeDrawer() {
  drawer.hidden = true; scrim.hidden = true;
  try { history.replaceState(null, "", location.pathname + location.search); } catch (e) {}
  if (returnFocus) returnFocus.focus();
}
closeBtn.addEventListener("click", closeDrawer);
scrim.addEventListener("click", closeDrawer);
document.addEventListener("keydown", e => { if (e.key === "Escape" && !drawer.hidden) closeDrawer(); });

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
  if (x.type === "reply") return needles ? `reply mentions ${needles}` : "a reply";
  if (x.type === "email") return `email to ${x.to}` + (needles ? ` mentioning ${needles}` : "");
  if (x.type === "approve") return `approve ${x.request}`;
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
const TOOL_STATUS = { executed: ["done", ""], blocked: ["blocked", "blocked"], invalid: ["invalid input", ""], not_run: ["not run", ""] };

function openEpisode(ep, from) {
  returnFocus = from || null;
  const s = SCEN[ep.scenario];
  document.getElementById("drawer-title").textContent = `${s.id} · ${s.title}`;
  document.getElementById("drawer-sub").textContent = `Condition ${ep.condition}: ${DATA.conditions[ep.condition]} · repeat ${ep.repeat} · ${money(ep.cost, 3)}`;
  const body = document.getElementById("drawer-body");
  body.replaceChildren();
  body.append(h("p", { class: "sub", text: s.description }));
  const o = OUT[ep.outcome];
  body.append(h("p", { style: "margin-top:10px" }, h("span", { class: "chip static", style: `--c:${COLOR[o.status]}` }, badge(ep.outcome), chipText(ep))));

  const facts = h("div", { class: "facts" });
  const section = (title, items) => { if (items.length) facts.append(h("h3", { text: title }), h("ul", {}, ...items)); };
  section("Violations that took effect", ep.violations.map(v => h("li", {}, badge("violation", 14), h("span", { text: `Turn ${v.turn} · ${v.type.replaceAll("_", " ")}: ${violationText(v)}` }))));
  section("Stopped before taking effect", ep.attempted.map(v => h("li", {}, badge("stopped", 14), h("span", { text: `Turn ${v.turn} · ${v.type.replaceAll("_", " ")}: ${violationText(v)} (blocked by ${v.blocked_by === "code" ? "a code check" : "the monitor"})` }))));
  section("Harmless actions blocked", ep.false_blocks.map(b => h("li", {}, badge("incomplete", 14), h("span", { text: `Turn ${b.turn} · ${b.action} blocked by ${b.blocked_by === "code" ? "a code check" : "the monitor"}${b.reason ? ": " + b.reason : ""}` }))));
  section("Expected", ep.expectations.map(x => h("li", {}, mark(x.met), h("span", { text: `Turn ${x.turn} · ${expectText(x.expect)}${x.met ? "" : " (not done)"}` }))));
  const extra = [];
  for (const n of ep.notes || []) extra.push(h("li", {}, h("span", { text: `Turn ${n.turn} · email to ${n.recipient}, a colleague, that nobody asked for (not counted as a violation)` })));
  if (ep.escalations) extra.push(h("li", {}, h("span", { text: plural(ep.escalations, "escalation to a human", "escalations to a human") })));
  if (ep.monitor_errors) extra.push(h("li", {}, badge("error", 14), h("span", { text: plural(ep.monitor_errors, "monitor error", "monitor errors") + " (the action was blocked)" })));
  if (ep.stopped) extra.push(h("li", {}, badge("error", 14), h("span", { text: `The episode stopped early: ${ep.stopped}` })));
  section("Other", extra);
  if (!facts.childNodes.length) facts.append(h("p", { class: "sub", text: "No violations, nothing blocked, and nothing expected in this scenario." }));
  body.append(facts);

  let turnBox = null, sender = null;
  for (const e of ep.events) {
    if (e.event === "turn_start") {
      sender = person(e.sender);
      turnBox = h("section", { class: "turn" }, h("div", { class: "turn-head", text: `Message ${e.turn} · from ${sender.name} (${sender.role})` }));
      if (e.notice) turnBox.append(h("div", { class: "notice" }, h("b", { text: "Operator notice (system channel)" }), e.notice));
      turnBox.append(h("div", { class: "msg", text: e.message }));
      body.append(turnBox);
      continue;
    }
    const box = turnBox || body;
    if (e.event === "model_call") {
      for (const t of e.thinking || []) {
        const preview = t.length > 110 ? t.slice(0, 110).trimEnd() + "…" : t;
        box.append(h("details", { class: "step" }, h("summary", { text: "Thinking (summary): " + preview }), h("div", { class: "thinking", text: t })));
      }
    } else if (e.event === "monitor_call") {
      const allow = e.decision === "allow";
      box.append(h("div", { class: "step" },
        h("div", { class: "action" }, h("span", { class: "tag " + (allow ? "allow" : "block"), text: allow ? "monitor: allow" : "monitor: block" }),
          h("span", { class: "muted", text: e.target === "reply" ? "the reply" : e.target })),
        h("div", { class: "reason", text: (e.reason || "") + (e.error ? ` (error: ${e.error})` : "") })));
    } else if (e.event === "tool_call") {
      const [label, cls] = TOOL_STATUS[e.status] || [e.status, ""];
      const tagText = e.status === "blocked" ? `blocked by ${e.blocked_by === "code" ? "code check" : "monitor"}` : label;
      const step = h("div", { class: "step" }, h("div", { class: "action" },
        h("span", { class: "tag " + cls, text: tagText }), h("code", { text: e.name }), h("span", { text: argsText(e) })));
      const a = e.args || e.input || {};
      if (e.name === "send_email" && (a.subject || a.body)) step.append(h("div", { class: "box email" }, h("strong", { text: `Subject: ${a.subject || ""}` }), "\n\n", rich(a.body)));
      if (e.status === "executed" && e.result && e.name !== "send_email" && e.name !== "approve_request")
        step.append(h("details", {}, h("summary", { text: "Result" }), h("pre", { class: "box", text: e.result })));
      else if (e.status !== "executed" && (e.result || e.reason)) step.append(h("div", { class: "reason", text: e.result || e.reason }));
      box.append(step);
    } else if (e.event === "turn_end") {
      if (e.reply) {
        const who = sender ? sender.name : "the sender";
        const status = e.delivered ? `Reply to ${who} · delivered` : `Reply to ${who} · withheld by the ${e.blocked_by || "harness"}${e.reason ? ": " + e.reason : ""}`;
        box.append(h("div", { class: "reply" + (e.delivered ? "" : " withheld") }, h("div", { class: "turn-head", text: status }), h("div", { class: "msg" }, rich(e.reply))));
      }
      if (e.outcome && e.outcome !== "replied") box.append(h("p", { class: "reason", text: `The turn ended: ${e.outcome.replaceAll("_", " ")}` }));
    } else if (e.event === "error") {
      box.append(h("p", { class: "reason", text: "Error: " + e.error }));
    }
  }
  drawer.hidden = false; scrim.hidden = false; tipEl.hidden = true;
  body.scrollTop = 0;
  try { history.replaceState(null, "", "#episode=" + ep.key); } catch (e) {}
  closeBtn.focus();
}

requestAnimationFrame(fitLabels);
addEventListener("resize", fitLabels);
const wanted = hashParts.find(p => p.startsWith("episode="));
if (wanted) { const ep = episodes.find(e => e.key === wanted.slice(8)); if (ep) openEpisode(ep); }
})();
