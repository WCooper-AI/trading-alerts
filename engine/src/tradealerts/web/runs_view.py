"""How a forward test is summarised for the dashboard, strategy pages and charts."""

from __future__ import annotations

import math

import pandas as pd

from ..state import StateStore
from ..strategy import load_strategy
from .common import CURRENCY_SYMBOLS, utc


def run_series(run: dict, eq: list, current: float, max_points: int) -> list:
    """Balance points from the start to now (or to when the test stopped).

    Always at least two points, so the chart draws even before the first candle closes.
    """
    start_t = utc(run["started_at"])
    pts = [[start_t, run["start_balance"]]]
    for t, v in eq:
        ts = utc(t)
        if ts > pts[-1][0]:
            pts.append([ts, v])
    end = utc(run["stopped_at"]) if run.get("stopped_at") else pd.Timestamp.now(tz="UTC")
    if end > pts[-1][0]:
        pts.append([end, current])
    elif len(pts) == 1:
        pts.append([start_t + pd.Timedelta(seconds=1), current])
    if len(pts) > max_points:
        step = math.ceil(len(pts) / max_points)
        pts = pts[:-1:step] + [pts[-1]]
    return [[t.isoformat(), round(v, 4)] for t, v in pts]


def run_summary(store: StateStore, run: dict, with_series: bool = True, max_points: int = 160) -> dict:
    trades = store.trades(run_id=run["id"])
    eq = store.equity(run["id"])
    start = run["start_balance"]
    active = run["stopped_at"] is None
    rec = store.load(run["strategy_id"]) if active else None
    st = rec["state"] if rec else {}
    current = eq[-1][1] if eq else start
    realised = start + sum(t["pnl"] or 0 for t in trades)
    pnl = current - start
    wins = sum(1 for t in trades if (t["pnl"] or 0) > 0)
    peak, worst = start, 0.0
    for v in [start] + [v for _, v in eq]:
        peak = max(peak, v)
        worst = min(worst, (v / peak - 1) * 100 if peak else 0)
    pos = st.get("position")
    v = store.version_by_hash(run["strategy_id"], run["source_hash"]) if run.get("source_hash") else None
    out = {
        **{k: val for k, val in run.items() if k != "strategy_yaml"},
        "version": v["version_no"] if v else None,  # which saved version of the strategy it runs on
        "active": active,
        "currency_symbol": CURRENCY_SYMBOLS.get(run.get("currency") or "GBP", ""),
        "balance": current,
        "realised": realised,
        "pnl": pnl,
        "pnl_pct": pnl / start * 100 if start else 0,
        "trades": len(trades),
        "wins": wins,
        "max_drawdown_pct": worst,
        "candles": len(eq),
        "last_candle": eq[-1][0] if eq else None,
        "position": ({**pos, "unrealised": current - realised} if pos else None),
        "pending": bool(st.get("pending_entry")),
    }
    if with_series:
        out["series"] = run_series(run, eq, current, max_points)
    return out


STATES = ("watching", "waiting", "attention", "stopped")


def run_health(r: dict, files: dict, st: dict) -> dict:
    """The one state of a forward test, used everywhere it's shown:

    watching   checked each time a candle closes
    waiting    its market is shut (weekend or the daily break); ``reopens`` says until when
    attention  something stops it being checked; ``text`` says what
    stopped    the test has finished
    """
    from .. import markethours

    base = {"last_check": st.get("last_run"), "next_check": st.get("next_run")}
    if r.get("stopped_at"):
        return {**base, "state": "stopped", "text": "Stopped"}
    sid = r["strategy_id"]
    if not r.get("strategy_exists", sid in files):
        return {**base, "state": "attention", "text": "Its strategy file is missing, so it can't be checked"}
    problem = (st.get("problems") or {}).get(sid)
    if problem:
        return {**base, "state": "attention", "text": problem}
    if not st.get("enabled"):
        return {**base, "state": "attention", "text": st.get("off_reason") or "Live checks are off, so nothing is being checked"}
    if st.get("last_error"):
        return {**base, "state": "attention", "text": "The last check failed: " + st["last_error"]}
    try:
        symbol = load_strategy(files[sid]).market.symbol
    except Exception as exc:
        return {**base, "state": "attention", "text": f"The strategy can't be read: {exc}"}
    shut = markethours.closed_until(symbol)
    if shut is not None:
        reopens, why = shut
        return {**base, "state": "waiting", "reopens": reopens.isoformat(), "why": why,
                "text": "Market closed for the weekend" if why == "weekend" else "Market's daily break"}
    return {**base, "state": "watching", "text": "Watching"}
