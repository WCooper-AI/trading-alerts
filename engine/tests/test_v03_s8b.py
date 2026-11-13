"""0.3 session 8B: optimiser (cross product, caps, shared indicator cache) and job progress/cancel."""
import copy
import time

import pytest
from fastapi.testclient import TestClient

from tradealerts import analysis, indicators, jobs
from tradealerts.backtest import run_backtest
from tradealerts.sample import synthetic_bars
from tradealerts.strategy import strategy_from_dict
from tradealerts.web import create_app

from .test_web import CSV_STRATEGY, H


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "data").mkdir()
    synthetic_bars(days=240, freq="1h", seed=4).to_csv(tmp_path / "data" / "bars.csv")
    (tmp_path / "config.yaml").write_text("poll_minutes: 15\nnotifiers:\n  - type: console\n")
    return tmp_path


@pytest.fixture
def client(ws):
    return TestClient(create_app(ws / "config.yaml"))


def variant(rsi_level, atr):
    d = copy.deepcopy(CSV_STRATEGY)
    d["entry"]["all"][0]["right"] = rsi_level
    d["exit"]["stop"]["atr"] = atr
    return d


def small_grid():
    levels, atrs = [30, 35, 40], [1.5, 2.0]
    return [((lv, a), variant(lv, a)) for lv in levels for a in atrs]


def wait_done(client, job, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get(f"/api/jobs/{job}", headers=H).json()
        if st["status"] != "running":
            return st
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# ------------------------------------------------------------------ grid and caps


def test_grid_is_the_cross_product_in_order():
    g = analysis.grid([[1, 2], ["a", "b", "c"], [True]], cap=100)
    assert len(g) == 6 and g[0] == (1, "a", True) and g[-1] == (2, "c", True)
    assert analysis.combo_count([[1, 2], [1, 2, 3], [1, 2, 3, 4]]) == 24


def test_grid_caps_and_limits():
    with pytest.raises(ValueError, match="200"):
        analysis.grid([list(range(15)), list(range(15))], cap=analysis.MAX_COMBOS["cloud"])   # 225 > 200
    analysis.grid([list(range(13)), list(range(13)), list(range(11))], cap=analysis.MAX_COMBOS["selfhost"])  # 1859 ok
    with pytest.raises(ValueError, match="2,000"):
        analysis.grid([list(range(13))] * 3, cap=analysis.MAX_COMBOS["selfhost"])  # 2197 > 2000
    with pytest.raises(ValueError, match="at most 3"):
        analysis.grid([[1]] * 4, cap=10)
    with pytest.raises(ValueError, match="at least one value"):
        analysis.grid([[1], []], cap=10)


def test_score_choices():
    row = {"error": None, "trades": 40, "return_pct": 12.0, "max_drawdown_pct": 4.0, "profit_factor": 1.6, "sharpe": 2.1}
    assert analysis.score_of(row, "return") == 12.0
    assert analysis.score_of(row, "return_dd") == 3.0
    assert analysis.score_of(row, "profit_factor") == 1.6
    assert analysis.score_of(row, "sharpe") == 2.1
    assert analysis.score_of({**row, "trades": 0}, "return") is None
    assert analysis.score_of({**row, "error": "bad"}, "return") is None


# ------------------------------------------------------------------ indicator cache


def test_cached_chunk_matches_uncached_backtests(monkeypatch):
    df = synthetic_bars(days=120, freq="1h", seed=4)
    items = small_grid()
    calls = []
    real = indicators.compute
    # count only indicators computed on the test data (the runtime check probes synthetic bars too)
    monkeypatch.setattr(indicators, "compute", lambda d, name, **p: (calls.append(name) if d is df else None) or real(d, name, **p))
    rows = analysis.run_chunk(df, None, None, items)
    cached_calls = len(calls)
    calls.clear()
    for (values, d), row in zip(items, rows):
        res = run_backtest(strategy_from_dict(copy.deepcopy(d)), df, None)
        assert row["values"] == list(values) and row["error"] is None
        assert row["return_pct"] == res.metrics["total_return_pct"]
        assert row["trades"] == res.metrics["trades"]
    assert cached_calls == 2 and len(calls) == 2 * len(items)  # rsi(14) and atr(14) computed once for the whole chunk


def test_optimise_sequential_ranks_and_flags_few_trades():
    df = synthetic_bars(days=120, freq="1h", seed=4)
    out = analysis.optimise(None, small_grid(), df, None, None, score="return_dd", min_trades=5, workers=1,
                            extra={"currency": "£"})
    assert len(out["rows"]) == 6 and out["tested"] == 6 and out["currency"] == "£"
    assert [r["values"] for r in out["rows"]] == [list(v) for v, _ in small_grid()]  # grid order kept
    scored = [r for r in out["rows"] if r["score"] is not None]
    best = max(scored, key=lambda r: (r["enough_trades"], r["score"]))
    assert out["best"] == best["values"]
    assert all(r["enough_trades"] == (r["trades"] >= 5) for r in scored)


def test_optimise_process_pool_matches_sequential():
    df = synthetic_bars(days=90, freq="1h", seed=4)
    items = small_grid()
    a = analysis.optimise(None, items, df, None, None, workers=1)
    b = analysis.optimise(None, items, df, None, None, workers=2)
    assert [(r["values"], r["return_pct"], r["trades"]) for r in a["rows"]] == \
           [(r["values"], r["return_pct"], r["trades"]) for r in b["rows"]]


def test_optimise_reports_errors_per_row():
    df = synthetic_bars(days=60, freq="1h", seed=4)
    bad = copy.deepcopy(CSV_STRATEGY)
    bad["entry"]["all"][0]["left"]["period"] = -3
    out = analysis.optimise(None, [((1,), bad), ((2,), "not a valid value"), ((3,), variant(35, 2))], df, None, None)
    assert out["failed"] == 2 and out["tested"] == 1
    assert out["rows"][1]["error"] == "not a valid value" and out["rows"][0]["error"]


# ------------------------------------------------------------------ progress and cancel


def slow_steps(progress, n, delay):
    for i in range(n):
        progress.check()
        time.sleep(delay)
        progress.update(i + 1, n, f"step {i + 1}")
    return {"steps": n}


def test_inline_jobs_progress_and_result():
    runner = jobs.InlineJobs()
    job = runner.start("me", slow_steps, 4, 0.02)
    assert runner.status("other", job) is None  # another owner can't see it
    end = time.time() + 5
    while runner.status("me", job)["status"] == "running" and time.time() < end:
        time.sleep(0.01)
    st = runner.status("me", job)
    assert st["status"] == "done" and st["done"] == 4 and st["total"] == 4 and st["result"] == {"steps": 4}
    assert st["note"] == "step 4"


def test_inline_jobs_cancel_and_busy():
    runner = jobs.InlineJobs()
    job = runner.start("me", slow_steps, 200, 0.02)
    with pytest.raises(jobs.Busy):
        runner.start("me", slow_steps, 1, 0)
    assert runner.start("someone-else", slow_steps, 1, 0)
    time.sleep(0.05)
    assert runner.cancel("other", job) is False
    assert runner.cancel("me", job) is True
    end = time.time() + 5
    while runner.status("me", job)["status"] == "running" and time.time() < end:
        time.sleep(0.01)
    st = runner.status("me", job)
    assert st["status"] == "cancelled" and 0 < st["done"] < 200


def test_inline_jobs_error_is_reported():
    def boom(progress):
        raise ValueError("no good")
    runner = jobs.InlineJobs()
    job = runner.start("me", boom)
    time.sleep(0.1)
    st = runner.status("me", job)
    assert st["status"] == "error" and st["error"] == "no good"


def test_pool_jobs_progress_and_cancel():
    runner = jobs.PoolJobs(workers=1, max_queue=2)
    try:
        job = runner.start("me", slow_steps, 3, 0.05)
        end = time.time() + 30
        while runner.status("me", job)["status"] == "running" and time.time() < end:
            time.sleep(0.05)
        st = runner.status("me", job)
        assert st["status"] == "done" and st["done"] == 3 and st["result"] == {"steps": 3}
        job = runner.start("me", slow_steps, 400, 0.05)
        with pytest.raises(jobs.Busy):  # one at a time per customer
            runner.start("me", slow_steps, 1, 0)
        time.sleep(0.3)
        assert runner.cancel("me", job)
        end = time.time() + 30
        while runner.status("me", job)["status"] == "running" and time.time() < end:
            time.sleep(0.05)
        st = runner.status("me", job)
        assert st["status"] == "cancelled" and st["done"] < 400
    finally:
        runner.close()


# ------------------------------------------------------------------ endpoints


def body_for(levels, atrs, **more):
    return {"data": CSV_STRATEGY, "score": "return", "min_trades": 5,
            "settings": [{"setting": {"kind": "path", "path": ["entry", "all", 0, "right"]}, "values": levels},
                         {"setting": {"kind": "path", "path": ["exit", "stop", "atr"]}, "values": atrs}], **more}


def test_optimise_endpoint_runs_and_reports(client):
    r = client.post("/api/optimise", json=body_for([30, 35, 40], [1.5, 2.0]), headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["combinations"] == 6
    st = wait_done(client, r.json()["job"])
    assert st["status"] == "done" and st["done"] == 6 and st["total"] == 6
    res = st["result"]
    assert res["combinations"] == 6 and len(res["rows"]) == 6 and res["currency"] == "£"
    assert all(len(row["values"]) == 2 for row in res["rows"])
    assert res["best"] in [row["values"] for row in res["rows"]]
    assert res["start"] and res["end"] and res["score"] == "return"


def test_optimise_endpoint_rejects_bad_requests(client):
    assert client.post("/api/optimise", json={"data": CSV_STRATEGY, "settings": []}, headers=H).status_code == 422
    four = {"data": CSV_STRATEGY, "settings": [{"setting": {"kind": "path", "path": ["exit", "max_bars"]}, "values": [1]}] * 4}
    assert client.post("/api/optimise", json=four, headers=H).status_code == 422
    b = body_for(list(range(20, 45)), [1.5, 2.0], score="banana")
    assert client.post("/api/optimise", json=b, headers=H).status_code == 422
    b = body_for([30], [1.5], min_trades=-1)
    assert client.post("/api/optimise", json=b, headers=H).status_code == 422


def test_optimise_endpoint_enforces_edition_cap(client):
    b = {"data": CSV_STRATEGY, "settings": [
        {"setting": {"kind": "path", "path": ["entry", "all", 0, "right"]}, "values": list(range(20, 45))},   # 25
        {"setting": {"kind": "path", "path": ["exit", "stop", "atr"]}, "values": [1 + i / 10 for i in range(25)]},  # 25
        {"setting": {"kind": "path", "path": ["exit", "max_bars"]}, "values": [12, 24, 36, 48]}]}             # 4 → 2500
    r = client.post("/api/optimise", json=b, headers=H)
    assert r.status_code == 422 and "2,500" in r.json()["detail"] and "2,000" in r.json()["detail"]
    b["settings"][2]["values"] = [24]  # 625: fine self-hosted, over the hosted cap
    assert analysis.grid([x["values"] for x in b["settings"]], analysis.MAX_COMBOS["selfhost"])
    with pytest.raises(ValueError, match="200"):
        analysis.grid([x["values"] for x in b["settings"]], analysis.MAX_COMBOS["cloud"])


def test_optimise_endpoint_cancel(client):
    r = client.post("/api/optimise", json=body_for(list(range(20, 45)), [1 + i / 10 for i in range(16)]), headers=H)  # 400
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert client.post("/api/optimise", json=body_for([30, 35], [2.0]), headers=H).status_code == 429  # one at a time
    assert client.post(f"/api/jobs/{job}/cancel", headers=H).json() == {"ok": True}
    st = wait_done(client, job)
    assert st["status"] == "cancelled" and st["done"] < 400
    assert client.get("/api/jobs/nope", headers=H).status_code == 404
    assert client.post("/api/jobs/nope/cancel", headers=H).status_code == 404


def test_sweep_still_needs_two_values(client):
    body = {"data": CSV_STRATEGY, "setting": {"kind": "path", "path": ["entry", "all", 0, "right"]}, "values": [30]}
    assert client.post("/api/sweep", json=body, headers=H).status_code == 422
    body["values"] = [30, 35]
    assert client.post("/api/sweep", json=body, headers=H).status_code == 200


def test_score_of_return_dd_uses_absolute_negative_drawdown():
    low_dd = {"trades": 10, "return_pct": 12.0, "max_drawdown_pct": -3.0}   # 12 / 3 = 4.0
    high_dd = {"trades": 10, "return_pct": 20.0, "max_drawdown_pct": -10.0}  # 20 / 10 = 2.0
    assert analysis.score_of(low_dd, "return_dd") == pytest.approx(4.0)
    assert analysis.score_of(high_dd, "return_dd") == pytest.approx(2.0)
    assert analysis.score_of(low_dd, "return_dd") > analysis.score_of(high_dd, "return_dd")
    # Real backtest summaries store negative max_drawdown_pct; optimise(..., score="return_dd") must rank by return/|dd|.
    df = synthetic_bars(days=240, freq="1h", seed=4)
    items = [((rsi, atr), variant(rsi, atr)) for rsi in (25, 30, 35, 40) for atr in (1.0, 1.5, 2.0, 3.0)]
    out = analysis.optimise(None, items, df, None, None, score="return_dd", min_trades=1, holdout_pct=0)
    scored = [r for r in out["rows"] if r.get("score") is not None and (r.get("max_drawdown_pct") or 0) < 0]
    assert scored, "expected rows with negative max_drawdown_pct from real backtest"
    for r in scored:
        assert r["score"] == pytest.approx(r["return_pct"] / abs(r["max_drawdown_pct"]))
    best_row = max((r for r in out["rows"] if r.get("score") is not None), key=lambda r: r["score"])
    assert out["best"] == best_row["values"]
