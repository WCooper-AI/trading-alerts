"""0.3 session 8D: optimiser hold-out split and walk-forward v2 over the whole grid."""
import copy
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradealerts import analysis
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
    return [((lv, a), variant(lv, a)) for lv in (30, 35, 40) for a in (1.5, 2.0)]


def wait_done(client, job, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get(f"/api/jobs/{job}", headers=H).json()
        if st["status"] != "running":
            return st
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def ns(ts):
    return analysis._ns(pd.Timestamp(ts))


# ------------------------------------------------------------------ stretch arithmetic


def test_stretch_splits_trades_by_exit_time():
    exits = np.array([ns("2024-01-05"), ns("2024-01-20"), ns("2024-02-10")], dtype="int64")
    closed = (1000.0, exits, np.array([50.0, -20.0, 30.0]))
    first = analysis.stretch(closed, ns("2024-01-01"), ns("2024-02-01"))
    second = analysis.stretch(closed, ns("2024-02-01"), ns("2024-03-01"))
    assert first["trades"] == 2 and first["return_pct"] == pytest.approx(3.0)
    assert first["profit_factor"] == pytest.approx(2.5)
    assert second["trades"] == 1 and second["return_pct"] == pytest.approx(30 / 1030 * 100)  # starts from the balance at the cut
    assert second["profit_factor"] is None and second["sharpe"] is None
    empty = analysis.stretch(closed, ns("2025-01-01"), ns("2025-02-01"))
    assert empty == {"trades": 0, "return_pct": 0.0, "max_drawdown_pct": 0.0, "profit_factor": None, "sharpe": None}


def test_stretch_drawdown_is_relative_to_the_stretch_start():
    exits = np.array([ns("2024-01-02"), ns("2024-01-03"), ns("2024-01-04")], dtype="int64")
    closed = (1000.0, exits, np.array([100.0, -200.0, 50.0]))
    s = analysis.stretch(closed, ns("2024-01-01"), ns("2024-02-01"))
    assert s["max_drawdown_pct"] == pytest.approx(200 / 1100 * 100)


# ------------------------------------------------------------------ hold-out


def test_holdout_ranks_on_train_and_reports_winner_on_holdout():
    df = synthetic_bars(days=200, freq="1h", seed=4)
    out = analysis.optimise(None, small_grid(), df, None, None, score="return", min_trades=5, holdout_pct=30)
    ho = out["holdout"]
    assert ho["pct"] == 30 and out["walk_forward"] is None
    cut = pd.Timestamp(ho["from"])
    span = df.index[-1] - df.index[0]
    assert abs((cut - df.index[0]) / span - 0.7) < 0.01
    rows = [r for r in out["rows"] if not r["error"]]
    for r in rows:
        assert "_trades" not in r
        assert r["train"]["trades"] + r["holdout"]["trades"] == r["trades"]
        assert r["score"] == analysis.score_of({**r, **r["train"]}, "return")
        assert r["enough_trades"] == (r["train"]["trades"] >= 5)
    best = max((r for r in rows if r["score"] is not None), key=lambda r: (r["enough_trades"], r["score"]))
    assert out["best"] == best["values"] == ho["best"]["values"]
    assert ho["best"]["holdout_return_pct"] == best["holdout"]["return_pct"]
    assert ho["hindsight"]["holdout_return_pct"] == max(r["holdout"]["return_pct"] for r in rows)


def test_holdout_matches_direct_backtest_on_the_kept_back_trades():
    df = synthetic_bars(days=200, freq="1h", seed=4)
    items = small_grid()
    out = analysis.optimise(None, items, df, None, None, score="return", min_trades=5, holdout_pct=40)
    cut = pd.Timestamp(out["holdout"]["from"])
    for (values, d), row in zip(items, out["rows"]):
        res = run_backtest(strategy_from_dict(copy.deepcopy(d)), df, None)
        exits = pd.to_datetime(res.trades["exit_time"], utc=True)
        kept = res.trades.loc[(exits >= cut).to_numpy()]
        assert row["values"] == list(values) and row["holdout"]["trades"] == len(kept)
        before = float(res.trades.loc[(exits < cut).to_numpy(), "pnl"].sum())
        base = res.metrics["start_equity"] + before
        assert row["holdout"]["return_pct"] == pytest.approx(float(kept["pnl"].sum()) / base * 100)


def test_no_holdout_keeps_the_old_row_shape():
    df = synthetic_bars(days=120, freq="1h", seed=4)
    out = analysis.optimise(None, small_grid(), df, None, None, score="return", min_trades=5)
    assert out["holdout"] is None and out["walk_forward"] is None
    for r in out["rows"]:
        assert "train" not in r and "_trades" not in r and r["holdout_score"] is None
        assert r["score"] == analysis.score_of(r, "return")


# ------------------------------------------------------------------ walk-forward v2


def test_walk_forward_grid_chains_fold_results_and_only_uses_the_past():
    df = synthetic_bars(days=240, freq="1h", seed=4)
    out = analysis.optimise(None, small_grid(), df, None, None, score="return", min_trades=5, walk_forward_folds=4)
    wf = out["walk_forward"]
    assert wf["folds"] == 4 and len(wf["windows"]) == 4 and "skipped" not in wf
    chained = 1.0
    for w in wf["windows"]:
        assert pd.Timestamp(w["train_to"]) < pd.Timestamp(w["test_to"])
        chained *= 1 + w["test_return_pct"] / 100
    assert wf["walk_forward_pct"] == pytest.approx((chained - 1) * 100)
    assert wf["hindsight_values"] in [list(v) for v, _ in small_grid()]
    assert wf["hindsight_pct"] >= wf["average_pct"]
    assert pd.Timestamp(wf["from"]) == pd.Timestamp(wf["windows"][0]["train_to"])


def test_walk_forward_picks_the_best_train_score_each_fold():
    exits = {"early": ns("2024-01-10"), "late": ns("2024-06-20")}
    # A wins on every ranking so far; B's big trade only lands in the last stretch
    rows = [
        {"values": [1], "error": None, "_trades": (1000.0, np.array([exits["early"]], dtype="int64"), np.array([100.0]))},
        {"values": [2], "error": None, "_trades": (1000.0, np.array([exits["early"], exits["late"]], dtype="int64"), np.array([10.0, 200.0]))},
    ]
    wf = analysis.walk_forward_grid(rows, "return", 0, ns("2024-01-01"), ns("2024-07-01"), 3)
    assert [w["values"] for w in wf["windows"]] == [[1], [1], [1]]  # A keeps the best score on data so far
    assert wf["hindsight_values"] == [2]
    assert wf["walk_forward_pct"] == pytest.approx(0.0)
    assert wf["hindsight_pct"] == pytest.approx(200 / 1010 * 100)


def test_walk_forward_skips_short_periods_and_tiny_grids():
    row = {"values": [1], "error": None, "_trades": (1000.0, np.empty(0, dtype="int64"), np.empty(0))}
    assert "skipped" in analysis.walk_forward_grid([row], "return", 0, ns("2024-01-01"), ns("2025-01-01"), 4)
    two = [row, {**row, "values": [2]}]
    assert "stretches" in analysis.walk_forward_grid(two, "return", 0, ns("2024-01-01"), ns("2024-02-01"), 4)["skipped"]


def test_walk_forward_runs_on_the_train_part_when_holding_out():
    df = synthetic_bars(days=240, freq="1h", seed=4)
    out = analysis.optimise(None, small_grid(), df, None, None, score="return", min_trades=5, holdout_pct=25, walk_forward_folds=3)
    assert pd.Timestamp(out["walk_forward"]["to"]) <= pd.Timestamp(out["holdout"]["from"])


# ------------------------------------------------------------------ API


def opt_body(**extra):
    return {"data": CSV_STRATEGY, "score": "return", "min_trades": 5,
            "settings": [{"setting": {"kind": "path", "path": ["entry", "all", 0, "right"]}, "values": [30, 35, 40]}], **extra}


def test_api_validates_holdout_and_walk_forward(client):
    for bad in ({"holdout_pct": 60}, {"holdout_pct": -1}, {"holdout_pct": "20"}, {"holdout_pct": True}):
        r = client.post("/api/optimise", json=opt_body(**bad), headers=H)
        assert r.status_code == 422 and "between 0 and 50" in r.json()["detail"], bad
    for bad in ({"walk_forward": 2}, {"walk_forward": 7}, {"walk_forward": 4.0}, {"walk_forward": "4"}):
        r = client.post("/api/optimise", json=opt_body(**bad), headers=H)
        assert r.status_code == 422 and "stretches" in r.json()["detail"], bad


def test_api_job_result_carries_holdout_and_walk_forward(client):
    r = client.post("/api/optimise", json=opt_body(holdout_pct=30, walk_forward=3), headers=H)
    assert r.status_code == 200, r.text
    st = wait_done(client, r.json()["job"])
    assert st["status"] == "done", st
    res = st["result"]
    assert res["holdout"]["pct"] == 30 and res["walk_forward"]["folds"] == 3
    assert all("train" in row and "holdout" in row and "_trades" not in row for row in res["rows"])
    r = client.post("/api/optimise", json=opt_body(), headers=H)
    st = wait_done(client, r.json()["job"])
    assert st["result"]["holdout"] is None and st["result"]["walk_forward"] is None
