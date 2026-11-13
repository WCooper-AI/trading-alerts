"use strict";

/* Trading Alerts web interface: Sign in, the top bar, the router and start-up.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- login

function showLogin() {
  document.getElementById("topbar").hidden = true;
  if (window.cloudAccount) { window.cloudAccount.showSignIn(); return; } // hosted: email and password
  const pw = h("input", { type: "password", id: "pw", autocomplete: "current-password", required: true });
  const msg = h("p", { class: "loss small", "aria-live": "polite" });
  const form = h("form", {}, h("div", { class: "field" }, h("label", { for: "pw" }, "Password"), pw), msg,
    h("button", { class: "primary", type: "submit" }, "Sign in"));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    msg.textContent = "";
    try { await POST("/login", { password: pw.value }); boot(); }
    catch (err) { msg.textContent = err.message; pw.select(); }
  });
  clear($main).appendChild(h("div", { class: "login" }, h("h1", {}, "Trading Alerts"),
    h("p", { class: "muted" }, "Sign in to manage your strategies."), form));
  pw.focus();
}

// --------------------------------------------------------------------- top bar + router

function setNav(name) {
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === name) {
      a.setAttribute("aria-current", "page");
      a.scrollIntoView({ block: "nearest", inline: "nearest" }); // keeps the tab visible on phones
    } else a.removeAttribute("aria-current");
  });
}

async function refreshStatus() {
  refreshBell();
  const el = document.getElementById("runner");
  try { app.status = await GET("/status"); } catch (_) { return; }
  const s = app.status;
  const r = s.runner || {};
  const problems = Object.keys(r.problems || {}).length;
  let text, cls = "dot";
  if (!r.enabled) text = "Live checks off";
  else if (r.last_error) { text = "Check failed"; cls += " err"; }
  else if (problems) { text = `${problems} need${problems === 1 ? "s" : ""} attention`; cls += " err"; }
  else if (!s.running_tests) text = "Nothing running";
  else {
    const st = s.states || {};
    const parts = [];
    if (st.watching) parts.push(`${st.watching} watching`);
    if (st.waiting) parts.push(`${st.waiting} waiting`);
    if (st.attention) parts.push(`${st.attention} need${st.attention === 1 ? "s" : ""} attention`);
    cls += st.attention ? " err" : st.watching ? " on" : " paused";
    text = parts.join(", ");
  }
  if (s.channel_error) { text = "Alert settings problem"; cls = "dot err"; }
  const open = !document.getElementById("runner-panel").hidden;
  clear(el);
  el.appendChild(h("button", { type: "button", class: "runner-btn", "aria-haspopup": "dialog", "aria-expanded": String(open),
    title: "Live checks: status and Check now", onclick: (e) => { e.stopPropagation(); toggleRunnerPanel(); } },
    h("span", { class: cls }), h("span", { class: "runner-text" }, text),
    (() => { const c = svg("svg", { viewBox: "0 0 12 12", class: "chev", "aria-hidden": "true" }); c.appendChild(svg("path", { d: "M3 4.5l3 3 3-3" })); return c; })()));
  if (open) drawRunnerPanel();
}

function toggleRunnerPanel(force) {
  const panel = document.getElementById("runner-panel");
  const show = force === undefined ? panel.hidden : force;
  panel.hidden = !show;
  const btn = document.querySelector(".runner-btn");
  if (btn) btn.setAttribute("aria-expanded", String(show));
  if (show) { document.getElementById("bell-panel").hidden = true; drawRunnerPanel(); }
}

async function drawRunnerPanel() {
  const panel = document.getElementById("runner-panel");
  const s = app.status || {};
  const r = s.runner || {};
  let dash = null;
  try { dash = await GET("/dashboard"); } catch (_) { /* the list is optional */ }
  const check = h("button", { type: "button", class: "primary", disabled: !r.enabled || r.checking, onclick: async () => {
    check.disabled = true; check.textContent = "Checking…";
    try { await POST("/run-now"); toast("Check finished"); await refreshStatus(); if (!app.isDirty) route(); }
    catch (e) { toast(e.message, true); }
    finally { check.disabled = false; check.textContent = "Check now"; }
  } }, r.checking ? "Checking…" : "Check now");
  const lines = [];
  if (!r.enabled) lines.push(h("p", { class: "small" }, "Live checks are off because the app was started with --no-runner. Forward tests and alerts aren't being checked."));
  else {
    lines.push(h("p", { class: "small muted" }, `Every ${s.poll_minutes} minutes the app checks each running strategy for candles that have closed.`));
    lines.push(h("dl", { class: "facts" },
      h("dt", {}, "Last check"), h("dd", {}, r.last_run ? `${hhmm(r.last_run)} (${ago(r.last_run)})` : "Not yet"),
      h("dt", {}, "Next check"), h("dd", {}, r.next_run ? hhmm(r.next_run) : "–"),
      s.reopens ? [h("dt", {}, "Markets reopen"), h("dd", {}, reopenText(s.reopens))] : null));
    if (s.reopens) lines.push(h("p", { class: "small muted" }, "A market is shut, so there are no new candles to check. Waiting tests carry on by themselves when it reopens."));
  }
  if (r.last_error) lines.push(h("p", { class: "error-box small" }, "The last check failed: " + r.last_error));
  if (s.channel_error) lines.push(h("p", { class: "error-box small" }, h("a", { href: "#/settings" }, "Alert settings problem: " + s.channel_error)));
  const runs = dash ? dash.running : [];
  clear(panel);
  append(panel, [
    h("div", { class: "bell-head" }, h("strong", {}, "Live checks"),
      h("button", { type: "button", class: "quiet", onclick: () => toggleRunnerPanel(false) }, "Close")),
    h("div", { class: "runner-body" }, lines,
      runs.length ? h("ul", { class: "runner-list" }, runs.map((x) => h("li", {},
        h("a", { href: "#/run/" + x.id, onclick: () => toggleRunnerPanel(false) },
          h("span", {}, h("span", { class: "rl-name" }, x.name), h("span", { class: "small muted" }, x.market)),
          stateBadge(x.health)),
        (x.health || {}).state === "attention" ? h("p", { class: "small loss rl-problem" }, x.health.text) : null)))
        : h("p", { class: "small muted" }, "Nothing running. Start a forward test or turn on alerts for a strategy."),
      h("div", { class: "runner-actions" }, check, h("a", { class: "button quiet", href: "#/settings", onclick: () => toggleRunnerPanel(false) }, "Change how often")))]);
}
document.addEventListener("click", (e) => {
  const panel = document.getElementById("runner-panel");
  if (panel && !panel.hidden && !panel.contains(e.target)) toggleRunnerPanel(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const panel = document.getElementById("runner-panel");
  if (panel && !panel.hidden) { toggleRunnerPanel(false); const b = document.querySelector(".runner-btn"); if (b) b.focus(); }
});

let lastHash = location.hash;
async function route() {
  for (const id of app.timers.splice(0)) clearInterval(id);
  for (const fn of app.cleanups.splice(0)) fn();
  hideTip();
  document.getElementById("bell-panel").hidden = true;
  document.getElementById("runner-panel").hidden = true;
  // Show a placeholder while a slow page loads, but never on top of a page that has already drawn itself.
  const before = $main.firstChild;
  const skel = setTimeout(() => { if ($main.firstChild === before) clear($main).appendChild(skeleton()); }, 160);
  const hash = location.hash || "#/";
  const parts = hash.slice(2).split("?")[0].split("/").map(decodeURIComponent);
  try {
    if (parts[0] === "s" && parts[1]) {
      const rec = await GET("/strategies/" + encodeURIComponent(parts[1]));
      if (rec.data.recipe && parts[2] !== "advanced") await viewSimple(parts[1], rec.data, rec.run);
      else await viewEditor(parts[1], null, rec.data, rec.run);
    }
    else if (parts[0] === "history" && parts[1]) await viewHistory(parts[1]);
    else if (parts[0] === "create") await viewCreate(parts[1]);
    else if (parts[0] === "new") { app.guard = null; await viewEditor(null, parts[1]); }
    else if (parts[0] === "strategies") await viewStrategies();
    else if (parts[0] === "run" && parts[1]) await viewRun(parts[1]);
    else if (parts[0] === "markets" && parts[1] === "stats") await viewMarketStats();
    else if (parts[0] === "markets") await viewMarkets();
    else if (parts[0] === "journal") await viewJournal();
    else if (parts[0] === "activity") await viewActivity();
    else if (parts[0] === "portfolio") await viewPortfolio();
    else if (parts[0] === "compare") await viewCompare();
    else if (parts[0] === "news") await viewNews();
    else if (parts[0] === "import" && parts[1]) await viewImport(parts[1]);
    else if (parts[0] === "settings") { app.guard = null; await viewSettings(); }
    else if (parts[0] === "account" && window.cloudAccount) await window.cloudAccount.viewAccount(parts[1]);
    else if ((parts[0] === "verify" || parts[0] === "reset") && parts[1] && window.cloudAccount) await window.cloudAccount.viewLink(parts[0], parts[1]);
    else {
      app.guard = null;
      await viewDashboard();
      if (parts[0]) toast("That page doesn't exist, so you're on the dashboard", true);
    }
    clearTimeout(skel);
    lastHash = location.hash;
    $main.classList.remove("enter");
    void $main.offsetWidth; // restart the entrance animation
    $main.classList.add("enter");
    $main.focus({ preventScroll: true });
  } catch (e) {
    clearTimeout(skel);
    if (e instanceof ApiError && e.message === "Sign in to continue") return;
    clear($main).appendChild(h("div", { class: "panel" }, h("div", { class: "empty" }, h("h2", {}, "Couldn't load this page"),
      h("p", { class: "error-box" }, e.message), h("div", { class: "actions" }, h("a", { class: "button", href: "#/strategies" }, "Back to strategies")))));
  }
}

// ----- "jump to" bar: built from the page's section headings, on any page with three or more
let jumpTimer = null, jumpObs = null;
function buildJumpBar() {
  if (jumpObs) jumpObs.disconnect();
  try {
    $main.querySelectorAll(".jump-bar").forEach((x) => x.remove());
    if ($main.querySelector(".section-nav")) return; // the full editor has its own
    const heads = [...$main.querySelectorAll("h2")].filter((el) =>
      !el.closest(".empty, dialog, .onboard, [hidden], .bell-panel") && el.textContent.trim() && el.offsetParent !== null);
    if (heads.length < 3) return;
    const bar = h("nav", { class: "jump-bar", "aria-label": "Jump to a section" }, h("span", { class: "jump-label small muted" }, "Jump to"));
    heads.forEach((el, i) => {
      const target = el.closest("section, .panel") || el;
      if (!target.id) target.id = "jump-" + i;
      const text = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim() || el.textContent.trim();
      bar.appendChild(h("a", { href: "#", onclick: (e) => { e.preventDefault(); target.scrollIntoView({ behavior: "smooth", block: "start" }); } }, text));
    });
    const head = $main.querySelector(".page-head");
    if (head) head.after(bar);
    else ($main.firstElementChild || $main).prepend(bar);
  } finally {
    if (jumpObs) jumpObs.observe($main, { childList: true, subtree: true });
  }
}
jumpObs = new MutationObserver((list) => {
  if (list.every((m) => m.target.closest && m.target.closest(".jump-bar"))) return;
  clearTimeout(jumpTimer);
  jumpTimer = setTimeout(buildJumpBar, 250);
});
jumpObs.observe($main, { childList: true, subtree: true });

window.addEventListener("hashchange", async () => {
  if (app.asking) { history.replaceState(null, "", lastHash || "#/"); return; } // already asking about leaving
  const message = app.guard && location.hash !== lastHash ? app.guard() : null;
  if (message) {
    // Stay put while asking; only go once they've said yes.
    const target = location.hash;
    history.replaceState(null, "", lastHash || "#/");
    app.asking = true;
    const leave = await ask(message, { ok: "Leave", cancel: "Stay" });
    app.asking = false;
    if (!leave) return;
    app.guard = null;
    app.isDirty = null;
    location.hash = target;
    return;
  }
  app.guard = null;
  app.isDirty = null;
  window.scrollTo(0, 0);
  route();
});
window.addEventListener("beforeunload", (e) => {
  if (app.isDirty && app.isDirty()) { e.preventDefault(); e.returnValue = ""; }
});

let statusTimer = null;
async function boot() {
  try {
    app.session = await GET("/session");
    if (app.session.auth_required && !app.session.signed_in) { showLogin(); return; }
    document.documentElement.dataset.edition = app.session.edition || "selfhost";
    if (app.session.appearance) {
      let legacy = null;
      try { legacy = localStorage.getItem("ta-theme"); } catch (_) { /* no storage */ }
      if (!app.session.appearance_saved && (legacy === "light" || legacy === "dark")) {
        applyAppearance(app.session.appearance);
        saveAppearance({ theme: legacy }); // carry over the light/dark choice made before it was saved in settings
      } else applyAppearance(app.session.appearance);
      let migrated = "1";
      try { migrated = localStorage.getItem("ta-theme-v2"); localStorage.setItem("ta-theme-v2", "1"); } catch (_) { /* no storage */ }
      if (!migrated && String(app.appearance.accent).toLowerCase() === "#0a84ff") saveAppearance({ accent: "#d9912b" }); // old blue default → amber, once
      try { localStorage.removeItem("ta-theme"); } catch (_) { /* no storage */ }
    }
    [app.meta, app.recipes] = await Promise.all([GET("/meta"), GET("/recipes")]);
  } catch (e) {
    if (!(e instanceof ApiError)) clear($main).appendChild(h("p", { class: "error-box" }, "Can't reach the Trading Alerts server. Is it still running?"));
    return;
  }
  document.getElementById("topbar").hidden = false;
  if (window.cloudAccount && !document.querySelector('[data-nav="account"]')) {
    const icon = svg("svg", { class: "nav-icon", viewBox: "0 0 24 24", "aria-hidden": "true" });
    icon.appendChild(svg("path", { d: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 20a8 8 0 0 1 16 0" }));
    document.querySelector(".nav").appendChild(h("a", { href: "#/account", "data-nav": "account" }, icon, h("span", {}, "Account")));
  }
  showReadOnly();
  refreshStatus();
  if (!statusTimer) statusTimer = setInterval(refreshStatus, 60000); // once, however often we sign in again
  route();
}

/** Hosted: a banner while the account is read-only (no active subscription). */
function showReadOnly() {
  document.querySelectorAll(".read-only-banner").forEach((x) => x.remove());
  if (!app.session.read_only) return;
  const msg = (window.cloudAccount && window.cloudAccount.readOnlyText()) ||
    "Your account is read-only. You can look at and download everything, but forward tests, alerts and new tests are paused.";
  document.getElementById("topbar").after(h("div", { class: "read-only-banner", role: "status" }, h("span", {}, msg),
    h("a", { class: "button small primary", href: "#/account" }, "Subscribe")));
}

async function signOut() {
  try { await POST("/logout"); } catch (_) { /* signed out anyway */ }
  app.session = null;
  location.hash = "#/";
  boot();
}

boot();
