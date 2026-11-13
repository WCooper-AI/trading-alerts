"""0.3.0: data_problem events, drawdown / near-level warnings, daily summary, Telegram and Slack."""

import json
from types import SimpleNamespace

import pandas as pd

from tradealerts import notify, watch
from tradealerts.notify import Message, build_notifiers
from tradealerts.state import StateStore
from tradealerts.web.activity import CATEGORY_OF


class Rec:
    name = "rec"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def _strategy():
    return SimpleNamespace(id="s1", name="Test", market=SimpleNamespace(timeframe="1h", label="EUR/USD"))


def _kinds(store):
    return [e["kind"] for e in store.recent_events(50)]


def _frame(end):
    idx = pd.date_range(end=end, periods=20, freq="1h", tz="UTC")
    return pd.DataFrame({"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0}, index=idx)


def test_data_problem_after_n_stale_polls(tmp_path):
    store, rec = StateStore(tmp_path / "s.db"), Rec()
    now = pd.Timestamp("2026-11-11 12:00", tz="UTC")  # a Wednesday
    stale = _frame(now - pd.Timedelta(hours=10))
    fired = [watch.check_data(store, _strategy(), stale, None, now, [rec], True) for _ in range(watch.BAD_POLLS + 1)]
    assert fired == [False] * (watch.BAD_POLLS - 1) + [True, False]
    assert _kinds(store).count("data_problem") == 1 and len(rec.sent) == 1
    assert not watch.check_data(store, _strategy(), _frame(now - pd.Timedelta(hours=1)), None, now, [rec], True)
    assert store.get_value("watch:data:s1") == "0"
    assert CATEGORY_OF["data_problem"] == "problems"


def test_drawdown_fires_once_and_rearms(tmp_path):
    store, rec = StateStore(tmp_path / "s.db"), Rec()
    run, now = {"id": 1, "start_balance": 1000}, pd.Timestamp("2026-11-11", tz="UTC")
    args = (now, 10, [rec], True)
    assert not watch.check_drawdown(store, _strategy(), run, 1200, *args)
    assert watch.check_drawdown(store, _strategy(), run, 1070, *args)  # 10.8% off the 1200 peak
    assert not watch.check_drawdown(store, _strategy(), run, 1000, *args)
    assert not watch.check_drawdown(store, _strategy(), run, 1180, *args)  # recovered: re-arm
    assert watch.check_drawdown(store, _strategy(), run, 1050, *args)
    assert _kinds(store).count("drawdown") == 2
    assert not watch.check_drawdown(store, _strategy(), run, 1, now, 0, [rec], True)  # 0 = off


def test_near_level_once_per_trade(tmp_path):
    store, rec = StateStore(tmp_path / "s.db"), Rec()
    now = pd.Timestamp("2026-11-11", tz="UTC")
    pos = {"entry_time": "t1", "entry_price": 1.0, "stop": 0.9, "target": 1.2}
    assert watch.check_near(store, _strategy(), 1, pos, 1.05, now, 20, [rec], True) == []
    assert watch.check_near(store, _strategy(), 1, pos, 1.17, now, 20, [rec], True) == ["target"]
    assert watch.check_near(store, _strategy(), 1, pos, 1.18, now, 20, [rec], True) == []
    assert watch.check_near(store, _strategy(), 1, pos, 0.91, now, 20, [rec], True) == ["stop"]
    new = {**pos, "entry_time": "t2"}
    assert watch.check_near(store, _strategy(), 1, new, 0.91, now, 20, [rec], True) == ["stop"]
    assert "near_level" in _kinds(store) and len(rec.sent) == 3


def test_daily_due_and_summary(tmp_path):
    marker = tmp_path / "daily.sent"
    rep = {"daily": True, "daily_hour": 18, "tz": "Europe/London"}
    assert not watch.daily_due(rep, marker, pd.Timestamp("2026-11-11 17:00", tz="UTC"))
    assert watch.daily_due(rep, marker, pd.Timestamp("2026-11-11 18:30", tz="UTC"))
    marker.write_text("2026-11-11")
    assert not watch.daily_due(rep, marker, pd.Timestamp("2026-11-11 20:00", tz="UTC"))
    assert not watch.daily_due({"daily": False}, marker)
    msg = watch.build_daily(StateStore(tmp_path / "s.db"))
    assert msg.title.startswith("Daily summary") and "No forward tests" in msg.body


def test_telegram_and_slack(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_post", lambda url, data, headers: calls.append((url, json.loads(data))))
    tg, sl = build_notifiers([{"type": "telegram", "token": "T", "chat_id": "42"},
                              {"type": "slack", "webhook_url": "https://hooks.slack.com/x"}])
    tg.send(Message("Hi", "there", "low"))
    sl.send(Message("Hi", "there"))
    assert calls[0][0].endswith("/botT/sendMessage") and calls[0][1]["chat_id"] == "42"
    assert calls[0][1]["disable_notification"] is True
    assert calls[1][1]["text"] == "*Hi*\nthere" and calls[1][1]["blocks"]


def test_settings_round_trip():
    from tradealerts.web.common import raw_from_settings, settings_from_raw

    s = settings_from_raw({})
    s["notifiers"]["telegram"].update(enabled=True, token="T", chat_id="1")
    s["report"].update(daily=True, daily_hour=7, drawdown_pct=15, near_pct=10)
    raw = raw_from_settings({}, s)
    assert {"type": "telegram", "token": "T", "chat_id": "1"} in raw["notifiers"]
    assert raw["report"]["drawdown_pct"] == 15 and raw["report"]["daily_hour"] == 7
    back = settings_from_raw(raw)
    assert back["notifiers"]["telegram"]["token_set"] and back["report"]["near_pct"] == 10
