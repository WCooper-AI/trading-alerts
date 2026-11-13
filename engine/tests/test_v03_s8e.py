"""0.3 Session 8E: portfolio weighting modes and the one-shared-account replay."""

import copy

import pytest
from fastapi.testclient import TestClient

from tradealerts import analysis
from tradealerts.backtest import run_backtest
from tradealerts.sample import synthetic_bars
from tradealerts.web import create_app

from .conftest import make_strategy
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


def _parts(bars, risk_b=1):
    risk = {"account_size": 5000, "risk_per_trade_pct": 1, "point_value": 1, "compound": False}
    a = make_strategy(name="Long", exit={"stop": {"points": 40}, "target": {"points": 80}}, risk=risk)
    b = make_strategy(name="Short", direction="short", market={"symbol": "OTHER", "label": "Other", "timeframe": "1h"},
                      entry={"left": "close", "op": "<", "right": {"indicator": "sma", "period": 3}},
                      exit={"stop": {"points": 40}, "target": {"points": 80}}, risk={**risk, "risk_per_trade_pct": risk_b})
    return [("a", "Long", run_backtest(a, bars)), ("b", "Short", run_backtest(b, bars))]


# --------------------------------------------------------------------------- weights

def test_equal_weights_split_evenly(bars_1h):
    parts = _parts(bars_1h)
    assert analysis.portfolio_weights("equal", parts) == {"a": 0.5, "b": 0.5}
    assert analysis.portfolio_weights(None, parts) == {"a": 0.5, "b": 0.5}


def test_inverse_vol_gives_calmer_strategy_more(bars_1h):
    parts = _parts(bars_1h, risk_b=3)  # b risks 3x per trade, so its daily swings are bigger
    w = analysis.portfolio_weights("inverse_vol", parts)
    assert sum(w.values()) == pytest.approx(1)
    assert w["a"] > w["b"] > 0


def test_custom_weights_normalise_and_validate(bars_1h):
    parts = _parts(bars_1h)
    assert analysis.portfolio_weights("custom", parts, {"a": 75, "b": 25}) == {"a": 0.75, "b": 0.25}
    assert analysis.portfolio_weights("custom", parts, {"a": 3, "b": 1}) == {"a": 0.75, "b": 0.25}
    with pytest.raises(ValueError):
        analysis.portfolio_weights("custom", parts, {"a": 100})
    with pytest.raises(ValueError):
        analysis.portfolio_weights("custom", parts, {"a": -1, "b": 101})
    with pytest.raises(ValueError):
        analysis.portfolio_weights("custom", parts, {"a": 0, "b": 0})
    with pytest.raises(ValueError):
        analysis.portfolio_weights("nope", parts)


def test_equal_weighting_matches_unweighted_portfolio(bars_1h):
    parts = _parts(bars_1h)
    plain = analysis.portfolio(parts, 5000.0)
    eq = analysis.portfolio(parts, 5000.0, analysis.portfolio_weights("equal", parts))
    assert eq["metrics"]["end_equity"] == pytest.approx(plain["metrics"]["end_equity"])
    assert eq["metrics"]["max_open_risk_pct"] == pytest.approx(plain["metrics"]["max_open_risk_pct"])
    assert all(p["weight_pct"] == pytest.approx(50) for p in eq["per_strategy"])


def test_custom_weighting_scales_each_strategy(bars_1h):
    parts = _parts(bars_1h)
    plain = {p["id"]: p for p in analysis.portfolio(parts, 5000.0)["per_strategy"]}
    out = analysis.portfolio(parts, 5000.0, {"a": 0.75, "b": 0.25})
    per = {p["id"]: p for p in out["per_strategy"]}
    assert per["a"]["pnl"] == pytest.approx(plain["a"]["pnl"] * 1.5)
    assert per["b"]["pnl"] == pytest.approx(plain["b"]["pnl"] * 0.5)
    assert per["a"]["weight_pct"] == 75 and per["b"]["weight_pct"] == 25
    assert per["a"]["trades"] == plain["a"]["trades"]
    assert out["metrics"]["end_equity"] == pytest.approx(5000 + per["a"]["pnl"] + per["b"]["pnl"])


# --------------------------------------------------------------------------- shared account

def test_shared_account_without_caps_takes_every_trade(bars_1h):
    parts = _parts(bars_1h)
    out = analysis.shared_account(parts, 5000.0)
    per = {p["id"]: p for p in out["per_strategy"]}
    for sid, _, res in parts:
        assert per[sid]["taken"] + per[sid]["skipped"] == len(res.trades)
        assert per[sid]["skipped"] == 0
    m = out["metrics"]
    assert m["trades"] == sum(len(res.trades) for _, _, res in parts)
    assert m["trades_skipped"] == 0 and out["skipped"] == {"open_trades": 0, "open_risk": 0, "too_small": 0}
    assert m["end_equity"] == pytest.approx(5000 + sum(p["pnl"] for p in per.values()))
    assert float(out["pnl_curves"].sum(axis=1).iloc[-1]) == pytest.approx(m["end_equity"] - 5000)
    assert m["max_drawdown_pct"] <= 0 and 0 <= m["time_in_market_pct"] <= 100
    assert len(out["equity"]) == len(out["drawdown"]) == len(out["open_risk"])


def test_shared_account_compounds_unlike_summed_curves(bars_1h):
    parts = _parts(bars_1h)
    summed = analysis.portfolio(parts, 5000.0)["metrics"]["end_equity"]
    shared = analysis.shared_account(parts, 5000.0)["metrics"]["end_equity"]
    # sizing from the live balance changes the result; the direction of travel is the same
    assert shared != pytest.approx(summed, abs=1e-6)
    assert (shared > 5000) == (summed > 5000)


def test_shared_account_max_open_trades_skips_overlaps(bars_1h):
    parts = _parts(bars_1h)
    free = analysis.shared_account(parts, 5000.0)
    assert free["metrics"]["max_open_trades"] >= 2, "fixture needs overlapping trades"
    out = analysis.shared_account(parts, 5000.0, max_open_trades=1)
    assert out["metrics"]["max_open_trades"] <= 1
    assert out["skipped"]["open_trades"] > 0
    assert out["metrics"]["trades"] + out["metrics"]["trades_skipped"] == free["metrics"]["trades"]
    assert sum(p["skipped"] for p in out["per_strategy"]) == out["metrics"]["trades_skipped"]


def test_shared_account_open_risk_cap_holds(bars_1h):
    parts = _parts(bars_1h)
    out = analysis.shared_account(parts, 5000.0, max_open_risk_pct=1.0)
    assert out["metrics"]["max_open_risk_pct"] <= 1.0 + 0.05
    assert out["skipped"]["open_risk"] > 0


def test_shared_account_weights_shift_risk(bars_1h):
    parts = _parts(bars_1h)
    heavy_a = analysis.shared_account(parts, 5000.0, {"a": 0.9, "b": 0.1})
    heavy_b = analysis.shared_account(parts, 5000.0, {"a": 0.1, "b": 0.9})
    pa = {p["id"]: p for p in heavy_a["per_strategy"]}
    pb = {p["id"]: p for p in heavy_b["per_strategy"]}
    assert pa["a"]["weight_pct"] == 90 and pb["a"]["weight_pct"] == pytest.approx(10)
    assert abs(pa["a"]["pnl"]) > abs(pb["a"]["pnl"])
    assert abs(pa["b"]["pnl"]) < abs(pb["b"]["pnl"])


# --------------------------------------------------------------------------- endpoint

def _two_ids(client):
    a = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    other = copy.deepcopy(CSV_STRATEGY)
    other["name"] = "CSV short"
    other["direction"] = "short"
    other["entry"]["all"][0]["op"] = "crosses_below"
    other["entry"]["all"][0]["right"] = 65
    b = client.post("/api/strategies", json={"data": other}, headers=H).json()["id"]
    return a, b


def test_portfolio_route_default_is_equal_summed(client):
    a, b = _two_ids(client)
    r = client.post("/api/portfolio", json={"ids": [a, b], "balance": 5000}, headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["weighting"] == "equal" and out["shared"] is False and out["skipped"] is None
    assert out["weights"] == {a: 50.0, b: 50.0}
    assert all(p["weight_pct"] == 50 for p in out["per_strategy"])


def test_portfolio_route_custom_weights_and_shared(client):
    a, b = _two_ids(client)
    body = {"ids": [a, b], "balance": 5000, "weighting": "custom", "weights": {a: 60, b: 40},
            "shared": True, "max_open_trades": 1, "max_open_risk_pct": 2}
    r = client.post("/api/portfolio", json=body, headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["shared"] is True and out["weighting"] == "custom"
    assert out["weights"][a] == pytest.approx(60) and out["weights"][b] == pytest.approx(40)
    assert set(out["skipped"]) == {"open_trades", "open_risk", "too_small"}
    assert "trades_skipped" in out["metrics"] and out["metrics"]["max_open_trades"] <= 1
    assert all({"taken", "skipped", "weight_pct"} <= set(p) for p in out["per_strategy"])
    assert set(out["pnl_curves"]) == {a, b}
    r = client.post("/api/portfolio", json={"ids": [a, b], "balance": 5000, "weighting": "inverse_vol"}, headers=H)
    assert r.status_code == 200 and r.json()["weighting"] == "inverse_vol"
    assert sum(r.json()["weights"].values()) == pytest.approx(100)


@pytest.mark.parametrize("extra,msg", [
    ({"weighting": "lucky"}, "Choose a weighting"),
    ({"weighting": "custom"}, "share"),
    ({"weighting": "custom", "weights": {"only-one": 50}}, "share"),
    ({"weighting": "custom", "weights": "__both_zero__"}, "add up"),
    ({"shared": True, "max_open_trades": 0}, "whole number"),
    ({"shared": True, "max_open_trades": 1.5}, "whole number"),
    ({"shared": True, "max_open_risk_pct": -2}, "above 0"),
    ({"max_open_trades": 2}, "share one account"),
])
def test_portfolio_route_rejects_bad_options(client, extra, msg):
    a, b = _two_ids(client)
    if extra.get("weights") == "__both_zero__":
        extra = {**extra, "weights": {a: 0, b: 0}}
    r = client.post("/api/portfolio", json={"ids": [a, b], "balance": 5000, **extra}, headers=H)
    assert r.status_code == 422, r.text
    assert msg in r.json()["detail"]
