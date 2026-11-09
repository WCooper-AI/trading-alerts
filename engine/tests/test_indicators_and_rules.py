import numpy as np
import pandas as pd
import pytest

from tradealerts import indicators, rules
from tradealerts.sample import synthetic_bars

from .conftest import make_bars

# One representative parameter set per indicator
CASES = {
    "sma": {}, "ema": {}, "macd": {}, "macd_signal": {}, "macd_hist": {}, "rsi": {},
    "pct_change": {"period": 3}, "zscore": {}, "true_range": {}, "atr": {}, "atr_pct": {},
    "volatility": {}, "bollinger_upper": {}, "bollinger_lower": {}, "keltner_upper": {},
    "keltner_lower": {}, "highest": {}, "lowest": {}, "pct_from_high": {}, "pct_from_low": {},
    "opening_range_high": {"session_start": "09:30", "minutes": 30},
    "opening_range_low": {"session_start": "09:30", "minutes": 30},
}


def test_every_indicator_has_a_case():
    assert set(CASES) == set(indicators.REGISTRY)


@pytest.mark.parametrize("name", sorted(CASES))
def test_no_lookahead(name):
    """A value at bar i must not change when later bars are added."""
    df = synthetic_bars(days=12, freq="15min", seed=11)
    full = indicators.compute(df, name, **CASES[name])
    for cut in [60, 200, 333, len(df) - 1]:
        part = indicators.compute(df.iloc[: cut + 1], name, **CASES[name])
        pd.testing.assert_series_equal(part, full.iloc[: cut + 1], check_names=False, rtol=1e-9)



@pytest.mark.parametrize("name", sorted(n for n in CASES if not n.startswith("opening_range")))
def test_lookback_covers_warm_up(name):
    """After ``lookback`` candles an indicator is known and close to its value with longer history."""
    df = synthetic_bars(days=12, freq="15min", seed=11)
    lb = indicators.lookback(name, CASES[name])
    full = indicators.compute(df, name, **CASES[name])
    for start in (100, 400):
        part = indicators.compute(df.iloc[start:], name, **CASES[name])
        ready = part.iloc[lb - 1:]
        assert ready.notna().all()
        diff = (ready - full.iloc[start + lb - 1:]).abs()
        assert diff.max() <= 0.1 * full.std(), name


def test_rule_max_lookback():
    sma = {"indicator": "sma", "period": 250, "shift": 100}
    assert rules.max_lookback({"left": "close", "op": ">", "right": sma}) == 350
    assert rules.max_lookback({"left": "close", "op": "crosses_above", "right": {"indicator": "sma", "period": 20}}) == 21
    assert rules.max_lookback({"left": {"field": "close", "shift": 5}, "op": ">", "right": 1}) == 6
    assert rules.max_lookback({"left": {"indicator": "ema", "period": 50}, "op": ">", "right": 1}) == 150
    nested = {"all": [{"weekdays": ["mon"]}, {"not": {"any": [
        {"left": {"indicator": "rsi"}, "op": "<", "right": 30},
        {"left": "close", "op": ">", "right": {"indicator": "highest", "period": 80, "shift": 1}}]}}]}
    assert rules.max_lookback(nested) == max(6 * 14 + 1, 81)


def test_sma_and_highest_values():
    df = make_bars([(1, 2, 0, 1), (2, 3, 1, 2), (3, 6, 2, 3), (4, 5, 3, 4)])
    assert indicators.sma(df, 2).tolist()[1:] == [1.5, 2.5, 3.5]
    assert indicators.highest(df, 3).tolist()[2:] == [6, 6]
    assert np.isnan(indicators.sma(df, 2).iloc[0])


def test_rsi_bounds_and_extremes():
    up = make_bars([(i, i + 1, i - 1, i) for i in range(1, 40)])
    assert indicators.rsi(up, 14).dropna().eq(100).all()
    df = synthetic_bars(days=30, seed=2)
    r = indicators.rsi(df).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_opening_range_values_and_availability():
    # New York 09:30 = 14:30 UTC in January
    idx = pd.date_range("2025-01-06 14:00", periods=8, freq="15min", tz="UTC")
    highs = [100, 105, 107, 103, 110, 111, 112, 113]  # 14:30 and 14:45 bars form the range
    lows = [90, 95, 96, 94, 99, 100, 101, 102]
    df = pd.DataFrame({"open": 98.0, "high": highs, "low": lows, "close": 99.0, "volume": 0.0}, index=idx)
    orh = indicators.opening_range_high(df, "09:30", 30, "America/New_York")
    orl = indicators.opening_range_low(df, "09:30", 30, "America/New_York")
    assert orh.iloc[:4].isna().all()  # unknown until 15:00 UTC
    assert orh.iloc[4:].eq(107).all()
    assert orl.iloc[4:].eq(94).all()


def test_rule_operators_and_crosses():
    df = make_bars([(1, 1, 1, c) for c in [1, 2, 3, 2, 1, 2, 3]])
    above = rules.evaluate({"left": "close", "op": ">", "right": 2}, df)
    assert above.tolist() == [False, False, True, False, False, False, True]
    cross = rules.evaluate({"left": "close", "op": "crosses_above", "right": 2.5}, df)
    assert cross.tolist() == [False, False, True, False, False, False, True]
    down = rules.evaluate({"left": "close", "op": "crosses_below", "right": 1.5}, df)
    assert down.tolist() == [False, False, False, False, True, False, False]


def test_combinators_shift_scale():
    df = make_bars([(1, 1, 1, c) for c in [10, 11, 12, 13]])
    prev = rules.evaluate({"left": "close", "op": ">", "right": {"field": "close", "shift": 1}}, df)
    assert prev.tolist() == [False, True, True, True]  # NaN on first bar -> False
    spec = {"all": [{"left": "close", "op": ">=", "right": 11},
                    {"not": {"left": "close", "op": ">", "right": {"field": "close", "scale": 1, "add": 100}}}]}
    assert rules.evaluate(spec, df).tolist() == [False, True, True, True]
    anyspec = {"any": [{"left": "close", "op": "==", "right": 10}, {"left": "close", "op": "==", "right": 13}]}
    assert rules.evaluate(anyspec, df).tolist() == [True, False, False, True]


def test_time_and_weekday_filters():
    idx = pd.date_range("2025-01-10 14:00", periods=4, freq="h", tz="UTC")  # Friday
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0}, index=idx)
    t = rules.evaluate({"time_between": ["10:00", "11:00"], "tz": "America/New_York"}, df)
    assert t.tolist() == [False, True, False, False]  # 15:00 UTC = 10:00 NY
    assert rules.evaluate({"weekdays": ["fri"]}, df).all()
    assert not rules.evaluate({"weekdays": ["mon", "tue"]}, df).any()


@pytest.mark.parametrize("spec,msg", [
    ({"left": "close", "op": "=>", "right": 1}, "unknown op"),
    ({"left": "price", "op": ">", "right": 1}, "unknown field"),
    ({"left": {"indicator": "smaa"}, "op": ">", "right": 1}, "unknown indicator"),
    ({"left": {"indicator": "sma", "length": 5}, "op": ">", "right": 1}, "doesn't take length"),
    ({"all": []}, "non-empty list"),
    ({"time_between": ["9am", "10:00"]}, "times must look like"),
    ({"foo": 1}, "a condition needs"),
])
def test_rule_errors_are_helpful(spec, msg):
    with pytest.raises(rules.RuleError, match=msg):
        rules.compile_condition(spec)


def test_no_code_execution_possible():
    with pytest.raises(rules.RuleError):
        rules.compile_condition({"left": "__import__('os').system('echo hi')", "op": ">", "right": 1})
