"""Extra checks the runner makes on each poll: bad price data, account drawdown,
price nearing an open trade's stop or target, and the daily summary.

Thresholds live in the ``report`` settings: ``drawdown_pct`` (0 = off), ``near_pct``
(0 = off; how close, as a % of the entry-to-level distance), ``daily`` and ``daily_hour``.
Each alert fires once and re-arms when the condition clears, so nothing repeats every poll.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import dataquality
from .notify import Message, broadcast
from .state import StateStore

# this many polls in a row with a stale feed before it counts as a data problem
BAD_POLLS = 3


def _notify(store: StateStore, sid: str, run_id, t: str, kind: str, msg: Message, notifiers, send: bool) -> None:
    errors = broadcast(notifiers, msg) if send else []
    store.add_event(sid, t, kind, f"{msg.title}\n{msg.body}", send and not errors, run_id)
    if errors:
        store.add_event(sid, t, "alert_failed", f"Alert not sent: {msg.title}\n" + "; ".join(errors), False, run_id)


def _get(store: StateStore, key: str, default):
    raw = store.get_value(key)
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


def _set(store: StateStore, key: str, value) -> None:
    store.set_value(key, json.dumps(value))


def check_data(store: StateStore, strategy, raw: pd.DataFrame, run_id, now: pd.Timestamp,
               notifiers, send: bool) -> bool:
    """Count polls in a row where the feed looks stopped; log one ``data_problem`` at BAD_POLLS.

    Returns True when the event was emitted on this call."""
    key = f"watch:data:{strategy.id}"
    warnings = [w for w in dataquality.check(raw, strategy.market.timeframe, now,
                                             symbol=getattr(strategy.market, "symbol", "")) if w["kind"] == "stale"]
    count = _get(store, key, 0)
    if not warnings:
        if count:
            _set(store, key, 0)
        return False
    count += 1
    _set(store, key, count)
    if count != BAD_POLLS:
        return False
    msg = Message(f"Price data problem: {strategy.name}",
                  f"{strategy.market.label}: {warnings[0]['text']}\nChecked {count} times in a row.",
                  "high", ("warning",))
    _notify(store, strategy.id, run_id, str(now), "data_problem", msg, notifiers, send)
    return True


def check_drawdown(store: StateStore, strategy, run: dict, equity: float, now: pd.Timestamp,
                   pct: float, notifiers, send: bool) -> bool:
    """Alert once when equity falls ``pct`` % below its peak in this forward test."""
    if not pct or pct <= 0:
        return False
    key = f"watch:dd:{run['id']}"
    st = _get(store, key, {"peak": float(run.get("start_balance") or equity), "fired": False})
    st["peak"] = max(st["peak"], equity)
    dd = (st["peak"] - equity) / st["peak"] * 100 if st["peak"] > 0 else 0.0
    fired = False
    if dd >= pct and not st["fired"]:
        st["fired"] = fired = True
        msg = Message(f"Drawdown alert: {strategy.name}",
                      f"Account is down {dd:.1f}% from its peak ({st['peak']:,.2f} to {equity:,.2f}). "
                      f"Your limit is {pct:g}%.", "high", ("chart_with_downwards_trend",))
        _notify(store, strategy.id, run["id"], str(now), "drawdown", msg, notifiers, send)
    elif dd < pct / 2:
        st["fired"] = False  # recovered well clear of the limit: re-arm
    _set(store, key, st)
    return fired


def check_near(store: StateStore, strategy, run_id, position: dict | None, price: float, now: pd.Timestamp,
               pct: float, notifiers, send: bool) -> list[str]:
    """Alert once per trade when price gets within ``pct`` % of the way left to its stop or target."""
    key = f"watch:near:{strategy.id}"
    if not position or not pct or pct <= 0:
        if store.get_value(key):
            _set(store, key, {})
        return []
    trade_key = str(position.get("entry_time"))
    st = _get(store, key, {})
    if st.get("trade") != trade_key:
        st = {"trade": trade_key, "done": []}
    entry = float(position["entry_price"])
    hit = []
    for level in ("stop", "target"):
        lv = position.get(level)
        if lv is None or level in st["done"]:
            continue
        full = abs(entry - float(lv))
        if full and abs(price - float(lv)) <= full * pct / 100:
            st["done"].append(level)
            hit.append(level)
            word = "stop loss" if level == "stop" else "target"
            msg = Message(f"Near {word}: {strategy.name}",
                          f"{strategy.market.label} at {price:g}, close to the {word} at {float(lv):g} "
                          f"(entry {entry:g}).", "high" if level == "stop" else "default", ("eyes",))
            _notify(store, strategy.id, run_id, str(now), "near_level", msg, notifiers, send)
    _set(store, key, st)
    return hit


# --------------------------------------------------------------------------- daily summary

def daily_due(rep: dict, marker: Path, now: pd.Timestamp | None = None) -> bool:
    """True once a day at or after ``daily_hour`` (local to ``tz``), if not already sent today."""
    if not rep.get("daily"):
        return False
    tz = rep.get("tz") or None
    if now is None:
        now = pd.Timestamp.now(tz=tz) if tz else pd.Timestamp.now()
    elif tz:
        now = pd.Timestamp(now)
        now = now.tz_localize("UTC").tz_convert(tz) if now.tzinfo is None else now.tz_convert(tz)
    if now.hour < int(rep.get("daily_hour", 18)):
        return False
    return not (marker.exists() and marker.read_text().strip() == now.strftime("%Y-%m-%d"))


def build_daily(store: StateStore, now: pd.Timestamp | None = None) -> Message:
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    since = now - pd.Timedelta(days=1)
    states = store.all_states()
    lines = []
    for sid, run in sorted(store.active_runs().items()):
        st = (states.get(sid) or {}).get("state") or {}
        pos = st.get("position")
        closed = [t for t in store.trades(run_id=run["id"]) if t.get("exit_time")
                  and pd.Timestamp(t["exit_time"]).tz_localize(None) >= since.tz_localize(None)]
        day_pnl = sum(float(t.get("pnl") or 0) for t in closed)
        eq = float(st.get("equity", run.get("start_balance") or 0))
        total = eq - float(run.get("start_balance") or 0)
        cur = run.get("currency") or ""
        line = f"{run.get('name') or sid}: balance {eq:,.2f} {cur} ({total:+,.2f} overall, {day_pnl:+,.2f} last 24h"
        line += f", {len(closed)} closed)"
        if pos:
            mark = (st.get("mark") or {}).get("close")
            line += (f"\n  Open since {pos.get('entry_time')} at {float(pos['entry_price']):g}"
                     f", stop {pos.get('stop')}, target {pos.get('target')}"
                     + (f", now {float(mark):g}" if mark is not None else ""))
        lines.append(line)
    body = "\n".join(lines) if lines else "No forward tests are running."
    return Message(f"Daily summary {now:%d %b}", body, "low", ("memo",))


def send_daily(store: StateStore, notifiers, now: pd.Timestamp | None = None) -> list[str]:
    return broadcast(notifiers, build_daily(store, now))
