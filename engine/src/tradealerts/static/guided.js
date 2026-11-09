"use strict";

/* Trading Alerts web interface: Guided setup for beginners and the simple editor for guided strategies.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- guided setup (beginners)

const recipeById = (id) => app.recipes.recipes.find((r) => r.id === id);
const marketByKey = (k) => app.recipes.markets.find((m) => m.key === k);
const RISK_CHOICES = [0.25, 0.5, 1, 2, 3];

function scrollIntoViewSoft(el) {
  const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
}

function marketPicker(selectedKey, onPick) {
  const wrap = h("div", { class: "picker" });
  const groups = [...new Set(app.recipes.markets.map((m) => m.group))];
  for (const g of groups) {
    append(wrap, [h("h3", { class: "picker-group" }, g), h("div", { class: "choice-grid" },
      app.recipes.markets.filter((m) => m.group === g).map((m) => h("button", {
        type: "button", class: "choice", "aria-pressed": String(m.key === selectedKey),
        onclick: () => onPick(m.key),
      }, h("span", { class: "choice-name" }, m.label), h("span", { class: "choice-note" }, m.note))))]);
  }
  return wrap;
}

function ideaPicker(selectedId, onPick) {
  return h("div", { class: "ideas" }, app.recipes.recipes.map((r) => h("button", {
    type: "button", class: "choice idea", "aria-pressed": String(r.id === selectedId), onclick: () => onPick(r.id),
  }, h("span", { class: "choice-name" }, r.name), h("span", { class: "choice-note" }, r.summary),
  r.id === selectedId ? h("span", { class: "idea-how" }, r.how) : null)));
}

/** Labels of the settings still blank for an idea, in the order they appear on screen. */
function missingSettings(recipe, st) {
  const out = [];
  if (!st.direction) out.push("Buy or sell");
  if (!st.timeframe) out.push("How often to check");
  for (const k of recipe.knobs) {
    if (k.kind === "toggle" || k.optional) continue;
    if (isBlank(st[k.key])) out.push(k.label);
  }
  if (isBlank(st.balance)) out.push("Practice balance");
  if (!st.currency) out.push("Currency");
  if (isBlank(st.risk_pct)) out.push("Risk on each trade");
  return out;
}

/** The handful of plain-language settings for one idea. Mutates `st` and calls onChange. Everything starts blank. */
function settingsForm(recipe, st, onChange) {
  const row = (label, control, help, unit) => h("div", { class: "knob" },
    h("div", { class: "knob-label" }, label, help ? h("span", { class: "knob-help" }, help) : null),
    h("div", { class: "knob-control" }, control, unit ? h("span", { class: "word" }, unit) : null));
  const wrap = h("div", { class: "knobs" });

  const dir = h("div", { class: "segmented", role: "group", "aria-label": "Buy or sell" });
  const renderDir = () => {
    clear(dir);
    dir.classList.toggle("to-fill", !st.direction);
    for (const [v, t] of [["long", "Buy"], ["short", "Sell"]]) {
      dir.appendChild(h("button", { type: "button", "aria-pressed": String(st.direction === v),
        onclick: () => { st.direction = v; renderDir(); onChange(); } }, t));
    }
  };
  renderDir();
  wrap.appendChild(row("Buy or sell", dir, "Buy if you expect prices to rise. Sell (going short) makes money when prices fall."));

  const tf = choiceSelect(recipe.timeframes.map((t) => [t.value, t.label]), st.timeframe,
    (v) => { st.timeframe = v; onChange(); }, { label: "How often to check" });
  wrap.appendChild(row("How often to check", tf, "Each check looks at the latest finished candle. Checking less often means fewer, slower trades."));

  for (const k of recipe.knobs) {
    const val = st[k.key];
    let control;
    if (k.kind === "number") {
      control = numberInput(val, (n) => { st[k.key] = n; onChange(); },
        { label: k.label, step: String(k.step || "any"), min: String(k.min), integer: !!k.integer, allowEmpty: !!k.optional,
          placeholder: k.optional ? "Optional" : `${k.min} to ${k.max}` });
    } else if (k.kind === "toggle") {
      wrap.appendChild(h("div", { class: "knob" }, h("label", { class: "check" },
        h("input", { type: "checkbox", checked: !!val, onchange: (e) => { st[k.key] = e.target.checked; onChange(); } }), k.label)));
      continue;
    } else if (k.kind === "select") {
      control = choiceSelect(k.options.map((o) => [o.value, o.label]), val, (v) => { st[k.key] = v; onChange(); }, { label: k.label });
    } else if (k.kind === "time") {
      control = timeInput(val, (v) => { st[k.key] = v; onChange(); }, k.label);
    }
    wrap.appendChild(row(k.label, control, k.help, k.unit));
  }

  const bal = numberInput(st.balance, (n) => { st.balance = n; renderRisk(); onChange(); },
    { label: "Practice balance", step: "any", min: "10", placeholder: "Amount" });
  const cur = choiceSelect([["GBP", "GBP"], ["USD", "USD"], ["EUR", "EUR"]], st.currency,
    (v) => { st.currency = v; renderRisk(); onChange(); }, { label: "Currency", placeholder: "Currency" });
  wrap.appendChild(row("Practice balance", h("span", { class: "inline" }, bal, cur),
    "Used for backtests and paper trades. It doesn't need to match a real account."));

  const riskBox = h("span", { class: "inline" });
  function renderRisk() {
    clear(riskBox);
    const sym = CURRENCY[st.currency] || "";
    const opts = RISK_CHOICES.includes(st.risk_pct) || isBlank(st.risk_pct) ? RISK_CHOICES : [...RISK_CHOICES, st.risk_pct].sort((a, b) => a - b);
    const label = (p) => (isBlank(st.balance) || !sym ? `${p}%` : `${p}% (${sym}${nf(2).format(st.balance * p / 100)})`);
    riskBox.appendChild(choiceSelect(opts.map((p) => [String(p), label(p)]), isBlank(st.risk_pct) ? "" : String(st.risk_pct),
      (v) => { st.risk_pct = Number(v); onChange(); }, { label: "Risk on each trade" }));
  }
  renderRisk();
  wrap.appendChild(row("Risk on each trade", riskBox, "What you'd lose if a trade hits its stop. Trade size is worked out from this."));
  return wrap;
}

/** Backtest results written as sentences, with the detail tucked away. */
function plainResults(res) {
  const m = res.metrics;
  const sym = res.currency;
  const box = h("div", { class: "plain-results" });
  const n = m.trades;
  const won = n ? Math.round((m.win_rate_pct / 100) * n) : 0;
  const ret = m.total_return_pct;
  append(box, [
    h("p", { class: "result-lede" }, `From ${day(m.start)} to ${day(m.end)}, these rules would have made `,
      h("strong", {}, `${n} ${n === 1 ? "trade" : "trades"}`),
      n ? `: ${won} won and ${n - won} lost.` : "."),
    h("p", { class: "result-lede" }, `${money(m.start_equity, sym)} would have become `,
      h("strong", { class: ret >= 0 ? "gain" : "loss" }, `${money(m.end_equity, sym)} (${signed(ret, "%")})`),
      `. Simply holding the market over the same time: ${signed(m.buy_and_hold_pct, "%")}.`),
    n ? h("p", { class: "muted" }, `That's after ${money(m.total_spread_cost || 0, sym)} in spreads` +
      (m.total_funding ? ` and ${money(m.total_funding, sym)} in overnight funding` : "") + ", the costs a broker would charge.") : null,
    h("p", { class: "muted" }, n
      ? `At its lowest point the balance was ${num(Math.abs(m.max_drawdown_pct), 1)}% below its previous high. The longest run of losing trades was ${m.longest_losing_streak}.`
      : "The rules never triggered on this data. Try a smaller move, a different market or checking more often."),
    h("ul", { class: "warnings" }, res.warnings.map((w) => h("li", {}, w))),
    lineChart(res.equity, { title: "Practice balance over time", fmt: (v) => money(v, sym), baseline: m.start_equity }),
  ]);
  if (res.chart && res.chart.candles.length) append(box, [h("details", { class: "chart price-details" },
    h("summary", {}, "See the trades on the price chart"), candleChart(res.chart, { height: 300 }))]);
  const more = renderBacktest({ ...res, chart: null });
  more.querySelector(".headline")?.remove();
  more.querySelector(".warnings")?.remove();
  more.querySelector(".chart")?.remove();
  append(box, [h("details", { class: "chart" }, h("summary", {}, "All the numbers"), more)]);
  return box;
}

async function buildFromRecipe(recipeId, marketKey, settings) {
  return POST("/recipes/build", { recipe: recipeId, market: marketKey, settings });
}

// ----- create page: 1 market, 2 idea, 3 settings

async function viewCreate(preset) {
  setNav("strategies");
  const st = { market: preset && marketByKey(preset) ? preset : null, recipe: null, settings: {}, built: null, name: "", nameTouched: false };
  let dirty = false;
  app.isDirty = () => dirty;
  app.guard = () => (dirty ? "Leave without saving this strategy?" : null);

  const step1 = h("section", { class: "step" });
  const step2 = h("section", { class: "step" });
  const step3 = h("section", { class: "step" });
  const page = h("div", { class: "create" },
    h("a", { class: "back", href: "#/strategies" }, "All strategies"),
    h("h1", {}, "Create a strategy"),
    h("p", { class: "lede" }, "Three choices, then you can test it on past prices. You can change anything later."),
    step1, step2, step3,
    h("p", { class: "muted small advanced-link" }, "Know exactly what rules you want? ", h("a", { href: "#/new" }, "Build from scratch instead"), "."));

  function render1() {
    clear(step1);
    append(step1, [h("h2", {}, "1. Choose a market"), marketPicker(st.market, (k) => {
      const first = !st.market;
      st.market = k; dirty = true; render1(); render2();
      if (st.recipe) rebuild(); else if (first) scrollIntoViewSoft(step2);
    })]);
  }
  function render2() {
    clear(step2);
    step2.classList.toggle("locked", !st.market);
    append(step2, [h("h2", {}, "2. Choose an idea")]);
    if (!st.market) { step2.appendChild(h("p", { class: "muted" }, "Choose a market first.")); return; }
    step2.appendChild(ideaPicker(st.recipe, (id) => {
      const changed = st.recipe !== id;
      st.recipe = id;
      if (changed) st.settings = { direction: st.settings.direction, balance: st.settings.balance, risk_pct: st.settings.risk_pct, currency: st.settings.currency };
      render2(); render3(); rebuild();
      if (changed) scrollIntoViewSoft(step3);
    }));
  }

  const sentence = h("div", { class: "preview" });
  const problem = h("p", { class: "validity bad", "aria-live": "polite" });
  const nameInput = h("input", { class: "wide", "aria-label": "Strategy name",
    oninput: (e) => { st.name = e.target.value; st.nameTouched = true; } });
  let results = null;
  let buildTimer, buildSeq = 0;

  function render3() {
    clear(step3);
    step3.classList.toggle("locked", !st.recipe);
    append(step3, [h("h2", {}, "3. Set it up")]);
    if (!st.recipe) { step3.appendChild(h("p", { class: "muted" }, "Choose an idea first.")); return; }
    const recipe = recipeById(st.recipe);
    const formBox = h("section", { class: "section" });
    const renderForm = () => clear(formBox).appendChild(settingsForm(recipe, st.settings, () => { dirty = true; rebuild(); }));
    renderForm();
    results = analysisPanel({
      getData: async () => {
        const miss = missingSettings(recipe, st.settings);
        if (miss.length) { toast("Still to fill in: " + miss.join(", "), true); return null; }
        if (!st.built) await rebuildNow();
        if (!st.built) toast(problem.textContent || "Fix the highlighted setting first", true);
        return st.built;
      },
      render: plainResults, runLabel: "Test on past prices",
      intro: "Test the rules on the market's price history before deciding whether to turn on alerts.",
      settings: () => knobSettings(recipe, st.settings),
      apply: (setting, value) => { st.settings[setting.key] = value; dirty = true; renderForm(); rebuild(); },
    });
    const saveDraft = h("button", { type: "button", class: "quiet", onclick: () => save("draft") }, "Save as draft");
    const saveTest = h("button", { class: "primary", type: "button", onclick: () => save("test") }, "Save and start a forward test");
    const saveLive = h("button", { type: "button", onclick: () => save("alerts") }, "Start with phone alerts");
    append(step3, [
      h("div", { class: "editor" },
        h("div", { class: "panel" },
          h("section", { class: "section" }, h("h3", {}, "In plain English"), sentence, problem),
          formBox,
          h("section", { class: "section" }, h("div", { class: "field" }, h("label", {}, "Name"), nameInput),
            h("p", { class: "small muted save-note" }, "A forward test follows the strategy on live prices from now on, so you can see how it does without risking money. Phone alerts tell you each time the rules are met."),
            h("div", { class: "actions save-row" }, saveTest, saveLive, saveDraft))),
        h("div", { class: "side" }, results)),
    ]);
  }

  async function rebuildNow() {
    const seq = ++buildSeq;
    try {
      const built = await buildFromRecipe(st.recipe, st.market, st.settings);
      if (seq !== buildSeq) return;
      st.built = built;
      Object.assign(st.settings, built.recipe.settings);
      if (!st.nameTouched) { st.name = built.name; nameInput.value = built.name; }
      clear(sentence).appendChild(renderParts(describeStrategy(built)));
      problem.className = "validity";
      problem.textContent = "";
    } catch (e) {
      if (seq !== buildSeq) return;
      st.built = null;
      problem.className = "validity bad";
      problem.textContent = e.message;
    }
  }
  function rebuild() {
    if (results && results.markStale) results.markStale();
    clearTimeout(buildTimer);
    const recipe = recipeById(st.recipe);
    const miss = recipe ? missingSettings(recipe, st.settings) : [];
    if (miss.length) {
      buildSeq++;
      st.built = null;
      clear(sentence).appendChild(h("p", { class: "sentence placeholder-sentence" }, "Fill in the settings and the rules appear here in plain English."));
      problem.className = "validity todo";
      problem.textContent = "Still to fill in: " + miss.join(", ") + ".";
      return;
    }
    buildTimer = setTimeout(rebuildNow, 250);
  }

  async function save(mode) {
    const miss = missingSettings(recipeById(st.recipe), st.settings);
    if (miss.length) { toast("Still to fill in: " + miss.join(", "), true); return; }
    await rebuildNow();
    if (!st.built) { toast("Fix the highlighted setting first", true); return; }
    const live = mode === "alerts";
    if (live && !(await confirmLive())) return;
    const data = { ...st.built, name: st.name || st.built.name, template: !live,
      alerts: { enabled: true, priority: "high" } };
    if (live) data.enabled = true;
    try {
      const r = await POST("/strategies", { data });
      dirty = false; app.guard = null; app.isDirty = null;
      if (mode !== "draft") {
        await POST(`/strategies/${encodeURIComponent(r.id)}/forward-test/start`);
        toast(live ? "Saved. Forward test running with alerts on" : "Saved. Forward test running");
        refreshStatus();
        location.hash = "#/";
      } else {
        toast("Saved as a draft");
        location.hash = "#/s/" + r.id;
      }
    } catch (e) { toast("Not saved: " + e.message, true); }
  }

  render1(); render2(); render3();
  if (st.market) setTimeout(() => step2.scrollIntoView({ block: "start", behavior: "smooth" }), 50);
  clear($main).appendChild(page);
}

// ----- simple editor for strategies made with the guided setup

async function viewSimple(id, data, run) {
  setNav("strategies");
  const recipe = recipeById(data.recipe.id);
  if (!recipe) return viewEditor(id, null, data);
  let marketKey = data.recipe.market;
  const st = clone(data.recipe.settings);
  let dirty = false;
  app.isDirty = () => dirty;
  app.guard = () => (dirty ? "You have unsaved changes. Leave without saving?" : null);

  const sentence = h("div");
  const validity = h("p", { class: "validity", "aria-live": "polite" });
  const dirtyNote = h("span", { class: "dirty" });
  const markDirty = () => { dirty = true; dirtyNote.textContent = "Unsaved changes"; };
  const showSentence = () => clear(sentence).appendChild(renderParts(describeStrategy(data)));
  let results;
  let timer, seq = 0;

  async function rebuildNow() {
    const mySeq = ++seq;
    try {
      const built = await buildFromRecipe(recipe.id, marketKey, st);
      if (mySeq !== seq) return false;
      const sameMarket = data.recipe.market === marketKey;
      const keepRisk = {};
      if (sameMarket) for (const k of ["point_value", "size_step", "min_size", "max_size", "compound"]) if (k in (data.risk || {})) keepRisk[k] = data.risk[k];
      const next = { ...built, name: data.name, template: data.template, alerts: data.alerts || { enabled: true, priority: "high" } };
      if ("enabled" in data) next.enabled = data.enabled;
      if (data.description) next.description = data.description;
      next.risk = { ...built.risk, ...keepRisk };
      if (sameMarket && data.costs) next.costs = { ...built.costs, ...data.costs };
      data = next;
      Object.assign(st, built.recipe.settings);
      validity.className = "validity";
      validity.textContent = "";
      showSentence();
      return true;
    } catch (e) {
      if (mySeq !== seq) return false;
      validity.className = "validity " + (e.message.startsWith("Still to fill in") ? "todo" : "bad");
      validity.textContent = e.message;
      return false;
    }
  }
  function changed() {
    markDirty();
    if (results) results.markStale();
    clearTimeout(timer);
    timer = setTimeout(rebuildNow, 250);
  }

  async function save() {
    clearTimeout(timer);
    if (!(await rebuildNow())) { toast("Fix the highlighted setting first", true); return false; }
    try {
      const r = await withRunningChoice((onRunning) => PUT("/strategies/" + encodeURIComponent(id), { data, on_running: onRunning }));
      if (!r) return false;
      dirty = false; dirtyNote.textContent = "Saved"; savedToast(r); refreshStatus();
      return true;
    } catch (e) { toast("Not saved: " + e.message, true); return false; }
  }

  const title = h("input", { class: "title-input", value: data.name || "", "aria-label": "Strategy name",
    oninput: (e) => { data.name = e.target.value; markDirty(); } });
  const statusSeg = statusControls({ id, isNew: false, run, getData: () => data, isDirty: () => dirty, save });
  const marketSel = h("select", { "aria-label": "Market", onchange: (e) => { marketKey = e.target.value; changed(); } },
    [...new Set(app.recipes.markets.map((m) => m.group))].map((g) => h("optgroup", { label: g },
      app.recipes.markets.filter((m) => m.group === g).map((m) => h("option", { value: m.key }, m.label)))));
  marketSel.value = marketKey;

  const formBox = h("div");
  const renderForm = () => clear(formBox).appendChild(settingsForm(recipe, st, changed));
  renderForm();
  results = analysisPanel({
    getData: async () => {
      clearTimeout(timer);
      const miss = missingSettings(recipe, st);
      if (miss.length) { toast("Still to fill in: " + miss.join(", "), true); return null; }
      return (await rebuildNow()) ? data : null;
    },
    render: plainResults, runLabel: "Test on past prices",
    intro: "Test these settings on the market's price history. Unsaved changes are included.",
    settings: () => knobSettings(recipe, st),
    apply: (setting, value) => { st[setting.key] = value; renderForm(); changed(); },
  });

  const head = h("div", { class: "editor-head" },
    h("a", { class: "back", href: "#/strategies" }, "All strategies"),
    title,
    h("div", { class: "row" }, h("div", { class: "inline" }, statusSeg, dirtyNote),
      h("div", { class: "inline" },
        h("button", { type: "button", onclick: async () => {
          const copy = clone(data); copy.name = (copy.name || "Strategy") + " copy"; copy.template = true; delete copy.enabled;
          const r = await attempt(() => POST("/strategies", { data: copy })); if (!r) return;
          toast("Copy saved as a draft"); app.guard = null; app.isDirty = null;
          location.hash = "#/s/" + r.id;
        } }, "Duplicate"),
        historyLink(id),
        ...shareButtons(id, () => data),
        h("button", { class: "danger", type: "button", onclick: async () => {
          if (!(await ask(`Delete "${data.name}"?`, { ok: "Delete", danger: true }))) return;
          if ((await attempt(() => DEL("/strategies/" + encodeURIComponent(id)))) === undefined) return;
          app.guard = null; app.isDirty = null;
          toast("Strategy deleted"); location.hash = "#/strategies";
        } }, "Delete"),
        h("button", { class: "primary", type: "button", onclick: save }, "Save changes"))),
    sentence, validity);

  const left = h("div", { class: "panel" },
    h("section", { class: "section" }, h("h2", {}, "Market and idea"),
      h("div", { class: "knob" }, h("div", { class: "knob-label" }, "Market"), h("div", { class: "knob-control" }, marketSel)),
      h("div", { class: "knob" }, h("div", { class: "knob-label" }, "Idea", h("span", { class: "knob-help" }, recipe.how)),
        h("div", { class: "knob-control" }, h("strong", {}, recipe.name)))),
    h("section", { class: "section" }, h("h2", {}, "Settings"), formBox),
    h("section", { class: "section" }, h("p", { class: "small muted" },
      "Want to add your own conditions or change how exits work? ",
      h("a", { href: "#/s/" + id + "/advanced" }, "Edit the full rules"),
      ". This turns it into a custom strategy.")));

  clear($main);
  append($main, [head, h("div", { class: "editor" }, left, h("div", { class: "side" }, results))]);
  showSentence();
}
