"""Tests for 0.3: strategy versions, forward-test snapshots, the reproducibility stamp and activity."""

import copy

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
