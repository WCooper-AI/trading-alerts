import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradealerts import analysis
from tradealerts.sample import synthetic_bars
from tradealerts.state import StateStore
from tradealerts.web import create_app

from .test_web import CSV_STRATEGY, H


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


def test_shuffled_market_keeps_candle_shapes_and_drift():
    df = synthetic_bars(days=60, freq="1h", seed=2)
    out = analysis.shuffled_market(df, np.random.default_rng(1))
    assert out.index.equals(df.index)
    assert (out["high"] >= out[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (out["low"] <= out[["open", "close"]].min(axis=1) + 1e-9).all()
    # same candles in another order: same end price, different path
    assert out["close"].iloc[-1] == pytest.approx(df["close"].iloc[-1])
    assert not np.allclose(out["close"].to_numpy(), df["close"].to_numpy())
    # candles only move to the same weekday and hour
    ret = lambda d: (d["close"] / d["close"].shift()).iloc[1:]
    rest = df.iloc[1:]  # the very first candle has no previous close to compare with
    for key, grp in rest.groupby([rest.index.dayofweek, rest.index.hour]):
        if key == (df.index[0].dayofweek, df.index[0].hour):
            continue  # holds the first candle, whose change is measured from its own open
        a = np.sort(ret(df).loc[grp.index].round(10).to_numpy())
        b = np.sort(ret(out).loc[grp.index].round(10).to_numpy())
        assert np.allclose(a, b)


def test_backtest_range_percentiles():
    idx = pd.date_range("2024-01-01", periods=24 * 200, freq="1h", tz="UTC")
    eq = pd.Series(1000 * 1.0001 ** np.arange(len(idx)), index=idx)
    bands = analysis.backtest_range(eq, [0, 24, 24 * 199])
    assert bands[0] == [0.0] * 5
    assert bands[1][2] == pytest.approx((1.0001 ** 24 - 1) * 100)
    assert bands[2] is None  # not enough stretches that long


def test_sweep_by_path(client):
    body = {"data": CSV_STRATEGY, "setting": {"kind": "path", "path": ["entry", "all", 0, "right"]},
            "values": [25, 30, 35, 40]}
    r = client.post("/api/sweep", json=body, headers=H)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [x["value"] for x in rows] == [25, 30, 35, 40]
    assert all(x["error"] is None and "return_pct" in x and "neighbourhood_pct" in x for x in rows)
    # lower RSI levels trigger less often
    assert rows[0]["trades"] <= rows[-1]["trades"]


def test_sweep_reports_bad_values_per_row(client):
    body = {"data": CSV_STRATEGY, "setting": {"kind": "path", "path": ["exit", "stop", "atr"]},
            "values": [-1, 2]}
    rows = client.post("/api/sweep", json=body, headers=H).json()["rows"]
    assert rows[0]["error"] and rows[1]["error"] is None


def test_sweep_limits(client):
    body = {"data": CSV_STRATEGY, "setting": {"kind": "path", "path": ["entry", "all", 0, "right"]}}
    assert client.post("/api/sweep", json={**body, "values": [1]}, headers=H).status_code == 422
    assert client.post("/api/sweep", json={**body, "values": list(range(30))}, headers=H).status_code == 422
    bad = {**body, "setting": {"kind": "path", "path": ["nope", 3]}, "values": [1, 2]}
    assert client.post("/api/sweep", json=bad, headers=H).status_code == 422


def test_sweep_recipe_knob(client, monkeypatch):
    from tradealerts.recipes import build_strategy, example_settings
    data = build_strategy("rsi", "us500", example_settings("rsi", timeframe="1h"))
    # point the built strategy at the CSV so no download happens
    data["market"] = {**CSV_STRATEGY["market"], "timeframe": "1h"}
    body = {"data": data, "setting": {"kind": "knob", "key": "level"}, "values": [20, 30, 40]}
    r = client.post("/api/sweep", json=body, headers=H)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [x["value"] for x in rows] == [20, 30, 40] and all(x["error"] is None for x in rows)


def test_luck_check(client):
    r = client.post("/api/luck", json={"data": CSV_STRATEGY}, headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    keys = [c["key"] for c in out["checks"]]
    assert keys[:2] == ["trades", "halves"] and "random" in keys
    assert 0 <= out["passed"] <= out["total"] <= 4
    assert out["random"]["runs"] >= 5 and len(out["random"]["returns"]) == out["random"]["runs"]
    if out["trades"] >= 5:
        assert 0 <= out["resample"]["p_losing"] <= 1
        assert out["order"]["bad_dd"] <= out["order"]["median_dd"] <= 0


def test_run_series_always_has_two_points_and_backtest_range(client, ws):
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    run = client.post(f"/api/strategies/{sid}/forward-test/start", headers=H).json()
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert len(detail["series"]) >= 2  # start point and now, even with no candles yet
    assert detail["series"][0][1] == detail["series"][-1][1] == 1000

    # pretend the test started near the end of the CSV, with a few candles counted
    store = StateStore(ws / "data" / "state.db")
    store.db.execute("UPDATE runs SET started_at = ? WHERE id = ?", ("2024-08-20T00:00:00+00:00", run["id"]))
    for i in range(1, 6):
        store.add_equity(run["id"], f"2024-08-2{i}T00:00:00+00:00", 1000 + i)
    store.commit()
    store.close()
    r = client.post(f"/api/runs/{run['id']}/backtest-range", json={}, headers=H)
    assert r.status_code == 200, r.text
    pts = r.json()["points"]
    assert pts and pts[0][1:] == [1000.0] * 5
    assert all(p[1] <= p[3] <= p[5] for p in pts)
