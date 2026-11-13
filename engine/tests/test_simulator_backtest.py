
import pandas as pd
import pytest

from tradealerts.backtest import compute_metrics, run_backtest
from tradealerts.risk import position_size
from tradealerts.simulator import Simulator
from tradealerts.strategy import SkipTrade, StrategyError

from .conftest import make_bars, make_strategy

ALWAYS = {"left": "close", "op": ">", "right": 0}
NEVER = {"left": "close", "op": "<", "right": 0}


def run(strategy, rows, entry=None, exit_=None):
    """Drive the simulator with explicit per-bar signals."""
    df = make_bars(rows)
    sim = Simulator(strategy)
    events = []
    for i, t in enumerate(df.index):
        r = df.iloc[i]
        e = entry[i] if entry else False
        x = exit_[i] if exit_ else False
        events += sim.step(t, r.open, r.high, r.low, r.close, e, x, {})
    return sim, events


def closed(events):
    return [e.data for e in events if e.kind == "closed"]


def test_position_size_rounds_down():
    # £1000 * 1% = £10 risk, stop 3 pts at £1/pt -> 3.333 -> 3.33
    assert position_size(1000, 1, 3, 1, 0.01) == 3.33
    assert position_size(1000, 1, 3, 1, 1) == 3
    assert position_size(1000, 0.1, 500, 1, 0.01, min_size=0.5) == 0.0
    assert position_size(1000, 1, 0, 1) == 0.0
    assert position_size(1000, 1, 1, 1, 0.01, max_size=2) == 2


def test_entry_fills_next_open_and_target_hit():
    s = make_strategy()
    rows = [(100, 101, 99, 100), (100.5, 103, 100, 102), (102, 105, 101, 104)]
    sim, ev = run(s, rows, entry=[True, False, False])
    opened = [e for e in ev if e.kind == "opened"][0].data
    assert opened["entry_price"] == 100.5  # next bar's open
    assert opened["stop"] == 98.5 and opened["target"] == 104.5
    t = closed(ev)[0]
    assert t["exit_reason"] == "target" and t["exit_price"] == 104.5
    assert t["size"] == 5.0  # £10 risk / 2 pts
    assert t["pnl"] == pytest.approx(20.0)
    assert t["r_multiple"] == pytest.approx(2.0)
    assert sim.state["equity"] == pytest.approx(1020.0)


def test_stop_wins_when_both_hit_same_bar():
    s = make_strategy()
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 110, 90, 100)]
    _, ev = run(s, rows, entry=[True, False, False])
    t = closed(ev)[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == 98


def test_gap_through_stop_fills_at_open():
    s = make_strategy()
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (95, 96, 94, 95)]
    _, ev = run(s, rows, entry=[True, False, False])
    t = closed(ev)[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == 95
    assert t["r_multiple"] == pytest.approx(-2.5)


def test_short_trade_mirrors_long():
    s = make_strategy(direction="short")
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (99, 99, 95, 96)]
    _, ev = run(s, rows, entry=[True, False, False])
    t = closed(ev)[0]
    assert t["exit_reason"] == "target" and t["exit_price"] == 96
    assert t["pnl"] > 0


def test_exit_rule_closes_next_open_and_max_bars():
    s = make_strategy(exit={"stop": {"points": 50}, "max_bars": 10})
    rows = [(100, 100, 100, 100)] * 2 + [(101, 101, 101, 101), (103, 103, 103, 103)]
    _, ev = run(s, rows, entry=[True, False, False, False], exit_=[False, False, True, False])
    t = closed(ev)[0]
    assert t["exit_reason"] == "exit_rule" and t["exit_price"] == 103

    s2 = make_strategy(exit={"stop": {"points": 50}, "max_bars": 2})
    rows2 = [(100, 100, 100, 100), (100, 100, 100, 101), (101, 101, 101, 102), (102, 102, 102, 103)]
    _, ev2 = run(s2, rows2, entry=[True, False, False, False])
    t2 = closed(ev2)[0]
    assert t2["exit_reason"] == "max_bars" and t2["exit_price"] == 102 and t2["bars_held"] == 2


def test_spread_charged_half_each_side():
    s = make_strategy(costs={"spread": 1.0})
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 110, 100, 105)]
    _, ev = run(s, rows, entry=[True, False, False])
    t = closed(ev)[0]
    assert t["entry_price"] == 100.5  # bought at the ask
    assert t["stop"] == 98.5 and t["target"] == 104.5
    assert t["exit_price"] == 104.5  # target is a bid level: filled at it once the bid gets there
    assert t["spread_cost"] == pytest.approx(1.0 * t["size"])


def test_atr_and_level_stops():
    s = make_strategy(exit={"stop": {"atr": 2, "atr_period": 3}, "target": {"r_multiple": 1}})
    stop, target = s.plan(100, {"stop_atr": 1.5})
    assert stop == 97 and target == 103
    lvl = make_strategy(exit={"stop": {"level": {"indicator": "lowest", "period": 3}}})
    assert lvl.plan(100, {"stop_level": 95})[0] == 95
    with pytest.raises(SkipTrade):
        lvl.plan(100, {"stop_level": 101})  # stop above a long entry


def test_strategy_validation_messages():
    with pytest.raises(StrategyError, match="timeframe"):
        make_strategy(market={"symbol": "X", "timeframe": "4hours"})
    with pytest.raises(StrategyError, match="exactly one"):
        make_strategy(exit={"stop": {"atr": 2, "percent": 1}})
    with pytest.raises(StrategyError, match="unknown key"):
        make_strategy(risk={"acount_size": 1000})
    with pytest.raises(StrategyError, match="typo"):
        make_strategy(risk={"risk_per_trade_pct": 50})
    with pytest.raises(StrategyError, match="unknown indicator"):
        make_strategy(entry={"left": {"indicator": "magic"}, "op": ">", "right": 1})


def test_backtest_runs_and_metrics_consistent(bars_1h):
    s = make_strategy(
        entry={"left": {"indicator": "rsi", "period": 14}, "op": "crosses_above", "right": 30},
        exit={"stop": {"atr": 2}, "target": {"r_multiple": 2}, "max_bars": 48},
    )
    res = run_backtest(s, bars_1h)
    m = res.metrics
    assert m["trades"] == len(res.trades) > 5
    assert m["end_equity"] == pytest.approx(1000 + res.trades["pnl"].sum())
    assert m["max_drawdown_pct"] <= 0
    # one position at a time: each entry is after the previous exit
    entries = pd.to_datetime(res.trades["entry_time"])
    exits = pd.to_datetime(res.trades["exit_time"])
    assert (entries.iloc[1:].to_numpy() >= exits.iloc[:-1].to_numpy()).all()


def test_profit_factor_is_none_without_losing_trades(bars_1h):
    s = make_strategy()
    period = bars_1h.iloc[:10]
    eq = pd.Series(1000.0, index=period.index)
    wins = pd.DataFrame({"pnl": [10.0, 5.0], "r_multiple": [1.0, 0.5], "bars_held": [3, 4],
                         "exit_reason": ["target", "target"], "funding": [0.0, 0.0], "spread_cost": [1.0, 1.0]})
    assert compute_metrics(s, wins, eq, period, 0)["profit_factor"] is None
    mixed = wins.assign(pnl=[10.0, -5.0])
    assert compute_metrics(s, mixed, eq, period, 0)["profit_factor"] == pytest.approx(2.0)


def test_backtest_signals_have_no_lookahead(bars_1h):
    """Truncating the data must not change any earlier trade."""
    s = make_strategy(
        entry={"all": [{"left": "close", "op": "<", "right": {"indicator": "bollinger_lower"}},
                       {"left": {"indicator": "rsi"}, "op": "<", "right": 40}]},
        exit={"stop": {"atr": 1.5}, "target": {"atr": 2}, "max_bars": 24},
    )
    full = run_backtest(s, bars_1h).trades
    half = run_backtest(s, bars_1h.iloc[: len(bars_1h) // 2]).trades
    done = half[half.exit_reason != "end_of_data"]
    pd.testing.assert_frame_equal(
        done.drop(columns=["equity_after"]).reset_index(drop=True),
        full.iloc[: len(done)].drop(columns=["equity_after"]).reset_index(drop=True),
    )


def test_warmup_follows_the_longest_rule():
    assert make_strategy().warmup_candles == 300  # never less than the usual 300
    long = make_strategy(entry={"left": "close", "op": ">", "right": {"indicator": "sma", "period": 250, "shift": 100}},
                         exit={"stop": {"atr": 2, "atr_period": 80}})
    assert long.lookback_candles == 481  # ATR(80) settles over 6 periods, plus one candle
    assert long.warmup_candles == 481


def test_backtest_notes_too_little_history(bars_1h):
    s = make_strategy(entry={"left": "close", "op": ">", "right": {"indicator": "sma", "period": 200}})
    short = run_backtest(s, bars_1h, start=bars_1h.index[50])
    assert any("need 200 candles" in n for n in short.notes)
    enough = run_backtest(s, bars_1h, start=bars_1h.index[250])
    assert not any("candles of history" in n for n in enough.notes)



def test_trades_record_why_and_how_far_they_went(bars_1h):
    s = make_strategy(
        entry={"left": {"indicator": "rsi", "period": 14}, "op": "crosses_above", "right": 30},
        exit={"stop": {"atr": 2}, "target": {"r_multiple": 2}, "max_bars": 48},
    )
    res = run_backtest(s, bars_1h)
    assert len(res.trades) > 5
    for t in res.trades.to_dict("records"):
        assert t["mae_r"] <= 0 <= t["mfe_r"]
        assert set(t["entry_reason"]) == {"RSI(14)"} and 0 <= t["entry_reason"]["RSI(14)"] <= 100
        assert set(t["exit_values"]) <= {"RSI(14)"}
        if t["exit_reason"] == "stop":
            assert t["mae_r"] == pytest.approx(-1.0, abs=0.35)  # it got to the stop (gaps and spread can overshoot)
        if t["exit_reason"] == "target":
            assert t["mfe_r"] >= 1.9
