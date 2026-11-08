import json

import pytest
from fastapi.testclient import TestClient

from tradealerts.backtest import run_backtest
from tradealerts.recipes import (MARKETS, RECIPES, MissingSettings, RecipeError, build_strategy, example_settings,
                                still_matches_recipe)
from tradealerts.sample import synthetic_bars
from tradealerts.strategy import check_runtime, strategy_from_dict
from tradealerts.web import create_app

H = {"X-Requested-With": "tradealerts"}


@pytest.mark.parametrize("recipe", [r.id for r in RECIPES])
@pytest.mark.parametrize("direction", ["long", "short"])
def test_every_recipe_builds_a_valid_strategy_that_trades(recipe, direction):
    data = build_strategy(recipe, "us500", example_settings(recipe, direction=direction))
    s = strategy_from_dict(json.loads(json.dumps(data)))
    check_runtime(s)
    assert s.direction == direction and still_matches_recipe(data)
    tf = data["market"]["timeframe"]
    bars = synthetic_bars(days=400 if tf not in ("5m", "15m") else 60,
                          freq="15min" if tf in ("5m", "15m") else "1h", seed=21)
    from tradealerts.data import resample_ohlcv
    res = run_backtest(s, resample_ohlcv(bars, tf))
    assert res.metrics["trades"] > 0, f"{recipe}/{direction} never traded on sample data"


def test_short_mirrors_long():
    long = build_strategy("rsi", "gold", example_settings("rsi", direction="long", level=25))
    short = build_strategy("rsi", "gold", example_settings("rsi", direction="short", level=25))
    lrule, srule = long["entry"]["all"][0], short["entry"]["all"][0]
    assert (lrule["op"], lrule["right"]) == ("crosses_above", 25)
    assert (srule["op"], srule["right"]) == ("crosses_below", 75)
    trend = build_strategy("trend", "gold", example_settings("trend", direction="short"))
    assert trend["entry"]["all"][0]["right"]["indicator"] == "lowest"
    assert trend["exit"]["when"]["all"][0]["right"]["indicator"] == "highest"


def test_market_defaults_applied():
    fx = build_strategy("dip", "eurusd", example_settings("dip"))
    assert fx["risk"]["point_value"] == 10000 and fx["costs"]["spread"] < 0.001
    assert fx["market"] == {"symbol": "eurusd", "label": "EUR/USD", "provider": "auto", "timeframe": "4h"}
    assert fx["name"] == "Buy the dip on EUR/USD" and fx["costs"]["funding_pct"] > 0


@pytest.mark.parametrize("recipe,settings,msg", [
    ("dip", {"dip_pct": 0}, "between"),
    ("dip", {"timeframe": "5m"}, "works on"),
    ("ma", {"fast": 50, "slow": 20}, "fewer candles"),
    ("orb", {"entry_until": "09:45"}, "Only enter until"),
    ("dip", {"risk_pct": 50}, "Risk on each trade"),
    ("dip", {"stop": "huge"}, "Choose one"),
])
def test_bad_settings_explained(recipe, settings, msg):
    with pytest.raises(RecipeError, match=msg):
        build_strategy(recipe, "us500", example_settings(recipe, **settings))


def test_blank_settings_are_listed_not_defaulted():
    with pytest.raises(MissingSettings) as exc:
        build_strategy("rsi", "us500", {"level": 30})
    assert "RSI measured over" in exc.value.labels and "Buy or sell" in exc.value.labels
    assert "Buy when RSI climbs back above" not in exc.value.labels
    assert str(exc.value).startswith("Still to fill in: ")
    # nothing in the public recipe carries a starting value
    for r in RECIPES:
        assert all(k["default"] is None and "example" not in k for k in r.public()["knobs"])


def test_orb_refuses_markets_without_an_open():
    with pytest.raises(RecipeError, match="opening time"):
        build_strategy("orb", "btc", {})


def test_hand_edited_rules_drop_the_recipe(tmp_path):
    (tmp_path / "config.yaml").write_text("{}\n")
    c = TestClient(create_app(tmp_path / "config.yaml"))
    meta = c.get("/api/recipes").json()
    assert {r["id"] for r in meta["recipes"]} == {r.id for r in RECIPES}
    assert len(meta["markets"]) == len(MARKETS)

    built = c.post("/api/recipes/build", json={"recipe": "dip", "market": "us500",
                                              "settings": example_settings("dip", dip_pct=2)}, headers=H).json()
    assert built["recipe"]["settings"]["dip_pct"] == 2
    sid = c.post("/api/strategies", json={"data": {**built, "template": True}}, headers=H).json()["id"]
    assert "recipe" in c.get(f"/api/strategies/{sid}").json()["data"]

    # changing risk keeps it a guided strategy
    built["risk"]["point_value"] = 2
    c.put(f"/api/strategies/{sid}", json={"data": built}, headers=H)
    assert "recipe" in c.get(f"/api/strategies/{sid}").json()["data"]

    # adding a rule by hand makes it custom
    built["entry"]["all"].append({"left": "close", "op": ">", "right": 0})
    c.put(f"/api/strategies/{sid}", json={"data": built}, headers=H)
    assert "recipe" not in c.get(f"/api/strategies/{sid}").json()["data"]

    bad = c.post("/api/recipes/build", json={"recipe": "ma", "market": "us500",
                                            "settings": example_settings("ma", fast=60, slow=10)}, headers=H)
    assert bad.status_code == 422 and "fewer candles" in bad.json()["detail"]
