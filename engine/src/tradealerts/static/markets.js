"use strict";

/* Trading Alerts web interface: Markets: live charts and price alerts.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- markets: live charts + price alerts

const LIVE_TFS = [["5m", "5m"], ["15m", "15m"], ["1h", "1h"], ["4h", "4h"], ["1d", "1d"]];

async function viewMarkets() {
  setNav("markets");
  let market = pref("ta-live-market", "us500");
  if (!marketByKey(market)) market = "us500";
  let tf = pref("ta-live-tf", "15m");
  let overlay = "";
  const list = await GET("/strategies");
  const page = h("div", { class: "stack" });
  const head = h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Markets"),
    h("p", { class: "muted dash-lede" }, "Live prices for the built-in markets, refreshed every 30 seconds, with your price alerts.")),
    marketsTabs("live"));
  const marketSel = h("select", { "aria-label": "Market" },
    [...new Set(app.recipes.markets.map((m) => m.group))].map((g) => h("optgroup", { label: g },
      app.recipes.markets.filter((m) => m.group === g).map((m) => h("option", { value: m.key }, m.label)))));
  marketSel.value = market;
  const tfSeg = h("div", { class: "segmented", role: "group", "aria-label": "Candle size" });
  const overlaySel = h("select", { "aria-label": "Show signals from a strategy" });
  const priceBig = h("div", { class: "live-price" });
  const status = h("span", { class: "live-status small muted" });
  const chartBox = h("div", { class: "chart-box" });
  const alertsBox = h("div");
  let chart = null, loading = false, reqSeq = 0, lastData = null, fails = 0;

  const pricePick = { set: null };
  const makeLink = h("span", { class: "make-link" });
  function renderTf() {
    clear(tfSeg);
    for (const [v, t] of LIVE_TFS) tfSeg.appendChild(h("button", { type: "button", "aria-pressed": String(tf === v),
      onclick: () => { tf = v; savePref("ta-live-tf", v); renderTf(); load(true); } }, t));
  }
  function renderOverlay() {
    const mine = list.strategies.filter((x) => !x.error && (x.data.market || {}).provider === "auto" && (x.data.market || {}).symbol === market);
    clear(overlaySel);
    overlaySel.appendChild(h("option", { value: "" }, mine.length ? "No strategy signals" : "No strategies on this market"));
    for (const x of mine) overlaySel.appendChild(h("option", { value: x.id }, `Signals: ${x.data.name}`));
    if (!mine.some((x) => x.id === overlay)) overlay = "";
    overlaySel.value = overlay;
    overlaySel.disabled = !mine.length;
    overlaySel.hidden = !mine.length;
    clear(makeLink);
    if (!mine.length) makeLink.appendChild(h("a", { class: "button small", href: "#/create/" + encodeURIComponent(market) },
      `+ Create a strategy for ${marketByKey(market).label}`));
  }
  marketSel.addEventListener("change", () => { market = marketSel.value; savePref("ta-live-market", market); renderOverlay(); load(true); renderAlerts(); });
  overlaySel.addEventListener("change", () => {
    overlay = overlaySel.value;
    const st = list.strategies.find((x) => x.id === overlay);
    const stf = st && (st.data.market || {}).timeframe;
    if (stf && LIVE_TFS.some(([v]) => v === stf)) { tf = stf; renderTf(); } // signals only make sense on the strategy's own candles
    load(true);
  });

  async function load(reset = false) {
    if (loading && !reset) return;
    loading = true;
    const seq = ++reqSeq;
    if (reset) {
      chart = null;
      const slow = h("p", { class: "small muted" });
      clear(chartBox).appendChild(h("div", { class: "loading" }, "Loading prices…", slow));
      setTimeout(() => { if (seq === reqSeq && !chart) slow.textContent = "The first download for a market can take up to a minute. After that it's saved, so it's quick."; }, 6000);
    }
    try {
      const q = overlay ? `strategy=${encodeURIComponent(overlay)}&timeframe=${tf}` : `market=${market}&timeframe=${tf}`;
      const d = await GET(`/candles?${q}&bars=300`, { timeout: chart ? 40 : 120 });
      if (seq !== reqSeq) return;
      if (!d.candles || !d.candles.length) throw new ApiError("No prices for that market yet");
      fails = 0;
      lastData = d;
      const c = d.candles;
      const last = c[c.length - 1][4], dg = priceDigits(last);
      const dayAgo = new Date(c[c.length - 1][0]).getTime() - 86400000;
      const ref = (c.find((x) => new Date(x[0]).getTime() >= dayAgo) || c[0])[1];
      const chg = last - ref;
      clear(priceBig);
      append(priceBig, [h("span", { class: "big" }, fmtPrice(last, dg)),
        h("span", { class: chg >= 0 ? "gain" : "loss" }, ` ${chg >= 0 ? "+" : ""}${fmtPrice(chg, dg)} (${signed((chg / ref) * 100, "%")}) over 24h`),
        d.spread !== null ? h("span", { class: "small muted" }, ` · spread ${fmtPrice(d.spread, dg)}`) : null]);
      clear(status);
      status.appendChild(feedBadge(d));
      clear(qualityBox);
      if (d.quality && d.quality.length) qualityBox.appendChild(h("ul", { class: "warnings quality-strip", "aria-label": "Data quality warnings" },
        mergeGapNotes(d.quality).map((w) => h("li", {}, w.text))));
      if (alertList.update) alertList.update(d);
      if (!chart) {
        chart = candleChart(d, { onPick: (p) => pricePick.set && pricePick.set(p), title: d.label });
        clear(chartBox).appendChild(chart);
      } else chart.update(d);
    } catch (e) {
      if (seq !== reqSeq) return;
      fails += 1;
      const retry = h("button", { type: "button", class: "quiet", onclick: () => load(!chart) }, "Try again");
      clear(status);
      append(status, [h("span", { class: "loss" }, chart ? "Couldn't refresh, still showing the last prices. " : ""), retry]);
      if (!chart) clear(chartBox).appendChild(h("div", { class: "error-box" }, h("p", {}, e.message),
        h("p", { class: "small" }, "It will try again by itself in a few seconds."),
        h("button", { type: "button", onclick: () => load(true) }, "Try again now")));
      // Try again soon, backing off, rather than waiting for the next 30-second refresh.
      const wait = Math.min(5 * 2 ** (fails - 1), 30);
      const again = setTimeout(() => { if (seq === reqSeq && document.body.contains(chartBox)) load(!chart); }, wait * 1000);
      app.cleanups.push(() => clearTimeout(again));
    } finally { if (seq === reqSeq) loading = false; }
  }

  const qualityBox = h("div", { class: "quality-box" });
  const alertList = {};  // alertList.update(d) refreshes the distance-to-target text when new prices arrive

  async function renderAlerts(editing = null) {
    /* editing: an existing alert whose values fill the form; saving then updates it in place. */
    const res = await GET("/price-alerts");
    const mine = res.alerts.filter((a) => a.market === market);
    const others = res.alerts.filter((a) => a.market !== market && a.active);
    const dir = { v: editing ? editing.direction : "above" };
    let price = editing ? editing.price : null;
    const opts = { repeat: editing ? editing.repeat || "once" : "once", cooldown: editing ? editing.cooldown_minutes : 60,
      cross: editing ? !!editing.cross : false };
    const dirSeg = h("div", { class: "segmented", role: "group", "aria-label": "Above or below" });
    const renderDir = () => {
      clear(dirSeg);
      for (const [v, t] of [["above", "Goes above"], ["below", "Goes below"]]) dirSeg.appendChild(h("button", { type: "button",
        "aria-pressed": String(dir.v === v), onclick: () => { dir.v = v; renderDir(); } }, t));
    };
    renderDir();
    const priceIn = numberInput(price, (n) => { price = n; }, { label: "Price", step: "any", min: "0", placeholder: "Price", allowEmpty: true, cls: "no-spin" });
    pricePick.set = (p) => {
      priceIn.value = p; price = p; priceIn.dispatchEvent(new Event("input"));
      if (lastData) { const last = lastData.candles[lastData.candles.length - 1][4]; dir.v = p >= last ? "above" : "below"; renderDir(); }
      priceIn.focus();
    };
    const note = h("input", { class: "wide", placeholder: "Note (optional)", "aria-label": "Note", maxlength: 200, value: editing ? editing.note || "" : "" });
    const cooldownIn = numberInput(opts.cooldown, (n) => { opts.cooldown = n; }, { label: "Cooldown in minutes", integer: true, min: "0", cls: "no-spin" });
    const cooldownWrap = h("span", { class: "inline small" }, "then wait ", cooldownIn, " min");
    const repeatSel = choiceSelect([["once", "Once, then switch off"], ["every", "Every time, after a cooldown"]], opts.repeat,
      (v) => { opts.repeat = v; cooldownWrap.hidden = v !== "every"; }, { label: "How often" });
    cooldownWrap.hidden = opts.repeat !== "every";
    const crossBox = h("input", { type: "checkbox", checked: opts.cross, onchange: (ev) => { opts.cross = ev.target.checked; } });
    const crossLbl = h("label", { class: "small inline" }, crossBox, " Only when it crosses the level",
      infoTip("Ticked: the price has to come from the other side of your level, so a market already past it doesn't fire straight away. Unticked: any touch of the level counts."));
    const add = h("button", { class: "primary", type: "button" }, editing ? "Save changes" : "Add alert");
    const cancel = editing ? h("button", { type: "button", onclick: () => renderAlerts() }, "Cancel") : null;
    add.addEventListener("click", async () => {
      if (isBlank(price)) {
        priceIn.setAttribute("aria-invalid", "true"); priceIn.focus();
        toast("Fill in the price, or click the chart", true); return;
      }
      if (opts.repeat === "every" && isBlank(opts.cooldown)) { toast("Fill in the cooldown", true); return; }
      const body = { market, direction: dir.v, price, note: note.value, repeat: opts.repeat, cooldown_minutes: opts.cooldown || 0, cross: opts.cross };
      try {
        if (editing) { await PUT(`/price-alerts/${editing.id}`, { ...body, active: true }); toast("Alert updated"); }
        else { await POST("/price-alerts", body); toast("Alert added"); }
        renderAlerts(); load();
      } catch (e) { toast(e.message, true); }
    });
    const distance = (a, d) => {
      /* "3.2 pts away (0.15%)", from the latest close of this market's chart */
      if (!d || a.market !== market || !a.active) return "";
      const last = d.candles[d.candles.length - 1][4], diff = a.price - last, dg = priceDigits(a.price);
      const past = a.direction === "above" ? diff <= 0 : diff >= 0;
      return past ? ` · at or past the level now (${fmtPrice(last, dg)})` : ` · ${fmtPrice(Math.abs(diff), dg)} away (${num((Math.abs(diff) / last) * 100, 2)}%)`;
    };
    const distEls = [];
    alertList.update = (d) => { for (const [el, a] of distEls) el.textContent = distance(a, d); };
    const rearm = (a) => PUT(`/price-alerts/${a.id}`, { direction: a.direction, price: a.price, note: a.note, active: true,
      repeat: a.repeat, cooldown_minutes: a.cooldown_minutes, cross: !!a.cross });
    const row = (a) => {
      const dist = h("span", {}, distance(a, lastData));
      distEls.push([dist, a]);
      const how = `${a.cross ? "Crosses" : "Goes"} ${a.direction} ${fmtPrice(a.price, priceDigits(a.price))}`;
      const times = a.repeat === "every" ? ` · every time (${a.cooldown_minutes} min cooldown)` + (a.fired_count ? `, fired ${a.fired_count}×` : "") : "";
      return h("li", { class: a.active ? "" : "done" },
      h("div", {}, h("strong", {}, how),
        a.market !== market ? h("span", { class: "muted" }, ` · ${a.label}`) : null,
        h("div", { class: "small muted" }, a.active ? `Waiting · set ${when(a.created_at)}` : `Hit ${when(a.triggered_at)}`, times,
          a.active && a.last_fired_at ? ` · last hit ${when(a.last_fired_at)}` : "", dist, a.note ? ` · ${a.note}` : "")),
      h("span", { class: "inline" },
        a.active ? h("button", { type: "button", class: "quiet", onclick: () => renderAlerts(a) }, "Edit") : null,
        a.active ? null : h("button", { type: "button", class: "quiet", onclick: async () => {
          try { await rearm(a); toast("Alert switched back on"); renderAlerts(); load(); } catch (e) { toast(e.message, true); }
        } }, "Re-arm"),
        h("button", { type: "button", class: "quiet icon", "aria-label": "Delete alert", title: "Delete", onclick: (ev) => {
          const li = ev.target.closest("li");
          undoable("Alert deleted", (on) => { li.hidden = on; }, async () => { await DEL(`/price-alerts/${a.id}`); load(); });
        } }, "✕")));
    };
    clear(alertsBox);
    append(alertsBox, [
      h("section", { class: "section" }, h("h2", {}, editing ? "Edit alert" : "Price alerts"),
        h("p", { class: "section-note" }, `Get a phone alert when ${marketByKey(market).label} reaches a price. Checked every ${res.poll_minutes} minutes by the live checker, on 5-minute candles, so a quick touch still counts.`),
        ((app.status && app.status.channels) || []).some((c) => c !== "console") ? null
          : h("p", { class: "small muted" }, "No alert channel is set up yet, so hits will only show in Activity. ",
            h("a", { href: "#/settings" }, "Set one up in Settings"), "."),
        h("div", { class: "alert-form" }, dirSeg, h("div", { class: "inline" }, priceIn, add, cancel), crossLbl,
          h("div", { class: "inline" }, repeatSel, cooldownWrap), note)),
      h("section", { class: "section" },
        mine.length ? h("ul", { class: "alert-list" }, mine.map(row)) : h("p", { class: "muted small" }, "No alerts for this market yet."),
        others.length ? [h("h3", { class: "picker-group" }, "Other markets"), h("ul", { class: "alert-list" }, others.map(row))] : null),
    ]);
  }

  renderTf(); renderOverlay();
  append(page, [head,
    h("div", { class: "editor live-layout" },
      h("div", { class: "panel" }, h("section", { class: "section" },
        h("div", { class: "live-controls" }, marketSel, tfSeg, overlaySel, makeLink),
        h("div", { class: "live-head" }, priceBig, status), qualityBox, chartBox)),
      h("div", { class: "side" }, h("div", { class: "panel" }, alertsBox)))]);
  clear($main).appendChild(page);
  every(30, () => { if (!document.hidden) load(); });
  const onVisible = () => { if (!document.hidden && document.body.contains(chartBox)) load(!chart); };
  document.addEventListener("visibilitychange", onVisible);
  app.cleanups.push(() => document.removeEventListener("visibilitychange", onVisible));
  load(true); // not awaited: the page is usable (alerts, controls) while the first download runs
  await renderAlerts().catch((e) => toast(e.message, true));
}

/** Green / amber / red: is the price feed healthy? Same capsule as a forward test's state. */
function feedBadge(d) {
  const ago = d.age_minutes === null || d.age_minutes === undefined ? "" : d.age_minutes < 60 ? `${d.age_minutes} min ago`
    : d.age_minutes < 2880 ? `${Math.round(d.age_minutes / 60)} h ago` : `${Math.round(d.age_minutes / 1440)} days ago`;
  let cls, label, detail;
  if (d.feed === "saved") {
    cls = "attention"; label = "Feed down";
    detail = "The price feed didn't answer, so these are the prices saved last time. Trying again shortly.";
  } else if (d.closed_until) {
    cls = "waiting"; label = `Market shut · opens ${reopenText(d.closed_until)}`;
    detail = "No new candles while the market is shut. Prices carry on by themselves when it opens.";
  } else if (d.feed === "delayed") {
    cls = "delayed"; label = `Delayed · last candle ${ago}`;
    detail = "The feed answered, but its newest finished candle is older than expected.";
  } else {
    cls = "watching"; label = `Live · updated ${new Date(d.updated).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
    detail = d.forming ? "The last candle is still forming." : "Prices are up to date.";
  }
  return h("span", { class: "health " + cls, title: detail }, h("span", { class: "health-dot", "aria-hidden": "true" }), label);
}

function marketsTabs(active) {
  return h("nav", { class: "segmented page-tabs", "aria-label": "Markets views" },
    h("a", { href: "#/markets", "aria-current": active === "live" ? "page" : null, "aria-pressed": String(active === "live") }, "Live chart"),
    h("a", { href: "#/markets/stats", "aria-current": active === "stats" ? "page" : null, "aria-pressed": String(active === "stats") }, "Stats"));
}

const hh = (n) => String(n).padStart(2, "0") + ":00";

/** Columns for the 24 hours of the day. values: 24 numbers or nulls. */
function hourBars(values, { fmt, label, tone = "", highlight = [] } = {}) {
  const valid = values.filter((v) => v !== null && v !== undefined);
  const top = Math.max(...valid, 0) || 1;
  const readout = h("span", { class: "readout small muted" });
  const cols = values.map((v, i) => {
    const bar = h("span", { class: "hb-bar " + tone });
    bar.style.setProperty("height", v === null || v === undefined ? "0" : Math.max(2, (v / top) * 100) + "%");
    const col = h("span", { class: "hb-col" + (highlight.includes(i) ? " hot" : ""), tabindex: "0",
      "aria-label": `${hh(i)}: ${v === null || v === undefined ? "no data" : fmt(v)}` }, bar);
    const show = () => { readout.textContent = `${hh(i)}–${hh((i + 1) % 24)} · ${v === null || v === undefined ? "no data" : fmt(v)}`; };
    col.addEventListener("pointerenter", show);
    col.addEventListener("focus", show);
    return col;
  });
  const wrap = h("div", { class: "hour-bars" },
    h("div", { class: "chart-title" }, h("h3", {}, label), readout),
    h("div", { class: "hb-plot", role: "img", "aria-label": label }, cols),
    h("div", { class: "hb-axis small muted" }, [0, 3, 6, 9, 12, 15, 18, 21].map((n) => h("span", {}, String(n).padStart(2, "0")))));
  wrap.addEventListener("pointerleave", () => { readout.textContent = ""; });
  return wrap;
}

function statHeatmap(grid, fmtPct) {
  const all = grid.cells.flat().filter((v) => v !== null);
  const lo = Math.min(...all), hi = Math.max(...all);
  const readout = h("p", { class: "small muted heat-readout" }, "Point at a square to see its number.");
  const table = h("div", { class: "heatmap", role: "grid", "aria-label": "Average hourly move by day and hour" });
  table.appendChild(h("span", { class: "hm-corner" }));
  for (let i = 0; i < 24; i++) table.appendChild(h("span", { class: "hm-hour small muted" }, i % 3 ? "" : String(i).padStart(2, "0")));
  grid.days.forEach((day, r) => {
    table.appendChild(h("span", { class: "hm-day small" }, day));
    grid.cells[r].forEach((v, i) => {
      const fillEl = h("span", { class: "hm-fill" });
      const t = v === null ? 0 : (hi > lo ? (v - lo) / (hi - lo) : 0.5);
      fillEl.style.setProperty("opacity", String(v === null ? 0 : 0.08 + 0.92 * t));
      const cell = h("span", { class: "hm-cell" + (v === null ? " empty" : ""), tabindex: v === null ? null : "0", role: "gridcell",
        "aria-label": `${day} ${hh(i)}: ${v === null ? "market closed" : fmtPct(v)}` }, fillEl);
      const show = () => {
        readout.textContent = v === null ? `${day} ${hh(i)}: usually closed, or too few candles`
          : `${day} ${hh(i)}–${hh((i + 1) % 24)}: the hourly candle averaged ${fmtPct(v)} from high to low`;
      };
      cell.addEventListener("pointerenter", show);
      cell.addEventListener("focus", show);
      table.appendChild(cell);
    });
  });
  const legend = h("div", { class: "hm-legend small muted" }, "Quieter", h("span", { class: "hm-scale" }), "Busier");
  return h("div", {}, h("div", { class: "table-wrap" }, table), h("div", { class: "hm-foot" }, readout, legend));
}

function rangeMeter(label, r, fmt, tip) {
  if (!r) return null;
  const share = r.current_share;
  const meter = h("div", { class: "range-meter" + (share !== null && share > 100 ? " over" : "") });
  const fillEl = h("span", { class: "rm-fill" });
  fillEl.style.setProperty("width", Math.min(share || 0, 150) / 1.5 + "%");
  const mark = h("span", { class: "rm-mark", title: "A typical " + label.toLowerCase() });
  mark.style.setProperty("left", 100 / 1.5 + "%");
  append(meter, [fillEl, mark]);
  return h("div", { class: "range-card" },
    h("div", { class: "lbl small muted" }, `Typical ${label.toLowerCase()} range`, infoTip(tip)),
    h("div", { class: "big" }, fmt(r.median), h("span", { class: "small muted range-pct" }, `${num(r.median_pct, 2)}% of price`)),
    h("div", { class: "small muted" }, `Half of all ${label.toLowerCase()}s fall between ${fmt(r.low)} and ${fmt(r.high)}. One in ten is bigger than ${fmt(r.big)}.`),
    meter,
    h("div", { class: "small" }, share === null ? "No candles yet this " + label.toLowerCase()
      : [h("strong", {}, r.current_is_now ? `${label === "Day" ? "Today" : "This week"} so far: ${fmt(r.current)}`
          : `${label === "Day" ? "Last trading day" : "Last week"} (${new Date(r.current_date).toLocaleDateString("en-GB", { weekday: label === "Day" ? "short" : undefined, day: "numeric", month: "short" })}): ${fmt(r.current)}`),
        ` · ${num(share, 0)}% of a typical ${label.toLowerCase()}, bigger than ${num(r.current_rank, 0)}% of past ${label.toLowerCase()}s`]));
}

async function viewMarketStats() {
  setNav("markets");
  let market = pref("ta-live-market", "us500");
  if (!marketByKey(market)) market = "us500";
  let years = Number(pref("ta-stats-years", "2")) || 2;
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "Europe/London";
  const page = h("div", { class: "stack" });
  const head = h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Markets"),
    h("p", { class: "muted dash-lede" }, "How each market has tended to move: which hours are busy, how far a normal day goes, and what happens around big news. This describes past prices only. It doesn't say what will happen next.")),
    marketsTabs("stats"));
  const marketSel = h("select", { "aria-label": "Market" },
    [...new Set(app.recipes.markets.map((m) => m.group))].map((g) => h("optgroup", { label: g },
      app.recipes.markets.filter((m) => m.group === g).map((m) => h("option", { value: m.key }, m.label)))));
  marketSel.value = market;
  const yearSeg = h("div", { class: "segmented", role: "group", "aria-label": "How much history" });
  const body = h("div", { class: "stack" });
  let seq = 0;
  function renderYears() {
    clear(yearSeg);
    for (const y of [1, 2, 5]) yearSeg.appendChild(h("button", { type: "button", "aria-pressed": String(years === y),
      onclick: () => { years = y; savePref("ta-stats-years", String(y)); renderYears(); load(); } }, `${y} year${y > 1 ? "s" : ""}`));
  }
  marketSel.addEventListener("change", () => { market = marketSel.value; savePref("ta-live-market", market); load(); });

  async function load() {
    const my = ++seq;
    const slow = h("p", { class: "small muted" });
    clear(body).appendChild(h("div", { class: "panel" }, h("div", { class: "loading" }, "Working out the stats…", slow)));
    const t = setTimeout(() => { if (my === seq) slow.textContent = `The first time, this downloads ${years} year${years > 1 ? "s" : ""} of hourly prices, which can take a minute or two. After that it's saved.`; }, 5000);
    try {
      const d = await GET(`/market-stats?market=${market}&years=${years}&tz=${encodeURIComponent(tz)}`, { timeout: 300 });
      if (my !== seq) return;
      draw(d);
    } catch (e) {
      if (my !== seq) return;
      clear(body).appendChild(h("div", { class: "panel" }, h("div", { class: "empty" }, h("h2", {}, "Couldn't work out the stats"),
        h("p", { class: "error-box" }, e.message), h("div", { class: "actions" }, h("button", { type: "button", onclick: load }, "Try again")))));
    } finally { clearTimeout(t); }
  }

  function draw(d) {
    const dg = priceDigits(d.last_price);
    const pts = (v) => v === null || v === undefined ? "–" : fmtPrice(v, Math.min(dg, 5));
    const pc = (v) => v === null || v === undefined ? "–" : num(v, v < 0.1 ? 3 : 2) + "%";
    const from = new Date(d.from).toLocaleDateString("en-GB", { month: "short", year: "numeric" });
    const tzNote = d.tz === "Europe/London" ? "UK time" : d.tz.replace(/_/g, " ") + " time";
    const g = d.grid;
    const sections = [];

    sections.push(h("p", { class: "small muted stats-meta" },
      `${d.label} · ${num(d.candles, 0)} hourly candles from ${from} · times in ${tzNote}`,
      d.stale ? h("span", { class: "loss" }, " · the feed didn't answer, so these use the prices already saved") : null));

    // ranges
    const r = d.ranges || {};
    sections.push(h("div", { class: "panel range-row" },
      rangeMeter("Day", r.day, pts, "From the day's highest price to its lowest, in price points. Useful for judging whether a stop or target is a normal day's move or an unusual one."),
      rangeMeter("Week", r.week, pts, "From the week's highest price to its lowest.")));

    // heatmap
    const busy = g.busiest !== null ? `${hh(g.busiest)}–${hh((g.busiest + 1) % 24)}` : "–";
    const quiet = g.quietest !== null ? `${hh(g.quietest)}–${hh((g.quietest + 1) % 24)}` : "–";
    sections.push(h("section", { class: "panel section" },
      h("h2", {}, "When it moves", infoTip("Each square is the average size of one hour's candle, from its high to its low, as a share of the price. Darker means that hour usually moves more.")),
      h("p", { class: "section-note" }, `Busiest hour on average: `, h("strong", {}, busy), `. Quietest: `, h("strong", {}, quiet), "."),
      statHeatmap(g, pc),
      hourBars(g.by_hour, { label: "Average hourly move, all days", fmt: pc, highlight: [g.busiest] })));

    // day high / low
    const hl = d.high_low;
    const both = hl.high.map((v, i) => (v || 0) + (hl.low[i] || 0));
    const top3 = both.map((v, i) => [v, i]).sort((a, b) => b[0] - a[0]).slice(0, 3).map((x) => x[1]).sort((a, b) => a - b);
    sections.push(h("section", { class: "panel section" },
      h("h2", {}, "When the day's high and low are made", infoTip("For each of the last " + hl.days + " days, the hour whose candle reached the day's highest price, and the one that reached its lowest. A day here runs midnight to midnight in " + tzNote + ".")),
      h("p", { class: "section-note" }, "Over ", h("strong", {}, String(hl.days)), " days, the high or low was most often set around ",
        h("strong", {}, top3.map(hh).join(", ")), "."),
      h("div", { class: "two-col" },
        hourBars(hl.high, { label: "Day's high made in this hour", fmt: (v) => num(v, 1) + "% of days", tone: "gain" }),
        hourBars(hl.low, { label: "Day's low made in this hour", fmt: (v) => num(v, 1) + "% of days", tone: "loss" }))));

    // sessions
    if (d.sessions && d.sessions.length) {
      sections.push(h("section", { class: "panel section" },
        h("h2", {}, "Sessions", infoTip("The trading day split into the three big sessions (UK time). 'Kept going' is how often the session ended further in the direction of its first hour. A coin flip would be about 50%.")),
        h("div", { class: "table-wrap" }, h("table", { class: "trades" },
          h("thead", {}, h("tr", {}, ["Session", "UK time", "Typical range", "Share of the day's range", "First hour kept going"].map((t) => h("th", {}, t)))),
          h("tbody", {}, d.sessions.map((x) => h("tr", {},
            h("td", {}, h("strong", {}, x.label), h("div", { class: "small muted" }, x.about)),
            h("td", {}, x.hours), h("td", {}, pts(x.median_range)),
            h("td", {}, x.share_of_day === null ? "–" : num(x.share_of_day, 0) + "%"),
            h("td", {}, x.carried_on === null ? "–" : `${num(x.carried_on, 0)}% of ${x.days} days`))))))));
    }

    // spread
    if (d.spread) {
      const sp = d.spread;
      sections.push(h("section", { class: "panel section" },
        h("h2", {}, "Spread by hour", infoTip("The gap between the buy and sell price on Dukascopy's feed, middle value for each hour. Your broker's spread will differ, but it usually widens at the same times.")),
        h("p", { class: "section-note" }, "Usually ", h("strong", {}, pts(sp.overall)), ". Widest around ", h("strong", {}, hh(sp.widest)),
          ", tightest around ", h("strong", {}, hh(sp.tightest)), "."),
        hourBars(sp.median, { label: "Middle spread", fmt: pts, tone: "warn", highlight: [sp.widest] })));
    }

    // gaps
    const gp = d.gaps || { kinds: [] };
    sections.push(h("section", { class: "panel section" },
      h("h2", {}, "Gaps", infoTip("A gap is when a candle opens at a different price from where the last one closed, because trading stopped in between (overnight or the weekend). 'Filled' means the price came back to the old close within the next 24 hours.")),
      gp.kinds.length ? h("div", { class: "gap-grid" }, gp.kinds.map((k) => h("div", { class: "gap-card" },
        h("h3", {}, k.label),
        h("dl", { class: "facts" },
          h("dt", {}, "How many"), h("dd", {}, String(k.count)),
          h("dt", {}, "Usual size"), h("dd", {}, pts(k.median)),
          h("dt", {}, "Biggest"), h("dd", {}, `${pts(k.biggest)} (${new Date(k.biggest_when).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })})`),
          gp.threshold !== null ? [h("dt", {}, `Bigger than ${pts(gp.threshold)}`), h("dd", {}, num(k.noticeable, 0) + "%")] : null,
          h("dt", {}, "Opened higher"), h("dd", {}, num(k.up, 0) + "%"),
          h("dt", {}, "Filled within a day"), h("dd", {}, num(k.filled, 0) + "%")))))
        : h("p", { class: "muted small" }, "This market trades almost around the clock, so there were too few breaks to measure.")));

    // news
    sections.push(h("section", { class: "panel section" },
      h("h2", {}, "Around big news", infoTip("For each kind of high-impact news this market follows, the size of the hour's candle the news landed in, compared with the same hour on other days. 2× means it was twice the usual size.")),
      d.news.length ? h("div", { class: "table-wrap" }, h("table", { class: "trades" },
        h("thead", {}, h("tr", {}, ["News", "Times", "Hour's move vs usual", "Usual move that hour", "Biggest", "Next 4 hours"].map((t) => h("th", {}, t)))),
        h("tbody", {}, d.news.map((n) => h("tr", {},
          h("td", {}, h("strong", {}, n.title), h("span", { class: "muted small" }, " " + n.currency)),
          h("td", {}, String(n.count)),
          h("td", {}, h("span", { class: n.times_normal >= 1.5 ? "pill warn" : "pill" }, num(n.times_normal, 1) + "×")),
          h("td", {}, pts(n.median_range)), h("td", {}, pts(n.biggest)), h("td", {}, pts(n.four_hours)))))))
        : h("p", { class: "muted small" }, "Not enough past news saved for this market yet. The app keeps the calendar each week, so this fills in over time.")));

    clear(body);
    append(body, sections);
  }

  renderYears();
  append(page, [head, h("div", { class: "panel live-controls stats-controls" }, marketSel, yearSeg), body]);
  clear($main).appendChild(page);
  load();
}

/** One line for price gaps: the "gap" note plus the weekday total from "missing_bars", instead of two notes on the same gaps. */
function mergeGapNotes(list) {
  const gap = list.find((w) => w.kind === "gap"), miss = list.find((w) => w.kind === "missing_bars");
  if (!gap || !miss) return list;
  const text = gap.text.replace(" UTC). ", ` UTC), about ${miss.count} weekday candles in all. `);
  return list.filter((w) => w !== miss).map((w) => (w === gap ? { ...w, text } : w));
}
