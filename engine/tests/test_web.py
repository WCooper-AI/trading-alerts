import pytest
import yaml
from fastapi.testclient import TestClient

from tradealerts.sample import synthetic_bars
from tradealerts.web import create_app

H = {"X-Requested-With": "tradealerts"}

CSV_STRATEGY = {
    "name": "CSV test",
    "template": True,
    "market": {"symbol": "data/bars.csv", "label": "Test", "provider": "csv", "timeframe": "1h"},
    "direction": "long",
    "entry": {"all": [{"left": {"indicator": "rsi", "period": 14}, "op": "crosses_above", "right": 35}]},
    "exit": {"stop": {"atr": 2, "atr_period": 14}, "target": {"r_multiple": 2}, "max_bars": 24},
    "risk": {"account_size": 1000, "risk_per_trade_pct": 1, "currency": "GBP"},
}


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


def test_index_and_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "Trading Alerts" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    from tradealerts.web.app import SCRIPTS
    for name in SCRIPTS:  # every interface script is linked from the page and served
        assert f"/static/{name}?v=" in r.text
        assert client.get(f"/static/{name}").status_code == 200


def test_post_without_csrf_header_is_blocked(client):
    assert client.post("/api/validate", json={"data": CSV_STRATEGY}).status_code == 403


def test_meta_lists_indicators_with_types(client):
    m = client.get("/api/meta").json()
    bb = next(i for i in m["indicators"] if i["name"] == "bollinger_upper")
    assert bb["types"]["std"] == "float" and bb["types"]["period"] == "int"
    assert any(p["symbol"] == "ES=F" for p in m["presets"])


def test_create_edit_list_delete(client, ws):
    r = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H)
    assert r.status_code == 201
    sid = r.json()["id"]
    assert sid == "csv-test"
    assert (ws / "strategies" / "csv-test.yaml").exists()
    # same name again gets a new id
    assert client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"] == "csv-test-2"

    edited = {**CSV_STRATEGY, "template": False, "enabled": True}
    assert client.put(f"/api/strategies/{sid}", json={"data": edited}, headers=H).status_code == 200
    saved = yaml.safe_load((ws / "strategies" / "csv-test.yaml").read_text())
    assert saved["template"] is False and list(saved)[0] == "name"

    items = client.get("/api/strategies").json()["strategies"]
    assert {i["id"] for i in items} == {"csv-test", "csv-test-2"}
    assert all(i["error"] is None for i in items)

    status = client.get("/api/status").json()
    assert status["live_strategies"] == 1 and status["runner"]["enabled"] is False

    assert client.delete(f"/api/strategies/{sid}", headers=H).status_code == 200
    assert not (ws / "strategies" / "csv-test.yaml").exists()
    assert list((ws / "data" / "deleted-strategies").glob("csv-test-*.yaml"))


def test_invalid_strategy_is_rejected_with_reason(client, ws):
    bad = {**CSV_STRATEGY, "entry": {"left": {"indicator": "nope"}, "op": ">", "right": 1}}
    r = client.post("/api/strategies", json={"data": bad}, headers=H)
    assert r.status_code == 422 and "unknown indicator" in r.json()["detail"]
    v = client.post("/api/validate", json={"data": bad}, headers=H).json()
    assert v["ok"] is False and "unknown indicator" in v["error"]
    bad_tz = {**CSV_STRATEGY, "entry": {"time_between": ["10:00", "11:00"], "tz": "Mars/Base"}}
    assert client.post("/api/validate", json={"data": bad_tz}, headers=H).json()["ok"] is False
    assert not list((ws / "strategies").glob("*.yaml"))


def test_path_traversal_ids_are_refused(client):
    for sid in ["..%2Fconfig", "../config", "CSV_test", "a" * 80]:
        assert client.get(f"/api/strategies/{sid}").status_code == 404


def test_backtest_draft_from_csv(client):
    r = client.post("/api/backtest", json={"data": CSV_STRATEGY, "start": "2024-02-01"}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metrics"]["trades"] == body["trade_count"] > 0
    assert body["metrics"]["start"] >= "2024-02-01"
    assert len(body["equity"]) <= 601 and body["currency"] == "£"
    assert any("past prices" in w for w in body["warnings"])


def test_csv_outside_data_folder_is_refused(client):
    sneaky = {**CSV_STRATEGY, "market": {**CSV_STRATEGY["market"], "symbol": "../../etc/passwd"}}
    r = client.post("/api/backtest", json={"data": sneaky}, headers=H)
    assert r.status_code == 422 and "inside the data folder" in r.json()["detail"]


def test_examples_load_and_validate(client):
    ex = client.get("/api/examples").json()
    assert {e["file"] for e in ex} == {"dip_buy", "opening_range_breakout", "trend_breakout"}
    for e in ex:
        data = client.get(f"/api/examples/{e['file']}").json()
        assert data["template"] is True
        assert client.post("/api/validate", json={"data": data}, headers=H).json()["ok"]
    assert client.get("/api/examples/..%2F..%2Fconfig").status_code == 404


def test_settings_round_trip_keeps_other_keys(client, ws):
    s = client.get("/api/settings").json()
    assert s["notifiers"]["console"]["enabled"] is True and s["notifiers"]["ntfy"]["enabled"] is False
    s["notifiers"]["ntfy"].update(enabled=True, topic="my-topic-123", token="tk")
    s["poll_minutes"] = 5
    assert client.put("/api/settings", json=s, headers=H).status_code == 200
    raw = yaml.safe_load((ws / "config.yaml").read_text())
    assert raw["poll_minutes"] == 5
    ntfy = next(n for n in raw["notifiers"] if n["type"] == "ntfy")
    assert ntfy == {"type": "ntfy", "server": "https://ntfy.sh", "topic": "my-topic-123", "token": "tk"}
    # enabling ntfy without a topic is refused
    s["notifiers"]["ntfy"]["topic"] = ""
    r = client.put("/api/settings", json=s, headers=H)
    assert r.status_code == 422 and "topic" in r.json()["detail"]


def test_password_protection(ws, monkeypatch):
    app = create_app(ws / "config.yaml", password="correct horse")
    c = TestClient(app)
    sess = c.get("/api/session").json()
    assert {k: sess[k] for k in ("auth_required", "signed_in", "desktop", "edition")} == {
        "auth_required": True, "signed_in": False, "desktop": False, "edition": "selfhost"}
    assert c.get("/api/strategies").status_code == 401
    assert c.get("/").status_code == 200  # login page itself is public
    assert c.post("/api/login", json={"password": "wrong"}, headers=H).status_code == 401
    r = c.post("/api/login", json={"password": "correct horse"}, headers=H)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/strategies").status_code == 200
    c.post("/api/logout", headers=H)
    assert c.get("/api/strategies").status_code == 401


def test_login_lockout_after_repeated_failures(ws):
    c = TestClient(create_app(ws / "config.yaml", password="pw"))
    for _ in range(5):
        c.post("/api/login", json={"password": "x"}, headers=H)
    r = c.post("/api/login", json={"password": "pw"}, headers=H)
    assert r.status_code == 429 and "Try again" in r.json()["detail"]


def test_forged_session_cookie_rejected(ws):
    c = TestClient(create_app(ws / "config.yaml", password="pw"))
    c.cookies.set("ta_session", "9999999999.deadbeef")
    assert c.get("/api/strategies").status_code == 401


def test_run_now_without_runner_explains(client):
    r = client.post("/api/run-now", headers=H)
    assert r.status_code == 409 and "--no-runner" in r.json()["detail"]


def test_live_runner_thread_runs_and_reports(ws):
    from tradealerts.runner import LiveRunner

    (ws / "strategies" / "t.yaml").write_text(yaml.safe_dump({**CSV_STRATEGY, "template": False}))
    runner = LiveRunner(ws / "config.yaml").start()
    try:
        assert runner.run_now(timeout=60)
        st = runner.status()
        assert st["enabled"] and st["last_error"] is None and "t" in st["last_results"]
        c = TestClient(create_app(ws / "config.yaml", runner=runner))
        assert c.post("/api/run-now", headers=H).json()["finished"] is True
        paper = c.get("/api/paper").json()
        assert "t" in paper["states"]
    finally:
        runner.stop()


def test_forward_test_start_stop_and_dashboard(client, ws):
    import pandas as pd

    from tradealerts.config import load_config
    from tradealerts.runner import run_cycle
    from tradealerts.state import StateStore

    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    assert client.get("/api/dashboard").json()["running"] == []

    run = client.post(f"/api/strategies/{sid}/forward-test/start", headers=H).json()
    assert run["strategy_id"] == sid and run["start_balance"] == 1000 and run["stopped_at"] is None

    # pretend time passes: backdate the start, then let the checker work through the candles
    cfg = load_config(ws / "config.yaml")
    store = StateStore(cfg.state_db)
    bars = synthetic_bars(days=90, freq="1h", seed=4)
    start = bars.index[1000]
    store.db.execute("UPDATE runs SET started_at = ? WHERE id = ?", (start.isoformat(), run["id"]))
    store.commit()
    for i in range(1001, 1500, 7):
        run_cycle(cfg, store, [], now=bars.index[i] + pd.Timedelta("1h"))
    store.close()

    dash = client.get("/api/dashboard").json()
    assert len(dash["running"]) == 1
    r = dash["running"][0]
    assert r["candles"] > 400 and r["trades"] >= 1 and r["currency_symbol"] == "£"
    assert r["series"][0] == [start.isoformat(), 1000] and len(r["series"]) <= 161
    assert r["pnl"] == pytest.approx(r["balance"] - 1000)

    items = client.get("/api/strategies").json()["strategies"]
    assert items[0]["live"]["run"]["id"] == run["id"]
    assert client.get("/api/status").json()["running_tests"] == 1

    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["trade_list"] and len(detail["series"]) > 100
    assert client.delete(f"/api/runs/{run['id']}", headers=H).status_code == 409  # still running

    stop = client.post(f"/api/strategies/{sid}/forward-test/stop", headers=H).json()
    assert stop["run"]["stopped_at"] and stop["alerts_turned_off"] is False
    dash = client.get("/api/dashboard").json()
    assert dash["running"] == [] and dash["finished"][0]["id"] == run["id"]
    assert client.delete(f"/api/runs/{run['id']}", headers=H).status_code == 200
    assert client.get(f"/api/runs/{run['id']}").status_code == 404


def test_stopping_a_test_with_alerts_on_turns_alerts_off(client, ws):
    live = {**CSV_STRATEGY, "template": False, "enabled": True}
    sid = client.post("/api/strategies", json={"data": live}, headers=H).json()["id"]
    client.post(f"/api/strategies/{sid}/forward-test/start", headers=H)
    r = client.post(f"/api/strategies/{sid}/forward-test/stop", headers=H).json()
    assert r["alerts_turned_off"] is True
    saved = yaml.safe_load((ws / "strategies" / f"{sid}.yaml").read_text())
    assert saved["enabled"] is False


def test_backtest_years_period_with_warmup(client, ws):
    import pandas as pd
    start = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=90)).strftime("%Y-%m-%d")
    synthetic_bars(start=start, days=89, freq="1h", seed=4).to_csv(ws / "data" / "recent.csv")
    sma = {**CSV_STRATEGY, "market": {**CSV_STRATEGY["market"], "symbol": "data/recent.csv"},
           "entry": {"all": [{"left": "close", "op": ">", "right": {"indicator": "sma", "period": 200}}]}}
    # ~90 days of data but a year asked for: results cover what exists, with a note saying so
    r = client.post("/api/backtest", json={"data": sma, "years": 1}, headers=H).json()
    assert any("only goes back to" in w for w in r["warnings"])
    # a period inside the data: trading starts at the period start, earlier candles warm up the SMA
    r2 = client.post("/api/backtest", json={"data": sma, "years": 0.1}, headers=H).json()
    assert r2["metrics"]["warmup_candles"] > 200
    assert not any("only goes back to" in w for w in r2["warnings"])
    bad = client.post("/api/backtest", json={"data": sma, "years": 50}, headers=H)
    assert bad.status_code == 422


def test_backtest_custom_dates_still_work(client):
    r = client.post("/api/backtest", json={"data": CSV_STRATEGY, "start": "2024-02-01", "end": "2024-02-20"}, headers=H)
    assert r.status_code == 200, r.text
    m = r.json()["metrics"]
    assert m["start"] >= "2024-02-01" and m["end"] < "2024-02-21"


def test_appearance_saved_in_settings(client, ws):
    assert client.get("/api/session").json()["appearance"] == {"theme": "auto", "accent": "#d9912b", "density": "comfortable"}
    r = client.put("/api/appearance", json={"accent": "#BF5AF2", "glass": False}, headers=H)
    assert r.status_code == 200 and r.json()["accent"] == "#bf5af2"
    assert client.get("/api/session").json()["appearance"] == {"theme": "auto", "accent": "#bf5af2", "density": "comfortable"}
    assert "notifiers" in yaml.safe_load((ws / "config.yaml").read_text())  # other settings kept
    assert client.put("/api/appearance", json={"accent": "red; }"}, headers=H).status_code == 422


def test_loopback_host_check_and_desktop_shutdown(ws):
    hosts = {"127.0.0.1:8321", "localhost:8321"}
    c = TestClient(create_app(ws / "config.yaml", allowed_hosts=hosts), base_url="http://127.0.0.1:8321")
    assert c.get("/api/session").status_code == 200
    evil = TestClient(create_app(ws / "config.yaml", allowed_hosts=hosts), base_url="http://evil.example:8321")
    assert evil.get("/api/session").status_code == 421
    # shutdown only exists in the desktop app
    assert c.post("/api/shutdown", headers=H).status_code == 404
    app = create_app(ws / "config.yaml", desktop=True)
    stopped = []
    app.state.shutdown = lambda: stopped.append(True)
    assert TestClient(app).post("/api/shutdown", headers=H).status_code == 200 and stopped


@pytest.mark.parametrize("change,msg", [
    (lambda d: d["market"].update(timeframe=None), "choose a candle size"),
    (lambda d: d.update(direction=None), "choose long (buy) or short (sell)"),
    (lambda d: d["entry"]["all"][0].update(right=""), "fill this in"),
    (lambda d: d["entry"]["all"][0].update(op=""), "choose how to compare"),
    (lambda d: d["entry"]["all"][0]["left"].update(period=None), "fill in period for rsi"),
    (lambda d: d["exit"].update(stop={"atr": None, "atr_period": 14}), "exit.stop.atr: fill this in"),
    (lambda d: d["risk"].update(account_size=None), "risk.account_size: fill this in"),
    (lambda d: d["risk"].update(currency=None), "choose a currency"),
    (lambda d: d["entry"]["all"].append({"time_between": ["", ""], "tz": ""}), "fill in both times"),
])
def test_blank_values_are_explained(client, change, msg):
    import copy
    d = copy.deepcopy(CSV_STRATEGY)
    change(d)
    r = client.post("/api/validate", json={"data": d}, headers=H).json()
    assert r["ok"] is False and msg in r["error"]


def test_builtin_market_fills_blank_costs():
    from tradealerts.strategy import strategy_from_dict
    d = {"name": "x", "market": {"symbol": "eurusd", "provider": "auto", "timeframe": "1h"}, "direction": "long",
         "entry": {"left": "close", "op": ">", "right": 1}, "exit": {"stop": {"percent": 1}},
         "risk": {"account_size": 1000, "risk_per_trade_pct": 1, "currency": "GBP"}}
    s = strategy_from_dict(d)
    assert s.risk.point_value == 10000 and s.spread > 0 and s.funding_pct > 0
    d["market"] = {"symbol": "^N225", "provider": "yahoo", "timeframe": "1h"}
    s = strategy_from_dict(d)
    assert s.risk.point_value == 1 and s.spread == 0
