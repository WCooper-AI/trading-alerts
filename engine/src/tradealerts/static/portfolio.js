"use strict";

/* Trading Alerts web interface: Testing several strategies together on one balance.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- portfolio: strategies together

async function viewPortfolio() {
  setNav("strategies");
  const list = await GET("/strategies");
  const ok = list.strategies.filter((x) => !x.error);
  const chosen = new Set(pref("ta-portfolio", "").split(",").filter((id) => ok.some((x) => x.id === id)));
  let balance = null;
  const opts = { weighting: "equal", weights: {}, shared: false, max_open_trades: null, max_open_risk_pct: null };
  const out = h("div");
  const period = periodPicker({ custom: true });
  const run = h("button", { class: "primary", type: "button" }, "Test together");
  const shareInputs = new Map();
  const showShares = () => shareInputs.forEach((el, id) => { el.hidden = opts.weighting !== "custom" || !chosen.has(id); });
  const picks = h("div", { class: "pick-list" }, ok.map((x) => {
    const box = h("input", { type: "checkbox", checked: chosen.has(x.id), onchange: (e) => {
      if (e.target.checked) chosen.add(x.id); else chosen.delete(x.id);
      savePref("ta-portfolio", [...chosen].join(","));
      showShares();
    } });
    const share = h("span", { class: "share-input", hidden: true },
      numberInput(null, (n) => { opts.weights[x.id] = n; }, { label: `Share of balance for ${x.data.name || x.id} (%)`, step: "any", min: "0", placeholder: "Share %" }), "%");
    shareInputs.set(x.id, share);
    return h("label", { class: "pick" }, box, h("span", {}, h("strong", {}, x.data.name || x.id),
      h("span", { class: "small muted" }, ` ${(x.data.market || {}).label || ""} · ${(x.data.market || {}).timeframe || ""}`)), share);
  }));
  const bal = numberInput(null, (n) => { balance = n; }, { label: "Shared starting balance", step: "any", min: "1", placeholder: "Amount" });
  const weighting = h("select", { "aria-label": "How the balance is split", onchange: (e) => { opts.weighting = e.target.value; showShares(); } },
    h("option", { value: "equal" }, "Equal: every strategy gets the same share"),
    h("option", { value: "inverse_vol" }, "Steadier strategies get more"),
    h("option", { value: "custom" }, "Set each share myself"));
  const caps = h("div", { class: "bt-controls caps", hidden: true },
    h("div", { class: "field" }, h("label", {}, "Most trades open at once", infoTip("A new trade is skipped while this many are already open across all the strategies. Leave blank for no limit.")),
      numberInput(null, (n) => { opts.max_open_trades = n; }, { label: "Most trades open at once", step: "1", min: "1", integer: true, placeholder: "No limit", allowEmpty: true })),
    h("div", { class: "field" }, h("label", {}, "Most at risk at once (% of balance)", infoTip("A new trade is skipped if it would take the total distance to the stops past this % of the balance at the time. Leave blank for no limit.")),
      numberInput(null, (n) => { opts.max_open_risk_pct = n; }, { label: "Most at risk at once", step: "any", min: "0", placeholder: "No limit", allowEmpty: true })));
  const sharedBox = h("input", { type: "checkbox", onchange: (e) => { opts.shared = e.target.checked; caps.hidden = !opts.shared; } });
  showShares();
  run.addEventListener("click", async () => {
    if (chosen.size < 1) { toast("Tick at least one strategy", true); return; }
    if (isBlank(balance)) { toast("Fill in the starting balance", true); return; }
    const body = { ids: [...chosen], balance, weighting: opts.weighting, shared: opts.shared, ...period.value() };
    if (opts.weighting === "custom") {
      body.weights = {};
      for (const id of chosen) {
        if (isBlank(opts.weights[id])) { toast("Fill in a share for every ticked strategy", true); return; }
        body.weights[id] = opts.weights[id];
      }
    }
    if (opts.shared) {
      if (!isBlank(opts.max_open_trades)) body.max_open_trades = opts.max_open_trades;
      if (!isBlank(opts.max_open_risk_pct)) body.max_open_risk_pct = opts.max_open_risk_pct;
    }
    run.disabled = true;
    clear(out).appendChild(h("p", { class: "loading" }, `Backtesting ${chosen.size} strategies…`));
    try { clear(out).appendChild(renderPortfolio(await POST("/portfolio", body), ok)); }
    catch (e) { clear(out).appendChild(h("p", { class: "error-box" }, e.message)); }
    finally { run.disabled = false; }
  });
  const page = h("div", { class: "stack" },
    h("div", {}, h("a", { class: "back", href: "#/strategies" }, "All strategies")),
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Test strategies together"),
      h("p", { class: "muted dash-lede" }, "Runs several strategies on one shared balance, to see the combined result, how much was at risk at once, and whether they tend to win and lose on the same days."))),
    h("div", { class: "editor portfolio-layout" },
      h("div", { class: "panel" },
        h("section", { class: "section" }, h("h2", {}, "Strategies"), ok.length ? picks : h("p", { class: "muted" }, "Create a strategy first.")),
        h("section", { class: "section" }, h("div", { class: "bt-controls" },
          h("div", { class: "field" }, h("label", {}, "Shared starting balance",
            infoTip("Every strategy risks its own % per trade of this balance. Trades are sized from the starting balance (not compounded) so the strategies' results can simply be added together.")), bal),
          h("div", { class: "field" }, h("label", {}, "How the balance is split",
            infoTip("Equal: each strategy trades at the size it was backtested at, as if it had 1/n of the balance each. Steadier strategies get more: shares are set so each strategy's typical daily swing is about the same size, with the same total. Set each share myself: type a % next to each ticked strategy; a strategy on twice the equal share trades twice the size.")), weighting),
          period)),
        h("section", { class: "section" },
          h("label", { class: "pick" }, sharedBox, h("span", {}, h("strong", {}, "Replay through one shared account"),
            h("span", { class: "small muted" }, " Trades are taken in the order they happened and sized from the balance as it stood at the time, so the result compounds and the caps below can skip trades."))),
          caps),
        h("section", { class: "section" }, run)),
      h("div", { class: "panel" }, h("section", { class: "section" }, out))));
  clear($main).appendChild(page);
}

function renderPortfolio(res, strategies) {
  const m = res.metrics, sym = res.currency;
  const name = (id) => ((strategies.find((x) => x.id === id) || {}).data || {}).name || id;
  const box = h("div", { class: "portfolio" });
  append(box, [
    h("div", { class: "headline" },
      h("div", {}, h("div", { class: "big " + (m.total_return_pct >= 0 ? "gain" : "loss") }, signed(m.total_return_pct, "%")), h("div", { class: "lbl" }, "Return together")),
      h("div", {}, h("div", { class: "big" }, num(m.max_drawdown_pct, 1) + "%"), h("div", { class: "lbl" }, "Worst drawdown")),
      h("div", {}, h("div", { class: "big" }, num(m.max_open_risk_pct, 1) + "%"), h("div", { class: "lbl" }, "Most at risk at once")),
      h("div", {}, h("div", { class: "big" }, String(m.trades)), h("div", { class: "lbl" }, res.shared ? "Trades taken" : "Trades")),
      res.shared ? h("div", {}, h("div", { class: "big" }, String(m.trades_skipped)), h("div", { class: "lbl" }, "Trades skipped")) : null,
      h("div", {}, h("div", { class: "big" }, num(m.time_in_market_pct, 0) + "%"), h("div", { class: "lbl" }, "Time in a trade")),
      h("div", {}, h("div", { class: "big" }, String(m.max_open_trades)), h("div", { class: "lbl" }, "Most trades open at once"))),
    h("p", { class: "muted" }, `${day(m.start)} to ${day(m.end)}: ${money(m.start_equity, sym)} would have become ${money(m.end_equity, sym)}. ` +
      `Some money was at risk ${num(m.time_with_risk_pct, 0)}% of the time; when it was, ${num(m.avg_open_risk_pct, 1)}% of the balance on average.`),
    res.shared ? skippedNote(res.skipped) : null,
    lineChart(res.equity, { title: "Shared balance", fmt: (v) => money(v, sym), baseline: m.start_equity }),
    lineChart(res.drawdown, { title: "Drawdown", fmt: (v) => num(v, 1) + "%", baseline: 0, tone: "loss", fill: true, height: 120 }),
    lineChart(res.open_risk, { title: "At risk at once (% of balance, to the stops)", fmt: (v) => num(v, 2) + "%", baseline: 0, fill: true, height: 120 }),
    pnlOverlayChart(res.pnl_curves || {}, res.per_strategy, sym),
    h("div", { class: "table-wrap" }, h("table", { class: "trades" },
      h("thead", {}, h("tr", {}, h("th", {}, "Strategy"), h("th", { class: "num" }, "Share"),
        h("th", { class: "num" }, res.shared ? "Taken / skipped" : "Trades"), h("th", { class: "num" }, "Profit"),
        h("th", { class: "num" }, "Return on balance"), h("th", { class: "num" }, "Share of profit"),
        h("th", { class: "num" }, "Time in a trade"), h("th", { class: "num" }, "Own worst drop"))),
      h("tbody", {}, res.per_strategy.map((p, i) => h("tr", {}, h("td", {}, h("span", { class: "swatch", style: `--swatch:${seriesColour(i)}` }), p.name),
        h("td", { class: "num" }, p.weight_pct === undefined ? "–" : num(p.weight_pct, 0) + "%"),
        h("td", { class: "num" }, res.shared ? `${p.taken} / ${p.skipped}` : String(p.trades)),
        h("td", { class: "num " + (p.pnl >= 0 ? "gain" : "loss") }, signedMoney(p.pnl, sym)),
        h("td", { class: "num" }, signed(p.return_pct, "%")),
        h("td", { class: "num" }, p.contribution_pct === undefined ? "–" : signed(p.contribution_pct, "%")),
        h("td", { class: "num" }, p.time_in_market_pct === undefined ? "–" : num(p.time_in_market_pct, 0) + "%"),
        h("td", { class: "num" }, num(p.max_drawdown_pct, 1) + "%")))))),
  ]);
  if (res.exposure) box.appendChild(exposureSection(res.exposure, m, sym));
  if (res.correlation) {
    const c = res.correlation;
    box.appendChild(h("div", { class: "corr" }, h("h3", {}, "Do they move together?"),
      h("p", { class: "small muted" }, "Correlation of each pair's daily results: near 1 means they win and lose on the same days (less spread of risk), near 0 means unrelated, below 0 means they tend to offset each other."),
      h("div", { class: "table-wrap" }, h("table", { class: "trades corr-table" },
        h("thead", {}, h("tr", {}, h("th", {}), c.ids.map((id) => h("th", { class: "num", title: name(id) }, name(id).slice(0, 18))))),
        h("tbody", {}, c.ids.map((id, i) => h("tr", {}, h("th", {}, name(id)), c.values[i].map((v, j) => {
          const td = h("td", { class: "num" }, v === null ? "–" : num(v, 2));
          if (v !== null && i !== j) td.style.setProperty("--corr", String(Math.abs(v)));
          if (v !== null && i !== j) td.classList.add(v >= 0 ? "corr-pos" : "corr-neg");
          return td;
        }))))))));
  }
  box.appendChild(h("p", { class: "notice" }, res.shared
    ? "Each trade was re-sized from the balance at the time it opened, so this compounds, and the result of every trade scales with its new size. Per-fill commissions scale too, which is an approximation. Open risk counts each trade's distance to its stop; gaps past a stop can lose more."
    : "Each strategy sizes its trades from the shared starting balance, so this doesn't compound. Open risk counts each trade's distance to its stop; gaps past a stop can lose more."));
  if (res.notes.length) box.appendChild(h("ul", { class: "warnings" }, res.notes.map((w) => h("li", {}, w))));
  return box;
}


function skippedNote(sk) {
  if (!sk) return null;
  const total = sk.open_trades + sk.open_risk + sk.too_small;
  if (!total) return h("p", { class: "small muted" }, "No trades were skipped.");
  const parts = [];
  if (sk.open_trades) parts.push(`${sk.open_trades} because the most-open-at-once cap was reached`);
  if (sk.open_risk) parts.push(`${sk.open_risk} because the at-risk cap would have been passed`);
  if (sk.too_small) parts.push(`${sk.too_small} because the balance was too small to size them`);
  return h("p", { class: "small muted" }, `${total} trades skipped: ${parts.join("; ")}.`);
}


// --------------------------------------------------------------------- portfolio: exposure and overlay

const SERIES_COLOURS = ["#2563eb", "#d97706", "#059669", "#dc2626", "#7c3aed", "#0891b2", "#be185d", "#4d7c0f", "#9333ea", "#ea580c", "#0f766e", "#b91c1c"];
function seriesColour(i) { return SERIES_COLOURS[i % SERIES_COLOURS.length]; }

/* Each strategy's running profit on one chart, so you can see who carried the account and when. */
function pnlOverlayChart(curves, per, sym) {
  const W = 560, H = 170, P = 6;
  const wrap = h("div", { class: "chart" });
  const readout = h("span", { class: "readout" });
  append(wrap, [h("div", { class: "chart-title" }, h("h3", {}, "Profit by strategy"), h("span", { class: "chart-tools" }, readout, expandable(wrap)))]);
  const ids = per.map((p) => p.id).filter((id) => (curves[id] || []).length > 1);
  if (!ids.length) { wrap.appendChild(h("p", { class: "muted small" }, "Not enough data to chart.")); return wrap; }
  const all = ids.flatMap((id) => curves[id]);
  const ts = all.map((p) => new Date(p[0]).getTime());
  const t0 = Math.min(...ts), t1 = Math.max(Math.max(...ts), t0 + 1);
  let lo = Math.min(0, ...all.map((p) => p[1])), hi = Math.max(0, ...all.map((p) => p[1]));
  if (hi === lo) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.06; hi += pad; lo -= pad;
  const x = (t) => P + ((t - t0) * (W - 2 * P)) / (t1 - t0);
  const y = (v) => P + ((hi - v) * (H - 2 * P)) / (hi - lo);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Running profit of ${ids.length} strategies` });
  s.appendChild(svg("rect", { x: 0, y: 0, width: W, height: H, rx: 14, class: "chart-bg" }));
  gridlines(s, lo, hi, y, (v) => money(v, sym), W, P);
  s.appendChild(svg("line", { x1: P, x2: W - P, y1: y(0), y2: y(0), class: "chart-base", "stroke-dasharray": "4 4" }));
  ids.forEach((id) => {
    const i = per.findIndex((p) => p.id === id);
    const d = curves[id].map((p, k) => `${k ? "L" : "M"}${x(new Date(p[0]).getTime()).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    const path = svg("path", { d, class: "chart-line", "stroke-width": 1.8, "stroke-linejoin": "round", fill: "none" });
    path.style.stroke = seriesColour(i);
    const t = svg("title", {}); t.textContent = `${per[i].name}: ${signedMoney(per[i].pnl, sym)} at the end`;
    path.appendChild(t);
    s.appendChild(path);
  });
  readout.textContent = `${day(new Date(t0).toISOString())} to ${day(new Date(t1).toISOString())}`;
  wrap.appendChild(s);
  wrap.appendChild(h("div", { class: "legend small" }, per.map((p, i) => h("span", { class: "legend-item" },
    h("span", { class: "swatch", style: `--swatch:${seriesColour(i)}` }), p.name))));
  return wrap;
}

/* Where the risk sat: by market, how often trades overlapped, and whether the mix spread the risk. */
function exposureSection(ex, m, sym) {
  const sec = h("div", { class: "corr exposure" }, h("h3", {}, "Where the risk sat"));
  const bits = [];
  if (m.avg_correlation !== null && m.avg_correlation !== undefined) {
    const c = m.avg_correlation;
    const verdict = c > 0.5 ? "they mostly move together, so the mix spreads risk less than it looks" :
      c > 0.2 ? "they move together some of the time" : c > -0.2 ? "they are largely unrelated, which spreads the risk" :
      "they tend to offset each other";
    bits.push(`On average the pairs' daily results have a correlation of ${num(c, 2)}: ${verdict}.`);
  }
  if (m.diversification_ratio) {
    bits.push(`Diversification ratio ${num(m.diversification_ratio, 2)}: the strategies' day-to-day swings add up to ` +
      `${num(m.diversification_ratio, 1)}× the combined account's swing (1 means no benefit from mixing them; higher is better).`);
  }
  const overlap = Object.entries(ex.overlap_pct || {}).map(([k, v]) => [Number(k), v]).sort((a, b) => a[0] - b[0]);
  if (overlap.length) {
    bits.push("Trades open at once: " + overlap.map(([k, v]) => `${k === 0 ? "none" : k} ${num(v, 0)}% of the time`).join(", ") + ".");
  }
  if (bits.length) sec.appendChild(h("p", { class: "small muted" }, bits.join(" ")));
  if (ex.by_market && ex.by_market.length) {
    sec.appendChild(h("div", { class: "table-wrap" }, h("table", { class: "trades" },
      h("thead", {}, h("tr", {}, h("th", {}, "Market"), h("th", { class: "num" }, "Strategies"), h("th", { class: "num" }, "Trades"),
        h("th", { class: "num" }, "Profit"), h("th", { class: "num" }, "Share of profit"), h("th", { class: "num" }, "Time in a trade"))),
      h("tbody", {}, ex.by_market.map((b) => h("tr", {}, h("td", {}, b.market), h("td", { class: "num" }, String(b.strategies)),
        h("td", { class: "num" }, String(b.trades)), h("td", { class: "num " + (b.pnl >= 0 ? "gain" : "loss") }, signedMoney(b.pnl, sym)),
        h("td", { class: "num" }, signed(b.share_pct, "%")), h("td", { class: "num" }, num(b.time_in_market_pct, 0) + "%")))))));
    if (ex.by_market.length === 1 && m.trades > 0) {
      sec.appendChild(h("p", { class: "small muted" }, "Everything trades the same market, so one bad day there hits every strategy at once."));
    }
  }
  return sec;
}
