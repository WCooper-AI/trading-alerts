"""The economic calendar: upcoming events, your notes and reminders on them."""

from __future__ import annotations

import logging
import re
import threading

import pandas as pd
from fastapi import APIRouter, Body, Depends, Request

from ... import news
from ...recipes import MARKETS_BY_KEY
from ...strategy import StrategyError, load_strategy, strategy_files
from ...workspace import Workspace
from ..common import clean, ctx_of, err, workspace

log = logging.getLogger(__name__)
router = APIRouter()


def refresh_in_background(ctx) -> None:
    """Fetch the calendar if it's more than 6 hours old, without making the page wait."""
    if ctx.news_busy:
        return

    def work():
        try:
            news.update_from_feed(max_age_hours=6)
        except Exception as exc:
            log.info("News calendar not updated: %s", exc)
        finally:
            ctx.news_busy = False

    ctx.news_busy = True
    threading.Thread(target=work, daemon=True).start()


@router.get("/api/news/events")
def news_events(request: Request, days: int = 14, ws: Workspace = Depends(workspace)):
    ctx = ctx_of(request)
    refresh_in_background(ctx)
    evs = news.upcoming(max(1, min(days, 31)), impacts=("high", "medium", "low"), since_hours=18)
    with ws.store(create=False) as store:
        notes = store.news_notes() if store else {}
    for e in evs:
        n = notes.get(e["key"]) or {}
        e.update(note=n.get("note") or "", alert_minutes=n.get("alert_minutes"), alerted_at=n.get("alerted_at"))
    # currencies your strategies trade, to offer as a filter
    mine = set()
    for p in strategy_files(ws.config().strategies_dir).values():
        try:
            s = load_strategy(p)
        except StrategyError:
            continue
        if s.market.provider == "auto":
            mine.update(MARKETS_BY_KEY.get(s.market.symbol, {}).get("news", []))
    return clean({"events": evs, "status": news.status(show_file=ws.trusted), "my_currencies": sorted(mine),
                  "refreshing": ctx.news_busy})


@router.put("/api/news/events/{key}")
def save_news_note(key: str, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    if not re.fullmatch(r"[0-9a-f]{12}", key):
        err(404, "Unknown event")
    note = str(body.get("note") or "")[:2000]
    mins = body.get("alert_minutes")
    if mins is not None and (isinstance(mins, bool) or not isinstance(mins, int) or not 0 <= mins <= 1440):
        err(422, "The reminder must be between 0 and 1440 minutes before")
    for k in ("time", "currency", "title"):
        if not isinstance(body.get(k), str) or not body[k]:
            err(422, "Missing event details")
    try:
        t = pd.Timestamp(body["time"])
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
    except ValueError:
        err(422, "Missing event details")
    if news.event_key(t, body["currency"], body["title"]) != key:
        err(422, "Those event details don't match")
    with ws.store() as store:
        return store.save_news_note(key, body["time"], body["currency"][:8], body["title"][:200], note, mins)


@router.get("/api/news")
def news_status(ws: Workspace = Depends(workspace)):
    return clean({**news.status(show_file=ws.trusted), "upcoming": news.upcoming(7)})


@router.post("/api/news/update")
def news_update(ws: Workspace = Depends(workspace)):
    try:
        # the hosted server shares one calendar: customers can't make it fetch more than hourly
        added = news.update_from_feed(max_age_hours=0 if ws.trusted else 1)
    except Exception as exc:
        err(502, f"Couldn't fetch the news calendar: {exc}")
    return clean({"added": added, **news.status(show_file=ws.trusted), "upcoming": news.upcoming(7)})
