import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
import pytest
import yaml

from tradealerts.backtest import run_backtest
from tradealerts.config import ConfigError, expand_env, load_config
from tradealerts.notify import Message, NtfyNotifier, build_notifiers
from tradealerts.runner import process_strategy, run_cycle, seconds_until_next_check
from tradealerts.sample import synthetic_bars
from tradealerts.state import StateStore
from tradealerts.strategy import load_strategy

STRATEGY = {
    "name": "Live test",
    "market": {"symbol": "TEST", "label": "Test", "timeframe": "1h"},
    "direction": "long",
    "entry": {"left": {"indicator": "rsi", "period": 14}, "op": "crosses_above", "right": 35},
    "exit": {"stop": {"atr": 2}, "target": {"r_multiple": 1.5}, "max_bars": 24},
    "risk": {"account_size": 1000, "risk_per_trade_pct": 1},
}


class Recorder:
    name = "recorder"

    def __init__(self):
        self.messages = []

    def send(self, msg):
        self.messages.append(msg)


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "live.yaml").write_text(yaml.safe_dump(STRATEGY))
    (tmp_path / "config.yaml").write_text("poll_minutes: 60\n")
    cfg = load_config(tmp_path / "config.yaml")
    bars = synthetic_bars(days=40, freq="1h", seed=5)
    return cfg, bars


def test_live_matches_backtest_bar_by_bar(setup):
    """Running live one bar at a time (saving state to SQLite each time)
    must produce exactly the same trades as a backtest over the same bars."""
    cfg, bars = setup
    strat = load_strategy(cfg.strategies_dir / "live.yaml")
    store = StateStore(cfg.state_db)
    rec = Recorder()
    step = pd.Timedelta("1h")
    for i in range(1, len(bars)):
        now = bars.index[i] + step  # bar i has just closed
        process_strategy(cfg, strat, store, [rec], now=now, loader=lambda c, s: bars)
    live = pd.DataFrame(store.trades()).drop(columns=["id", "strategy_id", "run_id"])
    bt = run_backtest(strat, bars).trades
    bt = bt[bt.exit_reason != "end_of_data"]
    assert len(live) == len(bt) > 3
    pd.testing.assert_frame_equal(live.reset_index(drop=True), bt.reset_index(drop=True), check_dtype=False)
    signals = [m for m in rec.messages if "triggered" in m.body]
    assert len(signals) >= len(bt)
    store.close()


def test_first_run_does_not_replay_history_and_catchup_suppresses_stale_signals(setup):
    cfg, bars = setup
    rec = Recorder()
    store = StateStore(cfg.state_db)
    now = bars.index[300] + pd.Timedelta("1h")
    run_cycle(cfg, store, [rec], now=now, loader=lambda c, s: bars)
    first = store.load("live")
    assert pd.Timestamp(first["cursor"]) == bars.index[300]
    assert len(store.recent_events(1000)) <= 2  # only the latest bar was checked

    # Come back after 200 bars of downtime: old signals are logged but not pushed
    rec.messages.clear()
    now2 = bars.index[500] + pd.Timedelta("1h")
    run_cycle(cfg, store, [rec], now=now2, loader=lambda c, s: bars)
    events = store.recent_events(1000)
    stale_signals = [e for e in events if e["kind"] == "signal" and e["bar_time"] != str(bars.index[500])]
    assert stale_signals and all(not e["notified"] for e in stale_signals)
    pushed_signals = [m for m in rec.messages if "triggered" in m.body]
    assert len(pushed_signals) <= 1  # at most the fresh one on the latest bar
    store.close()


def test_templates_never_alert(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "t.yaml").write_text(yaml.safe_dump({**STRATEGY, "template": True}))
    (tmp_path / "config.yaml").write_text("{}\n")
    cfg = load_config(tmp_path / "config.yaml")
    bars = synthetic_bars(days=10, seed=1)
    store = StateStore(cfg.state_db)
    out = run_cycle(cfg, store, [Recorder()], now=bars.index[-1], loader=lambda c, s: bars)
    assert out == {} and store.load("t") is None
    store.close()


def test_unfinished_bar_is_ignored(setup):
    cfg, bars = setup
    strat = load_strategy(cfg.strategies_dir / "live.yaml")
    store = StateStore(cfg.state_db)
    now = bars.index[200] + pd.Timedelta("30min")  # bar 200 still forming
    process_strategy(cfg, strat, store, [Recorder()], now=now, loader=lambda c, s: bars)
    assert pd.Timestamp(store.load("live")["cursor"]) == bars.index[199]
    store.close()


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("TOPIC", "abc")
    assert expand_env({"a": ["${TOPIC}", "${MISSING:-x}"]}) == {"a": ["abc", "x"]}
    with pytest.raises(ConfigError):
        expand_env("${DEFINITELY_NOT_SET_123}")


def test_poll_alignment():
    assert seconds_until_next_check(15, 45, now=900 * 10 + 1) == pytest.approx(900 + 44)


def test_ntfy_posts_json_with_utf8_and_token():
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["auth"] = self.headers.get("Authorization")
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    n = NtfyNotifier(topic="alerts", server=f"http://127.0.0.1:{server.server_port}", token="tk")
    n.send(Message("▲ US 500: £ test", "body →", "high", ("bell",)))
    server.server_close()
    assert received["auth"] == "Bearer tk"
    assert received["body"] == {"topic": "alerts", "title": "▲ US 500: £ test", "message": "body →",
                                "priority": 4, "tags": ["bell"]}


def test_build_notifiers_skips_disabled():
    ns = build_notifiers([{"type": "console"}, {"type": "ntfy", "enabled": False, "topic": "x"}])
    assert [n.name for n in ns] == ["console"]
    with pytest.raises(ValueError):
        build_notifiers([{"type": "pager"}])


# --------------------------------------------------------------------------- forward tests


def test_forward_test_runs_silently_for_drafts_and_only_counts_new_candles(setup):
    cfg, bars = setup
    draft = {**STRATEGY, "template": True}
    (cfg.strategies_dir / "live.yaml").write_text(yaml.safe_dump(draft))
    load_strategy(cfg.strategies_dir / "live.yaml")
    store = StateStore(cfg.state_db)
    rec = Recorder()
    loader = lambda c, s: bars

    # a draft with no forward test is ignored
    assert run_cycle(cfg, store, [rec], now=bars.index[100], loader=loader) == {}

    start = bars.index[100] + pd.Timedelta("20min")  # part-way through candle 100
    run = store.start_run("live", name="Live test", market="Test", timeframe="1h", currency="GBP",
                          start_balance=1000, started_at=start.isoformat())
    for i in range(101, 700):
        run_cycle(cfg, store, [rec], now=bars.index[i] + pd.Timedelta("1h"), loader=loader)

    eq = store.equity(run["id"])
    # candle 100 closed after the start, so it's the first one counted; nothing before it
    assert eq[0][0] == str(bars.index[100] + pd.Timedelta("1h"))
    assert len(eq) == 600
    trades = store.trades(run_id=run["id"])
    assert trades and all(pd.Timestamp(t["signal_time"]) >= bars.index[100] for t in trades)
    assert eq[-1][1] == pytest.approx(1000 + sum(t["pnl"] for t in trades) + 0, abs=50)
    assert rec.messages == []  # draft: no alerts sent
    events = store.recent_events(1000, run_id=run["id"])
    assert events and not any(e["notified"] for e in events)

    stopped = store.stop_run("live")
    assert stopped["stopped_at"] and store.active_run("live") is None and store.load("live") is None
    assert run_cycle(cfg, store, [rec], now=bars.index[701], loader=loader) == {}
    store.close()


def test_turning_alerts_on_starts_a_forward_test_automatically(setup):
    cfg, bars = setup
    store = StateStore(cfg.state_db)
    rec = Recorder()
    now = bars.index[300] + pd.Timedelta("1h")
    run_cycle(cfg, store, [rec], now=now, loader=lambda c, s: bars)
    run = store.active_run("live")
    assert run and run["start_balance"] == 1000 and run["started_at"].startswith(str(now.date()))
    store.close()


def test_restarting_a_forward_test_begins_fresh(setup):
    cfg, bars = setup
    store = StateStore(cfg.state_db)
    loader = lambda c, s: bars
    for i in range(300, 600):
        run_cycle(cfg, store, [Recorder()], now=bars.index[i] + pd.Timedelta("1h"), loader=loader)
    first = store.active_run("live")
    second = store.start_run("live", name="x", market="Test", timeframe="1h", currency="GBP",
                             start_balance=500, started_at=(bars.index[600] + pd.Timedelta("1h")).isoformat())
    assert store.run(first["id"])["stopped_at"] and second["id"] != first["id"]
    assert store.load("live") is None
    run_cycle(cfg, store, [Recorder()], now=bars.index[650], loader=loader)
    second_eq = store.equity(second["id"])
    assert second_eq and second_eq[0][0] == str(bars.index[601] + pd.Timedelta("1h"))
    assert abs(second_eq[0][1] - 500) < 25  # starts from the new balance
    second_trades = store.trades(run_id=second["id"])
    assert store.load("live")["state"]["equity"] == pytest.approx(500 + sum(t["pnl"] for t in second_trades))
    assert {t["run_id"] for t in store.trades("live")} <= {first["id"], second["id"]}
    store.close()


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, strategy_id TEXT, pnl REAL)")
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, strategy_id TEXT, bar_time TEXT, kind TEXT, "
                "message TEXT, notified INTEGER, created_at TEXT)")
    con.commit()
    con.close()
    store = StateStore(db)
    cols = [r[1] for r in store.db.execute("PRAGMA table_info(paper_trades)")]
    assert "run_id" in cols
    store.close()


def test_checks_before_a_test_starts_do_not_move_its_starting_point(setup):
    cfg, bars = setup
    store = StateStore(cfg.state_db)
    (cfg.strategies_dir / "live.yaml").write_text(yaml.safe_dump({**STRATEGY, "template": True}))
    start = bars.index[400]
    run = store.start_run("live", name="x", market="Test", timeframe="1h", currency="GBP",
                          start_balance=1000, started_at=start.isoformat())
    loader = lambda c, s: bars
    run_cycle(cfg, store, [], now=bars.index[200], loader=loader)  # clock earlier than the start
    assert store.load("live") is None
    run_cycle(cfg, store, [], now=bars.index[450], loader=loader)
    eq = store.equity(run["id"])
    assert eq[0][0] == str(bars.index[400] + pd.Timedelta("1h")) and len(eq) == 50
    store.close()
