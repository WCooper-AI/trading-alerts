"use strict";

/* Trading Alerts web interface: The trade journal.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- journal

function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
const marketName = (m) => (marketByKey(m) || {}).label || m;

async function journalDialog(entry, onSaved, prefill = null) {
  /* entry: an existing journal entry to edit. prefill: starting values for a new one
     (e.g. from a paper trade's "Log real trade" button), including paper_trade_id to link it. */
  const e = entry ? clone(entry) : { direction: null, ...(prefill || {}) };
  const given = entry || prefill;
  const builtIn = !given || !given.market || marketByKey(given.market);
  const marketSel = choiceSelect([...app.recipes.markets.map((m) => [m.key, m.label]), ["__other", "Something else…"]],
    given && given.market ? (builtIn ? given.market : "__other") : null, () => { syncOther(); loadPaper(); }, { label: "Market", cls: "wide" });
  const other = h("input", { class: "wide", placeholder: "Market name", value: builtIn ? "" : given.market, "aria-label": "Market name" });
  const syncOther = () => { other.hidden = marketSel.value !== "__other"; };
  syncOther();
  const dir = h("div", { class: "segmented", role: "group", "aria-label": "Buy or sell" });
  const renderDir = () => {
    clear(dir);
    dir.classList.toggle("to-fill", !e.direction);
    for (const [v, t] of [["long", "Buy"], ["short", "Sell"]]) dir.appendChild(h("button", { type: "button",
      "aria-pressed": String(e.direction === v), onclick: () => { e.direction = v; renderDir(); } }, t));
  };
  renderDir();
  const dt = (key) => h("input", { type: "datetime-local", value: toLocalInput(e[key]), "aria-label": key,
    oninput: (ev) => { e[key] = ev.target.value ? new Date(ev.target.value).toISOString() : null; } });
  const n = (key, label, opts = {}) => numberInput(e[key], (v) => { e[key] = v; }, { label, step: "any", min: "0", ...opts });
  const cur = choiceSelect([["GBP", "GBP"], ["USD", "USD"], ["EUR", "EUR"]], e.currency, (v) => { e.currency = v; }, { label: "Currency" });
  const paper = h("select", { class: "wide", "aria-label": "Forward-test trade" });
  async function loadPaper() {
    const key = marketSel.value && marketSel.value !== "__other" ? marketSel.value : "";
    clear(paper).appendChild(h("option", { value: "" }, "Not linked"));
    const want = e.paper_trade_id ? String(e.paper_trade_id) : "";
    try {
      const r = key ? await GET("/paper-trades?market=" + encodeURIComponent(key)) : { trades: [] };
      for (const t of r.trades) paper.appendChild(h("option", { value: String(t.id) },
        `${t.strategy_name}: ${t.direction === "short" ? "sell" : "buy"} ${when(t.entry_time)} at ${num(t.entry_price)}` +
        (t.exit_price !== null ? ` → ${num(t.exit_price)}` : "")));
      if (want && !r.trades.some((t) => String(t.id) === want)) {  // linked from a run page whose market isn't a built-in one
        paper.appendChild(h("option", { value: want }, `Forward-test trade #${want}`));
      }
      paper.value = want;
    } catch (_) { /* leave unlinked */ }
  }
  loadPaper();
  const notes = h("textarea", { "aria-label": "Notes", placeholder: "Why you took it, how it went…" }, e.notes || "");
  const tags = h("input", { class: "wide", "aria-label": "Tags", placeholder: "e.g. breakout, news, revenge", value: (e.tags || []).join(", ") });
  const setup = h("input", { class: "wide", "aria-label": "Setup", placeholder: "e.g. London open pullback", maxlength: 60, value: e.setup || "" });
  const plan = h("div", { class: "segmented", role: "group", "aria-label": "Followed the plan" });
  const renderPlan = () => {
    clear(plan);
    for (const [v, t] of [[true, "Yes"], [false, "No"], [null, "–"]]) plan.appendChild(h("button", { type: "button",
      "aria-pressed": String(e.followed_plan === v), onclick: () => { e.followed_plan = v; renderPlan(); } }, t));
  };
  renderPlan();
  const mood = choiceSelect(JOURNAL_MOODS.map((m) => [m, m[0].toUpperCase() + m.slice(1)]), e.mood || null, (v) => { e.mood = v; },
    { label: "Mood", placeholder: "Not noted" });
  const err = h("p", { class: "loss small", "aria-live": "polite" });
  const save = h("button", { class: "primary", type: "button" }, entry ? "Save changes" : "Add trade");
  const field = (label, control, tip) => h("div", { class: "field" }, h("label", {}, label, tip ? infoTip(tip) : null), control);
  const dlg = h("dialog", { class: "wide-dialog journal-dialog", "aria-labelledby": "jt" },
    h("h2", { id: "jt" }, entry ? "Edit trade" : "Add a real trade"),
    prefill && prefill.paper_trade_id ? h("p", { class: "small muted" }, "Filled in from the forward test's trade. Change the prices and times to what you actually got.") : null,
    h("div", { class: "fields" },
      field("Market", h("div", { class: "stack tight" }, marketSel, other)),
      field("Buy or sell", dir),
      field("Opened", dt("entry_time")), field("Entry price", n("entry_price", "Entry price")),
      field("Size", n("size", "Size"), "The size you traded, in the same units as your broker (e.g. £ per point)."),
      field("Value per point", n("point_value", "Value per point", { allowEmpty: true }), RISK_HELP.point_value),
      field("Closed", dt("exit_time")), field("Exit price", n("exit_price", "Exit price", { allowEmpty: true })),
      field("Fees and funding", n("fees", "Fees", { allowEmpty: true }), "Commission, overnight funding and any other charges for this trade, in your account currency. Blank means none."),
      field("Currency", cur),
      h("div", { class: "field full" }, h("label", {}, "Same trade in a forward test",
        infoTip("Link it to the paper trade your alert came from, and the journal shows the difference between your fills and the forward test's (slippage).")), paper),
      field("Setup", setup, "The pattern or plan this trade followed. Name it the same way each time and the journal shows how each setup does."),
      field("Tags", tags, "Comma-separated labels. Filter the journal by any of them."),
      field("Followed the plan?", plan, "Did you take the trade the way you planned it? Over time you'll see what breaking the plan costs."),
      field("Mood", mood),
      h("div", { class: "field full" }, h("label", {}, "Notes"), notes)),
    h("p", { class: "small muted" }, "Leave Closed and Exit price blank while the trade is still open."),
    err,
    h("div", { class: "actions" }, h("button", { type: "button", onclick: () => dlg.close() }, "Cancel"), save));
  save.addEventListener("click", async () => {
    const body = { ...e, market: marketSel.value === "__other" ? other.value.trim() : marketSel.value,
      paper_trade_id: paper.value ? Number(paper.value) : null, notes: notes.value, tags: tags.value, setup: setup.value,
      followed_plan: e.followed_plan === undefined ? null : e.followed_plan, mood: e.mood || null };
    try {
      if (entry) await PUT(`/journal/${entry.id}`, body); else await POST("/journal", body);
      dlg.close(); toast(entry ? "Trade updated" : "Trade added"); onSaved();
    } catch (ex) { err.textContent = ex.message; }
  });
  dlg.addEventListener("close", () => dlg.remove());
  document.body.appendChild(dlg);
  dlg.showModal();
}

const JOURNAL_MOODS = ["calm", "confident", "anxious", "bored", "frustrated", "rushed"];
const journalFilter = { tag: "", setup: "", mood: "", followed_plan: "" };

/** Running P/L after each closed trade, as a small inline SVG. */
function equityCurve(points, sym) {
  if (points.length < 2) return null;
  const W = 600, H = 120, pad = 6;
  const ys = points.map((p) => p[1]);
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys), span = hi - lo || 1;
  const x = (i) => pad + (i / (points.length - 1)) * (W - 2 * pad);
  const y = (v) => H - pad - ((v - lo) / span) * (H - 2 * pad);
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
  const last = ys[ys.length - 1];
  const svg = `<svg viewBox="0 0 ${W} ${H}" class="equity-curve" role="img" aria-label="Running profit and loss over ${points.length} closed trades">` +
    `<line x1="${pad}" x2="${W - pad}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}" class="zero"/>` +
    `<path d="${d}" fill="none" class="${last >= 0 ? "gain" : "loss"}"/></svg>`;
  const box = h("div", { class: "equity-wrap" });
  box.innerHTML = svg;
  return h("div", {}, box, h("p", { class: "small muted" }, `Running P/L after each closed trade: ${signedMoney(last, sym)} after ${points.length}.`));
}

async function viewJournal() {
  setNav("journal");
  const f = journalFilter;
  const q = Object.entries(f).filter(([, v]) => v).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  const res = await GET("/journal" + (q ? "?" + q : ""));
  const s = res.summary;
  const filtered = Object.values(f).some(Boolean);
  const sym = (e) => CURRENCY[e.currency] || "";
  const page = h("div", { class: "stack" });
  const add = h("button", { class: "primary", type: "button", onclick: () => journalDialog(null, viewJournal) }, "Add a trade");
  append(page, [h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "Journal"),
      h("p", { class: "muted dash-lede" }, "A record of the trades you placed yourself. Link one to a forward-test trade to see how your real fills compared with the alert's.")),
    h("div", { class: "actions" }, h("a", { class: "button", href: "/api/journal.csv", download: "trade-journal.csv" }, "Download CSV"), add))]);
  // filter chips: one per tag / setup / mood / plan value seen across the journal
  const chip = (key, value, label, n) => h("button", { type: "button", class: "chip" + (f[key] === value ? " on" : ""),
    "aria-pressed": String(f[key] === value), onclick: () => { f[key] = f[key] === value ? "" : value; viewJournal(); } }, `${label} (${n})`);
  const b = res.breakdowns;
  const chips = [
    ...b.tags.map((x) => chip("tag", x.value, "#" + x.value, x.trades)),
    ...b.setup.map((x) => chip("setup", x.value, x.value, x.trades)),
    ...b.mood.map((x) => chip("mood", x.value, x.value, x.trades)),
    ...b.followed_plan.map((x) => chip("followed_plan", x.value, x.value === "yes" ? "Followed plan" : "Broke plan", x.trades))];
  if (chips.length || filtered) append(page, [h("div", { class: "panel" }, h("section", { class: "section" },
    h("div", { class: "inline wrap chips", role: "group", "aria-label": "Filter the journal" }, chips,
      filtered ? h("button", { type: "button", class: "quiet", onclick: () => { for (const k in f) f[k] = ""; viewJournal(); } }, "Clear filter") : null)))]);
  if (!res.entries.length && filtered) {
    append(page, [h("div", { class: "panel" }, h("p", { class: "muted" }, "No trades match that filter."))]);
    clear($main).appendChild(page);
    return;
  }
  if (!res.entries.length) {
    append(page, [h("div", { class: "panel" }, h("div", { class: "empty" }, h("h2", {}, "No trades yet"),
      h("p", { class: "muted" }, "When you act on an alert, add the trade here. Over time you'll see whether your fills match the forward tests, and which strategies you actually follow.")))]);
    clear($main).appendChild(page);
    return;
  }
  const one = res.entries[0];
  const slip = (v) => (v === null || v === undefined ? "–" : `${v > 0 ? "worse by " : v < 0 ? "better by " : ""}${num(Math.abs(v), priceDigits(one.entry_price) > 2 ? 4 : 2)} pts`);
  append(page, [h("div", { class: "panel" }, h("section", { class: "section" }, h("div", { class: "headline" },
    h("div", {}, h("div", { class: "big" }, String(s.trades)), h("div", { class: "lbl" }, `trades, ${s.open} open`)),
    h("div", {}, h("div", { class: "big " + (s.pnl >= 0 ? "gain" : "loss") }, signedMoney(s.pnl, sym(one))), h("div", { class: "lbl" }, "closed trades")),
    s.closed ? h("div", {}, h("div", { class: "big" }, num((s.wins / s.closed) * 100, 0) + "%"), h("div", { class: "lbl" }, "won")) : null,
    s.linked ? h("div", {}, h("div", { class: "big " + (s.slippage_cost > 0 ? "loss" : "gain") }, money(Math.abs(s.slippage_cost), sym(one))),
      h("div", { class: "lbl" }, `${s.slippage_cost > 0 ? "lost" : "gained"} to slippage on ${s.linked} linked`)) : null),
  s.linked ? h("p", { class: "small muted" }, `Against the forward tests, your entries were on average ${slip(s.avg_entry_slippage)}` +
    (s.avg_exit_slippage !== null ? ` and your exits ${slip(s.avg_exit_slippage)}.` : ".")) : null,
  equityCurve(res.equity, sym(one)))),
  breakdownPanel(res.breakdowns, sym(one)),
  h("div", { class: "panel" }, h("div", { class: "table-wrap" }, h("table", { class: "trades journal" },
    h("thead", {}, h("tr", {}, ["Opened", "Market", "Side", "Entry", "Closed", "Exit", "Size"].map((x) => h("th", {}, x)),
      h("th", { class: "num" }, "P/L"), h("th", {}, "Slippage"), h("th", {}, "Notes"), h("th", {}))),
    h("tbody", {}, res.entries.map((e) => h("tr", {},
      h("td", {}, when(e.entry_time)), h("td", {}, marketName(e.market)), h("td", {}, e.direction === "short" ? "Sell" : "Buy"),
      h("td", {}, num(e.entry_price)), h("td", {}, e.exit_time ? when(e.exit_time) : h("span", { class: "pill testing" }, "Open")),
      h("td", {}, e.exit_price === null ? "–" : num(e.exit_price)), h("td", {}, short(e.size)),
      h("td", { class: "num " + (e.pnl === null ? "" : e.pnl >= 0 ? "gain" : "loss") }, e.pnl === null ? "–" : signedMoney(e.pnl, sym(e))),
      h("td", { class: "small" }, e.paper ? `in ${slip(e.entry_slippage)}` + (e.exit_slippage !== null ? `, out ${slip(e.exit_slippage)}` : "") : h("span", { class: "muted" }, "not linked")),
      h("td", { class: "small muted note-cell", title: e.notes || "" },
        e.setup ? h("span", { class: "tag" }, e.setup) : null, e.tags.map((t) => h("span", { class: "tag" }, "#" + t)),
        e.followed_plan === false ? h("span", { class: "tag loss" }, "off plan") : null, e.mood ? h("span", { class: "tag" }, e.mood) : null,
        e.notes || ""),
      h("td", { class: "num" }, h("span", { class: "inline nowrap" },
        h("button", { type: "button", class: "quiet", onclick: () => journalDialog(e, viewJournal) }, "Edit"),
        h("button", { type: "button", class: "quiet", onclick: (ev) => {
          const tr = ev.target.closest("tr");
          undoable("Trade deleted", (on) => { tr.hidden = on; }, async () => { await DEL(`/journal/${e.id}`); });
        } }, "Delete")))))))))]);
  clear($main).appendChild(page);
}


/** How each setup, tag, mood and plan-following did: trades, won, P/L. Hidden when nothing is tagged. */
function breakdownPanel(b, sym) {
  const groups = [["Setup", b.setup, (v) => v], ["Tag", b.tags, (v) => "#" + v], ["Mood", b.mood, (v) => v],
    ["Followed the plan", b.followed_plan, (v) => (v === "yes" ? "Yes" : "No")]].filter(([, rows]) => rows.length);
  if (!groups.length) return null;
  return h("div", { class: "panel" }, h("section", { class: "section" },
    h("h2", {}, "Breakdown", infoTip("Closed trades only. A trade with several tags counts under each of them.")),
    h("div", { class: "table-wrap" }, h("table", { class: "trades breakdown" },
      h("thead", {}, h("tr", {}, h("th", {}, "By"), h("th", {}, "Value"), h("th", { class: "num" }, "Trades"), h("th", { class: "num" }, "Won"), h("th", { class: "num" }, "P/L"))),
      h("tbody", {}, groups.flatMap(([name, rows, label]) => rows.map((r, i) => h("tr", {},
        h("td", { class: "muted" }, i ? "" : name), h("td", {}, label(r.value)), h("td", { class: "num" }, String(r.trades)),
        h("td", { class: "num" }, num((r.wins / r.trades) * 100, 0) + "%"),
        h("td", { class: "num " + (r.pnl >= 0 ? "gain" : "loss") }, signedMoney(r.pnl, sym))))))))));
}
