"""Data quality: is the price history good enough to trust what's drawn on it?

`check(df, timeframe)` looks at a bar frame and returns plain-English warnings about
gaps (missing candles), a stale last bar (the feed stopped), and zero-volume or
flat candles (the feed filled in a price without any trading behind it). It never
raises: bad input just means no warnings.
"""

from __future__ import annotations

import pandas as pd

from . import timeframes

# a gap must be at least this many bars wide before it counts (one missing candle is normal noise)
GAP_BARS = 3
# how many candles the last bar may lag "now" before it is called stale
STALE_BARS = 3
# a run of zero-volume (or flat) candles at least this long is reported
ZERO_RUN = 5


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _spans_weekend(a: pd.Timestamp, b: pd.Timestamp) -> bool:
    """True if a→b is a weekend shutdown rather than missing data: it includes part of a Saturday and is
    no longer than a long weekend. Anything wider than that is a real gap even if a weekend sits inside it."""
    if b - a > pd.Timedelta(days=3, hours=12):
        return False
    return any(d.weekday() == 5 for d in pd.date_range(a.normalize(), b.normalize(), freq="D"))


def _fmt(t: pd.Timestamp) -> str:
    return f"{t:%a %d %b %H:%M}"


def check(df: pd.DataFrame, timeframe: str, now=None) -> list[dict]:
    """Warnings about the frame, each {"kind", "text", "count"}; empty when it looks fine.

    kinds: "gap" (missing candles, weekends ignored), "stale" (last bar well behind now),
    "zero_volume" (a run of candles with no volume, or no price movement when there's no volume column).
    """
    out: list[dict] = []
    if df is None or len(df) < 2 or not isinstance(df.index, pd.DatetimeIndex):
        return out
    try:
        bar = timeframes.to_timedelta(timeframe)
    except Exception:
        return out
    idx = df.index
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    if not idx.is_monotonic_increasing:
        order = idx.argsort()
        idx, df = idx[order], df.iloc[order]

    # gaps
    diffs = pd.Series(idx[1:] - idx[:-1], index=idx[1:])
    gaps = [(a, b) for b, d in diffs[diffs > GAP_BARS * bar].items() for a in [b - d] if not _spans_weekend(a, b)]
    if gaps:
        a, b = max(gaps, key=lambda g: g[1] - g[0])
        missing = int((b - a) / bar) - 1
        out.append({"kind": "gap", "count": len(gaps),
                    "text": (f"{len(gaps)} gap{'s' if len(gaps) != 1 else ''} in the prices, the widest "
                             f"about {missing} candles ({_fmt(a)} to {_fmt(b)} UTC). "
                             "Rules that look back over those candles may have missed or invented signals.")})

    # stale last bar
    now = pd.Timestamp.now(tz="UTC") if now is None else _utc(now)
    age = now - (idx[-1] + bar)
    if age > STALE_BARS * bar and not _spans_weekend(idx[-1], now):
        hours = age.total_seconds() / 3600
        ago = f"{int(age.total_seconds() // 60)} min" if hours < 1 else f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} days"
        out.append({"kind": "stale", "count": 1,
                    "text": f"The last candle finished {ago} ago. The feed may have stopped, so anything drawn since is guesswork."})

    # zero-volume / flat runs
    if "volume" in df.columns:
        dead = (pd.to_numeric(df["volume"], errors="coerce").fillna(0) <= 0).to_numpy()
    elif {"open", "high", "low", "close"} <= set(df.columns):
        dead = ((df["high"] == df["low"]) & (df["open"] == df["close"])).to_numpy()
    else:
        dead = None
    if dead is not None and dead.any():
        runs, start = [], None
        for i, d in enumerate(list(dead) + [False]):
            if d and start is None:
                start = i
            elif not d and start is not None:
                if i - start >= ZERO_RUN:
                    runs.append((start, i - 1))
                start = None
        if runs:
            s, e = max(runs, key=lambda r: r[1] - r[0])
            what = "no volume" if "volume" in df.columns else "no price movement"
            out.append({"kind": "zero_volume", "count": len(runs),
                        "text": (f"{len(runs)} stretch{'es' if len(runs) != 1 else ''} of candles with {what}, the longest "
                                 f"{e - s + 1} candles from {_fmt(idx[s])} UTC. Those are filler, not real trading.")})
    return out
