"""The heavy parts of the testing tools, as plain functions of data in and JSON-ready data out.

They take strategies as dicts (not ``Strategy`` objects) so the hosted edition can run
them in separate worker processes: one customer's ten-year luck check then can't slow
down everyone else's pages.
"""

from __future__ import annotations

import copy
import math

import pandas as pd

from . import analysis
from .backtest import run_backtest
from .report import warnings_for
from .strategy import check_runtime, strategy_from_dict


def _clean(v):
    from .web.common import clean
    return clean(v)


def _points(series: pd.Series, max_points: int = 600) -> list:
    from .web.common import points
    return points(series, max_points)


def strategy(data: dict, sid: str = "draft"):
    return strategy_from_dict(copy.deepcopy(data), sid=sid)


# --------------------------------------------------------------------------- chart helpers


def candles_out(df: pd.DataFrame, limit: int) -> list:
    tail = df.tail(limit)
    return [[t.isoformat(), round(float(o), 6), round(float(h), 6), round(float(lo), 6), round(float(c), 6)]
            for t, o, h, lo, c in zip(tail.index, tail["open"], tail["high"], tail["low"], tail["close"])]


def trade_marks(trades: list[dict], since: pd.Timestamp | None) -> list:
    out = []
    for t in trades:
        et = pd.Timestamp(t["entry_time"])
        et = et.tz_localize("UTC") if et.tzinfo is None else et
        if since is not None and et < since and (not t.get("exit_time") or pd.Timestamp(t["exit_time"]) < since):
            continue
        side = "buy" if t.get("direction", "long") == "long" else "sell"
        out.append({"time": t["entry_time"], "price": t["entry_price"], "kind": side,
                    "label": f"{'Buy' if side == 'buy' else 'Sell'} {t['entry_price']:,.5g}"})
        if t.get("exit_time") and t.get("exit_price") is not None:
            pnl = t.get("pnl") or 0
            out.append({"time": t["exit_time"], "price": t["exit_price"], "kind": "exit",
                        "label": f"Closed by {str(t.get('exit_reason', '')).replace('_', ' ')}: "
                                 f"{'+' if pnl >= 0 else '-'}{abs(pnl):,.2f}",
                        "gain": pnl >= 0})
    return out


def signal_marks(s, df: pd.DataFrame, limit: int) -> list:
    try:
        prepared = s.prepare(df)
    except Exception:
        return []
    n = len(df)
    return [{"time": df.index[i].isoformat(), "price": float(df["close"].iloc[i]), "kind": "signal",
             "label": "Rules met at this close"}
            for i in range(max(0, n - limit), n) if prepared.entry[i]]


# --------------------------------------------------------------------------- jobs


def backtest(data: dict, df, fine, test_start, notes: list[str]) -> dict:
    s = strategy(data)
    res = run_backtest(s, df, fine, notes=notes, start=test_start)
    eq = res.equity
    dd = (eq / eq.cummax() - 1) * 100
    trades = res.trades.tail(500).to_dict("records") if len(res.trades) else []
    tail = df.tail(300)
    all_trades = res.trades.to_dict("records") if len(res.trades) else []
    return _clean({
        "chart": {"candles": candles_out(df, 300), "marks": trade_marks(all_trades, tail.index[0]) if len(tail) else [],
                  "timeframe": s.market.timeframe},
        "metrics": res.metrics,
        "warnings": warnings_for(res),
        "equity": _points(eq),
        "drawdown": _points(dd),
        "trades": trades,
        "trade_count": len(res.trades),
        "currency": s.risk.symbol,
    })


def sweep(variants: list[tuple], df, fine, test_start, currency: str, notes: list[str]) -> dict:
    """variants: (value, strategy dict or an error message) for each value tried."""
    runs = []
    for value, d in variants:
        if isinstance(d, str):
            runs.append((value, d))
            continue
        try:
            s = strategy(d)
            check_runtime(s)
            runs.append((value, s))
        except Exception as exc:  # a value that makes the rules invalid is shown as that row's error
            runs.append((value, str(exc)))
    curves: list = []
    rows = analysis.sweep(runs, df, fine, test_start, curves=curves)
    return _clean({"rows": rows, "notes": notes, "currency": currency, "walk_forward": analysis.walk_forward(curves),
                   "start": str(df.index[0] if test_start is None else max(df.index[0], test_start)),
                   "end": str(df.index[-1])})


def luck(data: dict, df, fine, test_start, notes: list[str]) -> dict:
    out = analysis.luck_check(strategy(data), df, fine, test_start)
    out["notes"] = notes
    return _clean(out)


def portfolio(parts: list[tuple], balance: float, currency: str, notes: list[str]) -> dict:
    """parts: (id, name, strategy dict, candles, finer candles, test start)."""
    results = []
    for sid, name, data, df, fine, start in parts:
        s = strategy(data)
        s.risk.account_size = float(balance)
        s.risk.compound = False
        try:
            results.append((sid, name, run_backtest(s, df, fine, start=start)))
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from None
    out = analysis.portfolio(results, float(balance))
    return _clean({
        "metrics": out["metrics"], "per_strategy": out["per_strategy"], "correlation": out["correlation"],
        "equity": _points(out["equity"]), "drawdown": _points(out["drawdown"]),
        "open_risk": _points(out["open_risk"]), "currency": currency, "notes": notes,
    })


def backtest_range(data: dict, start_balance: float, df, fine, test_start, series: list, notes: list[str]) -> dict:
    """The range a strategy's backtest moved over stretches as long as its forward test so far."""
    s = strategy(data)
    s.risk.account_size = start_balance
    res = run_backtest(s, df, fine, start=test_start)
    times = [pd.Timestamp(t) for t, _ in series]
    hours = [(t - times[0]) / pd.Timedelta(hours=1) for t in times]
    bands = analysis.backtest_range(res.equity, hours)
    pts = []
    for (t, _), b in zip(series, bands):
        if b is None:
            break
        pts.append([t] + [round(start_balance * (1 + x / 100), 4) for x in b])
    m = res.metrics
    return _clean({
        "points": pts,
        "complete": len(pts) == len(series),
        "backtest": {"start": m["start"], "end": m["end"], "return_pct": m["total_return_pct"], "trades": m["trades"]},
        "notes": notes,
    })


def warm_up() -> bool:
    """Imported and run once in each worker so the first real job doesn't pay for importing pandas."""
    return math.isfinite(1.0)
