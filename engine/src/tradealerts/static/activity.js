/* Trading Alerts: the Activity page. Everything that happened, newest first, with filters.
   It doubles as the notifications centre: opening it marks everything as seen. */

"use strict";

const ACTIVITY_FILTERS = [
  ["all", "All"], ["trades", "Trades"], ["signals", "Signals"], ["skipped", "Skipped"],
  ["price", "Price alerts"], ["news", "News reminders"], ["changes", "Changes"], ["problems", "Problems"],
];
const ACTIVITY_ICON = { closed: "✓", opened: "↗", signal: "◆", skipped: "!", price: "◎", news: "🗓", error: "!",
  strategy_saved: "✎", settings_changed: "⚙", alert_failed: "!" };

async function viewActivity() {
  setNav("activity");
  const params = new URLSearchParams((location.hash.split("?")[1] || ""));
  let category = params.get("filter") || "all";
  if (!ACTIVITY_FILTERS.some(([k]) => k === category)) category = "all";
  let strategy = params.get("strategy") || "";
  let unreadOnly = params.get("unread") === "1";
  const seenBefore = { time: null };  // the seen time as it was when the page opened, so unread marks stay while reading

  const list = await GET("/strategies").catch(() => ({ strategies: [] }));
  const names = Object.fromEntries(list.strategies.map((x) => [x.id, (x.data || {}).name || x.id]));

  const chips = h("div", { class: "chips", role: "group", "aria-label": "Show" });
  const strategyPick = h("select", { "aria-label": "Strategy", onchange: () => { strategy = strategyPick.value; reload(); } },
    h("option", { value: "" }, "All strategies"),
    Object.entries(names).sort((a, b) => a[1].localeCompare(b[1])).map(([id, name]) => h("option", { value: id, selected: id === strategy || null }, name)));
  const unreadToggle = h("label", { class: "check" },
    h("input", { type: "checkbox", checked: unreadOnly || null, onchange: (e) => { unreadOnly = e.target.checked; reload(); } }),
    h("span", {}, "Only new since I last looked"));
  const out = h("div", { class: "activity-list", "aria-live": "polite" });
  const more = h("button", { type: "button", class: "quiet" }, "Show older");
  const foot = h("div", { class: "table-foot" }, more);

  function drawChips() {
    clear(chips);
    for (const [k, label] of ACTIVITY_FILTERS) {
      chips.appendChild(h("button", { type: "button", class: "chip", "aria-pressed": String(k === category),
        onclick: () => { category = k; reload(); } }, label));
    }
  }

  function item(x) {
    const tone = x.kind === "closed" || x.kind === "opened" ? "trade" : x.kind === "error" || x.kind === "alert_failed" ? "problem"
      : x.kind === "skipped" ? "warn" : x.kind === "signal" ? "signal" : "quiet";
    return h("li", { class: "activity-item " + tone + (x.unread ? " unread" : "") },
      h("span", { class: "activity-time muted" }, when(x.time)),
      h("span", { class: "activity-icon " + x.kind, "aria-hidden": "true" }, ACTIVITY_ICON[x.kind] || "•"),
      h("div", {}, h("a", { href: x.link, class: "activity-title" }, x.title),
        x.body ? h("div", { class: "small muted what" }, x.body) : null));
  }

  function updateUrl() {
    const q = new URLSearchParams();
    if (category !== "all") q.set("filter", category);
    if (strategy) q.set("strategy", strategy);
    if (unreadOnly) q.set("unread", "1");
    const qs = q.toString();
    history.replaceState(null, "", "#/activity" + (qs ? "?" + qs : ""));
  }

  async function fetchPage(before) {
    const q = new URLSearchParams({ category, limit: "50" });
    if (strategy) q.set("strategy", strategy);
    if (unreadOnly) q.set("unread", "1");
    if (before) q.set("before", before);
    return GET("/activity?" + q.toString());
  }

  function emptyText() {
    if (unreadOnly) return "Nothing new since you last looked.";
    if (category === "problems") return "No problems. Alerts are being sent and every strategy can be checked.";
    if (category === "trades") return "No paper trades yet. They appear here when a forward test opens or closes one.";
    if (category === "signals") return "No signals yet. One appears each time a strategy's rules are met.";
    if (category === "price") return "No price alerts have gone off. Set one from the Markets page.";
    if (category === "news") return "No news reminders have been sent. Set one from the News page.";
    if (category === "changes") return "No changes recorded yet. Saving a strategy or changing settings shows up here.";
    return "Nothing yet. Signals, trades, price alerts, news reminders and changes show up here as they happen.";
  }

  async function reload() {
    drawChips();
    updateUrl();
    clear(out).appendChild(skeleton());
    let res;
    try { res = await fetchPage(null); } catch (e) { clear(out).appendChild(h("p", { class: "error-box" }, e.message)); return; }
    if (seenBefore.time === null) {
      seenBefore.time = res.seen;
      if (res.unread_count) {
        try { await POST("/activity/seen"); refreshBell(); } catch (_) { /* counted next time */ }
      }
    }
    // show unread marks as they were when the page was opened, even after marking them seen
    const unreadAt = seenBefore.time;
    const mark = (x) => ({ ...x, unread: !unreadAt || String(x.time) > unreadAt });
    clear(out);
    if (res.now.length) {
      out.appendChild(h("section", { class: "section activity-now" }, h("h2", {}, "Needs attention now"),
        h("ul", { class: "activity" }, res.now.map(item))));
    }
    const ul = h("ul", { class: "activity" }, res.items.map((x) => item(mark(x))));
    if (!res.items.length && !res.now.length) out.appendChild(h("div", { class: "empty" }, h("p", {}, emptyText())));
    else out.appendChild(ul);
    foot.hidden = !res.more;
    more.disabled = false;
    more.onclick = async () => {
      more.disabled = true;
      const last = res.items[res.items.length - 1];
      try {
        const next = await fetchPage(last ? last.time : null);
        next.items.forEach((x) => ul.appendChild(item(mark(x))));
        res = { ...next, items: res.items.concat(next.items) };
        foot.hidden = !next.more;
      } catch (e) { toast(e.message, true); }
      finally { more.disabled = false; }
    };
  }

  clear($main);
  append($main, [
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Activity"),
        h("p", { class: "muted" }, "Everything your forward tests and alerts have done, newest first.")),
      h("div", { class: "actions" }, h("a", { class: "button", href: "#/" }, "Dashboard"))),
    h("div", { class: "panel" }, h("section", { class: "section" },
      h("div", { class: "activity-filters" }, chips, h("div", { class: "inline" }, strategyPick, unreadToggle)),
      out, foot)),
  ]);
  await reload();
}
