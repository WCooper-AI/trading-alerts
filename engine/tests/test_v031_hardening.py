"""0.3.1 hardening: shareable backups, sign-in slow-down kept in state.db, train / test split, missing candles."""

import io
import sqlite3
import time
import zipfile

import numpy as np
import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from tradealerts import compute, dataquality
from tradealerts.backtest import run_backtest
from tradealerts.sample import synthetic_bars
from tradealerts.web import create_app
from tradealerts.web.auth import LoginThrottle
from tradealerts.web.routes.backup import redact_config

from .conftest import make_strategy

H = {"X-Requested-With": "tradealerts"}

CONFIG = """\
poll_minutes: 15
data:
  source: oanda
  oanda_token: abc-oanda-secret
  oanda_environment: practice
notifiers:
  - type: telegram
    token: "123:telegram-secret"
    chat_id: "42"
  - type: slack
    webhook_url: https://hooks.slack.com/services/slack-secret
  - type: discord
    webhook_url: https://discord.com/api/webhooks/discord-secret
  - type: ntfy
    topic: ntfy-secret-topic
    password: ntfy-secret-pass
  - type: webhook
    url: https://example.com/hook-secret
    headers: {Authorization: Bearer header-secret}
  - type: email
    host: smtp.example.com
    to: me@example.com
    password: ${SMTP_PASSWORD}
  - type: console
"""
SECRETS = ("abc-oanda-secret", "telegram-secret", "slack-secret", "discord-secret", "ntfy-secret-topic",
           "ntfy-secret-pass", "hook-secret", "header-secret")
STRAT = {
    "name": "Dip", "template": True,
    "market": {"symbol": "data/bars.csv", "label": "Sample", "provider": "csv", "timeframe": "1h"},
    "direction": "long",
    "entry": {"all": [{"left": {"indicator": "rsi", "period": 14}, "op": "<", "right": 35}]},
    "exit": {"stop": {"atr": 2, "atr_period": 14}, "target": {"r_multiple": 2}, "max_bars": 24},
    "risk": {"account_size": 1000, "risk_per_trade_pct": 0.5, "currency": "GBP"},
}


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "from-the-environment")
    (tmp_path / "data").mkdir()
    synthetic_bars(days=120, freq="1h", seed=4).to_csv(tmp_path / "data" / "bars.csv")
    (tmp_path / "config.yaml").write_text(CONFIG)
    return tmp_path


@pytest.fixture
def client(ws):
    return TestClient(create_app(ws / "config.yaml"))


def _unzip(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        return {n: z.read(n) for n in z.namelist()}


# --------------------------------------------------------------------------- 1. backups


def test_full_backup_keeps_credentials(client):
    files = _unzip(client.get("/api/backup").content)
    text = files["config.yaml"].decode()
    assert all(s in text for s in SECRETS)


def test_shareable_backup_strips_credentials(client):
    r = client.get("/api/backup?redact=true")
    assert r.status_code == 200 and "shareable-backup" in r.headers["content-disposition"]
    files = _unzip(r.content)
    text = files["config.yaml"].decode()
    assert text.startswith("# Shareable copy")
    assert not [s for s in SECRETS if s in text]
    assert "removed" in files["trading-alerts-backup.txt"].decode()
    cfg = yaml.safe_load(text)
    assert cfg["data"]["oanda_token"] == "" and cfg["data"]["source"] == "oanda"
    by_type = {n["type"]: n for n in cfg["notifiers"]}
    assert by_type["telegram"]["chat_id"] == "42" and by_type["telegram"]["enabled"] is False
    assert by_type["webhook"]["url"] == "" and by_type["webhook"]["headers"] == {}
    assert by_type["ntfy"]["topic"] == ""
    assert by_type["email"]["password"] == "${SMTP_PASSWORD}"  # names a variable, not the secret
    assert "enabled" not in by_type["email"] and "enabled" not in by_type["console"]
    assert cfg["poll_minutes"] == 15


def test_shareable_backup_restores(client):
    zipped = client.get("/api/backup?redact=true").content
    r = client.post("/api/restore", content=zipped, headers={**H, "Content-Type": "application/zip"})
    assert r.status_code == 200


def test_shareable_backup_leaves_out_sign_in_log(ws):
    client = TestClient(create_app(ws / "config.yaml", password="right"))
    client.post("/api/login", json={"password": "wrong"}, headers=H)
    client.post("/api/login", json={"password": "right"}, headers=H)
    full = _unzip(client.get("/api/backup").content)["data/state.db"]
    shared = _unzip(client.get("/api/backup?redact=true").content)["data/state.db"]
    path = ws / "shared.db"
    path.write_bytes(shared)
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    con.close()
    assert "login_attempts" not in tables and len(full) > 0


def test_redact_config_handles_odd_values():
    assert redact_config(None) is None
    assert redact_config({"notifiers": [{"type": "slack", "webhook_url": ""}]}) == \
        {"notifiers": [{"type": "slack", "webhook_url": ""}]}  # nothing to hide, left switched on
    assert redact_config({"x": {"api_key": "k", "keep": 1}}) == {"x": {"api_key": "", "keep": 1}}


# --------------------------------------------------------------------------- 2. sign-in slow-down


def test_throttle_survives_a_restart(tmp_path):
    db = tmp_path / "state.db"
    first = LoginThrottle(free_tries=3, db_path=db)
    for _ in range(3):
        first.failed("1.2.3.4")
    assert first.locked_for("1.2.3.4") > 0
    second = LoginThrottle(free_tries=3, db_path=db)  # as after a restart, or a second process
    assert 0 < second.locked_for("1.2.3.4") <= 30
    second.failed("1.2.3.4")
    assert first.locked_for("1.2.3.4") > 30  # doubles, counted across both
    assert second.locked_for("5.6.7.8") == 0
    first.ok("1.2.3.4")
    assert second.locked_for("1.2.3.4") == 0


def test_throttle_forgets_old_addresses(tmp_path):
    db = tmp_path / "state.db"
    t = LoginThrottle(free_tries=5, db_path=db)
    t.failed("old")
    t.failed("new")
    con = sqlite3.connect(db)
    con.execute("UPDATE login_attempts SET last_attempt = ?, locked_until = 0 WHERE ip = 'old'",
                (time.time() - 2 * LoginThrottle.FORGET_AFTER,))
    con.commit()
    con.close()
    assert t.cleanup() == 1
    con = sqlite3.connect(db)
    assert [r[0] for r in con.execute("SELECT ip FROM login_attempts")] == ["new"]
    con.close()


def test_throttle_without_db_stays_in_memory(tmp_path):
    t = LoginThrottle(free_tries=1)
    t.failed("x")
    assert t.locked_for("x") > 0 and t.db_path is None


def test_login_lockout_shared_by_two_servers(ws):
    a = TestClient(create_app(ws / "config.yaml", password="right"))
    for _ in range(5):
        assert a.post("/api/login", json={"password": "wrong"}, headers=H).status_code == 401
    b = TestClient(create_app(ws / "config.yaml", password="right"))  # restarted
    r = b.post("/api/login", json={"password": "right"}, headers=H)
    assert r.status_code == 429


# --------------------------------------------------------------------------- 3. train / test split


@pytest.fixture
def bars():
    return synthetic_bars(days=120, freq="1h", seed=3)


def test_split_backtest_gives_in_and_out_of_sample(bars):
    s = make_strategy()
    whole = run_backtest(s, bars)
    res = run_backtest(s, bars, split_pct=70)
    assert whole.oos_metrics is None and whole.split_date is None
    cut = int(round(len(bars) * 0.7))
    assert res.split_date == bars.index[cut]
    assert res.equity.index[-1] == bars.index[cut - 1] and res.oos_equity.index[0] == bars.index[cut]
    for m in (res.metrics, res.oos_metrics):
        assert {"total_return_pct", "sharpe", "max_drawdown_pct", "win_rate_pct", "trades"} <= set(m)
        assert m["split_pct"] == 70
    assert res.oos_metrics["start_equity"] == s.risk.account_size  # a fresh balance
    assert res.metrics["trades"] > 0 and res.oos_metrics["trades"] > 0
    assert (pd.to_datetime(res.trades["exit_time"]) < res.split_date).all()
    assert (pd.to_datetime(res.oos_trades["entry_time"]) >= res.split_date).all()


def test_split_has_no_boundary_artifact(bars):
    """Indicators are worked out on the whole history, so the out-of-sample part's signals
    are exactly the ones a backtest started at the split date (with warm-up) would see."""
    s = make_strategy()
    res = run_backtest(s, bars, split_pct=70)
    alone = run_backtest(s, bars, start=res.split_date)
    assert len(res.oos_trades) == len(alone.trades)
    assert np.allclose(res.oos_equity.to_numpy(), alone.equity.to_numpy())


@pytest.mark.parametrize("pct", [10, 49.9, 95, True])
def test_split_out_of_range_is_rejected(bars, pct):
    with pytest.raises(ValueError):
        run_backtest(make_strategy(), bars, split_pct=pct)


def test_split_needs_enough_candles(bars):
    with pytest.raises(ValueError, match="too short"):
        run_backtest(make_strategy(), bars.iloc[:3], split_pct=50)


def test_split_through_compute_and_api(client, bars):
    out = compute.backtest(make_strategy_dict(), bars, None, None, [], 80)
    assert {"oos_metrics", "oos_equity", "split_date", "oos_trade_count"} <= set(out)
    r = client.post("/api/backtest", json={"data": STRAT, "split_pct": 70}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"]["split_pct"] == 70 and body["oos_metrics"]["split_pct"] == 70
    assert body["split_date"] > body["metrics"]["start"]
    plain = client.post("/api/backtest", json={"data": STRAT}, headers=H).json()
    assert "oos_metrics" not in plain
    for bad in (30, "70", True):
        assert client.post("/api/backtest", json={"data": STRAT, "split_pct": bad}, headers=H).status_code == 422


def make_strategy_dict():
    return {
        "name": "Test", "market": {"symbol": "TEST", "label": "Test", "timeframe": "1h"}, "direction": "long",
        "entry": {"left": "close", "op": ">", "right": {"indicator": "sma", "period": 3}},
        "exit": {"stop": {"points": 2}, "target": {"points": 4}},
        "risk": {"account_size": 1000, "risk_per_trade_pct": 1, "point_value": 1, "size_step": 0.01, "min_size": 0.01},
    }


# --------------------------------------------------------------------------- 4. missing candles


def hourly(n=200, start="2024-03-04 00:00"):  # a Monday
    idx = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    close = 100 + np.arange(n) * 0.01
    return pd.DataFrame({"open": close, "high": close + 0.2, "low": close - 0.2, "close": close,
                         "volume": 100}, index=idx)


def _kinds(df):
    return {w["kind"]: w for w in dataquality.check(df, "1h", now=df.index[-1] + pd.Timedelta(hours=1))}


def test_missing_bars_totals_several_session_gaps():
    df = hourly()
    gapped = df.drop(df.index[30:36]).drop(df.index[60:70])  # 6 and 10 weekday candles
    w = _kinds(gapped)
    assert "gap" in w and w["missing_bars"]["count"] == 16 and w["missing_bars"]["gaps"] == 2
    assert w["missing_bars"]["largest_at"] == df.index[59].isoformat()
    assert "16 weekday candles" in w["missing_bars"]["text"]


def test_missing_bars_ignores_weekends_and_small_gaps():
    df = hourly(n=24 * 14)
    weekdays = df[df.index.day_of_week < 5]
    assert "missing_bars" not in _kinds(weekdays) and "gap" not in _kinds(weekdays)
    small = df.drop(df.index[[20, 21, 50, 51]])  # two candles missing twice: normal noise
    assert "missing_bars" not in _kinds(small)
    # a hole running over a weekend only counts its weekday candles
    long = weekdays.drop(weekdays.index[100:130]).drop(weekdays.index[10:15])
    w = _kinds(long)["missing_bars"]
    assert w["count"] == 35 and w["gaps"] == 2


def test_disordered_timestamps_are_reported():
    df = hourly(n=50)
    shuffled = pd.concat([df.iloc[:20], df.iloc[25:30], df.iloc[20:25], df.iloc[30:], df.iloc[[40]]])
    w = _kinds(shuffled)
    assert w["disordered"]["count"] == 2  # one step back, one repeated time
    assert "gap" not in w and "missing_bars" not in w  # checked after sorting
    assert "disordered" not in _kinds(df)
