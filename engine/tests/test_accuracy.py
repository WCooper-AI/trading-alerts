"""Tests for broker-realistic fills, costs, candle alignment and the data sources."""

import io
import json
import urllib.error

import numpy as np
import pandas as pd
import pytest

from tradealerts import data as data_mod
from tradealerts.backtest import run_backtest
from tradealerts.simulator import Simulator, nights_between

from .conftest import make_bars, make_strategy


def run(strategy, rows, entry=None, spreads=None, fine=None, start="2025-01-06 00:00", freq="1h"):
    df = make_bars(rows, start=start, freq=freq)
    sim = Simulator(strategy)
    events = []
    for i, t in enumerate(df.index):
        r = df.iloc[i]
        events += sim.step(t, r.open, r.high, r.low, r.close, bool(entry and entry[i]), False, {},
                           spread=None if spreads is None else spreads[i],
                           fine=(lambda t=t: fine.get(t)) if fine else None)
    return sim, [e.data for e in events if e.kind == "closed"]


# --------------------------------------------------------------------------- bid / ask


def test_long_stop_triggers_on_the_bid_not_the_mid():
    s = make_strategy(costs={"spread": 2.0}, exit={"stop": {"points": 3}, "target": {"points": 6}})
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 100.5, 98.8, 100)]
    _, trades = run(s, rows, entry=[True, False, False])
    t = trades[0]
    assert t["entry_price"] == 101          # ask
    assert t["stop"] == 98                  # 3 points below the fill
    assert t["exit_time"].startswith("2025-01-06 02:00")
    # mid low 98.8 never touched 98, but the bid (97.8) did
    assert t["exit_reason"] == "stop" and t["exit_price"] == 98


def test_a_stop_closer_than_the_spread_is_hit_straight_away():
    s = make_strategy(costs={"spread": 2.0})  # bought at 101, stop 99 = the bid
    _, trades = run(s, [(100, 100, 100, 100)] * 3, entry=[True, False, False])
    assert trades[0]["exit_reason"] == "stop" and trades[0]["bars_held"] == 0


def test_short_target_not_hit_until_the_ask_reaches_it():
    s = make_strategy(direction="short", costs={"spread": 2.0}, exit={"stop": {"points": 4}, "target": {"points": 4}})
    # sold at the bid (99): stop 103, target 95. Mid low 95.5 means the ask only got to 96.5.
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 99.5, 95.5, 96)]
    sim, trades = run(s, rows, entry=[True, False, False])
    assert trades == [] and sim.position["entry_price"] == 99 and sim.position["target"] == 95


def test_short_target_hit_when_ask_reaches_it():
    s = make_strategy(direction="short", costs={"spread": 2.0}, exit={"stop": {"points": 4}, "target": {"points": 4}})
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 99.5, 93.8, 94)]
    _, trades = run(s, rows, entry=[True, False, False])
    t = trades[0]
    assert t["exit_reason"] == "target" and t["exit_price"] == 95
    assert t["pnl"] == pytest.approx((99 - 95) * t["size"])


def test_gap_through_stop_fills_at_the_opening_bid():
    s = make_strategy(costs={"spread": 1.0})  # bought at 100.5, stop 98.5
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 100, 100), (96, 97, 95, 96)]
    _, trades = run(s, rows, entry=[True, False, False, False])
    t = trades[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == 95.5   # opening mid 96, bid 95.5


def test_spread_from_the_data_is_used_per_candle():
    s = make_strategy(costs={"spread": 0.2})
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 110, 100, 105)]
    _, trades = run(s, rows, entry=[True, False, False], spreads=[3.0, 3.0, 3.0])
    assert trades[0]["entry_price"] == 101.5
    s2 = make_strategy(costs={"spread": 0.2, "use_data_spread": False})
    _, trades2 = run(s2, rows, entry=[True, False, False], spreads=[3.0, 3.0, 3.0])
    assert trades2[0]["entry_price"] == 100.1


# --------------------------------------------------------------------------- funding


def test_nights_between_counts_weekends():
    fri = pd.Timestamp("2025-01-10 21:00", tz="UTC")
    mon = pd.Timestamp("2025-01-13 01:00", tz="UTC")
    assert nights_between(fri, mon) == 3
    assert nights_between(fri, fri + pd.Timedelta("30min")) == 0
    assert nights_between("2025-01-10T21:59:00+00:00", "2025-01-10T22:00:00+00:00") == 1


def test_overnight_funding_is_charged_per_night():
    s = make_strategy(exit={"stop": {"points": 50}, "max_bars": 5}, costs={"funding_pct": 36.5},
                      market={"symbol": "X", "timeframe": "1d", "candle_alignment": "utc"})
    rows = [(100, 100, 100, 100)] * 7
    sim, trades = run(s, rows, entry=[True] + [False] * 6, start="2025-01-06", freq="1D")
    t = trades[0]
    # daily candles from midnight: held from Tue 00:00 open to Sat 00:00 close = 5 nights
    expected_per_night = 100 * t["size"] * 1 * 0.365 / 365
    assert t["funding"] == pytest.approx(5 * expected_per_night)
    assert t["pnl"] == pytest.approx(-t["funding"])
    assert sim.state["equity"] == pytest.approx(1000 - t["funding"])


# --------------------------------------------------------------------------- stop vs target inside one candle


def fine_rows(t, rows):
    idx = pd.date_range(t, periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


@pytest.mark.parametrize("order,expected", [("target_first", "target"), ("stop_first", "stop")])
def test_finer_candles_decide_which_level_was_hit_first(order, expected):
    s = make_strategy(market={"symbol": "X", "timeframe": "4h"})
    coarse = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 110, 90, 100)]
    t2 = pd.Timestamp("2025-01-06 08:00", tz="UTC")
    if order == "target_first":
        inner = [(100, 105, 100, 104), (104, 104, 99, 100), (100, 100, 90, 92), (92, 100, 92, 100)]
    else:
        inner = [(100, 100, 95, 96), (96, 104, 96, 103), (103, 110, 103, 108), (108, 108, 100, 100)]
    fine = {t2: fine_rows(t2, inner)}
    _, trades = run(s, coarse, entry=[True, False, False], fine=fine, freq="4h")
    assert trades[0]["exit_reason"] == expected


def test_without_finer_candles_the_stop_is_assumed_first():
    s = make_strategy(market={"symbol": "X", "timeframe": "4h"})
    _, trades = run(s, [(100, 100, 100, 100), (100, 100, 100, 100), (100, 110, 90, 100)],
                    entry=[True, False, False], freq="4h")
    assert trades[0]["exit_reason"] == "stop"


@pytest.mark.parametrize("unit", ["ns", "us"])
def test_finer_candle_lookup_works_with_any_timestamp_unit(unit):
    from tradealerts.backtest import fine_lookup
    from tradealerts.sample import synthetic_bars
    fine = synthetic_bars(days=3, freq="1h", seed=1)
    fine.index = fine.index.as_unit(unit)
    rows = fine_lookup(fine, pd.Timedelta("4h"))(fine.index[8])
    assert len(rows) == 4 and rows.index[0] == fine.index[8]


def test_backtest_uses_finer_candles_and_reports_costs():
    from tradealerts.sample import synthetic_bars
    fine = synthetic_bars(days=200, freq="1h", seed=8)
    bars = data_mod.resample_ohlcv(fine, "4h")
    s = make_strategy(
        market={"symbol": "X", "timeframe": "4h"},
        entry={"left": {"indicator": "rsi", "period": 14}, "op": "crosses_above", "right": 40},
        exit={"stop": {"atr": 1}, "target": {"atr": 1}},
        costs={"spread": 0.5, "funding_pct": 5},
    )
    with_fine = run_backtest(s, bars, fine)
    without = run_backtest(s, bars)
    assert with_fine.metrics["used_finer_candles"] and not without.metrics["used_finer_candles"]
    assert with_fine.metrics["total_spread_cost"] > 0 and with_fine.metrics["total_funding"] > 0
    wins_fine = (with_fine.trades.exit_reason == "target").sum()
    wins_plain = (without.trades.exit_reason == "target").sum()
    assert wins_fine >= wins_plain  # resolving ambiguity can only turn assumed stops into targets


# --------------------------------------------------------------------------- candles


def test_four_hour_candles_start_at_5pm_new_york():
    idx = pd.date_range("2025-01-06 00:00", periods=48, freq="1h", tz="UTC")  # winter: NY = UTC-5
    raw = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}, index=idx)
    ny = data_mod.resample_ohlcv(raw, "4h", "new_york")
    assert {t.hour for t in ny.index} == {2, 6, 10, 14, 18, 22}   # 21:00, 01:00 ... New York
    utc = data_mod.resample_ohlcv(raw, "4h", "utc")
    assert {t.hour for t in utc.index} == {0, 4, 8, 12, 16, 20}
    daily = data_mod.resample_ohlcv(raw, "1d", "new_york")
    assert all(t.hour == 22 for t in daily.index)                  # 17:00 New York


def test_summer_alignment_follows_daylight_saving():
    idx = pd.date_range("2025-07-07 00:00", periods=48, freq="1h", tz="UTC")  # summer: NY = UTC-4
    raw = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}, index=idx)
    daily = data_mod.resample_ohlcv(raw, "1d", "new_york")
    assert all(t.hour == 21 for t in daily.index)


def test_normalise_repairs_and_drops_junk_candles():
    idx = pd.date_range("2025-01-06", periods=4, freq="1h", tz="UTC")
    df = pd.DataFrame({"open": [10, 10, 10, 10], "high": [11, 9, 10, 12], "low": [9, 9, 10, 9],
                       "close": [10.5, 10.5, 10, 11], "volume": [5, 5, 0, 5]}, index=idx)
    out = data_mod.normalise(df)
    assert len(out) == 3                       # the flat, zero-volume candle is gone
    assert out.iloc[1]["high"] == 10.5         # high repaired to contain the close


def test_csv_with_millisecond_timestamps(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("timestamp,open,high,low,close,volume\n"
                 "1736121600000,1,2,0.5,1.5,10\n1736125200000,1.5,2,1,1.8,10\n")
    df = data_mod.CSVProvider().load(p)
    assert str(df.index[0]) == "2025-01-06 00:00:00+00:00" and len(df) == 2


def test_jump_detection_ignores_weekends_but_flags_rolls():
    from tradealerts.sample import synthetic_bars
    bars = synthetic_bars(days=30, freq="1h", seed=3)
    assert len(data_mod.find_jumps(bars, "1h")) == 0  # weekend gaps aren't flagged
    i = 300
    bars.iloc[i:, :4] += 400  # a roll-sized jump
    jumps = data_mod.find_jumps(bars, "1h")
    assert list(jumps.index) == [bars.index[i]]


# --------------------------------------------------------------------------- OANDA


def candle(t, o, h, l, c, spread=0.4, complete=True):
    half = spread / 2
    q = lambda d: {k: f"{v:.5f}" for k, v in zip("ohlc", d)}
    return {"time": t, "complete": complete, "volume": 10,
            "mid": q((o, h, l, c)), "bid": q((o - half, h - half, l - half, c - half)),
            "ask": q((o + half, h + half, l + half, c + half))}


class FakeOanda:
    def __init__(self, pages):
        self.pages, self.requests = list(pages), []

    def __call__(self, req, timeout=30):
        self.requests.append(req)
        body = json.dumps({"candles": self.pages.pop(0) if self.pages else []}).encode()
        return io.BytesIO(body)


def test_oanda_parses_mid_and_spread_and_pages():
    page1 = [candle(f"2025-01-06T{h:02d}:00:00.000000000Z", 100 + h, 101 + h, 99 + h, 100.5 + h) for h in range(3)]
    page2 = [candle("2025-01-06T03:00:00.000000000Z", 103, 104, 102, 103.5),
             candle("2025-01-06T04:00:00.000000000Z", 104, 105, 103, 104.5, complete=False)]
    fake = FakeOanda([page1, page2])
    prov = data_mod.OandaProvider("tok", "practice", opener=fake)
    prov.PAGE = 3
    df = prov._candles("SPX500_USD", "H1", pd.Timestamp("2025-01-06", tz="UTC"), "new_york")
    assert len(df) == 4                                   # incomplete candle dropped
    assert df["spread"].iloc[0] == pytest.approx(0.4)
    assert df["close"].iloc[-1] == 103.5
    first = fake.requests[0]
    assert first.get_header("Authorization") == "Bearer tok"
    assert "price=MBA" in first.full_url and "granularity=H1" in first.full_url
    assert "alignmentTimezone=America%2FNew_York" in first.full_url and "dailyAlignment=17" in first.full_url
    assert len(fake.requests) == 2


def test_oanda_bad_token_explained():
    def deny(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"errorMessage":"Insufficient authorization"}'))
    prov = data_mod.OandaProvider("bad", opener=deny)
    with pytest.raises(data_mod.DataError, match="rejected the API token"):
        prov._candles("SPX500_USD", "H1", pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1), "new_york")


def test_markets_follow_the_price_data_setting():
    yahoo, sym = data_mod.MarketProvider({"source": "yahoo"}).resolve("us500")
    assert isinstance(yahoo, data_mod.YahooProvider) and sym == "ES=F"
    oanda, inst = data_mod.MarketProvider({"source": "oanda", "oanda_token": "t"}).resolve("de40")
    assert isinstance(oanda, data_mod.OandaProvider) and inst == "DE30_EUR"
    with pytest.raises(data_mod.DataError, match="OANDA API token"):
        data_mod.MarketProvider({"source": "oanda"}).resolve("us500")
    notes = data_mod.data_notes("us500", "auto", {"source": "yahoo"})
    assert any("contract roll" in n for n in notes)
    assert data_mod.data_notes("us500", "auto", {"source": "oanda"}) == []


def test_backtest_period_uses_earlier_candles_only_to_warm_up():
    from tradealerts.sample import synthetic_bars
    from tradealerts.backtest import warmup_days
    bars = synthetic_bars(days=120, freq="1h", seed=4)
    s = make_strategy(entry={"left": "close", "op": ">", "right": {"indicator": "sma", "period": 200}},
                      exit={"stop": {"atr": 2}, "target": {"r_multiple": 2}})
    start = bars.index[1000]
    res = run_backtest(s, bars, start=start)
    assert res.metrics["start"] == str(start) and res.metrics["warmup_candles"] == 1000
    assert res.equity.index[0] == start and res.equity.iloc[0] == pytest.approx(1000, abs=20)
    assert all(pd.Timestamp(t) >= start for t in res.trades["signal_time"])
    # indicators are ready at the start, so a trade can begin straight away
    cold = run_backtest(s, bars.iloc[1000:])
    assert res.metrics["trades"] >= cold.metrics["trades"]
    assert warmup_days("1h") == 30 and warmup_days("1d") == 451
