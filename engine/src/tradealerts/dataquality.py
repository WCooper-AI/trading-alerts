"""Data quality: is the price history good enough to trust what's drawn on it?

`check(df, timeframe)` looks at a bar frame and returns plain-English warnings about
gaps (missing candles), timestamps out of order, a stale last bar (the feed stopped),
and zero-volume or flat candles (the feed filled in a price without any trading behind
it). It never raises: bad input just means no warnings.
"""

from __future__ import annotations

import pandas as pd

from . import timeframes
from .markethours import trades_24_7

# a gap must be at least this many bars wide before it counts (one missing candle is normal noise)
GAP_BARS = 3
# how many candles the last bar may lag "now" before it is called stale
STALE_BARS = 3
# a run of zero-volume (or flat) candles at least this long is reported
ZERO_RUN = 5
# above this many candles in one gap, estimate its weekday share instead of listing every candle
EXACT_LIMIT = 50_000


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _spans_weekend(a: pd.Timestamp, b: pd.Timestamp) -> bool:
    """True if a→b is a weekend shutdown rather than missing data: it includes part of a Saturday and is
    no longer than a long weekend. Anything wider than that is a real gap even if a weekend sits inside it."""
    if b - a > pd.Timedelta(days=3, hours=12):
        return False
    return any(d.weekday() == 5 for d in pd.date_range(a.normalize(), b.normalize(), freq="D"))


def _weekday_candles(a: pd.Timestamp, b: pd.Timestamp, bar: pd.Timedelta) -> int:
    """Candles that should sit strictly between a and b on weekdays (Saturdays and Sundays skipped)."""
    n = int((b - a) / bar) - 1
    if n <= 0:
        return 0
    if n > EXACT_LIMIT:
        return int(n * 5 / 7)
    stamps = pd.date_range(a + bar, periods=n, freq=bar)
    return int((stamps.day_of_week < 5).sum())


def _fmt(t: pd.Timestamp) -> str:
    return f"{t:%a %d %b %H:%M}"


def check(df: pd.DataFrame, timeframe: str, now=None, symbol: str = "") -> list[dict]:
    """Warnings about the frame, each {"kind", "text", "count"}; empty when it looks fine.

    kinds: "gap" (missing candles, weekends ignored), "stale" (last bar well behind now),
    "zero_volume" (a run of candles with no volume, or no price movement when there's no volume column),
    "repaired" (candles with impossible high/low values that were widened when loaded),
    "missing_bars" (total weekday candles missing across several gaps, with ``gaps`` and ``largest_at``),
    "disordered" (timestamps that go backwards or repeat; checked after sorting them).
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
    steps = idx[1:] - idx[:-1]
    back = int(df.attrs.get("disordered_bars", 0) or 0) or int((steps <= pd.Timedelta(0)).sum())  # earlier than, or the same as, the candle before
    if back:
        out.append({"kind": "disordered", "count": back,
                    "text": (f"{back} candle{'s' if back != 1 else ''} came out of time order or repeated an "
                             "earlier time. They were put in order, but the source data may be mixed up there.")})
    if not idx.is_monotonic_increasing:
        order = idx.argsort()
        idx, df = idx[order], df.iloc[order]

    # gaps
    is_24_7 = bool(symbol and trades_24_7(symbol))
    diffs = pd.Series(idx[1:] - idx[:-1], index=idx[1:])
    gaps = [(a, b) for b, d in diffs[diffs > GAP_BARS * bar].items() for a in [b - d] if is_24_7 or not _spans_weekend(a, b)]
    if gaps:
        a, b = max(gaps, key=lambda g: g[1] - g[0])
        missing = int((b - a) / bar) - 1
        out.append({"kind": "gap", "count": len(gaps),
                    "text": (f"{len(gaps)} gap{'s' if len(gaps) != 1 else ''} in the prices, the widest "
                             f"about {missing} candles ({_fmt(a)} to {_fmt(b)} UTC). "
                             "Rules that look back over those candles may have missed or invented signals.")})
    # Weekday candles missing. Holes next to a weekend count too when more than GAP_BARS weekday
    # candles are gone (a market shut from Friday evening loses a few). One gap already reported
    # above is described in full there, so this adds the total when there are several.
    holes = []
    for b, d in diffs[diffs > GAP_BARS * bar].items():
        a = b - d
        n = max(0, int((b - a) / bar) - 1) if is_24_7 else _weekday_candles(a, b, bar)
        if n > (0 if (a, b) in gaps else GAP_BARS):
            holes.append((a, b, n))
    if len(holes) > 1 or (holes and not gaps):
        total = sum(g[2] for g in holes)
        a, b, worst = max(holes, key=lambda g: g[2])
        out.append({"kind": "missing_bars", "count": total, "gaps": len(holes), "largest_at": a.isoformat(),
                    "text": (f"About {total} weekday candles are missing across {len(holes)} gaps; the largest "
                             f"({worst} candles) starts {_fmt(a)} UTC. Results over those dates rest on less data than they seem.")})

    # stale last bar
    now = pd.Timestamp.now(tz="UTC") if now is None else _utc(now)
    age = now - (idx[-1] + bar)
    if age > STALE_BARS * bar and (is_24_7 or not _spans_weekend(idx[-1], now)):
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

    # candles whose high/low didn't contain the open/close, widened when loaded (data.normalise)
    repaired = int(df.attrs.get("repaired_bars", 0) or 0)
    if repaired:
        out.append({"kind": "repaired", "count": repaired,
                    "text": (f"{repaired} candle{'s' if repaired != 1 else ''} had a high below the open, close or low "
                             "(or a low above them) and were widened to fit. The source data has bad ticks there.")})
    return out
