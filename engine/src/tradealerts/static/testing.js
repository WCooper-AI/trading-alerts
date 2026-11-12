"use strict";

/* Trading Alerts web interface: Testing tools beside the editor: backtest, compare settings, luck check.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- backtest panel

// ----- test period picker, shared by both backtest panels

const PERIODS = [1, 2, 3, 5, 10];
function pref(key, fallback) { try { return localStorage.getItem(key) ?? fallback; } catch (_) { return fallback; } }
function savePref(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* not saved */ } }

/** Select for "Last N years" (and optionally custom dates). value() gives the request fields. */
function periodPicker({ custom = false } = {}) {
  const sel = h("select", { "aria-label": "Test period" },
    PERIODS.map((y) => h("option", { value: String(y) }, y === 1 ? "Last year" : `Last ${y} years`)),
    custom ? h("option", { value: "custom" }, "Custom dates") : null);
  const saved = pref("ta-bt-years", "2");
  sel.value = PERIODS.map(String).includes(saved) || (custom && saved === "custom") ? saved : "2";
  const from = h("input", { type: "date", "aria-label": "From" });
  const to = h("input", { type: "date", "aria-label": "To" });
  const dates = h("span", { class: "inline period-dates" },
    h("span", { class: "field" }, h("label", {}, "From"), from), h("span", { class: "field" }, h("label", {}, "To"), to));
  const sync = () => { dates.hidden = sel.value !== "custom"; };
  sel.addEventListener("change", () => { savePref("ta-bt-years", sel.value); sync(); });
  sync();
  const el = h("span", { class: "inline period" }, h("span", { class: "field" }, h("label", {}, "Test over"), sel), custom ? dates : null);
  el.value = () => (sel.value === "custom"
    ? { start: from.value || null, end: to.value || null }
    : { years: Number(sel.value) });
  return el;
}

// ----- testing tools: backtest, compare settings, luck check

function setPath(obj, path, value) {
  let node = obj;
  for (const k of path.slice(0, -1)) node = node[k];
  node[path[path.length - 1]] = value;
}

/** Numbers in a custom strategy's rules that "Compare settings" can vary. */
function pathSettings(d) {
  const out = [];
  const add = (path, label, v, integer = false) => {
    if (typeof v === "number") out.push({ kind: "path", path, label, type: "number", integer, current: v });
  };
  const walkOperand = (op, path, where) => {
    if (typeof op === "number") add(path, `${where}: the number ${short(op)}`, op);
    else if (isObj(op) && op.indicator) {
      const meta = indicatorMeta(op.indicator);
      const name = meta ? meta.short : op.indicator;
      for (const [k, v] of Object.entries(op)) {
        if (typeof v === "number" && !["shift", "scale", "add"].includes(k)) {
          add([...path, k], `${where}: ${name} ${PARAM_LABELS[k] || k}`, v, !!meta && meta.types[k] === "int");
        }
      }
    }
  };
  const walkCond = (c, path, where) => {
    if (!isObj(c)) return;
    const kind = groupKind(c);
    if (kind) {
      const items = groupItems(c);
      const base = { all: ["all"], any: ["any"], none: ["not", "any"], notall: ["not", "all"] }[kind];
      items.forEach((it, i) => walkCond(it, [...path, ...base, i], items.length > 1 ? `${where}, rule ${i + 1}` : where));
      return;
    }
    if ("left" in c) { walkOperand(c.left, [...path, "left"], where); walkOperand(c.right, [...path, "right"], where); }
  };
  walkCond(d.entry, ["entry"], "Entry");
  const ex = d.exit || {};
  for (const [which, label, kinds] of [["stop", "Stop loss", STOP_KINDS], ["target", "Take profit", TARGET_KINDS]]) {
    const spec = ex[which];
    if (!isObj(spec)) continue;
    for (const k of ["atr", "percent", "points", "r_multiple"]) if (typeof spec[k] === "number") add(["exit", which, k], `${label}: ${kinds[k]}`, spec[k]);
    if (typeof spec.atr_period === "number") add(["exit", which, "atr_period"], `${label}: ATR bars`, spec.atr_period, true);
    if (spec.level) walkOperand(spec.level, ["exit", which, "level"], label);
  }
  if (typeof ex.max_bars === "number") add(["exit", "max_bars"], "Close after this many candles", ex.max_bars, true);
  if (ex.when) walkCond(ex.when, ["exit", "when"], "Exit rule");
  return out;
}

/** A guided strategy's settings that "Compare settings" can vary. */
function knobSettings(recipe, st) {
  const out = [];
  for (const k of recipe.knobs) {
    const base = { kind: "knob", key: k.key, label: k.label + (k.unit ? ` (${k.unit})` : ""), current: st[k.key] };
    if (k.kind === "number") out.push({ ...base, type: "number", integer: !!k.integer || k.key === "max_hold", min: k.min, max: k.max });
    else if (k.kind === "select") out.push({ ...base, type: "select", options: k.options });
    else if (k.kind === "toggle") out.push({ ...base, type: "select", options: [{ value: false, label: "Off" }, { value: true, label: "On" }] });
  }
  return out;
}

const MAX_SWEEP = 25;
function decimals(x) { const t = String(x); return t.includes(".") ? t.split(".")[1].length : 0; }
function rangeValues(from, to, step, integer) {
  if ([from, to, step].some(isBlank)) return { values: [], note: "Fill in from, to and step." };
  if (step <= 0) return { values: [], note: "The step must be more than 0." };
  if (to <= from) return { values: [], note: "'To' must be bigger than 'from'." };
  const n = Math.floor((to - from) / step + 1e-9) + 1;
  if (n > MAX_SWEEP) return { values: [], note: `That's ${n} values. Use a bigger step: ${MAX_SWEEP} at most.` };
  const d = Math.max(decimals(from), decimals(step));
  let values = Array.from({ length: n }, (_, i) => +(from + i * step).toFixed(d));
  if (integer) values = [...new Set(values.map(Math.round))];
  if (values.length < 2) return { values: [], note: "That gives fewer than two values." };
  return { values, note: `Tests ${values.length} values: ${values.slice(0, 6).map(short).join(", ")}${values.length > 6 ? ", …" : ""}` };
}

/** Backtest, Compare settings and Luck check, sharing one test period. */
function analysisPanel({ getData, render, intro, settings, apply, runLabel = "Run backtest", strategyId = null }) {
  const period = periodPicker({ custom: true });
  const TABS = [["backtest", "Backtest"], ["sweep", "Compare settings"], ["luck", "Luck check"]];
  let current = pref("ta-tool", "backtest");
  if (!TABS.some(([k]) => k === current)) current = "backtest";
  const seg = h("div", { class: "segmented tabs", role: "tablist", "aria-label": "Testing tools" });
  const holder = h("div", { class: "tool-body" });

  async function fetchData() {
    const d = await getData();
    return d || null;
  }

  // --- backtest
  const btOut = h("div", {}, h("p", { class: "muted small" }, intro));
  const btRun = h("button", { class: "primary", type: "button" }, runLabel);
  btRun.addEventListener("click", async () => {
    const data = await fetchData();
    if (!data) return;
    btRun.disabled = true;
    clear(btOut).appendChild(h("p", { class: "loading" }, "Loading prices and testing your rules. Longer periods take longer the first time…"));
    try {
      const res = await POST("/backtest", { data, strategy_id: strategyId, ...period.value() });
      savePref("ta-did-backtest", "1");
      clear(btOut).appendChild(render(res));
      btRun.textContent = "Test again";
    } catch (e) { clear(btOut).appendChild(h("p", { class: "error-box" }, e.message)); }
    finally { btRun.disabled = false; }
  });
  const backtestPane = h("section", { class: "section" }, h("div", { class: "tool-actions" }, btRun), btOut);

  // --- compare settings
  let list = [];
  let chosen = null;
  const pick = h("select", { class: "wide", "aria-label": "Setting to compare" });
  const rangeBox = h("div", { class: "range-box" });
  const swOut = h("div");
  const swRun = h("button", { class: "primary", type: "button" }, "Compare");
  let range = { from: null, to: null, step: null };
  let values = [];
  const rangeNote = h("p", { class: "small muted", "aria-live": "polite" });
  function refreshSettings() {
    list = settings();
    const keep = chosen && list.findIndex((x) => x.label === chosen.label);
    clear(pick);
    pick.appendChild(h("option", { value: "", disabled: true }, list.length ? "Choose a setting…" : "Fill in some settings first"));
    list.forEach((x, i) => pick.appendChild(h("option", { value: String(i) }, x.label)));
    pick.value = keep !== null && keep >= 0 ? String(keep) : "";
    chosen = pick.value === "" ? null : list[Number(pick.value)];
    pick.classList.toggle("to-fill", !chosen);
    renderRange();
  }
  pick.addEventListener("change", () => { chosen = list[Number(pick.value)]; pick.classList.remove("to-fill"); range = { from: null, to: null, step: null }; renderRange(); });
  pick.addEventListener("focus", () => { const before = pick.value; refreshSettings(); if (pick.value !== before && before) pick.value = before; });
  function renderRange() {
    clear(rangeBox);
    values = [];
    if (!chosen) { rangeNote.textContent = ""; return; }
    const now = chosen.current;
    if (chosen.type === "select") {
      values = chosen.options.map((o) => o.value);
      rangeNote.textContent = `Tests every option: ${chosen.options.map((o) => o.label).join(", ")}.`;
      return;
    }
    const upd = () => { const r = rangeValues(range.from, range.to, range.step, chosen.integer); values = r.values; rangeNote.textContent = r.note; };
    const field = (key, label) => h("div", { class: "field" }, h("label", {}, label),
      numberInput(range[key], (n) => { range[key] = n; upd(); }, { label, step: "any", cls: "narrow", allowEmpty: false }));
    append(rangeBox, [h("div", { class: "inline range-fields" }, field("from", "From"), field("to", "To"), field("step", "Step")),
      isBlank(now) ? null : h("p", { class: "small muted" }, `Currently ${short(now)}.`)]);
    upd();
  }
  swRun.addEventListener("click", async () => {
    if (!chosen) { toast("Choose a setting to compare", true); return; }
    if (values.length < 2) { toast(rangeNote.textContent || "Choose the values to compare", true); return; }
    const data = await fetchData();
    if (!data) return;
    swRun.disabled = true;
    const setting = chosen.kind === "knob" ? { kind: "knob", key: chosen.key } : { kind: "path", path: chosen.path };
    clear(swOut).appendChild(h("p", { class: "loading" }, `Running ${values.length} backtests. This can take a minute…`));
    try {
      const res = await POST("/sweep", { data, setting, values, ...period.value() });
      clear(swOut).appendChild(renderSweep(res, chosen, apply ? (v) => { apply(chosen, v); chosen = { ...chosen, current: v }; toast(`${chosen.label} set to ${optionLabel(chosen, v)}`); } : null));
    } catch (e) { clear(swOut).appendChild(h("p", { class: "error-box" }, e.message)); }
    finally { swRun.disabled = false; }
  });
  const sweepPane = h("section", { class: "section" },
    h("p", { class: "muted small tool-intro" }, "Runs the same backtest with one setting changed, to see how sensitive the result is to it."),
    h("div", { class: "field" }, h("label", {}, "Setting"), pick), rangeBox, rangeNote,
    h("div", { class: "tool-actions" }, swRun), swOut);

  // --- luck check
  const lkOut = h("div");
  const lkRun = h("button", { class: "primary", type: "button" }, "Run luck check");
  lkRun.addEventListener("click", async () => {
    const data = await fetchData();
    if (!data) return;
    lkRun.disabled = true;
    clear(lkOut).appendChild(h("p", { class: "loading" }, "Backtesting on dozens of random price paths and reshuffling the trades. This takes up to a minute…"));
    try {
      clear(lkOut).appendChild(renderLuck(await POST("/luck", { data, ...period.value() })));
      lkRun.textContent = "Check again";
    } catch (e) { clear(lkOut).appendChild(h("p", { class: "error-box" }, e.message)); }
    finally { lkRun.disabled = false; }
  });
  const luckPane = h("section", { class: "section" },
    h("p", { class: "muted small tool-intro" }, "Asks whether the backtest's result could just be luck: too few trades, all the profit from one stretch, " +
      "a handful of big winners, or rules that do just as well on made-up prices."),
    h("div", { class: "tool-actions" }, lkRun), lkOut);

  const panes = { backtest: backtestPane, sweep: sweepPane, luck: luckPane };
  function show(k) {
    current = k;
    savePref("ta-tool", k);
    clear(seg);
    for (const [key, label] of TABS) {
      seg.appendChild(h("button", { type: "button", role: "tab", "aria-selected": String(key === k), "aria-pressed": String(key === k),
        onclick: () => show(key) }, label));
    }
    clear(holder).appendChild(panes[k]);
    if (k === "sweep") refreshSettings();
  }
  show(current);
  const panel = h("div", { class: "panel tools" },
    h("section", { class: "section tool-top" }, seg, h("div", { class: "bt-controls" }, period)), holder);
  panel.markStale = () => {
    for (const out of [btOut, swOut, lkOut]) {
      if (out.querySelector(".plain-results, .headline, .sweep, .luck") && !out.querySelector(".stale")) {
        out.insertBefore(h("p", { class: "notice stale" }, "You've changed the settings. Run it again to update these results."), out.firstChild);
      }
    }
  };
  return panel;
}

function optionLabel(setting, v) {
  if (setting.type === "select") { const o = setting.options.find((x) => x.value === v); return o ? o.label : String(v); }
  return short(v);
}

function renderSweep(res, setting, onUse) {
  const sym = res.currency;
  const rows = res.rows;
  const ok = rows.filter((r) => !r.error);
  const box = h("div", { class: "sweep" });
  if (!ok.length) {
    box.appendChild(h("p", { class: "error-box" }, "None of the values could be tested: " + rows[0].error));
    return box;
  }
  const best = ok.reduce((a, b) => (b.return_pct > a.return_pct ? b : a));
  const steady = ok.reduce((a, b) => (b.neighbourhood_pct > a.neighbourhood_pct ? b : a));
  append(box, [
    h("p", { class: "result-lede" }, `From ${day(res.start)} to ${day(res.end)}, the highest return came from `,
      h("strong", {}, optionLabel(setting, best.value)), ` (${signed(best.return_pct, "%")}).`),
    setting.type === "number" && steady.value !== best.value
      ? h("p", { class: "muted" }, `Taking each value together with its neighbours, `, h("strong", {}, optionLabel(setting, steady.value)),
        ` sits in the strongest area (${signed(steady.neighbourhood_pct, "%")} on average).`) : null,
    sweepChart(ok, setting),
  ]);
  const table = h("table", { class: "trades sweep-table" },
    h("thead", {}, h("tr", {}, h("th", {}, "Value"), h("th", { class: "num" }, "Return"), h("th", { class: "num" }, "Worst drop"),
      h("th", { class: "num" }, "Trades"), h("th", { class: "num" }, "Won"), h("th", {}, "Both halves"), onUse ? h("th", {}) : null)),
    h("tbody", {}, rows.map((r) => r.error
      ? h("tr", {}, h("td", {}, optionLabel(setting, r.value)), h("td", { colspan: onUse ? 6 : 5, class: "loss" }, r.error))
      : h("tr", { class: r.value === setting.current ? "current" : null },
        h("td", {}, optionLabel(setting, r.value), r.value === setting.current ? h("span", { class: "tag" }, "now") : null),
        h("td", { class: "num " + (r.return_pct >= 0 ? "gain" : "loss") }, signed(r.return_pct, "%")),
        h("td", { class: "num" }, num(r.max_drawdown_pct, 1) + "%"),
        h("td", { class: "num" }, String(r.trades)),
        h("td", { class: "num" }, r.trades ? num(r.win_rate_pct, 0) + "%" : "–"),
        h("td", {}, r.trades ? (r.first_half > 0 && r.second_half > 0 ? "Profitable in both" : "No") : "–"),
        onUse ? h("td", { class: "num" }, r.value === setting.current ? null : h("button", { type: "button", class: "quiet",
          onclick: (e) => { onUse(r.value); e.target.closest("tbody").querySelectorAll("tr").forEach((tr) => tr.classList.remove("current")); e.target.closest("tr").classList.add("current"); e.target.remove(); } }, "Use")) : null))));
  append(box, [h("div", { class: "table-wrap" }, table)]);
  if (res.walk_forward) box.appendChild(renderWalkForward(res.walk_forward, setting, sym));
  append(box, [
    h("p", { class: "notice" }, "Careful: the value that did best on past prices is usually luckier than it looks, because you picked it after seeing the results. " +
      "Prefer a value whose neighbours also did well, check it was profitable in both halves, then run the luck check on it."),
    res.notes && res.notes.length ? h("ul", { class: "warnings" }, res.notes.map((w) => h("li", {}, w))) : null]);
  return box;
}

function renderWalkForward(wf, setting, sym) {
  const gap = wf.hindsight_pct - wf.walk_forward_pct;
  let verdict;
  if (wf.walk_forward_pct <= 0) verdict = "Tuning as you went would have lost money, so the good results above depend on knowing the best value in advance.";
  else if (gap > Math.max(5, Math.abs(wf.hindsight_pct) * 0.5)) verdict = "Much of the best value's result came from hindsight. Expect live results closer to the walk-forward figure.";
  else verdict = "Choosing from past data held up reasonably well on the data that came after it.";
  return h("div", { class: "walk" }, h("h3", {}, "Walk-forward check", infoTip("Splits the period into stretches. Before each stretch it picks the value that did best on all the data so far, then records how that value did on the stretch. Every choice only uses the past, like tuning for real would.")),
    h("p", {}, `From ${day(wf.from)} to ${day(wf.to)}, picking the best value from the past each time made `,
      h("strong", { class: wf.walk_forward_pct >= 0 ? "gain" : "loss" }, signed(wf.walk_forward_pct, "%")),
      `. With hindsight, the best single value (${optionLabel(setting, wf.hindsight_value)}) made ${signed(wf.hindsight_pct, "%")}; the average of all values ${signed(wf.average_pct, "%")}.`),
    h("p", { class: "small muted" }, verdict),
    h("div", { class: "table-wrap" }, h("table", { class: "trades" },
      h("thead", {}, h("tr", {}, h("th", {}, "Tuned on data up to"), h("th", {}, "Picked"), h("th", { class: "num" }, "Result then"),
        h("th", {}, "Next stretch, to"), h("th", { class: "num" }, "Result"))),
      h("tbody", {}, wf.windows.map((w) => h("tr", {}, h("td", {}, day(w.train_to)), h("td", {}, optionLabel(setting, w.value)),
        h("td", { class: "num" }, signed(w.train_return_pct, "%")), h("td", {}, day(w.test_to)),
        h("td", { class: "num " + (w.test_return_pct >= 0 ? "gain" : "loss") }, signed(w.test_return_pct, "%"))))))));
}

function sweepChart(rows, setting) {
  const W = 560, H = 190, P = 8, B = 26;
  const ys = rows.map((r) => r.return_pct);
  const hi = Math.max(0, ...ys), lo = Math.min(0, ...ys);
  const span = hi - lo || 1;
  const y = (v) => P + ((hi - v) * (H - P - B)) / span;
  const slot = (W - 2 * P) / rows.length;
  const bw = Math.max(4, Math.min(42, slot * 0.66));
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Return for each value" });
  s.appendChild(svg("rect", { x: 0, y: 0, width: W, height: H, rx: 14, class: "chart-bg" }));
  s.appendChild(svg("line", { x1: P, x2: W - P, y1: y(0), y2: y(0), class: "chart-base" }));
  const wrap = h("div", { class: "chart" });
  const readout = h("span", { class: "readout" }, "Return for each value");
  wrap.appendChild(h("div", { class: "chart-title" }, h("h3", {}, "Return by value"), readout));
  rows.forEach((r, i) => {
    const cx = P + slot * (i + 0.5);
    const top = Math.min(y(r.return_pct), y(0));
    const height = Math.max(1.5, Math.abs(y(r.return_pct) - y(0)));
    const bar = svg("rect", { x: cx - bw / 2, y: top, width: bw, height, rx: Math.min(6, bw / 2),
      class: `bar ${r.return_pct >= 0 ? "gain" : "loss"}${r.value === setting.current ? " current" : ""}` });
    bar.style.animationDelay = `${i * 25}ms`;
    bar.addEventListener("pointerenter", () => { readout.textContent = `${optionLabel(setting, r.value)}: ${signed(r.return_pct, "%")}, ${r.trades} trades`; });
    bar.addEventListener("pointerleave", () => { readout.textContent = "Return for each value"; });
    s.appendChild(bar);
    if (rows.length <= 14 || i % Math.ceil(rows.length / 12) === 0) {
      const t = svg("text", { x: cx, y: H - 8, class: "bar-label", "text-anchor": "middle" });
      t.textContent = optionLabel(setting, r.value).slice(0, 10);
      s.appendChild(t);
    }
  });
  wrap.appendChild(s);
  return wrap;
}

function renderLuck(res) {
  const box = h("div", { class: "luck" });
  const sym = res.currency;
  const allPass = res.total && res.passed === res.total;
  append(box, [
    h("div", { class: "luck-score" },
      h("div", { class: "luck-ring " + (allPass ? "gain" : res.passed >= res.total - 1 ? "warn" : "loss") },
        h("strong", {}, `${res.passed}/${res.total}`)),
      h("div", {},
        h("p", { class: "result-lede" }, res.profitable
          ? `Passed ${res.passed} of ${res.total} luck checks.`
          : `These rules lost money over this period (${signed(res.return_pct, "%")}), so there's no profit to explain.`),
        h("p", { class: "muted small" }, `${day(res.start)} to ${day(res.end)}, ${res.trades} trades. Took ${res.seconds}s.`))),
    h("ul", { class: "checks" }, res.checks.map((c) => h("li", { class: c.passed === null ? "skip" : c.passed ? "pass" : "fail" },
      h("span", { class: "check-icon", "aria-hidden": "true" }, c.passed === null ? "–" : c.passed ? "✓" : "✕"),
      h("div", {}, h("strong", {}, c.title), h("p", { class: "small muted" }, c.detail),
        c.next_step ? h("p", { class: "small next-step" }, h("strong", {}, "What you can do: "), c.next_step) : null)))),
  ]);
  if (res.random) box.appendChild(randomStrip(res.random));
  if (res.order) {
    const o = res.order;
    box.appendChild(h("p", { class: "small" }, `Same trades in other orders (${o.runs.toLocaleString("en-GB")} reshuffles): the worst drop was typically `,
      h("strong", {}, `${num(Math.abs(o.median_dd), 1)}%`), `, 1 order in 20 saw `, h("strong", {}, `${num(Math.abs(o.bad_dd), 1)}%`),
      ` or worse, and 1 in 20 stayed under ${num(Math.abs(o.good_dd), 1)}%. This backtest's order gave ${num(Math.abs(o.actual_dd), 1)}%. Plan for the bigger number.`));
  }
  if (res.resample) {
    const r = res.resample;
    box.appendChild(h("p", { class: "small muted" }, `Average trade ${signed(r.avg_r, "R")}; across the reshuffled trade sets it ranged from ${signed(r.avg_r_low, "R")} to ${signed(r.avg_r_high, "R")} (middle 90%).`));
  }
  box.appendChild(h("p", { class: "notice" }, "Passing these checks makes luck a less likely explanation for the backtest. It doesn't mean the rules will make money in future: markets change."));
  if (res.notes && res.notes.length) box.appendChild(h("ul", { class: "warnings" }, res.notes.map((w) => h("li", {}, w))));
  void sym;
  return box;
}

function randomStrip(r) {
  const W = 560, H = 92, P = 14;
  const all = [...r.returns, r.real, 0];
  const lo = Math.min(...all), hi = Math.max(...all);
  const span = hi - lo || 1;
  const x = (v) => P + ((v - lo) * (W - 2 * P)) / span;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Returns on random price paths compared with the real backtest" });
  s.appendChild(svg("rect", { x: 0, y: 0, width: W, height: H, rx: 14, class: "chart-bg" }));
  s.appendChild(svg("line", { x1: x(0), x2: x(0), y1: 10, y2: H - 22, class: "chart-base", "stroke-dasharray": "3 4" }));
  const sorted = r.returns.slice().sort((a, b) => a - b);
  const stack = {};
  sorted.forEach((v, i) => {
    const bucket = Math.round(x(v) / 9);
    const n = (stack[bucket] = (stack[bucket] || 0) + 1);
    const c = svg("circle", { cx: x(v), cy: H - 30 - (n - 1) * 8, r: 3.6, class: "dot-random" });
    c.style.animationDelay = `${i * 12}ms`;
    s.appendChild(c);
  });
  s.appendChild(svg("line", { x1: x(r.real), x2: x(r.real), y1: 8, y2: H - 22, class: "real-line" }));
  const label = svg("text", { x: Math.min(W - P, Math.max(P, x(r.real))), y: H - 8, class: "bar-label strong", "text-anchor": "middle" });
  label.textContent = `Real: ${signed(r.real, "%")}`;
  s.appendChild(label);
  return h("div", { class: "chart" }, h("div", { class: "chart-title" }, h("h3", {}, "Real market vs random markets"),
    h("span", { class: "readout" }, `Random median ${signed(r.median, "%")}`)), s);
}

function renderBacktest(res) {
  const m = res.metrics;
  const sym = res.currency;
  const box = h("div");
  const ret = m.total_return_pct;
  append(box, [h("div", { class: "headline" },
    h("div", {}, h("div", { class: "big " + (ret >= 0 ? "gain" : "loss") }, signed(ret, "%")), h("div", { class: "lbl" }, "Return")),
    h("div", {}, h("div", { class: "big" }, num(m.max_drawdown_pct, 1) + "%"), h("div", { class: "lbl" }, "Worst drawdown")),
    h("div", {}, h("div", { class: "big" }, String(m.trades)), h("div", { class: "lbl" }, "Trades")),
    m.trades ? h("div", {}, h("div", { class: "big" }, num(m.win_rate_pct, 0) + "%"), h("div", { class: "lbl" }, "Won")) : null)]);

  const rows = [
    ["Period", `${day(m.start)} to ${day(m.end)}`],
    ["Balance", `${money(m.start_equity, sym)} to ${money(m.end_equity, sym)}`],
    ["Buy and hold, same period", signed(m.buy_and_hold_pct, "%")],
    ["Time in a trade", num(m.time_in_market_pct, 1) + "%"],
  ];
  if (m.trades) rows.push(
    ["Average win", money(m.avg_win, sym)], ["Average loss", money(m.avg_loss, sym)],
    ["Average trade", `${money(m.expectancy, sym)} (${signed(m.avg_r, "R")})`],
    ["Profit factor", m.profit_factor === null ? "No losses" : num(m.profit_factor)],
    ["Largest win / loss", `${money(m.largest_win, sym)} / ${money(m.largest_loss, sym)}`],
    ["Longest winning / losing run", `${m.longest_winning_streak} / ${m.longest_losing_streak}`],
    ["Candles held on average", `${num(m.avg_bars_held, 1)} (${hours(m.avg_hold_hours)})`],
    ["Sharpe / Sortino (yearly)", `${num(m.sharpe)} / ${num(m.sortino)}`],
    ["Longest time below a previous high", `${num(m.longest_underwater_days, 1)} days`],
    ["How trades ended", Object.entries(m.exit_reasons || {}).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(", ")],
    ["Spread paid", money(m.total_spread_cost || 0, sym) + (m.used_data_spread ? " (real bid/ask)" : "")],
    ["Overnight funding paid", money(m.total_funding || 0, sym)],
    ["Stop or target first?", m.used_finer_candles ? "Checked with finer candles" : "Stop assumed first"],
  );
  append(box, [h("dl", { class: "metrics" }, rows.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]))]);
  append(box, [h("ul", { class: "warnings" }, res.warnings.map((w) => h("li", {}, w)))]);
  if (res.why_no_trades) box.appendChild(renderWhyNoTrades(res.why_no_trades));
  // balance, with buy-and-hold over the same period for comparison
  let equityNode = null;
  const drawEquity = () => {
    const show = res.benchmark && res.benchmark.length && pref("ta-show-bh", "") === "1";
    const node = lineChart(res.equity, { title: "Balance", fmt: (v) => money(v, sym), baseline: m.start_equity,
      compare: show ? res.benchmark : null, compareLabel: "Buy and hold",
      onToggleCompare: res.benchmark && res.benchmark.length ? (on) => { savePref("ta-show-bh", on ? "1" : ""); drawEquity(); } : null,
      compareOn: show });
    if (equityNode) equityNode.replaceWith(node); else box.appendChild(node);
    equityNode = node;
  };
  drawEquity();
  // price chart: click a trade (row or marker) to zoom in on it
  let chart = null, details = null, stepper = null, current = -1;
  const rowsById = [];
  const showTrade = (k) => {
    if (!chart || k < 0 || k >= res.trades.length) return;
    current = k;
    const t = res.trades[k];
    details.open = true;
    chart.focus(t);
    rowsById.forEach((tr, i) => tr.classList.toggle("selected", i === k));
    clear(stepper);
    const hold = `held ${t.bars_held} candle${t.bars_held === 1 ? "" : "s"}`;
    const exc = t.mae_r !== undefined ? ` · worst ${signed(t.mae_r, "R", 1)}, best ${signed(t.mfe_r, "R", 1)}` : "";
    const why = t.entry_reason && Object.keys(t.entry_reason).length
      ? " · at entry " + Object.entries(t.entry_reason).map(([k2, v]) => `${k2} ${v === null ? "–" : num(v)}`).join(", ") : "";
    append(stepper, [
      h("button", { type: "button", class: "small", disabled: k === 0, onclick: () => showTrade(k - 1), "aria-label": "Previous trade" }, "‹ Prev"),
      h("span", { class: "small" }, h("strong", {}, `Trade ${k + 1} of ${res.trades.length}`), ` · `,
        h("span", { class: t.pnl >= 0 ? "gain" : "loss" }, `${money(t.pnl, sym)} (${signed(t.r_multiple, "R", 1)})`), ` · ${hold}${exc}${why}`),
      h("button", { type: "button", class: "small", disabled: k === res.trades.length - 1, onclick: () => showTrade(k + 1), "aria-label": "Next trade" }, "Next ›"),
      h("button", { type: "button", class: "quiet small", onclick: () => { current = -1; chart.clearFocus(); clear(stepper); rowsById.forEach((tr) => tr.classList.remove("selected")); } }, "Show all"),
    ]);
    details.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  if (res.chart && res.chart.candles.length) {
    chart = candleChart(res.chart, { height: 320, onMark: (mk) => showTrade(mk.trade) });
    stepper = h("div", { class: "trade-stepper inline" });
    details = h("details", { class: "chart price-details" },
      h("summary", {}, res.trades.length ? "Price chart with trades (click a trade to zoom in on it)" : "Price chart"), stepper, chart);
    append(box, [details]);
  }
  append(box, [lineChart(res.drawdown, { title: "Drawdown", fmt: (v) => num(v, 1) + "%", baseline: 0, tone: "loss", fill: true })]);
  if (res.monte_carlo) box.appendChild(renderMonteCarlo(res.monte_carlo));
  if (m.monthly_returns && m.monthly_returns.length) box.appendChild(monthlyGrid(m.monthly_returns));
  if (m.trades) {
    append(box, [h("div", { class: "chart-pair" },
      histogram(m.r_histogram, { title: "Trades by result (R)", fmt: (v) => signed(v, "R", 1), unit: "R" }),
      histogram(m.hold_histogram, { title: "Trades by time held", fmt: (v) => hours(v), unit: "" }))]);
  }
  if (res.trades.length) {
    const t = res.trades.slice().reverse();
    append(box, [h("details", { class: "chart" }, h("summary", {}, `Trades (${res.trade_count})`),
      h("div", { class: "table-wrap" }, h("table", { class: "trades" },
        h("thead", {}, h("tr", {}, ["Opened", "Entry", "Closed", "Exit", "Size", "Reason"].map((x) => h("th", {}, x)),
          h("th", { class: "num" }, "P/L"), h("th", { class: "num" }, "R"))),
        h("tbody", {}, t.map((x, ri) => {
          const k = t.length - 1 - ri;  // newest first in the table; k is the index in res.trades
          const tr = h("tr", { class: chart ? "clickable" : "", tabindex: chart ? "0" : null, title: chart ? "Show this trade on the chart" : null,
            onclick: () => showTrade(k), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showTrade(k); } } },
          h("td", {}, when(x.entry_time)), h("td", {}, num(x.entry_price)), h("td", {}, when(x.exit_time)),
          h("td", {}, num(x.exit_price)), h("td", {}, short(x.size)), h("td", {}, x.exit_reason.replace(/_/g, " ")),
          h("td", { class: "num " + (x.pnl >= 0 ? "gain" : "loss") }, money(x.pnl, sym)),
          h("td", { class: "num" }, signed(x.r_multiple, "", 2)));
          rowsById[k] = tr;
          return tr;
        })))))]);
  }
  if (res.meta) box.appendChild(testStamp(res.meta, sym));
  return box;
}

function hours(hrs) {
  if (hrs === null || hrs === undefined) return "–";
  if (hrs < 48) return `${num(hrs, 1)} h`;
  return `${num(hrs / 24, 1)} days`;
}

/** Trades reshuffled into other orders: how deep the worst drop could have been with the same trades. */
function renderMonteCarlo(mc) {
  const W = 560, H = 150, P = 8, B = 26;
  const bars = mc.histogram || [];
  const top = Math.max(1, ...bars.map((b) => b.count));
  const slot = (W - 2 * P) / Math.max(1, bars.length);
  const bw = Math.max(4, Math.min(42, slot * 0.72));
  const y = (c) => P + ((top - c) * (H - P - B)) / top;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Worst drawdown across reshuffled trade orders" });
  s.appendChild(svg("rect", { x: 0, y: 0, width: W, height: H, rx: 14, class: "chart-bg" }));
  const readout = h("span", { class: "readout" }, `${mc.runs.toLocaleString("en-GB")} reshuffles`);
  bars.forEach((b, i) => {
    const cx = P + slot * (i + 0.5);
    const bar = svg("rect", { x: cx - bw / 2, y: y(b.count), width: bw, height: Math.max(1, H - B - y(b.count)), rx: Math.min(6, bw / 2), class: "bar loss" });
    bar.style.animationDelay = `${i * 25}ms`;
    bar.addEventListener("pointerenter", () => { readout.textContent = `${num(b.from, 0)}% to ${num(b.to, 0)}%: ${b.count} of ${mc.runs} orders`; });
    bar.addEventListener("pointerleave", () => { readout.textContent = `${mc.runs.toLocaleString("en-GB")} reshuffles`; });
    s.appendChild(bar);
    if (bars.length <= 14 || i % 2 === 0) {
      const t = svg("text", { x: cx, y: H - 8, class: "bar-label", "text-anchor": "middle" });
      t.textContent = `${num(b.to, 0)}%`;
      s.appendChild(t);
    }
  });
  const stat = (label, v) => h("div", {}, h("div", { class: "big" }, `${num(Math.abs(v), 1)}%`), h("div", { class: "lbl" }, label));
  return h("div", { class: "chart monte-carlo" },
    h("div", { class: "chart-title" }, h("h3", {}, "Same trades, other orders"), readout),
    h("p", { class: "small muted" }, "The backtest's trades shuffled into random orders. The worst drop depends a lot on which trades happen to come together, so plan for the bad case, not this backtest's own order."),
    h("div", { class: "headline compact" }, stat("Mildest 1 in 20", mc.p95), stat("Typical (median)", mc.p50), stat("Worst 1 in 20", mc.p5), stat("This backtest's order", mc.actual_dd)),
    s);
}

/** Years × months grid of balance changes, with a year-to-date column. */
function monthlyGrid(years) {
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const cell = (v) => h("td", { class: "num " + (v === null ? "empty" : v > 0 ? "gain" : v < 0 ? "loss" : "") }, v === null ? "" : signed(v, "%", 1));
  return h("details", { class: "chart", open: true }, h("summary", {}, "Month by month"),
    h("div", { class: "table-wrap" }, h("table", { class: "monthly" },
      h("thead", {}, h("tr", {}, h("th", {}, "Year"), MONTHS.map((x) => h("th", { class: "num" }, x)), h("th", { class: "num ytd" }, "Year"))),
      h("tbody", {}, years.map((y) => h("tr", {}, h("th", {}, String(y.year)), y.months.map(cell), h("td", { class: "num ytd " + (y.ytd > 0 ? "gain" : y.ytd < 0 ? "loss" : "") }, signed(y.ytd, "%", 1))))))),
    h("p", { class: "small muted" }, "Change in the balance over each calendar month of the test. Blank months are outside the test period."));
}

/** Bar chart of bucket counts ({from, to, count}), e.g. trades by R or by time held. */
function histogram(buckets, { title, fmt, unit = "" }) {
  const W = 560, H = 170, P = 8, B = 26;
  let bars = buckets || [];
  while (bars.length > 1 && !bars[0].count) bars = bars.slice(1);  // drop empty tails, keep the middle
  while (bars.length > 1 && !bars[bars.length - 1].count) bars = bars.slice(0, -1);
  const top = Math.max(1, ...bars.map((b) => b.count));
  const slot = (W - 2 * P) / Math.max(1, bars.length);
  const bw = Math.max(4, Math.min(42, slot * 0.72));
  const y = (c) => P + ((top - c) * (H - P - B)) / top;
  const label = (b) => (b.from === null ? `< ${fmt(b.to)}` : b.to === null ? `> ${fmt(b.from)}` : `${fmt(b.from)} to ${fmt(b.to)}`);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title });
  s.appendChild(svg("rect", { x: 0, y: 0, width: W, height: H, rx: 14, class: "chart-bg" }));
  const total = bars.reduce((a, b) => a + b.count, 0);
  const readout = h("span", { class: "readout" }, `${total} trades`);
  bars.forEach((b, i) => {
    const cx = P + slot * (i + 0.5);
    const tone = unit !== "R" ? "ink" : b.to !== null && b.to <= 0 ? "loss" : "gain";
    const bar = svg("rect", { x: cx - bw / 2, y: y(b.count), width: bw, height: Math.max(1, H - B - y(b.count)), rx: Math.min(6, bw / 2), class: `bar ${tone}` });
    bar.style.animationDelay = `${i * 25}ms`;
    bar.addEventListener("pointerenter", () => { readout.textContent = `${label(b)}: ${b.count} trade${b.count === 1 ? "" : "s"}`; });
    bar.addEventListener("pointerleave", () => { readout.textContent = `${total} trades`; });
    s.appendChild(bar);
    const t = svg("text", { x: cx, y: H - 8, class: "bar-label", "text-anchor": "middle" });
    t.textContent = b.to === null ? `> ${fmt(b.from)}` : fmt(b.to);
    s.appendChild(t);
  });
  return h("div", { class: "chart" }, h("div", { class: "chart-title" }, h("h3", {}, title), readout), s);
}

/** Zero trades: how often each part of the entry rule held, so the blocking part stands out. */
function renderWhyNoTrades(why) {
  const rows = why.conditions || [];
  const leaves = rows.filter((r) => r.kind === "leaf");
  const least = leaves.length ? Math.min(...leaves.map((r) => (r.matched === null ? Infinity : r.matched))) : null;
  const pct = (r) => (r.matched === null || !r.candles ? "–" : `${num((r.matched / r.candles) * 100, 1)}%`);
  const list = h("ol", { class: "conditions" }, rows.map((r) => {
    const lowest = r.kind === "leaf" && r.matched === least;
    const name = r.kind === "leaf" ? renderParts(describeCondition(r.condition), "sentence small")
      : h("p", { class: "sentence small muted" }, r.kind === "all" ? "all of these together" : r.kind === "any" ? "any of these" : "not");
    return h("li", { class: (lowest ? "lowest " : "") + `depth-${Math.min(r.depth, 4)}` }, name,
      h("span", { class: "count num" }, r.matched === null ? "couldn't be checked alone" : `${r.matched.toLocaleString("en-GB")} of ${r.candles.toLocaleString("en-GB")} candles (${pct(r)})`));
  }));
  const blocked = leaves.filter((r) => r.matched === 0);
  const lede = blocked.length
    ? `${blocked.length === 1 ? "One part" : `${blocked.length} parts`} of the entry rule never held on any candle in the test period.`
    : rows.length && rows[0].matched === 0 && leaves.length > 1
      ? "Each part held on some candles, but never all on the same candle."
      : "The entry rule held on some candles; trades may have been skipped (see the notes above).";
  return h("div", { class: "chart why-no-trades" },
    h("div", { class: "chart-title" }, h("h3", {}, "Why didn't it trade?"), h("span", { class: "readout" }, `${why.candles.toLocaleString("en-GB")} candles checked`)),
    h("p", { class: "small" }, lede, " The counts show how often each part held on its own."), list);
}

/** What exactly was tested, so the same backtest can be run again and compared. */
function testStamp(meta, sym) {
  const rules = meta.version ? `Version ${meta.version} of this strategy` : "Unsaved changes (not a saved version)";
  const spread = meta.used_data_spread ? "Real bid/ask from the price data" : `${short(meta.spread)} price points`;
  const rows = [
    ["Rules", h("span", {}, rules, " ", h("span", { class: "muted" }, `(fingerprint ${meta.rules_hash})`))],
    ["Market", `${meta.market} (${meta.symbol})`],
    ["Price data", meta.source],
    ["Candle size", meta.timeframe],
    ["Period", `${day(meta.start)} to ${day(meta.end)}`],
    ["Warm-up before the period", meta.warmup_candles ? `${meta.warmup_candles} candles` : "None (the data starts here)"],
    ["Spread", spread],
    ["Overnight funding", meta.funding_pct ? `${short(meta.funding_pct)}% a year` : "None"],
    ["Tested", `${when(meta.tested_at)}, Trading Alerts ${meta.engine_version}`],
  ];
  return h("details", { class: "chart stamp" }, h("summary", {}, "How this was tested"),
    h("dl", { class: "metrics" }, rows.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])));
}

/**
 * Line chart of [isoTime, value] points. With timeX the x axis follows the clock (forward tests),
 * otherwise each point gets equal room (backtests skip nights and weekends that way).
 * band: optional [[isoTime, p10, p25, p50, p75, p90], ...] drawn behind the line.
 */
let gradSeq = 0;

/** 3-4 horizontal gridlines at round values, labelled on the right, behind the line. */
function gridlines(s, lo, hi, y, fmt, W, P) {
  const span = hi - lo;
  if (!(span > 0) || !isFinite(span)) return;
  const rough = span / 3.5;
  const mag = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((v) => v >= rough) || mag * 10;
  const inset = P + 10;  // keep labels clear of the rounded top and bottom edges
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    const yy = y(v);
    if (yy < inset || yy > s.viewBox.baseVal.height - inset) continue;
    s.appendChild(svg("line", { x1: P, x2: W - P, y1: yy.toFixed(1), y2: yy.toFixed(1), class: "chart-grid" }));
    const t = svg("text", { x: W - P - 4, y: (yy - 3).toFixed(1), "text-anchor": "end", class: "chart-grid-label" });
    t.textContent = fmt(v);
    s.appendChild(t);
  }
}

function lineChart(points, { title, fmt, baseline = null, tone = "ink", fill = true, height = 170, timeX = false, band = null,
  compare = null, compareLabel = "Comparison", onToggleCompare = null, compareOn = false }) {
  const W = 560, H = height, P = 6;
  const wrap = h("div", { class: "chart" });
  const readout = h("span", { class: "readout" });
  const toggle = onToggleCompare ? h("label", { class: "check small compare-toggle" },
    h("input", { type: "checkbox", checked: compareOn || null, onchange: (e) => onToggleCompare(e.target.checked) }), h("span", {}, `Show ${compareLabel.toLowerCase()}`)) : null;
  append(wrap, [h("div", { class: "chart-title" }, h("h3", {}, title), h("span", { class: "chart-tools" }, toggle, readout, title ? expandable(wrap) : null))]);
  if (!points || !points.length) { wrap.appendChild(h("p", { class: "muted small" }, "Not enough data to chart.")); return wrap; }
  if (points.length === 1) points = [points[0], [new Date().toISOString(), points[0][1]]];
  const ts = points.map((p) => new Date(p[0]).getTime());
  const bandRows = band && band.length > 1 ? band : null;
  const ys = points.map((p) => p[1]);
  if (bandRows) for (const b of bandRows) ys.push(b[1], b[5]);
  if (compare) for (const c of compare) ys.push(c[1]);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (baseline !== null) { lo = Math.min(lo, baseline); hi = Math.max(hi, baseline); }
  if (hi === lo) { const pad = Math.abs(hi) * 0.002 || 1; hi += pad; lo -= pad; }
  const pad = (hi - lo) * 0.06;
  hi += pad; lo -= pad;
  const t0 = ts[0], t1 = Math.max(ts[ts.length - 1], t0 + 1);
  const xt = (t) => P + ((t - t0) * (W - 2 * P)) / (t1 - t0);
  const x = (i) => (timeX ? xt(ts[i]) : P + (i * (W - 2 * P)) / (points.length - 1));
  const y = (v) => P + ((hi - v) * (H - 2 * P)) / (hi - lo);
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const first = points[0][1], last = points[points.length - 1][1];
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `${title || "Balance"} chart: from ${fmt(first)} to ${fmt(last)}, between ${fmt(Math.min(...points.map((p) => p[1])))} and ${fmt(Math.max(...points.map((p) => p[1])))}` });
  s.appendChild(svg("rect", { x: 0, y: 0, width: W, height: H, rx: 14, class: "chart-bg" }));
  if (bandRows) {
    const bx = (b) => xt(new Date(b[0]).getTime()).toFixed(1);
    const area = (a, z) => bandRows.map((b, i) => `${i ? "L" : "M"}${bx(b)},${y(b[a]).toFixed(1)}`).join("") +
      bandRows.slice().reverse().map((b) => `L${bx(b)},${y(b[z]).toFixed(1)}`).join("") + "Z";
    s.appendChild(svg("path", { d: area(1, 5), class: "band outer" }));
    s.appendChild(svg("path", { d: area(2, 4), class: "band inner" }));
    s.appendChild(svg("path", { d: bandRows.map((b, i) => `${i ? "L" : "M"}${bx(b)},${y(b[3]).toFixed(1)}`).join(""), class: "band-mid" }));
  }
  gridlines(s, lo, hi, y, fmt, W, P);
  if (baseline !== null) s.appendChild(svg("line", { x1: P, x2: W - P, y1: y(baseline), y2: y(baseline), class: "chart-base", "stroke-dasharray": "4 4" }));
  if (compare && compare.length > 1) {
    // same x mapping as the main line: by time, or by position when the main line is evenly spaced
    const cx = (k) => (timeX ? xt(new Date(compare[k][0]).getTime()) : P + (k * (W - 2 * P)) / (compare.length - 1));
    const cd = compare.map((c, k) => `${k ? "L" : "M"}${cx(k).toFixed(1)},${y(c[1]).toFixed(1)}`).join("");
    const cp = svg("path", { d: cd, class: "chart-compare", "stroke-width": 1.5 });
    const ct = svg("title", {}); ct.textContent = `${compareLabel}: ${fmt(compare[compare.length - 1][1])} at the end`;
    cp.appendChild(ct);
    s.appendChild(cp);
  }
  if (fill) {
    const gid = "g" + ++gradSeq;
    const grad = svg("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.appendChild(svg("stop", { offset: "0", class: `grad-top ${tone}` }));
    grad.appendChild(svg("stop", { offset: "1", class: `grad-bottom ${tone}` }));
    const defs = svg("defs", {});
    defs.appendChild(grad);
    s.appendChild(defs);
    const top = baseline !== null && baseline >= Math.max(...points.map((p) => p[1])); // drawdown: fill up to zero
    if (top) { grad.setAttribute("y1", 1); grad.setAttribute("y2", 0); }
    const edge = top ? y(baseline) : y(lo);
    const area = svg("path", { d: `${d}L${x(points.length - 1)},${edge}L${x(0)},${edge}Z`, class: "chart-fill grad" });
    area.style.fill = `url(#${gid})`; // inline style beats the stylesheet's flat fill
    s.appendChild(area);
  }
  s.appendChild(svg("path", { d, class: `chart-line ${tone}`, pathLength: 1, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  const end = svg("circle", { cx: x(points.length - 1), cy: y(points[points.length - 1][1]), r: 3.5, class: `chart-end ${tone}` });
  if (timeX) s.appendChild(end);
  const cursor = svg("line", { x1: 0, x2: 0, y1: P, y2: H - P, class: "chart-cursor", "stroke-width": 1, visibility: "hidden" });
  const dot = svg("circle", { r: 4, class: `chart-dot ${tone}`, visibility: "hidden" });
  s.appendChild(cursor); s.appendChild(dot);
  const base = points.length > 2 || !timeX ? `${fmt(Math.min(...points.map((p) => p[1])))} to ${fmt(Math.max(...points.map((p) => p[1])))}` : fmt(points[points.length - 1][1]);
  readout.textContent = base;
  const stamp = (iso) => (timeX ? when(iso) : day(iso));
  s.addEventListener("pointermove", (e) => {
    const rect = s.getBoundingClientRect();
    const rel = ((e.clientX - rect.left) / rect.width) * W;
    let i;
    if (timeX) {
      const t = t0 + ((rel - P) / (W - 2 * P)) * (t1 - t0);
      i = 0;
      for (let k = 0; k < ts.length; k++) if (Math.abs(ts[k] - t) < Math.abs(ts[i] - t)) i = k;
    } else i = Math.max(0, Math.min(points.length - 1, Math.round(((rel - P) / (W - 2 * P)) * (points.length - 1))));
    cursor.setAttribute("x1", x(i)); cursor.setAttribute("x2", x(i)); cursor.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(points[i][1])); dot.setAttribute("visibility", "visible");
    readout.textContent = `${stamp(points[i][0])}: ${fmt(points[i][1])}`;
  });
  s.addEventListener("pointerleave", () => {
    cursor.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); readout.textContent = base;
  });
  wrap.appendChild(s);
  const dates = h("div", { class: "inline small muted chart-dates" }, h("span", {}, stamp(points[0][0])),
    h("span", { class: "spacer" }), h("span", {}, stamp(points[points.length - 1][0])));
  wrap.appendChild(dates);
  return wrap;
}
