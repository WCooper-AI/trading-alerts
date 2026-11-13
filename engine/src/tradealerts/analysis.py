"""Checks that go beyond a single backtest.

* ``sweep``: run the same rules with one setting changed to each of several values.
* ``optimise``: every combination of up to three settings, ranked by a chosen score.
* ``luck_check``: is a backtest's result likely to be luck? Ported from the CLI research
  scripts: resample the trades, check both halves of the period, and run the same rules
  on random price paths with the same volatility.
* ``backtest_range``: how the balance moved over stretches of a given length in a
  backtest, used to compare a forward test with its own history.

None of these predict future results; they describe the past more honestly.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import timeframes
from .backtest import BacktestResult, run_backtest
from .strategy import Strategy

N_RESAMPLES = 5000
N_SHUFFLES = 2000
MIN_TRADES = 30


# --------------------------------------------------------------------------- shared


def halves(res: BacktestResult) -> tuple[float, float]:
    """Profit in the first and second half of the test period (by when trades closed)."""
    t = res.trades
    if not len(t):
        return 0.0, 0.0
    start = pd.Timestamp(res.metrics["start"])
    end = pd.Timestamp(res.metrics["end"])
    mid = start + (end - start) / 2
    exits = pd.to_datetime(t["exit_time"], utc=True)
    if mid.tzinfo is None:
        mid = mid.tz_localize("UTC")
    first = float(t.loc[(exits < mid).to_numpy(), "pnl"].sum())
    second = float(t.loc[(exits >= mid).to_numpy(), "pnl"].sum())
    return first, second


def summary(res: BacktestResult) -> dict:
    m = res.metrics
    first, second = halves(res)
    return {
        "return_pct": m["total_return_pct"],
        "max_drawdown_pct": m["max_drawdown_pct"],
        "trades": m["trades"],
        "win_rate_pct": m.get("win_rate_pct"),
        "profit_factor": m.get("profit_factor"),
        "avg_r": m.get("avg_r"),
        "first_half": first,
        "second_half": second,
    }


# --------------------------------------------------------------------------- settings sweep


def sweep(runs: list[tuple[object, Strategy | str]], df: pd.DataFrame, fine: pd.DataFrame | None,
          start: pd.Timestamp | None, curves: list | None = None) -> list[dict]:
    """Backtest each (value, strategy). A string instead of a strategy is an error for that value.
    Pass a list as ``curves`` to collect (value, equity) for the walk-forward check."""
    rows = []
    for value, s in runs:
        if isinstance(s, str):
            rows.append({"value": value, "error": s})
            continue
        try:
            res = run_backtest(s, df, fine, start=start)
        except ValueError as exc:
            rows.append({"value": value, "error": str(exc)})
            continue
        rows.append({"value": value, "error": None, **summary(res)})
        if curves is not None:
            curves.append((value, res.equity))
    ok = [r for r in rows if not r["error"]]
    # Average of each value and its neighbours: a lone spike is more likely luck than a plateau.
    for i, r in enumerate(ok):
        near = ok[max(0, i - 1): i + 2]
        r["neighbourhood_pct"] = float(np.mean([x["return_pct"] for x in near]))
    return rows


def _between(eq: pd.Series, a: pd.Timestamp, b: pd.Timestamp) -> float:
    """% change of a balance curve from time a to time b."""
    va, vb = eq.asof(a), eq.asof(b)
    if pd.isna(va):
        va = eq.iloc[0]
    if pd.isna(vb) or not va:
        return 0.0
    return (float(vb) / float(va) - 1) * 100


def walk_forward(curves: list[tuple[object, pd.Series]], folds: int = 4) -> dict | None:
    """Pick the best value on the data so far, then see how it does on the next stretch.

    The test period is cut into ``folds + 1`` equal parts. For each part after the first,
    the value with the best result on everything before it is chosen, and its result on
    that part is recorded. Chaining those results is an honest estimate of what tuning
    would have given you, because each choice only used the past.
    """
    if len(curves) < 3:
        return None
    start = max(eq.index[0] for _, eq in curves)
    end = min(eq.index[-1] for _, eq in curves)
    if end - start < pd.Timedelta(days=60):
        return None
    edges = pd.date_range(start, end, periods=folds + 2)
    windows, chained = [], 1.0
    for k in range(1, folds + 1):
        scored = [(_between(eq, edges[0], edges[k]), value, eq) for value, eq in curves]
        train, value, eq = max(scored, key=lambda x: x[0])
        test = _between(eq, edges[k], edges[k + 1])
        chained *= 1 + test / 100
        windows.append({"train_from": edges[0].isoformat(), "train_to": edges[k].isoformat(),
                        "test_to": edges[k + 1].isoformat(), "value": value,
                        "train_return_pct": train, "test_return_pct": test})
    later = [(_between(eq, edges[1], edges[-1]), value) for value, eq in curves]
    best_later, best_value = max(later, key=lambda x: x[0])
    return {
        "windows": windows,
        "walk_forward_pct": (chained - 1) * 100,
        "hindsight_pct": best_later,
        "hindsight_value": best_value,
        "average_pct": float(np.mean([x[0] for x in later])),
        "from": edges[1].isoformat(),
        "to": edges[-1].isoformat(),
    }


# --------------------------------------------------------------------------- portfolio


WEIGHTING_MODES = ("equal", "inverse_vol", "custom")


def _daily_pnl(res: BacktestResult) -> pd.Series:
    pnl = res.equity - res.strategy.risk.account_size
    pnl = pnl[~pnl.index.duplicated(keep="last")]
    return pnl.resample("1D").last().ffill().diff().dropna()


def portfolio_weights(mode: str | None, parts: list[tuple[str, str, BacktestResult]],
                      custom: dict[str, float] | None = None) -> dict[str, float]:
    """Each strategy's share of the shared balance; the shares add up to 1.

    ``equal`` gives every strategy the same share. ``inverse_vol`` gives more to strategies
    whose daily results moved less, so each one's typical daily swing is about the same size.
    ``custom`` takes the user's shares in % (any positive numbers; they are scaled to add up to 100).
    """
    ids = [sid for sid, _, _ in parts]
    if not ids:
        return {}
    mode = mode or "equal"
    if mode not in WEIGHTING_MODES:
        raise ValueError("Choose a weighting: equal, inverse_vol or custom")
    if mode == "equal":
        return {sid: 1.0 / len(ids) for sid in ids}
    if mode == "inverse_vol":
        vols = {}
        for sid, _, res in parts:
            d = _daily_pnl(res)
            v = float(d.std()) if len(d) > 1 else float("nan")
            vols[sid] = v if v and not np.isnan(v) and v > 0 else None
        inv = {k: 1.0 / v for k, v in vols.items() if v is not None}
        if not inv:
            return {sid: 1.0 / len(ids) for sid in ids}
        fill = sum(inv.values()) / len(inv)  # a strategy with no measurable swing gets an average share
        raw = {sid: inv.get(sid, fill) for sid in ids}
        total = sum(raw.values())
        return {sid: v / total for sid, v in raw.items()}
    custom = custom or {}
    raw = {}
    for sid in ids:
        v = custom.get(sid)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError("Fill in a share (%) of 0 or more for every chosen strategy")
        raw[sid] = float(v)
    total = sum(raw.values())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("The shares must add up to more than 0")
    return {sid: v / total for sid, v in raw.items()}


def portfolio(parts: list[tuple[str, str, BacktestResult]], balance: float,
              weights: dict[str, float] | None = None) -> dict:
    """Combine several backtests that each risked a % of the same starting balance.

    Each strategy was run without compounding at ``balance``, so adding their profit
    and loss together gives the shared account. Also works out how much was at risk
    at once (every open trade's distance to its stop) and how alike the strategies' days were.

    ``weights`` (from :func:`portfolio_weights`) scale each strategy against an equal share:
    a strategy on 1/n of the balance runs exactly as backtested, one on twice that trades
    twice the size. Trades sizes are linear in the balance, so the curves are scaled.
    """
    n = len(parts)
    scale = {sid: (weights[sid] * n if weights else 1.0) for sid, _, _ in parts}
    pnl_curves, per, risk_events, open_events = {}, [], [], []
    for sid, name, res in parts:
        pnl = (res.equity - res.strategy.risk.account_size) * scale[sid]
        pnl_curves[sid] = pnl[~pnl.index.duplicated(keep="last")]
        own = balance + pnl_curves[sid]
        t = res.trades
        m = res.metrics
        per.append({"id": sid, "name": name, "market": m["market"], "trades": m["trades"],
                    "pnl": float(pnl.iloc[-1]), "return_pct": float(pnl.iloc[-1]) / balance * 100,
                    "max_drawdown_pct": float(((own / own.cummax().clip(lower=balance) - 1) * 100).min()), "win_rate_pct": m.get("win_rate_pct"),
                    "direction": res.strategy.direction, "timeframe": res.strategy.market.timeframe,
                    "weight_pct": (weights[sid] if weights else 1.0 / n) * 100})
        if len(t):
            pv = res.strategy.risk.point_value
            for row in t.itertuples():
                amount = abs(row.entry_price - row.stop) * row.size * pv * scale[sid]
                risk_events.append((pd.Timestamp(row.entry_time), amount, sid))
                risk_events.append((pd.Timestamp(row.exit_time), -amount, sid))
                open_events.append((pd.Timestamp(row.entry_time), 1, sid))
                open_events.append((pd.Timestamp(row.exit_time), -1, sid))
    grid_start = min(c.index[0] for c in pnl_curves.values())
    grid_end = max(c.index[-1] for c in pnl_curves.values())
    grid = pd.date_range(grid_start.floor("h"), grid_end.ceil("h"), freq="1h")
    aligned = pd.DataFrame({k: v.reindex(v.index.union(grid)).ffill().reindex(grid) for k, v in pnl_curves.items()}).fillna(0.0)
    equity = balance + aligned.sum(axis=1)
    dd = (equity / equity.cummax().clip(lower=balance) - 1) * 100

    # open risk over time, as % of the starting balance
    if risk_events:
        ev = pd.DataFrame(risk_events, columns=["time", "amount", "sid"])
        ev["time"] = pd.to_datetime(ev["time"], utc=True)
        ev = ev.sort_values(["time", "amount"])  # closes before opens at the same moment
        running = ev.set_index("time")["amount"].cumsum()
        running = running[~running.index.duplicated(keep="last")]
        open_risk = running.reindex(running.index.union(grid)).ffill().reindex(grid).fillna(0.0).clip(lower=0) / balance * 100
    else:
        open_risk = pd.Series(0.0, index=grid)

    daily = aligned.resample("1D").last().diff().dropna(how="all")
    corr = None
    if aligned.shape[1] > 1 and len(daily) > 10:
        c = daily.loc[:, daily.std() > 0].corr()
        corr = {"ids": list(c.columns), "values": [[None if pd.isna(x) else float(x) for x in row] for row in c.to_numpy()]}
    exposure = _exposure(per, aligned, open_events, grid, daily, corr)
    return {
        "per_strategy": per,
        "equity": equity,
        "drawdown": dd,
        "open_risk": open_risk,
        "pnl_curves": aligned,
        "exposure": exposure,
        "metrics": {
            "start": grid[0].isoformat(), "end": grid[-1].isoformat(),
            "start_equity": balance, "end_equity": float(equity.iloc[-1]),
            "total_return_pct": (float(equity.iloc[-1]) / balance - 1) * 100,
            "max_drawdown_pct": float(dd.min()),
            "trades": int(sum(p["trades"] for p in per)),
            "max_open_risk_pct": float(open_risk.max()),
            "avg_open_risk_pct": float(open_risk[open_risk > 0].mean()) if (open_risk > 0).any() else 0.0,
            "time_with_risk_pct": float((open_risk > 0).mean() * 100),
            "max_open_trades": exposure["max_open_trades"],
            "time_in_market_pct": exposure["time_in_market_pct"],
            "avg_correlation": exposure["avg_correlation"],
            "diversification_ratio": exposure["diversification_ratio"],
        },
        "correlation": corr,
    }


def _exposure(per: list[dict], aligned: pd.DataFrame, open_events: list[tuple], grid: pd.DatetimeIndex,
              daily: pd.DataFrame, corr: dict | None) -> dict:
    """How the strategies share the account: who made the profit, how often trades overlap,
    how much sits in each market, and how much the mix actually spreads the risk."""
    # contribution: each strategy's share of the total profit (or loss), and time in a trade
    total = sum(p["pnl"] for p in per)
    gross = sum(abs(p["pnl"]) for p in per)
    in_trade = {p["id"]: pd.Series(0.0, index=grid) for p in per}
    if open_events:
        ev = pd.DataFrame(open_events, columns=["time", "n", "sid"])
        ev["time"] = pd.to_datetime(ev["time"], utc=True)
        ev = ev.sort_values(["time", "n"])
        for sid, g in ev.groupby("sid"):
            run = g.set_index("time")["n"].cumsum()
            run = run[~run.index.duplicated(keep="last")]
            in_trade[sid] = run.reindex(run.index.union(grid)).ffill().reindex(grid).fillna(0.0).clip(lower=0)
    open_trades = sum(in_trade.values()) if in_trade else pd.Series(0.0, index=grid)
    for p in per:
        p["contribution_pct"] = (p["pnl"] / total * 100) if total else (abs(p["pnl"]) / gross * 100 if gross else 0.0)
        p["time_in_market_pct"] = float((in_trade[p["id"]] > 0).mean() * 100)
        p["worst_day"] = float(daily[p["id"]].min()) if p["id"] in daily and len(daily) else 0.0
        p["best_day"] = float(daily[p["id"]].max()) if p["id"] in daily and len(daily) else 0.0

    # by market: profit, trades and time in a trade, per market label
    by_market: dict[str, dict] = {}
    for p in per:
        bm = by_market.setdefault(p["market"], {"market": p["market"], "strategies": 0, "trades": 0, "pnl": 0.0, "ids": []})
        bm["strategies"] += 1
        bm["trades"] += p["trades"]
        bm["pnl"] += p["pnl"]
        bm["ids"].append(p["id"])
    for bm in by_market.values():
        busy = sum(in_trade[i] for i in bm["ids"]) if bm["ids"] else pd.Series(0.0, index=grid)
        bm["time_in_market_pct"] = float((busy > 0).mean() * 100)
        bm["share_pct"] = (bm["pnl"] / total * 100) if total else 0.0
        del bm["ids"]

    # overlap: how many strategies were in a trade at once, as a share of time
    counts = open_trades.round().astype(int)
    overlap = counts.value_counts(normalize=True).sort_index()
    overlap_pct = {int(k): float(v * 100) for k, v in overlap.items()}

    # how alike: average of the pairwise correlations, and the diversification ratio
    # (sum of each strategy's daily volatility / the combined daily volatility; 1 = no benefit)
    avg_corr = None
    if corr and len(corr["ids"]) > 1:
        vals = [v for i, row in enumerate(corr["values"]) for j, v in enumerate(row) if j > i and v is not None]
        avg_corr = float(np.mean(vals)) if vals else None
    div_ratio = None
    if aligned.shape[1] > 1 and len(daily) > 10:
        stds = daily.std()
        combined = daily.sum(axis=1).std()
        if combined and combined > 0:
            div_ratio = float(stds.sum() / combined)
    return {
        "by_market": sorted(by_market.values(), key=lambda b: -abs(b["pnl"])),
        "overlap_pct": overlap_pct,
        "max_open_trades": int(counts.max()) if len(counts) else 0,
        "time_in_market_pct": float((open_trades > 0).mean() * 100),
        "avg_correlation": avg_corr,
        "diversification_ratio": div_ratio,
    }


# --------------------------------------------------------------------------- portfolio: one shared account


def shared_account(parts: list[tuple[str, str, BacktestResult]], balance: float,
                   weights: dict[str, float] | None = None, max_open_trades: int | None = None,
                   max_open_risk_pct: float | None = None) -> dict:
    """Replay every strategy's trades through one account, in the order they were entered.

    Unlike :func:`portfolio`, the balance compounds: each trade is sized from the account as it
    stood when the trade opened (its strategy's risk % times its share, see ``weights``), and a
    trade is skipped when it would take the account past ``max_open_trades`` open at once or past
    ``max_open_risk_pct`` of the balance at risk to the stops. Each trade's result scales with
    its new size against the backtest's size; a trade the backtest sized at 0 stays at 0.
    """
    from .risk import position_size

    n = len(parts)
    scale = {sid: (weights[sid] * n if weights else 1.0) for sid, _, _ in parts}
    rows = []
    for sid, name, res in parts:
        r = res.strategy.risk
        for row in res.trades.itertuples():
            rows.append({"sid": sid, "entry": pd.Timestamp(row.entry_time), "exit": pd.Timestamp(row.exit_time),
                         "entry_price": float(row.entry_price), "stop": float(row.stop), "size": float(row.size),
                         "pnl": float(row.pnl), "risk_pct": r.risk_per_trade_pct * scale[sid], "risk": r})
    rows.sort(key=lambda x: (x["entry"], x["exit"]))

    equity = float(balance)
    open_: list[dict] = []
    eq_points, risk_events, open_events, taken = [], [], [], []
    per = {sid: {"id": sid, "name": name, "market": res.metrics["market"], "trades": res.metrics["trades"],
                 "taken": 0, "skipped": 0, "pnl": 0.0, "win_rate_pct": None,
                 "direction": res.strategy.direction, "timeframe": res.strategy.market.timeframe,
                 "weight_pct": (weights[sid] if weights else 1.0 / n) * 100} for sid, name, res in parts}
    skipped = {"open_trades": 0, "open_risk": 0, "too_small": 0}

    def close_until(t):
        nonlocal equity
        open_.sort(key=lambda x: x["exit"])
        while open_ and open_[0]["exit"] <= t:
            tr = open_.pop(0)
            equity += tr["pnl"]
            per[tr["sid"]]["pnl"] += tr["pnl"]
            eq_points.append((tr["exit"], equity))
            risk_events.append((tr["exit"], -tr["risk_amount"], tr["sid"]))
            open_events.append((tr["exit"], -1, tr["sid"]))

    for tr in rows:
        close_until(tr["entry"])
        p = per[tr["sid"]]
        if max_open_trades is not None and len(open_) >= max_open_trades:
            p["skipped"] += 1
            skipped["open_trades"] += 1
            continue
        r = tr["risk"]
        dist = abs(tr["entry_price"] - tr["stop"])
        size = position_size(equity, tr["risk_pct"], dist, r.point_value, r.size_step, r.min_size, r.max_size)
        if size <= 0:
            p["skipped"] += 1
            skipped["too_small"] += 1
            continue
        risk_amount = dist * size * r.point_value
        open_risk = sum(x["risk_amount"] for x in open_)
        if max_open_risk_pct is not None and equity > 0 and (open_risk + risk_amount) / equity * 100 > max_open_risk_pct + 1e-9:
            p["skipped"] += 1
            skipped["open_risk"] += 1
            continue
        pnl = tr["pnl"] * size / tr["size"] if tr["size"] > 0 else 0.0
        open_.append({**tr, "size": size, "pnl": pnl, "risk_amount": risk_amount})
        taken.append({"sid": tr["sid"], "pnl": pnl, "entry": tr["entry"], "exit": tr["exit"]})
        risk_events.append((tr["entry"], risk_amount, tr["sid"]))
        open_events.append((tr["entry"], 1, tr["sid"]))
        p["taken"] += 1
    close_until(pd.Timestamp("2200-01-01", tz="UTC"))

    start = min(res.equity.index[0] for _, _, res in parts)
    end = max([res.equity.index[-1] for _, _, res in parts] + [t for t, _ in eq_points])  # exits can land after the last candle
    grid = pd.date_range(start.floor("h"), end.ceil("h"), freq="1h")
    eq = pd.Series([balance] + [v for _, v in eq_points], index=pd.DatetimeIndex([start] + [t for t, _ in eq_points]))
    eq = eq[~eq.index.duplicated(keep="last")].sort_index()
    equity_s = eq.reindex(eq.index.union(grid)).ffill().reindex(grid).fillna(float(balance))
    dd = (equity_s / equity_s.cummax().clip(lower=balance) - 1) * 100

    curves = {}
    for sid in per:
        pts = [(t["exit"], t["pnl"]) for t in taken if t["sid"] == sid]
        c = pd.Series([0.0] + [v for _, v in pts], index=pd.DatetimeIndex([start] + [t for t, _ in pts]))
        c = c.groupby(level=0).sum().sort_index().cumsum()
        curves[sid] = c.reindex(c.index.union(grid)).ffill().reindex(grid).fillna(0.0)
    aligned = pd.DataFrame(curves)

    if risk_events:
        ev = pd.DataFrame(risk_events, columns=["time", "amount", "sid"])
        ev["time"] = pd.to_datetime(ev["time"], utc=True)
        ev = ev.sort_values(["time", "amount"])
        running = ev.set_index("time")["amount"].cumsum()
        running = running[~running.index.duplicated(keep="last")]
        amt = running.reindex(running.index.union(grid)).ffill().reindex(grid).fillna(0.0).clip(lower=0)
        open_risk_s = amt / equity_s.where(equity_s > 0, np.nan) * 100
        open_risk_s = open_risk_s.fillna(0.0)
    else:
        open_risk_s = pd.Series(0.0, index=grid)

    per_list = list(per.values())
    for p in per_list:
        wins = [t for t in taken if t["sid"] == p["id"] and t["pnl"] > 0]
        p["win_rate_pct"] = len(wins) / p["taken"] * 100 if p["taken"] else None
        p["return_pct"] = p["pnl"] / balance * 100
        own = balance + aligned[p["id"]]
        p["max_drawdown_pct"] = float(((own / own.cummax().clip(lower=balance) - 1) * 100).min())
    daily = aligned.resample("1D").last().diff().dropna(how="all")
    corr = None
    if aligned.shape[1] > 1 and len(daily) > 10:
        c = daily.loc[:, daily.std() > 0].corr()
        corr = {"ids": list(c.columns), "values": [[None if pd.isna(x) else float(x) for x in row] for row in c.to_numpy()]}
    exposure = _exposure(per_list, aligned, open_events, grid, daily, corr)
    return {
        "per_strategy": per_list,
        "equity": equity_s,
        "drawdown": dd,
        "open_risk": open_risk_s,
        "pnl_curves": aligned,
        "exposure": exposure,
        "skipped": skipped,
        "metrics": {
            "start": grid[0].isoformat(), "end": grid[-1].isoformat(),
            "start_equity": balance, "end_equity": float(equity_s.iloc[-1]),
            "total_return_pct": (float(equity_s.iloc[-1]) / balance - 1) * 100,
            "max_drawdown_pct": float(dd.min()),
            "trades": len(taken),
            "trades_skipped": int(sum(skipped.values())),
            "max_open_risk_pct": float(open_risk_s.max()),
            "avg_open_risk_pct": float(open_risk_s[open_risk_s > 0].mean()) if (open_risk_s > 0).any() else 0.0,
            "time_with_risk_pct": float((open_risk_s > 0).mean() * 100),
            "max_open_trades": exposure["max_open_trades"],
            "time_in_market_pct": exposure["time_in_market_pct"],
            "avg_correlation": exposure["avg_correlation"],
            "diversification_ratio": exposure["diversification_ratio"],
        },
        "correlation": corr,
    }


# --------------------------------------------------------------------------- random markets


def shuffled_market(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """A random price path with the same candles, in a random order.

    Each candle keeps its shape relative to the previous close (gap, high, low, close),
    so volatility, fat tails and the overall drift stay the same, but any pattern from
    one candle to the next is destroyed. Candles are only swapped with others from the
    same weekday and time of day, so busy and quiet hours (e.g. the market open) stay
    where they were.
    """
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    prev = np.empty_like(c)
    prev[0] = o[0]
    prev[1:] = c[:-1]
    ratios = {"open": o / prev, "high": h / prev, "low": l / prev, "close": c / prev}
    idx = df.index
    key = idx.day_of_week.to_numpy() * 1440 + idx.hour.to_numpy() * 60 + idx.minute.to_numpy()
    order = np.arange(len(df))
    for k in np.unique(key):
        pos = np.flatnonzero(key == k)
        order[pos] = rng.permutation(pos)
    rc = ratios["close"][order]
    closes = prev[0] * np.cumprod(rc)
    new_prev = np.empty_like(closes)
    new_prev[0] = prev[0]
    new_prev[1:] = closes[:-1]
    out = pd.DataFrame({
        "open": new_prev * ratios["open"][order],
        "high": new_prev * ratios["high"][order],
        "low": new_prev * ratios["low"][order],
        "close": closes,
    }, index=idx)
    if "volume" in df.columns:
        out["volume"] = df["volume"].to_numpy()[order]
    if "spread" in df.columns:
        out["spread"] = (df["spread"].to_numpy(dtype=float) / prev)[order] * new_prev
    return out


# --------------------------------------------------------------------------- luck check


@dataclass
class _Check:
    key: str
    passed: bool | None
    title: str
    detail: str

    def public(self) -> dict:
        return {"key": self.key, "passed": self.passed, "title": self.title, "detail": self.detail,
                "next_step": next_step(self)}


def _max_drawdown(eq: np.ndarray) -> np.ndarray:
    """Worst drop from a peak, in % (negative), for each row of a 2-D array of balances."""
    peak = np.maximum.accumulate(eq, axis=-1)
    return ((eq / peak) - 1).min(axis=-1) * 100


def monte_carlo(trades: pd.DataFrame, start_equity: float, rng: np.random.Generator | None = None,
                runs: int = N_SHUFFLES, compound: bool = True) -> dict | None:
    """Shuffle the order of the backtest's trades ``runs`` times and measure the worst drawdown
    of each order. With ``compound`` each trade's gain is kept as a % of the balance before it,
    so the sizes still compound; without it (fixed position sizes) each trade's money result is
    added as is. None with fewer than 2 trades."""
    n = len(trades)
    if n < 2:
        return None
    rng = rng or np.random.default_rng(0)
    pnl = trades["pnl"].to_numpy(dtype=float)
    perms = np.argsort(rng.random((runs, n)), axis=1)
    if compound:
        before = (trades["equity_after"] - trades["pnl"]).to_numpy(dtype=float)
        pct = np.nan_to_num(pnl / np.where(before > 0, before, np.nan))
        eq = start_equity * np.cumprod(1 + pct[perms], axis=1)
        path = start_equity * np.cumprod(1 + pct)
    else:
        eq = start_equity + np.cumsum(pnl[perms], axis=1)
        path = start_equity + np.cumsum(pnl)
    eq = np.concatenate([np.full((runs, 1), start_equity), eq], axis=1)
    dd = _max_drawdown(eq)
    actual = float(_max_drawdown(np.concatenate([[start_equity], path]))[()])
    p5, p50, p95 = (float(x) for x in np.percentile(dd, [5, 50, 95]))
    return {"actual_dd": actual, "median_dd": p50, "bad_dd": p5, "good_dd": p95, "runs": runs,
            "p5": p5, "p50": p50, "p95": p95, "histogram": _dd_histogram(dd)}


def _dd_histogram(dd: np.ndarray, buckets: int = 12) -> list[dict]:
    """Counts of reshuffled orders per band of worst drawdown (% as positive numbers)."""
    depth = -dd
    hi = float(np.ceil(depth.max())) or 1.0
    edges = np.linspace(0, hi, buckets + 1)
    counts, _ = np.histogram(depth, bins=edges)
    return [{"from": float(edges[i]), "to": float(edges[i + 1]), "count": int(c)} for i, c in enumerate(counts)]


# Factual next steps when a luck check does not pass. No judgement words: the user decides.
NEXT_STEPS = {
    "trades": "Test over a longer period, add more markets, or use rules that trigger more often, then run the luck check again.",
    "halves": "Compare the trades in each half: the market may have behaved differently. "
              "Test the rules over other periods and markets to see which conditions they need.",
    "resample": "The profit depends on a few large trades. Look at the largest wins in the trade list "
                "and check whether they would be repeatable, then test over more data.",
    "random": "The rules did about as well on random price paths. The result is consistent with the market's drift "
              "or chance. Test on other markets or periods, or change the entry rule, and run the luck check again.",
}


def next_step(check: "_Check") -> str | None:
    if check.passed is None:
        return None
    return None if check.passed else NEXT_STEPS.get(check.key)


def luck_check(strategy: Strategy, df: pd.DataFrame, fine: pd.DataFrame | None, start: pd.Timestamp | None,
               time_budget: float = 40.0, max_random: int = 40, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    t0 = time.monotonic()
    res = run_backtest(strategy, df, fine, start=start)
    trades = res.trades
    n = len(trades)
    m = res.metrics
    out: dict = {"trades": n, "return_pct": m["total_return_pct"], "max_drawdown_pct": m["max_drawdown_pct"],
                 "start": m["start"], "end": m["end"], "currency": strategy.risk.symbol}
    checks: list[_Check] = []

    # 1. enough trades
    checks.append(_Check(
        "trades", n >= MIN_TRADES, f"{n} trade{'s' if n != 1 else ''}",
        f"With fewer than {MIN_TRADES} trades a few lucky ones can decide the whole result. "
        + ("That's enough to judge." if n >= MIN_TRADES else "Try a longer test period or rules that trade more often."),
    ))

    # 2. both halves
    first, second = halves(res)
    sym = strategy.risk.symbol
    both = first > 0 and second > 0
    checks.append(_Check(
        "halves", both if n else None, "Profitable in both halves of the period" if both else "Not profitable in both halves",
        f"First half {_money(first, sym)}, second half {_money(second, sym)}. "
        + ("A result that only comes from one stretch of time is more likely luck." if not both else
           "The result didn't all come from one lucky stretch."),
    ))
    out["halves"] = {"first": first, "second": second}

    # 3. resample the trades
    if n >= 5:
        r = trades["r_multiple"].to_numpy(dtype=float)
        idx = rng.integers(0, n, (N_RESAMPLES, n))
        means = r[idx].mean(axis=1)
        p = float((means <= 0).mean())
        lo, hi = np.percentile(means, [5, 95])
        out["resample"] = {"p_losing": p, "avg_r": float(r.mean()), "avg_r_low": float(lo), "avg_r_high": float(hi),
                           "runs": N_RESAMPLES}
        checks.append(_Check(
            "resample", p < 0.05,
            f"Lost money in {p:.1%} of {N_RESAMPLES:,} reshuffled trade sets",
            "Draws the same number of trades at random from this backtest's trades, thousands of times. "
            "If many of those sets lose money, the profit leans on a handful of trades. Under 5% passes.",
        ))
        # trade order: how bad could the drawdown have been with the same trades in another order?
        out["order"] = monte_carlo(trades, strategy.risk.account_size, rng, compound=strategy.risk.compound)
    else:
        checks.append(_Check("resample", None, "Too few trades to resample",
                             "Needs at least 5 trades."))

    # 4. random markets with the same volatility
    coarse = res if fine is None or fine.empty else run_backtest(strategy, df, None, start=start)
    real = coarse.metrics["total_return_pct"]
    one = max(0.05, time.monotonic() - t0)
    target = int(min(max_random, max(8, (time_budget - (time.monotonic() - t0)) / max(one / 2, 0.01))))
    randoms = []
    deadline = time.monotonic() + max(5.0, time_budget - (time.monotonic() - t0))
    while len(randoms) < target and (len(randoms) < 5 or time.monotonic() < deadline):
        path = shuffled_market(df, rng)
        try:
            randoms.append(run_backtest(strategy, path, None, start=start).metrics["total_return_pct"])
        except ValueError:
            break
    if randoms:
        arr = np.array(randoms)
        beaten = int((arr < real).sum())
        share = beaten / len(arr)
        out["random"] = {"real": real, "returns": [float(x) for x in arr], "beaten": beaten, "runs": len(arr),
                         "median": float(np.median(arr))}
        checks.append(_Check(
            "random", share >= 0.9,
            f"Beat {beaten} of {len(arr)} random markets",
            "Runs the same rules on made-up price paths built from this market's own candles in a random order: "
            "same ups and downs and the same overall drift, but no real patterns. If the rules do about as well on "
            "those, the result probably came from the market's drift or chance rather than the idea. Beating 90% passes.",
        ))

    out["checks"] = [c.public() for c in checks]
    decided = [c for c in checks if c.passed is not None]
    out["passed"] = sum(1 for c in decided if c.passed)
    out["total"] = len(decided)
    out["profitable"] = m["total_return_pct"] > 0
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


def _money(v: float, sym: str) -> str:
    return f"{'-' if v < 0 else '+'}{sym}{abs(v):,.2f}"


# --------------------------------------------------------------------------- forward test comparison


PCTS = (10, 25, 50, 75, 90)


def backtest_range(equity: pd.Series, hours: list[float], min_windows: int = 30) -> list[list[float] | None]:
    """For each horizon (in hours), the 10/25/50/75/90th percentile % change of the
    balance over every stretch of that length in the backtest. None where the backtest
    is too short to have enough stretches.
    """
    if equity.empty:
        return [None for _ in hours]
    eq = equity.copy()
    if eq.index.tz is None:
        eq.index = eq.index.tz_localize("UTC")
    grid = eq.resample("1h").last().ffill().dropna().to_numpy(dtype=float)
    out: list[list[float] | None] = []
    for hrs in hours:
        k = int(round(hrs))
        if k <= 0:
            out.append([0.0] * len(PCTS))
            continue
        if len(grid) - k < min_windows:
            out.append(None)
            continue
        change = (grid[k:] / grid[:-k] - 1) * 100
        out.append([float(x) for x in np.percentile(change, PCTS)])
    return out


def bar_hours(timeframe: str) -> float:
    return timeframes.to_timedelta(timeframe) / pd.Timedelta(hours=1)


# --------------------------------------------------------------------------- why didn't it trade?


def condition_counts(spec, df: pd.DataFrame, since: pd.Timestamp | None = None) -> list[dict]:
    """How often each part of an entry rule held over ``df``: one row per group and leaf
    condition, in rule order, with its path in the rule tree (``entry.all[1]``), the raw
    condition, the candles it matched and the candles checked. Shown when a backtest
    has no trades, to point at the part that blocked it."""
    from . import rules

    ctx = rules.Context(df)  # indicators warm up on the candles before ``since``
    in_period = (df.index >= since) if since is not None else np.ones(len(df), dtype=bool)
    total = int(in_period.sum())
    out: list[dict] = []

    def walk(node, path: str, depth: int):
        try:
            matched = int(rules.evaluate(rules.compile_condition(node, path), df, ctx).to_numpy()[in_period].sum())
        except Exception:  # noqa: BLE001  (a part that can't be evaluated alone still gets a row)
            matched = None
        kind = next((k for k in ("all", "any", "not") if isinstance(node, dict) and k in node), "leaf")
        out.append({"path": path, "kind": kind, "depth": depth, "condition": node, "matched": matched, "candles": total})
        if kind in ("all", "any"):
            for i, child in enumerate(node[kind]):
                walk(child, f"{path}.{kind}[{i}]", depth + 1)
        elif kind == "not":
            walk(node["not"], f"{path}.not", depth + 1)

    walk(spec, "entry", 0)
    return out


def why_no_trades(strategy: Strategy, df: pd.DataFrame, start: pd.Timestamp | None) -> dict:
    rows = condition_counts(strategy.entry_spec, df, start)
    return {"candles": rows[0]["candles"] if rows else 0, "conditions": rows}


# --------------------------------------------------------------------------- live vs backtest, trade by trade

WINDOW_METRICS = ("win_rate_pct", "profit_factor", "avg_r", "max_drawdown_pct", "trades_per_month",
                  "gross_pnl", "spread_cost", "funding")
FEW_TRADES = 20


def trades_frame(rows: list[dict]) -> pd.DataFrame:
    """Closed trades (backtest or paper) as a frame with the columns the window metrics need."""
    cols = ["entry_time", "exit_time", "pnl", "r_multiple", "spread_cost", "funding"]
    df = pd.DataFrame(rows, columns=cols) if not isinstance(rows, pd.DataFrame) else rows.reindex(columns=cols)
    df = df.dropna(subset=["pnl"]).copy()
    for c in ("pnl", "r_multiple", "spread_cost", "funding"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    for c in ("entry_time", "exit_time"):
        df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
    return df.reset_index(drop=True)


def window_metrics(trades: pd.DataFrame, start_equity: float) -> dict | None:
    """The facts about one stretch of trades: how often it won, profit factor, average R,
    the worst drop (as % of the starting balance), trades a month, and the gross P&L
    with what spread and funding took out of it."""
    if trades is None or len(trades) == 0:
        return None
    pnl = trades["pnl"].to_numpy(dtype=float)
    wins, losses = pnl[pnl > 0].sum(), -pnl[pnl <= 0].sum()
    eq = start_equity + np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate(([start_equity], eq)))[1:]
    dd = float(np.max((peak - eq) / peak * 100)) if start_equity > 0 else 0.0
    t0, t1 = trades["entry_time"].min(), trades["exit_time"].max()
    months = max((t1 - t0) / pd.Timedelta(days=30.4375), 1 / 30.4375) if pd.notna(t0) and pd.notna(t1) else None
    gross = float(pnl.sum() + trades["spread_cost"].sum() + trades["funding"].sum())
    return {
        "trades": int(len(pnl)),
        "win_rate_pct": float((pnl > 0).mean() * 100),
        "profit_factor": float(wins / losses) if losses > 0 else (float("inf") if wins > 0 else 0.0),
        "avg_r": float(trades["r_multiple"].mean()),
        "max_drawdown_pct": dd,
        "trades_per_month": float(len(pnl) / months) if months else None,
        "gross_pnl": gross,
        "spread_cost": float(trades["spread_cost"].sum()),
        "funding": float(trades["funding"].sum()),
        "net_pnl": float(pnl.sum()),
    }


def few_trades_notice(n: int) -> str | None:
    """Below 20 live trades the ranges say little, and the page says so in plain words."""
    return f"Only {n} trades so far — ranges are wide with this few trades" if n < FEW_TRADES else None


def _position(live: float | None, low: float, high: float) -> str | None:
    if live is None or not np.isfinite(live):
        return None
    return "below" if live < low else "above" if live > high else "within"


def trade_window_ranges(backtest: pd.DataFrame, live: list[dict] | pd.DataFrame, start_equity: float,
                        samples: int = 400, rng: np.random.Generator | None = None, max_window: int = 30) -> dict:
    """Where the live paper trades sit against the backtest, trade for trade.

    The last ``window`` live trades (all of them, up to ``max_window``) are measured, and
    the backtest is resampled ``samples`` times into stretches of the same number of
    consecutive trades (a block bootstrap, so trades a month keeps its meaning). The
    10th–90th percentile of each measure is the backtest's usual range; the live value
    is reported as below, within or above it. Facts only, no verdict."""
    rng = rng or np.random.default_rng(7)
    bt, lv = trades_frame(backtest), trades_frame(live)
    n_live, n_bt = len(lv), len(bt)
    window = max(1, min(n_live, max_window)) if n_live else min(max_window, max(1, n_bt))
    out = {"live_trades": n_live, "backtest_trades": n_bt, "window": window,
           "notice": few_trades_notice(n_live), "metrics": {}}
    if n_bt < window + 5:
        out["reason"] = "The backtest has too few trades to build a range of this length"
        return out
    starts = rng.integers(0, n_bt - window + 1, size=samples)
    draws = {k: [] for k in WINDOW_METRICS}
    for s0 in starts:
        m = window_metrics(bt.iloc[s0:s0 + window], start_equity)
        for k in WINDOW_METRICS:
            v = m[k]
            if v is not None and np.isfinite(v):
                draws[k].append(v)
    live_m = window_metrics(lv.iloc[-window:], start_equity) if n_live else None
    for k in WINDOW_METRICS:
        if not draws[k]:
            continue
        low, med, high = (float(x) for x in np.percentile(draws[k], (10, 50, 90)))
        lv_v = live_m[k] if live_m else None
        out["metrics"][k] = {"low": low, "median": med, "high": high,
                             "live": None if lv_v is None or not np.isfinite(lv_v) else float(lv_v),
                             "position": _position(lv_v, low, high)}
    out["live"] = live_m
    return out


# --------------------------------------------------------------------------- optimiser


MAX_OPT_SETTINGS = 3
MAX_COMBOS = {"selfhost": 2000, "cloud": 200}
SCORES = ("return", "return_dd", "profit_factor", "sharpe")
MAX_HOLDOUT_PCT = 50
WALK_FORWARD_FOLDS = (3, 4, 5, 6)


def combo_count(axes: list[list]) -> int:
    n = 1
    for values in axes:
        n *= len(values)
    return n


def grid(axes: list[list], cap: int) -> list[tuple]:
    """Every combination of the values in ``axes`` (the cross product), in order.
    Raises ``ValueError`` when there are no values, too many settings, or more than ``cap`` combinations."""
    import itertools

    if not axes or any(not isinstance(v, list) or not v for v in axes):
        raise ValueError("Give at least one value for each setting")
    if len(axes) > MAX_OPT_SETTINGS:
        raise ValueError(f"Optimise at most {MAX_OPT_SETTINGS} settings at once")
    n = combo_count(axes)
    if n > cap:
        raise ValueError(f"That's {n:,} combinations; this edition runs at most {cap:,} in one go. Use bigger steps or fewer values.")
    return list(itertools.product(*axes))


def score_of(row: dict, score: str) -> float | None:
    """The number a combination is ranked by. ``None`` when it can't be worked out."""
    if row.get("error") or not row.get("trades"):
        return None
    if score == "return_dd":
        dd = abs(row.get("max_drawdown_pct") or 0.0)
        return row["return_pct"] / dd if dd > 0 else (row["return_pct"] if row["return_pct"] > 0 else None)
    if score == "profit_factor":
        pf = row.get("profit_factor")
        return None if pf is None else float(pf)
    if score == "sharpe":
        return row.get("sharpe")
    return row.get("return_pct")


def _sharpe(trades: pd.DataFrame) -> float | None:
    """Per-trade Sharpe scaled to the number of trades: how steady the profits were."""
    if len(trades) < 2:
        return None
    pnl = trades["pnl"].to_numpy(dtype=float)
    sd = float(pnl.std(ddof=1))
    return float(pnl.mean() / sd * np.sqrt(len(pnl))) if sd > 0 else None


def run_chunk(df: pd.DataFrame, fine: pd.DataFrame | None, start: pd.Timestamp | None,
              items: list[tuple[tuple, dict | str]]) -> list[dict]:
    """Backtest each (values, strategy dict). Every strategy in the chunk shares one indicator
    cache, so an indicator that several combinations use is computed once."""
    import copy

    from . import rules
    from .strategy import check_runtime, strategy_from_dict

    ctx = rules.Context(df)
    rows = []
    for values, d in items:
        row = {"values": list(values), "error": None}
        if isinstance(d, str):
            rows.append({**row, "error": d})
            continue
        try:
            s = strategy_from_dict(copy.deepcopy(d))
            check_runtime(s)
            res = run_backtest(s, df, fine, start=start, ctx=ctx)
        except Exception as exc:  # noqa: BLE001 - an invalid combination is that row's error
            rows.append({**row, "error": str(exc)})
            continue
        rows.append({**row, **summary(res), "sharpe": _sharpe(res.trades), "_trades": _closed(res)})
    return rows


def _closed(res: BacktestResult) -> tuple:
    """(start equity, exit times as ns, P&L) of the closed trades: enough to replay the balance
    over any stretch of the period without running the backtest again."""
    t = res.trades
    if not len(t):
        return float(res.metrics.get("start_equity") or 0.0), np.empty(0, dtype="int64"), np.empty(0)
    exits = pd.to_datetime(t["exit_time"], utc=True).to_numpy(dtype="datetime64[ns]").astype("int64")
    return float(res.metrics.get("start_equity") or 0.0), exits, t["pnl"].to_numpy(dtype=float)


def _ns(ts: pd.Timestamp) -> int:
    ts = pd.Timestamp(ts)
    return int((ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")).value)


def stretch(closed: tuple, a: int, b: int | None) -> dict:
    """How the trades that closed in [a, b) did, starting from the balance as it stood at ``a``.
    A trade counts in the stretch its exit falls in, so a position open across the edge is
    booked to the later stretch. ``b`` None means to the end (a position closed out when the
    data ran out can be stamped just after the last candle)."""
    start_eq, exits, pnl = closed
    base = start_eq + float(pnl[exits < a].sum())
    p = pnl[(exits >= a) & ((exits < b) if b is not None else True)]
    wins, losses = float(p[p > 0].sum()), float(-p[p <= 0].sum())
    eq = base + np.cumsum(p)
    peak = np.maximum.accumulate(np.concatenate(([base], eq)))[1:]
    dd = float(np.max((peak - eq) / peak * 100)) if len(p) and base > 0 else 0.0
    sd = float(p.std(ddof=1)) if len(p) >= 2 else 0.0
    return {
        "trades": int(len(p)),
        "return_pct": float(p.sum() / base * 100) if base > 0 else 0.0,
        "max_drawdown_pct": dd,
        "profit_factor": float(wins / losses) if losses > 0 else None,
        "sharpe": float(p.mean() / sd * np.sqrt(len(p))) if sd > 0 else None,
    }


def _rank_key(row: dict, part: dict, score: str, min_trades: int) -> tuple:
    sc = score_of({**row, **part}, score)
    return (sc is not None, part["trades"] >= min_trades, sc if sc is not None else 0.0)


def holdout_check(rows: list[dict], score: str, min_trades: int, a: int, cut: int, b: int, pct: float) -> dict:
    """Rank on the first part of the period only, then show how the winner did on the part kept back."""
    for r in rows:
        if r["error"]:
            r["train"] = r["holdout"] = None
            continue
        r["train"] = stretch(r["_trades"], a, cut)
        r["holdout"] = stretch(r["_trades"], cut, None)
    ok = [r for r in rows if not r["error"]]
    out = {"pct": pct, "from": pd.Timestamp(cut, tz="UTC").isoformat(), "to": pd.Timestamp(b - 1, tz="UTC").isoformat(),
           "best": None, "hindsight": None}
    if not ok:
        return out
    best = max(ok, key=lambda r: _rank_key(r, r["train"], score, min_trades))
    if score_of({**best, **best["train"]}, score) is not None:
        out["best"] = {"values": best["values"], "train_return_pct": best["train"]["return_pct"],
                       "holdout_return_pct": best["holdout"]["return_pct"], "holdout_trades": best["holdout"]["trades"]}
    hind = max(ok, key=lambda r: r["holdout"]["return_pct"])
    out["hindsight"] = {"values": hind["values"], "holdout_return_pct": hind["holdout"]["return_pct"]}
    return out


def walk_forward_grid(rows: list[dict], score: str, min_trades: int, a: int, b: int, folds: int, b_open: bool = True) -> dict:
    """Walk-forward over the whole grid: before each stretch pick the combination with the best
    score on everything so far, then record how it did on that stretch. Chaining those results
    is what tuning as you went would have given you."""
    ok = [r for r in rows if not r["error"]]
    span = pd.Timedelta(b - a, unit="ns")
    if len(ok) < 2:
        return {"folds": folds, "skipped": "Too few combinations could be tested."}
    if span < pd.Timedelta(days=30 * folds):
        return {"folds": folds, "skipped": f"The period is too short to split into {folds} stretches: use at least {folds} months."}
    edges = [int(x) for x in np.linspace(a, b, folds + 2)]
    edges[-1] = None if b_open else edges[-1]
    windows, chained = [], 1.0
    for k in range(1, folds + 1):
        scored = [(_rank_key(r, stretch(r["_trades"], edges[0], edges[k]), score, min_trades), r) for r in ok]
        key, pick = max(scored, key=lambda x: x[0])
        if not key[0]:
            windows.append({"train_to": pd.Timestamp(edges[k], tz="UTC").isoformat(), "test_to": pd.Timestamp(edges[k + 1] or b - 1, tz="UTC").isoformat(),
                            "values": None, "train_score": None, "test_return_pct": 0.0, "test_trades": 0})
            continue
        train = stretch(pick["_trades"], edges[0], edges[k])
        test = stretch(pick["_trades"], edges[k], edges[k + 1])
        chained *= 1 + test["return_pct"] / 100
        windows.append({"train_to": pd.Timestamp(edges[k], tz="UTC").isoformat(), "test_to": pd.Timestamp(edges[k + 1] or b - 1, tz="UTC").isoformat(),
                        "values": pick["values"], "train_score": key[2], "train_return_pct": train["return_pct"],
                        "test_return_pct": test["return_pct"], "test_trades": test["trades"]})
    later = [(stretch(r["_trades"], edges[1], edges[-1])["return_pct"], r["values"]) for r in ok]
    hind_pct, hind_values = max(later, key=lambda x: x[0])
    return {
        "folds": folds,
        "windows": windows,
        "walk_forward_pct": (chained - 1) * 100,
        "hindsight_pct": hind_pct,
        "hindsight_values": hind_values,
        "average_pct": float(np.mean([x[0] for x in later])),
        "from": pd.Timestamp(edges[1], tz="UTC").isoformat(),
        "to": pd.Timestamp(b - 1, tz="UTC").isoformat(),
    }


def _chunks(items: list, workers: int) -> list[list]:
    size = max(1, min(20, -(-len(items) // max(1, workers * 6))))
    return [items[i:i + size] for i in range(0, len(items), size)]


def optimise(progress, variants: list[tuple[tuple, dict | str]], df: pd.DataFrame, fine: pd.DataFrame | None,
             start: pd.Timestamp | None, score: str = "return", min_trades: int = MIN_TRADES, workers: int = 1,
             extra: dict | None = None, holdout_pct: float = 0, walk_forward_folds: int = 0) -> dict:
    """Backtest every combination and rank them by ``score``.

    ``holdout_pct`` keeps back the last part of the period: rows are ranked on the rest and the
    winner's result on the kept-back part is reported separately. ``walk_forward_folds`` > 0 adds
    a walk-forward check over the grid (on the part that isn't kept back). Both reuse the one
    backtest per combination, splitting its trades by when they closed.

    ``progress`` is a ``jobs.Progress`` (or None). With ``workers`` > 1 the chunks run in a
    process pool (self-hosted); otherwise one after another in this process (hosted, where
    the job already has its own worker). Cancelling stops after the current chunk.
    """
    from .jobs import Cancelled

    if score not in SCORES:
        raise ValueError("Choose how to rank the results")
    t0 = time.time()
    total = len(variants)
    chunks = _chunks(variants, workers)
    workers = min(workers, len(chunks))
    rows: list[dict] = []

    def tick(done: int):
        if progress is not None:
            progress.update(done, total, f"{done:,} of {total:,} combinations tested")
            progress.check()

    tick(0)
    if workers <= 1:
        for chunk in chunks:
            rows += run_chunk(df, fine, start, chunk)
            tick(len(rows))
    else:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor, as_completed

        pool = ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"))
        try:
            futures = [pool.submit(run_chunk, df, fine, start, chunk) for chunk in chunks]
            for fut in as_completed(futures):
                rows += fut.result()
                try:
                    tick(len(rows))
                except Cancelled:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    by_values = {tuple(r["values"]): r for r in rows}
    rows = [by_values[tuple(v)] for v, _ in variants]  # back in grid order
    period_start = df.index[0] if start is None else max(df.index[0], start)
    a, b = _ns(period_start), _ns(df.index[-1]) + 1
    cut = a + int((b - a) * (1 - holdout_pct / 100)) if holdout_pct else b
    holdout = holdout_check(rows, score, min_trades, a, cut, b, holdout_pct) if holdout_pct else None
    wf = walk_forward_grid(rows, score, min_trades, a, cut, walk_forward_folds, b_open=not holdout_pct) if walk_forward_folds else None
    for r in rows:
        part = r["train"] if holdout and r.get("train") else {}
        r["score"] = score_of({**r, **part}, score)
        r["enough_trades"] = bool(not r["error"] and (part or r).get("trades", 0) >= min_trades)
        r["holdout_score"] = score_of({**r, **r["holdout"]}, score) if holdout and r.get("holdout") else None
        r.pop("_trades", None)
    ranked = sorted((r for r in rows if r["score"] is not None), key=lambda r: (r["enough_trades"], r["score"]), reverse=True)
    best = ranked[0] if ranked else None
    return {
        "rows": rows,
        "best": best["values"] if best else None,
        "score": score,
        "min_trades": min_trades,
        "holdout": holdout,
        "walk_forward": wf,
        "tested": sum(1 for r in rows if not r["error"]),
        "failed": sum(1 for r in rows if r["error"]),
        "seconds": round(time.time() - t0, 1),
        "start": str(df.index[0] if start is None else max(df.index[0], start)),
        "end": str(df.index[-1]),
        **(extra or {}),
    }
