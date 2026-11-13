"use strict";

/* Trading Alerts web interface: Appearance and the settings page.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- appearance (theme, accent, density)

const ACCENTS = [
  ["#d9912b", "Amber"], ["#0a84ff", "Blue"], ["#5e5ce6", "Indigo"], ["#bf5af2", "Purple"], ["#ff375f", "Pink"], ["#ff453a", "Red"],
  ["#ff9f0a", "Orange"], ["#ffd60a", "Yellow"], ["#30d158", "Green"], ["#40c8e0", "Teal"], ["#8e8e93", "Graphite"],
];
app.appearance = { theme: "auto", accent: "#d9912b" };
// The default accent defers to the per-theme amber in app.css.
const DEFAULT_ACCENTS = ["#d9912b"];

/** Black or white text, whichever reads better on the accent colour. */
function inkFor(hex) {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  const lum = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  return lum > 0.36 ? "#0b0d12" : "#ffffff";
}

function applyAppearance(change) {
  const a = (app.appearance = { ...app.appearance, ...change });
  const root = document.documentElement;
  if (a.theme === "light" || a.theme === "dark") root.setAttribute("data-theme", a.theme);
  else root.removeAttribute("data-theme");
  const custom = !DEFAULT_ACCENTS.includes(String(a.accent).toLowerCase());
  if (custom) {
    root.style.setProperty("--accent", a.accent);
    root.style.setProperty("--accent-ink", inkFor(a.accent));
  } else ["--accent", "--accent-ink", "--accent-text"].forEach((k) => root.style.removeProperty(k));
  root.removeAttribute("data-glass"); // retired setting
  if (a.density === "compact") root.setAttribute("data-density", "compact"); else root.removeAttribute("data-density");
  const dark = (a.theme === "dark") || (a.theme !== "light" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  if (custom) root.style.setProperty("--accent-text", readableAccent(a.accent, dark));
  try { localStorage.setItem("ta-appearance", JSON.stringify({ ...a, ink: inkFor(a.accent) })); } catch (_) { /* not cached */ }
  renderThemeToggle();
}

async function saveAppearance(change) {
  applyAppearance(change);
  try { await PUT("/appearance", change); }
  catch (e) { toast("Appearance not saved: " + e.message, true); }
}

function effectiveTheme() {
  const t = document.documentElement.getAttribute("data-theme");
  if (t) return t;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function setTheme(choice) { saveAppearance({ theme: choice === "light" || choice === "dark" ? choice : "auto" }); }
function renderThemeToggle() {
  const b = document.getElementById("theme-toggle");
  if (!b) return;
  const dark = effectiveTheme() === "dark";
  const label = dark ? "Switch to light mode" : "Switch to dark mode";
  clear(b);
  append(b, [h("span", { "aria-hidden": "true" }, dark ? "☀" : "☾"), h("span", { class: "sr-only" }, label)]);
  b.title = label;
}
document.getElementById("theme-toggle").addEventListener("click", () => setTheme(effectiveTheme() === "dark" ? "light" : "dark"));
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { applyAppearance({}); });
try {
  const cached = JSON.parse(localStorage.getItem("ta-appearance") || "null");
  if (cached) app.appearance = { ...app.appearance, ...cached };
} catch (_) { /* nothing cached */ }
renderThemeToggle();

function appearanceSection() {
  const sec = h("section", { class: "section" }, h("h2", {}, "Appearance"));
  const body = h("div", { class: "appearance" });
  sec.appendChild(body);
  function render() {
    clear(body);
    const a = app.appearance;
    const theme = h("div", { class: "segmented", role: "group", "aria-label": "Theme" });
    for (const [v, t] of [["auto", "Match my device"], ["light", "Light"], ["dark", "Dark"]]) {
      theme.appendChild(h("button", { type: "button", "aria-pressed": String((a.theme || "auto") === v),
        onclick: () => { saveAppearance({ theme: v }); render(); } }, t));
    }
    const swatches = h("div", { class: "swatches", role: "group", "aria-label": "Accent colour" });
    for (const [hex, name] of ACCENTS) {
      const b = h("button", { type: "button", class: "swatch-btn", title: name, "aria-label": name,
        "aria-pressed": String(a.accent.toLowerCase() === hex), onclick: () => { saveAppearance({ accent: hex }); render(); } });
      b.style.setProperty("--swatch", hex);
      swatches.appendChild(b);
    }
    const custom = h("input", { type: "color", value: a.accent, "aria-label": "Custom accent colour" });
    custom.addEventListener("input", () => applyAppearance({ accent: custom.value }));
    custom.addEventListener("change", () => { saveAppearance({ accent: custom.value }); render(); });
    swatches.appendChild(h("label", { class: "custom-colour" }, custom, "Custom"));
    append(body, [
      h("div", { class: "field" }, h("label", {}, "Theme"), theme),
      h("div", { class: "field" }, h("label", {}, "Accent colour"), swatches),
      h("div", { class: "field" }, h("label", {}, "Spacing"), (() => {
        const seg = h("div", { class: "segmented", role: "group", "aria-label": "Spacing" });
        for (const [v, t] of [["comfortable", "Comfortable"], ["compact", "Compact"]]) seg.appendChild(h("button", { type: "button",
          "aria-pressed": String((a.density || "comfortable") === v), onclick: () => { saveAppearance({ density: v }); render(); } }, t));
        return seg;
      })()),
    ]);
  }
  render();
  return sec;
}

// --------------------------------------------------------------------- page: settings

const PRICE_SOURCES = [
  { value: "dukascopy", name: "Dukascopy", tag: "Recommended",
    text: "Free, no account or token. CFD-style bid and ask prices with years of history, so spreads are real. The first download of a market takes a little while; after that only new candles are fetched." },
  { value: "yahoo", name: "Yahoo Finance",
    text: "Free, no account or token. Exchange prices with about 2 years of hourly history. Index futures jump at each contract roll and there's no spread data." },
  { value: "oanda", name: "OANDA",
    text: "Real CFD prices from a broker. Needs a free OANDA practice account and an API token." },
];

function priceDataSection(s) {
  const d = s.data;
  const sec = h("section", { class: "section" });
  const result = h("p", { class: "small", "aria-live": "polite" });
  function render() {
    clear(sec);
    append(sec, [h("h2", {}, "Price data"),
      h("p", { class: "section-note" }, "Where backtests and forward tests get prices for the built-in markets. Press Save settings to keep your choice.")]);
    const cards = h("div", { class: "source-cards", role: "radiogroup", "aria-label": "Price data source" });
    const allowed = (limits().sources || PRICE_SOURCES.map((x) => x.value));
    for (const src of PRICE_SOURCES.filter((x) => allowed.includes(x.value))) {
      const on = d.source === src.value;
      cards.appendChild(h("button", { type: "button", class: "choice source", role: "radio", "aria-checked": String(on),
        "aria-pressed": String(on), onclick: () => { d.source = src.value; result.textContent = ""; render(); } },
      h("span", { class: "choice-name" }, src.name, src.tag ? h("span", { class: "tag" }, src.tag) : null),
      h("span", { class: "choice-note" }, src.text)));
    }
    sec.appendChild(cards);
    if (d.source === "oanda") {
      const tok = h("input", { id: "oanda-token", type: "password", autocomplete: "off", spellcheck: "false",
        placeholder: d.oanda_token_set ? "Saved. Leave blank to keep it" : "Paste your API token here",
        oninput: (e) => { d.oanda_token = e.target.value.trim(); } });
      const env = h("select", { id: "oanda-env", onchange: (e) => { d.oanda_environment = e.target.value; } },
        h("option", { value: "practice" }, "Practice (demo) account"), h("option", { value: "live" }, "Live account"));
      env.value = d.oanda_environment || "practice";
      append(sec, [h("div", { class: "token-box" },
        h("div", { class: "fields price-fields" },
          h("div", { class: "field" }, h("label", { for: "oanda-token" }, "OANDA API token"), tok),
          h("div", { class: "field" }, h("label", { for: "oanda-env" }, "Account type"), env)),
        h("p", { class: "small muted" }, "Get one by opening a free practice account at oanda.com, then Manage API Access. Some markets depend on your region."))]);
    }
    const check = h("button", { type: "button", onclick: async () => {
      check.disabled = true;
      result.className = "small muted";
      result.textContent = d.source === "dukascopy" ? "Fetching a few days of US 500 prices from Dukascopy. This can take up to a minute…" : "Fetching a few days of US 500 prices…";
      try {
        const r = await POST("/data/test", { data: d });
        result.className = "small gain";
        result.textContent = `Working. Got ${r.candles} hourly candles for US 500, last close ${num(r.last_close)} at ${when(r.last)}` +
          (r.spread !== null ? `, typical spread ${num(r.spread, 2)}.` : ".");
      } catch (e) { result.className = "small loss"; result.textContent = e.message; }
      finally { check.disabled = false; }
    } }, "Check price data");
    append(sec, [h("div", { class: "inline check-row" }, check), result]);
  }
  render();
  return sec;
}

function reportSection(s) {
  const r = s.report;
  const sendBtn = h("button", { type: "button", onclick: async () => {
    sendBtn.disabled = true;
    try {
      const res = await POST("/report/send");
      if (res.errors.length) toast("Some channels failed: " + res.errors.join("; "), true);
      else toast(`Report sent to ${res.sent_to.join(", ")}`);
    } catch (e) { toast(e.message, true); } finally { sendBtn.disabled = false; }
  } }, "Send one now");
  const day = h("select", { "aria-label": "Day", onchange: (e) => { r.day = e.target.value; } },
    [["mon", "Monday"], ["tue", "Tuesday"], ["wed", "Wednesday"], ["thu", "Thursday"], ["fri", "Friday"], ["sat", "Saturday"], ["sun", "Sunday"]]
      .map(([v, t]) => h("option", { value: v }, t)));
  day.value = r.day;
  const hour = h("select", { "aria-label": "Time", onchange: (e) => { r.hour = Number(e.target.value); } },
    Array.from({ length: 24 }, (_, i) => h("option", { value: i }, `${String(i).padStart(2, "0")}:00`)));
  hour.value = String(r.hour);
  const zones = [...new Set([r.tz, Intl.DateTimeFormat().resolvedOptions().timeZone, ...app.meta.timezones].filter(Boolean))];
  const tz = h("select", { "aria-label": "Time zone", onchange: (e) => { r.tz = e.target.value; } },
    h("option", { value: "" }, isHosted() ? "UK time" : "This computer's time"), zones.map((z) => h("option", { value: z }, z.replace(/_/g, " "))));
  tz.value = r.tz || "";
  if (isHosted() && !r.tz) { r.tz = "Europe/London"; tz.value = "Europe/London"; }
  const reportLink = app.session.desktop
    ? h("a", { class: "button", href: "/api/report.html" }, "View this week's report")
    : h("a", { class: "button", href: "/api/report.html", target: "_blank", rel: "noopener" }, "View this week's report");
  return h("section", { class: "section" }, h("h2", {}, "Weekly report"),
    h("p", { class: "section-note" }, "A summary of the week: each forward test's result and trades, your journal, price alerts that hit, and the big news coming up. Email gets a formatted version; other channels get a short text one."),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: r.weekly, onchange: (e) => { r.weekly = e.target.checked; } }), "Send me a weekly report"),
    h("div", { class: "inline check-row" }, h("span", { class: "word" }, "Every"), day, h("span", { class: "word" }, "at"), hour, tz),
    h("div", { class: "inline check-row" }, reportLink, sendBtn),
    h("p", { class: "after-note" }, "The report page has a Print button: choose Save as PDF to keep a copy."));
}

function backupSection() {
  const file = h("input", { type: "file", accept: ".zip,application/zip", hidden: true });
  const status = h("p", { class: "small", "aria-live": "polite" });
  file.addEventListener("change", async () => {
    const f = file.files[0];
    file.value = "";
    if (!f) return;
    if (!(await ask(`Restore "${f.name}"? This replaces your strategies, settings, forward tests, journal, alerts and notes with the ones in the backup. A copy of what you have now is saved first.`, { ok: "Restore", danger: true }))) return;
    status.textContent = "Restoring…";
    try {
      const res = await fetch("/api/restore", { method: "POST", body: f, credentials: "same-origin",
        headers: { "X-Requested-With": "tradealerts", "Content-Type": "application/zip" } });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || "Restore failed");
      status.textContent = `Restored ${body.strategies} strategies. A copy of what you had before was saved as ${body.safety_copy}.` +
        ((body.stopped_over_limit || []).length ? ` Alerts or forward tests over your plan's limit were stopped: ${body.stopped_over_limit.join(", ")}.` : "");
      toast("Backup restored"); refreshStatus();
    } catch (e) { status.textContent = ""; toast(e.message, true); }
  });
  return h("section", { class: "section" }, h("h2", {}, "Backup"),
    h("p", { class: "section-note" }, "Everything in one file: settings, strategies, forward tests, journal, price alerts and news notes. " +
      (isHosted() ? "Use it to keep your own copy, or to move to or from the self-hosted version." : "Use it to move between the desktop app and a server, or before trying something risky.")),
    h("div", { class: "inline" }, h("a", { class: "button", href: "/api/backup", download: "" }, "Download backup"), file,
      h("button", { type: "button", onclick: () => file.click() }, "Restore from a backup…")),
    h("p", { class: "after-note" }, "The backup includes your alert and API tokens, so keep it somewhere private."), status);
}

async function viewSettings() {
  setNav("settings");
  const s = await GET("/settings");
  const n = s.notifiers;
  const page = h("div", { class: "stack" });
  const text = (obj, key, label, opts = {}) => h("div", { class: "field" }, h("label", {}, label),
    h("input", { value: obj[key] || "", type: opts.secret ? "password" : "text", placeholder: opts.placeholder || null,
      autocomplete: "off", spellcheck: "false", oninput: (e) => { obj[key] = e.target.value; } }),
    /^\$\{[A-Za-z_]/.test(obj[key] || "") ? h("span", { class: "small muted" }, "Read from an environment variable on the server")
      : opts.note ? h("span", { class: "small muted" }, opts.note) : null);
  const enabled = (obj, label) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: obj.enabled,
    onchange: (e) => { obj.enabled = e.target.checked; } }), label);

  const minPoll = limits().min_poll_minutes || 1;
  const poll = h("select", { onchange: (e) => { s.poll_minutes = Number(e.target.value); } },
    [1, 5, 15, 30, 60].filter((x) => x >= minPoll).map((x) => h("option", { value: x }, x === 1 ? "Every minute" : `Every ${x} minutes`)));
  poll.value = String(s.poll_minutes);
  if (poll.value !== String(s.poll_minutes)) { poll.appendChild(h("option", { value: s.poll_minutes }, `Every ${s.poll_minutes} minutes`)); poll.value = String(s.poll_minutes); }

  let onSaved = () => {};
  const saveBtn = h("button", { class: "primary", type: "button", onclick: async () => {
    saveBtn.disabled = true;
    try { await PUT("/settings", s); toast("Settings saved"); refreshStatus(); onSaved(); }
    catch (e) { toast("Not saved: " + e.message, true); }
    finally { saveBtn.disabled = false; }
  } }, "Save settings");
  const testBtn = h("button", { type: "button", onclick: async () => {
    testBtn.disabled = true;
    try {
      const r = await POST("/test-alert");
      if (r.errors.length) toast("Some alerts failed: " + r.errors.join("; "), true);
      else toast(`Test alert sent to ${r.sent_to.join(", ")}`);
    } catch (e) { toast(e.message, true); } finally { testBtn.disabled = false; }
  } }, "Send test alert");

  append(page, [
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Settings"),
      h("p", { class: "muted" }, "Where alerts go and how often live strategies are checked.")),
    h("div", { class: "actions" }, testBtn, saveBtn)),
    h("div", { class: "panel" },
      priceDataSection(s),
      h("section", { class: "section" }, h("h2", {}, "Alerts"),
        h("p", { class: "section-note" }, "Save your settings before sending a test alert."),
        h("div", { class: "channel" }, h("div", {}, h("h3", {}, "Phone (ntfy)"), h("p", { class: "small muted" }, "Install the ntfy app and subscribe to the same topic. On the public ntfy.sh server, pick a long random topic name.")),
          h("div", { class: "stack" }, enabled(n.ntfy, "Send alerts with ntfy"), h("div", { class: "fields" },
            text(n.ntfy, "server", "Server", { placeholder: "https://ntfy.sh" }),
            text(n.ntfy, "topic", "Topic"),
            text(n.ntfy, "token", "Access token", { secret: true, note: n.ntfy.token_set ? "Saved. Leave blank to keep it" : "Only needed if your server requires one" })))),
        h("div", { class: "channel" }, h("div", {}, h("h3", {}, "Discord"), h("p", { class: "small muted" }, "Create a webhook in your channel's settings.")),
          h("div", { class: "stack" }, enabled(n.discord, "Send alerts to Discord"), h("div", { class: "fields" },
            text(n.discord, "webhook_url", "Webhook URL", { secret: true })))),
        h("div", { class: "channel" }, h("div", {}, h("h3", {}, "Webhook"), h("p", { class: "small muted" }, "Sends title, body, priority and tags as JSON.")),
          h("div", { class: "stack" }, enabled(n.webhook, "Send alerts to a webhook"), h("div", { class: "fields" },
            text(n.webhook, "url", "URL")))),
        h("div", { class: "channel" }, h("div", {}, h("h3", {}, "Email"), h("p", { class: "small muted" }, "Through your own email account's SMTP server. Gmail and Outlook need an app password; Proton Mail needs Proton Bridge.")),
          h("div", { class: "stack" }, enabled(n.email, "Send alerts and reports by email"), h("div", { class: "fields" },
            text(n.email, "host", "SMTP server", { placeholder: "smtp.gmail.com" }),
            text(n.email, "port", "Port", { placeholder: "587" }),
            h("div", { class: "field" }, h("label", {}, "Security"), (() => {
              const sel = h("select", { onchange: (e) => { n.email.security = e.target.value; } },
                h("option", { value: "starttls" }, "STARTTLS (port 587)"), h("option", { value: "ssl" }, "SSL/TLS (port 465)"),
                h("option", { value: "none" }, "None (local servers only)"));
              sel.value = n.email.security || "starttls";
              return sel;
            })()),
            text(n.email, "username", "Username"),
            text(n.email, "password", "Password", { secret: true, note: n.email.password_set ? "Saved. Leave blank to keep it" : "Usually an app password" }),
            text(n.email, "sender", "From address", { note: "Blank uses the username" }),
            text(n.email, "to", "Send to", { placeholder: "you@example.com" })))),
        n.console ? h("div", { class: "channel" }, h("div", {}, h("h3", {}, "Server log")),
          h("div", {}, enabled(n.console, "Also print alerts in the server log"))) : null,
      ),
      appearanceSection(),
      h("section", { class: "section" }, h("h2", {}, "Checking"),
        h("div", { class: "fields" }, h("div", { class: "field" }, h("label", {}, "Look for newly closed candles"), poll)),
        h("p", { class: "after-note" }, "Checks run just after each interval so the candle that just closed is included. Shorter intervals only help strategies on short candles.")),
      reportSection(s),
      uploadsSection(),
      isHosted() ? null : newsSection(),
      backupSection(),
      accountSection()),
  ]);
  // A bar at the bottom while there are unsaved changes, so Save is always in reach.
  const bar = h("div", { class: "save-bar", hidden: true }, h("span", {}, "You have unsaved changes"),
    h("button", { type: "button", class: "quiet", onclick: () => { app.isDirty = null; app.guard = null; viewSettings(); } }, "Discard"),
    h("button", { type: "button", class: "primary", onclick: () => saveBtn.click() }, "Save settings"));
  const markDirty = (e) => {
    if (e.target.closest(".appearance")) return; // appearance saves straight away
    if (e.type === "click" && !e.target.closest(".source")) return;
    bar.hidden = false; app.isDirty = () => true;
    app.guard = () => "You have unsaved settings. Leave without saving?";
  };
  for (const t of ["input", "change", "click"]) page.addEventListener(t, markDirty);
  onSaved = () => { bar.hidden = true; app.isDirty = null; app.guard = null; };
  page.appendChild(bar);
  clear($main).appendChild(page);
}

// --------------------------------------------------------------------- undo instead of "are you sure?"

/**
 * Hide something straight away and delete it after a few seconds, unless Undo is pressed.
 * hide(true) hides it, hide(false) brings it back; run() does the real delete.
 */
function undoable(label, hide, run) {
  hide(true);
  const t = document.getElementById("toast");
  let undone = false;
  clear(t);
  const btn = h("button", { type: "button", class: "toast-action" }, "Undo");
  append(t, [h("span", {}, label), btn]);
  t.classList.remove("bad");
  t.hidden = true; void t.offsetWidth; t.hidden = false;
  clearTimeout(toastTimer);
  const finish = async () => {
    t.hidden = true;
    if (undone) return;
    try { await run(); } catch (e) { hide(false); toast(e.message, true); }
  };
  btn.addEventListener("click", () => { undone = true; hide(false); t.hidden = true; clearTimeout(toastTimer); });
  toastTimer = setTimeout(finish, 6000);
  // leaving the page doesn't cancel it
  app.pendingDeletes = (app.pendingDeletes || []).concat(() => { if (!undone) { clearTimeout(toastTimer); finish(); } });
}
window.addEventListener("beforeunload", () => { for (const f of app.pendingDeletes || []) f(); });

// --------------------------------------------------------------------- news calendar (settings)

function newsSection() {
  const sec = h("section", { class: "section" }, h("h2", {}, "News calendar"));
  const body = h("div");
  sec.appendChild(body);
  async function render(data) {
    const d = data || await GET("/news");
    clear(body);
    const update = h("button", { type: "button" }, "Update now");
    update.addEventListener("click", async () => {
      update.disabled = true;
      try { const r = await POST("/news/update"); toast(`Calendar updated: ${r.added} new events`); render(r); }
      catch (e) { toast(e.message, true); update.disabled = false; }
    });
    append(body, [
      h("p", { class: "section-note" }, "Used by the \"stay out around big news\" rule. The US jobs report is built in. Each week's high-impact events from the Forex Factory calendar are added automatically while the app runs, so the history used by backtests grows over time. You can also add events to the file below (time in UTC, currency, impact, title)."),
      h("p", { class: "small" }, d.saved_events ? `${d.saved_events} events saved, ${day(d.first)} to ${day(d.last)}.` : "No calendar events saved yet.",
        d.updated ? ` Last updated ${when(d.updated)}.` : ""),
      d.file ? h("p", { class: "small muted" }, "File: ", h("code", {}, d.file)) : null,
      h("div", { class: "inline check-row" }, update),
      d.upcoming.length ? h("div", {}, h("h3", { class: "picker-group" }, "High-impact news in the next 7 days"),
        h("ul", { class: "events" }, d.upcoming.slice(0, 12).map((e) => h("li", {}, h("span", { class: "muted" }, when(e.time)),
          h("div", {}, h("strong", {}, e.currency), " ", e.title))))) : null,
    ]);
  }
  render().catch((e) => clear(body).appendChild(h("p", { class: "error-box" }, e.message)));
  return sec;
}

// --------------------------------------------------------------------- edition helpers

/** The hosted (sign-in) edition, rather than the self-hosted app. */
function isHosted() { return !!app.session && app.session.edition === "cloud"; }
/** What this edition allows: data sources, minimum check interval, plan limits. */
function limits() { return (app.session && app.session.limits) || (app.meta && app.meta.limits) || {}; }

function accountSection() {
  if (isHosted() && window.cloudAccount) return window.cloudAccount.settingsSection();
  if (!app.session.auth_required) return null;
  return h("section", { class: "section" }, h("h2", {}, "Signing in"),
    h("p", { class: "section-note" }, "This copy of Trading Alerts is protected by the password in TRADEALERTS_PASSWORD. Changing it signs everyone out."),
    h("div", { class: "inline" }, h("button", { type: "button", onclick: signOut }, "Sign out")));
}

// --------------------------------------------------------------------- your own CSV price files

async function uploadCsv(file) {
  const res = await fetch("/api/uploads/" + encodeURIComponent(file.name), { method: "POST", body: file, credentials: "same-origin",
    headers: { "X-Requested-With": "tradealerts", "Content-Type": "text/csv" } });
  let body = {};
  try { body = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) throw new ApiError(body.detail || `Upload failed (${res.status})`);
  return body;
}

/** A button that uploads a CSV of candles, then calls done(result). */
function csvUploadButton(done, label = "Upload a CSV file…") {
  const file = h("input", { type: "file", accept: ".csv,text/csv", hidden: true });
  const btn = h("button", { type: "button", onclick: () => file.click() }, label);
  file.addEventListener("change", async () => {
    const f = file.files[0];
    file.value = "";
    if (!f) return;
    btn.disabled = true;
    try {
      const r = await uploadCsv(f);
      toast(`Uploaded ${r.name}: ${r.candles} candles${r.bar_minutes ? ` of ${r.bar_minutes} minutes` : ""}`);
      done(r);
    } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  });
  return h("span", {}, file, btn);
}

function uploadsSection() {
  const sec = h("section", { class: "section" }, h("h2", {}, "Your price files"));
  const body = h("div");
  sec.appendChild(body);
  async function render() {
    const r = await GET("/uploads");
    clear(body);
    append(body, [
      h("p", { class: "section-note" }, "CSV files of candles to test strategies on: a time column, then open, high, low and close (volume and spread optional). " +
        "Pick one in a strategy's Market, under My own CSV file."),
      r.files.length ? h("ul", { class: "events" }, r.files.map((f) => h("li", {},
        h("span", {}, h("strong", {}, f.name), h("span", { class: "small muted" }, ` ${(f.bytes / 1024 / 1024).toFixed(1)} MB, ${when(f.modified)}`)),
        h("button", { type: "button", class: "quiet small", onclick: () => undoable(`${f.name} deleted`, (hide) => { body.querySelectorAll("li").forEach((li) => { if (li.textContent.startsWith(f.name)) li.hidden = hide; }); },
          async () => { await DEL("/uploads/" + encodeURIComponent(f.name)); render(); }) }, "Delete"))))
        : h("p", { class: "small muted" }, "No files yet."),
      h("div", { class: "inline" }, csvUploadButton(() => render())),
      r.max_files ? h("p", { class: "after-note" }, `Up to ${r.max_files} files of ${r.max_mb} MB each.`) : null,
    ]);
  }
  render().catch((e) => clear(body).appendChild(h("p", { class: "error-box" }, e.message)));
  return sec;
}
