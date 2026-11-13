"""Regression tests for data integrity, drawdown, annualisation, and data-quality fixes."""

from __future__ import annotations

import pandas as pd
import pytest

from tradealerts import dataquality
from tradealerts.analysis import portfolio, portfolio_weights
from tradealerts.backtest import BacktestResult, bars_per_year, compute_metrics
from tradealerts.data import DataError, normalise
from tradealerts.strategy import Market, Risk, Strategy


def _make_df(n: int = 5, freq: str = "1h", start: str = "2025-01-06 00:00:00+00:00") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [10.0] * n,
        },
        index=idx,
    )


def test_normalise_rejects_infinite_ohlcv():
    df = _make_df(3)
    df.iloc[1, df.columns.get_loc("high")] = float("inf")
    with pytest.raises(DataError, match="non-finite"):
        normalise(df)

    df2 = _make_df(3)
    df2.iloc[1, df2.columns.get_loc("low")] = float("-inf")
    with pytest.raises(DataError, match="non-finite"):
        normalise(df2)


def test_normalise_rejects_negative_volume():
    df = _make_df(3)
    df.iloc[0, df.columns.get_loc("volume")] = -5.0
    with pytest.raises(DataError, match="negative volume"):
        normalise(df)


def test_portfolio_weights_rejects_infinite_custom_weights():
    strat = Strategy(
        id="s",
        name="s",
        market=Market(symbol="EURUSD=X", label="EUR/USD"),
        direction="long",
        entry_spec={"left": "close", "op": ">", "right": 0},
        stop_spec={"type": "percent", "percent": 1.0},
    )
    eq = pd.Series([10000.0, 10100.0], index=pd.date_range("2025-01-06", periods=2, freq="1h", tz="UTC"))
    res = BacktestResult(strategy=strat, trades=pd.DataFrame(), equity=eq, metrics={"market": "EUR/USD", "trades": 0})
    parts = [("a", "A", res), ("b", "B", res)]

    with pytest.raises(ValueError, match="share"):
        portfolio_weights("custom", parts, {"a": float("inf"), "b": 50.0})

    with pytest.raises(ValueError, match="share"):
        portfolio_weights("custom", parts, {"a": float("-inf"), "b": 50.0})


def test_drawdown_includes_initial_balance():
    strat = Strategy(
        id="dd_test",
        name="dd_test",
        market=Market(symbol="EURUSD=X", label="EUR/USD", timeframe="1h"),
        direction="long",
        entry_spec={"left": "close", "op": ">", "right": 0},
        stop_spec={"type": "percent", "percent": 1.0},
        risk=Risk(account_size=10_000.0),
    )
    df = _make_df(3)
    # First candle drops immediately to 9500, then recovers to 9600, 9700.
    eq = pd.Series([9500.0, 9600.0, 9700.0], index=df.index)
    m = compute_metrics(strat, pd.DataFrame(), eq, df, bars_in_market=3)
    assert m["max_drawdown_pct"] == pytest.approx(-5.0)

    res = BacktestResult(strategy=strat, trades=pd.DataFrame(), equity=eq, metrics=m)
    port = portfolio([("s1", "S1", res)], balance=10_000.0)
    assert port["metrics"]["max_drawdown_pct"] == pytest.approx(-5.0)
    assert port["per_strategy"][0]["max_drawdown_pct"] == pytest.approx(-5.0)


def test_bars_per_year_crypto_uses_365_days():
    idx = pd.date_range("2025-01-01", periods=24 * 14, freq="1h", tz="UTC")
    btc_bars = bars_per_year(idx, "1h", symbol="BTC-USD")
    fx_bars = bars_per_year(idx, "1h", symbol="EURUSD=X")
    assert btc_bars == pytest.approx(365 * 24)
    assert fx_bars == pytest.approx(252 * 24)


def test_dataquality_reports_weekend_gaps_for_crypto():
    # Friday 2025-01-03 20:00 to Monday 2025-01-06 00:00 (52 hours gap over weekend)
    fri = pd.date_range("2025-01-03 16:00", periods=5, freq="1h", tz="UTC")
    mon = pd.date_range("2025-01-06 00:00", periods=5, freq="1h", tz="UTC")
    idx = fri.append(mon)
    df = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0},
        index=idx,
    )
    fx_warnings = dataquality.check(df, "1h", now=mon[-1], symbol="EURUSD=X")
    assert not any(w["kind"] == "gap" for w in fx_warnings)

    btc_warnings = dataquality.check(df, "1h", now=mon[-1], symbol="BTC-USD")
    assert any(w["kind"] == "gap" for w in btc_warnings)
