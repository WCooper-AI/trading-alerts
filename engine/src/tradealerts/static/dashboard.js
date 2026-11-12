"use strict";

/* Trading Alerts web interface: The dashboard and each forward test's page.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- dashboard (forward tests)

const EVERY_TEXT = { "5m": "every 5 minutes", "15m": "every 15 minutes", "30m": "every 30 minutes", "1h": "every hour",
  "2h": "every 2 hours", "4h": "every 4 hours", "1d": "once a day" };

function ago(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  const mins = Math.round(ms / 60000);
  if (mins < 60) return mins <= 1 ? "just now" : `${mins} minutes ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return `${hrs} ${hrs === 1 ? "hour" : "hours"} ago`;
  return `${Math.round(hrs / 24)} days ago`;
}

function runTone(run) { return run.pnl > 0.005 ? "gain" : run.pnl < -0.005 ? "loss" : "flat"; }

function runCard(run, onChange, { chartHeight = 96 } = {}) {
  const sym = run.currency_symbol;
  const tone = runTone(run);
  const stop = h("button", { type: "button", class: "small danger", onclick: async () => {
    if (!(await ask(`Stop the forward test for "${run.name}"? Its results stay in the history below.`, { ok: "Stop it", danger: true }))) return;
    const r = await attempt(() => POST(`/strategies/${encodeURIComponent(run.strategy_id)}/forward-test/stop`));
    if (!r) return;
    toast(r.alerts_turned_off ? "Forward test stopped and alerts turned off" : "Forward test stopped");
    refreshStatus(); onChange();
  } }, "Stop");
  const state = (run.health || {}).state || "watching";
  return h("article", { class: "run-card is-" + state },
    h("div", { class: "run-head" },
      h("div", {}, h("h3", {}, h("a", { href: "#/run/" + run.id }, run.name)),
        h("p", { class: "small muted" }, `${run.market}, checked ${EVERY_TEXT[run.timeframe] || run.timeframe}`)),
      h("div", { class: "run-badges" }, stateBadge(run.health),
        run.alerts ? h("span", { class: "tag", title: "Alerts are on: you get a message when the rules are met" }, "Alerts on") : null)),
    h("div", { class: "run-numbers" },
      h("div", {}, h("div", { class: "big" }, money(run.balance, sym)), h("div", { class: "lbl" }, `from ${money(run.start_balance, sym)}`)),
      h("div", {}, h("div", { class: "big " + (tone === "flat" ? "" : tone) }, signedMoney(run.pnl, sym)),
        h("div", { class: "lbl" }, signed(run.pnl_pct, "%"))),
      h("div", {}, h("div", { class: "big" }, String(run.trades)), h("div", { class: "lbl" }, run.trades ? `${run.wins} won` : "trades"))),
    lineChart(run.series, { title: "", fmt: (v) => money(v, sym), baseline: run.start_balance, tone, height: chartHeight, timeX: true }),
    h("ul", { class: "run-status small" },
      h("li", {}, doingText(run, run.direction)),
      run.health && run.health.state !== "attention" ? h("li", { class: run.health.state === "waiting" ? "muted" : "" }, marketText(run.health)) : null,
      checksText(run.health) ? h("li", { class: "muted" }, checksText(run.health)) : null),
    run.health && run.health.state === "attention" ? stateLine(run.health) : null,
    h("div", { class: "run-foot" },
      h("span", { class: "small muted" }, `Started ${when(run.started_at)} (${ago(run.started_at)})`),
      h("span", { class: "inline" }, h("a", { class: "button small", href: "#/run/" + run.id }, "Details"), stop)));
}

function comingUp(dash, newsRes, startable, pick) {
  const st = (app.status && app.status.runner) || {};
  const news = ((newsRes && newsRes.events) || [])
    .filter((e) => (e.impact === "high" || e.impact === "medium") && new Date(e.time) > Date.now()).slice(0, 4);
  const closed = dash.running.filter((r) => (r.health || {}).state === "waiting");
  const rows = [];
  if (closed.length === dash.running.length && closed.length) {
    rows.push(h("li", {}, h("span", { class: "cu-icon" }, new Date(closed[0].health.reopens).toLocaleDateString("en-GB", { weekday: "short" })), h("div", {},
      h("strong", {}, closed[0].health.why === "weekend" ? "Markets closed for the weekend" : "Market on its daily break"),
      h("div", { class: "small muted" }, `Checks start again ${reopenText(closed[0].health.reopens)}`))));
  } else if (st.next_run) {
    rows.push(h("li", {}, h("span", { class: "cu-icon" }, "↻"), h("div", {},
      h("strong", {}, `Next check at ${hhmm(st.next_run)}`),
      h("div", { class: "small muted" }, untilText(st.next_run)))));
  }
  for (const e of news) {
    rows.push(h("li", {}, h("span", { class: "cu-icon impact-" + e.impact }, e.currency),
      h("a", { href: "#/news" }, h("strong", {}, e.title),
        h("div", { class: "small muted" }, `${new Date(e.time).toLocaleDateString("en-GB", { weekday: "short" })} ${hhmm(e.time)} · ${untilText(e.time)}`))));
  }
  if (!news.length) rows.push(h("li", {}, h("span", { class: "cu-icon" }, "News"), h("div", {},
    h("strong", {}, "No big news in the next week"), h("div", { class: "small muted" }, "Big releases show up here a few days ahead"))));
  return h("aside", { class: "run-card coming-up" },
    h("h3", {}, "Coming up"),
    h("ul", { class: "cu-list" }, rows),
    h("div", { class: "cu-actions" },
      h("a", { class: "button small", href: "#/markets/stats" }, "Market stats"),
      h("a", { class: "button small", href: "#/news" }, "All news")));
}

async function viewDashboard() {
  setNav("dashboard");
  const [dash, list, settings, newsRes] = await Promise.all([GET("/dashboard"), GET("/strategies"),
    GET("/settings").catch(() => null), GET("/news/events?days=7").catch(() => null)]);
  if (!list.strategies.length && !dash.running.length && !dash.finished.length) return viewStrategies();
  const byId = Object.fromEntries(list.strategies.map((x) => [x.id, x]));
  for (const r of [...dash.running, ...dash.finished]) {
    const d = (byId[r.strategy_id] || {}).data || {};
    r.direction = d.direction || "long";
    r.alerts = r.active && statusOf(d) === "live";
  }
  const page = h("div", { class: "stack" });

  const startable = list.strategies.filter((x) => !x.error && !(x.live || {}).run);
  const pick = choiceSelect(startable.map((x) => [x.id, x.data.name || x.id]), null, () => {},
    { label: "Strategy to forward test", placeholder: "Choose a strategy…" });
  const startBtn = h("button", { type: "button", class: "primary", onclick: async () => {
    if (!pick.value) { toast("Choose a strategy to forward test", true); pick.focus(); return; }
    startBtn.disabled = true;
    try {
      await POST(`/strategies/${encodeURIComponent(pick.value)}/forward-test/start`);
      toast("Forward test started. Candles that close from now on are counted");
      refreshStatus(); viewDashboard();
    } catch (e) { toast(e.message, true); startBtn.disabled = false; }
  } }, "Start forward test");
  const starter = startable.length
    ? h("div", { class: "inline" }, pick, startBtn)
    : h("a", { class: "button primary", href: "#/create" }, "Create a strategy");

  const n = dash.running.length;
  let lede = "Forward tests follow your strategies on live prices from the moment you start them, so you can see how they'd do without risking money.";
  if (n) {
    const syms = new Set(dash.running.map((r) => r.currency_symbol));
    const total = dash.running.reduce((a, r) => a + r.pnl, 0);
    const waiting = dash.running.filter((r) => (r.health || {}).state === "waiting").length;
    const attention = dash.running.filter((r) => (r.health || {}).state === "attention").length;
    const watching = n - waiting - attention;
    const parts = [];
    if (watching) parts.push(`${watching} watching`);
    if (waiting) parts.push(`${waiting} waiting for ${waiting === 1 ? "its market" : "markets"} to open`);
    if (attention) parts.push(`${attention} ${attention === 1 ? "needs" : "need"} attention`);
    lede = `${n} forward ${n === 1 ? "test" : "tests"}: ${parts.join(", ")}.`
      + (syms.size === 1 ? ` Together they're ${signedMoney(total, [...syms][0])} since they started.` : "");
  }
  append(page, [h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "Dashboard"), h("p", { class: "muted dash-lede" }, lede)),
    h("div", { class: "actions" }, starter))]);

  // ----- getting started checklist (until it's done or dismissed)
  const steps = [
    ["Set up phone or email alerts", settings && Object.entries(settings.notifiers).some(([k, v]) => k !== "console" && v.enabled), "#/settings"],
    ["Create a strategy", list.strategies.length > 0, "#/create"],
    ["Test it on past prices", pref("ta-did-backtest", "") === "1", list.strategies[0] ? "#/s/" + list.strategies[0].id : "#/create"],
    ["Start a forward test", dash.running.length + dash.finished.length > 0, null],
  ];
  const done = steps.filter((x) => x[1]).length;
  const nearlyDone = done >= 3;
  if (done < steps.length && pref("ta-onboard-hidden", "") !== "1" && nearlyDone && pref("ta-onboard-open", "") !== "1") {
    const left = steps.find((x) => !x[1]);
    append(page, [h("div", { class: "panel onboard onboard-line" }, h("div", { class: "row-between" },
      h("p", { class: "small" }, h("strong", {}, `Getting started: ${done} of ${steps.length} done.`), " Still to do: ",
        left[2] ? h("a", { href: left[2] }, left[0]) : left[0]),
      h("div", { class: "inline" },
        h("button", { type: "button", class: "quiet small", onclick: () => { savePref("ta-onboard-open", "1"); viewDashboard(); } }, "Show"),
        h("button", { type: "button", class: "quiet small", onclick: () => { savePref("ta-onboard-hidden", "1"); viewDashboard(); } }, "Hide"))))]);
  } else if (done < steps.length && pref("ta-onboard-hidden", "") !== "1") {
    const card = h("div", { class: "panel onboard" }, h("section", { class: "section" },
      h("div", { class: "row-between" }, h("div", {}, h("h2", {}, "Getting started"), h("p", { class: "small muted" }, `${done} of ${steps.length} done`)),
        h("div", { class: "inline" },
          nearlyDone ? h("button", { type: "button", class: "quiet", onclick: () => { savePref("ta-onboard-open", ""); viewDashboard(); } }, "Collapse") : null,
          h("button", { type: "button", class: "quiet", onclick: () => { savePref("ta-onboard-hidden", "1"); card.remove(); } }, "Hide"))),
      h("div", { class: "progress" }, h("span", { class: "progress-fill" })),
      h("ol", { class: "steps-list" }, steps.map(([t, ok, href]) => h("li", { class: ok ? "done" : "" },
        h("span", { class: "step-check", "aria-hidden": "true" }, ok ? "✓" : ""),
        ok || !href ? h("span", {}, t, ok ? h("span", { class: "sr-only" }, " (done)") : null) : h("a", { href }, t))))));
    card.querySelector(".progress-fill").style.width = `${(done / steps.length) * 100}%`;
    append(page, [card]);
  }

  // ----- summary strip
  if (n) {
    const syms = new Set(dash.running.map((r) => r.currency_symbol));
    const one = syms.size === 1 ? [...syms][0] : null;
    const total = dash.running.reduce((a, r) => a + r.balance, 0);
    const dayAgo = Date.now() - 86400000;
    let today = 0;
    for (const r of dash.running) {
      const before = (r.series || []).filter((p) => new Date(p[0]) <= dayAgo).pop();
      today += r.balance - (before ? before[1] : r.start_balance);
    }
    const nextNews = newsRes && newsRes.events.find((e) => e.impact === "high" && new Date(e.time) > Date.now());
    const st = app.status && app.status.runner;
    const nextCheck = st && st.next_run ? hhmm(st.next_run) : null;
    const newsText = nextNews ? `${untilText(nextNews.time).replace(/^in /, "")} until ${nextNews.currency} ${nextNews.title}` : "no big news this week";
    if (n <= 2) {
      // a card or two: one line is enough, the cards carry the detail
      const bits = [];
      if (one) bits.push(h("span", {}, "Paper balance ", h("strong", { class: "num" }, money(total, one))));
      if (one) bits.push(h("span", {}, "Last 24 hours ", h("strong", { class: "num " + (today >= 0 ? "gain" : "loss") }, signedMoney(today, one))));
      bits.push(h("a", { href: "#/news" }, newsText[0].toUpperCase() + newsText.slice(1)));
      if (nextCheck) bits.push(h("button", { type: "button", class: "linklike", onclick: () => toggleRunnerPanel(true) }, `Next check ${nextCheck}`));
      append(page, [h("p", { class: "summary-line" }, bits.flatMap((b, i) => (i ? [h("span", { class: "sep", "aria-hidden": "true" }, "·"), b] : [b])))]);
    } else {
      const tile = (big, label, cls = "", href = null, onclick = null) => {
        const inner = [h("div", { class: "big " + cls }, big), h("div", { class: "lbl" }, label)];
        if (onclick) return h("button", { type: "button", class: "tile", onclick }, inner);
        return href ? h("a", { class: "tile", href }, inner) : h("div", { class: "tile" }, inner);
      };
      append(page, [h("div", { class: "panel summary-strip" },
        one ? tile(money(total, one), `paper balance across ${n} tests`, "", "#/strategies") : tile(String(n), "forward tests running", "", "#/strategies"),
        one ? tile(signedMoney(today, one), "in the last 24 hours", today >= 0 ? "gain" : "loss", "#/activity?filter=trades") : null,
        tile(nextNews ? untilText(nextNews.time).replace(/^in /, "") : "–", nextNews ? `until ${nextNews.currency} ${nextNews.title}` : "no big news this week", "", "#/news"),
        tile(nextCheck || "–", "next check", "", null, nextCheck ? () => toggleRunnerPanel(true) : null))]);
    }
  }

  if (n) {
    const few = n <= 2;
    const grid = h("div", { class: "run-grid" + (few ? " few-" + n : "") },
      dash.running.map((r) => runCard(r, viewDashboard, { chartHeight: n === 1 ? 170 : few ? 130 : 96 })));
    // With only a card or two there's a lot of empty space, so fill it with what's coming up.
    if (few || n % 3) grid.appendChild(comingUp(dash, newsRes, startable, pick));
    append(page, [grid]);
  } else {
    append(page, [h("div", { class: "panel" }, h("div", { class: "empty" }, h("h2", {}, "No forward tests running"),
      h("p", { class: "muted" }, startable.length
        ? "Pick a strategy above and press Start. Each time a candle closes, the rules are checked and any trades are recorded here as if you'd placed them."
        : "Create a strategy first, then start a forward test to watch how it does from now on.")))]);
  }

  if (dash.events.length) {
    const KIND = { signal: "Rules met", opened: "Trade opened", closed: "Trade closed", skipped: "Signal skipped" };
    const nameOf = (sid) => ((byId[sid] || {}).data || {}).name || sid;
    const item = (e) => h("li", {},
      h("span", { class: "muted" }, when(e.bar_time)),
      h("div", {}, h("span", { class: "event-kind " + e.kind }, KIND[e.kind] || e.kind), " ", nameOf(e.strategy_id),
        e.kind === "closed" || e.kind === "skipped" ? h("div", { class: "what muted" }, (e.message || "").split("\n").slice(1).join("\n")) : null));
    const ul = h("ul", { class: "events" }, dash.events.slice(0, 8).map(item));
    const more = dash.events.length > 8 ? h("button", { type: "button", class: "quiet", onclick: () => {
      clear(ul); dash.events.forEach((e) => ul.appendChild(item(e))); more.remove();
    } }, `Show all ${dash.events.length}`) : null;
    append(page, [h("div", { class: "panel" }, h("section", { class: "section" }, h("h2", {}, "Latest activity"), ul, more))]);
  }

  if (dash.finished.length) {
    append(page, [h("div", { class: "panel" }, h("section", { class: "section" }, h("h2", {}, "Finished forward tests"),
      h("div", { class: "table-wrap" }, h("table", { class: "trades" },
        h("thead", {}, h("tr", {}, h("th", {}, "Strategy"), h("th", {}, "Ran"), h("th", { class: "num" }, "Trades"),
          h("th", { class: "num" }, "Result"), h("th", {}))),
        h("tbody", {}, dash.finished.map((r) => h("tr", {},
          h("td", {}, h("a", { href: "#/run/" + r.id }, r.name)),
          h("td", {}, `${day(r.started_at)} to ${day(r.stopped_at)}`),
          h("td", { class: "num" }, String(r.trades)),
          h("td", { class: "num " + runTone(r) }, `${signedMoney(r.pnl, r.currency_symbol)} (${signed(r.pnl_pct, "%")})`),
          h("td", { class: "num" }, h("button", { type: "button", class: "quiet", onclick: (ev) => {
            const tr = ev.target.closest("tr");
            undoable("Forward test removed from the history", (on) => { tr.hidden = on; }, async () => { await DEL("/runs/" + r.id); });
          } }, "Remove")))))))))]);
  }
  clear($main).appendChild(page);
}

function span(ms) {
  const hrs = ms / 3600000;
  if (hrs < 1) return `${Math.max(1, Math.round(ms / 60000))} minutes`;
  if (hrs < 48) return `${Math.round(hrs)} hours`;
  return `${Math.round(hrs / 24)} days`;
}

/** The forward test's balance chart, with an optional range from the strategy's own backtest behind it. */
function compareBox(r, tone) {
  const sym = r.currency_symbol;
  const box = h("div", { class: "compare" });
  const chartBox = h("div");
  const note = h("div", { class: "compare-note", "aria-live": "polite" });
  let band = null;
  const draw = () => clear(chartBox).appendChild(lineChart(r.series, { title: "Balance since the start", fmt: (v) => money(v, sym),
    baseline: r.start_balance, tone, height: 230, timeX: true, band: band && band.points }));
  draw();
  const period = periodPicker();
  const btn = h("button", { type: "button" }, "Compare with backtest");
  const hide = h("button", { type: "button", class: "quiet", hidden: true }, "Hide");
  hide.addEventListener("click", () => { band = null; draw(); clear(note); hide.hidden = true; btn.textContent = "Compare with backtest"; });
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    clear(note).appendChild(h("p", { class: "loading" }, "Backtesting the period before this forward test started…"));
    try {
      band = await POST(`/runs/${r.id}/backtest-range`, period.value());
      draw();
      clear(note).appendChild(describeComparison(r, band));
      hide.hidden = false;
      btn.textContent = "Update";
    } catch (e) { clear(note).appendChild(h("p", { class: "error-box" }, e.message)); }
    finally { btn.disabled = false; }
  });
  append(box, [chartBox,
    h("div", { class: "compare-controls" }, h("p", { class: "small muted" },
      "See how this compares with the strategy's own backtest over the years before the test started."),
    h("div", { class: "bt-controls" }, period, btn, hide)), note]);
  return box;
}

function describeComparison(r, res) {
  const out = h("div");
  const bt = res.backtest;
  const pts = res.points;
  const start = r.start_balance;
  const pct = (v) => (v / start - 1) * 100;
  append(out, [h("p", { class: "small muted" }, `Backtest from ${day(bt.start)} to ${day(bt.end)}: ${signed(bt.return_pct, "%")}, ${bt.trades} trades, with the rules as they are now.`)]);
  if (pts.length < 2) {
    out.appendChild(h("p", {}, "Not enough time has passed to compare yet. Check back once a few candles have closed."));
    return out;
  }
  const last = pts[pts.length - 1];
  const elapsed = new Date(last[0]) - new Date(pts[0][0]);
  const now = pct(r.balance);
  let where;
  if (r.balance < last[1]) where = "below the usual range: worse than 9 in 10 stretches of the backtest";
  else if (r.balance < last[2]) where = "in the lower part of the usual range";
  else if (r.balance <= last[4]) where = "inside the middle half of the backtest's results";
  else if (r.balance <= last[5]) where = "in the upper part of the usual range";
  else where = "above the usual range: better than 9 in 10 stretches of the backtest";
  append(out, [
    h("p", {}, `Over stretches as long as this test has run (${span(elapsed)}), the middle half of the backtest's results were between `,
      h("strong", {}, signed(pct(last[2]), "%")), " and ", h("strong", {}, signed(pct(last[4]), "%")),
      `. This forward test is at `, h("strong", { class: now >= 0 ? "gain" : "loss" }, signed(now, "%")), `, ${where}.`),
    res.complete ? null : h("p", { class: "small muted" }, "The backtest isn't long enough to compare the whole forward test. Try a longer period."),
    h("p", { class: "legend small muted" }, "Shaded:", h("span", {}, h("span", { class: "swatch inner" }), "middle 50%"),
      h("span", {}, h("span", { class: "swatch outer" }), "middle 80% of the backtest's results over the same length of time")),
    h("p", { class: "small muted" }, "It's the strategy's own history for comparison, not a forecast."),
  ]);
  if (res.windows) out.appendChild(windowTable(r, res.windows));
  return out;
}

const WINDOW_ROWS = [
  ["win_rate_pct", "Win rate", (v) => num(v, 0) + "%"],
  ["profit_factor", "Profit factor", (v) => num(v, 2)],
  ["avg_r", "Average R", (v) => signed(v, "R")],
  ["max_drawdown_pct", "Worst drop", (v) => num(v, 1) + "%"],
  ["trades_per_month", "Trades a month", (v) => num(v, 1)],
];
const COST_ROWS = [["gross_pnl", "Gross P/L"], ["spread_cost", "Spread cost"], ["funding", "Funding cost"]];
const POSITION_TEXT = { below: "below the usual range", within: "within the usual range", above: "above the usual range" };

function windowTable(r, w) {
  const sym = r.currency_symbol;
  const box = h("div", { class: "stack tight" });
  const title = h("h3", {}, `The last ${w.window} trade${w.window === 1 ? "" : "s"}, against the backtest's usual range`);
  if (w.notice) box.appendChild(h("p", { class: "notice small" }, w.notice));
  if (!w.live_trades) {
    append(box, [title, h("p", { class: "muted" }, "No closed trades yet. The comparison starts with the first one.")]);
    return box;
  }
  if (w.reason || !Object.keys(w.metrics || {}).length) {
    append(box, [title, h("p", { class: "muted" }, w.reason || "The backtest gave no range to compare with.")]);
    return box;
  }
  const row = (key, label, fmt) => {
    const m = w.metrics[key];
    if (!m) return null;
    return h("tr", {}, h("td", {}, label), h("td", { class: "num" }, m.live === null ? "–" : fmt(m.live)),
      h("td", { class: "num" }, `${fmt(m.low)} to ${fmt(m.high)}`), h("td", {}, POSITION_TEXT[m.position] || "–"));
  };
  const head = h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", { class: "num" }, "This test"),
    h("th", { class: "num" }, "Backtest, 10th–90th pct"), h("th", {}, "Where it sits")));
  const money2 = (v) => signedMoney(v, sym);
  append(box, [title,
    h("div", { class: "table-wrap" }, h("table", { class: "compare-table" }, head,
      h("tbody", {}, WINDOW_ROWS.map(([k, l, f]) => row(k, l, f))))),
    h("h3", {}, "Where the money went"),
    h("div", { class: "table-wrap" }, h("table", { class: "compare-table" }, head,
      h("tbody", {}, COST_ROWS.map(([k, l]) => row(k, l, money2))))),
    h("p", { class: "small muted" }, `Each range is from ${w.backtest_trades} backtest trades, resampled into stretches of ${w.window} in a row. ` +
      "Gross P/L is before spread and funding; net is what the balance shows."),
  ]);
  return box;
}

function paperPrefill(r, t) {
  /* What the journal can take from a paper trade: the same market, side, times and prices, linked to it. */
  const known = (app.recipes.markets || []).find((m) => m.label === r.market || m.key === r.market);
  return { market: known ? known.key : r.market, direction: t.direction || "long", entry_time: t.entry_time,
    entry_price: t.entry_price, exit_time: t.exit_time, exit_price: t.exit_price, size: t.size,
    currency: r.currency || null, paper_trade_id: t.id };
}

async function viewRun(id) {
  setNav("dashboard");
  const r = await GET("/runs/" + encodeURIComponent(id));
  let direction = "long", alerts = false;
  if (r.strategy_exists) {
    try {
      const rec = await GET("/strategies/" + encodeURIComponent(r.strategy_id));
      direction = rec.data.direction || "long";
      alerts = r.active && statusOf(rec.data) === "live";
    } catch (_) { /* strategy unreadable */ }
  }
  const sym = r.currency_symbol;
  const tone = runTone(r);
  const actions = h("div", { class: "actions" },
    r.strategy_exists ? h("a", { class: "button", href: "#/s/" + r.strategy_id }, "Open strategy") : null,
    r.active ? h("button", { type: "button", onclick: async () => {
      if (!(await ask("Stop this forward test? Its results stay in the history.", { ok: "Stop it", danger: true }))) return;
      if (!(await attempt(() => POST(`/strategies/${encodeURIComponent(r.strategy_id)}/forward-test/stop`)))) return;
      toast("Forward test stopped"); refreshStatus(); viewRun(id);
    } }, "Stop forward test") : h("button", { type: "button", class: "danger", onclick: async () => {
      if (!(await ask("Remove this forward test from the history? This can't be undone.", { ok: "Remove", danger: true }))) return;
      if (!(await attempt(() => DEL("/runs/" + r.id)))) return;
      toast("Removed"); location.hash = "#/";
    } }, "Remove"));

  const period = r.active
    ? `Running since ${when(r.started_at)} (${ago(r.started_at)}).`
    : `Ran from ${when(r.started_at)} to ${when(r.stopped_at)}.`;
  const page = h("div", { class: "stack" },
    h("div", {}, h("a", { class: "back", href: "#/" }, "Dashboard")),
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, r.name),
        h("p", { class: "muted" }, `${r.market}, checked ${EVERY_TEXT[r.timeframe] || r.timeframe}. ${period}`),
        h("div", { class: "inline run-pills" }, [stateBadge(r.health || { state: r.active ? "watching" : "stopped" }), r.active && alerts ? h("span", { class: "tag" }, "Alerts on") : null])),
      actions),
    h("div", { class: "editor" },
      h("div", { class: "panel" }, h("section", { class: "section" },
        h("div", { class: "headline" },
          h("div", {}, h("div", { class: "big" }, money(r.balance, sym)), h("div", { class: "lbl" }, `Balance, from ${money(r.start_balance, sym)}`)),
          h("div", {}, h("div", { class: "big " + (tone === "flat" ? "" : tone) }, `${signedMoney(r.pnl, sym)}`), h("div", { class: "lbl" }, signed(r.pnl_pct, "%"))),
          h("div", {}, h("div", { class: "big" }, String(r.trades)), h("div", { class: "lbl" }, r.trades ? `trades, ${r.wins} won` : "trades")),
          h("div", {}, h("div", { class: "big" }, num(Math.abs(r.max_drawdown_pct), 1) + "%"), h("div", { class: "lbl" }, "Worst drop"))),
        compareBox(r, tone),
        h("p", { class: "small muted" }, r.active ? positionText(r, direction) + "." : "",
          r.last_candle ? ` Last candle counted: ${when(r.last_candle)}.` : " No candles have closed since it started yet."))),
      h("div", { class: "panel" }, h("section", { class: "section" }, h("h2", {}, "Trades"),
        r.trade_list.length ? h("div", { class: "table-wrap" }, h("table", { class: "trades" },
          h("thead", {}, h("tr", {}, ["Opened", "Entry", "Closed", "Exit", "Reason"].map((x) => h("th", {}, x)),
            h("th", { class: "num" }, "P/L"), h("th", {}))),
          h("tbody", {}, r.trade_list.map((t) => h("tr", {},
            h("td", {}, when(t.entry_time)), h("td", {}, num(t.entry_price)), h("td", {}, when(t.exit_time)),
            h("td", {}, num(t.exit_price)), h("td", {}, (t.exit_reason || "").replace(/_/g, " ")),
            h("td", { class: "num " + (t.pnl >= 0 ? "gain" : "loss") }, signedMoney(t.pnl, sym)),
            h("td", { class: "num" }, h("button", { type: "button", class: "quiet", title: "Add the trade you placed on this alert to the journal, linked to this paper trade",
              onclick: () => journalDialog(null, () => { location.hash = "#/journal"; }, paperPrefill(r, t)) }, "Log real trade")))))))
          : h("p", { class: "muted" }, r.position ? "A trade is open. It will appear here once it closes." : "No trades yet. They appear here as the rules are met.")))));
  if (r.active && r.strategy_exists) page.appendChild(liveStrategyChart(r.strategy_id, r.market));
  clear($main).appendChild(page);
}

/** The strategy's market as live candles, with its signals, paper trades and open trade's stop and target. */
function liveStrategyChart(sid, label) {
  const box = h("section", { class: "section" });
  const status = h("span", { class: "live-status small muted" });
  const holder = h("div", {}, h("p", { class: "loading" }, "Loading prices…"));
  let chart = null;
  async function load() {
    try {
      const d = await GET(`/candles?strategy=${encodeURIComponent(sid)}&bars=300`);
      if (!chart) { chart = candleChart(d, { title: label }); clear(holder).appendChild(chart); } else chart.update(d);
      clear(status);
      append(status, [h("span", { class: "dot on" }), `Live · updated ${new Date(d.updated).toLocaleTimeString("en-GB")}`]);
    } catch (e) { if (!chart) clear(holder).appendChild(h("p", { class: "error-box" }, e.message)); }
  }
  append(box, [h("div", { class: "row-between" }, h("h2", {}, `${label}, live`), status),
    h("p", { class: "small muted legend-marks" }, h("span", { class: "lm buy" }, "▲"), " buy  ", h("span", { class: "lm sell" }, "▼"),
      " sell  ", h("span", { class: "lm exit" }, "●"), " closed  ", h("span", { class: "lm signal" }, "◆"), " rules met"), holder]);
  load();
  every(60, load);
  return h("div", { class: "panel" }, box);
}
