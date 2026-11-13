"use strict";

/* Trading Alerts web interface: Upcoming economic news, notes and reminders.
   Loaded as plain scripts in order (see SCRIPTS in web/app.py); they share one scope. */

// --------------------------------------------------------------------- news: upcoming economic events

const IMPACT_LABEL = { high: "High impact", medium: "Medium", low: "Low" };
const REMINDERS = [[null, "No reminder"], [0, "When it happens"], [5, "5 minutes before"], [15, "15 minutes before"],
  [30, "30 minutes before"], [60, "1 hour before"], [240, "4 hours before"], [1440, "The day before"]];

function untilText(iso) {
  const ms = new Date(iso) - Date.now();
  if (ms < -60000) return ms > -3600000 ? `${Math.round(-ms / 60000)} min ago` : "earlier";
  if (ms < 60000) return "now";
  const m = Math.round(ms / 60000);
  if (m < 60) return `in ${m} min`;
  const hrs = Math.floor(m / 60), rest = m % 60;
  if (hrs < 24) return `in ${hrs}h${rest ? ` ${rest}m` : ""}`;
  const d = Math.round(hrs / 24);
  return `in ${d} day${d === 1 ? "" : "s"}`;
}

function dayLabel(iso) {
  const d = new Date(iso);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(d); that.setHours(0, 0, 0, 0);
  const diff = Math.round((that - today) / 86400000);
  const date = d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
  return diff === 0 ? `Today · ${date}` : diff === 1 ? `Tomorrow · ${date}` : diff === -1 ? `Yesterday · ${date}` : date;
}

async function viewNews() {
  setNav("news");
  const res = await GET("/news/events?days=14");
  const all = res.events;
  const state = {
    impacts: new Set(pref("ta-news-impact", "high,medium").split(",").filter(Boolean)),
    currencies: new Set(pref("ta-news-cur", "").split(",").filter(Boolean)),
    q: "", open: null,
  };
  const currencies = [...new Set(all.map((e) => e.currency))].filter(Boolean).sort();
  const page = h("div", { class: "stack" });
  const hero = h("div", { class: "panel news-hero" });
  const listBox = h("div", { class: "news-list" });
  const save = () => { savePref("ta-news-impact", [...state.impacts].join(",")); savePref("ta-news-cur", [...state.currencies].join(",")); };

  // filters
  const impactChips = h("div", { class: "chips", role: "group", "aria-label": "Impact" });
  const curChips = h("div", { class: "chips", role: "group", "aria-label": "Currencies" });
  function renderFilters() {
    clear(impactChips);
    for (const k of ["high", "medium", "low"]) impactChips.appendChild(h("button", { type: "button", class: "chip impact-" + k,
      "aria-pressed": String(state.impacts.has(k)), onclick: () => {
        if (state.impacts.has(k)) state.impacts.delete(k); else state.impacts.add(k);
        save(); renderFilters(); renderList();
      } }, h("span", { class: "impact-dot " + k }), IMPACT_LABEL[k]));
    clear(curChips);
    curChips.appendChild(h("button", { type: "button", class: "chip", "aria-pressed": String(!state.currencies.size),
      onclick: () => { state.currencies.clear(); save(); renderFilters(); renderList(); } }, "All currencies"));
    if (res.my_currencies.length) curChips.appendChild(h("button", { type: "button", class: "chip",
      "aria-pressed": String(state.currencies.size && res.my_currencies.every((c) => state.currencies.has(c)) && state.currencies.size === res.my_currencies.length),
      title: "The currencies behind the markets your strategies trade",
      onclick: () => { state.currencies = new Set(res.my_currencies); save(); renderFilters(); renderList(); } }, "My markets"));
    for (const c of currencies) curChips.appendChild(h("button", { type: "button", class: "chip", "aria-pressed": String(state.currencies.has(c)),
      onclick: () => {
        if (state.currencies.has(c)) state.currencies.delete(c); else state.currencies.add(c);
        save(); renderFilters(); renderList();
      } }, c));
  }
  const search = h("input", { type: "search", class: "search", placeholder: "Search events", "aria-label": "Search events",
    oninput: (e) => { state.q = e.target.value.trim().toLowerCase(); renderList(); } });

  const visible = () => all.filter((e) => state.impacts.has(e.impact) &&
    (!state.currencies.size || state.currencies.has(e.currency)) &&
    (!state.q || `${e.title} ${e.currency} ${(e.info || {}).name || ""}`.toLowerCase().includes(state.q)));

  function renderHero() {
    clear(hero);
    const next = all.find((e) => e.impact === "high" && new Date(e.time) > Date.now() - 60000 &&
      (!state.currencies.size || state.currencies.has(e.currency)));
    if (!next) {
      hero.appendChild(h("section", { class: "section" }, h("p", { class: "muted" }, state.currencies.size ? "No high-impact news coming up for the chosen currencies. Lower-impact events are listed below."
        : "No high-impact news coming up in the calendar. Lower-impact events are listed below.")));
      return;
    }
    append(hero, [h("section", { class: "section hero-row" },
      h("div", {}, h("p", { class: "eyebrow" }, "Next high-impact event"),
        h("h2", { class: "hero-title" }, h("span", { class: "cur-badge" }, next.currency), " ", next.title),
        h("p", { class: "muted" }, `${new Date(next.time).toLocaleString("en-GB", { weekday: "long", hour: "2-digit", minute: "2-digit" })}` +
          (next.forecast ? ` · forecast ${next.forecast}` : "") + (next.previous ? ` · previous ${next.previous}` : ""))),
      h("div", { class: "countdown" }, untilText(next.time)))]);
  }

  function eventRow(e) {
    const li = h("li", { class: "news-item" + (state.open === e.key ? " open" : "") + (new Date(e.time) < Date.now() - 60000 ? " past" : "") });
    const head = h("button", { type: "button", class: "news-head", "aria-expanded": String(state.open === e.key),
      onclick: () => { state.open = state.open === e.key ? null : e.key; renderList(); } },
      h("span", { class: "news-time" }, new Date(e.time).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })),
      h("span", { class: "impact-dot " + e.impact, title: IMPACT_LABEL[e.impact] }),
      h("span", { class: "cur-badge" }, e.currency),
      h("span", { class: "news-title" }, e.title, e.info ? h("span", { class: "small muted" }, ` · ${e.info.name}`) : null),
      h("span", { class: "news-figs small muted", title: "Forecast and previous figure" },
        [e.forecast ? `Fcst ${e.forecast}` : "", e.previous ? `Prev ${e.previous}` : ""].filter(Boolean).join(" · ")),
      h("span", { class: "news-flags" },
        e.note ? h("span", { class: "flag", title: "You've added a note" }, "✎") : null,
        e.alert_minutes !== null && e.alert_minutes !== undefined ? h("span", { class: "flag on", title: "Reminder set" }, "🔔") : null),
      h("span", { class: "news-until small" }, untilText(e.time)));
    li.appendChild(head);
    if (state.open === e.key) li.appendChild(eventDetail(e));
    return li;
  }

  function eventDetail(e) {
    const note = h("textarea", { placeholder: "Anything to remember for this one: positions to close, levels to watch…", "aria-label": "Your note" }, e.note || "");
    const remind = h("select", { "aria-label": "Reminder" },
      REMINDERS.map(([v, t]) => h("option", { value: v === null ? "" : String(v) }, t)));
    remind.value = e.alert_minutes === null || e.alert_minutes === undefined ? "" : String(e.alert_minutes);
    const status = h("span", { class: "small muted", "aria-live": "polite" });
    async function persist() {
      const mins = remind.value === "" ? null : Number(remind.value);
      try {
        const r = await PUT(`/news/events/${e.key}`, { time: e.time, currency: e.currency, title: e.title, note: note.value, alert_minutes: mins });
        e.note = r.note; e.alert_minutes = r.alert_minutes; e.alerted_at = r.alerted_at;
        status.textContent = "Saved";
        setTimeout(() => { status.textContent = ""; }, 1500);
        const flags = note.closest(".news-item").querySelector(".news-flags");
        clear(flags);
        if (e.note) flags.appendChild(h("span", { class: "flag", title: "You've added a note" }, "✎"));
        if (e.alert_minutes !== null) flags.appendChild(h("span", { class: "flag on", title: "Reminder set" }, "🔔"));
      } catch (ex) { status.textContent = ex.message; }
    }
    let t;
    note.addEventListener("input", () => { clearTimeout(t); t = setTimeout(persist, 700); });
    remind.addEventListener("change", () => {
      persist();
      if (remind.value !== "") toast("You'll get a phone alert " + (remind.value === "0" ? "when it happens" : REMINDERS.find(([v]) => String(v) === remind.value)[1].toLowerCase()));
    });
    const when2 = new Date(e.time);
    return h("div", { class: "news-detail" },
      h("div", { class: "news-about" },
        e.info ? [h("h3", {}, e.info.name), h("p", {}, e.info.text)]
          : h("p", { class: "muted" }, "No description for this one yet. Check the impact level and how the market moved last time."),
        h("dl", { class: "news-facts" },
          h("dt", {}, "When"), h("dd", {}, when2.toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })),
          h("dt", {}, "Currency"), h("dd", {}, e.currency),
          h("dt", {}, "Impact"), h("dd", {}, IMPACT_LABEL[e.impact]),
          e.forecast ? [h("dt", {}, "Forecast", infoTip("What economists expect. Markets move most when the actual number is far from this.")), h("dd", {}, e.forecast)] : null,
          e.previous ? [h("dt", {}, "Previous"), h("dd", {}, e.previous)] : null)),
      h("div", { class: "news-mine" },
        h("div", { class: "field" }, h("label", {}, "Your note"), note),
        h("div", { class: "field" }, h("label", {}, "Phone reminder", infoTip("Sent through your alert settings (ntfy, Discord or webhook) while Altius is running.")), remind),
        e.alerted_at ? h("p", { class: "small muted" }, `Reminder sent ${when(e.alerted_at)}.`) : null,
        status));
  }

  function renderList() {
    const shown = visible();
    clear(listBox);
    if (!shown.length) {
      listBox.appendChild(h("div", { class: "panel" }, h("div", { class: "empty" }, h("h2", {}, all.length ? "Nothing matches" : "No events yet"),
        h("p", { class: "muted" }, all.length ? "Try other impact levels or currencies." :
          "The calendar downloads in the background while the app runs. Check back in a minute, or press Update now."))));
      return;
    }
    const days = new Map();
    for (const e of shown) {
      const k = new Date(e.time).toDateString();
      if (!days.has(k)) days.set(k, []);
      days.get(k).push(e);
    }
    for (const evs of days.values()) {
      listBox.appendChild(h("div", { class: "panel news-day" }, h("h2", { class: "day-title" }, dayLabel(evs[0].time),
        h("span", { class: "small muted" }, `${evs.length} event${evs.length === 1 ? "" : "s"}`)),
        h("ul", { class: "news-items" }, evs.map(eventRow))));
    }
  }

  const st = res.status;
  const update = h("button", { type: "button" }, "Update now");
  update.addEventListener("click", async () => {
    update.disabled = true;
    try { const r = await POST("/news/update"); toast(`Calendar updated: ${r.added} new events`); viewNews(); }
    catch (e) { toast(e.message, true); update.disabled = false; }
  });
  append(page, [
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "News"),
      h("p", { class: "muted dash-lede" }, "Scheduled economic news for the next two weeks, in your local time. Open an event to read what it is, keep a note, or get a reminder before it happens.")),
      h("div", { class: "actions" }, h("span", { class: "small muted" }, st.updated ? `Updated ${when(st.updated)}` : res.refreshing ? "Downloading…" : ""), update)),
    hero,
    h("div", { class: "panel" }, h("div", { class: "toolbar" }, search, impactChips), h("div", { class: "toolbar sub" }, curChips)),
    listBox,
    h("p", { class: "small muted" }, "Calendar from Forex Factory (faireconomy.media), plus the US jobs report worked out in advance. Times can change; it's a guide. " +
      "Events are kept, so the \"stay out around news\" rule can use them in backtests. You can add your own to ",
      h("code", {}, st.file || "news_events.csv"), "."),
  ]);
  renderFilters(); renderHero(); renderList();
  clear($main).appendChild(page);
  every(30, () => {
    renderHero();
    // refresh the "in 2h 15m" labels without closing an open note
    listBox.querySelectorAll(".news-until").forEach((elx, i) => { const e = visible()[i]; if (e) elx.textContent = untilText(e.time); });
  });
}
