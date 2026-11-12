"""When markets are shut, so the app can say "waiting" instead of looking stuck.
Most CFD markets (currencies, metals, indices) pause from 17:00 to 18:00 New York time
every day, stop at 17:00 on Friday and start again at 18:00 on Sunday. Crypto never stops."""

from __future__ import annotations

import pandas as pd

from .recipes import MARKETS_BY_KEY

NY = "America/New_York"
CRYPTO_HINTS = ("BTC", "ETH", "SOL", "XRP", "LTC", "DOGE", "VCCY")


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
    Covers the weekend and the daily 17:00-18:00 New York break."""
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

