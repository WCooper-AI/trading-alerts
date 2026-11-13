"""When markets are shut, so the app can say "waiting" instead of looking stuck.
Most CFD markets (currencies, metals, indices) pause from 17:00 to 18:00 New York time
every day, stop at 17:00 on Friday and start again at 18:00 on Sunday. Crypto never stops.
Stocks only trade in exchange hours: with the optional ``exchange_calendars`` package
(``pip install 'tradealerts[stocks]'``) the real calendar is used, holidays included;
without it a plain Monday to Friday 09:30-16:00 New York timetable stands in."""

from __future__ import annotations

from functools import lru_cache

import pandas as pd

from .recipes import MARKETS_BY_KEY

NY = "America/New_York"
CRYPTO_HINTS = ("BTC", "ETH", "SOL", "XRP", "LTC", "DOGE", "VCCY")
# fallback timetables when exchange_calendars isn't installed: (timezone, open, close), Monday to Friday
EXCHANGE_FALLBACK = {
    "XNYS": (NY, (9, 30), (16, 0)),
    "XNAS": (NY, (9, 30), (16, 0)),
    "XLON": ("Europe/London", (8, 0), (16, 30)),
    "XETR": ("Europe/Berlin", (9, 0), (17, 30)),
}


def exchange_for(symbol: str) -> str | None:
    """The exchange_calendars code of a market that only trades in exchange hours, else None."""
    mk = MARKETS_BY_KEY.get(symbol)
    return (mk or {}).get("exchange") or None


@lru_cache(maxsize=16)
def _calendar(code: str):
    """The exchange_calendars calendar for ``code``, or None when the package is missing."""
    try:
        import exchange_calendars as xcals
    except ImportError:
        return None
    try:
        return xcals.get_calendar(code)
    except Exception:  # unknown code: fall back to the plain timetable
        return None


def _utc(now) -> pd.Timestamp:
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    return now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")


def exchange_closed_until(code: str, now: pd.Timestamp | None = None) -> tuple[pd.Timestamp, str] | None:
    """(next open, why it's shut) for an exchange, or None while it's open.
    Reasons: "weekend", "holiday" or "outside exchange hours"."""
    now = _utc(now).floor("min")
    cal = _calendar(code)
    if cal is not None:
        try:
            if cal.is_open_on_minute(now, ignore_breaks=True):
                return None
            nxt = pd.Timestamp(cal.next_open(now)).tz_convert("UTC")
            local = now.tz_convert(cal.tz)
            if _over_weekend(local, nxt.tz_convert(cal.tz)):
                return nxt, "weekend"
            if not cal.is_session(local.normalize().tz_localize(None)):
                return nxt, "holiday"
            return nxt, "outside exchange hours"
        except Exception:
            pass  # outside the calendar's date range: use the plain timetable
    tz, (oh, om), (ch, cm) = EXCHANGE_FALLBACK.get(code, EXCHANGE_FALLBACK["XNYS"])
    local = now.tz_convert(tz)
    day = local.normalize()
    opens, closes = day.replace(hour=oh, minute=om), day.replace(hour=ch, minute=cm)
    if local.day_of_week < 5 and opens <= local < closes:
        return None
    nxt = opens if local.day_of_week < 5 and local < opens else opens + pd.Timedelta(days=1)
    while nxt.day_of_week >= 5:
        nxt += pd.Timedelta(days=1)
    return nxt.tz_convert("UTC"), "weekend" if _over_weekend(local, nxt) else "outside exchange hours"


def _over_weekend(local: pd.Timestamp, next_open_local: pd.Timestamp) -> bool:
    """Shut for the weekend: it's Saturday or Sunday, or Friday after the close with Monday next."""
    return local.day_of_week >= 5 or (local.day_of_week == 4 and next_open_local.normalize() > local.normalize())


def trades_24_7(symbol: str) -> bool:
    mk = MARKETS_BY_KEY.get(symbol)
    if mk:
        return mk.get("group") == "Crypto"
    up = (symbol or "").upper()
    return any(h in up for h in CRYPTO_HINTS)


def weekend_reopen(symbol: str, now: pd.Timestamp | None = None) -> pd.Timestamp | None:
    """When the market reopens if it's shut for the weekend right now, else None."""
    if trades_24_7(symbol):
        return None
    code = exchange_for(symbol)
    if code:
        shut = exchange_closed_until(code, now)
        return shut[0] if shut and shut[1] == "weekend" else None
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    ny = now.tz_convert(NY)
    dow, hour = ny.day_of_week, ny.hour  # Monday = 0
    closed = (dow == 4 and hour >= 17) or dow == 5 or (dow == 6 and hour < 18)
    if not closed:
        return None
    sunday = (ny + pd.Timedelta(days=(6 - dow) % 7)).normalize()
    return sunday.replace(hour=18).tz_convert("UTC")


def closed_until(symbol: str, now: pd.Timestamp | None = None) -> tuple[pd.Timestamp, str] | None:
    """(when the market opens again, why it's shut) if it's shut right now, else None.
    Covers the weekend and the daily 17:00-18:00 New York break, or exchange hours for stocks."""
    code = exchange_for(symbol)
    if code:
        return exchange_closed_until(code, now)
    back = weekend_reopen(symbol, now)
    if back is not None:
        return back, "weekend"
    if trades_24_7(symbol):
        return None
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    ny = now.tz_convert(NY)
    if ny.hour == 17:
        return ny.normalize().replace(hour=18).tz_convert("UTC"), "daily break"
    return None

