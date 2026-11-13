"use strict";

/* Trading Alerts web interface: Strategies in plain English, and their status (draft, alerts on, forward testing).
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- describing rules in plain English
// Each describer returns a list of parts: strings, or {m: text} for values the user chose (highlighted).

const OP_WORDS = {
  "<": "is below", "<=": "is at or below", ">": "is above", ">=": "is at or above", "==": "equals",
  crosses_above: "crosses above", crosses_below: "crosses below",
};
const FIELD_WORDS = { close: "the close", open: "the open", high: "the high", low: "the low", volume: "volume" };
const FIELD_LABELS = { close: "Close price", open: "Open price", high: "High", low: "Low", volume: "Volume" };
const DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const M = (text) => ({ m: String(text) });
const isBlank = (v) => v === null || v === undefined || v === "" || (typeof v === "number" && Number.isNaN(v));
/** A value the user still has to fill in, shown as a gap in the sentence. */
const B = (hint = "…") => ({ m: hint, blank: true });
const V = (v, fmt = (x) => x) => (isBlank(v) ? B() : M(fmt(v)));

function indicatorMeta(name) { return (app.meta.indicators || []).find((i) => i.name === name); }

function describeOperand(v) {
  if (v === "") return [B("a number")];
  if (v === null || v === undefined) return [B("something")];
  if (typeof v === "number") return [M(short(v))];
  if (typeof v === "string") return [FIELD_WORDS[v] || v];
  if (!isObj(v)) return ["?"];
  if (isObj(v) && "indicator" in v && !v.indicator) return [B("an indicator")];
  let parts;
  const FRIENDLY = {
    sma: (x) => ["the ", V(x.period), "-candle average"],
    ema: (x) => ["the ", V(x.period), "-candle exponential average"],
    highest: (x) => ["the highest ", (x.source || "high") === "high" ? "price" : x.source, ` of the ${x.shift ? "previous" : "last"} `, V(x.period), " candles"],
    lowest: (x) => ["the lowest ", (x.source || "low") === "low" ? "price" : x.source, ` of the ${x.shift ? "previous" : "last"} `, V(x.period), " candles"],
  };
  if (v.indicator && FRIENDLY[v.indicator] && (v.source === undefined || v.source === null || ["close", "high", "low"].includes(v.source))) {
    const shiftShown = v.indicator === "highest" || v.indicator === "lowest" ? Math.max(0, (v.shift || 0) - 1) : v.shift || 0;
    let fp = FRIENDLY[v.indicator](v);
    if (v.scale !== undefined && v.scale !== 1) fp = [M(short(v.scale)), " × ", ...fp];
    if (v.add) fp = [...fp, v.add > 0 ? " + " : " − ", M(short(Math.abs(v.add)))];
    if (shiftShown) fp = [...fp, ", ", M(shiftShown), shiftShown === 1 ? " candle ago" : " candles ago"];
    return fp;
  }
  if (v.field) parts = [FIELD_WORDS[v.field] || v.field];
  else if (v.indicator) {
    const meta = indicatorMeta(v.indicator);
    const label = meta ? meta.short : v.indicator;
    if (v.indicator.startsWith("opening_range")) {
      parts = [label + " (", V(v.session_start), " + ", V(v.minutes, (x) => x + "m"), ")"];
    } else {
      const shown = [];
      let gaps = false;
      const params = meta ? meta.params : {};
      for (const k of Object.keys(params)) {
        const val = k in v ? v[k] : params[k];
        if (isBlank(val)) gaps = true;
        else if (typeof val === "number") shown.push(short(val));
        else if (k === "source" && val !== params[k]) shown.push(val);
      }
      if (gaps) parts = [label + "(", B(), ")"];
      else parts = shown.length ? [label + "(", M(shown.join(", ")), ")"] : [label];
    }
  } else return ["?"];
  if (v.scale !== undefined && v.scale !== 1) parts = [M(short(v.scale)), " × ", ...parts];
  if (v.add) parts = [...parts, v.add > 0 ? " + " : " − ", M(short(Math.abs(v.add)))];
  if (v.shift) parts = [...parts, " ", M(v.shift), v.shift === 1 ? " bar ago" : " bars ago"];
  return parts;
}

function joinParts(list, word) {
  const out = [];
  list.forEach((p, i) => {
    if (i) out.push(i === list.length - 1 ? ` ${word} ` : ", ");
    out.push(...p);
  });
  return out;
}

function describeCondition(c, nested = false) {
  if (!isObj(c)) return ["?"];
  const groupMode = groupKind(c);
  if (groupMode) {
    const items = groupItems(c).map((x) => describeCondition(x, true));
    if (!items.length) return ["(no conditions yet)"];
    let body;
    if (groupMode === "all") body = joinParts(items, "and");
    else if (groupMode === "any") body = joinParts(items, "or");
    else if (groupMode === "none") body = ["none of: ", ...joinParts(items, "or")];
    else body = ["not all of: ", ...joinParts(items, "and")];
    return nested && items.length > 1 ? ["(", ...body, ")"] : body;
  }
  if (isNegatedLeaf(c)) return ["it's not true that ", ...describeCondition(c.not, true)];
  if ("left" in c && isObj(c.left) && typeof c.right === "number" && !c.left.shift && (c.left.scale ?? 1) === 1 && !c.left.add) {
    const L = c.left;
    if (L.indicator === "pct_from_high" && ["<", "<="].includes(c.op) && c.right <= 0)
      return ["the price is ", M(short(Math.abs(c.right)) + "%"), " or more below its high of the last ", V(L.period), " candles"];
    if (L.indicator === "pct_from_low" && [">", ">="].includes(c.op) && c.right >= 0)
      return ["the price is ", M(short(c.right) + "%"), " or more above its low of the last ", V(L.period), " candles"];
  }
  if ("left" in c) return [...describeOperand(c.left), " ", c.op ? OP_WORDS[c.op] || c.op : B("compares with"), " ", ...describeOperand(c.right)];
  if (c.time_between) {
    const tz = (c.tz || "UTC").split("/").pop().replace(/_/g, " ");
    return ["the bar starts between ", V(c.time_between[0]), " and ", V(c.time_between[1]), ` ${tz} time`];
  }
  if (c.avoid_news) {
    const n = c.avoid_news;
    return ["it's not within ", V(n.before, (x) => `${x} min`), " before or ", V(n.after, (x) => `${x} min`), " after high-impact ",
      (n.currencies || []).length ? M(n.currencies.join("/")) : B("which currency?"), " news"];
  }
  if (c.weekdays) {
    const names = c.weekdays.map((d) => String(d).slice(0, 1).toUpperCase() + String(d).slice(1, 3));
    return ["it's ", names.length ? M(names.join(", ")) : B("which days?")];
  }
  return ["?"];
}

function describeLevel(spec, which) {
  if (!spec) return null;
  if ("atr" in spec) {
    return [V(spec.atr, (x) => short(x) + "×"), " the typical candle size away (ATR over ", V(spec.atr_period), " candles)"];
  }
  if ("percent" in spec) return [V(spec.percent, (x) => short(x) + "%"), " away"];
  if ("points" in spec) return [V(spec.points, short), " points away"];
  if ("r_multiple" in spec) return [V(spec.r_multiple, short), "× the risk"];
  if ("level" in spec) return ["at ", ...describeOperand(spec.level)];
  return null;
}

function describeStrategy(d) {
  const m = d.market || {};
  const r = d.risk || {};
  const sym = CURRENCY[r.currency] || "";
  const EVERY = { "5m": "every 5 minutes", "15m": "every 15 minutes", "30m": "every 30 minutes", "1h": "every hour",
    "2h": "every 2 hours", "4h": "every 4 hours", "1d": "once a day" };
  const tf = m.timeframe;
  const parts = [
    d.direction ? M(d.direction === "short" ? "Sell" : "Buy") : B("Buy or sell"), " ",
    m.label || m.symbol ? M(m.label || m.symbol) : B("which market?"),
    ", checking ", tf ? M(EVERY[tf] || `${tf} candles`) : B("how often?"), ", when ", ...describeCondition(d.entry || {}), ". ",
  ];
  const ex = d.exit || {};
  const stop = describeLevel(ex.stop, "stop");
  parts.push("Stop loss ", ...(stop || [B("not chosen")]));
  const target = describeLevel(ex.target, "target");
  if (target) parts.push(", target ", ...target);
  if (ex.max_bars) parts.push(", close after ", M(ex.max_bars), " bars");
  if (ex.when) parts.push(", or exit when ", ...describeCondition(ex.when));
  parts.push(". Risk ", V(r.risk_per_trade_pct, (x) => short(x) + "%"), " of ", V(r.account_size, (x) => sym + nf(0).format(x)), " a trade.");
  return parts;
}

/** Turn stand-in values (a preview's placeholders) back into gaps. Each placeholder blanks one match. */
function markBlanks(parts, placeholders) {
  const left = (placeholders || []).map((v) => short(v));
  return parts.map((p) => {
    if (typeof p === "string" || p.blank) return p;
    const i = left.indexOf(p.m.replace(/[×%]/g, ""));
    if (i < 0) return p;
    left.splice(i, 1);
    return B();
  });
}

/** The sentence as plain text, for tooltips and titles. */
function partsText(parts) { return parts.map((p) => (typeof p === "string" ? p : p.m)).join(""); }

function renderParts(parts, cls = "sentence") {
  const p = h("p", { class: cls });
  for (const part of parts) {
    if (typeof part === "string") p.appendChild(document.createTextNode(part));
    else p.appendChild(h("span", { class: part.blank ? "mark blank" : "mark" }, part.m));
  }
  return p;
}

// --------------------------------------------------------------------- strategy status (draft / live / paused)

/** Export, copy a share link. The link carries the rules only, never results or account details. */
function shareButtons(id, getData) {
  const fileBtn = h("a", { class: "button quiet", href: `/api/strategies/${encodeURIComponent(id)}/export`, download: "" , title: "Download the rules as a file" }, "Export");
  const linkBtn = h("button", { type: "button", class: "quiet", title: "Copy a link that imports these rules into someone's Altius", onclick: async () => {
    const d = clone(getData());
    delete d.enabled; d.template = true;
    const b64 = btoa(unescape(encodeURIComponent(JSON.stringify(d)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const url = `${location.origin}${location.pathname}#/import/${b64}`;
    try { await navigator.clipboard.writeText(url); toast("Share link copied. It contains the rules only"); }
    catch (_) { showCopy("Copy this link:", url); }
  } }, "Share link");
  return [fileBtn, linkBtn];
}

async function viewImport(b64) {
  let data;
  try {
    const json = decodeURIComponent(escape(atob(b64.replace(/-/g, "+").replace(/_/g, "/"))));
    data = JSON.parse(json);
  } catch (_) {
    clear($main).appendChild(h("div", { class: "panel" }, h("div", { class: "empty" }, h("h2", {}, "That link doesn't work"),
      h("p", { class: "muted" }, "It may have been cut short when it was copied."))));
    return;
  }
  const btn = h("button", { class: "primary", type: "button" }, "Import as a draft");
  btn.addEventListener("click", async () => {
    try { const r = await POST("/strategies/import", { data }); toast("Imported as a draft"); location.hash = "#/s/" + r.id; }
    catch (e) { toast(e.message, true); }
  });
  clear($main).appendChild(h("div", { class: "stack" }, h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Import a strategy"),
    h("p", { class: "muted dash-lede" }, "Someone shared these rules with you. They arrive as a draft: nothing runs until you test them and turn them on."))),
    h("div", { class: "panel" }, h("section", { class: "section" }, h("h2", {}, data.name || "Shared strategy"),
      renderParts(describeStrategy(data)),
      h("p", { class: "notice" }, "Rules someone else made aren't advice, and their results aren't shared with the link. Backtest them yourself before relying on them."),
      h("div", { class: "save-row" }, h("a", { class: "button", href: "#/strategies" }, "Cancel"), btn)))));
}

function statusOf(d) {
  if (d.template !== false) return "draft";
  return d.enabled === false ? "paused" : "live";
}
const STATUS_TEXT = { draft: "Draft", live: "Alerts on", paused: "Paused" };
function pill(status, text) { return h("span", { class: `pill ${status}` }, text || STATUS_TEXT[status]); }
function problemOf(sid) { return (((app.status || {}).runner || {}).problems || {})[sid] || null; }
const hhmm = (t) => new Date(t).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
function reopenText(t) {
  const d = new Date(t);
  return d.toLocaleDateString("en-GB", { weekday: "short" }) + " " + hhmm(d);
}

/** The one state of a forward test, the same wherever it's shown (server decides it):
    watching / waiting (market shut, reopens at…) / attention (with the reason) / stopped. */
function stateLabel(hl) {
  if (hl.state === "waiting") return hl.reopens ? `Waiting: market opens ${reopenText(hl.reopens)}` : "Waiting";
  return { watching: "Watching", attention: "Needs attention", stopped: "Stopped" }[hl.state] || "Watching";
}
function stateDetail(hl) {
  if (hl.state === "waiting") return `${hl.text || "Market closed"}. Checks start again ${reopenText(hl.reopens)}.`;
  if (hl.state === "attention") return hl.text || "Something is stopping this test being checked.";
  if (hl.state === "stopped") return "This forward test has finished.";
  return hl.next_check ? `Checked every time a candle closes. Next check at ${hhmm(hl.next_check)}.` : "Checked every time a candle closes.";
}
function stateBadge(hl) {
  hl = hl || { state: "watching" };
  return h("span", { class: "health " + hl.state, title: stateDetail(hl) }, h("span", { class: "health-dot", "aria-hidden": "true" }), stateLabel(hl));
}
function stateLine(hl) {
  if (!hl) return null;
  const cls = hl.state === "attention" ? "health-line attention small" : hl.state === "waiting" ? "health-line waiting small" : "health-line small muted";
  return h("p", { class: cls }, stateDetail(hl));
}

/** What a strategy is doing: alerts on (always forward-testing), forward testing silently, or a draft. */
function displayStatus(d, run) {
  if (statusOf(d) === "live") return "live";
  if (run) return "testing";
  return "draft";
}
STATUS_TEXT.testing = "Forward testing";
function signedMoney(v, sym) { return (v > 0 ? "+" : "") + money(v, sym); }
function positionText(run, direction) {
  if (run.position) {
    const u = run.position.unrealised;
    return `Open ${direction === "short" ? "sell" : "buy"} at ${num(run.position.entry_price)}` +
      (u ? ` (${signedMoney(u, run.currency_symbol)})` : "");
  }
  if (run.pending) return "Opening at the next candle";
  if (!run.candles) return "Waiting for the first candle to close";
  return "Waiting for a signal";
}
/** The fuller version for a card: the trade's levels, or what it's waiting for. */
function doingText(run, direction) {
  const p = run.position;
  if (p) {
    const u = p.unrealised;
    const levels = [`stop ${num(p.stop)}`, p.target ? `target ${num(p.target)}` : null].filter(Boolean).join(", ");
    return `In a trade: ${direction === "short" ? "sell" : "buy"} from ${num(p.entry_price)}, ${levels}` +
      (u ? ` (${signedMoney(u, run.currency_symbol)} so far)` : "");
  }
  if (run.pending) return "Rules met: opening a trade at the next candle";
  if (!run.candles) return "Waiting for the first candle to close";
  return "Waiting for the rules to be met";
}
function marketText(hl) {
  if (!hl) return null;
  if (hl.state === "waiting") return `Market shut (${hl.why === "weekend" ? "weekend" : "daily break"}), opens ${reopenText(hl.reopens)}`;
  return "Market open";
}
function checksText(hl) {
  if (!hl || hl.state === "attention" || hl.state === "stopped") return null;
  const parts = [];
  if (hl.last_check) parts.push(`Last check ${hhmm(hl.last_check)}`);
  if (hl.next_check) parts.push(`Next check ${hhmm(hl.next_check)}`);
  return parts.join(" · ") || null;
}

/** Forward test start/stop and the phone alerts switch, shared by both editors. */
function statusControls(o) {
  const wrap = h("div", { class: "status-controls" });
  let run = o.run || null;
  const render = () => {
    clear(wrap);
    if (o.isNew) { wrap.appendChild(h("span", { class: "small muted" }, "Save the strategy to forward test it or turn on alerts.")); return; }
    const d = o.getData();
    const alertsOn = statusOf(d) === "live";
    const ft = h("button", { type: "button", class: run ? "" : "primary" }, run ? "Stop forward test" : "Start forward test");
    ft.addEventListener("click", async () => {
      ft.disabled = true;
      try {
        if (run) {
          const extra = alertsOn ? " Phone alerts will be turned off too." : "";
          if (!(await ask("Stop this forward test? Its results stay on the dashboard." + extra, { ok: "Stop it", danger: true }))) return;
          const r = await POST(`/strategies/${encodeURIComponent(o.id)}/forward-test/stop`);
          if (r.alerts_turned_off) d.enabled = false;
          run = null;
          toast("Forward test stopped. Its results are on the dashboard");
        } else {
          if (o.isDirty() && !(await o.save())) return;
          run = await POST(`/strategies/${encodeURIComponent(o.id)}/forward-test/start`);
          run.candles = 0;
          toast("Forward test started. Candles that close from now on are counted");
        }
        refreshStatus();
      } catch (e) { toast(e.message, true); } finally { ft.disabled = false; render(); }
    });
    const al = h("button", { type: "button", class: "toggle", "aria-pressed": String(alertsOn),
      title: alertsOn ? "Phone alerts are on. Click to turn them off." : "Get a phone alert each time the rules are met" },
    h("span", { class: "toggle-dot" }), alertsOn ? "Phone alerts on" : "Phone alerts off");
    al.addEventListener("click", async () => {
      const before = { template: d.template, enabled: d.enabled };
      if (!alertsOn) {
        if (!(await confirmLive())) return;
        d.template = false; d.enabled = true;
      } else {
        d.enabled = false;
      }
      if (!(await o.save())) { Object.assign(d, before); render(); return; }
      if (!alertsOn && !run) {
        try { run = await POST(`/strategies/${encodeURIComponent(o.id)}/forward-test/start`); run.candles = 0; } catch (_) { /* runner starts one */ }
      }
      refreshStatus();
      render();
    });
    append(wrap, [ft, al, run ? h("span", { class: "small muted run-note" }, `Forward testing since ${when(run.started_at)}. `,
      h("a", { href: "#/run/" + run.id }, "See progress")) : null]);
  };
  render();
  return wrap;
}
