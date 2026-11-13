"""P1 robustness: small-sample warnings, a 95% range on the Sharpe ratio, and slippage on fills."""

import numpy as np
import pandas as pd
import pytest

from tradealerts.backtest import compute_metrics, sample_warning, sharpe_ci, t_crit_95
from tradealerts.simulator import Simulator
from tradealerts.strategy import StrategyError

from .conftest import make_strategy

H = pd.Timedelta(hours=1)
T0 = pd.Timestamp("2025-01-06 10:00", tz="UTC")


# --------------------------------------------------------------------------- 1. sample size


def _metrics(n, gap=10, seed=1):
    """compute_metrics on n trades, one closing every ``gap`` hourly candles."""
    rng = np.random.default_rng(seed)
    pnl = rng.normal(6, 20, n)
    equity_after = 1000 + np.cumsum(pnl)
    bars = gap * (n + 1)
    idx = pd.date_range(T0, periods=bars, freq="1h")
    eq = np.full(bars, 1000.0)
    for k, e in enumerate(equity_after):
        eq[(k + 1) * gap:] = e
    equity = pd.Series(eq, index=idx)
    trades = pd.DataFrame({
        "pnl": pnl, "equity_after": equity_after, "r_multiple": pnl / 10, "bars_held": 3,
        "exit_reason": "stop", "funding": 0.0, "spread_cost": 0.0, "slippage_cost": 0.5,
    })
    df = pd.DataFrame({"open": 100.0, "close": 101.0}, index=idx)
    return compute_metrics(make_strategy(), trades, equity, df, bars_in_market=bars // 2)


@pytest.mark.parametrize("n, expect", [
    (5, "Critically low trade sample (< 10)"),
    (20, "Small trade sample (< 30)"),
    (50, None),
])
def test_sample_warning_by_trade_count(n, expect):
    m = _metrics(n)
    if expect is None:
        assert m["sample_warning"] is None
    else:
        assert m["sample_warning"].startswith(expect)
    assert m["total_slippage"] == pytest.approx(0.5 * n)


def test_sample_warning_edges():
    assert sample_warning(0).startswith("Critically")
    assert sample_warning(9).startswith("Critically")
    assert sample_warning(10).startswith("Small")
    assert sample_warning(29).startswith("Small")
    assert sample_warning(30) is None


def test_sharpe_ci_none_under_ten_trades():
    m = _metrics(5)
    assert m["sharpe"] is not None and m["sharpe_ci"] is None


@pytest.mark.parametrize("n", [20, 50])
def test_sharpe_ci_brackets_the_headline_sharpe(n):
    m = _metrics(n)
    lo, hi = m["sharpe_ci"]
    assert lo < m["sharpe"] < hi
    assert m["sharpe"] - lo == pytest.approx(hi - m["sharpe"])


def test_sharpe_ci_narrows_with_more_trades():
    """Same trade spread and spacing: the range shrinks roughly with 1 / sqrt(trades)."""
    w20 = np.diff(_metrics(20)["sharpe_ci"])[0]
    w50 = np.diff(_metrics(50)["sharpe_ci"])[0]
    assert w50 < w20


def test_sharpe_ci_none_without_sharpe_or_variation():
    m = _metrics(20)
    trades = pd.DataFrame({"pnl": [5.0] * 20, "equity_after": 1000 + 5.0 * np.arange(1, 21)})
    equity = pd.Series(np.linspace(1000, 1100, 200), index=pd.date_range(T0, periods=200, freq="1h"))
    assert sharpe_ci(trades, equity, "1h", None) is None
    assert sharpe_ci(trades.iloc[:0], equity, "1h", m["sharpe"]) is None


def test_t_critical_values():
    for df, ref in ((9, 2.2622), (19, 2.0930), (49, 2.0096)):
        assert t_crit_95(df) == pytest.approx(ref, abs=1e-3)


# --------------------------------------------------------------------------- 2. slippage settings


@pytest.mark.parametrize("key", ["slippage", "slippage_pct"])
@pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf"), True])
def test_slippage_rejects_bad_values(key, bad):
    with pytest.raises(StrategyError, match=f"costs.{key}"):
        make_strategy(costs={key: bad})


def test_slippage_defaults_to_zero_and_pct_typo_guard():
    s = make_strategy()
    assert s.slippage == 0 and s.slippage_pct == 0
    assert make_strategy(costs={"slippage": "", "slippage_pct": None}).slippage == 0
    with pytest.raises(StrategyError, match="typo"):
        make_strategy(costs={"slippage_pct": 6})


# --------------------------------------------------------------------------- 3. slippage on fills


def _sim(direction="long", **costs):
    s = make_strategy(direction=direction, costs={"spread": 0, "use_data_spread": False, **costs},
                      entry={"left": "close", "op": ">", "right": 0},
                      exit={"stop": {"points": 2}, "target": {"points": 4}})
    sim = Simulator(s)
    sim.step(T0, 100, 100, 100, 100, True, False, {})  # signal; fills at the next open
    return sim


def _closed(events):
    return [e for e in events if e.kind == "closed"][0].data


def test_long_entry_and_stop_pay_slippage():
    sim = _sim(slippage=0.5)
    sim.step(T0 + H, 100, 100.2, 99.9, 100, False, False, {})
    p = sim.position
    assert p["entry_price"] == pytest.approx(100.5) and p["stop"] == pytest.approx(98.5)
    t = _closed(sim.step(T0 + 2 * H, 100, 100, 95, 96, False, False, {}))
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(98.0)
    assert t["slippage_cost"] == pytest.approx(1.0 * t["size"])
    assert t["pnl"] == pytest.approx((98.0 - 100.5) * t["size"])


def test_short_entry_and_stop_pay_slippage():
    sim = _sim("short", slippage=0.5)
    sim.step(T0 + H, 100, 100.1, 99.8, 100, False, False, {})
    p = sim.position
    assert p["entry_price"] == pytest.approx(99.5) and p["stop"] == pytest.approx(101.5)
    t = _closed(sim.step(T0 + 2 * H, 100, 105, 100, 104, False, False, {}))
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(102.0)
    assert t["pnl"] == pytest.approx((99.5 - 102.0) * t["size"])
    assert t["slippage_cost"] == pytest.approx(1.0 * t["size"])


def test_gap_through_stop_slips_beyond_the_open():
    sim = _sim(slippage=0.5)
    sim.step(T0 + H, 100, 100.2, 99.9, 100, False, False, {})
    t = _closed(sim.step(T0 + 2 * H, 97, 97, 96, 96, False, False, {}))
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(96.5)


def test_target_is_a_limit_fill_without_slippage():
    sim = _sim(slippage=0.5)
    sim.step(T0 + H, 100, 100.2, 99.9, 100, False, False, {})
    t = _closed(sim.step(T0 + 2 * H, 101, 110, 101, 108, False, False, {}))
    assert t["exit_reason"] == "target" and t["exit_price"] == pytest.approx(104.5)
    assert t["slippage_cost"] == pytest.approx(0.5 * t["size"])  # entry only


@pytest.mark.parametrize("direction, entry, exit_", [("long", 101.0, 99.99), ("short", 99.0, 101.0)])
def test_slippage_pct_on_entry_and_exit_rule(direction, entry, exit_):
    sim = _sim(direction, slippage_pct=1)
    sim.step(T0 + H, 100, 100.3, 99.9, 100, False, True, {})  # exit rule fires on this close
    assert sim.position["entry_price"] == pytest.approx(entry)
    t = _closed(sim.step(T0 + 2 * H, 101 if direction == "long" else 100, 101.5, 99.5, 101, False, False, {}))
    assert t["exit_reason"] == "exit_rule" and t["exit_price"] == pytest.approx(exit_)


def test_no_slippage_leaves_fills_unchanged():
    sim = _sim()
    sim.step(T0 + H, 100, 100.2, 99.9, 100, False, False, {})
    t = _closed(sim.step(T0 + 2 * H, 100, 100, 95, 96, False, False, {}))
    assert sim.s.slippage == 0 and t["exit_price"] == pytest.approx(98.0) and t["slippage_cost"] == 0
