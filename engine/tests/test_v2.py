"""Tests for the 0.2 fixes and the workspace split (shared by both editions)."""

import io
import zipfile

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from tradealerts import rules
from tradealerts.config import load_config
from tradealerts.sample import synthetic_bars
from tradealerts.web import create_app
from tradealerts.web.routes.journal import _cell
from tradealerts.workspace import Limits, Workspace

H = {"X-Requested-With": "tradealerts"}
STRAT = {
    "name": "Dip", "template": True,
    "market": {"symbol": "data/bars.csv", "label": "Sample", "provider": "csv", "timeframe": "1h"},
    "direction": "long",
    "entry": {"all": [{"left": {"indicator": "rsi", "period": 14}, "op": "<", "right": 30}]},
    "exit": {"stop": {"atr": 2, "atr_period": 14}, "target": {"r_multiple": 2}, "max_bars": 24},
    "risk": {"account_size": 1000, "risk_per_trade_pct": 0.5, "currency": "GBP"},
}


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "data").mkdir()
    synthetic_bars(days=120, freq="1h", seed=4).to_csv(tmp_path / "data" / "bars.csv")
    (tmp_path / "config.yaml").write_text("notifiers: [{type: console}]\n")
    return tmp_path


@pytest.fixture
def client(ws):
    return TestClient(create_app(ws / "config.yaml"))


# --------------------------------------------------------------------------- rules


def test_not_is_unknown_while_an_indicator_warms_up():
    df = synthetic_bars(days=5, freq="1h", seed=1)
    cond = {"not": {"left": {"indicator": "rsi", "period": 14}, "op": ">", "right": 70}}
    out = rules.evaluate(cond, df)
    assert not out.iloc[:14].any()  # RSI isn't known yet, so "not above 70" isn't true either
    rsi = rules.Context(df).indicator("rsi", {"period": 14})
    assert (out.iloc[20:] == ~(rsi.iloc[20:] > 70)).all()


def test_none_of_group_waits_for_its_indicators():
    df = synthetic_bars(days=5, freq="1h", seed=2)
    cond = {"not": {"any": [{"left": "close", "op": ">", "right": {"indicator": "sma", "period": 50}}]}}
    assert not rules.evaluate(cond, df).iloc[:49].any()


def test_unknown_time_zone_is_caught_when_saving():
    with pytest.raises(rules.RuleError, match="time zone"):
        rules.compile_condition({"time_between": ["09:00", "10:00"], "tz": "Mars/Olympus"})
    with pytest.raises(rules.RuleError, match="time zone"):
        rules.compile_condition({"weekdays": ["mon"], "tz": "Nowhere"})
    with pytest.raises(rules.RuleError):
        rules.compile_condition({"time_between": ["24:30", "10:00"], "tz": "UTC"})


def test_flat_prices_give_neutral_rsi():
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0},
                      index=pd.date_range("2025-01-01", periods=30, freq="h", tz="UTC"))
    from tradealerts.indicators import rsi
    assert rsi(df).iloc[-1] == 50


# --------------------------------------------------------------------------- simulator


def test_time_limit_exit_is_stamped_at_the_candle_close():
    from tradealerts.backtest import run_backtest
    from tradealerts.strategy import strategy_from_dict

    d = {**STRAT, "entry": {"left": "close", "op": ">", "right": 0}, "exit": {"stop": {"points": 50}, "max_bars": 2}}
    s = strategy_from_dict(d)
    df = synthetic_bars(days=3, freq="1h", seed=3)
    res = run_backtest(s, df)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "max_bars"
    assert pd.Timestamp(t["exit_time"]) == pd.Timestamp(t["entry_time"]) + pd.Timedelta(hours=2)


# --------------------------------------------------------------------------- workspaces


def test_untrusted_workspace_ignores_env_and_folder_moves(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET", "server-secret")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "strategies_dir": "/etc", "state_db": "/tmp/evil.db",
        "notifiers": [{"type": "webhook", "url": "https://x.example/${SECRET}"}],
    }))
    trusted = load_config(tmp_path / "config.yaml")
    assert trusted.notifiers[0]["url"].endswith("server-secret")
    cfg = Workspace(tmp_path / "config.yaml", trusted=False).config()
    assert cfg.strategies_dir == tmp_path / "strategies" and cfg.state_db == tmp_path / "data" / "state.db"
    assert "${SECRET}" in cfg.notifiers[0]["url"]
    assert cfg.csv_root == tmp_path / "data" / "uploads"


def test_workspace_store_without_creating(tmp_path):
    w = Workspace(tmp_path / "config.yaml")
    with w.store(create=False) as s:
        assert s is None
    assert not (tmp_path / "data" / "state.db").exists()


# --------------------------------------------------------------------------- web


def test_tokens_are_never_sent_back(client):
    s = client.get("/api/settings").json()
    s["notifiers"]["ntfy"].update(enabled=True, topic="t", token="tk_secret")
    out = client.put("/api/settings", json=s, headers=H).json()
    assert out["notifiers"]["ntfy"]["token"] == "" and out["notifiers"]["ntfy"]["token_set"] is True
    out["notifiers"]["ntfy"]["token"] = ""  # blank keeps it
    client.put("/api/settings", json=out, headers=H)
    raw = yaml.safe_load(open(client.app.state.ctx.ws.config_path))
    assert next(n for n in raw["notifiers"] if n["type"] == "ntfy")["token"] == "tk_secret"


def test_alert_urls_must_be_web_addresses(client):
    s = client.get("/api/settings").json()
    s["notifiers"]["webhook"].update(enabled=True, url="file:///etc/passwd")
    r = client.put("/api/settings", json=s, headers=H)
    assert r.status_code == 422 and "https://" in r.json()["detail"]


def test_csv_upload_list_and_use(client, ws):
    csv = synthetic_bars(days=30, freq="1h", seed=9).to_csv().encode()
    r = client.post("/api/uploads/my prices.csv", content=csv, headers=H)
    assert r.status_code == 201 and r.json()["bar_minutes"] == 60
    files = client.get("/api/uploads").json()["files"]
    assert files[0]["symbol"] == "data/csv/my prices.csv"
    d = {**STRAT, "market": {**STRAT["market"], "symbol": files[0]["symbol"]}}
    assert client.post("/api/backtest", json={"data": d}, headers=H).status_code == 200
    assert client.post("/api/uploads/../evil.csv", content=csv, headers=H).status_code in (404, 422)
    assert client.post("/api/uploads/bad.csv", content=b"not,a,price\n1,2,3", headers=H).status_code == 422
    assert client.delete("/api/uploads/my prices.csv", headers=H).json() == {"deleted": "my prices.csv"}


def test_journal_csv_defuses_formulas():
    assert _cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _cell("-3.5") == "-3.5" and _cell(None) == "" and _cell("note") == "note"


def test_backup_and_restore_round_trip(client, ws):
    sid = client.post("/api/strategies", json={"data": STRAT}, headers=H).json()["id"]
    client.post(f"/api/strategies/{sid}/forward-test/start", headers=H)
    client.post("/api/journal", json={"market": "us500", "direction": "long", "entry_time": "2025-01-02T10:00",
                                      "entry_price": 100, "size": 1}, headers=H)
    zipped = client.get("/api/backup").content
    client.delete(f"/api/strategies/{sid}", headers=H)
    r = client.post("/api/restore", content=zipped, headers={**H, "Content-Type": "application/zip"})
    assert r.status_code == 200 and r.json()["strategies"] == 1
    assert client.get("/api/journal").json()["summary"]["trades"] == 1
    assert len(client.get("/api/dashboard").json()["running"]) == 1


def test_restore_rejects_unexpected_files(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("trading-alerts-backup.txt", "x")
        z.writestr("strategies/../../evil.yaml", "name: x")
    r = client.post("/api/restore", content=buf.getvalue(), headers=H)
    assert r.status_code == 422


def test_limits_are_enforced(ws):
    app = create_app(ws / "config.yaml")
    app.state.ctx.ws.limits = Limits(max_strategies=1, max_running=1, max_price_alerts=1, min_poll_minutes=5,
                                     console_notifier=False)
    c = TestClient(app)
    sid = c.post("/api/strategies", json={"data": STRAT}, headers=H).json()["id"]
    r = c.post("/api/strategies", json={"data": {**STRAT, "name": "Two"}}, headers=H)
    assert r.status_code == 409 and "most this plan allows" in r.json()["detail"]
    s = c.get("/api/settings").json()
    assert "console" not in s["notifiers"]
    s["poll_minutes"] = 1
    assert c.put("/api/settings", json=s, headers=H).status_code == 422
    assert c.post(f"/api/strategies/{sid}/forward-test/start", headers=H).status_code == 200
    assert c.post("/api/price-alerts", json={"market": "us500", "direction": "above", "price": 1}, headers=H).status_code == 201
    assert c.post("/api/price-alerts", json={"market": "us500", "direction": "above", "price": 2}, headers=H).status_code == 409


def test_cli_new_handles_awkward_names(tmp_path, monkeypatch):
    from tradealerts.cli import main
    monkeypatch.chdir(tmp_path)
    assert main(["new", 'Buy: the "dip" #1']) == 0
    p = next((tmp_path / "strategies").glob("*.yaml"))
    assert yaml.safe_load(p.read_text())["name"] == 'Buy: the "dip" #1'


# --------------------------------------------------------------------------- live checker


def test_runner_checks_each_workspace_once_per_period(tmp_path):
    from tradealerts.runner import LiveRunner, _slot

    seen = []
    a, b = (Workspace(tmp_path / n / "config.yaml", owner=n) for n in ("a", "b"))
    r = LiveRunner(lambda: [a, b])
    r.check_workspace = lambda w: seen.append(w.owner)
    r.cycle()
    r.cycle()
    assert seen == ["a", "b"]  # second pass: same 15-minute period, nothing due
    r.wake("b")
    r.cycle()
    assert seen == ["a", "b", "b"]
    assert _slot(15, 15 * 60 + 44) == 0 and _slot(15, 15 * 60 + 46) == 1


def test_runner_reports_per_workspace(tmp_path):
    from tradealerts.runner import LiveRunner

    a = Workspace(tmp_path / "a" / "config.yaml", owner="a")
    r = LiveRunner(lambda: [a])

    def boom(w):
        raise RuntimeError("feed down")
    r.check_workspace = boom
    r.cycle()
    assert r.status("a")["last_error"] == "feed down"
    assert r.status("other")["last_error"] is None
