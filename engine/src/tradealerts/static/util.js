"use strict";

/* Trading Alerts web interface. No build step, no external requests.
   All user content is inserted with textContent, never innerHTML.
   The interface is split into plain scripts loaded in order (see SCRIPTS in web/app.py); they share one scope.

   This file: helpers used everywhere: building elements, formatting numbers and dates, talking to the API. */


// --------------------------------------------------------------------- helpers

const $main = document.getElementById("main");
const SVG_NS = "http://www.w3.org/2000/svg";
const app = { meta: null, session: null, status: null, guard: null, timers: [], cleanups: [] };

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, kids);
  return el;
}
function append(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
function svg(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}
const clone = (o) => JSON.parse(JSON.stringify(o));
const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);

const nf = (d = 2) => new Intl.NumberFormat("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
function num(v, d = 2) { return v === null || v === undefined || Number.isNaN(v) ? "–" : nf(d).format(v); }
function short(v) { return typeof v === "number" ? String(+v.toFixed(6)) : String(v); }
function money(v, sym) {
  if (v === null || v === undefined) return "–";
  return (v < 0 ? "\u2212" : "") + sym + nf(2).format(Math.abs(v));
}
function signed(v, suffix = "", d = 2) { return v === null || v === undefined ? "–" : (v > 0 ? "+" : "") + num(v, d) + suffix; }
function when(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
function day(iso) { return iso ? new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "–"; }
const CURRENCY = { GBP: "£", USD: "$", EUR: "€" };

let toastTimer;
function toast(text, bad = false) {
  const t = document.getElementById("toast");
  t.textContent = text;
  t.classList.toggle("bad", bad);
  t.hidden = true;
  void t.offsetWidth; // replay the slide-in for back-to-back toasts
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 2800);
}

// --------------------------------------------------------------------- API

class ApiError extends Error {
  constructor(message, status = 0, code = null) { super(message); this.status = status; this.code = code; }
}
async function api(method, path, body, { timeout = 0 } = {}) {
  const opts = { method, headers: { "X-Requested-With": "tradealerts" }, credentials: "same-origin" };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let timer = null;
  if (timeout) {
    const ctl = new AbortController();
    opts.signal = ctl.signal;
    timer = setTimeout(() => ctl.abort(), timeout * 1000);
  }
  let res;
  try { res = await fetch("/api" + path, opts); }
  catch (e) {
    if (e.name === "AbortError") throw new ApiError("The price feed is taking too long to answer");
    throw new ApiError("Can't reach the app's server. Is it still running?");
  } finally { clearTimeout(timer); }
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (res.status === 401 && path !== "/login") { showLogin(); throw new ApiError("Sign in to continue"); }
  if (!res.ok) {
    let msg = data && data.detail;
    let code = null;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    else if (msg && typeof msg === "object") { code = msg.code || null; msg = msg.message; }
    throw new ApiError(msg || `Request failed (${res.status})`, res.status, code);
  }
  return data;
}
const GET = (p, o) => api("GET", p, undefined, o);
const POST = (p, b) => api("POST", p, b || {});
const PUT = (p, b) => api("PUT", p, b);
const DEL = (p) => api("DELETE", p);

// --------------------------------------------------------------------- dialogs
// In-app dialogs instead of confirm() and prompt(): they look like the rest of the app and
// work in every desktop app window (some webviews don't show the built-in ones).

/** Ask a yes/no question. Resolves true for the confirm button, false otherwise. */
function ask(message, { ok = "OK", cancel = "Cancel", danger = false, title = null } = {}) {
  return new Promise((resolve) => {
    const yes = h("button", { type: "button", class: danger ? "danger" : "primary" }, ok);
    const no = h("button", { type: "button" }, cancel);
    const dlg = h("dialog", { class: "ask-dialog", "aria-label": title || "Confirm" },
      title ? h("h2", {}, title) : null, h("p", {}, message), h("div", { class: "save-row" }, no, yes));
    let answer = false;
    yes.addEventListener("click", () => { answer = true; dlg.close(); });
    no.addEventListener("click", () => dlg.close());
    dlg.addEventListener("close", () => { dlg.remove(); resolve(answer); });
    document.body.appendChild(dlg);
    dlg.showModal();
    (danger ? no : yes).focus();
  });
}

/** Ask the user to pick one of several options. Resolves to the chosen option's key, or null if cancelled.
    options: [{ key, label, detail?, primary? }] */
function choose(message, options, { title = null, cancel = "Cancel" } = {}) {
  return new Promise((resolve) => {
    let answer = null;
    const dlg = h("dialog", { class: "ask-dialog choose-dialog", "aria-label": title || message });
    const buttons = options.map((o) => h("button", { type: "button", class: "choice" + (o.primary ? " primary" : ""),
      onclick: () => { answer = o.key; dlg.close(); } },
      h("strong", {}, o.label), o.detail ? h("span", { class: "small" }, o.detail) : null));
    const no = h("button", { type: "button", class: "quiet", onclick: () => dlg.close() }, cancel);
    append(dlg, [title ? h("h2", {}, title) : null, h("p", {}, message), h("div", { class: "choices" }, buttons),
      h("div", { class: "save-row" }, no)]);
    dlg.addEventListener("close", () => { dlg.remove(); resolve(answer); });
    document.body.appendChild(dlg);
    dlg.showModal();
    buttons[0].focus();
  });
}

/** Show text to copy by hand, when the clipboard isn't available. */
function showCopy(label, text) {
  const input = h("input", { value: text, readonly: true, class: "wide", "aria-label": label });
  const close = h("button", { type: "button", class: "primary" }, "Done");
  const dlg = h("dialog", { class: "ask-dialog", "aria-label": label }, h("p", {}, label), input,
    h("div", { class: "save-row" }, close));
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("close", () => dlg.remove());
  document.body.appendChild(dlg);
  dlg.showModal();
  input.select();
}

/** Run an action from a button, showing any error as a toast instead of failing silently. */
async function attempt(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); return undefined; }
}
