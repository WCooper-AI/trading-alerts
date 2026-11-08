"use strict";

/* Trading Alerts web interface: Comparing backtests side by side.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- compare backtests side by side

const SERIES_TONES = ["s1", "s2", "s3", "s4"];
function multiChart(series, { title, fmt }) {
  const W = 560, H = 230, P = 6;
  const wrap = h("div", { class: "chart" });
  const readout = h("span", { class: "readout" });
  append(wrap, [h("div", { class: "chart-title" }, h("h3", {}, title), h("span", { class: "chart-tools" }, readout, expandable(wrap)))]);
  const all = series.flatMap((x) => x.points);
  const ts = all.map((p) => new Date(p[0]).getTime());
  const t0 = Math.min(...ts), t1 = Math.max(...ts);
  let lo = Math.min(0, ...all.map((p) => p[1])), hi = Math.max(0, ...all.map((p) => p[1]));
  const pad = (hi - lo || 1) * 0.06; lo -= pad; hi += pad;
  const x = (t) => P + ((t - t0) * (W - 2 * P)) / (t1 - t0 || 1);
  const y = (v) => P + ((hi - v) * (H - 2 * P)) / (hi - lo);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title });
  s.appendChild(svg("rect", { x: 0, y: 0, width: W, height: H, rx: 14, class: "chart-bg" }));
  s.appendChild(svg("line", { x1: P, x2: W - P, y1: y(0), y2: y(0), class: "chart-base", "stroke-dasharray": "4 4" }));
  series.forEach((ser, i) => {
    const d = ser.points.map((p, k) => `${k ? "L" : "M"}${x(new Date(p[0]).getTime()).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    s.appendChild(svg("path", { d, class: `chart-line ${SERIES_TONES[i]}`, pathLength: 1, "stroke-width": 2, "stroke-linejoin": "round" }));
  });
  s.addEventListener("pointermove", (e) => {
    const r = s.getBoundingClientRect();
    const t = t0 + (((e.clientX - r.left) / r.width) * W - P) / (W - 2 * P) * (t1 - t0);
    readout.textContent = series.map((ser) => {
      let best = ser.points[0];
      for (const p of ser.points) if (new Date(p[0]).getTime() <= t) best = p;
      return `${ser.name.slice(0, 18)} ${fmt(best[1])}`;
    }).join(" · ");
  });
  s.addEventListener("pointerleave", () => { readout.textContent = ""; });
  wrap.appendChild(s);
  wrap.appendChild(h("div", { class: "legend small" }, series.map((ser, i) => h("span", {}, h("span", { class: `swatch line ${SERIES_TONES[i]}` }), ser.name))));
  return wrap;
}

async function viewCompare() {
  setNav("strategies");
  const list = await GET("/strategies");
  const ok = list.strategies.filter((x) => !x.error);
  const chosen = new Set(pref("ta-compare", "").split(",").filter((id) => ok.some((x) => x.id === id)));
  const out = h("div");
  const period = periodPicker({ custom: true });
  const run = h("button", { class: "primary", type: "button" }, "Compare");
  const picks = h("div", { class: "pick-list" }, ok.map((x) => h("label", { class: "pick" },
    h("input", { type: "checkbox", checked: chosen.has(x.id), onchange: (e) => {
      if (e.target.checked) {
        if (chosen.size >= 4) { e.target.checked = false; toast("Compare up to 4 at a time", true); return; }
        chosen.add(x.id);
      } else chosen.delete(x.id);
      savePref("ta-compare", [...chosen].join(","));
    } }), h("span", {}, h("strong", {}, x.data.name || x.id), h("span", { class: "small muted" }, ` ${(x.data.market || {}).label || ""} · ${(x.data.market || {}).timeframe || ""}`)))));
  run.addEventListener("click", async () => {
    if (chosen.size < 2) { toast("Tick 2 to 4 strategies", true); return; }
    run.disabled = true;
    const ids = [...chosen];
    const results = [];
    try {
      for (const [i, id] of ids.entries()) {
        clear(out).appendChild(h("p", { class: "loading" }, `Backtesting ${i + 1} of ${ids.length}…`));
        const rec = await GET("/strategies/" + encodeURIComponent(id));
        results.push({ id, name: rec.data.name || id, res: await POST("/backtest", { data: rec.data, ...period.value() }) });
      }
      savePref("ta-did-backtest", "1");
      clear(out).appendChild(renderCompare(results));
    } catch (e) { clear(out).appendChild(h("p", { class: "error-box" }, e.message)); }
    finally { run.disabled = false; }
  });
  clear($main).appendChild(h("div", { class: "stack" },
    h("div", {}, h("a", { class: "back", href: "#/strategies" }, "All strategies")),
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Compare backtests"),
      h("p", { class: "muted dash-lede" }, "Backtest up to four strategies over the same period and see them side by side. Each uses its own balance and risk settings, so returns are shown in %."))),
    h("div", { class: "editor portfolio-layout" },
      h("div", { class: "panel" }, h("section", { class: "section" }, h("h2", {}, "Strategies"), picks),
        h("section", { class: "section" }, h("div", { class: "bt-controls" }, period, run))),
      h("div", { class: "panel" }, h("section", { class: "section" }, out)))));
}

function renderCompare(results) {
  const series = results.map((r) => {
    const start = r.res.metrics.start_equity;
    return { name: r.name, points: r.res.equity.map((p) => [p[0], (p[1] / start - 1) * 100]) };
  });
  const rows = [
    ["Return", (m) => signed(m.total_return_pct, "%"), (m) => m.total_return_pct],
    ["Worst drawdown", (m) => num(m.max_drawdown_pct, 1) + "%", (m) => m.max_drawdown_pct],
    ["Trades", (m) => String(m.trades), null],
    ["Won", (m) => (m.trades ? num(m.win_rate_pct, 0) + "%" : "–"), (m) => m.win_rate_pct],
    ["Profit factor", (m) => (m.profit_factor === null || m.profit_factor === undefined ? "–" : num(m.profit_factor, 2)), (m) => m.profit_factor],
    ["Average trade", (m) => (m.avg_r === null || m.avg_r === undefined ? "–" : signed(m.avg_r, "R")), (m) => m.avg_r],
  ];
  return h("div", { class: "compare-out" },
    multiChart(series, { title: "Return over time (%)", fmt: (v) => signed(v, "%") }),
    h("div", { class: "table-wrap" }, h("table", { class: "trades compare-table" },
      h("thead", {}, h("tr", {}, h("th", {}), results.map((r, i) => h("th", { class: "num" }, h("span", { class: `swatch line ${SERIES_TONES[i]}` }), " ", r.name)))),
      h("tbody", {}, rows.map(([label, fmt, val]) => {
        const vals = val ? results.map((r) => val(r.res.metrics)) : [];
        const best = vals.length ? Math.max(...vals.filter((v) => v !== null && v !== undefined)) : null;
        return h("tr", {}, h("th", {}, label), results.map((r, i) => h("td", { class: "num" + (val && vals[i] === best && results.length > 1 ? " best" : "") }, fmt(r.res.metrics))));
      })))),
    h("p", { class: "small muted" }, "The best value in each row is highlighted. A higher number isn't always better on its own: weigh return against the worst drop."));
}
