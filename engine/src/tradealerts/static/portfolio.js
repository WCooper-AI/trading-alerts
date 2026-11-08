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
  const out = h("div");
  const period = periodPicker({ custom: true });
  const run = h("button", { class: "primary", type: "button" }, "Test together");
  const picks = h("div", { class: "pick-list" }, ok.map((x) => {
    const box = h("input", { type: "checkbox", checked: chosen.has(x.id), onchange: (e) => {
      if (e.target.checked) chosen.add(x.id); else chosen.delete(x.id);
      savePref("ta-portfolio", [...chosen].join(","));
    } });
    return h("label", { class: "pick" }, box, h("span", {}, h("strong", {}, x.data.name || x.id),
      h("span", { class: "small muted" }, ` ${(x.data.market || {}).label || ""} · ${(x.data.market || {}).timeframe || ""}`)));
  }));
  const bal = numberInput(null, (n) => { balance = n; }, { label: "Shared starting balance", step: "any", min: "1", placeholder: "Amount" });
  run.addEventListener("click", async () => {
    if (chosen.size < 1) { toast("Tick at least one strategy", true); return; }
    if (isBlank(balance)) { toast("Fill in the starting balance", true); return; }
    run.disabled = true;
    clear(out).appendChild(h("p", { class: "loading" }, `Backtesting ${chosen.size} strategies…`));
    try { clear(out).appendChild(renderPortfolio(await POST("/portfolio", { ids: [...chosen], balance, ...period.value() }), ok)); }
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
          period, run))),
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
      h("div", {}, h("div", { class: "big" }, String(m.trades)), h("div", { class: "lbl" }, "Trades"))),
    h("p", { class: "muted" }, `${day(m.start)} to ${day(m.end)}: ${money(m.start_equity, sym)} would have become ${money(m.end_equity, sym)}. ` +
      `Some money was at risk ${num(m.time_with_risk_pct, 0)}% of the time; when it was, ${num(m.avg_open_risk_pct, 1)}% of the balance on average.`),
    lineChart(res.equity, { title: "Shared balance", fmt: (v) => money(v, sym), baseline: m.start_equity }),
    lineChart(res.drawdown, { title: "Drawdown", fmt: (v) => num(v, 1) + "%", baseline: 0, tone: "loss", fill: true, height: 120 }),
    lineChart(res.open_risk, { title: "At risk at once (% of balance, to the stops)", fmt: (v) => num(v, 2) + "%", baseline: 0, fill: true, height: 120 }),
    h("div", { class: "table-wrap" }, h("table", { class: "trades" },
      h("thead", {}, h("tr", {}, h("th", {}, "Strategy"), h("th", { class: "num" }, "Trades"), h("th", { class: "num" }, "Profit"),
        h("th", { class: "num" }, "Share of balance"), h("th", { class: "num" }, "Own worst drop"))),
      h("tbody", {}, res.per_strategy.map((p) => h("tr", {}, h("td", {}, p.name), h("td", { class: "num" }, String(p.trades)),
        h("td", { class: "num " + (p.pnl >= 0 ? "gain" : "loss") }, signedMoney(p.pnl, sym)),
        h("td", { class: "num" }, signed(p.return_pct, "%")), h("td", { class: "num" }, num(p.max_drawdown_pct, 1) + "%")))))),
  ]);
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
  box.appendChild(h("p", { class: "notice" }, "Each strategy sizes its trades from the shared starting balance, so this doesn't compound. Open risk counts each trade's distance to its stop; gaps past a stop can lose more."));
  if (res.notes.length) box.appendChild(h("ul", { class: "warnings" }, res.notes.map((w) => h("li", {}, w))));
  return box;
}
