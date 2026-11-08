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

async function journalDialog(entry, onSaved) {
  const e = entry ? clone(entry) : { direction: null };
  const builtIn = !entry || marketByKey(entry.market);
  const marketSel = choiceSelect([...app.recipes.markets.map((m) => [m.key, m.label]), ["__other", "Something else…"]],
    entry ? (builtIn ? entry.market : "__other") : null, () => { syncOther(); loadPaper(); }, { label: "Market", cls: "wide" });
  const other = h("input", { class: "wide", placeholder: "Market name", value: builtIn ? "" : entry.market, "aria-label": "Market name" });
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
    if (!key) return;
    try {
      const r = await GET("/paper-trades?market=" + encodeURIComponent(key));
      for (const t of r.trades) paper.appendChild(h("option", { value: String(t.id) },
        `${t.strategy_name}: ${t.direction === "short" ? "sell" : "buy"} ${when(t.entry_time)} at ${num(t.entry_price)}` +
        (t.exit_price !== null ? ` → ${num(t.exit_price)}` : "")));
      paper.value = e.paper_trade_id ? String(e.paper_trade_id) : "";
    } catch (_) { /* leave unlinked */ }
  }
  loadPaper();
  const notes = h("textarea", { "aria-label": "Notes", placeholder: "Why you took it, how it went…" }, e.notes || "");
  const err = h("p", { class: "loss small", "aria-live": "polite" });
  const save = h("button", { class: "primary", type: "button" }, entry ? "Save changes" : "Add trade");
  const field = (label, control, tip) => h("div", { class: "field" }, h("label", {}, label, tip ? infoTip(tip) : null), control);
  const dlg = h("dialog", { class: "wide-dialog journal-dialog", "aria-labelledby": "jt" },
    h("h2", { id: "jt" }, entry ? "Edit trade" : "Add a real trade"),
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
      h("div", { class: "field full" }, h("label", {}, "Notes"), notes)),
    h("p", { class: "small muted" }, "Leave Closed and Exit price blank while the trade is still open."),
    err,
    h("div", { class: "actions" }, h("button", { type: "button", onclick: () => dlg.close() }, "Cancel"), save));
  save.addEventListener("click", async () => {
    const body = { ...e, market: marketSel.value === "__other" ? other.value.trim() : marketSel.value,
      paper_trade_id: paper.value ? Number(paper.value) : null, notes: notes.value };
    try {
      if (entry) await PUT(`/journal/${entry.id}`, body); else await POST("/journal", body);
      dlg.close(); toast(entry ? "Trade updated" : "Trade added"); onSaved();
    } catch (ex) { err.textContent = ex.message; }
  });
  dlg.addEventListener("close", () => dlg.remove());
  document.body.appendChild(dlg);
  dlg.showModal();
}

async function viewJournal() {
  setNav("journal");
  const res = await GET("/journal");
  const s = res.summary;
  const sym = (e) => CURRENCY[e.currency] || "";
  const page = h("div", { class: "stack" });
  const add = h("button", { class: "primary", type: "button", onclick: () => journalDialog(null, viewJournal) }, "Add a trade");
  append(page, [h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "Journal"),
      h("p", { class: "muted dash-lede" }, "A record of the trades you placed yourself. Link one to a forward-test trade to see how your real fills compared with the alert's.")),
    h("div", { class: "actions" }, h("a", { class: "button", href: "/api/journal.csv", download: "trade-journal.csv" }, "Download CSV"), add))]);
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
    (s.avg_exit_slippage !== null ? ` and your exits ${slip(s.avg_exit_slippage)}.` : ".")) : null)),
  h("div", { class: "panel" }, h("div", { class: "table-wrap" }, h("table", { class: "trades journal" },
    h("thead", {}, h("tr", {}, ["Opened", "Market", "Side", "Entry", "Closed", "Exit", "Size"].map((x) => h("th", {}, x)),
      h("th", { class: "num" }, "P/L"), h("th", {}, "Slippage"), h("th", {}, "Notes"), h("th", {}))),
    h("tbody", {}, res.entries.map((e) => h("tr", {},
      h("td", {}, when(e.entry_time)), h("td", {}, marketName(e.market)), h("td", {}, e.direction === "short" ? "Sell" : "Buy"),
      h("td", {}, num(e.entry_price)), h("td", {}, e.exit_time ? when(e.exit_time) : h("span", { class: "pill testing" }, "Open")),
      h("td", {}, e.exit_price === null ? "–" : num(e.exit_price)), h("td", {}, short(e.size)),
      h("td", { class: "num " + (e.pnl === null ? "" : e.pnl >= 0 ? "gain" : "loss") }, e.pnl === null ? "–" : signedMoney(e.pnl, sym(e))),
      h("td", { class: "small" }, e.paper ? `in ${slip(e.entry_slippage)}` + (e.exit_slippage !== null ? `, out ${slip(e.exit_slippage)}` : "") : h("span", { class: "muted" }, "not linked")),
      h("td", { class: "small muted note-cell", title: e.notes || "" }, e.notes || ""),
      h("td", { class: "num" }, h("span", { class: "inline nowrap" },
        h("button", { type: "button", class: "quiet", onclick: () => journalDialog(e, viewJournal) }, "Edit"),
        h("button", { type: "button", class: "quiet", onclick: (ev) => {
          const tr = ev.target.closest("tr");
          undoable("Trade deleted", (on) => { tr.hidden = on; }, async () => { await DEL(`/journal/${e.id}`); });
        } }, "Delete")))))))))]);
  clear($main).appendChild(page);
}
