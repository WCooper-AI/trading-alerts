"""Tests for 0.3: strategy versions, forward-test snapshots, the reproducibility stamp and activity."""

import copy

import pytest
import yaml
from fastapi.testclient import TestClient

from tradealerts.state import StateStore
from tradealerts.web import create_app
from tradealerts.workspace import Limits

from .test_web import CSV_STRATEGY, H, client, ws  # noqa: F401  (fixtures)


def edited(**changes):
    d = copy.deepcopy(CSV_STRATEGY)
    for k, v in changes.items():
        d[k] = v
    return d


NEW_ENTRY = {"all": [{"left": {"indicator": "rsi", "period": 14}, "op": "crosses_above", "right": 40}]}


# --------------------------------------------------------------------------- versions


def test_every_save_keeps_a_version(client, ws):
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    r = client.put(f"/api/strategies/{sid}", json={"data": edited(entry=NEW_ENTRY)}, headers=H)
    assert r.status_code == 200 and r.json()["version"] == 2
    # saving it unchanged doesn't add one
    assert client.put(f"/api/strategies/{sid}", json={"data": edited(entry=NEW_ENTRY)}, headers=H).json()["version"] == 2
    vs = client.get(f"/api/strategies/{sid}/versions").json()
    assert [v["version_no"] for v in vs["versions"]] == [2, 1] and vs["current"] == 2
    assert [v["note"] for v in vs["versions"]] == ["Saved", "Created"]
    v1 = client.get(f"/api/strategies/{sid}/versions/1").json()
    assert v1["data"]["entry"] == CSV_STRATEGY["entry"] and "entry:" in v1["yaml"]
    assert client.get(f"/api/strategies/{sid}/versions/9").status_code == 404


def test_hand_edits_become_versions(client, ws):
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    f = ws / "strategies" / f"{sid}.yaml"
    f.write_text(f.read_text().replace("right: 35", "right: 33"))
    assert client.get(f"/api/strategies/{sid}").json()["version"] == 2
    vs = client.get(f"/api/strategies/{sid}/versions").json()["versions"]
    assert vs[0]["note"] == "Edited outside the app"


def test_restore_saves_a_new_version_and_keeps_alerts_as_they_are(client, ws):
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    client.put(f"/api/strategies/{sid}", json={"data": edited(entry=NEW_ENTRY, template=False)}, headers=H)
    r = client.post(f"/api/strategies/{sid}/versions/1/restore", json={"on_running": "keep"}, headers=H)
    assert r.status_code == 200 and r.json()["version"] == 3
    got = client.get(f"/api/strategies/{sid}").json()["data"]
    assert got["entry"] == CSV_STRATEGY["entry"]
    assert got["template"] is False  # version 1 was a draft; alerts weren't switched off by restoring it
    notes = [v["note"] for v in client.get(f"/api/strategies/{sid}/versions").json()["versions"]]
    assert notes[0] == "Restored version 1"


def test_hosted_keeps_the_newest_versions(ws):
    app = create_app(ws / "config.yaml")
    app.state.ctx.ws.limits = Limits(max_versions=3)
    c = TestClient(app)
    sid = c.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    for level in (31, 32, 33, 34):
        entry = {"all": [{"left": {"indicator": "rsi", "period": 14}, "op": "crosses_above", "right": level}]}
        c.put(f"/api/strategies/{sid}", json={"data": edited(entry=entry)}, headers=H)
    assert [v["version_no"] for v in c.get(f"/api/strategies/{sid}/versions").json()["versions"]] == [5, 4, 3]


def test_deleting_a_strategy_removes_its_versions(client, ws):
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    client.delete(f"/api/strategies/{sid}", headers=H)
    store = StateStore(ws / "data" / "state.db")
    assert store.versions(sid) == []
    store.close()


# --------------------------------------------------------------------------- running forward tests


def start(client):
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    run = client.post(f"/api/strategies/{sid}/forward-test/start", headers=H).json()
    return sid, run


def test_forward_test_keeps_a_copy_of_its_rules(client, ws):
    sid, run = start(client)
    store = StateStore(ws / "data" / "state.db")
    row = store.run(run["id"])
    assert row["source_hash"] and yaml.safe_load(row["strategy_yaml"])["entry"] == CSV_STRATEGY["entry"]
    store.close()
    assert client.get(f"/api/runs/{run['id']}").json()["version"] == 1
    assert "strategy_yaml" not in client.get("/api/dashboard").json()["running"][0]


def test_changing_rules_with_a_running_test_asks_first(client, ws):
    sid, run = start(client)
    r = client.put(f"/api/strategies/{sid}", json={"data": edited(entry=NEW_ENTRY)}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "running_test"
    assert client.get(f"/api/strategies/{sid}").json()["data"]["entry"] == CSV_STRATEGY["entry"]  # not saved
    # a new name isn't a rule change: no question
    assert client.put(f"/api/strategies/{sid}", json={"data": edited(name="Renamed")}, headers=H).status_code == 200


def test_keep_running_with_new_rules(client, ws):
    sid, run = start(client)
    r = client.put(f"/api/strategies/{sid}", json={"data": edited(entry=NEW_ENTRY), "on_running": "keep"}, headers=H)
    assert r.json()["forward_test"] == "kept"
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["active"] and detail["version"] == 2
    assert any(e["kind"] == "strategy_saved" and "next candle" in e["message"] for e in detail["events"])


def test_restart_finishes_the_test_and_starts_a_fresh_one(client, ws):
    sid, run = start(client)
    r = client.put(f"/api/strategies/{sid}", json={"data": edited(entry=NEW_ENTRY), "on_running": "restart"}, headers=H)
    assert r.json()["forward_test"] == "restarted"
    old = client.get(f"/api/runs/{run['id']}").json()
    assert not old["active"] and old["version"] == 1
    new = client.get("/api/dashboard").json()["running"][0]
    assert new["id"] != run["id"] and new["version"] == 2


def test_backtest_range_uses_the_rules_the_test_ran_on(client, ws):
    sid, run = start(client)
    store = StateStore(ws / "data" / "state.db")
    store.db.execute("UPDATE runs SET started_at = ? WHERE id = ?", ("2024-08-20T00:00:00+00:00", run["id"]))
    for i in range(1, 6):
        store.add_equity(run["id"], f"2024-08-2{i}T00:00:00+00:00", 1000 + i)
    store.commit()
    store.close()
    client.post(f"/api/strategies/{sid}/forward-test/stop", headers=H)
    client.delete(f"/api/strategies/{sid}", headers=H)
    r = client.post(f"/api/runs/{run['id']}/backtest-range", json={}, headers=H)
    assert r.status_code == 200, r.text


def test_hand_edit_while_running_is_noted_by_the_checker(client, ws):
    import pandas as pd

    from tradealerts.config import load_config
    from tradealerts.runner import run_cycle

    sid, run = start(client)
    f = ws / "strategies" / f"{sid}.yaml"
    f.write_text(f.read_text().replace("right: 35", "right: 33"))
    cfg = load_config(ws / "config.yaml")
    store = StateStore(cfg.state_db)
    run_cycle(cfg, store, [], now=pd.Timestamp.now(tz="UTC"))
    row = store.run(run["id"])
    assert "right: 33" in row["strategy_yaml"]
    assert any(e["kind"] == "strategy_saved" and "outside the app" in e["message"]
               for e in store.recent_events(10, run_id=run["id"]))
    assert store.latest_version(sid)["note"] == "Edited outside the app"
    store.close()


# --------------------------------------------------------------------------- reproducibility stamp


def test_backtest_stamp_says_what_was_tested(client, ws):
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    # the editor sends empty sections the file doesn't have; same rules, same version
    sent = edited(costs={}, alerts={}, name="Renamed on screen")
    r = client.post("/api/backtest", json={"data": sent, "strategy_id": sid}, headers=H)
    meta = r.json()["meta"]
    assert meta["version"] == 1 and meta["source"] == "CSV file" and meta["timeframe"] == "1h"
    assert meta["engine_version"] and meta["start"] and meta["end"] and len(meta["rules_hash"]) == 12
    unsaved = client.post("/api/backtest", json={"data": edited(entry=NEW_ENTRY), "strategy_id": sid}, headers=H).json()["meta"]
    assert unsaved["version"] is None and unsaved["rules_hash"] != meta["rules_hash"]
    assert client.post("/api/backtest", json={"data": sent}, headers=H).json()["meta"]["version"] is None


def test_empty_sections_are_not_a_rule_change(client, ws):
    sid, run = start(client)
    r = client.put(f"/api/strategies/{sid}", json={"data": edited(costs={}, alerts={})}, headers=H)
    assert r.status_code == 200



# --------------------------------------------------------------------------- activity log


def test_activity_log_filters_and_unread(client, ws):
    sid, run = start(client)
    client.put(f"/api/strategies/{sid}", json={"data": edited(entry=NEW_ENTRY), "on_running": "keep"}, headers=H)
    st = client.get("/api/settings").json()
    st["poll_minutes"] = 30
    client.put("/api/settings", json=st, headers=H)
    store = StateStore(ws / "data" / "state.db")
    store.add_event(sid, "2025-01-01T10:00:00+00:00", "signal", "Rules met\nRSI crossed", True, run["id"])
    store.add_event(sid, "2025-01-01T11:00:00+00:00", "alert_failed", "Alert not sent: Rules met\nntfy: timed out", False, run["id"])
    store.close()

    everything = client.get("/api/activity").json()
    kinds = [x["kind"] for x in everything["items"]]
    assert kinds[:4] == ["alert_failed", "signal", "settings_changed", "strategy_saved"]
    assert all(x["unread"] for x in everything["items"]) and everything["unread_count"] == len(everything["items"])
    saved = next(x for x in everything["items"] if x["kind"] == "strategy_saved")
    assert saved["title"].startswith("CSV test: saved") and saved["link"] == f"#/history/{sid}"
    settings = next(x for x in everything["items"] if x["kind"] == "settings_changed")
    assert "how often prices are checked" in settings["body"] and settings["link"] == "#/settings"

    assert [x["kind"] for x in client.get("/api/activity?category=problems").json()["items"]] == ["alert_failed"]
    # two saves: "Created" when the strategy was made, then the rule change
    assert [x["kind"] for x in client.get("/api/activity?category=changes").json()["items"]] == ["settings_changed", "strategy_saved", "strategy_saved"]
    assert client.get("/api/activity?category=trades").json()["items"] == []
    assert client.get("/api/activity?category=nope").status_code == 422
    assert len(client.get(f"/api/activity?strategy={sid}").json()["items"]) == 4  # the settings change isn't the strategy's

    # looking marks everything seen, on the server, so another browser agrees
    client.post("/api/activity/seen", headers=H)
    after = client.get("/api/activity").json()
    assert after["unread_count"] == 0 and not any(x["unread"] for x in after["items"])
    assert client.get("/api/activity?unread=1").json()["items"] == []
    store = StateStore(ws / "data" / "state.db")
    store.add_event(sid, "2025-01-01T12:00:00+00:00", "closed", "Trade closed\n+£12", True, run["id"])
    store.close()
    fresh = client.get("/api/activity?unread=1").json()
    assert [x["kind"] for x in fresh["items"]] == ["closed"] and fresh["unread_count"] == 1


def test_failed_alerts_are_recorded(ws):
    import pandas as pd

    from tradealerts.config import load_config
    from tradealerts.notify import Notifier
    from tradealerts.runner import run_cycle
    from tradealerts.sample import synthetic_bars

    class Broken(Notifier):
        name = "ntfy"

        def send(self, msg):
            raise RuntimeError("timed out")

    c = TestClient(create_app(ws / "config.yaml"))
    sid = c.post("/api/strategies", json={"data": {**CSV_STRATEGY, "template": False}}, headers=H).json()["id"]
    cfg = load_config(ws / "config.yaml")
    store = StateStore(cfg.state_db)
    bars = synthetic_bars(days=90, freq="1h", seed=4)
    run = store.active_run(sid) or c.post(f"/api/strategies/{sid}/forward-test/start", headers=H).json()
    store.db.execute("UPDATE runs SET started_at = ? WHERE id = ?", (bars.index[1000].isoformat(), run["id"]))
    store.commit()
    for i in range(1001, len(bars), 7):
        run_cycle(cfg, store, [Broken()], now=bars.index[i] + pd.Timedelta("1h"))
    failed = store.events_page(["alert_failed"])
    assert failed and "ntfy: timed out" in failed[0]["message"]
    store.close()


# --------------------------------------------------------------------------- one status everywhere


def test_market_closed_until_covers_weekend_and_daily_break():
    import pandas as pd

    from tradealerts import markethours

    sat = pd.Timestamp("2026-11-07 12:00", tz="UTC")
    back, why = markethours.closed_until("us500", sat)
    assert why == "weekend" and back == pd.Timestamp("2026-11-08 23:00", tz="UTC")
    # Tuesday 17:30 New York (22:30 UTC in winter): the daily break, back at 18:00 New York
    back, why = markethours.closed_until("us500", pd.Timestamp("2026-11-10 22:30", tz="UTC"))
    assert why == "daily break" and back == pd.Timestamp("2026-11-10 23:00", tz="UTC")
    assert markethours.closed_until("us500", pd.Timestamp("2026-11-10 23:00", tz="UTC")) is None
    assert markethours.closed_until("btc", pd.Timestamp("2026-11-10 22:30", tz="UTC")) is None


def test_status_dashboard_and_strategies_agree_on_each_tests_state(client, ws, monkeypatch):
    import pandas as pd

    from tradealerts import markethours

    sid, run = start(client)
    states = lambda: (  # noqa: E731
        client.get("/api/status").json()["states"],
        client.get("/api/dashboard").json()["running"][0]["health"],
        client.get(f"/api/strategies").json()["strategies"][0]["live"]["run"]["health"],
        client.get(f"/api/runs/{run['id']}").json()["health"],
    )
    # no live checker in the test app: every view says the test needs attention
    counts, dash, listed, detail = states()
    assert counts["attention"] == 1 and dash["state"] == listed["state"] == detail["state"] == "attention"
    assert "Live checks are off" in detail["text"]
    # a working checker on a Saturday: waiting, and all three know when the market reopens
    client.app.state.ctx.runner_status = lambda ws: {"enabled": True, "last_run": None, "next_run": None, "problems": {}}
    monkeypatch.setattr(markethours, "closed_until", lambda symbol, now=None: (pd.Timestamp("2026-11-08 23:00", tz="UTC"), "weekend"))
    counts, dash, listed, detail = states()
    assert counts == {"watching": 0, "waiting": 1, "attention": 0, "stopped": 0}
    assert dash["state"] == listed["state"] == detail["state"] == "waiting" and dash["reopens"] == listed["reopens"]
    assert client.get("/api/status").json()["reopens"] == dash["reopens"]
    monkeypatch.setattr(markethours, "closed_until", lambda symbol, now=None: None)
    counts, dash, listed, detail = states()
    assert counts["watching"] == 1 and {dash["state"], listed["state"], detail["state"]} == {"watching"}
    client.post(f"/api/strategies/{sid}/forward-test/stop", headers=H)
    assert client.get(f"/api/runs/{run['id']}").json()["health"]["state"] == "stopped"


def test_dashboard_cards_carry_what_each_test_is_doing(client, ws):
    import pandas as pd

    from tradealerts.config import load_config
    from tradealerts.runner import run_cycle
    from tradealerts.sample import synthetic_bars

    sid, run = start(client)
    cfg = load_config(ws / "config.yaml")
    store = StateStore(cfg.state_db)
    bars = synthetic_bars(days=90, freq="1h", seed=4)
    store.db.execute("UPDATE runs SET started_at = ? WHERE id = ?", (bars.index[1000].isoformat(), run["id"]))
    store.commit()
    for i in range(1001, len(bars), 7):
        run_cycle(cfg, store, [], now=bars.index[i] + pd.Timedelta("1h"))
    rec = store.load(sid)
    store.close()
    client.app.state.ctx.runner_status = lambda ws: {"enabled": True, "last_run": "2026-11-10T14:02:00+00:00",
                                                     "next_run": "2026-11-10T14:15:00+00:00", "problems": {}}
    card = client.get("/api/dashboard").json()["running"][0]
    assert card["health"]["last_check"] == "2026-11-10T14:02:00+00:00" and card["health"]["next_check"]
    assert card["candles"] > 0 and "pending" in card
    if rec["state"]["position"]:
        assert card["position"]["stop"] and "unrealised" in card["position"]
    else:
        assert card["position"] is None


def test_strategy_list_spark_only_for_running_tests(client, ws):
    client.post("/api/strategies", json={"data": {**CSV_STRATEGY, "name": "Draft only"}}, headers=H)
    sid, run = start(client)
    items = {x["id"]: x for x in client.get("/api/strategies").json()["strategies"]}
    assert "spark" in items[sid] and items[sid]["live"]["run"]["trades"] == 0
    assert "spark" not in items["draft-only"] and items["draft-only"]["live"] == {}


def test_guided_preview_shows_gaps_until_filled(client):
    from tradealerts.recipes import RECIPES, build_strategy, preview_strategy

    for recipe in RECIPES:
        market = "us500" if recipe.id != "orb" else "us100"
        empty = preview_strategy(recipe.id, market, {})
        assert empty["data"]["entry"] and "direction" not in empty["data"] and "account_size" not in empty["data"]["risk"]
        assert not empty["complete"]
        full = {k["key"]: k.get("example") for k in recipe.all_knobs() if k.get("example") is not None}
        full.update(direction="long", timeframe=recipe.default_timeframe, balance=1000, risk_pct=1, currency="GBP")
        done = preview_strategy(recipe.id, market, full)
        built = build_strategy(recipe.id, market, full)
        assert done["complete"] and done["placeholders"] == [] and done["data"]["entry"] == built["entry"]
    r = client.post("/api/recipes/preview", json={"recipe": "rsi", "market": "uk100", "settings": {"level": 25}}, headers=H)
    assert r.status_code == 200 and 25 not in r.json()["placeholders"] and r.json()["data"]["market"]["symbol"] == "uk100"


# --------------------------------------------------------------------------- trade drill-down


def test_backtest_payload_has_benchmark_and_trade_details(client):
    r = client.post("/api/backtest", json={"data": CSV_STRATEGY}, headers=H).json()
    m = r["metrics"]
    assert r["benchmark"][0][1] == pytest.approx(m["start_equity"], rel=0.02)  # the first candle's own move
    assert r["benchmark"][-1][1] == pytest.approx(m["start_equity"] * (1 + m["buy_and_hold_pct"] / 100), rel=1e-6)
    assert len(r["benchmark"]) == len(r["equity"])
    if r["trades"]:
        t = r["trades"][0]
        assert "mae_r" in t and "entry_reason" in t
        marks = [mk for mk in r["chart"]["marks"] if "trade" in mk]
        assert marks and all(0 <= mk["trade"] < len(r["trades"]) for mk in marks)
        # the candles cover the first trade
        import pandas as pd
        assert pd.Timestamp(r["chart"]["candles"][0][0]) <= pd.Timestamp(t["entry_time"])


def test_forward_test_trades_keep_the_details(client, ws):
    import pandas as pd

    from tradealerts.config import load_config
    from tradealerts.runner import run_cycle
    from tradealerts.sample import synthetic_bars

    sid, run = start(client)
    cfg = load_config(ws / "config.yaml")
    store = StateStore(cfg.state_db)
    bars = synthetic_bars(days=90, freq="1h", seed=4)
    store.db.execute("UPDATE runs SET started_at = ? WHERE id = ?", (bars.index[1000].isoformat(), run["id"]))
    store.commit()
    for i in range(1001, len(bars), 7):
        run_cycle(cfg, store, [], now=bars.index[i] + pd.Timedelta("1h"))
    trades = store.trades(run_id=run["id"])
    store.close()
    assert trades and isinstance(trades[0]["entry_reason"], dict) and "RSI(14)" in trades[0]["entry_reason"]
    assert trades[0]["mae_r"] <= 0 <= trades[0]["mfe_r"]
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["trade_list"][0]["entry_reason"]


# --------------------------------------------------------------------------- indicator overlays


def test_chart_carries_the_indicators_the_rule_reads(client):
    r = client.post("/api/backtest", json={"data": CSV_STRATEGY}, headers=H).json()
    inds = r["chart"]["indicators"]
    assert [i["pane"] for i in inds] == ["oscillator"] and inds[0]["label"] == "RSI(14)"
    assert len(inds[0]["values"]) == len(r["chart"]["candles"])
    assert {35.0, 30.0, 70.0} <= set(inds[0]["levels"])  # the rule's level plus RSI's usual ones
    bands = edited(entry={"all": [{"left": "close", "op": "<", "right": {"indicator": "bollinger_lower", "period": 20}},
                                  {"left": "close", "op": ">", "right": {"indicator": "bollinger_upper", "period": 20, "shift": 1}}]})
    inds = client.post("/api/backtest", json={"data": bands}, headers=H).json()["chart"]["indicators"]
    assert [(i["name"], i["pane"]) for i in inds] == [("bollinger_lower", "price"), ("bollinger_upper", "price")]
    assert inds[1]["label"] == "BOLLINGER UPPER(20) 1 bar ago" and inds[1]["levels"] == []
    plain = edited(entry={"left": "close", "op": ">", "right": 5000})
    assert client.post("/api/backtest", json={"data": plain}, headers=H).json()["chart"]["indicators"] == []
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    c = client.get(f"/api/candles?strategy={sid}&bars=100").json()
    assert c["indicators"][0]["label"] == "RSI(14)" and len(c["indicators"][0]["values"]) == len(c["candles"])


# --------------------------------------------------------------------------- markets feed status


def test_candles_feed_status_fresh_delayed_saved(client, ws, monkeypatch):
    import pandas as pd

    from tradealerts.sample import synthetic_bars
    from tradealerts.web.routes import markets as markets_route

    # the sample CSV stops in 2024: answered, but far behind
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    d = client.get(f"/api/candles?strategy={sid}&bars=50").json()
    assert d["feed"] == "delayed" and d["age_minutes"] > 60 * 24 * 30
    # a CSV that runs up to now: fresh
    recent = synthetic_bars(days=10, freq="1h", seed=2)
    recent.index = recent.index + (pd.Timestamp.now(tz="UTC").floor("h") - recent.index[-1])
    recent.to_csv(ws / "data" / "recent.csv")
    fresh = {**CSV_STRATEGY, "name": "Recent", "market": {**CSV_STRATEGY["market"], "symbol": "data/recent.csv"}}
    sid2 = client.post("/api/strategies", json={"data": fresh}, headers=H).json()["id"]
    d = client.get(f"/api/candles?strategy={sid2}&bars=50").json()
    assert d["feed"] == "fresh" and (d["age_minutes"] or 0) <= 60
    # the feed stops answering: the saved copy is shown and the badge says so
    def broken(*a, **k):
        class P:
            def fetch_bars(self, *a2, **k2):
                raise RuntimeError("no answer")
        return P()
    monkeypatch.setattr(markets_route.data_mod, "provider_for", broken)
    import time

    cache = client.app.state.ctx.live_cache
    for k, (t, v) in list(cache._items.items()):
        cache._items[k] = (time.monotonic() - cache.ttl - 1, v)  # just expired: the feed is asked again, the copy kept
    r = client.get(f"/api/candles?strategy={sid2}&bars=50")
    assert r.status_code == 200, r.text
    assert r.json()["feed"] == "saved" and r.json()["stale"] is True


# --------------------------------------------------------------------------- session 5: deeper metrics, Monte Carlo, diagnostics


def test_deeper_metrics_in_backtest_payload(client):
    r = client.post("/api/backtest", json={"data": CSV_STRATEGY}, headers=H).json()
    m = r["metrics"]
    assert m["trades"] > 1
    for k in ("sharpe", "sortino", "longest_winning_streak", "longest_losing_streak", "largest_win", "largest_loss",
              "avg_hold_hours", "longest_underwater_days", "monthly_returns", "r_histogram", "hold_histogram"):
        assert k in m, k
    assert m["largest_win"] >= max(t["pnl"] for t in r["trades"]) - 1e-9
    assert m["largest_loss"] <= min(t["pnl"] for t in r["trades"]) + 1e-9
    assert m["avg_hold_hours"] > 0 and m["longest_underwater_days"] >= 0
    assert sum(b["count"] for b in m["r_histogram"]) == m["trades"]
    assert sum(b["count"] for b in m["hold_histogram"]) == m["trades"]
    assert m["r_histogram"][0]["from"] is None and m["r_histogram"][-1]["to"] is None
    # monthly grid: a row per year, 12 month slots, year-to-date compounds the months in the test
    years = m["monthly_returns"]
    assert years and all(len(y["months"]) == 12 for y in years)
    growth = 1.0
    for y in years:
        for v in y["months"]:
            if v is not None:
                growth *= 1 + v / 100
    assert growth - 1 == pytest.approx(m["total_return_pct"] / 100, abs=1e-6)
    # Monte Carlo: the percentiles are ordered and the actual drawdown is a real drawdown
    mc = r["monte_carlo"]
    assert mc["runs"] >= 500 and mc["p5"] <= mc["p50"] <= mc["p95"] <= 0
    assert sum(b["count"] for b in mc["histogram"]) == mc["runs"]
    assert "why_no_trades" not in r


def test_metric_helpers_edge_cases():
    import numpy as np
    import pandas as pd
    from tradealerts.backtest import histogram, longest_underwater_days, monthly_returns, risk_ratios, streaks

    assert streaks(pd.Series([1.0, 2.0, -1.0, -1.0, -1.0, 3.0])) == (2, 3)
    assert streaks(pd.Series([], dtype=float)) == (0, 0)
    assert [b["count"] for b in histogram(np.array([-2.0, 0.2, 5.0]), [-1, 0, 1])] == [1, 0, 1, 1]
    flat = pd.Series(1000.0, index=pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC"))
    assert risk_ratios(flat, "1h") == {"sharpe": None, "sortino": None}
    assert longest_underwater_days(flat) == 0.0
    # a dip from hour 2 to hour 6 lasts 4 hours below the earlier high
    dip = flat.copy()
    dip.iloc[3:6] = 990.0
    assert longest_underwater_days(dip) == pytest.approx(4 / 24)
    rising = pd.Series(np.linspace(1000, 1100, 10), index=flat.index)
    rr = risk_ratios(rising, "1h")
    assert rr["sharpe"] is not None and rr["sortino"] is None  # no down moves: nothing to divide by
    assert monthly_returns(flat.iloc[:1]) == []
    two = pd.Series([1000.0, 1010.0, 1111.0], index=pd.to_datetime(["2024-01-31", "2024-02-15", "2024-02-29"], utc=True))
    grid = monthly_returns(two)
    assert grid[0]["year"] == 2024 and grid[0]["months"][0] == pytest.approx(0.0) and grid[0]["months"][1] == pytest.approx(11.1)
    assert grid[0]["ytd"] == pytest.approx(11.1)


def test_profit_factor_never_divides_by_zero():
    import pandas as pd
    from tests.test_simulator_backtest import make_strategy
    from tradealerts.backtest import compute_metrics
    from tradealerts.sample import synthetic_bars

    bars = synthetic_bars(days=3, freq="1h", seed=1)
    eq = pd.Series(1000.0, index=bars.index)
    zero_loss = pd.DataFrame({"pnl": [10.0, 0.0], "r_multiple": [1.0, 0.0], "bars_held": [3, 4], "exit_reason": ["target", "time"],
                              "funding": [0.0, 0.0], "spread_cost": [0.0, 0.0]})
    m = compute_metrics(make_strategy(), zero_loss, eq, bars, 0)
    assert m["profit_factor"] is None and m["avg_hold_hours"] == 0.0  # a zero-P/L trade counts as a loss worth nothing


def test_monte_carlo_shuffle():
    import numpy as np
    import pandas as pd
    from tradealerts.analysis import monte_carlo

    assert monte_carlo(pd.DataFrame({"pnl": [5.0], "equity_after": [1005.0]}), 1000.0) is None
    pnl = [50.0, -40.0, 60.0, -30.0, 20.0, -45.0, 70.0, -20.0]
    eq_after = 1000.0 + np.cumsum(pnl)
    mc = monte_carlo(pd.DataFrame({"pnl": pnl, "equity_after": eq_after}), 1000.0, np.random.default_rng(1), runs=500)
    assert mc["runs"] == 500 and mc["p5"] <= mc["p50"] <= mc["p95"] <= 0
    assert mc["p5"] <= mc["actual_dd"] <= 0  # the worst 1-in-20 order is at least as bad as this one
    assert mc["bad_dd"] == mc["p5"] and mc["median_dd"] == mc["p50"]  # luck check keeps its old field names


def test_why_no_trades_counts_each_condition(client):
    impossible = {"left": "close", "op": ">", "right": 1e9}
    entry = {"all": [{"left": {"indicator": "rsi", "period": 14}, "op": ">", "right": 20}, impossible]}
    r = client.post("/api/backtest", json={"data": edited(entry=entry)}, headers=H).json()
    assert r["trade_count"] == 0 and r["monte_carlo"] is None
    why = r["why_no_trades"]
    rows = why["conditions"]
    assert [x["path"] for x in rows] == ["entry", "entry.all[0]", "entry.all[1]"]
    assert rows[0]["kind"] == "all" and rows[0]["matched"] == 0
    assert rows[1]["kind"] == "leaf" and 0 < rows[1]["matched"] <= why["candles"]
    assert rows[2]["matched"] == 0 and rows[2]["condition"] == impossible
    assert all(x["candles"] == why["candles"] for x in rows)
    assert r["metrics"]["sharpe"] is None and "r_histogram" not in r["metrics"]


def test_why_no_trades_walks_nested_groups():
    import pandas as pd
    from tradealerts.analysis import condition_counts
    from tradealerts.sample import synthetic_bars

    bars = synthetic_bars(days=20, freq="1h", seed=2)
    spec = {"all": [{"not": {"left": "close", "op": ">", "right": 1e9}}, {"any": [{"weekdays": ["mon"]}, {"weekdays": ["tue"]}]}]}
    since = bars.index[100]
    rows = condition_counts(spec, bars, since)
    assert [x["path"] for x in rows] == ["entry", "entry.all[0]", "entry.all[0].not", "entry.all[1]", "entry.all[1].any[0]", "entry.all[1].any[1]"]
    assert [x["depth"] for x in rows] == [0, 1, 2, 1, 2, 2]
    n = int((bars.index >= since).sum())
    assert rows[0]["candles"] == n and rows[2]["matched"] == 0 and rows[1]["matched"] == n
    assert rows[3]["matched"] == rows[4]["matched"] + rows[5]["matched"]


def test_luck_check_failed_items_carry_next_steps(client):
    res = client.post("/api/luck", json={"data": CSV_STRATEGY}, headers=H).json()
    for c in res["checks"]:
        if c["passed"] is False:
            assert c["next_step"], c["key"]
            assert not any(w in c["next_step"].lower() for w in ("broken", "degraded", "bad", "should"))
        else:
            assert c["next_step"] is None
    assert any(c["passed"] is False for c in res["checks"])  # the 90-day CSV sample has too few trades
    o = res["order"]
    assert o["p5"] <= o["p50"] <= o["p95"] and "histogram" in o
