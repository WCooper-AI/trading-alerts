"""Loading the prices a test needs, through the shared cache."""

from __future__ import annotations

import pandas as pd

from .. import data as data_mod
from ..backtest import warmup_days
from ..workspace import Workspace
from .common import err


def load_period(ctx, ws: Workspace, s, body: dict, end_at: pd.Timestamp | None = None, warmup: int | None = None):
    """Prices for a test: (candles, finer candles, test start, notes). Uses the request's
    ``years`` or ``start``/``end``; ``end_at`` cuts the data off (e.g. when a forward test began).
    ``warmup`` overrides the candles loaded before the test period (default: what ``s`` needs)."""
    c = ws.config()
    m = s.market
    now = pd.Timestamp.now(tz="UTC")
    lookback = m.lookback_days or m.default_lookback()
    years = body.get("years")
    test_start = None
    end = None
    try:
        if years not in (None, ""):
            if isinstance(years, bool) or not isinstance(years, (int, float)) or not 0 < years <= 20:
                err(422, "The test period must be between 1 and 20 years")
            test_start = (end_at or now) - pd.Timedelta(days=round(years * 365.25))
        elif body.get("start"):
            test_start = pd.Timestamp(str(body["start"]), tz="UTC")
        if body.get("end"):
            end = pd.Timestamp(str(body["end"]), tz="UTC") + pd.Timedelta(days=1)
    except ValueError:
        err(422, "Dates must look like 2025-01-31")
    if end_at is not None:
        end = end_at if end is None else min(end, end_at)
    if test_start is not None and end is not None and test_start >= end:
        err(422, "The start date must be before the end date")
    if test_start is not None:
        lookback = max(1, (now - test_start).days) + warmup_days(m.timeframe, warmup or s.warmup_candles)
    provider = data_mod.provider_for(c, m.provider, m.tz)
    try:
        source = c.data.get("source") if m.provider == "auto" else m.provider
        key = (m.provider, source, m.symbol, m.timeframe, lookback, m.tz, m.candle_alignment, str(c.cache_dir),
               str(c.csv_base_dir) if m.provider == "csv" else "")
        df, fine = ctx.cache.get(key, lambda: provider.fetch_bars(m.symbol, m.timeframe, lookback, m.candle_alignment))
    except data_mod.DataError as exc:
        err(422, str(exc))
    except Exception as exc:
        err(502, f"Couldn't load prices for {m.label or m.symbol}: {exc}")
    if end is not None:
        df = df[df.index < end]
        if fine is not None:
            fine = fine[fine.index < end]
    df = data_mod.closed_bars(df, m.timeframe)
    if df.empty or (test_start is not None and df.index[-1] < test_start):
        err(422, "No price data in that period")
    notes = data_mod.data_notes(m.symbol, m.provider, c.data)
    if test_start is not None and df.index[0] > test_start + pd.Timedelta(days=7):
        asked = f"the last {years:g} year{'s' if years != 1 else ''}" if years else f"from {test_start:%d %b %Y}"
        notes.insert(0, f"You asked for {asked}, but this price data only goes back to "
                        f"{df.index[0].day} {df.index[0]:%b %Y}, so the results cover a shorter period"
                        + (" (Yahoo keeps about 2 years of hourly prices)." if "Yahoo" in " ".join(notes) else "."))
    return df, fine, test_start, notes
