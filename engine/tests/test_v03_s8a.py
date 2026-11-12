"""0.3 session 8A: data quality checks, price alert repeat/cross/edit, journal tags/setup/plan/mood."""
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradealerts import dataquality, pricealerts
from tradealerts.sample import synthetic_bars
from tradealerts.state import StateStore
from tradealerts.web import create_app

H = {"X-Requested-With": "tradealerts"}


def bars(n=200, start="2024-03-04 00:00", freq="1h", seed=1):
    """n candles of noise starting on a Monday, with volume."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    close = 100 + rng.normal(0, 0.5, n).cumsum()
    return pd.DataFrame({"open": close - 0.1, "high": close + 0.3, "low": close - 0.3, "close": close,
                         "volume": rng.integers(50, 500, n)}, index=idx)


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


# ------------------------------------------------------------------ data quality


def test_clean_frame_has_no_warnings():
    df = bars()
    assert dataquality.check(df, "1h", now=df.index[-1] + pd.Timedelta(hours=1)) == []


def test_gap_is_reported_but_weekends_are_not():
    df = bars(n=120)
    gapped = df.drop(df.index[40:50])  # 10 missing candles mid-week (Tue/Wed)
    now = df.index[-1] + pd.Timedelta(hours=1)
    kinds = [w["kind"] for w in dataquality.check(gapped, "1h", now=now)]
    assert kinds == ["gap"]
    gap = dataquality.check(gapped, "1h", now=now)[0]
    assert gap["count"] == 1 and "10 candles" in gap["text"]
    # Friday 21:00 -> Sunday 22:00 is a shut market, not a gap
    wk = df[(df.index.day_of_week < 5) & ~((df.index.day_of_week == 4) & (df.index.hour > 20))]
    assert not [w for w in dataquality.check(wk, "1h", now=wk.index[-1] + pd.Timedelta(hours=1)) if w["kind"] == "gap"]


def test_stale_last_bar():
    df = bars(n=50)
    late = df.index[-1] + pd.Timedelta(hours=9)
    ws = dataquality.check(df, "1h", now=late)
    assert [w["kind"] for w in ws] == ["stale"] and "8 h ago" in ws[0]["text"]
    assert dataquality.check(df, "1h", now=df.index[-1] + pd.Timedelta(hours=2)) == []


def test_zero_volume_run_and_flat_candles_without_volume():
    df = bars(n=80)
    df.iloc[20:30, df.columns.get_loc("volume")] = 0
    now = df.index[-1] + pd.Timedelta(hours=1)
    ws = dataquality.check(df, "1h", now=now)
    assert [w["kind"] for w in ws] == ["zero_volume"] and "10 candles" in ws[0]["text"] and "no volume" in ws[0]["text"]
    # four zero candles is below the threshold
    df2 = bars(n=80)
    df2.iloc[20:24, df2.columns.get_loc("volume")] = 0
    assert dataquality.check(df2, "1h", now=now) == []
    # no volume column: flat OHLC runs are the filler signal
    flat = bars(n=80).drop(columns="volume")
    flat.iloc[10:16] = [[50, 50, 50, 50]] * 6
    assert [w["kind"] for w in dataquality.check(flat, "1h", now=now)] == ["zero_volume"]


def test_bad_input_never_raises():
    assert dataquality.check(None, "1h") == []
    assert dataquality.check(pd.DataFrame({"close": [1, 2]}), "1h") == []
    assert dataquality.check(bars(), "nonsense") == []


def test_candles_endpoint_carries_quality(client):
    strategy = {"name": "Q", "market": {"symbol": "data/bars.csv", "label": "Test", "provider": "csv", "timeframe": "1h"},
                "direction": "long", "entry": {"left": "close", "op": ">", "right": "open"},
                "exit": {"stop": {"atr": 2, "atr_period": 14}, "max_bars": 5},
                "risk": {"account_size": 1000, "risk_per_trade_pct": 1, "currency": "GBP"}}
    assert client.post("/api/strategies", json={"data": strategy}, headers=H).status_code == 201
    r = client.get("/api/candles?strategy=q&bars=100", headers=H)
    assert r.status_code == 200, r.text
    q = r.json()["quality"]
    assert isinstance(q, list) and any(w["kind"] == "stale" for w in q)  # synthetic data ends in March 2024
    assert "days ago" in next(w for w in q if w["kind"] == "stale")["text"]


# ------------------------------------------------------------------ price alerts: cross + repeat


def _alert(**kw):
    base = {"id": 1, "market": "XAUUSD", "direction": "above", "price": 101.0, "created_at": "2024-03-04T00:00:00+00:00",
            "checked_to": "2024-03-04T00:00:00+00:00", "repeat": "once", "cooldown_minutes": 60, "cross": 0,
            "fired_count": 0, "last_fired_at": None, "active": 1, "note": ""}
    return {**base, **kw}


def five_min(closes, start="2024-03-04 00:00"):
    idx = pd.date_range(start, periods=len(closes), freq="5min", tz="UTC")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c.shift(1).fillna(c), "high": c + 0.2, "low": c - 0.2, "close": c})


def test_touch_fires_when_already_past_but_cross_waits():
    already = five_min([102, 102.5, 103])  # market already above 101
    assert pricealerts.hit(_alert(), already) is not None
    assert pricealerts.hit(_alert(cross=1), already) is None
    crossing = five_min([100, 100.5, 101.3, 102])
    t, _ = pricealerts.hit(_alert(cross=1), crossing)
    assert t == crossing.index[2]
    down = five_min([102, 101.5, 100.6, 99])
    t, _ = pricealerts.hit(_alert(cross=1, direction="below", price=101.0), down)
    assert t == down.index[2]


def test_repeat_every_respects_cooldown():
    b = five_min([102] * 30)  # touching the level the whole time
    a = _alert(repeat="every", cooldown_minutes=60, last_fired_at="2024-03-04T00:00:00+00:00")
    t, _ = pricealerts.hit(a, b)
    assert t >= pd.Timestamp("2024-03-04 00:55", tz="UTC")  # first candle whose end is after the hour's cooldown
    assert pricealerts.hit(_alert(repeat="every", cooldown_minutes=600, last_fired_at="2024-03-04T00:00:00+00:00"), b) is None


class _Cfg:
    data, cache_dir = {}, None


def test_checker_keeps_every_alert_on_and_counts_fires(tmp_path):
    store = StateStore(tmp_path / "state.db")
    once = store.add_price_alert("XAUUSD", "Gold", "above", 101.0)
    every = store.add_price_alert("XAUUSD", "Gold", "above", 101.0, repeat="every", cooldown_minutes=60)
    for a in (once, every):
        store.update_price_alert(a["id"], checked_to="2024-03-04T00:00:00+00:00")
    b = five_min([100, 102, 102, 102, 102, 102, 102, 102, 102, 102])
    fired = pricealerts.check_price_alerts(_Cfg(), store, [], now=b.index[-1], loader=lambda cfg, m: b)
    assert {f["id"] for f in fired} == {once["id"], every["id"]}
    assert store.price_alert(once["id"])["active"] == 0
    e = store.price_alert(every["id"])
    assert e["active"] == 1 and e["fired_count"] == 1 and e["last_fired_at"].startswith("2024-03-04T00:05")
    # straight away again: inside the cooldown, nothing fires; after it, it fires again
    assert pricealerts.check_price_alerts(_Cfg(), store, [], now=b.index[-1], loader=lambda cfg, m: b) == []
    later = five_min([102] * 12, start="2024-03-04 00:50")
    fired = pricealerts.check_price_alerts(_Cfg(), store, [], now=later.index[-1], loader=lambda cfg, m: later)
    assert [f["id"] for f in fired] == [every["id"]] and store.price_alert(every["id"])["fired_count"] == 2
    store.close()


def test_old_database_gains_new_columns(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE price_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL, label TEXT, "
                      "direction TEXT NOT NULL, price REAL NOT NULL, note TEXT, created_at TEXT NOT NULL, checked_to TEXT, "
                      "triggered_at TEXT, triggered_price REAL, active INTEGER NOT NULL DEFAULT 1);"
                      "INSERT INTO price_alerts (market, direction, price, created_at) VALUES ('X', 'above', 1, 'now');"
                      "CREATE TABLE journal (id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL, direction TEXT NOT NULL, "
                      "entry_time TEXT NOT NULL, entry_price REAL NOT NULL, exit_time TEXT, exit_price REAL, size REAL NOT NULL, "
                      "point_value REAL NOT NULL DEFAULT 1, fees REAL NOT NULL DEFAULT 0, currency TEXT, strategy_id TEXT, "
                      "paper_trade_id INTEGER, notes TEXT, created_at TEXT NOT NULL);")
    con.commit(); con.close()
    store = StateStore(db)
    a = store.price_alert(1)
    assert a["repeat"] == "once" and a["cross"] == 0 and a["fired_count"] == 0
    assert "mood" in [r[1] for r in store.db.execute("PRAGMA table_info(journal)")]
    store.close()


def test_price_alert_endpoints_accept_and_validate_new_fields(client):
    r = client.post("/api/price-alerts", json={"market": "us500", "direction": "above", "price": 2500,
                                               "repeat": "every", "cooldown_minutes": 15, "cross": True}, headers=H)
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["repeat"] == "every" and a["cooldown_minutes"] == 15 and a["cross"] == 1
    # defaults
    d = client.post("/api/price-alerts", json={"market": "us500", "direction": "below", "price": 2400}, headers=H).json()
    assert d["repeat"] == "once" and d["cooldown_minutes"] == 60 and d["cross"] == 0
    # edit in place
    r = client.put(f"/api/price-alerts/{a['id']}", json={"direction": "below", "price": 2450, "repeat": "once", "cross": False,
                                                          "note": "edited"}, headers=H)
    assert r.status_code == 200
    e = r.json()
    assert e["direction"] == "below" and e["price"] == 2450 and e["repeat"] == "once" and e["cross"] == 0 and e["note"] == "edited"
    bad = client.post("/api/price-alerts", json={"market": "us500", "direction": "above", "price": 1, "repeat": "sometimes"}, headers=H)
    assert bad.status_code == 422 and "once or every" in bad.json()["detail"]
    bad = client.post("/api/price-alerts", json={"market": "us500", "direction": "above", "price": 1, "cooldown_minutes": -5}, headers=H)
    assert bad.status_code == 422


# ------------------------------------------------------------------ journal tags / setup / plan / mood


def _entry(**kw):
    base = {"market": "XAUUSD", "direction": "long", "entry_time": "2024-03-04T09:00:00Z", "entry_price": 100,
            "exit_time": "2024-03-04T11:00:00Z", "exit_price": 102, "size": 1, "point_value": 1, "currency": "GBP"}
    return {**base, **kw}


def test_journal_fields_round_trip_and_validate(client):
    r = client.post("/api/journal", json=_entry(tags="Breakout, news ,breakout", setup=" London open ", followed_plan=True,
                                                 mood="calm"), headers=H)
    assert r.status_code == 201, r.text
    e = client.get("/api/journal", headers=H).json()["entries"][0]
    assert e["tags"] == ["breakout", "news"] and e["setup"] == "London open" and e["followed_plan"] is True and e["mood"] == "calm"
    # list form for tags, blank plan and mood
    r = client.post("/api/journal", json=_entry(tags=["a", "b"], followed_plan=None, mood=""), headers=H)
    assert r.status_code == 201
    e = client.get("/api/journal", headers=H).json()["entries"][0]
    assert e["tags"] == ["a", "b"] and e["followed_plan"] is None and e["mood"] is None
    assert client.post("/api/journal", json=_entry(mood="ecstatic"), headers=H).status_code == 422
    assert client.post("/api/journal", json=_entry(followed_plan="maybe"), headers=H).status_code == 422
    csv = client.get("/api/journal.csv", headers=H).text
    assert "tags,setup,followed_plan,mood" in csv.splitlines()[0] and "breakout news,London open,yes,calm" in csv


def test_journal_filters_breakdowns_and_equity(client):
    rows = [_entry(tags="breakout", setup="A", followed_plan=True, mood="calm", exit_price=102),
            _entry(tags="breakout", setup="A", followed_plan=False, mood="rushed", exit_price=99,
                   entry_time="2024-03-05T09:00:00Z", exit_time="2024-03-05T11:00:00Z"),
            _entry(tags="news", setup="B", followed_plan=True, mood="calm", exit_price=103,
                   entry_time="2024-03-06T09:00:00Z", exit_time="2024-03-06T11:00:00Z"),
            _entry(tags="news", setup="B", exit_time=None, exit_price=None, entry_time="2024-03-07T09:00:00Z")]
    for r in rows:
        assert client.post("/api/journal", json=r, headers=H).status_code == 201
    j = client.get("/api/journal", headers=H).json()
    assert j["summary"]["trades"] == 4 and j["summary"]["closed"] == 3 and j["summary"]["pnl"] == 4
    assert j["equity"] == [["2024-03-04T11:00:00+00:00", 2.0], ["2024-03-05T11:00:00+00:00", 1.0], ["2024-03-06T11:00:00+00:00", 4.0]]
    b = j["breakdowns"]
    assert b["setup"] == [{"value": "A", "trades": 2, "pnl": 1.0, "wins": 1}, {"value": "B", "trades": 1, "pnl": 3.0, "wins": 1}]
    assert {x["value"]: x["trades"] for x in b["tags"]} == {"breakout": 2, "news": 1}
    assert {x["value"]: x["pnl"] for x in b["followed_plan"]} == {"yes": 5.0, "no": -1.0}
    assert {x["value"]: x["trades"] for x in b["mood"]} == {"calm": 2, "rushed": 1}
    # filters narrow the entries, summary and equity, but the breakdowns stay whole
    f = client.get("/api/journal?tag=BREAKOUT", headers=H).json()
    assert f["summary"]["trades"] == 2 and f["summary"]["pnl"] == 1 and len(f["equity"]) == 2 and len(f["breakdowns"]["setup"]) == 2
    assert client.get("/api/journal?setup=b", headers=H).json()["summary"]["trades"] == 2  # includes the open one
    assert client.get("/api/journal?followed_plan=no", headers=H).json()["summary"]["pnl"] == -1
    assert client.get("/api/journal?mood=calm&setup=A", headers=H).json()["summary"]["trades"] == 1
    assert client.get("/api/journal?mood=bored", headers=H).json()["entries"] == []


def test_journal_ui_has_new_fields_and_filters():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src" / "tradealerts" / "static" / "journal.js").read_text()
    for needle in ("followed_plan", "JOURNAL_MOODS", "equityCurve", "breakdownPanel", "journalFilter", '"Setup"', '"Tags"'):
        assert needle in js
    mk = (Path(__file__).resolve().parents[1] / "src" / "tradealerts" / "static" / "markets.js").read_text()
    for needle in ("quality-strip", "cooldown_minutes", "Only when it crosses", "Save changes", "away ("):
        assert needle in mk
