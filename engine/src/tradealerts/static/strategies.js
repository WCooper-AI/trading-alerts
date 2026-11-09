"use strict";

/* Trading Alerts web interface: The strategies list and the full strategy editor (market, entry, exit, risk).
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- page: strategies list

function importButton() {
  const file = h("input", { type: "file", accept: ".yaml,.yml,text/yaml", hidden: true });
  file.addEventListener("change", async () => {
    const f = file.files[0];
    if (!f) return;
    try { const r = await POST("/strategies/import", { yaml: await f.text() }); toast("Imported as a draft"); location.hash = "#/s/" + r.id; }
    catch (e) { toast(e.message, true); }
    file.value = "";
  });
  return h("span", {}, file, h("button", { type: "button", class: "quiet", onclick: () => file.click(), title: "Import a strategy file someone exported" }, "Import"));
}

async function viewStrategies() {
  setNav("strategies");
  const page = h("div");
  clear($main).appendChild(page);
  const list = await GET("/strategies");
  const items = list.strategies;

  if (!items.length) {
    append(page, [h("div", { class: "welcome" },
      h("h1", {}, "Test a trading idea, then get told when it happens"),
      h("p", { class: "lede" }, "Pick a market and an idea, try it on past prices, and turn on alerts if you like what you see. Nothing is traded for you, and every strategy starts as a draft."),
      h("div", { class: "actions" },
        h("a", { class: "button primary big", href: "#/create" }, "Create your first strategy"),
        h("a", { class: "button quiet", href: "#/new" }, "Build from scratch (advanced)")))]);
    return;
  }

  append(page, [h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "Strategies"),
      h("p", { class: "muted" }, "Drafts are for testing. Strategies with alerts on are checked on every new candle.")),
    h("div", { class: "actions" },
      h("a", { class: "button quiet", href: "#/new" }, "Build from scratch"),
      importButton(),
      h("a", { class: "button", href: "#/compare" }, "Compare"),
      h("a", { class: "button", href: "#/portfolio" }, "Test together"),
      h("a", { class: "button primary", href: "#/create" }, "Create a strategy")))]);

  // ----- search, filter and sort (remembered between visits)
  const STATUS_LABEL = { live: "Alerts on", testing: "Forward testing", draft: "Drafts", paused: "Paused", broken: "Needs fixing" };
  const info = items.map((it) => {
    const d = it.data || {};
    const run = (it.live || {}).run;
    const st = it.error ? "broken" : displayStatus(d, run);
    return { it, d, run, st, market: (d.market || {}).label || (d.market || {}).symbol || "",
      text: `${d.name || it.file} ${(d.market || {}).label || ""} ${(d.recipe || {}).id || ""}`.toLowerCase() };
  });
  const state = { q: "", status: pref("ta-st-status", "all"), market: pref("ta-st-market", ""), sort: pref("ta-st-sort", "name") };
  const statuses = ["all", ...Object.keys(STATUS_LABEL).filter((k) => info.some((x) => x.st === k))];
  const markets = [...new Set(info.map((x) => x.market).filter(Boolean))].sort();
  if (!statuses.includes(state.status)) state.status = "all";
  if (state.market && !markets.includes(state.market)) state.market = "";

  const search = h("input", { type: "search", class: "search", placeholder: "Search strategies", "aria-label": "Search strategies",
    oninput: (e) => { state.q = e.target.value.trim().toLowerCase(); renderRows(); } });
  const chips = h("div", { class: "chips", role: "group", "aria-label": "Filter by status" });
  const renderChips = () => {
    clear(chips);
    for (const k of statuses) {
      const n = k === "all" ? info.length : info.filter((x) => x.st === k).length;
      chips.appendChild(h("button", { type: "button", class: "chip", "aria-pressed": String(state.status === k),
        onclick: () => { state.status = k; savePref("ta-st-status", k); renderChips(); renderRows(); } },
        k === "all" ? "All" : STATUS_LABEL[k], h("span", { class: "chip-count" }, String(n))));
    }
  };
  const marketSel = h("select", { "aria-label": "Filter by market", onchange: (e) => { state.market = e.target.value; savePref("ta-st-market", state.market); renderRows(); } },
    h("option", { value: "" }, "All markets"), markets.map((m) => h("option", { value: m }, m)));
  marketSel.value = state.market;
  const SORTS = { name: "Name (A to Z)", market: "Market", status: "Status", pnl: "Forward test result", trades: "Most trades", newest: "Newest forward test" };
  const sortSel = h("select", { "aria-label": "Sort by", onchange: (e) => { state.sort = e.target.value; savePref("ta-st-sort", state.sort); renderRows(); } },
    Object.entries(SORTS).map(([k, t]) => h("option", { value: k }, "Sort: " + t)));
  sortSel.value = SORTS[state.sort] ? state.sort : "name";
  const STATUS_ORDER = { live: 0, testing: 1, paused: 2, draft: 3, broken: 4 };
  const byName = (a, b) => (a.d.name || "").localeCompare(b.d.name || "");
  const SORTERS = {
    name: byName,
    market: (a, b) => a.market.localeCompare(b.market) || byName(a, b),
    status: (a, b) => (STATUS_ORDER[a.st] ?? 9) - (STATUS_ORDER[b.st] ?? 9) || byName(a, b),
    pnl: (a, b) => (b.run ? b.run.pnl_pct ?? b.run.pnl : -Infinity) - (a.run ? a.run.pnl_pct ?? a.run.pnl : -Infinity) || byName(a, b),
    trades: (a, b) => ((b.run || {}).trades ?? -1) - ((a.run || {}).trades ?? -1) || byName(a, b),
    newest: (a, b) => String((b.run || {}).started_at || "").localeCompare(String((a.run || {}).started_at || "")) || byName(a, b),
  };
  const count = h("span", { class: "small muted" });
  const tbody = h("tbody");
  function renderRows() {
    const shown = info.filter((x) => (state.status === "all" || x.st === state.status) &&
      (!state.market || x.market === state.market) && (!state.q || x.text.includes(state.q)))
      .sort(SORTERS[state.sort] || byName);
    count.textContent = shown.length === info.length ? `${info.length} strategies` : `${shown.length} of ${info.length} shown`;
    clear(tbody);
    if (!shown.length) {
      tbody.appendChild(h("tr", {}, h("td", { colspan: 6 }, h("p", { class: "muted empty-filter" }, "Nothing matches. ",
        h("button", { type: "button", class: "quiet", onclick: () => {
          state.q = ""; search.value = ""; state.status = "all"; state.market = ""; marketSel.value = "";
          savePref("ta-st-status", "all"); savePref("ta-st-market", ""); renderChips(); renderRows();
        } }, "Clear filters")))));
      return;
    }
    for (const x of shown) tbody.appendChild(strategyRow(x));
  }
  function strategyRow({ it, d, run, st }) {
    const sym = CURRENCY[(d.risk || {}).currency || "GBP"] || "";
    const position = run ? positionText(run, d.direction) : "Not running";
    const go = () => { location.hash = "#/s/" + it.id; };
    return h("tr", { class: "row-link", onclick: (e) => { if (e.target.tagName !== "A") go(); } },
      h("td", {}, h("a", { class: "name", href: "#/s/" + it.id }, d.name || it.file),
        it.error ? h("p", { class: "small loss" }, it.error) : renderParts(describeCondition(d.entry || {}), "sentence small-sentence")),
      h("td", { class: "nowrap" }, (d.market || {}).label || (d.market || {}).symbol || "–", h("div", { class: "small muted" }, (d.market || {}).timeframe || "")),
      h("td", {}, (st === "testing" || st === "live") && problemOf(it.id)
        ? h("span", { class: "pill problem", title: problemOf(it.id) }, "Problem")
        : pill(st, st === "broken" ? "Needs fixing" : null)),
      h("td", { class: "num" }, run ? money(run.balance, sym) : "–",
        h("div", { class: "small muted" }, position)),
      h("td", { class: "num spark-cell" }, sparkline(it.spark, run ? (run.pnl >= 0 ? "gain" : "loss") : "")),
      h("td", { class: "num" }, run ? h("span", { class: run.pnl >= 0 ? "gain" : "loss" }, signedMoney(run.pnl, sym)) : "–",
        h("div", { class: "small muted" }, run ? `${run.trades} ${run.trades === 1 ? "trade" : "trades"} since ${day(run.started_at)}` : "")));
  }
  renderChips();
  renderRows();
  append(page, [h("div", { class: "panel" },
    h("div", { class: "toolbar" }, search, chips, h("span", { class: "spacer" }), marketSel, sortSel),
    h("div", { class: "table-wrap" }, h("table", { class: "list" },
      h("thead", {}, h("tr", {}, h("th", {}, "Strategy"), h("th", {}, "Market"), h("th", {}, "Status"),
        h("th", { class: "num" }, "Forward test"), h("th", { class: "num spark-cell" }, h("span", { class: "sr-only" }, "Balance trend")),
        h("th", { class: "num" }, "Since it started"))),
      tbody)),
    h("div", { class: "table-foot" }, count))]);
  // "/" jumps to the search box
  const slash = (e) => {
    if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); search.focus(); }
  };
  document.addEventListener("keydown", slash);
  app.cleanups.push(() => document.removeEventListener("keydown", slash));
}

const BLANK = {
  name: "New strategy", template: true,
  market: {},
  entry: { all: [newComparison()] },
  exit: {},
  risk: { account_size: null, risk_per_trade_pct: null, currency: null },
  costs: {},
  alerts: { enabled: true, priority: "high" },
};

/** Choices the advanced editor still needs before it can test or save. */
function stillToChoose(d) {
  const out = [];
  const m = d.market || {};
  if (!m.symbol) out.push("market");
  if (!m.timeframe) out.push("candle size");
  if (!d.direction) out.push("direction");
  if (!d.exit || !d.exit.stop) out.push("stop loss");
  if (!d.exit || d.exit.target === undefined) out.push("take profit (or No target)");
  const r = d.risk || {};
  if (isBlank(r.account_size)) out.push("starting balance");
  if (isBlank(r.risk_per_trade_pct)) out.push("risk per trade");
  if (!r.currency) out.push("currency");
  return out;
}

/** Run a save (or restore) of a strategy. If its trading rules changed while a forward test is running,
    the server asks first: let the user choose, then send it again with that choice.
    ``send(onRunning)`` makes the request. Resolves to its result, or null if the user cancelled. */
async function withRunningChoice(send, { message = "This strategy has a forward test running, and you've changed its trading rules.",
  cancel = "Don't save" } = {}) {
  try { return await send(undefined); }
  catch (e) {
    if (e.code !== "running_test") throw e;
    const pick = await choose(message, [
      { key: "keep", primary: true, label: "Keep the forward test running with the new rules",
        detail: "Results so far stay. The new rules apply from the next candle, and the test's history notes the change." },
      { key: "restart", label: "Finish it and start a fresh forward test",
        detail: "The current test is kept under Finished forward tests with its results. A new one starts with the new rules." },
    ], { title: "What should happen to the forward test?", cancel });
    if (!pick) return null;
    return send(pick);
  }
}

function savedToast(r) {
  toast(r.forward_test === "restarted" ? "Changes saved. A fresh forward test has started"
    : r.forward_test === "kept" ? "Changes saved. The forward test uses the new rules from the next candle"
    : "Changes saved");
}

function historyLink(id) {
  return h("a", { class: "button quiet", href: "#/history/" + encodeURIComponent(id), title: "Earlier versions of this strategy" }, "History");
}

async function viewEditor(id, exampleName, preloaded, run) {
  setNav("strategies");
  let data, isNew = !id;
  if (preloaded) data = preloaded;
  else if (id) data = (await GET("/strategies/" + encodeURIComponent(id))).data;
  else if (exampleName) {
    data = await GET("/examples/" + encodeURIComponent(exampleName));
    data.name = data.name.replace(" (template)", "");
    data.template = true;
  } else data = clone(BLANK);

  // normalise for editing
  data.entry = asGroup(data.entry);
  data.exit = data.exit || {};
  if (data.exit.when) data.exit.when = asGroup(data.exit.when);
  data.market = data.market || {};
  data.risk = data.risk || {};
  data.costs = data.costs || {};
  data.alerts = data.alerts || {};
  if (!isNew && data.exit.target === undefined) data.exit.target = null; // saved without a target: "No target"

  let dirty = isNew;
  const summary = h("div");
  const validity = h("p", { class: "validity", "aria-live": "polite" });
  const dirtyNote = h("span", { class: "dirty" });
  const saveBtn = h("button", { class: "primary", type: "button", onclick: save }, isNew ? "Save strategy" : "Save changes");
  let validateTimer;

  function changed() {
    dirty = true;
    dirtyNote.textContent = "Unsaved changes";
    clear(summary).appendChild(renderParts(describeStrategy(data)));
    clearTimeout(validateTimer);
    const missing = stillToChoose(data);
    if (missing.length) {
      validity.className = "validity todo";
      validity.textContent = "Still to choose: " + missing.join(", ") + ".";
      return;
    }
    validateTimer = setTimeout(checkRules, 450);
  }
  async function checkRules() {
    try {
      const r = await POST("/validate", { data: exportData() });
      validity.className = "validity " + (r.ok ? "ok" : "bad");
      validity.textContent = r.ok ? "Rules check out." : r.error;
    } catch (e) { /* shown elsewhere */ }
  }
  function exportData() {
    const d = clone(data);
    if (d.exit && d.exit.when && !groupItems(d.exit.when).length) delete d.exit.when;
    if (d.exit && (d.exit.max_bars === null || d.exit.max_bars === undefined)) delete d.exit.max_bars;
    for (const k of ["max_size", "point_value", "size_step", "min_size"]) if (d.risk && isBlank(d.risk[k])) delete d.risk[k];
    for (const k of ["spread", "funding_pct"]) if (d.costs && isBlank(d.costs[k])) delete d.costs[k];
    if (d.exit && d.exit.target === null) delete d.exit.target;
    if (d.market && !d.market.lookback_days) delete d.market.lookback_days;
    if (!d.description) delete d.description;
    return d;
  }
  async function save() {
    const missing = stillToChoose(data);
    if (missing.length) { toast("Still to choose: " + missing.join(", "), true); return false; }
    saveBtn.disabled = true;
    try {
      if (isNew) {
        const r = await POST("/strategies", { data: exportData() });
        dirty = false;
        toast("Strategy saved");
        app.guard = null;
        app.isDirty = null;
        location.replace("#/s/" + r.id);
      } else {
        const r = await withRunningChoice((onRunning) =>
          PUT("/strategies/" + encodeURIComponent(id), { data: exportData(), on_running: onRunning }));
        if (!r) return false;
        dirty = false;
        dirtyNote.textContent = "Saved";
        savedToast(r);
      }
      refreshStatus();
      return true;
    } catch (e) {
      validity.className = "validity bad";
      validity.textContent = e.message;
      toast("Not saved: " + e.message, true);
      return false;
    } finally { saveBtn.disabled = false; }
  }
  app.isDirty = () => dirty;
  app.guard = () => (dirty ? "You have unsaved changes. Leave without saving?" : null);

  // ----- header: name, status, sentence
  const title = h("input", { class: "title-input", value: data.name || "", "aria-label": "Strategy name",
    oninput: (e) => { data.name = e.target.value; changed(); } });
  const statusSeg = statusControls({ id, isNew, run, getData: () => data, isDirty: () => dirty, save });
  const deleteBtn = isNew ? null : h("button", { class: "danger", type: "button", onclick: async () => {
    if (!(await ask(`Delete "${data.name}"? Its forward test stops and it moves to the deleted strategies folder.`, { ok: "Delete", danger: true }))) return;
    if ((await attempt(() => DEL("/strategies/" + encodeURIComponent(id)))) === undefined) return;
    app.guard = null;
    toast("Strategy deleted");
    location.hash = "#/strategies";
  } }, "Delete");
  const dupBtn = isNew ? null : h("button", { type: "button", onclick: async () => {
    const copy = exportData();
    copy.name = (copy.name || "Strategy") + " copy";
    copy.template = true; delete copy.enabled;
    const r = await attempt(() => POST("/strategies", { data: copy }));
    if (!r) return;
    toast("Copy created as a draft");
    app.guard = null;
    location.hash = "#/s/" + r.id;
  } }, "Duplicate");

  const head = h("div", { class: "editor-head" },
    h("a", { class: "back", href: "#/strategies" }, "All strategies"),
    data.recipe ? h("p", { class: "notice" }, "This strategy was made with the guided setup. If you change its rules here it becomes a custom strategy and opens in this full editor from then on. ",
      h("a", { href: "#/s/" + id }, "Back to the simple view")) : null,
    title,
    h("div", { class: "row" }, h("div", { class: "inline" }, statusSeg, dirtyNote),
      h("div", { class: "inline" }, dupBtn, isNew ? null : historyLink(id), isNew ? null : shareButtons(id, exportData), deleteBtn, saveBtn)),
    summary, validity);

  // ----- sections
  const left = h("div", { class: "panel" });
  function rerender() {
    clear(left);
    const jump = h("nav", { class: "section-nav", "aria-label": "Sections" });
    append(left, [jump,
      marketSection(data, changed),
      h("section", { class: "section" }, h("h2", {}, "Entry"),
        h("p", { class: "section-note" }, "Checked when each candle closes. If the rules are true, you get an alert and a paper trade opens at the next candle's open."),
        groupEditor(() => data.entry, (v) => { data.entry = v; }, changed)),
      exitSection(data, changed),
      riskSection(data, changed)]);
    left.querySelectorAll(":scope > section").forEach((sec, i) => {
      const title = sec.querySelector("h2");
      if (!title) return;
      sec.id = "sec-" + i;
      jump.appendChild(h("a", { href: "#", onclick: (e) => { e.preventDefault(); sec.scrollIntoView({ behavior: "smooth", block: "start" }); } }, title.textContent));
    });
  }
  rerender();

  const testable = () => {
    const missing = stillToChoose(data);
    if (missing.length) { toast("Still to choose: " + missing.join(", "), true); return null; }
    return exportData();
  };
  const right = h("div", { class: "side" }, analysisPanel({
    getData: testable, render: renderBacktest, strategyId: isNew ? null : id,
    intro: "Tests the rules as they are on screen, including unsaved changes, on the market's price history.",
    settings: () => pathSettings(data),
    apply: (setting, value) => { setPath(data, setting.path, value); changed(); rerender(); },
  }));

  clear($main);
  append($main, [head, h("div", { class: "editor" }, left, right)]);
  changed();
  dirty = isNew;
  dirtyNote.textContent = isNew ? "Not saved yet" : "";
}

function randomTopic() {
  const abc = "abcdefghijkmnpqrstuvwxyz23456789";
  const bytes = crypto.getRandomValues(new Uint8Array(18));
  return "alerts-" + Array.from(bytes, (b) => abc[b % abc.length]).join("");
}

async function copyText(text, input) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch (_) { if (input) { input.focus(); input.select(); } toast("Press Ctrl+C to copy"); }
}

/** Ask before going live. If no phone alerts are set up yet, offer to set up ntfy in the same dialog. */
async function confirmLive() {
  let status = app.status;
  try { status = await GET("/status"); } catch (_) { /* fall back to cached */ }
  const hasPhone = ((status && status.channels) || []).some((c) => c !== "console");
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    const box = h("input", { type: "checkbox", id: "own-rules" });
    const go = h("button", { class: "primary", type: "button", disabled: true }, "Turn on alerts");
    box.addEventListener("change", () => { go.disabled = !box.checked; });

    let setup = null;
    if (!hasPhone) {
      const topic = randomTopic();
      const topicInput = h("input", { value: topic, readonly: true, class: "wide", "aria-label": "Your ntfy topic" });
      const result = h("p", { class: "small", "aria-live": "polite" });
      const testBtn = h("button", { type: "button" }, "Save and send a test");
      testBtn.addEventListener("click", async () => {
        testBtn.disabled = true;
        result.className = "small muted";
        result.textContent = "Sending…";
        try {
          const st = await GET("/settings");
          st.notifiers.ntfy = { ...st.notifiers.ntfy, enabled: true, topic, server: st.notifiers.ntfy.server || "https://ntfy.sh" };
          await PUT("/settings", st);
          const r = await POST("/test-alert");
          if (r.errors.length) { result.className = "small loss"; result.textContent = "Couldn't send: " + r.errors.join("; "); }
          else { result.className = "small gain"; result.textContent = "Test sent. It should appear on your phone within a few seconds."; refreshStatus(); }
        } catch (e) { result.className = "small loss"; result.textContent = e.message; }
        finally { testBtn.disabled = false; }
      });
      setup = h("div", { class: "setup" },
        h("h3", {}, "Get the alerts on your phone"),
        h("ol", { class: "steps" },
          h("li", {}, "Install the free ntfy app from the App Store or Google Play."),
          h("li", {}, "In the app, tap + and subscribe to this topic:",
            h("div", { class: "inline copy-row" }, topicInput,
              h("button", { type: "button", onclick: () => copyText(topic, topicInput) }, "Copy"))),
          h("li", {}, "Come back here and send a test.", h("div", { class: "inline" }, testBtn), result)),
        h("p", { class: "small muted" }, "Keep the topic private: anyone who knows it can read your alerts. Prefer Discord? Set it up in ",
          h("a", { href: "#/settings", onclick: () => { finish(false); dlg.close(); } }, "Settings"), " instead."));
    }

    const dlg = h("dialog", { "aria-labelledby": "live-title", class: setup ? "wide-dialog" : null },
      h("h2", { id: "live-title" }, "Turn on alerts?"),
      h("p", {}, "From now on, every new candle is checked against these rules. When they're met you get an alert and a practice (paper) trade is recorded. Nothing is traded for you."),
      setup,
      h("label", { class: "check confirm", for: "own-rules" }, box,
        "I chose these rules and settings myself, and I understand alerts aren't financial advice."),
      h("div", { class: "actions" },
        h("button", { type: "button", onclick: () => { finish(false); dlg.close(); } }, "Not now"), go));
    go.addEventListener("click", () => { finish(true); dlg.close(); });
    dlg.addEventListener("close", () => { dlg.remove(); finish(false); });
    document.body.appendChild(dlg);
    dlg.showModal();
  });
}

function marketSection(data, changed) {
  const m = data.market;
  const sec = h("section", { class: "section" }, h("h2", {}, "Market"));
  const body = h("div", { class: "fields" });
  sec.appendChild(body);
  function presetKey() {
    if (!m.provider && !m.symbol) return "";
    if (m.provider === "csv") return "csv";
    if (m.provider === "oanda") return "oanda";
    if (m.provider === "dukascopy") return "dukascopy";
    if (m.provider === "auto") return "auto:" + m.symbol;
    const p = app.meta.presets.find((x) => x.symbol === m.symbol);
    return p ? "preset:" + p.symbol : "other";
  }
  const sources = limits().sources || ["dukascopy", "yahoo", "oanda"];
  function render() {
    clear(body);
    const sel = h("select", { class: "wide", "aria-label": "Market", onchange: (e) => {
      const v = e.target.value;
      if (v === "csv") { m.provider = "csv"; m.symbol = ""; m.label = m.label || "My data"; }
      else if (v === "other") { m.provider = "yahoo"; m.symbol = ""; }
      else if (v === "oanda") { m.provider = "oanda"; m.symbol = ""; }
      else if (v === "dukascopy") { m.provider = "dukascopy"; m.symbol = ""; }
      else if (v.startsWith("auto:")) {
        const mk = marketByKey(v.slice(5));
        m.provider = "auto"; m.symbol = mk.key; m.label = mk.label;
      }
      else {
        const p = app.meta.presets.find((x) => "preset:" + x.symbol === v);
        m.provider = "yahoo"; m.symbol = p.symbol; m.label = p.label;
      }
      changed(); render();
    } },
    h("option", { value: "", disabled: true }, "Choose a market…"),
    h("optgroup", { label: "Markets (prices from your Settings)" }, app.recipes.markets.map((mk) => h("option", { value: "auto:" + mk.key, title: mk.note }, mk.label))),
    sources.includes("yahoo") ? h("optgroup", { label: "Yahoo Finance symbols" }, app.meta.presets.map((p) => h("option", { value: "preset:" + p.symbol, title: p.note }, `${p.label} (${p.symbol})`))) : null,
    h("optgroup", { label: "Other" },
      sources.includes("yahoo") ? h("option", { value: "other" }, "Another Yahoo symbol") : null,
      sources.includes("dukascopy") ? h("option", { value: "dukascopy" }, "A Dukascopy instrument") : null,
      sources.includes("oanda") ? h("option", { value: "oanda" }, "An OANDA instrument") : null,
      h("option", { value: "csv" }, "My own CSV file")));
    sel.value = presetKey();
    sel.classList.toggle("to-fill", sel.value === "");
    const key = presetKey();
    const preset = app.meta.presets.find((x) => "preset:" + x.symbol === key);
    const builtIn = key.startsWith("auto:") ? marketByKey(m.symbol) : null;
    append(body, [h("div", { class: "field", style: null }, h("label", {}, "Market"), sel,
      preset ? h("span", { class: "small muted" }, preset.note) : null,
      builtIn ? h("span", { class: "small muted" }, `${builtIn.note}. Prices come from the source chosen in Settings.`) : null)]);
    if (key === "csv") {
      append(body, [csvFilePicker(m, () => { changed(); }),
        h("div", { class: "field" }, h("label", {}, "Name in alerts"),
          h("input", { value: m.label || "", oninput: (e) => { m.label = e.target.value; changed(); } })),
        h("div", { class: "field" }, h("label", {}, "Time zone of the timestamps"),
          tzInput(m.tz || "UTC", (v) => { m.tz = v; changed(); }))]);
    }
    if (key === "other" || key === "oanda" || key === "dukascopy") {
      const labels = { other: "Yahoo symbol", oanda: "OANDA instrument", dukascopy: "Dukascopy instrument" };
      const holders = { csv: "data/prices.csv", other: "e.g. ^N225", oanda: "e.g. SPX500_USD", dukascopy: "e.g. INSTRUMENT_IDX_ASIA_E_N225JAP" };
      append(body, [
        h("div", { class: "field" }, h("label", {}, labels[key]),
          h("input", { value: m.symbol || "", placeholder: holders[key],
            oninput: (e) => { m.symbol = e.target.value.trim(); changed(); } })),
        h("div", { class: "field" }, h("label", {}, "Name in alerts"),
          h("input", { value: m.label || "", oninput: (e) => { m.label = e.target.value; changed(); } })),
      ]);
    }
    const TF = { "5m": "5 minutes", "15m": "15 minutes", "30m": "30 minutes", "1h": "1 hour", "2h": "2 hours", "4h": "4 hours", "1d": "1 day" };
    const tfs = app.meta.timeframes.slice();
    if (m.timeframe && !tfs.includes(m.timeframe)) tfs.push(m.timeframe);
    const tf = choiceSelect(tfs.map((t) => [t, TF[t] || t]), m.timeframe, (v) => { m.timeframe = v; changed(); }, { label: "Candle size" });
    const dir = h("div", { class: "segmented", role: "group", "aria-label": "Direction" });
    const renderDir = () => {
      clear(dir);
      dir.classList.toggle("to-fill", !data.direction);
      for (const d of ["long", "short"]) dir.appendChild(h("button", { type: "button", "aria-pressed": String(data.direction === d),
        onclick: () => { data.direction = d; renderDir(); changed(); } }, d === "long" ? "Long (buy)" : "Short (sell)"));
    };
    renderDir();
    append(body, [
      h("div", { class: "field" }, h("label", {}, "Candle size"), tf),
      h("div", { class: "field" }, h("label", {}, "Direction"), dir),
      h("div", { class: "field" }, h("label", {}, "History to load (days)"),
        numberInput(m.lookback_days, (n) => { m.lookback_days = n; changed(); }, { step: "1", min: "1", integer: true, allowEmpty: true })),
      (() => {
        const al = h("select", { onchange: (e) => { m.candle_alignment = e.target.value; changed(); } },
          h("option", { value: "new_york" }, "5pm New York (most brokers)"), h("option", { value: "utc" }, "Midnight UTC"));
        al.value = m.candle_alignment || "new_york";
        return h("div", { class: "field" }, h("label", {}, "4-hour and daily candles start at"), al);
      })(),
    ]);
  }
  render();
  return sec;
}

/** Choose one of your uploaded CSV files (or, self-hosted, type a path inside the data folder). */
function csvFilePicker(m, changed) {
  const field = h("div", { class: "field" }, h("label", {}, "File"));
  const sel = h("select", { class: "wide", "aria-label": "CSV file" });
  const typed = limits().csv_paths !== false;
  async function fill(pick) {
    let files = [];
    try { files = (await GET("/uploads")).files; } catch (e) { toast(e.message, true); }
    clear(sel);
    sel.appendChild(h("option", { value: "", disabled: true }, files.length ? "Choose a file…" : "Upload a file first"));
    for (const f of files) sel.appendChild(h("option", { value: f.symbol }, f.name));
    if (pick) { m.symbol = pick; changed(); }
    const known = files.some((f) => f.symbol === m.symbol);
    if (m.symbol && !known) sel.appendChild(h("option", { value: m.symbol }, m.symbol + (typed ? "" : " (missing)")));
    sel.value = m.symbol || "";
    sel.classList.toggle("to-fill", !sel.value);
  }
  sel.addEventListener("change", () => { m.symbol = sel.value; sel.classList.toggle("to-fill", !sel.value); changed(); });
  const upload = csvUploadButton((r) => fill(null).then(async () => {
    const f = (await GET("/uploads")).files.find((x) => x.name === r.name);
    if (f) { m.symbol = f.symbol; sel.value = f.symbol; changed(); }
  }), "Upload…");
  append(field, [h("div", { class: "inline" }, sel, upload),
    typed ? h("details", { class: "small" }, h("summary", {}, "Or type a path inside the data folder"),
      h("input", { value: m.symbol || "", placeholder: "data/prices.csv", oninput: (e) => { m.symbol = e.target.value.trim(); changed(); } })) : null]);
  fill(null);
  return field;
}

const STOP_KINDS = { atr: "ATR multiple", percent: "Percent", points: "Points", level: "A price from a rule" };
const TARGET_KINDS = { none: "No target", r_multiple: "Multiple of the risk (R)", atr: "ATR multiple", percent: "Percent", points: "Points", level: "A price from a rule" };

function levelEditor(get, set, kinds, changed, label) {
  const wrap = h("div", { class: "inline" });
  function kindOf(spec) {
    if (spec === undefined) return "";
    if (spec === null) return "none";
    return Object.keys(kinds).find((k) => k in spec) || "";
  }
  function render() {
    clear(wrap);
    const spec = get();
    const kind = kindOf(spec);
    const sel = choiceSelect(Object.entries(kinds), kind, (k) => {
      set(k === "none" ? null : k === "level" ? { level: null } : k === "atr" ? { atr: null, atr_period: null } : { [k]: null });
      changed(); render();
    }, { label });
    wrap.appendChild(sel);
    if (!kind || kind === "none") return;
    if (kind === "level") {
      wrap.appendChild(operandEditor(() => get().level, (v) => { get().level = v; }, changed, "Price level"));
      return;
    }
    wrap.appendChild(numberInput(spec[kind], (n) => { get()[kind] = n; changed(); }, { step: "any", min: "0", cls: "narrow", label: kinds[kind] }));
    if (kind === "atr") wrap.appendChild(h("span", { class: "param" }, "ATR bars",
      numberInput(spec.atr_period, (n) => { get().atr_period = n; changed(); }, { step: "1", min: "1", integer: true, label: "ATR bars" })));
  }
  render();
  return wrap;
}

function exitSection(data, changed) {
  const ex = data.exit;
  const whenBox = h("div");
  function renderWhen() {
    clear(whenBox);
    if (ex.when) whenBox.appendChild(groupEditor(() => ex.when, (v) => { ex.when = v; }, changed));
  }
  const toggle = h("input", { type: "checkbox", id: "exit-when", checked: !!ex.when, onchange: (e) => {
    if (e.target.checked) ex.when = { all: [newComparison()] };
    else delete ex.when;
    renderWhen(); changed();
  } });
  renderWhen();
  return h("section", { class: "section" }, h("h2", {}, "Exit"),
    h("p", { class: "section-note" }, "If the stop and target are both hit in the same candle, the backtest counts it as a stop."),
    h("div", { class: "fields" },
      h("div", { class: "field full" }, h("label", {}, "Stop loss"),
        levelEditor(() => ex.stop, (v) => { ex.stop = v; }, STOP_KINDS, changed, "Stop loss")),
      h("div", { class: "field full" }, h("label", {}, "Take profit"),
        levelEditor(() => ex.target, (v) => { ex.target = v; }, TARGET_KINDS, changed, "Take profit")),
      h("div", { class: "field" }, h("label", {}, "Close after this many candles"),
        numberInput(ex.max_bars, (n) => { ex.max_bars = n; changed(); }, { step: "1", min: "1", integer: true, allowEmpty: true }))),
    h("div", { class: "stack", style: null }, h("label", { class: "check", for: "exit-when" }, toggle, "Also close the trade when a rule is true"), whenBox));
}

const RISK_HELP = {
  account_size: "The practice balance that backtests and forward tests start from. It doesn't have to match a real account, but using roughly what you'd trade with keeps sizes and costs realistic.",
  currency: "The currency of the balance and of all profit and loss figures. Usually your broker account's currency.",
  risk_per_trade_pct: "How much of the balance you'd lose if a trade hits its stop. The trade size is worked out from this. Many traders keep it small, often somewhere around 0.5% to 2%, because losing runs happen to every strategy.",
  point_value: "How much money 1 unit of size makes or loses when the price moves by 1. Your broker's contract details list it (for example £1 per point at size 1). Leave it blank for the built-in markets and their usual value is used.",
  size_step: "The smallest step your broker lets trade sizes go up in, such as 0.01 or 0.1. Sizes are rounded down to it. Blank means 0.01.",
  min_size: "The smallest trade your broker accepts. A signal that would need less than this is skipped rather than traded bigger. Blank means 0.01.",
  max_size: "An upper limit on trade size, if your broker has one or you want one. Blank means no limit.",
  spread: "The gap between the buy and sell price, in price points, which you pay on every trade. It's only used when the price data doesn't include it (Dukascopy and OANDA do). Your broker's market info page shows it; it's usually wider overnight and around news.",
  funding_pct: "The yearly interest your broker charges for holding a CFD past its daily cut-off (around 10pm UK time). Brokers publish it as a daily or yearly rate: enter the yearly percentage. Blank uses the built-in market's typical rate.",
  priority: "How phone alerts arrive in ntfy. High and urgent can sound even when the phone is on Do Not Disturb (depending on your phone's settings).",
  compound: "On: trade sizes grow and shrink with the balance, like a real account. Off: every trade is sized from the starting balance, which makes results from different periods easier to compare.",
  alerts: "Whether a phone alert is sent when this strategy's rules are met. Forward tests still record trades either way.",
  use_data_spread: "Dukascopy and OANDA prices include each candle's real spread, which is more accurate than one fixed number. Leave this on unless you want to test a specific spread.",
};

/** A small (i) that explains a setting on hover or focus. */
function infoTip(text) {
  if (!text) return null;
  const el = h("span", { class: "info", tabindex: "0", role: "button", "aria-label": "More about this: " + text }, "i");
  el.dataset.tip = text;
  el.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); showTip(el); });
  return el;
}
let tipEl = null;
function showTip(target) {
  hideTip();
  tipEl = h("div", { class: "floating-tip", role: "tooltip" }, target.dataset.tip);
  document.body.appendChild(tipEl);
  const r = target.getBoundingClientRect();
  const w = Math.min(320, window.innerWidth - 24);
  tipEl.style.width = w + "px";
  const left = Math.max(12, Math.min(window.innerWidth - w - 12, r.left + r.width / 2 - w / 2));
  const below = r.bottom + 8 + tipEl.offsetHeight < window.innerHeight;
  tipEl.style.left = left + "px";
  tipEl.style.top = (below ? r.bottom + 8 : r.top - 8 - tipEl.offsetHeight) + "px";
}
function hideTip() { if (tipEl) { tipEl.remove(); tipEl = null; } }
document.addEventListener("pointerover", (e) => { const t = e.target.closest && e.target.closest(".info"); if (t) showTip(t); });
document.addEventListener("pointerout", (e) => { if (e.target.closest && e.target.closest(".info")) hideTip(); });
document.addEventListener("focusin", (e) => { if (e.target.classList && e.target.classList.contains("info")) showTip(e.target); });
document.addEventListener("focusout", (e) => { if (e.target.classList && e.target.classList.contains("info")) hideTip(); });
window.addEventListener("scroll", hideTip, { passive: true });

function riskSection(data, changed) {
  const r = data.risk;
  const c = data.costs;
  const a = data.alerts;
  const f = (label, key, opts, obj = r, note = null) => h("div", { class: "field" }, h("label", {}, label, infoTip(RISK_HELP[key])),
    numberInput(obj[key], (n) => { obj[key] = n; changed(); }, { label, ...opts }),
    note ? h("span", { class: "small muted" }, note) : null);
  const cur = choiceSelect(app.meta.currencies.map((x) => [x, x]), r.currency, (v) => { r.currency = v; changed(); }, { label: "Currency" });
  const prio = h("select", { onchange: (e) => { a.priority = e.target.value; changed(); } },
    app.meta.priorities.map((x) => h("option", { value: x }, x[0].toUpperCase() + x.slice(1))));
  prio.value = a.priority || "default";
  return h("section", { class: "section" }, h("h2", {}, "Risk and alerts"),
    h("p", { class: "section-note" }, "Trade size is worked out so that hitting the stop loses the percentage you set. Check point value, size step and spread with your broker."),
    h("div", { class: "fields" },
      f("Starting balance", "account_size", { step: "any", min: "1" }),
      h("div", { class: "field" }, h("label", {}, "Currency", infoTip(RISK_HELP.currency)), cur),
      f("Risk per trade (%)", "risk_per_trade_pct", { step: "0.05", min: "0.01" }),
      f("Value per point", "point_value", { step: "any", min: "0", allowEmpty: true }, r, "Per 1 unit of size. Blank: the built-in market's own value, otherwise 1"),
      f("Size step", "size_step", { step: "any", min: "0", allowEmpty: true }, r, "Blank: 0.01"),
      f("Minimum size", "min_size", { step: "any", min: "0", allowEmpty: true }, r, "Blank: 0.01"),
      f("Maximum size", "max_size", { step: "any", min: "0", allowEmpty: true }, r, "Leave blank for no limit"),
      f("Spread (points)", "spread", { step: "any", min: "0", allowEmpty: true }, c, "Used when the price data has no bid/ask. Blank: the built-in market's typical spread, otherwise none"),
      f("Overnight funding (% a year)", "funding_pct", { step: "0.1", min: "0", allowEmpty: true }, c, "Charged for each night a trade is open. Blank: the built-in market's typical rate, otherwise none"),
      h("div", { class: "field" }, h("label", {}, "Alert priority", infoTip(RISK_HELP.priority)), prio)),
    h("div", { class: "inline", style: null },
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: r.compound !== false,
        onchange: (e) => { r.compound = e.target.checked; changed(); } }), "Size trades from the current balance", infoTip(RISK_HELP.compound)),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: a.enabled !== false,
        onchange: (e) => { a.enabled = e.target.checked; changed(); } }), "Send alerts for this strategy", infoTip(RISK_HELP.alerts)),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: c.use_data_spread !== false,
        onchange: (e) => { c.use_data_spread = e.target.checked; changed(); } }), "Use the real spread when the price data has it", infoTip(RISK_HELP.use_data_spread))));
}
