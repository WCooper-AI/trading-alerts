"""Synthetic price data for trying the tool offline. Randomly generated, not real prices."""

from __future__ import annotations

import numpy as np
import pandas as pd


def synthetic_bars(
    start: str = "2024-01-01",
    days: int = 730,
    freq: str = "1h",
    start_price: float = 5000.0,
    seed: int = 7,
    weekdays_only: bool = True,
) -> pd.DataFrame:
    """Random-walk OHLCV bars with drifting trends and changing volatility."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=int(pd.Timedelta(days=days) / pd.Timedelta(freq)), freq=freq, tz="UTC")
    if weekdays_only:
        idx = idx[idx.dayofweek < 5]
    n = len(idx)
    bar_hours = pd.Timedelta(freq).total_seconds() / 3600
    # Slowly changing trend and volatility regimes
    drift = np.repeat(rng.normal(0.00002, 0.00012, n // 300 + 1), 300)[:n] * bar_hours
    vol = np.exp(np.cumsum(rng.normal(0, 0.03, n)).clip(-1.5, 1.5)) * 0.0025 * np.sqrt(bar_hours)
    rets = drift + vol * rng.standard_t(5, n) / np.sqrt(5 / 3)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]]) * np.exp(rng.normal(0, 0.0003, n))
    wick = np.abs(rng.normal(0, 1, (2, n))) * vol * close * 0.6
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    volume = rng.integers(1_000, 10_000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    ).rename_axis("time").round(2)
