"""0.3 Session 8C: portfolio contribution and exposure, stock costs (commission, borrow fee),
exchange hours for stocks, and the Stocks market group."""
import copy

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradealerts import analysis, markethours
from tradealerts.backtest import run_backtest
from tradealerts.recipes import MARKETS, MARKETS_BY_KEY
from tradealerts.sample import synthetic_bars
from tradealerts.simulator import Simulator, nights_between
from tradealerts.strategy import StrategyError, strategy_from_dict
from tradealerts.web import create_app

from .conftest import make_bars, make_strategy
from .test_web import CSV_STRATEGY, H

T = pd.Timestamp


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "data").mkdir()
    synthetic_bars(days=240, freq="1h", seed=4).to_csv(tmp_path / "data" / "bars.csv")
    (tmp_path / "config.yaml").write_text("poll_minutes: 15\nnotifiers:\n  - type: console\n")
    return tmp_path


@pytest.fixture
def client(ws):
    return TestClient(create_app(ws / "config.yaml"))


# --------------------------------------------------------------------------- portfolio stage A & B

def _two_results(bars):
    long = make_strategy(name="Long", risk={"account_size": 5000, "risk_per_trade_pct": 1, "point_value": 1, "compound": False})
    short = make_strategy(name="Short", direction="short", market={"symbol": "OTHER", "label": "Other", "timeframe": "1h"},
                          entry={"left": "close", "op": "<", "right": {"indicator": "sma", "period": 3}},
                          risk={"account_size": 5000, "risk_per_trade_pct": 1, "point_value": 1, "compound": False})
    return [("a", "Long", run_backtest(long, bars)), ("b", "Short", run_backtest(short, bars))]


def test_portfolio_contribution_and_exposure(bars_1h):
    out = analysis.portfolio(_two_results(bars_1h), 5000.0)
    per = {p["id"]: p for p in out["per_strategy"]}
    assert set(per) == {"a", "b"}
    for p in per.values():
        assert 0 <= p["time_in_market_pct"] <= 100
        assert "contribution_pct" in p and p["worst_day"] <= 0 <= p["best_day"]
        assert p["direction"] in ("long", "short") and p["timeframe"] == "1h"
    total = sum(p["pnl"] for p in per.values())
    if total:
        assert sum(p["contribution_pct"] for p in per.values()) == pytest.approx(100, abs=1e-6)
    ex = out["exposure"]
    assert {b["market"] for b in ex["by_market"]} == {"Test", "Other"}
    assert sum(ex["overlap_pct"].values()) == pytest.approx(100, abs=1e-6)
    assert ex["max_open_trades"] <= 2 and ex["max_open_trades"] == max(ex["overlap_pct"])
    assert 0 <= ex["time_in_market_pct"] <= 100
    m = out["metrics"]
    assert m["max_open_trades"] == ex["max_open_trades"] and m["time_in_market_pct"] == ex["time_in_market_pct"]
    # per-strategy curves line up with the shared equity
    curves = out["pnl_curves"]
    assert list(curves.columns) == ["a", "b"]
    assert float(curves.sum(axis=1).iloc[-1]) == pytest.approx(m["end_equity"] - 5000, abs=1e-6)
    if out["correlation"]:
        assert -1 <= m["avg_correlation"] <= 1
        assert m["diversification_ratio"] >= 1 - 1e-9


def test_portfolio_single_strategy_has_no_pairwise_metrics(bars_1h):
    parts = _two_results(bars_1h)[:1]
    out = analysis.portfolio(parts, 5000.0)
    assert out["correlation"] is None
    assert out["metrics"]["avg_correlation"] is None and out["metrics"]["diversification_ratio"] is None
    assert out["per_strategy"][0]["contribution_pct"] == pytest.approx(100) or out["per_strategy"][0]["pnl"] == 0
    assert len(out["exposure"]["by_market"]) == 1


def test_portfolio_route_returns_exposure_and_curves(client):
    a = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    other = copy.deepcopy(CSV_STRATEGY)
    other["name"] = "CSV short"
    other["direction"] = "short"
    other["entry"]["all"][0]["op"] = "crosses_below"
    other["entry"]["all"][0]["right"] = 65
    b = client.post("/api/strategies", json={"data": other}, headers=H).json()["id"]
    r = client.post("/api/portfolio", json={"ids": [a, b], "balance": 5000}, headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out["pnl_curves"]) == {a, b}
    assert all(len(pt) == 2 for pt in out["pnl_curves"][a])
    assert "by_market" in out["exposure"] and "overlap_pct" in out["exposure"]
    assert "max_open_trades" in out["metrics"] and "time_in_market_pct" in out["metrics"]
    assert all("contribution_pct" in p and "time_in_market_pct" in p for p in out["per_strategy"])


# --------------------------------------------------------------------------- stocks: costs

def _flat_bars(n=30, start="2025-01-06 15:00"):
    return make_bars([(100, 100.5, 99.5, 100)] * n, start=start)


def _one_trade(**costs):
    """A long that enters once at 100 (stop 98, target 104) and is closed by max_bars after 3 candles."""
    rows = [(100, 100, 100, 100)] * 4 + [(100, 100, 100, 101)] + [(100, 100.5, 99.5, 100)] * 10
    bars = make_bars(rows, start="2025-01-06 10:00")
    s = make_strategy(costs={"spread": 0, "use_data_spread": False, **costs},
                      entry={"left": "close", "op": ">", "right": 100.5},
                      exit={"stop": {"points": 2}, "target": {"points": 4}, "max_bars": 3},
                      risk={"account_size": 1000, "risk_per_trade_pct": 1, "point_value": 1,
                            "size_step": 0.01, "min_size": 0.01, "compound": False})
    return run_backtest(s, bars)


def test_commission_per_fill_reduces_pnl():
    base = _one_trade()
    assert len(base.trades) == 1 and base.trades.iloc[0]["commission"] == 0
    with_fee = _one_trade(commission=1.5)
    t = with_fee.trades.iloc[0]
    assert t["commission"] == pytest.approx(3.0)  # entry and exit
    assert t["pnl"] == pytest.approx(base.trades.iloc[0]["pnl"] - 3.0)
    assert with_fee.metrics["total_commission"] == pytest.approx(3.0)
    assert with_fee.metrics["end_equity"] == pytest.approx(base.metrics["end_equity"] - 3.0)


def test_commission_pct_of_fill_value():
    res = _one_trade(commission_pct=0.1)
    t = res.trades.iloc[0]
    expect = (t["entry_price"] + t["exit_price"]) * t["size"] * 0.1 / 100
    assert t["commission"] == pytest.approx(expect)


def test_borrow_fee_only_for_shorts():
    def run(direction, **costs):
        s = make_strategy(direction=direction, costs={"spread": 0, "use_data_spread": False, **costs},
                          entry={"left": "close", "op": ">", "right": 0},
                          exit={"stop": {"points": 50}, "target": {"points": 50}, "max_bars": 60},
                          risk={"account_size": 100_000, "risk_per_trade_pct": 1, "point_value": 1,
                                "size_step": 0.01, "min_size": 0.01, "compound": False})
        return run_backtest(s, _flat_bars(70)).trades.iloc[0]
    long_t, short_t = run("long", borrow_pct=3.65), run("short", borrow_pct=3.65)
    assert long_t["borrow"] == 0 and long_t["funding"] == 0
    nights = nights_between(short_t["entry_time"], short_t["exit_time"])
    assert nights >= 2
    assert short_t["borrow"] == pytest.approx(nights * 100 * short_t["size"] * 3.65 / 100 / 365)
    assert short_t["funding"] == pytest.approx(short_t["borrow"])  # no base funding set
    both = run("short", borrow_pct=3.65, funding_pct=3.65)
    assert both["funding"] == pytest.approx(2 * short_t["borrow"])
    assert both["borrow"] == pytest.approx(short_t["borrow"])


def test_cost_keys_validated():
    with pytest.raises(StrategyError, match="costs.borrow_pct"):
        make_strategy(costs={"borrow_pct": 150})
    with pytest.raises(StrategyError, match="costs.commission_pct"):
        make_strategy(costs={"commission_pct": 7})
    with pytest.raises(StrategyError, match="costs.commission"):
        make_strategy(costs={"commission": -1})
    s = make_strategy(costs={"commission": 1, "commission_pct": 0.05, "borrow_pct": 2})
    assert (s.commission, s.commission_pct, s.borrow_pct) == (1, 0.05, 2)


def test_stock_market_defaults_fill_blank_costs():
    d = {"name": "AAPL dip", "market": {"symbol": "aapl", "label": "Apple", "provider": "auto", "timeframe": "1h"}, "direction": "short",
         "entry": {"left": "close", "op": ">", "right": 1}, "exit": {"stop": {"points": 1}},
         "risk": {"account_size": 1000, "risk_per_trade_pct": 1}}
    s = strategy_from_dict(d)
    assert s.borrow_pct == MARKETS_BY_KEY["aapl"]["borrow"] and s.spread == MARKETS_BY_KEY["aapl"]["spread"]
    assert s.commission == 0


def test_simulator_state_keeps_commission_through_a_restart():
    s = make_strategy(costs={"spread": 0, "use_data_spread": False, "commission": 2},
                      entry={"left": "close", "op": ">", "right": 0},
                      exit={"stop": {"points": 50}, "target": {"points": 50}, "max_bars": 5})
    sim = Simulator(s)
    t0 = T("2025-01-06 10:00", tz="UTC")
    sim.step(t0, 100, 100, 100, 100, True, False, {})
    sim.step(t0 + pd.Timedelta(hours=1), 100, 100, 100, 100, False, False, {})
    assert sim.position["commission"] == 2 and sim.state["equity"] == pytest.approx(1000 - 2)
    again = Simulator(s, state=copy.deepcopy(sim.state))
    events = again.finish(t0 + pd.Timedelta(hours=1), 100)
    assert events[0].data["commission"] == 4 and events[0].data["pnl"] == pytest.approx(-4)


# --------------------------------------------------------------------------- stocks: exchange hours

def test_stocks_group_in_markets():
    stocks = [m for m in MARKETS if m["group"] == "Stocks"]
    assert {m["key"] for m in stocks} >= {"spy", "aapl", "msft", "nvda"}
    for m in stocks:
        assert m["exchange"] == "XNYS" and m["symbol"] and m["dukascopy"] and "borrow" in m
    assert MARKETS_BY_KEY["us500"].get("exchange") is None
    assert markethours.exchange_for("aapl") == "XNYS" and markethours.exchange_for("us500") is None


def test_exchange_hours_with_calendar_fallback(monkeypatch):
    markethours._calendar.cache_clear()
    monkeypatch.setattr(markethours, "_calendar", lambda code: None)  # as if exchange_calendars were missing
    assert markethours.closed_until("aapl", T("2026-11-12 15:00", tz="UTC")) is None  # Thu 10:00 NY
    shut = markethours.closed_until("aapl", T("2026-11-12 12:00", tz="UTC"))  # Thu 07:00 NY
    assert shut == (T("2026-11-12 14:30", tz="UTC"), "outside exchange hours")
    shut = markethours.closed_until("aapl", T("2026-11-13 22:00", tz="UTC"))  # Fri 17:00 NY
    assert shut == (T("2026-11-16 14:30", tz="UTC"), "weekend")
    assert markethours.weekend_reopen("aapl", T("2026-11-14 15:00", tz="UTC")) == T("2026-11-16 14:30", tz="UTC")
    assert markethours.weekend_reopen("aapl", T("2026-11-12 12:00", tz="UTC")) is None
    # CFD markets are unchanged
    assert markethours.closed_until("us500", T("2026-11-12 12:00", tz="UTC")) is None


def test_exchange_hours_with_exchange_calendars():
    pytest.importorskip("exchange_calendars")
    markethours._calendar.cache_clear()
    assert markethours.closed_until("spy", T("2026-11-13 15:00", tz="UTC")) is None
    shut = markethours.closed_until("spy", T("2026-11-26 15:00", tz="UTC"))  # Thanksgiving
    assert shut is not None and shut[1] == "holiday" and shut[0] == T("2026-11-27 14:30", tz="UTC")
    shut = markethours.closed_until("spy", T("2026-11-14 15:00", tz="UTC"))  # Saturday
    assert shut == (T("2026-11-16 14:30", tz="UTC"), "weekend")
    assert markethours.closed_until("eurusd", T("2026-11-14 15:00", tz="UTC"))[1] == "weekend"

