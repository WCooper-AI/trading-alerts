"use strict";

/* Trading Alerts web interface: Shared pieces: loading placeholders, charts, the command palette, the notifications bell.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- loading skeletons

function skeleton() {
  const block = (cls) => h("div", { class: "sk " + cls });
  return h("div", { class: "stack skeleton", "aria-busy": "true", "aria-label": "Loading" },
    h("div", {}, block("sk-title"), block("sk-line")),
    h("div", { class: "run-grid" }, [1, 2, 3].map(() => h("div", { class: "panel sk-card" }, block("sk-line w60"), block("sk-big"), block("sk-chart")))));
}

// --------------------------------------------------------------------- tiny charts

function sparkline(points, tone) {
  if (!points || points.length < 2) return null;
  const W = 96, H = 28;
  const ys = points.map((p) => p[1]);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const span = hi - lo || 1;
  const d = points.map((p, i) => `${i ? "L" : "M"}${((i / (points.length - 1)) * (W - 2) + 1).toFixed(1)},${(H - 2 - ((p[1] - lo) / span) * (H - 4)).toFixed(1)}`).join("");
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark", "aria-hidden": "true" });
  s.appendChild(svg("path", { d, class: `spark-line ${tone}` }));
  return s;
}

// --------------------------------------------------------------------- full-screen charts

function expandable(wrap, onKey = null) {
  const btn = h("button", { type: "button", class: "icon chart-expand", title: "Full screen", "aria-label": "Full screen" }, "⤢");
  const close = () => { wrap.classList.remove("fullscreen"); document.body.classList.remove("has-fullscreen"); btn.textContent = "⤢"; document.removeEventListener("keydown", keys); };
  const keys = (e) => {
    if (e.key === "Escape") close();
    else if (onKey) onKey(e);
  };
  btn.addEventListener("click", () => {
    if (wrap.classList.contains("fullscreen")) { close(); return; }
    wrap.classList.add("fullscreen"); document.body.classList.add("has-fullscreen"); btn.textContent = "✕";
    document.addEventListener("keydown", keys);
  });
  app.cleanups.push(close);
  return btn;
}

// --------------------------------------------------------------------- command palette (Ctrl+K)

let paletteCache = null;
async function openPalette() {
  if (document.querySelector(".palette")) return;
  const input = h("input", { type: "search", placeholder: "Jump to a page, strategy or market, or run an action…", "aria-label": "Search", autocomplete: "off" });
  const list = h("ul", { class: "palette-list", role: "listbox" });
  const dlg = h("dialog", { class: "palette", "aria-label": "Search and jump" }, input, list,
    h("p", { class: "small muted palette-hint" }, "↑ ↓ to choose · Enter to open · Esc to close"));
  const go = (hash) => () => { location.hash = hash; };
  const base = [
    ["Page", "Dashboard", go("#/")], ["Page", "Markets", go("#/markets")], ["Page", "Strategies", go("#/strategies")],
    ["Page", "News", go("#/news")], ["Page", "Activity", go("#/activity")], ["Page", "Journal", go("#/journal")], ["Page", "Settings", go("#/settings")],
    ["Action", "Create a strategy", go("#/create")], ["Action", "Build a strategy from scratch", go("#/new")],
    ["Action", "Test strategies together", go("#/portfolio")], ["Action", "Compare backtests side by side", go("#/compare")],
    ["Action", "Add a journal trade", () => { location.hash = "#/journal"; setTimeout(() => journalDialog(null, viewJournal), 400); }],
    ["Action", "Switch light / dark", () => setTheme(effectiveTheme() === "dark" ? "light" : "dark")],
    ["Action", "Check now", async () => { try { await POST("/run-now"); toast("Check finished"); refreshStatus(); } catch (e) { toast(e.message, true); } }],
    ["Action", "View this week's report", () => window.open("/api/report.html", "_blank", "noopener")],
    ["Action", "Download a backup", () => { location.href = "/api/backup"; }],
  ];
  for (const m of app.recipes.markets) base.push(["Market", `${m.label} chart`, () => { savePref("ta-live-market", m.key); location.hash = "#/markets"; if (location.hash === "#/markets") route(); }]);
  let items = base;
  try {
    paletteCache = paletteCache || await GET("/strategies");
    items = [...paletteCache.strategies.filter((x) => !x.error).map((x) => ["Strategy", x.data.name || x.id, go("#/s/" + x.id)]), ...base];
  } catch (_) { /* pages and actions still work */ }
  let sel = 0, shown = [];
  function render() {
    const q = input.value.trim().toLowerCase();
    shown = items.filter(([k, t]) => !q || `${k} ${t}`.toLowerCase().includes(q)).slice(0, 12);
    sel = Math.min(sel, Math.max(0, shown.length - 1));
    clear(list);
    shown.forEach(([kind, title, fn], i) => list.appendChild(h("li", { role: "option", "aria-selected": String(i === sel),
      onclick: () => { dlg.close(); fn(); }, onmousemove: () => { if (sel !== i) { sel = i; render(); } } },
      h("span", { class: "palette-kind" }, kind), h("span", {}, title))));
    if (!shown.length) list.appendChild(h("li", { class: "muted" }, "Nothing found"));
  }
  input.addEventListener("input", () => { sel = 0; render(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { sel = Math.min(sel + 1, shown.length - 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); render(); e.preventDefault(); }
    else if (e.key === "Enter" && shown[sel]) { dlg.close(); shown[sel][2](); }
    else if (e.key === "Escape") { e.preventDefault(); dlg.close(); }
  });
  const closeOnNav = () => { if (dlg.open) dlg.close(); };
  window.addEventListener("hashchange", closeOnNav, { once: true });
  dlg.addEventListener("close", () => dlg.remove());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  document.body.appendChild(dlg);
  render();
  dlg.showModal();
  input.focus();
}
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); if (app.recipes) openPalette(); }
});
document.getElementById("palette-btn").addEventListener("click", () => openPalette());
window.addEventListener("hashchange", () => { paletteCache = null; });

// --------------------------------------------------------------------- notifications bell
// A short view of the activity log. What counts as unread is kept on the server, so the
// browser and the desktop app agree.

const BELL_ICON = { closed: "✓", opened: "↗", signal: "◆", skipped: "!", price: "◎", news: "🗓", error: "!",
  strategy_saved: "✎", settings_changed: "⚙", alert_failed: "!" };
async function refreshBell(open = false) {
  const btn = document.getElementById("bell");
  const panel = document.getElementById("bell-panel");
  let res;
  try { res = await GET("/activity?limit=12"); } catch (_) { return; }
  const items = res.now.concat(res.items);
  const unread = res.unread_count;
  const badge = btn.querySelector(".badge");
  badge.hidden = !unread;
  badge.textContent = unread > 9 ? "9+" : String(unread);
  btn.setAttribute("aria-label", unread ? `Notifications, ${unread} new` : "Notifications");
  if (!open) return;
  clear(panel);
  append(panel, [h("div", { class: "bell-head" }, h("strong", {}, "Notifications"),
    h("button", { type: "button", class: "quiet", onclick: () => { panel.hidden = true; } }, "Close")),
  items.length ? h("ul", { class: "bell-list" }, items.map((x) => h("li", { class: x.unread ? "unread" : "" },
    h("a", { href: x.link, onclick: () => { panel.hidden = true; } },
      h("span", { class: "bell-icon " + x.kind }, BELL_ICON[x.kind] || "•"),
      h("span", {}, h("span", { class: "bell-title" }, x.title), x.body ? h("span", { class: "small muted bell-body" }, x.body.split("\n")[0]) : null,
        h("span", { class: "small muted" }, when(x.time)))))))
    : h("p", { class: "muted small bell-empty" }, "Nothing yet. Signals, trades, price alerts and news reminders show up here."),
  h("div", { class: "bell-foot" }, h("a", { class: "button small", href: "#/activity", onclick: () => { panel.hidden = true; } }, "All activity"))]);
  panel.hidden = false;
  if (unread) {
    badge.hidden = true;
    try { await POST("/activity/seen"); } catch (_) { /* counted as read next time */ }
  }
}
document.getElementById("bell").addEventListener("click", (e) => {
  e.stopPropagation();
  const panel = document.getElementById("bell-panel");
  if (!panel.hidden) { panel.hidden = true; return; }
  toggleRunnerPanel(false);
  refreshBell(true);
});
document.addEventListener("click", (e) => {
  const panel = document.getElementById("bell-panel");
  if (!panel.hidden && !panel.contains(e.target)) panel.hidden = true;
});

// --------------------------------------------------------------------- readable accent text (contrast)

function contrastRatio(a, b) {
  const lum = (hex) => {
    const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}
function mix(hex, toward, t) {
  const a = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const b = [1, 3, 5].map((i) => parseInt(toward.slice(i, i + 2), 16));
  return "#" + a.map((v, i) => Math.round(v + (b[i] - v) * t).toString(16).padStart(2, "0")).join("");
}
/** The accent, darkened or lightened just enough to be readable as text on the page (WCAG 4.5:1). */
function readableAccent(accent, dark) {
  const bg = dark ? "#0F151C" : "#E8EBEE";
  const toward = dark ? "#ffffff" : "#000000";
  let c = accent;
  for (let t = 0; t <= 1 && contrastRatio(c, bg) < 4.5; t += 0.05) c = mix(accent, toward, t);
  return c;
}

// --------------------------------------------------------------------- candlestick chart (live)

function priceDigits(v) {
  const a = Math.abs(v);
  return a >= 1000 ? 1 : a >= 100 ? 2 : a >= 10 ? 3 : a >= 1 ? 4 : 5;
}
function fmtPrice(v, d) { return v === null || v === undefined ? "–" : nf(d).format(v); }

/**
 * Candles with trades, signals and price lines. Drag to scroll back, + and − to zoom,
 * click to pick a price (onPick). Call .update(data) with fresh data; the view stays put.
 */
function candleChart(initial, { height = 380, onPick = null, title = null, onMark = null } = {}) {
  const W = 900, H = height, AX = 70, BOT = 24, TOP = 8;
  const wrap = h("div", { class: "candles" });
  const readout = h("div", { class: "candle-readout" });
  const svgEl = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": (title || "Price") + " candlestick chart" });
  const zoomIn = h("button", { type: "button", class: "icon", title: "Zoom in", "aria-label": "Zoom in" }, "+");
  const zoomOut = h("button", { type: "button", class: "icon", title: "Zoom out", "aria-label": "Zoom out" }, "−");
  const latest = h("button", { type: "button", class: "quiet", hidden: true }, "Back to latest");
  const resetZoom = h("button", { type: "button", class: "quiet", hidden: true, title: "Show the latest 120 candles again" }, "Reset");
  const full = expandable(wrap, (e) => keyNav(e));
  svgEl.setAttribute("tabindex", "0");
  svgEl.addEventListener("keydown", (e) => keyNav(e));
  const legend = h("div", { class: "chart-legend small" });
  append(wrap, [h("div", { class: "candle-top" }, readout, h("span", { class: "spacer" }), latest, resetZoom, zoomOut, zoomIn, full), svgEl, legend,
    h("p", { class: "small muted candle-hint" }, onPick ? "Drag to scroll back. Click a price to fill in an alert." : "Drag to scroll back in time.")]);
  let data = initial;
  // indicators the strategy reads: on the price pane, or in an oscillator pane below it
  let hiddenInd = new Set();
  try { hiddenInd = new Set(JSON.parse(pref("ta-ind-hidden", "[]"))); } catch (_) { /* fresh */ }
  const shownInd = () => (data.indicators || []).filter((ind) => !hiddenInd.has(ind.label));
  const OSC_SHARE = 0.28;
  const DEFAULT_VISIBLE = 120;
  let visible = Math.min(DEFAULT_VISIBLE, (data.candles || []).length || DEFAULT_VISIBLE);
  let offset = 0; // candles hidden on the right (0 = showing the latest)
  let first = true;
  const bar = () => {
    const c = data.candles;
    return c.length > 1 ? new Date(c[c.length - 1][0]) - new Date(c[c.length - 2][0]) : 3600000;
  };
  let geom = null;
  let focus = null;  // { entryIdx, exitIdx, lines } while looking at one trade

  function draw() {
    clear(svgEl);
    const all = data.candles || [];
    if (!all.length) { readout.textContent = "No prices yet"; return; }
    visible = Math.max(20, Math.min(visible, all.length));
    offset = Math.max(0, Math.min(offset, all.length - visible));
    latest.hidden = offset === 0;
    resetZoom.hidden = visible === Math.min(DEFAULT_VISIBLE, all.length) && offset === 0;
    const end = all.length - offset;
    const view = all.slice(end - visible, end);
    const inds = shownInd();
    const oscs = inds.filter((ind) => ind.pane === "oscillator");
    const PB = oscs.length ? Math.round(H - BOT - H * OSC_SHARE) : H - BOT;  // bottom of the price pane
    const at = (ind, i) => ind.values[ind.values.length - all.length + (end - visible) + i];  // value for view candle i
    let lo = Math.min(...view.map((c) => c[3])), hi = Math.max(...view.map((c) => c[2]));
    for (const ind of inds) if (ind.pane === "price") view.forEach((_, i) => { const v = at(ind, i); if (v !== null && v !== undefined) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
    const span0 = hi - lo || Math.abs(hi) * 0.001 || 1;
    for (const l of [...(data.lines || []), ...(focus ? focus.lines : []), ...(data.alerts || []).map((a) => ({ price: a.price }))]) {
      if (l.price > lo - span0 * 0.6 && l.price < hi + span0 * 0.6) { lo = Math.min(lo, l.price); hi = Math.max(hi, l.price); }
    }
    const pad = (hi - lo || 1) * 0.06;
    lo -= pad; hi += pad;
    const d = priceDigits(view[view.length - 1][4]);
    const slot = (W - AX - 8) / visible;
    const x = (i) => 8 + slot * (i + 0.5);
    const y = (v) => TOP + ((hi - v) * (PB - TOP)) / (hi - lo);
    geom = { view, slot, x, y, lo, hi, d, end, PB, inds, at };
    svgEl.appendChild(svg("rect", { x: 0, y: 0, width: W, height: H, rx: 8, class: "chart-bg" }));
    // grid + price axis
    for (let k = 0; k <= 4; k++) {
      const v = lo + ((hi - lo) * k) / 4;
      if (oscs.length && k === 0) continue;
      svgEl.appendChild(svg("line", { x1: 8, x2: W - AX, y1: y(v), y2: y(v), class: "grid" }));
      const t = svg("text", { x: W - AX + 8, y: y(v) + 4, class: "axis-label" });
      t.textContent = fmtPrice(v, d);
      svgEl.appendChild(t);
    }
    // time labels
    const every = Math.max(1, Math.round(visible / 5));
    const daily = bar() >= 86400000;
    view.forEach((c, i) => {
      if (i % every !== Math.floor(every / 2)) return;
      const t = svg("text", { x: x(i), y: H - 7, class: "axis-label", "text-anchor": "middle" });
      const dt = new Date(c[0]);
      t.textContent = daily ? dt.toLocaleDateString("en-GB", { day: "numeric", month: "short" })
        : dt.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
      svgEl.appendChild(t);
    });
    // the trade being looked at: shade the candles it was open for
    if (focus) {
      const a = focus.entryIdx - (end - visible), b = focus.exitIdx - (end - visible);
      const x1 = Math.max(8, x(a) - slot / 2), x2 = Math.min(W - AX, x(b) + slot / 2);
      if (x2 > x1) svgEl.appendChild(svg("rect", { x: x1, y: TOP, width: x2 - x1, height: H - TOP - BOT, class: "trade-span" }));
    }
    // candles
    const bw = Math.max(1, Math.min(14, slot * 0.68));
    const g = svg("g", { class: first ? "candle-group enter" : "candle-group" });
    view.forEach((c, i) => {
      const up = c[4] >= c[1];
      const cls = up ? "up" : "down";
      g.appendChild(svg("line", { x1: x(i), x2: x(i), y1: y(c[2]), y2: y(c[3]), class: "wick " + cls }));
      const top = y(Math.max(c[1], c[4]));
      g.appendChild(svg("rect", { x: x(i) - bw / 2, y: top, width: bw, height: Math.max(1, Math.abs(y(c[1]) - y(c[4]))),
        rx: Math.min(2, bw / 3), class: "body " + cls }));
    });
    svgEl.appendChild(g);
    // indicators on the price pane: lines, and a light fill between a pair of bands
    const linePath = (ind, yy) => {
      let dd = "", pen = false;
      view.forEach((_, i) => { const v = at(ind, i); if (v === null || v === undefined) { pen = false; return; } dd += `${pen ? "L" : "M"}${x(i).toFixed(1)},${yy(v).toFixed(1)}`; pen = true; });
      return dd;
    };
    const tone = (ind) => "ind-" + ((data.indicators || []).indexOf(ind) % 4);
    const priceInds = inds.filter((ind) => ind.pane === "price");
    const pts = (ind, yy) => view.map((_, i) => [at(ind, i), i]).filter(([v]) => v !== null && v !== undefined).map(([v, i]) => `${x(i).toFixed(1)},${yy(v).toFixed(1)}`);
    for (const [up, down] of [["bollinger_upper", "bollinger_lower"], ["keltner_upper", "keltner_lower"]]) {
      const a = priceInds.find((ind) => ind.name === up), b = priceInds.find((ind) => ind.name === down);
      if (!a || !b) continue;
      const top = pts(a, y), bottom = pts(b, y);
      if (top.length && bottom.length) svgEl.appendChild(svg("path", { d: "M" + top.join("L") + "L" + bottom.reverse().join("L") + "Z", class: "ind-fill " + tone(a) }));
    }
    for (const ind of priceInds) {
      const dd = linePath(ind, y);
      if (dd) svgEl.appendChild(svg("path", { d: dd, class: "ind-line " + tone(ind), "stroke-width": 1.5 }));
    }
    // oscillator pane
    if (oscs.length) {
      const OT = PB + 12, OB = H - BOT;
      let olo = Infinity, ohi = -Infinity;
      for (const ind of oscs) {
        view.forEach((_, i) => { const v = at(ind, i); if (v !== null && v !== undefined) { olo = Math.min(olo, v); ohi = Math.max(ohi, v); } });
        for (const lv of ind.levels || []) { olo = Math.min(olo, lv); ohi = Math.max(ohi, lv); }
      }
      if (!isFinite(olo)) { olo = 0; ohi = 1; }
      if (ohi === olo) { ohi += 1; olo -= 1; }
      const opad = (ohi - olo) * 0.08; olo -= opad; ohi += opad;
      const oy = (v) => OT + ((ohi - v) * (OB - OT)) / (ohi - olo);
      geom.osc = { oy, olo, ohi, OT, OB };
      svgEl.appendChild(svg("line", { x1: 8, x2: W - AX, y1: PB + 6, y2: PB + 6, class: "pane-split" }));
      for (let k = 0; k <= 2; k++) {
        const v = olo + ((ohi - olo) * k) / 2;
        svgEl.appendChild(svg("line", { x1: 8, x2: W - AX, y1: oy(v), y2: oy(v), class: "grid" }));
        const t = svg("text", { x: W - AX + 8, y: oy(v) + 4, class: "axis-label" });
        t.textContent = short(+v.toFixed(2));
        svgEl.appendChild(t);
      }
      const drawn = new Set();
      for (const ind of oscs) for (const lv of ind.levels || []) {
        if (drawn.has(lv)) continue;
        drawn.add(lv);
        svgEl.appendChild(svg("line", { x1: 8, x2: W - AX, y1: oy(lv), y2: oy(lv), class: "osc-level", "stroke-dasharray": "4 4" }));
        const t = svg("text", { x: 14, y: oy(lv) - 3, class: "pline-label level" });
        t.textContent = short(lv);
        svgEl.appendChild(t);
      }
      for (const ind of oscs) {
        if (ind.name === "macd_hist") {
          view.forEach((_, i) => { const v = at(ind, i); if (v === null || v === undefined) return;
            svgEl.appendChild(svg("rect", { x: x(i) - bw / 2, y: Math.min(oy(v), oy(0)), width: bw, height: Math.max(1, Math.abs(oy(v) - oy(0))), class: "osc-bar " + (v >= 0 ? "up" : "down") })); });
          continue;
        }
        const dd = linePath(ind, oy);
        if (dd) svgEl.appendChild(svg("path", { d: dd, class: "ind-line " + tone(ind), "stroke-width": 1.5 }));
      }
    }
    // legend: click an indicator to hide or show it
    clear(legend);
    for (const ind of data.indicators || []) {
      const off = hiddenInd.has(ind.label);
      legend.appendChild(h("button", { type: "button", class: "legend-item" + (off ? " off" : ""), "aria-pressed": String(!off),
        title: off ? `Show ${ind.label}` : `Hide ${ind.label}`,
        onclick: () => { if (off) hiddenInd.delete(ind.label); else hiddenInd.add(ind.label); savePref("ta-ind-hidden", JSON.stringify([...hiddenInd])); draw(); } },
        h("span", { class: "swatch line " + tone(ind) }), ind.label));
    }
    // price lines (stop/target) and alert lines
    const hline = (price, label, cls) => {
      if (price < lo || price > hi) return;
      svgEl.appendChild(svg("line", { x1: 8, x2: W - AX, y1: y(price), y2: y(price), class: "pline " + cls }));
      const tag = svg("text", { x: 14, y: y(price) - 5, class: "pline-label " + cls });
      tag.textContent = `${label} ${fmtPrice(price, d)}`;
      svgEl.appendChild(tag);
    };
    for (const l of data.lines || []) hline(l.price, l.label, l.kind || "level");
    for (const l of focus ? focus.lines : []) hline(l.price, l.label, l.kind);
    for (const a of data.alerts || []) hline(a.price, a.direction === "above" ? "Alert above" : "Alert below", "alert");
    // marks
    const t0 = new Date(view[0][0]).getTime(), bl = bar();
    const idxOf = (iso) => {
      const t = new Date(iso).getTime();
      if (t < t0) return -1;
      let lo2 = 0, hi2 = view.length - 1;
      while (lo2 < hi2) { const m = (lo2 + hi2 + 1) >> 1; if (new Date(view[m][0]).getTime() <= t) lo2 = m; else hi2 = m - 1; }
      return t - new Date(view[lo2][0]).getTime() < bl * 1.5 ? lo2 : -1;
    };
    for (const m of data.marks || []) {
      const i = idxOf(m.time);
      if (i < 0) continue;
      const c = view[i];
      let shape;
      if (m.kind === "buy") shape = svg("path", { d: `M${x(i)},${y(c[3]) + 6}l6,10h-12z`, class: "mark-buy" });
      else if (m.kind === "sell") shape = svg("path", { d: `M${x(i)},${y(c[2]) - 6}l6,-10h-12z`, class: "mark-sell" });
      else if (m.kind === "exit") shape = svg("circle", { cx: x(i), cy: y(m.price), r: 4.5, class: "mark-exit" + (m.gain ? " gain" : " loss") });
      else shape = svg("path", { d: `M${x(i)},${y(c[2]) - 14}l4,4l-4,4l-4,-4z`, class: "mark-signal" });
      const tt = svg("title", {});
      tt.textContent = `${when(m.time)}: ${m.label}`;
      shape.appendChild(tt);
      if (onMark && m.trade !== undefined) {
        shape.classList.add("clickable");
        shape.addEventListener("pointerup", (e) => { e.stopPropagation(); onMark(m); });
      }
      svgEl.appendChild(shape);
    }
    // last price
    const last = all[all.length - 1][4];
    if (last >= lo && last <= hi) {
      svgEl.appendChild(svg("line", { x1: 8, x2: W - AX, y1: y(last), y2: y(last), class: "last-line" }));
      svgEl.appendChild(svg("rect", { x: W - AX + 2, y: y(last) - 10, width: AX - 4, height: 20, rx: 6, class: "last-box" }));
      const t = svg("text", { x: W - AX / 2, y: y(last) + 4, class: "last-text", "text-anchor": "middle" });
      t.textContent = fmtPrice(last, d);
      svgEl.appendChild(t);
    }
    // crosshair
    geom.cx = svg("line", { y1: TOP, y2: H - BOT, class: "cross", visibility: "hidden" });
    geom.cy = svg("line", { x1: 8, x2: W - AX, class: "cross", visibility: "hidden" });
    geom.cbox = svg("rect", { x: W - AX + 2, width: AX - 4, height: 20, rx: 6, class: "cross-box", visibility: "hidden" });
    geom.ctext = svg("text", { x: W - AX / 2, class: "cross-text", "text-anchor": "middle", visibility: "hidden" });
    for (const k of ["cx", "cy", "cbox", "ctext"]) svgEl.appendChild(geom[k]);
    showReadout(view.length - 1);
    first = false;
  }

  function showReadout(i) {
    const c = geom.view[i];
    const d = geom.d;
    const chg = c[4] - c[1];
    clear(readout);
    append(readout, [h("strong", {}, when(c[0])),
      ` O ${fmtPrice(c[1], d)}  H ${fmtPrice(c[2], d)}  L ${fmtPrice(c[3], d)}  C ${fmtPrice(c[4], d)} `,
      h("span", { class: chg >= 0 ? "gain" : "loss" }, `${chg >= 0 ? "+" : ""}${fmtPrice(chg, d)}`),
      ...(geom.inds || []).map((ind) => { const v = geom.at(ind, i); return h("span", { class: "readout-ind" }, ` ${ind.label} ${v === null || v === undefined ? "–" : short(+v.toFixed(ind.pane === "price" ? d : 2))}`); })]);
  }

  const toLocal = (e) => {
    const r = svgEl.getBoundingClientRect();
    return { px: ((e.clientX - r.left) / r.width) * W, py: ((e.clientY - r.top) / r.height) * H };
  };
  let drag = null;
  svgEl.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, offset, moved: false }; svgEl.setPointerCapture(e.pointerId); });
  svgEl.addEventListener("pointermove", (e) => {
    if (!geom) return;
    if (drag) {
      const r = svgEl.getBoundingClientRect();
      const bars = Math.round(((e.clientX - drag.x) / r.width) * W / geom.slot);
      if (Math.abs(e.clientX - drag.x) > 4) drag.moved = true;
      if (drag.moved && bars !== offset - drag.offset) { offset = drag.offset + bars; draw(); }
    }
    const { px, py } = toLocal(e);
    if (px > W - AX || py < TOP || py > H - BOT) return;
    const i = Math.max(0, Math.min(geom.view.length - 1, Math.round((px - 8) / geom.slot - 0.5)));
    const inPrice = py <= geom.PB;
    const price = inPrice ? geom.hi - ((py - TOP) * (geom.hi - geom.lo)) / (geom.PB - TOP)
      : geom.osc ? geom.osc.ohi - ((py - geom.osc.OT) * (geom.osc.ohi - geom.osc.olo)) / (geom.osc.OB - geom.osc.OT) : null;
    geom.cx.setAttribute("x1", geom.x(i)); geom.cx.setAttribute("x2", geom.x(i));
    geom.cy.setAttribute("y1", py); geom.cy.setAttribute("y2", py);
    geom.cbox.setAttribute("y", py - 10);
    geom.ctext.setAttribute("y", py + 4);
    geom.ctext.textContent = price === null ? "" : inPrice ? fmtPrice(price, geom.d) : short(+price.toFixed(2));
    for (const k of ["cx", "cy", "cbox", "ctext"]) geom[k].setAttribute("visibility", "visible");
    showReadout(i);
  });
  svgEl.addEventListener("pointerup", (e) => {
    const wasDrag = drag && drag.moved;
    drag = null;
    if (!wasDrag && onPick && geom) {
      const { py } = toLocal(e);
      if (py >= TOP && py <= geom.PB) onPick(+(geom.hi - ((py - TOP) * (geom.hi - geom.lo)) / (geom.PB - TOP)).toFixed(geom.d));
    }
  });
  svgEl.addEventListener("pointerleave", () => {
    if (!geom) return;
    for (const k of ["cx", "cy", "cbox", "ctext"]) geom[k].setAttribute("visibility", "hidden");
    showReadout(geom.view.length - 1);
  });
  function keyNav(e) {
    const step = Math.max(1, Math.round(visible / 10));
    if (e.key === "ArrowLeft") offset += step;
    else if (e.key === "ArrowRight") offset -= step;
    else if (e.key === "+" || e.key === "=") visible = Math.round(visible / 1.4);
    else if (e.key === "-") visible = Math.round(visible * 1.4);
    else if (e.key === "End") offset = 0;
    else return;
    e.preventDefault();
    draw();
  }
  /** Zoom by ``factor`` keeping the candle at view index ``i`` under the pointer. */
  function zoomAt(factor, i) {
    const all = data.candles || [];
    const want = Math.max(20, Math.min(all.length, Math.round(visible * factor)));
    if (want === visible) return;
    const rightOf = visible - 1 - i;  // candles to the right of the pointer, kept in proportion
    offset = Math.max(0, Math.round(offset - (want - visible) * (rightOf / Math.max(1, visible - 1))));
    visible = want;
    draw();
  }
  svgEl.addEventListener("wheel", (e) => {
    if (!geom) return;
    e.preventDefault();  // the page doesn't scroll while zooming the chart
    const { px } = toLocal(e);
    const i = Math.max(0, Math.min(geom.view.length - 1, Math.round((px - 8) / geom.slot - 0.5)));
    const factor = e.ctrlKey ? Math.exp(e.deltaY * 0.01) : e.deltaY > 0 ? 1.25 : 0.8;  // ctrl: trackpad pinch
    zoomAt(factor, i);
  }, { passive: false });
  // touch pinch: two pointers
  const touches = new Map();
  let pinch = null;
  svgEl.addEventListener("pointerdown", (e) => { if (e.pointerType === "touch") { touches.set(e.pointerId, e.clientX); if (touches.size === 2) { pinch = { span: Math.abs([...touches.values()].reduce((a, b) => a - b)), visible }; drag = null; } } });
  svgEl.addEventListener("pointermove", (e) => {
    if (!pinch || !touches.has(e.pointerId)) return;
    touches.set(e.pointerId, e.clientX);
    const span = Math.abs([...touches.values()].reduce((a, b) => a - b));
    if (span > 10) { const want = Math.round(pinch.visible * pinch.span / span); if (want !== visible) zoomAt(want / visible, Math.floor(visible / 2)); }
  });
  const endTouch = (e) => { touches.delete(e.pointerId); if (touches.size < 2) pinch = null; };
  svgEl.addEventListener("pointerup", endTouch);
  svgEl.addEventListener("pointercancel", endTouch);
  zoomIn.addEventListener("click", () => { visible = Math.round(visible / 1.4); draw(); });
  zoomOut.addEventListener("click", () => { visible = Math.round(visible * 1.4); draw(); });
  latest.addEventListener("click", () => { offset = 0; draw(); });
  resetZoom.addEventListener("click", () => { visible = DEFAULT_VISIBLE; offset = 0; draw(); });
  const indexOf = (iso) => {
    const all = data.candles || [], t = new Date(iso).getTime();
    let i = 0;
    while (i < all.length - 1 && new Date(all[i + 1][0]).getTime() <= t) i++;
    return i;
  };
  /** Zoom to one trade: its candles with ``context`` candles either side, and its price levels. */
  wrap.focus = (trade, { context = 20 } = {}) => {
    const all = data.candles || [];
    if (!all.length) return;
    const entryIdx = indexOf(trade.entry_time), exitIdx = trade.exit_time ? indexOf(trade.exit_time) : all.length - 1;
    const lines = [{ price: trade.entry_price, label: "Entry", kind: "entry" }];
    if (trade.exit_price !== null && trade.exit_price !== undefined) lines.push({ price: trade.exit_price, label: "Exit", kind: "exit" });
    if (trade.stop) lines.push({ price: trade.stop, label: "Stop", kind: "stop" });
    if (trade.target) lines.push({ price: trade.target, label: "Target", kind: "target" });
    focus = { entryIdx, exitIdx, lines };
    visible = Math.max(20, exitIdx - entryIdx + 1 + 2 * context);
    offset = Math.max(0, all.length - (exitIdx + context + 1));
    draw();
  };
  wrap.clearFocus = () => { focus = null; visible = DEFAULT_VISIBLE; offset = 0; draw(); };
  wrap.update = (next) => {
    const grew = (next.candles || []).length - (data.candles || []).length;
    if (offset && grew > 0) offset += grew; // keep looking at the same candles while scrolled back
    data = next;
    draw();
  };
  draw();
  return wrap;
}

/** Re-runs fn every `seconds` while this page is open and the tab is visible. */
function every(seconds, fn) {
  const id = setInterval(() => { if (!document.hidden) fn(); }, seconds * 1000);
  app.timers.push(id);
  return id;
}
