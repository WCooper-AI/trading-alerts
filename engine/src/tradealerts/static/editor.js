"use strict";

/* Trading Alerts web interface: The rule builder: groups of conditions and the inputs for each one.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- rule builder: groups

function groupKind(c) {
  if (!isObj(c)) return null;
  if (Array.isArray(c.all)) return "all";
  if (Array.isArray(c.any)) return "any";
  if (isObj(c.not) && Array.isArray(c.not.any)) return "none";
  if (isObj(c.not) && Array.isArray(c.not.all)) return "notall";
  return null;
}
/** A single condition wrapped in {not: ...} (rules.py supports it; shown as a row with a "not" switch). */
function isNegatedLeaf(c) {
  return isObj(c) && !groupKind(c) && isObj(c.not) && !groupKind(c.not) && Object.keys(c).length === 1;
}
/** The leaf itself, whether or not it is wrapped in {not: ...}. */
function leafOf(c) { return isNegatedLeaf(c) ? c.not : c; }
/** True for leaf shapes the builder has inputs for; anything else is kept as a read-only card. */
function editableLeaf(c) {
  const x = leafOf(c);
  return isObj(x) && Boolean("left" in x || x.time_between || x.weekdays || x.avoid_news);
}
function groupItems(c) {
  const k = groupKind(c);
  if (k === "all") return c.all;
  if (k === "any") return c.any;
  if (k === "none") return c.not.any;
  if (k === "notall") return c.not.all;
  return [];
}
function makeGroup(kind, items) {
  if (kind === "all") return { all: items };
  if (kind === "any") return { any: items };
  if (kind === "none") return { not: { any: items } };
  return { not: { all: items } };
}
/** Always edit conditions as a group so there's somewhere to add rows. */
function asGroup(c) {
  if (groupKind(c)) return c;
  if (isObj(c) && Object.keys(c).length) return { all: [c] };
  return { all: [] };
}

function newComparison() {
  return { left: null, op: "", right: null };
}

/**
 * Editor for a group of conditions.
 * get/set read and replace the group in its parent; onChange tells the page something changed.
 */
function groupEditor(get, set, onChange, depth = 0, onRemove = null, pos = null) {
  const wrap = h("div", { class: depth ? "group nested" : "group" });
  function render() {
    clear(wrap);
    const g = get();
    const kind = groupKind(g);
    const items = groupItems(g);
    const mode = h("select", {
      "aria-label": "How the conditions combine",
      onchange: (e) => { set(makeGroup(e.target.value, groupItems(get()))); onChange(); render(); },
    },
    h("option", { value: "all" }, "all of these are true"),
    h("option", { value: "any" }, "any of these is true"),
    h("option", { value: "none" }, "none of these is true"),
    h("option", { value: "notall" }, "not all of these are true"));
    mode.value = kind;
    append(wrap, [h("div", { class: "group-head" },
      h("span", { class: "word" }, depth ? "A group where" : "When"), mode,
      moveButtons(pos, "group"),
      onRemove ? h("button", { class: "quiet icon remove", type: "button", "aria-label": "Remove group", title: "Remove group",
        onclick: () => { onRemove(); } }, "✕") : null)]);
    items.forEach((item, i) => {
      const getItem = () => groupItems(get())[i];
      const setItem = (v) => { groupItems(get())[i] = v; };
      const remove = () => { groupItems(get()).splice(i, 1); onChange(); render(); };
      const move = (d) => {
        const arr = groupItems(get()), j = i + d;
        if (j < 0 || j >= arr.length) return;
        [arr[i], arr[j]] = [arr[j], arr[i]]; onChange(); render();
      };
      const pos = { move, first: i === 0, last: i === items.length - 1 };
      if (groupKind(item)) wrap.appendChild(groupEditor(getItem, setItem, onChange, depth + 1, remove, pos));
      else wrap.appendChild(conditionRow(getItem, setItem, onChange, remove, pos));
    });
    const add = (v) => { groupItems(get()).push(v); onChange(); render(); };
    append(wrap, [h("div", { class: "adders" },
      h("button", { type: "button", onclick: () => add(newComparison()) }, "Add comparison"),
      h("button", { type: "button", onclick: () => add({ time_between: ["", ""], tz: "" }) }, "Add time window"),
      h("button", { type: "button", onclick: () => add({ weekdays: [] }) }, "Add weekdays"),
      h("button", { type: "button", onclick: () => add({ avoid_news: { before: null, after: null, currencies: [], impact: "high" } }) }, "Add news filter"),
      depth < 2 ? h("button", { type: "button", onclick: () => add({ any: [newComparison()] }) }, "Add group") : null)]);
  }
  render();
  return wrap;
}

// --------------------------------------------------------------------- rule builder: single conditions

const NEWS_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY"];

/** Up/down buttons for a row or group; null when there is nowhere to move. */
function moveButtons(pos, what) {
  if (!pos || (pos.first && pos.last)) return null;
  return h("span", { class: "move" },
    h("button", { class: "quiet icon", type: "button", disabled: pos.first, "aria-label": `Move ${what} up`, title: "Move up",
      onclick: () => pos.move(-1) }, "▲"),
    h("button", { class: "quiet icon", type: "button", disabled: pos.last, "aria-label": `Move ${what} down`, title: "Move down",
      onclick: () => pos.move(1) }, "▼"));
}

function conditionRow(getRaw, setRaw, onChange, onRemove, pos = null) {
  const row = h("div", { class: "cond" });
  const body = h("div", { class: "body" });
  const raw = getRaw();
  if (!editableLeaf(raw)) {
    // Anything the builder has no inputs for (custom YAML) is kept exactly as written.
    append(body, [h("span", { class: "muted" }, "Custom rule (edit it in YAML; kept as written):"),
      h("code", { class: "custom-card" }, JSON.stringify(raw))]);
    append(row, [body, moveButtons(pos, "condition"), h("button", { class: "quiet icon remove", type: "button",
      "aria-label": "Remove condition", title: "Remove", onclick: onRemove }, "✕")]);
    return row;
  }
  const negated = isNegatedLeaf(raw);
  const get = negated ? () => getRaw().not : getRaw;
  const set = negated ? (v) => { getRaw().not = v; } : setRaw;
  const notBtn = h("button", { class: "quiet not-toggle", type: "button", "aria-pressed": String(negated),
    title: negated ? "Switch back to a plain condition" : "Require this to be false", onclick: () => {
      setRaw(negated ? getRaw().not : { not: getRaw() });
      onChange();
      row.replaceWith(conditionRow(getRaw, setRaw, onChange, onRemove, pos));
    } }, negated ? "not" : "not?");
  body.appendChild(notBtn);
  const c = get();
  if ("left" in c) {
    const op = choiceSelect(Object.entries(OP_WORDS), c.op, (v) => { get().op = v; onChange(); }, { label: "Comparison" });
    append(body, [
      operandEditor(() => get().left, (v) => { get().left = v; }, onChange, "Left side"),
      op,
      operandEditor(() => get().right, (v) => { get().right = v; }, onChange, "Right side"),
    ]);
  } else if (c.time_between) {
    const t = (i) => timeInput(c.time_between[i], (v) => { get().time_between[i] = v; onChange(); }, i ? "End time" : "Start time");
    append(body, [h("span", { class: "word" }, "Bar starts between"), t(0), h("span", { class: "word" }, "and"), t(1),
      tzInput(c.tz, (v) => { get().tz = v; onChange(); }, true)]);
  } else if (c.weekdays) {
    const days = h("div", { class: "days", role: "group", "aria-label": "Days" });
    DAY_NAMES.forEach((d) => {
      const on = () => get().weekdays.map((x) => String(x).toLowerCase().slice(0, 3)).includes(d);
      const b = h("button", { type: "button", "aria-pressed": String(on()),
        onclick: () => {
          const list = get().weekdays.map((x) => String(x).toLowerCase().slice(0, 3));
          get().weekdays = on() ? list.filter((x) => x !== d) : DAY_NAMES.filter((x) => list.includes(x) || x === d);
          b.setAttribute("aria-pressed", String(on())); onChange();
        } }, d[0].toUpperCase() + d.slice(1));
      days.appendChild(b);
    });
    append(body, [h("span", { class: "word" }, "Only on"), days]);
  } else if (c.avoid_news) {
    const n = c.avoid_news;
    const cur = h("div", { class: "days", role: "group", "aria-label": "Currencies" });
    for (const code of NEWS_CURRENCIES) {
      const b = h("button", { type: "button", "aria-pressed": String((n.currencies || []).includes(code)), onclick: () => {
        const list = get().avoid_news.currencies || [];
        get().avoid_news.currencies = list.includes(code) ? list.filter((x) => x !== code) : [...list, code];
        b.setAttribute("aria-pressed", String(get().avoid_news.currencies.includes(code))); onChange();
      } }, code);
      cur.appendChild(b);
    }
    append(body, [h("span", { class: "word" }, "Not within"),
      numberInput(n.before, (v) => { get().avoid_news.before = v; onChange(); }, { label: "Minutes before", step: "1", min: "0", integer: true, cls: "narrow" }),
      h("span", { class: "word" }, "min before or"),
      numberInput(n.after, (v) => { get().avoid_news.after = v; onChange(); }, { label: "Minutes after", step: "1", min: "0", integer: true, cls: "narrow" }),
      h("span", { class: "word" }, "min after high-impact news for"), cur,
      infoTip("Skips signals near scheduled high-impact news (like the US jobs report or interest rate decisions) for the currencies you pick. Settings, News calendar shows what's included.")]);
  }
  append(row, [body, moveButtons(pos, "condition"), h("button", { class: "quiet icon remove", type: "button",
    "aria-label": "Remove condition", title: "Remove", onclick: onRemove }, "✕")]);
  return row;
}

function tzInput(value, onInput, required = false) {
  const id = "tz-list";
  if (!document.getElementById(id)) {
    document.body.appendChild(h("datalist", { id }, app.meta.timezones.map((z) => h("option", { value: z }))));
  }
  const el = h("input", { type: "text", list: id, value: value || "", "aria-label": "Time zone", size: 16,
    placeholder: required ? "Time zone" : "UTC" });
  const mark = () => el.classList.toggle("to-fill", required && !el.value.trim());
  el.addEventListener("input", () => { mark(); onInput(el.value.trim() || (required ? "" : "UTC")); });
  mark();
  return el;
}

function timeInput(value, onSet, label = null) {
  const el = h("input", { type: "time", value: value || "", "aria-label": label });
  const mark = () => el.classList.toggle("to-fill", !el.value);
  el.addEventListener("input", () => { mark(); onSet(el.value); });
  mark();
  return el;
}

/** Picker for one side of a comparison: a price field, a number or an indicator with its settings. */
function operandEditor(get, set, onChange, label) {
  const wrap = h("span", { class: "operand" });
  function kindOf(v) {
    if (v === null || v === undefined) return "";
    if (v === "" || typeof v === "number") return "number";
    if (typeof v === "string") return "field:" + v;
    if (isObj(v) && v.field) return "field:" + v.field;
    if (isObj(v) && v.indicator) return "ind:" + v.indicator;
    return "number";
  }
  function render() {
    clear(wrap);
    const v = get();
    const sel = h("select", { "aria-label": label },
      h("option", { value: "", disabled: true }, "Choose…"),
      h("optgroup", { label: "Price" }, Object.entries(FIELD_LABELS).map(([k, t]) => h("option", { value: "field:" + k }, t))),
      h("option", { value: "number" }, "A number"),
      h("optgroup", { label: "Indicators" }, app.meta.indicators.map((i) => h("option", { value: "ind:" + i.name, title: i.description }, i.label))));
    sel.value = kindOf(v);
    sel.classList.toggle("to-fill", sel.value === "");
    sel.addEventListener("change", () => {
      const k = sel.value;
      if (k === "number") set(typeof get() === "number" ? get() : "");
      else if (k.startsWith("field:")) set(k.slice(6));
      else {
        const name = k.slice(4);
        const params = {};
        for (const key of Object.keys(indicatorMeta(name).params)) params[key] = null;
        set({ indicator: name, ...params });
      }
      onChange(); render();
    });
    wrap.appendChild(sel);

    if (typeof v === "number" || v === "") {
      wrap.appendChild(numberInput(v, (n) => { set(n); onChange(); }, { label: "Value", step: "any", cls: "narrow", emptyValue: "" }));
      return;
    }
    if (isObj(v) && v.indicator) {
      const meta = indicatorMeta(v.indicator);
      for (const [k, def] of Object.entries(meta ? meta.params : {})) {
        wrap.appendChild(paramInput(k, k in v ? v[k] : null, def, meta.types[k], (val) => { get()[k] = val; onChange(); }));
      }
    }
    wrap.appendChild(extrasEditor(get, set, onChange));
  }
  render();
  return wrap;
}

const PARAM_LABELS = { period: "bars", source: "of", std: "std devs", multiplier: "×", atr_period: "ATR bars",
  fast: "fast", slow: "slow", signal: "signal", session_start: "from", minutes: "for (min)", tz: "zone" };

function paramInput(key, value, def, type, onSet) {
  const label = PARAM_LABELS[key] || key;
  let input;
  if (key === "source") {
    input = choiceSelect(app.meta.fields.map((f) => [f, f]), value, onSet, { label });
  } else if (key === "tz") {
    input = tzInput(value || "", onSet, true);
  } else if (key === "session_start") {
    input = timeInput(value, onSet);
  } else {
    const integer = type === "int";
    input = numberInput(value, onSet, { step: integer ? "1" : "any", min: integer ? "1" : null, integer });
  }
  input.setAttribute("aria-label", label);
  return h("span", { class: "param" }, label, input);
}

function numberInput(value, onSet, { label, step = "any", min = null, integer = false, cls = null, allowEmpty = false,
  emptyValue = null, placeholder = null } = {}) {
  const el = h("input", { type: "number", step, min, value: isBlank(value) ? "" : value, class: cls, inputmode: "decimal",
    "aria-label": label, placeholder });
  const mark = () => el.classList.toggle("to-fill", !allowEmpty && el.value.trim() === "");
  el.addEventListener("input", () => {
    const raw = el.value.trim();
    mark();
    if (raw === "") { el.removeAttribute("aria-invalid"); onSet(emptyValue); return; }
    const n = Number(raw);
    const bad = Number.isNaN(n) || (integer && !Number.isInteger(n)) || (min !== null && n < Number(min));
    el.setAttribute("aria-invalid", String(bad));
    el.setCustomValidity(!bad ? "" : Number.isNaN(n) ? "Enter a number"
      : integer && !Number.isInteger(n) ? "Enter a whole number" : `Must be at least ${min}`);
    if (!bad) onSet(n);
  });
  el.addEventListener("change", () => { if (el.getAttribute("aria-invalid") === "true") el.reportValidity(); });
  mark();
  return el;
}

/** A select that starts on a "Choose…" option until something is picked. */
function choiceSelect(options, value, onPick, { label, placeholder = "Choose…", cls = null } = {}) {
  const sel = h("select", { "aria-label": label, class: cls },
    h("option", { value: "", disabled: true }, placeholder),
    options.map(([v, text, title]) => h("option", { value: v, title }, text)));
  sel.value = isBlank(value) ? "" : String(value);
  if (sel.value !== String(value ?? "")) sel.value = "";
  const mark = () => sel.classList.toggle("to-fill", sel.value === "");
  sel.addEventListener("change", () => { mark(); onPick(sel.value); });
  mark();
  return sel;
}

/** "bars ago", "×" and "+" adjustments, tucked behind a button unless already used. */
function extrasEditor(get, set, onChange) {
  const v = get();
  const used = isObj(v) && ((v.shift || 0) !== 0 || (v.scale ?? 1) !== 1 || (v.add || 0) !== 0);
  const box = h("span", { class: "operand" });
  function show() {
    clear(box);
    const cur = () => {
      const x = get();
      if (typeof x === "string") { set({ field: x }); return get(); }
      return x;
    };
    const tidy = () => {
      const x = get();
      if (isObj(x) && x.field && !x.shift && (x.scale ?? 1) === 1 && !x.add) set(x.field);
    };
    const field = (key, lbl, def, opts) => h("span", { class: "param" }, lbl, numberInput(isObj(get()) ? get()[key] ?? def : def, (n) => {
      const x = cur();
      if (n === def) delete x[key]; else x[key] = n;
      tidy(); onChange();
    }, { ...opts, label: lbl }));
    append(box, [field("shift", "bars ago", 0, { step: "1", min: "0", integer: true }),
      field("scale", "×", 1, {}), field("add", "+", 0, {})]);
  }
  if (used) show();
  else box.appendChild(h("button", { type: "button", class: "quiet more", title: "Look back, multiply or add",
    onclick: show }, "Adjust"));
  return box;
}
