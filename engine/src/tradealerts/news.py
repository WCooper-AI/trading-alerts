"""Economic calendar, for the "avoid news" rule.

Events come from three places:

* Built in: the US jobs report (non-farm payrolls), released at 08:30 New York time,
  normally on the first Friday of the month. The schedule is approximate: the
  Bureau of Labor Statistics occasionally moves it a week.
* The weekly calendar feed from faireconomy.media (the data behind Forex Factory),
  fetched by the live checker about twice a day. Each week is added to
  ``news_events.csv`` in the data folder, so history builds up over time.
* Anything you add to that CSV yourself (columns: time, currency, impact, title;
  time in UTC, e.g. 2025-03-07T13:30:00Z), for example a calendar exported elsewhere.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FEED_URLS = (FEED_URL, "https://nfs.faireconomy.media/ff_calendar_nextweek.json")
FILE = "news_events.csv"
COLUMNS = ["time", "currency", "impact", "title"]
EXTRA = ["forecast", "previous"]
IMPACTS = {"high": 3, "medium": 2, "low": 1}

DATA_DIR: Path | None = None  # set when settings are loaded
_lock = threading.Lock()
_cache: dict = {"key": None, "events": None}


def set_data_dir(path: Path) -> None:
    global DATA_DIR
    DATA_DIR = Path(path)


def us_jobs_reports(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """First Friday of each month, 08:30 New York time (approximate schedule)."""
    rows = []
    month = pd.Timestamp(start.year, start.month, 1)
    last = pd.Timestamp(end.year, end.month, 1)
    while month <= last:
        first_friday = month + pd.Timedelta(days=(4 - month.day_of_week) % 7)
        t = pd.Timestamp(f"{first_friday:%Y-%m-%d} 08:30", tz="America/New_York").tz_convert("UTC")
        rows.append({"time": t, "currency": "USD", "impact": "high", "title": "Non-Farm Employment Change",
                     "forecast": "", "previous": ""})
        month = month + pd.offsets.MonthBegin(1)
    return pd.DataFrame(rows, columns=COLUMNS + EXTRA)


def _file() -> Path | None:
    return DATA_DIR / FILE if DATA_DIR else None


def load_file(path: Path | None = None) -> pd.DataFrame:
    path = path or _file()
    if not path or not path.exists():
        return pd.DataFrame(columns=COLUMNS + EXTRA)
    try:
        df = pd.read_csv(path, dtype={"forecast": str, "previous": str})
        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        df["currency"] = df["currency"].astype(str).str.upper().str.strip()
        df["impact"] = df["impact"].astype(str).str.lower().str.strip()
        for c in EXTRA:
            df[c] = df[c].fillna("").astype(str) if c in df.columns else ""
        return df.dropna(subset=["time"])[COLUMNS + EXTRA]
    except (ValueError, KeyError, OSError) as exc:
        log.warning("Couldn't read %s: %s", path, exc)
        return pd.DataFrame(columns=COLUMNS + EXTRA)


def events(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """All known events between start and end (UTC), built-in and from the CSV."""
    path = _file()
    stamp = path.stat().st_mtime if path and path.exists() else None
    with _lock:
        if _cache["key"] != (str(path), stamp):
            _cache["key"], _cache["events"] = (str(path), stamp), load_file(path)
        saved = _cache["events"]
    built = us_jobs_reports(start - pd.Timedelta(days=40), end + pd.Timedelta(days=40))
    both = pd.concat([x for x in (saved, built) if len(x)], ignore_index=True) if len(saved) or len(built) \
        else pd.DataFrame(columns=COLUMNS + EXTRA)
    both["time"] = pd.to_datetime(both["time"], utc=True)
    for c in EXTRA:
        both[c] = both[c].fillna("") if c in both.columns else ""
    both = both[(both["time"] >= start - pd.Timedelta(days=2)) & (both["time"] <= end + pd.Timedelta(days=2))]
    # the feed also lists the jobs report, under the same name: keep the feed's copy (it has forecasts)
    both = both.assign(_m=both["time"].dt.floor("min"))
    both = both.sort_values("forecast", ascending=False, key=lambda c: c.astype(str).str.len())
    both = both.drop_duplicates(["_m", "currency", "title"]).drop(columns="_m")
    return both.sort_values("time").reset_index(drop=True)


def parse_feed(items: list[dict]) -> pd.DataFrame:
    rows = []
    for it in items:
        impact = str(it.get("impact", "")).lower()
        if impact not in IMPACTS:
            continue  # holidays and the like
        try:
            t = pd.Timestamp(it["date"]).tz_convert("UTC")
        except (KeyError, ValueError, TypeError):
            continue
        rows.append({"time": t, "currency": str(it.get("country", "")).upper(), "impact": impact,
                     "title": str(it.get("title", "")), "forecast": str(it.get("forecast") or ""),
                     "previous": str(it.get("previous") or "")})
    return pd.DataFrame(rows, columns=COLUMNS + EXTRA)


_update_lock = threading.Lock()
RETRY_AFTER_FAILURE = 3600  # seconds: don't hammer the feed while it's down


def update_from_feed(path: Path | None = None, opener=None, max_age_hours: float = 12) -> int:
    """Fetch this week's calendar and add it to the CSV. Returns events added (0 if fresh)."""
    path = path or _file()
    if not path:
        return 0
    marker = path.with_suffix(".updated")
    failed = path.with_suffix(".failed")
    with _update_lock:  # the live checker and the web interface may both ask at once
        if marker.exists() and time.time() - marker.stat().st_mtime < max_age_hours * 3600:
            return 0
        if max_age_hours and failed.exists() and time.time() - failed.stat().st_mtime < RETRY_AFTER_FAILURE:
            return 0
        open_ = opener or urllib.request.urlopen
        items = []
        for i, url in enumerate(FEED_URLS):
            req = urllib.request.Request(url, headers={"User-Agent": "TradingAlerts/0.2"})
            try:
                with open_(req, timeout=20) as resp:
                    items += json.loads(resp.read().decode("utf-8"))
            except Exception:  # next week's list isn't always published yet
                if i == 0:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    failed.touch()
                    raise
        new = parse_feed(items)
        old = load_file(path)
        both = pd.concat([x for x in (old, new) if len(x)], ignore_index=True) if len(old) or len(new) \
            else pd.DataFrame(columns=COLUMNS + EXTRA)
        # newer copies win: forecasts get filled in and revised during the week
        both = both.drop_duplicates(["time", "currency", "title"], keep="last").sort_values("time")
        added = len(both) - len(old)
        path.parent.mkdir(parents=True, exist_ok=True)
        out = both.assign(time=both["time"].map(lambda t: pd.Timestamp(t).isoformat()))
        tmp = path.with_name(path.name + ".tmp")
        out.to_csv(tmp, index=False)
        tmp.replace(path)
        marker.touch()
        failed.unlink(missing_ok=True)
        return max(0, added)


def blocked(index: pd.DatetimeIndex, bar: pd.Timedelta, before_min: float, after_min: float,
            currencies: list[str], impact: str = "high") -> pd.Series:
    """True for candles too close to an event: from ``after_min`` after an event until
    ``before_min`` before the next one, counting from the candle's close (when a trade would open)."""
    out = pd.Series(False, index=index)
    if not len(index):
        return out
    ev = events(index[0], index[-1] + bar)
    need = IMPACTS.get(impact, 3)
    ev = ev[ev["impact"].map(lambda x: IMPACTS.get(x, 0)) >= need]
    if currencies:
        ev = ev[ev["currency"].isin([c.upper() for c in currencies])]
    if ev.empty:
        return out
    import numpy as np

    starts = index.as_unit("ns").asi8
    ends = starts + bar.value
    times = np.sort(pd.to_datetime(ev["time"], utc=True).dt.as_unit("ns").astype("int64").to_numpy())
    # blocked if an event falls between (candle start - after) and (candle close + before)
    first = np.searchsorted(times, starts - int(after_min * 60e9), "left")
    last = np.searchsorted(times, ends + int(before_min * 60e9), "right")
    out[:] = last > first
    return out


def event_key(t: pd.Timestamp, currency: str, title: str) -> str:
    import hashlib
    raw = f"{pd.Timestamp(t).tz_convert('UTC'):%Y-%m-%dT%H:%M}|{currency}|{title}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def upcoming(days: int = 7, currencies: list[str] | None = None, impacts: tuple = ("high",),
             since_hours: float = 2) -> list[dict]:
    now = pd.Timestamp.now(tz="UTC")
    ev = events(now - pd.Timedelta(hours=since_hours), now + pd.Timedelta(days=days))
    ev = ev[(ev["time"] >= now - pd.Timedelta(hours=since_hours)) & (ev["impact"].isin(impacts))]
    if currencies:
        ev = ev[ev["currency"].isin(currencies)]
    out = []
    for r in ev.itertuples():
        out.append({"key": event_key(r.time, r.currency, r.title), "time": r.time.isoformat(), "currency": r.currency,
                    "impact": r.impact, "title": r.title, "forecast": r.forecast or "", "previous": r.previous or "",
                    "info": describe(r.title)})
    return out


# What common events are, in plain English. Matched on words in the calendar's title.
ABOUT = [
    (("non-farm", "nonfarm", "jobs report"), "US jobs report",
     "How many jobs the US economy added last month, outside farming. One of the biggest market movers: "
     "a strong number can mean interest rates stay higher for longer, which tends to lift the dollar and can weigh on gold and shares."),
    (("unemployment rate", "claimant count"), "Unemployment",
     "The share of people looking for work. Rising unemployment points to a slowing economy and makes interest rate cuts more likely."),
    (("jobless claims",), "US weekly jobless claims",
     "How many people signed on for unemployment benefits last week. A quick, noisy read on the US jobs market."),
    (("core pce",), "US core PCE inflation",
     "The Federal Reserve's preferred inflation measure, leaving out food and energy. Higher than expected tends to push rate-cut hopes back."),
    (("cpi",), "Consumer price inflation (CPI)",
     "How much the prices people pay rose over the month or year. Inflation drives interest rate decisions, so surprises often move currencies, indices and gold quickly."),
    (("ppi",), "Producer prices (PPI)",
     "Prices businesses receive for their goods. Often seen as an early hint of where consumer inflation is heading."),
    (("fomc", "federal funds rate", "fed chair", "powell"), "US Federal Reserve",
     "The US central bank's interest rate decision, statement or speech. Rate changes, and hints about future ones, move almost every market."),
    (("official bank rate", "boe", "mpc"), "Bank of England",
     "The UK interest rate decision and votes. Moves the pound and UK shares in particular."),
    (("main refinancing rate", "ecb", "lagarde", "monetary policy statement"), "European Central Bank",
     "The eurozone interest rate decision and press conference. Moves the euro and European indices like the Germany 40."),
    (("cash rate", "rba"), "Reserve Bank of Australia", "Australia's interest rate decision."),
    (("boj", "policy rate"), "Bank of Japan", "Japan's interest rate decision. Can move the yen sharply."),
    (("gdp",), "GDP (economic growth)",
     "How much the economy grew. The first estimate usually moves markets most; later revisions less."),
    (("retail sales",), "Retail sales", "How much shoppers spent. A read on consumer demand, a big part of most economies."),
    (("pmi", "ism"), "Business surveys (PMI)",
     "Purchasing managers say whether business is growing (above 50) or shrinking (below 50). An early read on the economy."),
    (("crude oil inventories", "oil inventories"), "US oil stocks",
     "Weekly change in US crude oil stored. A surprise build or draw often moves the oil price within minutes."),
    (("consumer sentiment", "consumer confidence"), "Consumer confidence", "How upbeat households are about money and the economy."),
    (("trade balance",), "Trade balance", "Exports minus imports. Usually a smaller market mover."),
    (("bank holiday", "holiday"), "Market holiday", "Some markets are closed or quiet, so prices can move oddly on thin trading."),
]


def describe(title: str) -> dict | None:
    t = title.lower()
    for words, name, text in ABOUT:
        if any(w in t for w in words):
            return {"name": name, "text": text}
    return None


def status(show_file: bool = True) -> dict:
    path = _file()
    saved = load_file(path)
    marker = path.with_suffix(".updated") if path else None
    return {
        "file": str(path) if path and show_file else None,
        "saved_events": int(len(saved)),
        "first": saved["time"].min().isoformat() if len(saved) else None,
        "last": saved["time"].max().isoformat() if len(saved) else None,
        "updated": pd.Timestamp(marker.stat().st_mtime, unit="s", tz="UTC").isoformat()
        if marker and marker.exists() else None,
    }


def send_reminders(store, notifiers, now: pd.Timestamp | None = None) -> list[str]:
    """Phone alerts for events you asked to be reminded about. Returns the keys sent."""
    from .notify import Message, broadcast

    now = now or pd.Timestamp.now(tz="UTC")
    sent = []
    for r in store.due_news_reminders(now.isoformat()):
        t = pd.Timestamp(r["time"])
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        due = t - pd.Timedelta(minutes=r["alert_minutes"])
        if now < due:
            continue
        if now > t + pd.Timedelta(hours=1):  # missed (app wasn't running): don't alert late
            store.mark_news_reminded(r["key"], now.isoformat(timespec="seconds"))
            continue
        mins = max(0, round((t - now) / pd.Timedelta(minutes=1)))
        lead = "now" if mins == 0 else f"in {mins} minute{'s' if mins != 1 else ''}"
        body = f"{r['currency']} {r['title']} is {lead} ({t:%H:%M} UTC)." + (f"\nYour note: {r['note']}" if r.get("note") else "")
        title = f"News {lead}: {r['currency']} {r['title']}"
        errors = broadcast(notifiers, Message(title, body, "high", ("newspaper",)))
        if errors:
            store.add_event("", now.isoformat(timespec="seconds"), "alert_failed",
                            f"Alert not sent: {title}\n" + "; ".join(errors), False)
        store.mark_news_reminded(r["key"], now.isoformat(timespec="seconds"))
        sent.append(r["key"])
    return sent
