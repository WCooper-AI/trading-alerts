import pandas as pd
import pytest

from tradealerts.sample import synthetic_bars
from tradealerts.strategy import strategy_from_dict


@pytest.fixture
def bars_1h():
    return synthetic_bars(days=120, freq="1h", seed=3)


def make_bars(rows, start="2025-01-06 00:00", freq="1h"):
    """rows: list of (open, high, low, close)."""
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    df["volume"] = 0.0
    return df


def make_strategy(**overrides):
    base = {
        "name": "Test",
        "market": {"symbol": "TEST", "label": "Test", "timeframe": "1h"},
        "direction": "long",
        "entry": {"left": "close", "op": ">", "right": {"indicator": "sma", "period": 3}},
        "exit": {"stop": {"points": 2}, "target": {"points": 4}},
        "risk": {"account_size": 1000, "risk_per_trade_pct": 1, "point_value": 1,
                 "size_step": 0.01, "min_size": 0.01},
    }
    for k, v in overrides.items():
        base[k] = v
    return strategy_from_dict(base)
