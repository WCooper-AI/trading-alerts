import threading
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradealerts import data as data_mod
from tradealerts import marketstats, news
from tradealerts.web import DataCache, create_app


def hourly(days=200, seed=3):
    idx = pd.date_range("2025-01-06", periods=24 * days, freq="h", tz="UTC")
    idx = idx[idx.dayofweek < 5]
    rng = np.random.default_rng(seed)
    busy = np.where(idx.hour == 14, 4.0, 1.0)
    c = 5000 + np.cumsum(rng.normal(0, 2, len(idx)))
    o = np.r_[c[0], c[:-1]] + rng.normal(0, 1.5, len(idx))
    wick = rng.random(len(idx)) * 2 * busy
    return pd.DataFrame({"open": o, "close": c, "high": np.maximum(o, c) + wick, "low": np.minimum(o, c) - wick,
                         "spread": np.where(idx.hour == 22, 2.0, 0.5)}, index=idx)


def test_compute_describes_hours_ranges_gaps_and_news():
    df = hourly()
    ev = news.us_jobs_reports(df.index[0], df.index[-1])
    out = marketstats.compute(df, ev, tz="UTC", now=df.index[-1] + pd.Timedelta(hours=1))
    assert out["grid"]["days"] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert out["grid"]["busiest"] == 14
    assert out["spread"]["widest"] == 22
    assert out["ranges"]["day"]["median"] > 0 and out["ranges"]["week"]["median"] > out["ranges"]["day"]["median"]
    assert [k["key"] for k in out["gaps"]["kinds"]] == ["weekend"]
    assert {s["key"] for s in out["sessions"]} == {"asia", "london", "new_york"}
    assert sum(out["high_low"]["high"]) == pytest.approx(100, abs=0.5)
    # weekend: the last trading day stands in for "today"
    sat = marketstats.compute(df, None, tz="UTC", now=pd.Timestamp("2025-07-26 12:00", tz="UTC"))
    assert sat["ranges"]["day"]["current_is_now"] is False and sat["ranges"]["day"]["current"] is not None


def test_data_cache_shares_one_download_and_keeps_the_last_copy():
    cache, calls = DataCache(ttl=0.2), []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return "prices"

    threads = [threading.Thread(target=cache.get, args=(("k",), slow)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    time.sleep(0.25)

    def broken():
        raise data_mod.DataError("feed down")

    value, age = cache.get_or_last(("k",), broken)
    assert value == "prices" and age is not None
    with pytest.raises(data_mod.DataError):
        cache.get(("k",), broken)


def test_dukascopy_requests_get_a_timeout(monkeypatch):
    import sys
    import types

    seen = {}

    class FakeRequests:
        @staticmethod
        def get(url, **kw):
            seen.update(kw)

    fake = types.ModuleType("dukascopy_python._fake")
    fake.requests = FakeRequests
    monkeypatch.setitem(sys.modules, "dukascopy_python._fake", fake)
    monkeypatch.setitem(sys.modules, "requests", FakeRequests)
    data_mod._add_timeouts()
    fake.requests.get("x")
    assert seen["timeout"] == data_mod._TimeoutRequests.TIMEOUT


def test_market_stats_endpoint(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("notifiers:\n  - type: console\n")
    monkeypatch.setattr(data_mod.MarketProvider, "fetch_bars", lambda self, *a, **k: (hourly(), None))
    client = TestClient(create_app(tmp_path / "config.yaml"))
    r = client.get("/api/market-stats?market=us500&years=1&tz=Nowhere/Fake")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["label"] == "US 500" and d["tz"] == "Europe/London" and d["grid"]["cells"]
    assert client.get("/api/market-stats?market=nope").status_code == 404


def test_dukascopy_falls_back_to_saved_prices_when_the_feed_fails(tmp_path, monkeypatch):
    p = data_mod.DukascopyProvider(tmp_path)
    df = hourly(days=10)[["open", "high", "low", "close"]].assign(volume=1.0)
    now = df.index[-1] + pd.Timedelta(hours=1)
    monkeypatch.setattr(p, "_fetch_range", lambda *a: df)
    first = p._side("X", "INTERVAL_HOUR_1", "BID", df.index[0], now)
    assert len(first) == len(df)
    path = p._cache_path("X", "INTERVAL_HOUR_1", "BID")
    stamp = path.stat().st_mtime_ns

    def down(*a):
        raise data_mod.DataError("Couldn't reach Dukascopy")

    monkeypatch.setattr(p, "_fetch_range", down)
    again = p._side("X", "INTERVAL_HOUR_1", "BID", df.index[0], now)
    assert len(again) == len(df) and p.stale
    assert path.stat().st_mtime_ns == stamp  # nothing new, so the file isn't rewritten


def test_weekend_reopen():
    from tradealerts import markethours

    sat = pd.Timestamp("2026-11-07 12:00", tz="UTC")
    back = markethours.weekend_reopen("us500", sat)
    assert back == pd.Timestamp("2026-11-08 23:00", tz="UTC")  # 18:00 New York in winter
    assert markethours.weekend_reopen("us500", pd.Timestamp("2026-11-09 12:00", tz="UTC")) is None
    assert markethours.weekend_reopen("us500", pd.Timestamp("2026-11-06 21:59", tz="UTC")) is None
    assert markethours.weekend_reopen("us500", pd.Timestamp("2026-11-06 22:00", tz="UTC")) is not None
    assert markethours.weekend_reopen("btc", sat) is None


def test_run_cycle_records_problems(tmp_path):
    from tradealerts import runner
    from tradealerts.config import load_config
    from tradealerts.state import StateStore

    (tmp_path / "strategies").mkdir()
    (tmp_path / "config.yaml").write_text("notifiers:\n  - type: console\n")
    import pathlib
    src = pathlib.Path(runner.__file__).parent / "templates" / "strategies" / "dip_buy.yaml"
    text = src.read_text().replace("template: true", "template: false")
    (tmp_path / "strategies" / "dip.yaml").write_text(text)
    cfg = load_config(tmp_path / "config.yaml")
    store = StateStore(cfg.state_db)

    def broken(cfg, s):
        raise data_mod.DataError("feed down")

    problems = {}
    runner.run_cycle(cfg, store, [], loader=broken, problems=problems)
    store.close()
    assert list(problems.values()) == ["feed down"]
