"""Reliability patch: spread checks, finite strategy numbers, Monte Carlo without compounding,
session-aware annualisation, Wilder smoothing and repaired-candle tracking."""

import numpy as np
import pandas as pd
import pytest
import yaml

from tradealerts import analysis, dataquality, indicators
from tradealerts.backtest import bars_per_year, run_backtest
from tradealerts.data import DataError, normalise, resample_ohlcv
from tradealerts.sample import synthetic_bars
from tradealerts.simulator import Simulator
from tradealerts.strategy import StrategyError, load_strategy

from .conftest import make_strategy


def _bars(n=5, **cols):
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    base = {"open": [10.0] * n, "high": [11.0] * n, "low": [9.0] * n, "close": [10.5] * n, "volume": [1.0] * n}
    base.update(cols)
    return pd.DataFrame(base, index=idx)


# --------------------------------------------------------------------------- 1. spreads


@pytest.mark.parametrize("bad", [-0.1, np.inf, -np.inf])
def test_normalise_rejects_bad_spread(bad):
    with pytest.raises(DataError, match="spread"):
        normalise(_bars(spread=[0.1, 0.1, bad, 0.1, 0.1]))


def test_normalise_accepts_zero_positive_and_missing_spread():
    out = normalise(_bars(spread=[0.0, 0.2, np.nan, 0.1, 0.0]))
    assert out["spread"].iloc[1] == 0.2 and np.isnan(out["spread"].iloc[2])


def test_half_spread_guard():
    sim = Simulator(make_strategy())
    assert sim._half_spread(0.4) == 0.2
    assert sim._half_spread(0.0) == 0.0
    assert sim._half_spread(float("nan")) == sim.s.spread / 2  # missing: use the strategy's spread
    for bad in (-0.2, float("inf")):
        with pytest.raises(ValueError):
            sim._half_spread(bad)


def test_oanda_crossed_quotes_clip_to_zero():
    from tradealerts.data import OandaProvider
    q = {"o": "1.1000", "h": "1.1010", "l": "1.0990", "c": "1.1005"}
    lower = {"o": "1.1002", "h": "1.1012", "l": "1.0992", "c": "1.1007"}  # bid above ask
    df = OandaProvider.parse([{"time": "2024-01-01T00:00:00Z", "bid": lower, "ask": q, "volume": 5}])
    assert df["spread"].iloc[0] == 0.0


# --------------------------------------------------------------------------- 2. finite strategy numbers


@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf"])
def test_strategy_rejects_nan_and_inf(value):
    v = yaml.safe_load(value)
    with pytest.raises(StrategyError, match="finite"):
        make_strategy(exit={"stop": {"points": v}, "target": {"points": 4}})
    with pytest.raises(StrategyError, match="finite"):
        make_strategy(risk={"account_size": v, "risk_per_trade_pct": 1, "point_value": 1})


@pytest.mark.parametrize("market,exit_", [
    ({"symbol": "T", "label": "T", "timeframe": "1h", "lookback_days": True}, None),
    (None, {"stop": {"points": 2}, "max_bars": True}),
    (None, {"stop": {"atr": 2, "atr_period": True}}),
])
def test_strategy_rejects_booleans_for_whole_numbers(market, exit_):
    kw = {}
    if market:
        kw["market"] = market
    if exit_:
        kw["exit"] = exit_
    with pytest.raises(StrategyError, match="whole number"):
        make_strategy(**kw)


def test_strategy_yaml_file_with_nan(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("name: X\nmarket: {symbol: T, label: T, timeframe: 1h}\ndirection: long\n"
                 "entry: {left: close, op: '>', right: 1}\nexit: {stop: {points: .nan}}\n"
                 "risk: {account_size: 1000, risk_per_trade_pct: 1, point_value: 1}\n")
    with pytest.raises(StrategyError, match="finite"):
        load_strategy(p)


def test_indicator_period_rejects_bool():
    with pytest.raises(ValueError):
        indicators.sma(_bars(), True)


# --------------------------------------------------------------------------- 3. Monte Carlo


def _trades(pnl, start=1000.0):
    eq = start + np.cumsum(pnl)
    return pd.DataFrame({"pnl": pnl, "equity_after": eq})


def test_monte_carlo_fixed_size_adds_pnl():
    t = _trades([100.0, -300.0, 50.0, -200.0, 400.0])
    mc = analysis.monte_carlo(t, 1000.0, np.random.default_rng(1), runs=500, compound=False)
    # actual path 1000, 1100, 800, 850, 650, 1050: worst drop 1100 -> 650
    assert mc["actual_dd"] == pytest.approx(-450 / 1100 * 100)
    # worst possible order: both losses first, -500 from 1000
    assert mc["bad_dd"] >= -50.0 - 1e-9
    assert min(b["to"] for b in mc["histogram"]) > 0


def test_monte_carlo_modes_differ_and_compound_is_default():
    t = _trades([100.0, -300.0, 50.0, -200.0, 400.0])
    a = analysis.monte_carlo(t, 1000.0, np.random.default_rng(1), runs=300)
    b = analysis.monte_carlo(t, 1000.0, np.random.default_rng(1), runs=300, compound=True)
    c = analysis.monte_carlo(t, 1000.0, np.random.default_rng(1), runs=300, compound=False)
    assert a == b
    assert a["actual_dd"] == pytest.approx(c["actual_dd"])  # same actual path
    assert a["p50"] != c["p50"]


def test_monte_carlo_fixed_paths_match_brute_force():
    pnl = np.array([10.0, -40.0, 25.0, -5.0])
    t = _trades(pnl)
    rng = np.random.default_rng(3)
    mc = analysis.monte_carlo(t, 100.0, rng, runs=50, compound=False)
    perms = np.argsort(np.random.default_rng(3).random((50, 4)), axis=1)
    worst = []
    for p in perms:
        eq = np.concatenate([[100.0], 100.0 + np.cumsum(pnl[p])])
        worst.append(((eq / np.maximum.accumulate(eq)) - 1).min() * 100)
    assert mc["p50"] == pytest.approx(float(np.percentile(worst, 50)))


@pytest.mark.parametrize("compound", [True, False])
def test_callers_pass_compound_setting(monkeypatch, compound):
    from tradealerts import compute
    seen = []
    real = analysis.monte_carlo
    monkeypatch.setattr(analysis, "monte_carlo", lambda *a, **kw: seen.append(kw.get("compound")) or real(*a, **kw))
    risk = {"account_size": 1000, "risk_per_trade_pct": 1, "point_value": 1,
            "size_step": 0.01, "min_size": 0.01, "compound": compound}
    s = make_strategy(risk=risk)
    df = synthetic_bars(days=120, freq="1h", seed=2)
    monkeypatch.setattr(compute, "strategy", lambda data: s)
    compute.backtest({}, df, None, None, [])
    analysis.luck_check(s, df, None, None, time_budget=5, max_random=2)
    assert seen and all(c is compound for c in seen)


# --------------------------------------------------------------------------- 4. annualisation


def _session_index(days, start="09:30", end="16:00", freq="1h"):
    out = []
    for d in pd.bdate_range("2024-01-01", periods=days):
        out.extend(pd.date_range(f"{d.date()} {start}", f"{d.date()} {end}", freq=freq, inclusive="left",
                                 tz="America/New_York"))
    return pd.DatetimeIndex(out).tz_convert("UTC")


def test_stock_session_bars_per_year():
    idx = _session_index(40)  # 7 one-hour candles a day (09:30 ... 15:30)
    assert bars_per_year(idx, "1h") == pytest.approx(252 * 7)


def test_24h_markets_keep_full_day():
    crypto = pd.date_range("2024-01-01", periods=24 * 60, freq="1h", tz="UTC")
    assert bars_per_year(crypto, "1h") == pytest.approx(252 * 24)
    fx = crypto[crypto.day_of_week < 5]
    assert bars_per_year(fx, "1h") == pytest.approx(252 * 24)


def test_daily_and_weekly_unchanged():
    idx = pd.bdate_range("2024-01-01", periods=300, tz="UTC")
    assert bars_per_year(idx, "1d") == pytest.approx(252)
    assert bars_per_year(pd.date_range("2024-01-01", periods=300, freq="D", tz="UTC"), "1d") == pytest.approx(252)
    assert bars_per_year(pd.date_range("2024-01-01", periods=60, freq="2D", tz="UTC"), "2d") == pytest.approx(365 / 2)


def test_intraday_sharpe_not_inflated_vs_daily():
    """Daily candles built from the same session candles give about the same Sharpe."""
    from tradealerts.backtest import risk_ratios
    rng = np.random.default_rng(0)
    days = 1500
    r = rng.normal(0.0004, 0.004, days * 7)  # independent hourly returns, 7 a session
    idx = _session_index(days + 1)[: days * 7 + 1]
    intraday = pd.Series(1000 * np.concatenate([[1], np.cumprod(1 + r)]), index=idx)
    daily = intraday.iloc[::7]
    daily.index = pd.bdate_range("2024-01-01", periods=len(daily), tz="UTC")
    d = risk_ratios(daily, "1d")["sharpe"]
    i = risk_ratios(intraday, "1h")["sharpe"]
    assert 0.8 < i / d < 1.25  # assuming 24 h of trading would have given about sqrt(24/7) = 1.85x


# --------------------------------------------------------------------------- 5. Wilder smoothing

# StockCharts' published RSI(14) worked example
REF_CLOSES = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
              45.8931, 46.0328, 45.614, 46.282, 46.282, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439]
REF_RSI = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97]


def test_rsi_matches_reference_table():
    out = indicators.rsi(pd.DataFrame({"close": REF_CLOSES}), 14)
    assert out.iloc[:14].isna().all()
    assert out.iloc[14:].round(2).tolist() == REF_RSI


def test_wilder_smooth_seed_and_recursion():
    s = pd.Series([np.nan, 1.0, 2.0, 3.0, 4.0, 10.0])
    out = indicators.wilder_smooth(s, 3)
    assert out.iloc[:3].isna().all()
    assert out.iloc[3] == pytest.approx(2.0)  # mean of 1, 2, 3
    assert out.iloc[4] == pytest.approx(2.0 + (4.0 - 2.0) / 3)
    assert out.iloc[5] == pytest.approx(out.iloc[4] + (10.0 - out.iloc[4]) / 3)
    assert indicators.wilder_smooth(pd.Series([1.0, 2.0]), 3).isna().all()


def test_atr_seeded_with_average_true_range():
    df = synthetic_bars(days=10, freq="1h", seed=5)
    tr = indicators.true_range(df)
    out = indicators.atr(df, 14)
    assert out.iloc[13] == pytest.approx(tr.iloc[:14].mean())
    assert out.iloc[14] == pytest.approx((out.iloc[13] * 13 + tr.iloc[14]) / 14)


def test_wilder_no_lookahead():
    df = synthetic_bars(days=20, freq="1h", seed=6)
    full_rsi, full_atr = indicators.rsi(df, 14), indicators.atr(df, 14)
    cut = df.iloc[:200]
    pd.testing.assert_series_equal(indicators.rsi(cut, 14), full_rsi.iloc[:200], check_names=False)
    pd.testing.assert_series_equal(indicators.atr(cut, 14), full_atr.iloc[:200], check_names=False)


# --------------------------------------------------------------------------- 6. repaired candles


def test_repaired_candles_counted_and_reported():
    raw = _bars(high=[11.0, 8.0, 11.0, 10.2, 11.0], low=[9.0, 9.0, 12.0, 9.0, 9.0])
    out = normalise(raw)
    assert out.attrs["repaired_bars"] == 3
    assert out["high"].iloc[1] == 10.5 and out["low"].iloc[1] == 8.0
    assert out["high"].iloc[3] == 10.5
    assert (out["high"] >= out[["open", "close", "low"]].max(axis=1)).all()
    assert resample_ohlcv(out, "4h").attrs["repaired_bars"] == 3
    warn = [w for w in dataquality.check(out, "1h", now=out.index[-1]) if w["kind"] == "repaired"]
    assert warn and warn[0]["count"] == 3


def test_clean_candles_report_nothing_and_backtest_metric():
    df = normalise(synthetic_bars(days=20, freq="1h", seed=1))
    assert df.attrs["repaired_bars"] == 0
    assert not [w for w in dataquality.check(df, "1h", now=df.index[-1]) if w["kind"] == "repaired"]
    df.attrs["repaired_bars"] = 4
    assert run_backtest(make_strategy(), df).metrics["repaired_bars"] == 4
