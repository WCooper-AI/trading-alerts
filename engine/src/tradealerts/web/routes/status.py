"""Live checker status, "Check now", the activity log (and notifications) and the raw paper-trading state."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from ...config import ConfigError
from ...notify import build_notifiers
from ...strategy import StrategyError, load_strategy, strategy_files
from ...workspace import Workspace
from .. import activity
from ..runs_view import STATES, run_health
from ..common import clean, ctx_of, err, workspace

router = APIRouter()


@router.get("/api/status")
def status(request: Request, ws: Workspace = Depends(workspace)):
    ctx = ctx_of(request)
    c = ws.config()
    files = strategy_files(c.strategies_dir)
    live = 0
    for sid, p in files.items():
        try:
            s = load_strategy(p)
        except StrategyError:
            continue
        live += int(not s.template and s.enabled)
    runner = ctx.runner_status(ws)
    counts = {k: 0 for k in STATES}
    reopens = None
    with ws.store(create=False) as store:
        active = [r for sid, r in store.active_runs().items() if sid in files] if store else []
    for run in active:  # the same state each test shows on the dashboard and strategies list
        health = run_health(run, files, runner)
        counts[health["state"]] += 1
        if health.get("reopens") and (reopens is None or health["reopens"] < reopens):
            reopens = health["reopens"]
    running = len(active)
    try:
        channels = [n.name for n in build_notifiers(c.notifiers)]
        channel_error = None
    except (ConfigError, ValueError, TypeError) as exc:
        channels, channel_error = [], str(exc)
    return clean({
        "runner": runner,
        "poll_minutes": c.poll_minutes,
        "live_strategies": live,
        "running_tests": running,
        "states": counts,                 # watching / waiting / attention / stopped (stopped is always 0 here)
        "reopens": reopens,
        "strategies": len(files),
        "channels": channels,
        "channel_error": channel_error,
        "read_only": ws.read_only,
    })


@router.post("/api/run-now")
def run_now(request: Request, ws: Workspace = Depends(workspace)):
    ctx = ctx_of(request)
    if not ctx.runner:
        err(409, ctx.no_runner_reason())
    finished = ctx.run_now(ws)
    return clean({"finished": finished, "runner": ctx.runner_status(ws)})


@router.get("/api/activity")
def activity_log(request: Request, category: str = "all", strategy: str = "", before: str = "", unread: int = 0,
                 limit: int = 50, ws: Workspace = Depends(workspace)):
    """The activity log, newest first. Also the notifications centre: ``unread_count`` and each
    item's ``unread`` follow the time the person last looked (POST /api/activity/seen)."""
    if category not in ("all", *activity.CATEGORIES, *activity.EXTRA):
        err(422, "Unknown activity filter")
    names = {}
    for sid, p in strategy_files(ws.config().strategies_dir).items():
        try:
            names[sid] = load_strategy(p).name
        except Exception:
            names[sid] = sid
    with ws.store(create=False) as store:
        if store is None:
            return {"items": [], "now": [], "more": False, "seen": "", "unread_count": 0}
        return clean(activity.items(store, ctx_of(request).runner_status(ws), names, category=category,
                                    strategy_id=strategy or None, before=before or None, unread_only=bool(unread),
                                    limit=max(1, min(int(limit), 200))))


@router.post("/api/activity/seen")
def activity_seen(body: dict = Body(default={}), ws: Workspace = Depends(workspace)):
    """Everything up to now (or ``until``) has been looked at."""
    until = body.get("until")
    with ws.store() as store:
        return {"seen": activity.mark_seen(store, str(until) if until else None)}


@router.get("/api/paper")
def paper(ws: Workspace = Depends(workspace)):
    with ws.store(create=False) as store:
        if store is None:
            return {"states": {}, "trades": [], "events": []}
        return clean({"states": store.all_states(), "trades": list(reversed(store.trades()))[:500],
                      "events": store.recent_events(100)})
