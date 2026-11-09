"""Live checker status, "Check now", the activity log (and notifications) and the raw paper-trading state."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from ... import markethours
from ...config import ConfigError
from ...notify import build_notifiers
from ...strategy import StrategyError, load_strategy, strategy_files
from ...workspace import Workspace
from .. import activity
from ..common import clean, ctx_of, err, workspace

router = APIRouter()


@router.get("/api/status")
def status(request: Request, ws: Workspace = Depends(workspace)):
    ctx = ctx_of(request)
    c = ws.config()
    files = strategy_files(c.strategies_dir)
    live = 0
    symbols = {}
    for sid, p in files.items():
        try:
            s = load_strategy(p)
        except StrategyError:
            continue
        symbols[sid] = s.market.symbol
        live += int(not s.template and s.enabled)
    running, paused, reopens = 0, 0, None
    with ws.store(create=False) as store:
        active = [sid for sid in store.active_runs() if sid in files] if store else []
    running = len(active)
    for sid in active:  # which are waiting for their market to open after the weekend
        if sid not in symbols:
            continue
        back = markethours.weekend_reopen(symbols[sid])
        if back is not None:
            paused += 1
            reopens = min(reopens, back) if reopens is not None else back
    try:
        channels = [n.name for n in build_notifiers(c.notifiers)]
        channel_error = None
    except (ConfigError, ValueError, TypeError) as exc:
        channels, channel_error = [], str(exc)
    return clean({
        "runner": ctx.runner_status(ws),
        "poll_minutes": c.poll_minutes,
        "live_strategies": live,
        "running_tests": running,
        "paused_tests": paused,
        "reopens": reopens.isoformat() if reopens is not None else None,
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
