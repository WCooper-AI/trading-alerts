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
