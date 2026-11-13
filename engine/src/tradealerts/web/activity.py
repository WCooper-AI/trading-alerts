"""The activity log: everything that happened, newest first, in one list.

Forward-test events (signals, trades, skipped signals, saves, failed alerts) come from the
events table; price alerts and news reminders from their own tables; problems with the
checker from the runner. It also serves as the notifications centre: what's newer than
the time the person last looked counts as unread, and that time is kept on the server so
the browser and the desktop app agree.
"""

from __future__ import annotations

from datetime import datetime, timezone

# filter -> event kinds in the events table
CATEGORIES = {
    "trades": ["opened", "closed", "near_level"],
    "signals": ["signal"],
    "skipped": ["skipped"],
    "changes": ["strategy_saved", "settings_changed"],
    "problems": ["alert_failed", "data_problem", "drawdown"],
}
EXTRA = {"price", "news"}  # categories that come from other tables
KIND_TITLE = {"signal": "Rules met", "opened": "Trade opened", "closed": "Trade closed", "skipped": "Signal skipped",
              "strategy_saved": "Strategy saved", "alert_failed": "Alert not sent",
              "data_problem": "Price data problem", "drawdown": "Drawdown limit hit", "near_level": "Near stop or target"}
CATEGORY_OF = {kind: cat for cat, kinds in CATEGORIES.items() for kind in kinds}
SEEN_KEY = "activity_seen"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _event_item(e: dict, names: dict[str, str]) -> dict:
    kind, sid = e["kind"], e.get("strategy_id") or ""
    lines = (e.get("message") or "").split("\n")
    name = names.get(sid) or sid
    if kind in ("strategy_saved", "settings_changed", "alert_failed"):
        title, body = lines[0], "\n".join(lines[1:]).strip()
        if kind == "strategy_saved" and name:
            title = f"{name}: {title[0].lower()}{title[1:]}" if title else name
    else:
        title = f"{KIND_TITLE.get(kind, kind)}: {name}"
        body = "\n".join(lines[1:]).strip()
    if kind == "settings_changed":
        link = "#/settings"
    elif kind == "strategy_saved" and sid:
        link = f"#/history/{sid}"
    elif e.get("run_id"):
        link = f"#/run/{e['run_id']}"
    else:
        link = f"#/s/{sid}" if sid else "#/settings"
    return {"id": f"e{e['id']}", "time": e["created_at"], "kind": kind, "category": CATEGORY_OF.get(kind, "other"),
            "title": title, "body": body, "link": link, "strategy_id": sid or None}


def _price_items(store) -> list[dict]:
    out = []
    for a in store.price_alerts():
        if not a.get("triggered_at"):
            continue
        word = "above" if a["direction"] == "above" else "below"
        out.append({"id": f"p{a['id']}", "time": a.get("checked_to") or a["triggered_at"], "kind": "price",
                    "category": "price", "title": f"{a.get('label') or a['market']} went {word} {a['price']:,.5g}",
                    "body": a.get("note") or "", "link": "#/markets", "strategy_id": None})
    return out


def _news_items(store) -> list[dict]:
    return [{"id": f"n{n['key']}", "time": n["alerted_at"], "kind": "news", "category": "news",
             "title": f"Reminder: {n['currency']} {n['title']}", "body": n.get("note") or "", "link": "#/news",
             "strategy_id": None}
            for n in store.news_notes().values() if n.get("alerted_at")]


def _problem_items(runner_status: dict, names: dict[str, str]) -> list[dict]:
    """Problems happening now (not history): the checker failing, or a strategy that can't be checked.
    They're reported again at every check, so they never count as unread; the page shows them first."""
    at = runner_status.get("last_run") or _now()
    out = []
    if runner_status.get("last_error"):
        out.append({"id": "checker", "time": at, "kind": "error", "category": "problems", "title": "A check failed",
                    "body": runner_status["last_error"], "link": "#/settings", "strategy_id": None})
    for sid, text in (runner_status.get("problems") or {}).items():
        out.append({"id": f"problem-{sid}", "time": at, "kind": "error", "category": "problems",
                    "title": f"{names.get(sid) or sid} can't be checked", "body": text, "link": f"#/s/{sid}",
                    "strategy_id": sid})
    return out


def items(store, runner_status: dict, names: dict[str, str], *, category: str = "all",
          strategy_id: str | None = None, before: str | None = None, unread_only: bool = False,
          limit: int = 50) -> dict:
    """One page of the log. ``names`` maps strategy ids to their names."""
    names = {**{r["strategy_id"]: r.get("name") for r in store.runs()}, **names}  # deleted ones keep a name
    seen = seen_time(store)
    after = seen if unread_only else None
    out: list[dict] = []
    if category == "all" or category in CATEGORIES:
        kinds = None if category == "all" else CATEGORIES[category]
        out += [_event_item(e, names)
                for e in store.events_page(kinds, strategy_id, before=before, after=after, limit=limit)]
    if not strategy_id:
        if category in ("all", "price"):
            out += _price_items(store)
        if category in ("all", "news"):
            out += _news_items(store)
    if before:
        out = [x for x in out if str(x["time"]) < before]
    for x in out:
        x["unread"] = not seen or str(x["time"]) > seen
    if unread_only:
        out = [x for x in out if x["unread"]]
    out.sort(key=lambda x: str(x["time"]), reverse=True)
    page = out[:limit]
    now = []
    if category in ("all", "problems") and not before:
        now = [p for p in _problem_items(runner_status, names) if not strategy_id or p["strategy_id"] == strategy_id]
        for p in now:
            p["unread"] = False
    return {"items": page, "now": now, "more": len(out) > limit, "seen": seen,
            "unread_count": unread_count(store, seen)}


def unread_count(store, seen: str) -> int:
    n = len(store.events_page(after=seen or None, limit=100))
    return n + sum(1 for x in _price_items(store) + _news_items(store) if not seen or str(x["time"]) > seen)


def seen_time(store) -> str:
    return store.get_value(SEEN_KEY, "") or ""


def mark_seen(store, until: str | None = None) -> str:
    """Everything up to ``until`` (default now) counts as read. Never moves backwards."""
    until = until or _now()
    if until > seen_time(store):
        store.set_value(SEEN_KEY, until)
    return seen_time(store)
