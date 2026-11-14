"""`backtest --from` loads enough history to cover the requested period plus warm-up."""

import pandas as pd
import yaml

from tradealerts import cli
from tradealerts import data as data_mod
from tradealerts.backtest import warmup_days
from tradealerts.strategy import load_strategy

STRAT = {
    "name": "From test",
    "market": {"symbol": "TEST", "label": "Test", "timeframe": "4h"},
    "direction": "long",
    "entry": {"left": "close", "op": ">", "right": {"indicator": "sma", "period": 3}},
    "exit": {"stop": {"points": 2}, "target": {"points": 4}},
    "risk": {"account_size": 1000, "risk_per_trade_pct": 1, "point_value": 1, "size_step": 0.01, "min_size": 0.01},
}


class _StubProvider:
    """4-hour bars covering exactly ``lookback`` days back from now; records the lookback asked for."""

    def __init__(self):
        self.lookbacks = []

    def fetch_bars(self, symbol, timeframe, lookback, alignment):
        self.lookbacks.append(lookback)
        end = pd.Timestamp.now(tz="UTC").normalize()
        idx = pd.date_range(end - pd.Timedelta(days=lookback), end, freq="4h")
        close = pd.Series(range(len(idx)), index=idx, dtype=float) % 20 + 100
        df = pd.DataFrame({"open": close, "high": close + 3, "low": close - 3, "close": close, "volume": 0.0})
        return df, None


def _run(tmp_path, monkeypatch, *extra):
    (tmp_path / "config.yaml").write_text("notifiers: [{type: console}]\n")
    path = tmp_path / "from_test.yaml"
    path.write_text(yaml.safe_dump(STRAT))
    stub = _StubProvider()
    monkeypatch.setattr(data_mod, "provider_for", lambda *a, **k: stub)
    seen = {}

    def run(s, df, fine, **kw):
        seen.update(df=df, start=kw["start"])
        return real(s, df, fine, **kw)

    real = cli.run_backtest
    monkeypatch.setattr(cli, "run_backtest", run)
    rc = cli.main(["--config", str(tmp_path / "config.yaml"), "backtest", str(path), "--no-report", *extra])
    return rc, stub, seen, load_strategy(path)


def test_from_beyond_default_lookback_loads_whole_period(tmp_path, monkeypatch):
    rc, stub, seen, s = _run(tmp_path, monkeypatch, "--from", "2021-01-01", "--to", "2026-01-01")
    assert rc == 0
    start = pd.Timestamp("2021-01-01", tz="UTC")
    assert (pd.Timestamp.now(tz="UTC") - start).days > s.market.default_lookback()
    expected = (pd.Timestamp.now(tz="UTC") - start).days + warmup_days("4h", s.warmup_candles)
    assert abs(stub.lookbacks[0] - expected) <= 1
    df = seen["df"]
    assert seen["start"] == start
    assert (df.index < start).sum() >= s.warmup_candles  # warm-up candles before the start
    assert df.index[-1] < pd.Timestamp("2026-01-01", tz="UTC")


def test_years_still_sets_lookback(tmp_path, monkeypatch):
    rc, stub, _, s = _run(tmp_path, monkeypatch, "--years", "2")
    assert rc == 0
    assert stub.lookbacks[0] == int(2 * 365.25) + warmup_days("4h", s.warmup_candles)
