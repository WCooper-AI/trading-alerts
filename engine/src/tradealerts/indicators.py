"""Indicator library.

Every indicator takes the OHLCV DataFrame plus keyword parameters and returns a
Series aligned to the DataFrame's index. Each value uses only data up to and
including its own bar (no lookahead), which the test suite checks.
"""

from __future__ import annotations

import inspect
from typing import Callable

import numpy as np
import pandas as pd

PRICE_FIELDS = ("open", "high", "low", "close", "volume")


def _src(df: pd.DataFrame, source: str) -> pd.Series:
    if source not in PRICE_FIELDS:
        raise ValueError(f"source must be one of {', '.join(PRICE_FIELDS)}, got {source!r}")
    return df[source]


def _period(p, name="period") -> int:
    if isinstance(p, bool) or not np.isfinite(p) or int(p) != p or int(p) < 1:
        raise ValueError(f"{name} must be a whole number of 1 or more, got {p!r}")
    return int(p)


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Welles Wilder's smoothing (TradingView ``ta.rma``, TA-Lib RSI/ATR): the first value is the
    plain average of the first ``period`` values, then S = S_prev + (x - S_prev) / period.
    NaN until ``period`` values have been seen; uses only the current and earlier values."""
    p = _period(period)
    out = pd.Series(np.nan, index=series.index, dtype=float)
    valid = np.flatnonzero(series.notna().to_numpy())
    if len(valid) < p:
        return out
    seed_at = valid[p - 1]
    seeded = series.iloc[seed_at:].astype(float).copy()
    seeded.iloc[0] = float(series.iloc[valid[:p]].mean())
    out.iloc[seed_at:] = seeded.ewm(alpha=1 / p, adjust=False).mean().to_numpy()
    return out


# --------------------------------------------------------------------------- trend


def sma(df, period=20, source="close"):
    """Simple moving average."""
    p = _period(period)
    return _src(df, source).rolling(p, min_periods=p).mean()


def ema(df, period=20, source="close"):
    """Exponential moving average."""
    p = _period(period)
    return _src(df, source).ewm(span=p, adjust=False, min_periods=p).mean()


def macd(df, fast=12, slow=26, signal=9, source="close"):
    """MACD line (fast EMA minus slow EMA)."""
    s = _src(df, source)
    f, sl = _period(fast, "fast"), _period(slow, "slow")
    return s.ewm(span=f, adjust=False, min_periods=f).mean() - s.ewm(
        span=sl, adjust=False, min_periods=sl
    ).mean()


def macd_signal(df, fast=12, slow=26, signal=9, source="close"):
    """Signal line: EMA of the MACD line."""
    sg = _period(signal, "signal")
    return macd(df, fast, slow, signal, source).ewm(span=sg, adjust=False, min_periods=sg).mean()


def macd_hist(df, fast=12, slow=26, signal=9, source="close"):
    """MACD histogram (MACD minus signal)."""
    return macd(df, fast, slow, signal, source) - macd_signal(df, fast, slow, signal, source)


# --------------------------------------------------------------------------- momentum


def rsi(df, period=14, source="close"):
    """Wilder's RSI, 0-100."""
    p = _period(period)
    delta = _src(df, source).diff()
    gain = wilder_smooth(delta.clip(lower=0), p)
    loss = wilder_smooth(-delta.clip(upper=0), p)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.where(loss != 0, 100.0)
    out = out.where((gain != 0) | (loss != 0), 50.0)  # no movement at all: neutral, not overbought
    return out.where(gain.notna())


def pct_change(df, period=1, source="close"):
    """Percent change over ``period`` bars."""
    return _src(df, source).pct_change(_period(period), fill_method=None) * 100


def zscore(df, period=20, source="close"):
    """How many standard deviations price is from its rolling mean."""
    p = _period(period)
    s = _src(df, source)
    mean = s.rolling(p, min_periods=p).mean()
    std = s.rolling(p, min_periods=p).std(ddof=0)
    return (s - mean) / std.replace(0, np.nan)


# --------------------------------------------------------------------------- volatility


def true_range(df):
    """True range of each bar."""
    prev = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1, skipna=True)


def atr(df, period=14):
    """Average true range (Wilder smoothing), in price points."""
    p = _period(period)
    return wilder_smooth(true_range(df), p)


def atr_pct(df, period=14):
    """ATR as a percent of the close."""
    return atr(df, period) / df["close"] * 100


def volatility(df, period=20, source="close"):
    """Rolling standard deviation of bar-to-bar returns, in percent."""
    p = _period(period)
    return _src(df, source).pct_change(fill_method=None).rolling(p, min_periods=p).std() * 100


def bollinger_upper(df, period=20, std=2.0, source="close"):
    p = _period(period)
    s = _src(df, source)
    return s.rolling(p, min_periods=p).mean() + std * s.rolling(p, min_periods=p).std(ddof=0)


def bollinger_lower(df, period=20, std=2.0, source="close"):
    p = _period(period)
    s = _src(df, source)
    return s.rolling(p, min_periods=p).mean() - std * s.rolling(p, min_periods=p).std(ddof=0)


def keltner_upper(df, period=20, atr_period=10, multiplier=2.0):
    return ema(df, period) + multiplier * atr(df, atr_period)


def keltner_lower(df, period=20, atr_period=10, multiplier=2.0):
    return ema(df, period) - multiplier * atr(df, atr_period)


# --------------------------------------------------------------------------- range / levels


def highest(df, period=20, source="high"):
    """Highest value over the last ``period`` bars, including this one."""
    p = _period(period)
    return _src(df, source).rolling(p, min_periods=p).max()


def lowest(df, period=20, source="low"):
    """Lowest value over the last ``period`` bars, including this one."""
    p = _period(period)
    return _src(df, source).rolling(p, min_periods=p).min()


def pct_from_high(df, period=20):
    """Percent the close is below the highest high of the last ``period`` bars (0 or negative)."""
    return (df["close"] / highest(df, period, "high") - 1) * 100


def pct_from_low(df, period=20):
    """Percent the close is above the lowest low of the last ``period`` bars (0 or positive)."""
    return (df["close"] / lowest(df, period, "low") - 1) * 100


def _opening_range(df, session_start, minutes, tz, field, how):
    if df.empty:
        return pd.Series(dtype=float, index=df.index)
    try:
        h, m = (int(x) for x in str(session_start).split(":"))
    except ValueError as exc:
        raise ValueError(f"session_start must look like '09:30', got {session_start!r}") from exc
    minutes = _period(minutes, "minutes")
    local = df.index.tz_convert(tz)
    start = h * 60 + m
    end = start + minutes
    diffs = df.index.to_series().diff().dropna()
    bar_minutes = int(diffs.mode().iloc[0].total_seconds() // 60) if not diffs.empty else 0
    mod = np.asarray(local.hour * 60 + local.minute)
    dates = np.asarray(local.date)
    in_range = (mod >= start) & (mod + bar_minutes <= end)
    values = df[field].where(in_range)
    per_day = values.groupby(dates).agg(how)
    mapped = pd.Series(dates, index=df.index).map(per_day)
    # Only known once the range has finished: from the first bar starting at/after the end.
    return mapped.where(mod >= end).astype(float)


def opening_range_high(df, session_start="09:30", minutes=30, tz="America/New_York"):
    """High of the session's opening range. NaN until the range has finished."""
    return _opening_range(df, session_start, minutes, tz, "high", "max")


def opening_range_low(df, session_start="09:30", minutes=30, tz="America/New_York"):
    """Low of the session's opening range. NaN until the range has finished."""
    return _opening_range(df, session_start, minutes, tz, "low", "min")


REGISTRY: dict[str, Callable[..., pd.Series]] = {
    f.__name__: f
    for f in [
        sma, ema, macd, macd_signal, macd_hist, rsi, pct_change, zscore,
        true_range, atr, atr_pct, volatility, bollinger_upper, bollinger_lower,
        keltner_upper, keltner_lower, highest, lowest, pct_from_high, pct_from_low,
        opening_range_high, opening_range_low,
    ]
}


def params_for(name: str) -> dict:
    """Parameter names and defaults for an indicator."""
    sig = inspect.signature(REGISTRY[name])
    return {k: v.default for k, v in list(sig.parameters.items())[1:]}


def compute(df: pd.DataFrame, name: str, **params) -> pd.Series:
    if name not in REGISTRY:
        raise ValueError(
            f"Unknown indicator {name!r}. Available: {', '.join(sorted(REGISTRY))}"
        )
    allowed = params_for(name)
    unknown = set(params) - set(allowed)
    if unknown:
        raise ValueError(
            f"Indicator {name!r} doesn't take {', '.join(sorted(unknown))}. "
            f"Its parameters are: {', '.join(allowed) or 'none'}"
        )
    return REGISTRY[name](df, **params)


# Smoothed indicators start from a guess and take a while to settle. Measured on sample
# prices: an EMA is within a fraction of a percent after 3 periods; Wilder's smoothing
# (RSI, ATR) is slower, and RSI can still be several points off after 3, so it gets 6.
SETTLE_EMA = 3
SETTLE_WILDER = 6


def lookback(name: str, params: dict | None = None) -> int:
    """Candles of history ``name`` needs before its values can be trusted."""
    p = {**params_for(name), **(params or {})}

    def n(key: str) -> int:
        return _period(p[key], key)

    if name in ("sma", "zscore", "bollinger_upper", "bollinger_lower", "highest", "lowest",
                "pct_from_high", "pct_from_low"):
        return n("period")
    if name in ("pct_change", "volatility"):
        return n("period") + 1
    if name == "ema":
        return SETTLE_EMA * n("period")
    if name in ("rsi", "atr", "atr_pct"):
        return SETTLE_WILDER * n("period") + 1  # first value needs the previous close
    if name == "macd":
        return SETTLE_EMA * max(n("fast"), n("slow"))
    if name in ("macd_signal", "macd_hist"):
        return SETTLE_EMA * (max(n("fast"), n("slow")) + n("signal"))
    if name in ("keltner_upper", "keltner_lower"):
        return max(SETTLE_EMA * n("period"), SETTLE_WILDER * n("atr_period") + 1)
    if name == "true_range":
        return 2
    return 0  # opening range: built fresh each day
