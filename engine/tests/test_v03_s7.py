"""0.3 session 7: live vs backtest trade-window ranges and the paper-trade → journal link."""
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradealerts import analysis
from tradealerts.sample import synthetic_bars
from tradealerts.state import StateStore
from tradealerts.web import create_app

from .test_web import CSV_STRATEGY, H

STATIC = Path(__file__).resolve().parents[1] / "src" / "tradealerts" / "static"
FORBIDDEN = re.compile(r"\b(broken|degraded|bad|score)\b", re.I)


def fake_trades(n, seed=1, win=0.5, start="2024-01-01"):
    """n closed trades, one a day, each a +2R win or -1R loss with small spread and funding."""
    rng = np.random.default_rng(seed)
    rows = []
    t0 = pd.Timestamp(start, tz="UTC")
    for i in range(n):
        won = rng.random() < win
        rows.append({"entry_time": (t0 + pd.Timedelta(days=i)).isoformat(),
                     "exit_time": (t0 + pd.Timedelta(days=i, hours=6)).isoformat(),
                     "pnl": 20.0 if won else -10.0, "r_multiple": 2.0 if won else -1.0,
                     "spread_cost": 0.5, "funding": 0.1})
    return rows


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "data").mkdir()
    synthetic_bars(days=90, freq="1h", seed=4).to_csv(tmp_path / "data" / "bars.csv")
    (tmp_path / "config.yaml").write_text("poll_minutes: 15\nnotifiers:\n  - type: console\n")
    return tmp_path


@pytest.fixture
def client(ws):
    return TestClient(create_app(ws / "config.yaml"))


# ------------------------------------------------------------------ range math


def test_window_metrics_facts():
    m = analysis.window_metrics(analysis.trades_frame(fake_trades(10, win=1.0)), 1000)
    assert m["trades"] == 10 and m["win_rate_pct"] == 100 and m["avg_r"] == 2.0
    assert m["profit_factor"] is not None and m["max_drawdown_pct"] == 0
    assert m["net_pnl"] == 200 and m["gross_pnl"] == pytest.approx(200 + 10 * 0.6)
    assert m["spread_cost"] == pytest.approx(5.0) and m["funding"] == pytest.approx(1.0)
    assert m["trades_per_month"] == pytest.approx(10 / ((9 * 24 + 6) / 24 / 30.4375))
    # the worst drop is measured from the running peak of the balance
    m = analysis.window_metrics(analysis.trades_frame([{"pnl": 50, "r_multiple": 1}, {"pnl": -105, "r_multiple": -2}]), 1000)
    assert m["max_drawdown_pct"] == pytest.approx(10.0)
    assert analysis.window_metrics(analysis.trades_frame([]), 1000) is None


def test_trade_window_ranges_percentiles_and_positions():
    bt = fake_trades(200, seed=2, win=0.5)
    live = fake_trades(30, seed=3, win=0.5)
    out = analysis.trade_window_ranges(bt, live, 1000, samples=300)
    assert out["window"] == 30 and out["live_trades"] == 30 and out["backtest_trades"] == 200
    assert out["notice"] is None
    assert set(out["metrics"]) == set(analysis.WINDOW_METRICS)
    for m in out["metrics"].values():
        assert m["low"] <= m["median"] <= m["high"]
        assert m["position"] in ("below", "within", "above")
        if m["position"] == "within":
            assert m["low"] <= m["live"] <= m["high"]
    # a run of only wins sits above the backtest's win-rate range, and reads as a fact, not a verdict
    hot = analysis.trade_window_ranges(bt, fake_trades(30, win=1.0), 1000, samples=300)
    assert hot["metrics"]["win_rate_pct"]["position"] == "above"
    cold = analysis.trade_window_ranges(bt, fake_trades(30, win=0.0), 1000, samples=300)
    assert cold["metrics"]["win_rate_pct"]["position"] == "below"
    # resampling is seeded, so the same inputs give the same ranges
    again = analysis.trade_window_ranges(bt, live, 1000, samples=300)
    assert again["metrics"] == out["metrics"]


def test_window_follows_live_count_and_backtest_too_short():
    bt = fake_trades(60)
    out = analysis.trade_window_ranges(bt, fake_trades(8), 1000, samples=50)
    assert out["window"] == 8 and out["live"]["trades"] == 8
    short = analysis.trade_window_ranges(fake_trades(12), fake_trades(10), 1000)
    assert short["metrics"] == {} and "too few trades" in short["reason"]
    none = analysis.trade_window_ranges(bt, [], 1000, samples=50)
    assert none["live_trades"] == 0 and all(m["live"] is None and m["position"] is None for m in none["metrics"].values())


def test_few_trades_notice():
    assert analysis.few_trades_notice(7) == "Only 7 trades so far — ranges are wide with this few trades"
    assert analysis.few_trades_notice(19) and analysis.few_trades_notice(20) is None
    out = analysis.trade_window_ranges(fake_trades(80), fake_trades(5), 1000, samples=50)
    assert out["notice"] == "Only 5 trades so far — ranges are wide with this few trades"


# ------------------------------------------------------------------ wording


def test_wording_is_factual_only():
    out = analysis.trade_window_ranges(fake_trades(80), fake_trades(5, win=0.0), 1000, samples=50)
    text = " ".join(str(v) for v in [out["notice"], out.get("reason")] + [m["position"] for m in out["metrics"].values()])
    assert not FORBIDDEN.search(text)
    js = (STATIC / "dashboard.js").read_text()
    body = js[js.index("const WINDOW_ROWS"):js.index("function paperPrefill")]
    assert not FORBIDDEN.search(body)
    assert "below the usual range" in body and "within the usual range" in body and "above the usual range" in body
    assert "Log real trade" in js and "paperPrefill(r, t)" in js
    journal = (STATIC / "journal.js").read_text()
    assert "prefill = null" in journal and "paper_trade_id" in journal


# ------------------------------------------------------------------ endpoint


def test_backtest_range_endpoint_returns_windows(client, ws):
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    run = client.post(f"/api/strategies/{sid}/forward-test/start", headers=H).json()
    store = StateStore(ws / "data" / "state.db")
    store.db.execute("UPDATE runs SET started_at = ? WHERE id = ?", ("2024-08-20T00:00:00+00:00", run["id"]))
    for i in range(1, 6):
        store.add_equity(run["id"], f"2024-08-2{i}T00:00:00+00:00", 1000 + i)
    for t in fake_trades(3, start="2024-08-21"):
        store.add_trade(sid, {**t, "direction": "long", "entry_price": 100, "exit_price": 101, "size": 1,
                              "exit_reason": "target"}, run_id=run["id"])
    store.commit()
    store.close()
    r = client.post(f"/api/runs/{run['id']}/backtest-range", json={}, headers=H)
    assert r.status_code == 200, r.text
    w = r.json()["windows"]
    assert w["live_trades"] == 3 and w["window"] == 3
    assert w["notice"] == "Only 3 trades so far — ranges are wide with this few trades"
    if w["metrics"]:  # the sample data gave the backtest enough trades for a range
        assert set(w["metrics"]) <= set(analysis.WINDOW_METRICS)
        assert w["metrics"]["win_rate_pct"]["live"] is not None
    else:
        assert "too few trades" in w["reason"]
    # the paper trades carry what the journal needs to pre-fill and link
    t = client.get(f"/api/runs/{run['id']}").json()["trade_list"][0]
    assert {"id", "direction", "entry_time", "entry_price", "exit_time", "exit_price", "size"} <= set(t)
