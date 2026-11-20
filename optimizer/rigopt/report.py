"""Self-contained HTML report (spec §9), v2 layout.

Schedule (Gantt) first; Overview one click away; the other views in a menu.
No external resources; all data embedded as strict JSON.
"""
from __future__ import annotations

import json
import math
from pathlib import Path


def _compact(an: dict) -> dict:
    sched = [[r["well_id"], r["phase"], r["rig_id"], r["start_date"], r["end_date"], r["asset"], r["category"],
              r["winter_tier"], r["base_duration_days"], r["effective_duration_days"], r["sail_days_in"],
              int(r["is_frozen"]), round(r["value"] / 1e6, 2), r["purpose"], r["name"], round(r.get("wow_cost", 0.0), 0)]
             for r in an["schedule"]]
    out = {k: an[k] for k in ("kpis", "exploration", "unscheduled", "infeasibility", "bottlenecks",
                               "weather", "campaigns")}
    ec = an["economics"]
    out["economics"] = {k: ec[k] for k in ("total", "npv_development", "emv_exploration", "yearly", "by_asset",
                                            "by_rig", "by_category")}
    out["schedule"] = sched
    for k in ("comparison", "discovery_risk", "monte_carlo"):
        if k in an:
            out[k] = an[k]
    return out


def _clean(x):
    """Strict JSON: NaN/inf -> null, numpy scalars -> plain numbers."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):
        x = x.item()
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def write_report(path: Path, meta: dict, variants: list):
    data = _clean(dict(meta=meta, variants=[_compact(v) for v in variants]))
    payload = json.dumps(data, allow_nan=False, separators=(",", ":")).replace("</", "<\\/")
    Path(path).write_text(TEMPLATE.replace("__DATA__", payload), encoding="utf-8")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rig schedule optimizer</title>
<style>
:root{color-scheme:light;--bg:#f6f6f4;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#8a8984;--line:#e4e3df;--grid:#efeeea;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948;
--winter:rgba(42,120,214,.08);--unavail:#d3d2cd;--good:#0a7d32;--bad:#c2261f;--badbg:#fdecea;--warn:#9a6700;--warnbg:#fff4d6;--accent:#2a78d6;--onbar:#ffffff;--onbar-dark:#0b0b0b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#121211;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#8f8e86;--line:#2e2e2b;--grid:#252523;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#0ca30c;--s7:#9085e9;--s8:#e66767;--winter:rgba(57,135,229,.12);--unavail:#3d3d3a;--good:#3fb950;--bad:#f0605a;--badbg:#3a1d1b;--warn:#d2a23a;--warnbg:#33290f;--accent:#3987e5}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121211;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#8f8e86;--line:#2e2e2b;--grid:#252523;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#0ca30c;--s7:#9085e9;--s8:#e66767;--winter:rgba(57,135,229,.12);--unavail:#3d3d3a;--good:#3fb950;--bad:#f0605a;--badbg:#3a1d1b;--warn:#d2a23a;--warnbg:#33290f;--accent:#3987e5}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header.top{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
header.top h1{font-size:17px;margin:0;white-space:nowrap}
.grow{flex:1}
select,button{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:7px;padding:5px 10px}
button{cursor:pointer}button:hover{border-color:var(--muted)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:none;border-radius:0;background:var(--surface);padding:6px 12px}
.seg button+button{border-left:1px solid var(--line)}
.seg button[aria-pressed="true"]{background:var(--ink);color:var(--bg)}
.menuwrap{position:relative}
#menubtn{font-size:18px;line-height:1;padding:5px 10px}
#menu{position:absolute;right:0;top:calc(100% + 6px);background:var(--surface);border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.15);padding:6px;min-width:230px;z-index:30}
#menu button{display:block;width:100%;text-align:left;border:none;background:none;padding:8px 10px;border-radius:6px}
#menu button:hover,#menu button[aria-current="true"]{background:var(--grid)}
.sub{color:var(--ink2);font-size:12.5px}
#banner{margin:10px 16px 0;padding:10px 14px;border-radius:9px;background:var(--warnbg);border:1px solid var(--warn);color:var(--ink)}
#banner b{color:var(--warn)}
#banner ul{margin:4px 0 0;padding-left:18px}
main{padding:12px 16px 30px}
main.wide{padding:8px 16px 8px}
.wrapc{max-width:1400px;margin:0 auto}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px;overflow:hidden}
.card h2{font-size:15px;margin:0 0 10px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin-bottom:12px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px}
.tile.click{cursor:pointer}.tile.click:hover{border-color:var(--muted)}
.tile .k{color:var(--ink2);font-size:12.5px;display:flex;gap:6px;align-items:center}
.tile .v{font-size:22px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.tile .d{font-size:12px;color:var(--ink2);margin-top:2px}
.tile.cost .v{color:var(--bad)}
.info{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;border:1px solid var(--muted);color:var(--ink2);font-size:10.5px;font-weight:700;cursor:help;flex:none;font-style:normal}
.info:focus{outline:2px solid var(--accent);outline-offset:1px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--ink2);font-weight:600;position:sticky;top:0;background:var(--surface)}
td.n,th.n{text-align:right}
tr.sel td{background:var(--grid)}
tr.pick{cursor:pointer}tr.pick:hover td{background:var(--grid)}
.scroll{overflow:auto;max-height:560px}
.pos{color:var(--good)}.neg{color:var(--bad)}.warnc{color:var(--warn)}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--ink2);align-items:center}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-2px}
ul.plain{margin:0;padding-left:18px}ul.plain li{margin:4px 0}
#tip{position:fixed;pointer-events:none;z-index:50;background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.18);display:none;max-width:340px}
#tip b{font-weight:600}
svg text{fill:var(--ink2);font-size:11px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(440px,1fr));gap:12px}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:8px}
.gantt{display:grid;grid-template-columns:190px 1fr;border:1px solid var(--line);border-radius:10px;background:var(--surface);overflow-y:auto;overflow-x:hidden}
.glabels{border-right:1px solid var(--line);background:var(--surface);position:relative}
.glabels .rl{display:flex;flex-direction:column;justify-content:center;padding:0 10px;border-bottom:1px solid var(--grid)}
.glabels .rl b{font-size:13px;font-weight:600}
.glabels .rl span{font-size:11px;color:var(--muted)}
.gscroll{overflow-x:scroll;overflow-y:hidden;scrollbar-width:auto}
.gscroll::-webkit-scrollbar{height:14px}
.gscroll::-webkit-scrollbar-thumb{background:var(--muted);border-radius:7px;border:3px solid var(--surface)}
.gscroll::-webkit-scrollbar-track{background:var(--grid)}
.bar-label{font-size:11px;font-weight:600;pointer-events:none}
details summary{cursor:pointer;color:var(--ink2);margin-top:8px}
[hidden]{display:none!important}
@media (max-width:640px){.two{grid-template-columns:1fr}.gantt{grid-template-columns:120px 1fr}.tile .v{font-size:18px}header.top h1{font-size:15px}}
</style>
</head>
<body>
<header class="top">
  <h1>Rig schedule</h1>
  <select id="vsel" aria-label="Schedule"></select>
  <span class="info" id="vinfo" tabindex="0" aria-label="About this schedule">i</span>
  <span class="sub grow" id="subtitle"></span>
  <div class="seg" role="group" aria-label="Main views">
    <button id="bSched" aria-pressed="true">Schedule</button>
    <button id="bOver" aria-pressed="false">Overview</button>
  </div>
  <div class="menuwrap">
    <button id="menubtn" aria-haspopup="true" aria-expanded="false" aria-label="More views">☰</button>
    <div id="menu" role="menu" hidden></div>
  </div>
</header>
<div id="banner" role="alert" hidden></div>
<main id="main"></main>
<div id="tip" role="tooltip"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const META = D.meta;
const MENU = [["economics", "Economics"], ["util", "Rig utilization"], ["expl", "Exploration"], ["weather", "Weather (winter)"], ["batch", "Batch campaigns"], ["uns", "Unscheduled wells"], ["risk", "Risk & uncertainty"], ["diff", "Changes vs last approved"], ["about", "About the schedules"]];
let cur = 0, view = "schedule", horizonMonths = 18, ganttDraw = null;
const gz = {ppd: null, mode: "fit", center: null, reset: false};
const $ = (t, a = {}, ...kids) => { const e = document.createElement(t); for (const [k, v] of Object.entries(a)) { if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v); } for (const c of kids) if (c != null) e.append(c); return e; };
const NS = "http://www.w3.org/2000/svg";
const S = (t, a = {}) => { const e = document.createElementNS(NS, t); for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); return e; };
const V = () => D.variants[cur];
const MU = () => V().kpis.money_unit;
const byKind = k => D.variants.find(v => v.kpis.kind === k);
const fM = x => x == null ? "–" : (x / 1e6).toLocaleString("en", {maximumFractionDigits: 1}) + " M";
const fB = x => (x / 1e9).toLocaleString("en", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + " bn";
const fN = (x, d = 0) => x == null ? "–" : Number(x).toLocaleString("en", {maximumFractionDigits: d, minimumFractionDigits: 0});
const sgn = x => (x >= 0 ? "+" : "−") + fM(Math.abs(x));
const DAY = 86400000, toT = s => Date.parse(s + "T00:00:00Z"), iso = t => new Date(t).toISOString().slice(0, 10);
const addMonths = (s, n) => { const d = new Date(toT(s)); d.setUTCMonth(d.getUTCMonth() + n); return d.getTime(); };
const ASSETS = [...new Set(D.variants.flatMap(v => v.schedule.map(r => r[5])))].sort((a, b) => a === "Exploration" ? 1 : b === "Exploration" ? -1 : a.localeCompare(b));
const acol = a => `var(--s${(ASSETS.indexOf(a) % 8) + 1})`;
const DARK_TEXT = new Set([3, 4, 5]); // aqua, yellow, magenta slots need dark text
const aink = a => DARK_TEXT.has((ASSETS.indexOf(a) % 8) + 1) ? "var(--onbar-dark)" : "var(--onbar)";
const tip = document.getElementById("tip");
function showTip(ev, lines) { tip.replaceChildren(...lines.filter(l => l !== "").map((l, i) => { const d = $("div"); if (i === 0) d.append($("b", {text: l})); else d.textContent = l; return d; })); tip.style.display = "block"; moveTip(ev); }
function moveTip(ev) { const w = tip.offsetWidth, h = tip.offsetHeight; let x = (ev.clientX ?? 0) + 14, y = (ev.clientY ?? 0) + 14; if (x + w > innerWidth - 8) x = Math.max(8, x - w - 28); if (y + h > innerHeight - 8) y = Math.max(8, y - h - 28); tip.style.left = x + "px"; tip.style.top = y + "px"; }
function hideTip() { tip.style.display = "none"; }
function hover(el, lines) { el.addEventListener("mouseenter", e => showTip(e, lines())); el.addEventListener("mousemove", moveTip); el.addEventListener("mouseleave", hideTip); }
function info(text) { const i = $("span", {class: "info", tabindex: "0", role: "img", "aria-label": text, text: "i"}); const lines = () => [text.split("\n")[0], ...text.split("\n").slice(1)]; hover(i, lines); i.addEventListener("focus", () => { const r = i.getBoundingClientRect(); showTip({clientX: r.right, clientY: r.bottom}, lines()); }); i.addEventListener("blur", hideTip); return i; }
function table(cols, rows, opts = {}) {
  const t = $("table"); const tr = $("tr");
  for (const c of cols) { const th = $("th", {class: c.n ? "n" : ""}); th.append(document.createTextNode(c.h)); if (c.info) { th.append(document.createTextNode(" ")); th.append(info(c.info)); } tr.append(th); }
  t.append($("thead", {}, tr)); const tb = $("tbody");
  rows.forEach((r, i) => { const row = $("tr", {class: (opts.pick ? "pick " : "") + (opts.sel && opts.sel(r) ? "sel" : "")}); if (opts.pick) row.addEventListener("click", () => opts.pick(r, i)); for (const c of cols) { const v = c.f ? c.f(r) : r[c.k]; const td = $("td", {class: (c.n ? "n " : "") + (c.cls ? c.cls(r) : "")}); td.textContent = v == null ? "" : String(v); row.append(td); } tb.append(row); });
  t.append(tb); const w = $("div", {class: "scroll"}); w.append(t); return w;
}
function card(title, ...kids) { const c = $("div", {class: "card"}); if (title) { const h = $("h2"); if (Array.isArray(title)) h.append(...title); else h.textContent = title; c.append(h); } c.append(...kids.filter(Boolean)); return c; }
function tile(k, v, d, opts = {}) { const t = $("div", {class: "tile" + (opts.cls ? " " + opts.cls : "") + (opts.click ? " click" : "")}); const kk = $("div", {class: "k"}, document.createTextNode(k)); if (opts.info) kk.append(info(opts.info)); t.append(kk, $("div", {class: "v", text: v})); if (d) t.append($("div", {class: "d", text: d})); if (opts.click) { t.addEventListener("click", opts.click); t.setAttribute("role", "button"); t.setAttribute("tabindex", "0"); t.addEventListener("keydown", e => { if (e.key === "Enter") opts.click(); }); } return t; }

// ---------------------------------------------------------------- header & navigation
const sel = document.getElementById("vsel");
D.variants.forEach((v, i) => sel.append($("option", {value: i, text: v.kpis.label})));
sel.addEventListener("change", () => { cur = +sel.value; render(); });
const vinfo = document.getElementById("vinfo");
hover(vinfo, () => [V().kpis.label, V().kpis.description]);
const bSched = document.getElementById("bSched"), bOver = document.getElementById("bOver");
bSched.addEventListener("click", () => go("schedule")); bOver.addEventListener("click", () => go("overview"));
const menu = document.getElementById("menu"), menubtn = document.getElementById("menubtn");
for (const [id, name] of MENU) { const b = $("button", {role: "menuitem", "data-v": id, text: name}); b.addEventListener("click", () => { closeMenu(); go(id); }); menu.append(b); }
menubtn.addEventListener("click", e => { e.stopPropagation(); const open = menu.hidden; menu.hidden = !open; menubtn.setAttribute("aria-expanded", String(open)); });
function closeMenu() { menu.hidden = true; menubtn.setAttribute("aria-expanded", "false"); }
document.addEventListener("click", e => { if (!menu.hidden && !menu.contains(e.target)) closeMenu(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeMenu(); });
function go(v) { view = v; window.scrollTo(0, 0); render(); }

function render() {
  hideTip();
  const v = V(), k = v.kpis;
  document.getElementById("subtitle").textContent = `Plan from ${k.plan_start} · frozen until ${k.freeze_end} · horizon to ${k.horizon_end} · rigs sail at ${META.rig_speed_knots || "per-rig"} knots · values pre-tax ${k.money_unit}`;
  bSched.setAttribute("aria-pressed", String(view === "schedule")); bOver.setAttribute("aria-pressed", String(view === "overview"));
  for (const b of menu.children) b.setAttribute("aria-current", String(b.dataset.v === view));
  const ban = document.getElementById("banner");
  if (v.infeasibility.length) { ban.hidden = false; ban.replaceChildren($("b", {text: `⚠ ${v.infeasibility.length} issue${v.infeasibility.length > 1 ? "s" : ""} in this schedule`})); const ul = $("ul"); v.infeasibility.forEach(s => ul.append($("li", {text: s}))); ban.append(ul); } else ban.hidden = true;
  const m = document.getElementById("main"); m.replaceChildren(); m.className = view === "schedule" ? "wide" : ""; ganttDraw = null;
  const inner = view === "schedule" ? m : m.appendChild($("div", {class: "wrapc"}));
  ({schedule: scheduleView, overview, economics, util, expl, weather, batch, uns, risk, diff, about})[view](inner, v);
}

// ---------------------------------------------------------------- window metrics (horizon filter)
function windowMetrics(v, months) {
  const T0 = toT(v.kpis.plan_start), T1 = months ? addMonths(v.kpis.plan_start, months) : toT(v.kpis.horizon_end);
  const ov = (a, b) => Math.max(0, Math.min(b, T1) - Math.max(a, T0)) / DAY;
  const rigs = {};
  for (const r of META.rigs) {
    const a = Math.max(T0, toT(r.contract_start)), b = Math.min(T1, toT(r.contract_end));
    const avail = Math.max(0, (b - a) / DAY);
    const un = r.unavailability.reduce((s, u) => s + Math.max(0, Math.min(b, toT(u.end)) - Math.max(a, toT(u.start))) / DAY, 0);
    rigs[r.rig_id] = {rig_id: r.rig_id, name: r.name, rig_type: r.rig_type, available: avail, unavailable: un, drilling: 0, weather: 0, sailing: 0, idle: 0};
  }
  let wow = 0; const spud = new Set();
  for (const s of v.schedule) {
    const [wid, ph, rig, sd, ed, , , , base, eff, sail, , , , , wc] = s;
    const a = toT(sd), b = toT(ed), R = rigs[rig]; const o = ov(a, b);
    if (o > 0 && eff > 0) { R.drilling += o * Math.min(1, base / eff); R.weather += o * Math.max(0, (eff - base) / eff); wow += wc * o / eff; }
    R.sailing += ov(a - sail * DAY, a);
    if (ph !== "main" && a >= T0 && a < T1) spud.add(wid);
  }
  let sail = 0, idle = 0;
  for (const R of Object.values(rigs)) { R.idle = Math.max(0, R.available - R.unavailable - R.drilling - R.weather - R.sailing); sail += R.sailing; idle += R.idle; R.util = R.available ? (R.drilling + R.weather) / R.available : 0; }
  return {rigs: Object.values(rigs), sail, idle, wow, spud: spud.size, end: iso(T1)};
}
function horizonControl(onchange) {
  const w = $("div", {class: "toolbar"}); w.append($("span", {class: "sub", text: "Look ahead:"}));
  const seg = $("div", {class: "seg", role: "group", "aria-label": "Look-ahead horizon"});
  for (const [mo, lab] of [[18, "18 months"], [36, "3 years"], [60, "5 years"], [0, "Full horizon"]]) { const b = $("button", {"aria-pressed": String(horizonMonths === mo), text: lab}); b.addEventListener("click", () => { horizonMonths = mo; onchange(); }); seg.append(b); }
  w.append(seg);
  const wm = windowMetrics(V(), horizonMonths);
  w.append($("span", {class: "sub", text: `${V().kpis.plan_start} → ${wm.end}. Applies to sailing, idle days, WoW cost and wells started.`}));
  return w;
}

// ---------------------------------------------------------------- schedule (Gantt)
function scheduleView(m, v) {
  const rigs = META.rigs, T0 = toT(v.kpis.plan_start), T1 = addMonths(v.kpis.horizon_end, 6);
  const days = (T1 - T0) / DAY;
  const tb = $("div", {class: "toolbar"});
  const zseg = $("div", {class: "seg", role: "group", "aria-label": "Zoom"});
  const zooms = [["fit", "Whole plan"], [5 * 365, "5 years"], [2 * 365, "2 years"], [365, "1 year"], [182, "6 months"]];
  for (const [z, lab] of zooms) { const b = $("button", {"aria-pressed": String(gz.mode === z), text: lab}); b.addEventListener("click", () => { gz.mode = z; gz.center = null; gz.reset = true; draw(); }); zseg.append(b); }
  const zin = $("button", {text: "+", "aria-label": "Zoom in"}), zout = $("button", {text: "−", "aria-label": "Zoom out"});
  zin.addEventListener("click", () => zoomBy(1.6)); zout.addEventListener("click", () => zoomBy(1 / 1.6));
  tb.append(zseg, zout, zin);
  const leg = $("div", {class: "legend"});
  for (const a of ASSETS) leg.append($("span", {}, $("span", {class: "sw", style: `background:${acol(a)}`}), document.createTextNode(a)));
  leg.append($("span", {}, $("span", {class: "sw", style: "background:repeating-linear-gradient(45deg,var(--muted) 0 2px,transparent 2px 5px);border:1px solid var(--muted)"}), document.createTextNode("frozen")));
  leg.append($("span", {}, $("span", {class: "sw", style: "background:var(--unavail)"}), document.createTextNode("rig unavailable")));
  leg.append($("span", {}, $("span", {class: "sw", style: "background:var(--winter);border:1px solid var(--line)"}), document.createTextNode("winter")));
  leg.append($("span", {}, $("span", {class: "sw", style: "height:2px;background:var(--ink2)"}), document.createTextNode("sailing")));
  leg.append($("span", {text: "◆ exploration · thin bar = top hole · dark strip = weather-added time"}));
  tb.append(leg);
  m.append(tb);
  const g = $("div", {class: "gantt"}); const labels = $("div", {class: "glabels"}); const sc = $("div", {class: "gscroll"});
  g.append(labels, sc); m.append(g);
  const AX = 38;
  function size() { const top = g.getBoundingClientRect().top; const H = Math.max(320, innerHeight - top - 12); g.style.height = H + "px"; const rowH = Math.max(40, Math.min(110, Math.floor((H - AX - 18) / rigs.length))); return rowH; }
  function zoomBy(f) { const w = sc.clientWidth; const c = (sc.scrollLeft + w / 2) / gz.ppd; gz.mode = "custom"; gz.ppd = Math.min(40, Math.max(w / days, gz.ppd * f)); gz.center = c; draw(); }
  sc.addEventListener("wheel", e => { if (!e.ctrlKey) return; e.preventDefault(); zoomBy(e.deltaY < 0 ? 1.25 : 0.8); }, {passive: false});
  function draw() {
    for (const b of zseg.children) b.setAttribute("aria-pressed", "false");
    const zi = zooms.findIndex(z => z[0] === gz.mode); if (zi >= 0) zseg.children[zi].setAttribute("aria-pressed", "true");
    const rowH = size(), w = sc.clientWidth || 800;
    const oldCenter = gz.center ?? (gz.ppd ? (sc.scrollLeft + w / 2) / gz.ppd : null);
    if (gz.mode === "fit") gz.ppd = w / days; else if (typeof gz.mode === "number") gz.ppd = w / gz.mode;
    const ppd = gz.ppd, W = Math.max(w, Math.ceil(days * ppd)), H = AX + rigs.length * rowH;
    const x = t => (t - T0) / DAY * ppd;
    // labels column
    labels.replaceChildren($("div", {style: `height:${AX}px;border-bottom:1px solid var(--line)`}));
    for (const r of rigs) { const d = $("div", {class: "rl", style: `height:${rowH}px`}); d.append($("b", {text: `${r.rig_id} ${r.name}`}), $("span", {text: r.rig_type + (r.capabilities.includes("HPHT") ? " · HPHT" : "") + (r.capabilities.includes("WELL_TEST") ? " · well test" : "")})); labels.append(d); }
    const svg = S("svg", {width: W, height: H, role: "img", "aria-label": "Rig schedule: wells per rig over time"});
    const defs = S("defs"); const pat = S("pattern", {id: "hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)"}); pat.append(S("rect", {width: 2.2, height: 6, fill: "rgba(255,255,255,.55)"})); defs.append(pat); svg.append(defs);
    // winter shading, months, years
    const y0 = new Date(T0).getUTCFullYear(), y1 = new Date(T1).getUTCFullYear();
    const showMonths = ppd * 30 >= 26, mname = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"], mlong = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    for (let y = y0; y <= y1; y++) for (let mo = 0; mo < 12; mo++) {
      const a = Date.UTC(y, mo, 1), b = Date.UTC(y, mo + 1, 1); if (b <= T0 || a >= T1) continue;
      if (META.winter_months.includes(mo + 1)) svg.append(S("rect", {x: x(Math.max(a, T0)), y: AX, width: x(Math.min(b, T1)) - x(Math.max(a, T0)), height: H - AX, fill: "var(--winter)"}));
      if (mo === 0) { svg.append(S("line", {x1: x(a), x2: x(a), y1: 0, y2: H, stroke: "var(--line)"})); const t = S("text", {x: x(a) + 4, y: 14, style: "font-weight:600;fill:var(--ink)"}); t.textContent = y; svg.append(t); }
      else if (showMonths) svg.append(S("line", {x1: x(a), x2: x(a), y1: 20, y2: H, stroke: "var(--grid)"}));
      if (showMonths) { const t = S("text", {x: x(a) + 3, y: 32}); t.textContent = ppd * 30 >= 40 ? mlong[mo] : mname[mo]; svg.append(t); }
    }
    svg.append(S("line", {x1: 0, x2: W, y1: AX, y2: AX, stroke: "var(--line)"}));
    const fz = toT(v.kpis.freeze_end); svg.append(S("line", {x1: x(fz), x2: x(fz), y1: AX - 4, y2: H, stroke: "var(--ink)", "stroke-dasharray": "5 4"}));
    const ft = S("text", {x: x(fz) - 5, y: 14, "text-anchor": "end", style: "fill:var(--ink)"}); ft.textContent = "frozen period"; if (x(fz) > 90) svg.append(ft);
    rigs.forEach((r, i) => {
      const yy = AX + i * rowH;
      svg.append(S("line", {x1: 0, x2: W, y1: yy + rowH, y2: yy + rowH, stroke: "var(--grid)"}));
      const off = [[toT(r.contract_end), T1, "off contract"], [T0 - DAY * 3650, toT(r.contract_start), "off contract"]].concat(r.unavailability.map(u => [toT(u.start), toT(u.end), u.reason]));
      for (const [a, b, why] of off) { if (b <= T0 || a >= T1 || b <= a) continue; const gg = S("rect", {x: x(Math.max(a, T0)), y: yy + 5, width: x(Math.min(b, T1)) - x(Math.max(a, T0)), height: rowH - 10, fill: "var(--unavail)", rx: 3}); svg.append(gg); hover(gg, () => [`${r.rig_id} – ${why}`, `${iso(Math.max(a, T0))} → ${iso(Math.min(b, T1))}`]); }
    });
    const ri = Object.fromEntries(rigs.map((r, i) => [r.rig_id, i]));
    for (const s of v.schedule) {
      const [wid, ph, rig, sd, ed, asset, cat, tier, base, eff, sail, frozen, val, purpose, name, wc] = s;
      const i = ri[rig], yy = AX + i * rowH, a = toT(sd), b = toT(ed);
      if (b <= T0) continue;
      const xa = x(Math.max(a, T0)), xb = x(b), bw = Math.max(1.5, xb - xa - 1);
      if (sail > 0 && a - sail * DAY >= T0) svg.append(S("line", {x1: x(a - sail * DAY), x2: x(a), y1: yy + rowH / 2, y2: yy + rowH / 2, stroke: "var(--ink2)", "stroke-width": 2}));
      const th = ph === "top_hole"; const hgt = th ? Math.max(10, rowH * 0.3) : rowH - 12; const yb = yy + (rowH - hgt) / 2;
      const gg = S("g");
      gg.append(S("rect", {x: xa, y: yb, width: bw, height: hgt, fill: acol(asset), rx: 3}));
      if (frozen) gg.append(S("rect", {x: xa, y: yb, width: bw, height: hgt, fill: "url(#hatch)", rx: 3}));
      if (eff > base + 0.5) { const fw = (eff - base) / eff * (xb - x(a)); gg.append(S("rect", {x: xb - 1 - Math.min(fw, bw), y: yb + hgt - 3, width: Math.min(fw, bw), height: 3, fill: "var(--ink)", opacity: .45})); }
      let lx = xa + 5;
      if (cat !== "development") { gg.append(S("path", {d: `M${xa + 6} ${yb + 3} l4 4 l-4 4 l-4 -4z`, fill: "var(--ink)"})); lx += 9; }
      const label = name || wid;
      if (!th && bw >= label.length * 6.3 + 14) { const t = S("text", {x: lx, y: yb + Math.min(hgt / 2 + 4, 16), class: "bar-label", style: `fill:${aink(asset)}`}); t.textContent = label; gg.append(t); if (rowH >= 54 && bw >= 70) { const t2 = S("text", {x: lx, y: yb + Math.min(hgt / 2 + 4, 16) + 13, class: "bar-label", style: `fill:${aink(asset)};font-weight:400;opacity:.9`}); t2.textContent = wid; gg.append(t2); } }
      svg.append(gg);
      hover(gg, () => [`${name} (${wid})${ph !== "full" ? " – " + ph.replace("_", " ") : ""}`, `${asset} · ${cat}${purpose !== "n/a" ? " · " + purpose : ""}`, `${rig} · ${sd} → ${ed}`, `planned ${base} d, with weather ${eff} d (+${(eff - base).toFixed(1)} d)`, sail ? `sailing in: ${sail} d` : "", `winter tier: ${tier.replace("_", " ")}`, ph === "top_hole" ? "" : `value ${val.toLocaleString("en")} M ${MU()}`, frozen ? "FROZEN (admin only)" : ""]);
    }
    sc.replaceChildren(svg);
    if (gz.reset) sc.scrollLeft = 0;
    else if (oldCenter != null && gz.mode !== "fit") sc.scrollLeft = Math.max(0, oldCenter * ppd - w / 2);
    gz.center = null; gz.reset = false;
  }
  requestAnimationFrame(draw);
  ganttDraw = draw;
  let lastW = 0; new ResizeObserver(() => { if (Math.abs(sc.clientWidth - lastW) > 2) { lastW = sc.clientWidth; draw(); } }).observe(m);
}

// ---------------------------------------------------------------- overview
function overview(m, v) {
  const k = v.kpis, last = byKind("last_approved"), base = byKind("baseline");
  m.append(horizonControl(render));
  const wm = windowMetrics(v, horizonMonths);
  const t = $("div", {class: "tiles"});
  const dl = last ? k.portfolio_value - last.kpis.portfolio_value : null, db = base ? k.portfolio_value - base.kpis.portfolio_value : null;
  const deltas = [last && v !== last ? `${sgn(dl)} vs last approved` : "", base && v !== base ? `${sgn(db)} vs manual baseline` : ""].filter(Boolean).join(" · ");
  t.append(tile("Portfolio value (NPV + EMV)", fB(k.portfolio_value) + " " + k.money_unit, deltas, {info: "Portfolio value, pre-tax, discounted to plan start\nNPV (net present value) of development wells: discounted production revenue minus OPEX and CAPEX.\nEMV (expected monetary value) of exploration wells: chance of success × value of a discovery, minus the well cost. It is a risked value: a 25 % chance at 400 M counts as 100 M.\nLifetime value of all wells in this schedule – not affected by the look-ahead filter."}));
  const ur = Object.entries(k.unscheduled_reasons || {}).map(([r, n]) => `${n} × ${r.replaceAll("_", " ")}`);
  t.append(tile("Wells scheduled", fN(k.wells_scheduled), `${wm.spud} start before ${wm.end} · ${k.wells_unscheduled} not scheduled →`, {click: () => go("uns"), info: `Wells not scheduled: ${k.wells_unscheduled}\nWells in the portfolio that get no rig slot in this schedule. Click the tile to see each well and why.\n${ur.join("\n")}\nnegative value = the well loses money even if drilled as early as possible.`}));
  const expMiss = k.target_years - k.target_years_met;
  t.append(tile("Exploration wells", fN(k.n_exploration), `yearly targets met in ${k.target_years_met} of ${k.target_years} years`, {cls: expMiss ? "cost" : "", info: "Exploration and appraisal wells in this schedule\nThe portfolio has a target range (min–max) of exploration wells to spud each year. See Exploration in the menu."}));
  t.append(tile("Strategic wells", `${k.n_strategic_in_window} of ${k.n_strategic}`, "drilled inside their strategic window", {cls: k.n_strategic_in_window < k.n_strategic ? "cost" : "", info: "Strategic exploration wells\nMandatory wells that must be drilled inside a fixed window (e.g. a licence commitment)."}));
  t.append(tile("Estimated WoW cost", "−" + fM(wm.wow) + " " + k.money_unit, `waiting on weather until ${wm.end}`, {cls: "cost", info: "Estimated waiting-on-weather (WoW) cost\nExtra rig days caused by weather (mainly Nov–Feb, worse further north) × the well's rig day cost (rig share of well CAPEX ÷ planned days).\nThe optimizer steers wells away from winter with a share of this cost: 0 % for weather-indifferent wells, 10 % for 'avoid', 50 % for 'strongly avoid' wells."}));
  t.append(tile("Sailing days", fN(wm.sail), `all rigs, until ${wm.end}`, {info: `How sailing is calculated\nTransit only: distance (nautical miles) ÷ (${META.rig_speed_knots || "rig"} knots × 24 h), rounded to whole days – moves under 12 hours count as 0.\nIn bad-weather months the transit takes longer: × (1 + rig move sensitivity × weather factor).\nRig down / rig up is part of each well's duration, not of the sailing.\nNext well on the same platform or template: ${META.same_installation_move_days ?? 0} days. Templates in the same field are a few nautical miles apart, so moves inside a field are ~0 days.`}));
  t.append(tile("Idle rig-days", fN(wm.idle), `all rigs, until ${wm.end}`, {info: "Idle rig-days\nDays a rig is on contract and available but neither drilling nor sailing (gaps waiting for wells to be ready, frozen-period gaps, or no compatible work)."}));
  m.append(t);
  // schedules table
  const rows = D.variants.map((x, i) => ({x, i, w: windowMetrics(x, horizonMonths)}));
  m.append(card(["Schedules ", info("Five schedules for the same wells and rigs\nThree optimized versions with different freedom to change the last approved plan, the last approved plan itself, and a manual-style baseline. Click a row to open it.")],
    table([
      {h: "Schedule", f: r => r.x.kpis.label}, {h: "What it is", f: r => r.x.kpis.description},
      {h: `Value (M ${k.money_unit})`, n: 1, f: r => fN(r.x.kpis.portfolio_value / 1e6, 0)},
      {h: "Δ vs last approved", n: 1, f: r => last ? sgn(r.x.kpis.portfolio_value - last.kpis.portfolio_value) : "", cls: r => last && r.x.kpis.portfolio_value < last.kpis.portfolio_value ? "neg" : "pos"},
      {h: "Δ vs manual baseline", n: 1, f: r => base ? sgn(r.x.kpis.portfolio_value - base.kpis.portfolio_value) : "", cls: r => base && r.x.kpis.portfolio_value < base.kpis.portfolio_value ? "neg" : "pos"},
      {h: "Wells", n: 1, f: r => r.x.kpis.wells_scheduled},
      {h: "Exploration target misses", n: 1, f: r => r.x.kpis.exploration_target_deviation, cls: r => r.x.kpis.exploration_target_deviation ? "neg" : "", info: "Exploration target misses\nNumber of exploration wells by which the yearly targets are missed, summed over all years: wells short of the yearly minimum plus wells above the yearly maximum. 0 = every year is inside its target range."},
      {h: `Est. WoW cost (M)`, n: 1, f: r => fN(r.w.wow / 1e6, 0), cls: () => "neg"},
      {h: "Sailing days", n: 1, f: r => fN(r.w.sail)}, {h: "Idle rig-days", n: 1, f: r => fN(r.w.idle)}],
      rows, {pick: r => { cur = r.i; sel.value = String(cur); render(); }, sel: r => r.i === cur}),
    $("div", {class: "sub", text: `WoW cost, sailing and idle days: from plan start until ${wm.end} (look-ahead filter above).`})));
  const g = $("div", {class: "two"});
  const bn = $("ul", {class: "plain"}); v.bottlenecks.forEach(s => bn.append($("li", {text: s})));
  g.append(card("Bottlenecks", bn));
  const nwell = {}; const seen = new Set();
  for (const s of v.schedule) { if (s[1] === "top_hole" || seen.has(s[0])) continue; seen.add(s[0]); nwell[s[5]] = (nwell[s[5]] || 0) + 1; }
  const ba = Object.entries(v.economics.by_asset).sort((a, b) => b[1] - a[1]);
  g.append(card(`Value by asset (M ${k.money_unit})`, barList(ba.map(([a, x]) => [a, x, acol(a), `${nwell[a] || 0} wells`]), k.money_unit)));
  m.append(g);
}
function barList(items, unit) {
  const w = $("div"); const max = Math.max(...items.map(x => Math.abs(x[1])), 1);
  for (const [name, val, col, note] of items) {
    const row = $("div", {style: "display:grid;grid-template-columns:120px 1fr 90px;gap:8px;align-items:center;margin:6px 0"});
    const track = $("div", {style: "height:22px;position:relative"});
    const pct = Math.max(0.5, Math.abs(val) / max * 100);
    const b = $("div", {style: `position:absolute;left:0;top:0;bottom:0;width:${pct}%;background:${val < 0 ? "var(--bad)" : col};border-radius:0 4px 4px 0`});
    track.append(b);
    if (note) { const inside = pct > 28; const lab = $("span", {text: note, style: `position:absolute;top:2px;font-size:12px;font-weight:600;${inside ? `left:8px;color:${name ? aink(name) : "#fff"}` : `left:calc(${pct}% + 6px);color:var(--ink2)`}`}); track.append(lab); }
    hover(track, () => [name, fM(val) + " " + unit, note || ""]);
    row.append($("div", {text: name}), track, $("div", {style: "text-align:right;font-variant-numeric:tabular-nums", text: fN(val / 1e6, 0)}));
    w.append(row);
  }
  return w;
}

// ---------------------------------------------------------------- generic charts
function chartBox(drawFn, height = 240) {
  const wrap = $("div", {style: `position:relative;height:${height}px`});
  const draw = () => drawFn(wrap, Math.max(300, wrap.clientWidth || 560), height);
  requestAnimationFrame(draw); let lw = 0; new ResizeObserver(() => { if (Math.abs(wrap.clientWidth - lw) > 2) { lw = wrap.clientWidth; draw(); } }).observe(wrap);
  return wrap;
}
function axes(svg, W, H, l, b, tp, max, min, fmt) {
  const ticks = 4, yv = q => H - b - (H - b - tp) * (q - min) / (max - min || 1);
  for (let k = 0; k <= ticks; k++) { const v = min + (max - min) * k / ticks; svg.append(S("line", {x1: l, x2: W - 8, y1: yv(v), y2: yv(v), stroke: v === 0 ? "var(--muted)" : "var(--grid)"})); const t = S("text", {x: l - 6, y: yv(v) + 4, "text-anchor": "end"}); t.textContent = fmt(v); svg.append(t); }
  if (min < 0) svg.append(S("line", {x1: l, x2: W - 8, y1: yv(0), y2: yv(0), stroke: "var(--muted)"}));
  return yv;
}
function niceMax(x) { if (x <= 0) return 1; const p = Math.pow(10, Math.floor(Math.log10(x))); for (const f of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (f * p >= x) return f * p; return 10 * p; }
function columns(years, series, fmt, unitLabel) {  // stacked columns; series: [{name,color,values}]
  return chartBox((wrap, W, H) => {
    const l = 52, b = 24, tp = 10; const tot = years.map((_, i) => series.reduce((s, x) => s + Math.max(0, x.values[i]), 0));
    const max = niceMax(Math.max(...tot)); const svg = S("svg", {width: W, height: H}); const yv = axes(svg, W, H, l, b, tp, max, 0, fmt);
    const bw = (W - l - 8) / years.length;
    years.forEach((yr, i) => {
      let acc = 0; const g = S("g");
      series.forEach((s, si) => { const v = Math.max(0, s.values[i]); if (v <= 0) return; const y1 = yv(acc + v), y2 = yv(acc); const top = si === series.length - 1 || series.slice(si + 1).every(q => q.values[i] <= 0); g.append(S("rect", {x: l + i * bw + 1.5, y: y1 + (acc > 0 ? 1 : 0), width: Math.max(1, bw - 3), height: Math.max(0, y2 - y1 - (acc > 0 ? 1 : 0)), fill: s.color, rx: top ? Math.min(3, bw / 4) : 0})); acc += v; });
      g.append(S("rect", {x: l + i * bw, y: tp, width: bw, height: H - b - tp, fill: "transparent"}));
      hover(g, () => [String(yr), ...series.map(s => `${s.name}: ${fmt(s.values[i])} ${unitLabel}`), series.length > 1 ? `Total: ${fmt(tot[i])} ${unitLabel}` : ""]);
      svg.append(g);
      if (years.length <= 14 || i % Math.ceil(years.length / 12) === 0) { const t = S("text", {x: l + i * bw + bw / 2, y: H - 7, "text-anchor": "middle"}); t.textContent = yr; svg.append(t); }
    });
    wrap.replaceChildren(svg);
  });
}
function lines(years, series, fmt, unitLabel) {
  return chartBox((wrap, W, H) => {
    const l = 56, b = 24, tp = 12, r = 74; const all = series.flatMap(s => s.values);
    const max = niceMax(Math.max(...all)), mn = Math.min(0, ...all), min = mn < 0 ? -niceMax(-mn) : 0;
    const svg = S("svg", {width: W, height: H}); const yv = axes(svg, W - r + 8, H, l, b, tp, max, min, fmt);
    const xs = i => l + (W - l - r) * i / Math.max(1, years.length - 1);
    years.forEach((yr, i) => { if (years.length <= 14 || i % Math.ceil(years.length / 12) === 0) { const t = S("text", {x: xs(i), y: H - 7, "text-anchor": "middle"}); t.textContent = yr; svg.append(t); } });
    for (const s of series) { svg.append(S("path", {d: s.values.map((v, i) => `${i ? "L" : "M"}${xs(i)} ${yv(v)}`).join(""), fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round"})); const t = S("text", {x: xs(years.length - 1) + 6, y: yv(s.values[s.values.length - 1]) + 4, style: "font-weight:600"}); t.textContent = s.name; svg.append(t); }
    const cross = S("line", {y1: tp, y2: H - b, stroke: "var(--muted)", "stroke-dasharray": "3 3", visibility: "hidden"}); svg.append(cross);
    const dots = series.map(s => { const c = S("circle", {r: 4.5, fill: s.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden"}); svg.append(c); return c; });
    const hit = S("rect", {x: l, y: tp, width: W - l - r, height: H - b - tp, fill: "transparent"}); svg.append(hit);
    hit.addEventListener("mousemove", e => { const bx = svg.getBoundingClientRect(); const i = Math.max(0, Math.min(years.length - 1, Math.round((e.clientX - bx.left - l) / ((W - l - r) / Math.max(1, years.length - 1))))); cross.setAttribute("x1", xs(i)); cross.setAttribute("x2", xs(i)); cross.setAttribute("visibility", "visible"); dots.forEach((d, k) => { d.setAttribute("cx", xs(i)); d.setAttribute("cy", yv(series[k].values[i])); d.setAttribute("visibility", "visible"); }); showTip(e, [String(years[i]), ...series.map(s => `${s.name}: ${fmt(s.values[i])} ${unitLabel}`)]); });
    hit.addEventListener("mouseleave", () => { hideTip(); cross.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); });
    wrap.replaceChildren(svg);
  }, 280);
}
function legendRow(items) { const l = $("div", {class: "legend", style: "margin-bottom:6px"}); for (const [n, c] of items) l.append($("span", {}, $("span", {class: "sw", style: `background:${c}`}), document.createTextNode(n))); return l; }

// ---------------------------------------------------------------- economics
function economics(m, v) {
  const e = v.economics, u = MU(), nok = v.kpis.unit_set === "NOK_Sm3";
  const t = $("div", {class: "tiles"});
  t.append(tile("Total value", fB(e.total) + " " + u), tile("Development NPV", fB(e.npv_development) + " " + u, "", {info: "NPV – net present value\nDiscounted revenue minus OPEX and CAPEX of development wells, pre-tax, 8 % discount rate."}), tile("Exploration EMV", fB(e.emv_exploration) + " " + u, "", {info: "EMV – expected monetary value\nChance of success × value of a discovery − well cost, discounted. A risked value."}));
  m.append(t);
  const endY = +v.kpis.horizon_end.slice(0, 4) + 10;
  const ys = e.yearly.filter(r => r.year >= +v.kpis.plan_start.slice(0, 4) && r.year <= endY);
  const years = ys.map(r => r.year), dpy = y => ((y % 4 === 0 && y % 100 !== 0) || y % 400 === 0) ? 366 : 365;
  const oilD = ys.map(r => r.oil / dpy(r.year) / 1000), gasD = ys.map(r => (r.boe - r.oil) / dpy(r.year) / 1000);  // gas in oil-equivalent units
  const pu = nok ? "thousand Sm³ o.e./day" : "thousand boe/day";
  const g = $("div", {class: "two"});
  g.append(card(`Production (${pu})`, legendRow([[nok ? "Oil" : "Oil", "var(--s1)"], ["Gas", "var(--s2)"]]), columns(years, [{name: "Oil", color: "var(--s1)", values: oilD}, {name: "Gas", color: "var(--s2)", values: gasD}], x => fN(x, 1), nok ? "k Sm³/d" : "kboe/d"), $("div", {class: "sub", text: "Average daily rate per calendar year, wells in this schedule only (no existing base production). Gas converted at 5.8 MMBtu per boe."})));
  g.append(card(`Revenue (M ${u} per year)`, columns(years, [{name: "Revenue", color: "var(--s3)", values: ys.map(r => r.revenue / 1e6)}], x => fN(x, 0), `M ${u}`), $("div", {class: "sub", text: "Oil and gas sales at the corporate price deck, undiscounted."})));
  m.append(g);
  const net = ys.map(r => (r.revenue - r.opex - r.capex) / 1e6);
  const cs = [{name: "Revenue", color: "var(--s3)", values: ys.map(r => r.revenue / 1e6)}, {name: "OPEX", color: "var(--s2)", values: ys.map(r => r.opex / 1e6)}, {name: "CAPEX", color: "var(--s7)", values: ys.map(r => r.capex / 1e6)}, {name: "Net cash flow", color: "var(--ink)", values: net}];
  const det = $("details"); det.append($("summary", {text: "Show as table"}), table([{h: "Year", k: "year"}, {h: "Revenue (M)", n: 1, f: r => fN(r.revenue / 1e6, 0)}, {h: "OPEX (M)", n: 1, f: r => fN(r.opex / 1e6, 0)}, {h: "CAPEX (M)", n: 1, f: r => fN(r.capex / 1e6, 0)}, {h: "Net (M)", n: 1, f: r => fN((r.revenue - r.opex - r.capex) / 1e6, 0)}, {h: pu, n: 1, f: r => fN(r.boe / dpy(r.year) / 1000, 1)}], ys));
  m.append(card(`Yearly cash flow (M ${u}, undiscounted)`, legendRow(cs.map(s => [s.name, s.color])), lines(years, cs, x => fN(x, 0), `M ${u}`), det));
  const g2 = $("div", {class: "two"});
  g2.append(card(`Value by rig (M ${u})`, barList(Object.entries(e.by_rig).map(([a, x]) => [a, x, "var(--s1)"]), u)));
  g2.append(card(`Value by category (M ${u})`, barList(Object.entries(e.by_category).map(([a, x]) => [a, x, "var(--s3)"]), u)));
  m.append(g2);
}

// ---------------------------------------------------------------- utilization
function util(m, v) {
  m.append(horizonControl(render));
  const wm = windowMetrics(v, horizonMonths);
  const parts = [["drilling", "Drilling", "var(--s1)"], ["weather", "Weather-added", "var(--s7)"], ["sailing", "Sailing", "var(--s4)"], ["idle", "Idle", "var(--grid)"], ["unavailable", "Unavailable", "var(--unavail)"]];
  const c = card(`Rig time, ${v.kpis.plan_start} → ${wm.end} (days)`, legendRow(parts.map(p => [p[1], p[2]])));
  for (const r of wm.rigs) {
    const row = $("div", {style: "display:grid;grid-template-columns:170px 1fr 54px;gap:8px;align-items:center;margin:6px 0"});
    const tr = $("div", {style: "display:flex;height:18px;gap:2px"}); const tot = parts.reduce((s, p) => s + r[p[0]], 0) || 1;
    for (const [k, n, col] of parts) { if (r[k] <= 0.5) continue; const seg = $("div", {style: `flex:${r[k] / tot};background:${col};border-radius:3px;min-width:2px;border:1px solid var(--line)`}); hover(seg, () => [`${r.rig_id} – ${n}`, fN(r[k]) + " days"]); tr.append(seg); }
    row.append($("div", {text: r.rig_id + " " + r.name}), tr, $("div", {style: "text-align:right", text: Math.round(r.util * 100) + "%"})); c.append(row);
  }
  m.append(c);
  m.append(card("Table", table([{h: "Rig", k: "rig_id"}, {h: "Name", k: "name"}, {h: "On contract", n: 1, f: r => fN(r.available)}, {h: "Drilling", n: 1, f: r => fN(r.drilling)}, {h: "Weather-added", n: 1, f: r => fN(r.weather)}, {h: "Sailing", n: 1, f: r => fN(r.sailing)}, {h: "Idle", n: 1, f: r => fN(r.idle)}, {h: "Unavailable", n: 1, f: r => fN(r.unavailable)}, {h: "Utilization", n: 1, f: r => Math.round(r.util * 100) + "%"}], wm.rigs)));
}

// ---------------------------------------------------------------- exploration
function expl(m, v) {
  const x = v.exploration, u = MU();
  const t = $("div", {class: "tiles"}); t.append(tile("Exploration & appraisal wells", fN(x.n_wells)), tile("Total risked value (EMV)", fM(x.risked_value) + " " + u, "", {info: "EMV – expected monetary value\nChance of success × discovery value − well cost, discounted."}), tile("Strategic wells in window", `${v.kpis.n_strategic_in_window} of ${v.kpis.n_strategic}`)); m.append(t);
  const rows = x.per_year.filter(r => r.min != null);
  const c = card("Wells spudded per year vs target range");
  c.append(chartBox((wrap, W, H) => {
    const l = 34, b = 24, tp = 10; const max = Math.max(...rows.map(r => Math.max(r.max || 0, r.spudded))) + 1;
    const svg = S("svg", {width: W, height: H}); const yv = axes(svg, W, H, l, b, tp, max, 0, q => fN(q)); const bw = (W - l - 8) / rows.length;
    rows.forEach((r, i) => { const cx = l + i * bw + bw / 2; svg.append(S("rect", {x: cx - bw * 0.3, y: yv(r.max), width: bw * 0.6, height: yv(r.min) - yv(r.max), fill: "var(--winter)", stroke: "var(--s1)", "stroke-dasharray": "3 2", rx: 3})); const ok = r.spudded >= r.min && r.spudded <= r.max; const d = S("circle", {cx, cy: yv(r.spudded), r: 6.5, fill: ok ? "var(--s1)" : "var(--bad)", stroke: "var(--surface)", "stroke-width": 2}); svg.append(d); hover(d, () => [String(r.year), `${r.spudded} spudded`, `target ${r.min}–${r.max}`]); const tx = S("text", {x: cx, y: H - 7, "text-anchor": "middle"}); tx.textContent = r.year; svg.append(tx); });
    wrap.replaceChildren(svg);
  }, 220), $("div", {class: "sub", text: "Dashed band: yearly target range. Dot: wells spudded (red when outside the range)."}));
  m.append(c);
  m.append(card("Strategic wells vs windows", table([{h: "Well", k: "well_id"}, {h: "Prospect", k: "prospect"}, {h: "Window", f: r => r.window_start + " → " + r.window_end}, {h: "Scheduled", f: r => r.start_date ? r.start_date + " → " + r.end_date : ""}, {h: "Status", k: "status", cls: r => r.status === "in window" ? "pos" : "neg"}], x.strategic)));
}

// ---------------------------------------------------------------- weather
function weather(m, v) {
  const u = MU();
  m.append(card("Wells drilled in winter (≥ 50 % of their days in Nov–Feb), by winter tier", table([
    {h: "Winter", k: "season"}, {h: "Indifferent", n: 1, f: r => r.counts.none}, {h: "Avoid", n: 1, f: r => r.counts.avoid}, {h: "Strongly avoid", n: 1, f: r => r.counts.strongly_avoid, cls: r => r.counts.strongly_avoid ? "warnc" : ""},
    {h: "Weather-added days", n: 1, f: r => fN(r.weather_added_days)}, {h: `Est. WoW cost (M ${u})`, n: 1, f: r => fN((r.wow_cost || 0) / 1e6, 1), cls: () => "neg"},
    {h: "Strongly-avoid wells in winter", f: r => r.wells.strongly_avoid.map(w => w.well_id + (w.frozen ? " (frozen)" : "")).join(", ")}], v.weather)),
    $("div", {class: "sub", text: "WoW cost = weather-added rig days × the well's rig day cost. Weather-added days count every phase touching a winter month."}));
}

// ---------------------------------------------------------------- campaigns
function batch(m, v) {
  m.append($("p", {class: "sub", text: "Conductor (batch) campaigns: one rig drills the top holes of several wells in a row; a rig returns later for each main phase."}));
  for (const c of v.campaigns) m.append(card(`${c.campaign} · ${c.installation}` + (c.status === "scheduled" ? ` · conductor rig ${c.rig_id} · ${c.start} → ${c.end}` : " · not scheduled"),
    table([{h: "Well", k: "well_id"}, {h: "Top hole", f: r => r.top_hole_start + " → " + r.top_hole_end}, {h: "Main rig", k: "main_rig"}, {h: "Main phase", f: r => r.main_start + " → " + r.main_end}, {h: "Gap (days)", n: 1, k: "gap_days"}, {h: "Same rig", f: r => r.same_rig ? "yes" : "no"}], c.wells)));
}

// ---------------------------------------------------------------- unscheduled
function uns(m, v) {
  const cnt = {}; v.unscheduled.forEach(r => cnt[r.reason] = (cnt[r.reason] || 0) + 1);
  const expl = {negative_value: "Loses money even if drilled at the earliest possible date.", no_compatible_rig: "No rig in the fleet meets water depth, drilling depth or equipment needs.", precedence_blocked: "Must wait for another well (e.g. an injector) that is not scheduled.", no_capacity_in_horizon: "Positive value, but compatible rigs are used for more valuable wells.", time_window_infeasible: "Its time window cannot be met on any compatible rig.", installation_conflict: "Another rig occupies the installation throughout its window.", freedom_limit: "Adding wells is not allowed for this schedule."};
  const t = $("div", {class: "tiles"}); for (const [k, n] of Object.entries(cnt)) t.append(tile(k.replaceAll("_", " "), fN(n), "wells", {info: k.replaceAll("_", " ") + "\n" + (expl[k] || "")})); m.append(t);
  m.append(card("Wells not scheduled, with reason", table([{h: "Well", k: "well_id"}, {h: "Asset", k: "asset"}, {h: "Category", k: "category"}, {h: "Purpose", k: "purpose"}, {h: "Mandatory", f: r => r.mandatory ? "yes" : ""}, {h: "Reason", f: r => r.reason.replaceAll("_", " ")}, {h: "Detail", k: "detail"}, {h: `Value at earliest start (M ${MU()})`, n: 1, f: r => fN(r.ideal_value / 1e6, 1), cls: r => r.ideal_value < 0 ? "neg" : ""}], v.unscheduled)));
}

// ---------------------------------------------------------------- risk
function risk(m, v) {
  const u = MU();
  if (v.monte_carlo) {
    const mc = v.monte_carlo; const t = $("div", {class: "tiles"});
    t.append(tile("Planned value (P50 durations)", fB(mc.deterministic) + " " + u), tile("P10 value", fB(mc.value_p10) + " " + u, "10 % chance of lower"), tile("P50 value", fB(mc.value_p50) + " " + u), tile("P90 value", fB(mc.value_p90) + " " + u, "10 % chance of higher"));
    m.append(t);
    m.append(card(`Start-date uncertainty (${mc.samples} simulations)`, $("div", {class: "sub", text: mc.note}), table([{h: "Well", k: "well_id"}, {h: "Planned start", k: "planned"}, {h: "P10", k: "p10"}, {h: "P50", k: "p50"}, {h: "P90", k: "p90"}], mc.wells)));
  }
  if (v.discovery_risk) m.append(card("Discovery risk: knock-on if an exploration well finds hydrocarbons", table([
    {h: "Exploration well", k: "well_id"}, {h: "Rig", k: "rig_id"}, {h: "Start", k: "start_date"}, {h: "Extra days", n: 1, k: "extra_days"}, {h: "Wells delayed", n: 1, k: "n_delayed"}, {h: "Max delay (d)", n: 1, k: "max_delay_days"},
    {h: `Value effect (M ${u})`, n: 1, f: r => fN(r.value_impact_other_wells / 1e6, 1)}, {h: "Delayed wells", f: r => r.delayed.map(d => `${d.well_id} (+${d.days} d)`).join(", ")}], v.discovery_risk)));
  if (!v.monte_carlo && !v.discovery_risk) m.append(card("Risk & uncertainty", $("div", {class: "sub", text: "Risk analysis is run for the optimized schedules. Pick one in the schedule selector."})));
}

// ---------------------------------------------------------------- diff
function diff(m, v) {
  if (!v.comparison) { m.append(card("Changes vs last approved", $("div", {class: "sub", text: "This is the last approved plan. Pick another schedule in the selector to see its changes."}))); return; }
  const c = v.comparison, u = MU(); const t = $("div", {class: "tiles"});
  t.append(tile("Value change vs last approved", sgn(c.value_delta) + " " + u, "", {cls: c.value_delta < 0 ? "cost" : ""}));
  const nm = {"moved": "Moved (same rig)", "rig change": "Moved to another rig", "added": "Added", "dropped": "Dropped"};
  for (const k of Object.keys(nm)) t.append(tile(nm[k], fN(c.counts[k] || 0), "wells"));
  t.append(tile("Average shift", fN(c.mean_abs_shift_days) + " days", `largest ${c.max_abs_shift_days} days`));
  m.append(t);
  m.append(card("Well by well", table([{h: "Well", k: "well_id"}, {h: "Change", k: "change"}, {h: "From", f: r => (r.from_rig || "") + " " + (r.from_start || "")}, {h: "To", f: r => (r.to_rig || "") + " " + (r.to_start || "")}, {h: "Shift (days)", n: 1, k: "shift_days"}, {h: `Value Δ (M ${u})`, n: 1, f: r => fN(r.value_delta / 1e6, 1), cls: r => r.value_delta >= 0 ? "pos" : "neg"}], c.rows)));
}

// ---------------------------------------------------------------- about
function about(m) {
  m.append(card("The schedules", table([{h: "Schedule", f: r => r.kpis.label}, {h: "What it is", f: r => r.kpis.description}], D.variants)));
  const ul = $("ul", {class: "plain"});
  [["Frozen period", "The first 12 months are fixed (hatched bars); only admins change them. The optimizer never places wells there."],
   ["Value", "Pre-tax NPV for development wells and risked EMV for exploration wells, discounted at 8 % to plan start, corporate price deck."],
   ["Weather", "Effective duration = planned duration × (1 + weather sensitivity × tier factor × monthly WoW factor of the region)."],
   ["Sailing", `Transit only: distance ÷ (${META.rig_speed_knots || "rig"} knots × 24 h), longer in bad-weather months, rounded to whole days. Rig down/up is part of each well's duration. ${META.same_installation_move_days ?? 0} days between wells on the same platform or template.`],
   ["Hard rules", "Rig compatibility (water depth, drilling depth, HPHT, well-test equipment), one rig per installation at a time, rig contracts and yard stays, well time windows, precedence, strategic windows."]]
   .forEach(([a, b]) => ul.append($("li", {}, $("b", {text: a + ": "}), document.createTextNode(b))));
  m.append(card("How to read this report", ul));
}
render();
let rzT = null;
addEventListener("resize", () => { if (view !== "schedule" || !ganttDraw) return; clearTimeout(rzT); rzT = setTimeout(() => ganttDraw(), 120); });
</script>
</body>
</html>
"""
