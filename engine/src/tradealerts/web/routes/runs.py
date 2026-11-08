"""Forward tests: start, stop, the dashboard, one test's page, and comparing it with its backtest."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from ... import compute
from ...jobs import Busy
from ...strategy import read_raw, strategy_files
from ...workspace import Workspace
from ..common import clean, ctx_of, err, utc, workspace
from ..prices import load_period
from ..runs_view import run_health, run_series, run_summary
from ..strategy_io import alerts_on, check_running_limit, load, strategy_path, write

router = APIRouter()


@router.post("/api/strategies/{sid}/forward-test/start")
def start_forward_test(sid: str, request: Request, ws: Workspace = Depends(workspace)):
    strategy_path(ws, sid)
    s = load(ws, sid)
    check_running_limit(ws, sid)
    with ws.store() as store:
        run = store.start_run(sid, name=s.name, market=s.market.label, timeframe=s.market.timeframe,
                              currency=s.risk.currency, start_balance=s.risk.account_size)
    ctx_of(request).wake(ws)
    return clean(run)


@router.post("/api/strategies/{sid}/forward-test/stop")
def stop_forward_test(sid: str, ws: Workspace = Depends(workspace)):
    p = strategy_path(ws, sid)
    data = read_raw(p)
    alerts_were_on = alerts_on(data)
    if alerts_were_on:
        # Alerts would start a new test straight away, so switch them off too.
        data["enabled"] = False
        write(p, data)
    with ws.store() as store:
        run = store.stop_run(sid)
    return clean({"run": run, "alerts_turned_off": alerts_were_on})


@router.get("/api/dashboard")
def dashboard(request: Request, ws: Workspace = Depends(workspace)):
    c = ws.config()
    with ws.store(create=False) as store:
        if store is None:
            return {"running": [], "finished": [], "events": []}
        files = strategy_files(c.strategies_dir)
        running, finished = [], []
        for run in store.runs():
            summary = run_summary(store, run)
            summary["strategy_exists"] = run["strategy_id"] in files
            (running if summary["active"] else finished).append(summary)
        running.sort(key=lambda r: r["id"])
        st = ctx_of(request).runner_status(ws)
        for r in running:
            r["health"] = run_health(r, files, st)
        events = [e for e in store.recent_events(40) if e.get("run_id")][:15]
    return clean({"running": running, "finished": finished[:30], "events": events})


@router.get("/api/runs/{run_id}")
def run_detail(run_id: int, request: Request, ws: Workspace = Depends(workspace)):
    c = ws.config()
    with ws.store(create=False) as store:
        run = store.run(run_id) if store else None
        if not run:
            err(404, "No forward test with that id")
        files = strategy_files(c.strategies_dir)
        summary = run_summary(store, run, max_points=1200)
        summary["strategy_exists"] = run["strategy_id"] in files
        summary["trade_list"] = list(reversed(store.trades(run_id=run_id)))
        summary["events"] = store.recent_events(100, run_id=run_id)
        if summary["active"]:
            summary["health"] = run_health(summary, files, ctx_of(request).runner_status(ws))
    return clean(summary)


@router.delete("/api/runs/{run_id}")
def delete_run(run_id: int, ws: Workspace = Depends(workspace)):
    with ws.store() as store:
        run = store.run(run_id)
        if not run:
            err(404, "No forward test with that id")
        if run["stopped_at"] is None:
            err(409, "Stop this forward test before removing it")
        store.delete_run(run_id)
    return {"deleted": run_id}


@router.post("/api/runs/{run_id}/backtest-range")
def run_backtest_range(run_id: int, request: Request, body: dict = Body(default={}), ws: Workspace = Depends(workspace)):
    """The range a strategy's backtest moved over stretches as long as its forward test so far."""
    ctx = ctx_of(request)
    with ws.store(create=False) as store:
        run = store.run(run_id) if store else None
        if not run:
            err(404, "No forward test with that id")
        eq = store.equity(run_id)
    if run["strategy_id"] not in strategy_files(ws.config().strategies_dir):
        err(404, "The strategy for this forward test has been deleted")
    s = load(ws, run["strategy_id"])
    data = read_raw(strategy_path(ws, run["strategy_id"]))
    df, fine, test_start, notes = load_period(ctx, ws, s, body, end_at=utc(run["started_at"]))
    current = eq[-1][1] if eq else run["start_balance"]
    series = run_series(run, eq, current, 160)
    try:
        return ctx.jobs.run(ws.owner, compute.backtest_range, data, float(run["start_balance"]), df, fine,
                            test_start, series, notes)
    except Busy as exc:
        err(429, str(exc))
    except ValueError as exc:
        err(422, str(exc))
