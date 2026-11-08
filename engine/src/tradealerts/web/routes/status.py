"""Live checker status, "Check now", the notifications bell and the raw paper-trading state."""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Depends, Request

from ... import markethours
from ...config import ConfigError
from ...notify import build_notifiers
from ...strategy import StrategyError, load_strategy, strategy_files
from ...workspace import Workspace
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


KIND = {"signal": "Rules met", "opened": "Trade opened", "closed": "Trade closed", "skipped": "Signal skipped"}


@router.get("/api/notifications")
def notifications(request: Request, ws: Workspace = Depends(workspace)):
    out = []
    with ws.store(create=False) as store:
        if store is not None:
            names = {r["strategy_id"]: r.get("name") for r in store.runs()}
            for e in store.recent_events(30):
                msg = e.get("message") or ""
                out.append({"time": e.get("created_at") or e["bar_time"], "kind": e["kind"],
                            "title": f"{KIND.get(e['kind'], e['kind'])}: {names.get(e['strategy_id']) or e['strategy_id']}",
                            "body": msg.split("\n")[1] if "\n" in msg else "",
                            "link": f"#/s/{e['strategy_id']}"})
            for a in store.price_alerts():
                if a.get("triggered_at"):
                    out.append({"time": a["triggered_at"], "kind": "price",
                                "title": f"{a.get('label') or a['market']} went {a['direction']} {a['price']:,.5g}",
                                "body": a.get("note") or "", "link": "#/markets"})
            for n in store.news_notes().values():
                if n.get("alerted_at"):
                    out.append({"time": n["alerted_at"], "kind": "news", "title": f"Reminder: {n['currency']} {n['title']}",
                                "body": n.get("note") or "", "link": "#/news"})
    st = ctx_of(request).runner_status(ws)
    if st.get("last_error"):
        out.append({"time": st.get("last_run") or pd.Timestamp.now(tz="UTC").isoformat(), "kind": "error",
                    "title": "A check failed", "body": st["last_error"], "link": "#/settings"})
    out.sort(key=lambda x: str(x["time"]), reverse=True)
    return clean({"items": out[:40]})


@router.get("/api/paper")
def paper(ws: Workspace = Depends(workspace)):
    with ws.store(create=False) as store:
        if store is None:
            return {"states": {}, "trades": [], "events": []}
        return clean({"states": store.all_states(), "trades": list(reversed(store.trades()))[:500],
                      "events": store.recent_events(100)})
