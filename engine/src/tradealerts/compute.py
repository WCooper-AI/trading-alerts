"""The heavy parts of the testing tools, as plain functions of data in and JSON-ready data out.

They take strategies as dicts (not ``Strategy`` objects) so the hosted edition can run
them in separate worker processes: one customer's ten-year luck check then can't slow
down everyone else's pages.
"""

from __future__ import annotations

import copy
import math

import pandas as pd

from . import rules, analysis
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
    """Entry and exit markers; ``trade`` is the index into ``trades`` so a click can find the trade."""
    out = []
    for k, t in enumerate(trades):
        et = pd.Timestamp(t["entry_time"])
        et = et.tz_localize("UTC") if et.tzinfo is None else et
        if since is not None and et < since and (not t.get("exit_time") or pd.Timestamp(t["exit_time"]) < since):
            continue
        side = "buy" if t.get("direction", "long") == "long" else "sell"
        out.append({"time": t["entry_time"], "price": t["entry_price"], "kind": side, "trade": k,
                    "label": f"{'Buy' if side == 'buy' else 'Sell'} {t['entry_price']:,.5g}"})
        if t.get("exit_time") and t.get("exit_price") is not None:
            pnl = t.get("pnl") or 0
            out.append({"time": t["exit_time"], "price": t["exit_price"], "kind": "exit", "trade": k,
                        "label": f"Closed by {str(t.get('exit_reason', '')).replace('_', ' ')}: "
                                 f"{'+' if pnl >= 0 else '-'}{abs(pnl):,.2f}",
                        "gain": pnl >= 0})
    return out


PRICE_PANE = {"sma", "ema", "bollinger_upper", "bollinger_lower", "keltner_upper", "keltner_lower", "highest", "lowest",
              "opening_range_high", "opening_range_low"}
ZERO_LINE = {"macd", "macd_signal", "macd_hist", "pct_change", "zscore", "pct_from_high", "pct_from_low"}


def _rule_levels(spec) -> dict[str, list[float]]:
    """Numbers an operand is compared with in the rules, e.g. {"RSI(14)": [30.0]}."""
    out: dict[str, list[float]] = {}

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            if {"left", "op", "right"} <= set(node):
                for a, b in ((node["left"], node["right"]), (node["right"], node["left"])):
                    label = rules.operand_label(a)
                    if label and isinstance(b, (int, float)) and not isinstance(b, bool):
                        out.setdefault(label, []).append(float(b))
            for k in ("all", "any", "not"):
                if k in node:
                    walk(node[k])

    walk(spec)
    return out


def chart_indicators(s, df: pd.DataFrame, limit: int) -> list:
    """Each indicator the entry rule reads, over the last ``limit`` candles: drawn on the price
    pane (averages, bands) or in an oscillator pane below (RSI, MACD...), with reference levels."""
    ctx = rules.Context(df)
    levels = _rule_levels(s.entry_spec)
    out = []
    for label, spec in rules.reason_operands(s.entry_spec):
        if not isinstance(spec, dict) or "indicator" not in spec:
            continue
        try:
            series = rules.compile_operand(spec)(ctx)
        except Exception:
            continue
        name = str(spec["indicator"])
        refs = set(levels.get(label, []))
        if name in ZERO_LINE:
            refs.add(0.0)
        if name == "rsi":
            refs |= {30.0, 70.0}
        vals = series.tail(limit)
        out.append({"label": label, "name": name, "pane": "price" if name in PRICE_PANE else "oscillator",
                    "values": [None if pd.isna(v) else round(float(v), 6) for v in vals], "levels": sorted(refs)})
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


def backtest(data: dict, df, fine, test_start, notes: list[str], split_pct: float | None = None) -> dict:
    s = strategy(data)
    res = run_backtest(s, df, fine, notes=notes, start=test_start, split_pct=split_pct)
    eq = res.equity
    dd = (eq / eq.cummax() - 1) * 100
    trades = res.trades.tail(2000).to_dict("records") if len(res.trades) else []
    # candles from a little before the first shown trade to the end, so any trade can be looked at
    start = len(df) - 300
    if trades:
        first_entry = pd.Timestamp(trades[0]["entry_time"])
        start = min(start, int(df.index.searchsorted(first_entry)) - 50)
    start = max(0, start, len(df) - 5000)
    shown = df.iloc[start:]
    period = df.loc[eq.index[0]:] if len(eq) else df
    # bought at the first candle's open, like the buy-and-hold figure in the metrics
    bench = period["close"] / float(period["open"].iloc[0]) * s.risk.account_size if len(period) else pd.Series(dtype=float)
    out = {
        "chart": {"candles": candles_out(shown, len(shown)), "marks": trade_marks(trades, shown.index[0]) if len(shown) else [],
                  "indicators": chart_indicators(s, df, len(shown)), "timeframe": s.market.timeframe},
        "metrics": res.metrics,
        "warnings": warnings_for(res),
        "equity": _points(eq),
        "benchmark": _points(bench),
        "drawdown": _points(dd),
        "trades": trades,
        "trade_count": len(res.trades),
        "currency": s.risk.symbol,
        "monte_carlo": analysis.monte_carlo(res.trades, s.risk.account_size, compound=s.risk.compound) if len(res.trades) >= 2 else None,
    }
    if not len(res.trades):
        out["why_no_trades"] = analysis.why_no_trades(s, df, test_start)
    if res.oos_metrics is not None:
        out["oos_metrics"] = res.oos_metrics
        out["oos_equity"] = _points(res.oos_equity)
        out["oos_trade_count"] = len(res.oos_trades)
        out["split_date"] = res.split_date.isoformat()
    return _clean(out)


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


def portfolio(parts: list[tuple], balance: float, currency: str, notes: list[str], options: dict | None = None) -> dict:
    """parts: (id, name, strategy dict, candles, finer candles, test start).

    options: ``weighting`` (equal / inverse_vol / custom), ``weights`` ({id: share %} for custom),
    ``shared`` (replay through one compounding account), ``max_open_trades``, ``max_open_risk_pct``.
    """
    options = options or {}
    results = []
    for sid, name, data, df, fine, start in parts:
        s = strategy(data)
        s.risk.account_size = float(balance)
        s.risk.compound = False
        try:
            results.append((sid, name, run_backtest(s, df, fine, start=start)))
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from None
    weighting = options.get("weighting") or "equal"
    weights = analysis.portfolio_weights(weighting, results, options.get("weights"))
    if options.get("shared"):
        out = analysis.shared_account(results, float(balance), weights, options.get("max_open_trades"), options.get("max_open_risk_pct"))
    else:
        out = analysis.portfolio(results, float(balance), weights)
    return _clean({
        "metrics": out["metrics"], "per_strategy": out["per_strategy"], "correlation": out["correlation"],
        "equity": _points(out["equity"]), "drawdown": _points(out["drawdown"]),
        "open_risk": _points(out["open_risk"]), "currency": currency, "notes": notes,
        "exposure": out["exposure"], "skipped": out.get("skipped"),
        "weighting": weighting, "weights": {sid: w * 100 for sid, w in weights.items()},
        "shared": bool(options.get("shared")),
        "pnl_curves": {sid: _points(out["pnl_curves"][sid]) for sid in out["pnl_curves"].columns},
    })


def backtest_range(data: dict, start_balance: float, df, fine, test_start, series: list, notes: list[str],
                   live_trades: list[dict] | None = None) -> dict:
    """The range a strategy's backtest moved over stretches as long as its forward test so far,
    and (given the forward test's closed trades) where they sit against the backtest's trade windows."""
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
        "windows": analysis.trade_window_ranges(res.trades, live_trades or [], start_balance),
        "notes": notes,
    })


def warm_up() -> bool:
    """Imported and run once in each worker so the first real job doesn't pay for importing pandas."""
    return math.isfinite(1.0)
