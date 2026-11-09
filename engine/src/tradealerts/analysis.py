"""Checks that go beyond a single backtest.

* ``sweep``: run the same rules with one setting changed to each of several values.
* ``luck_check``: is a backtest's result likely to be luck? Ported from the CLI research
  scripts: resample the trades, check both halves of the period, and run the same rules
  on random price paths with the same volatility.
* ``backtest_range``: how the balance moved over stretches of a given length in a
  backtest, used to compare a forward test with its own history.

None of these predict future results; they describe the past more honestly.
"""

from __future__ import annotations

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


def portfolio(parts: list[tuple[str, str, BacktestResult]], balance: float) -> dict:
    """Combine several backtests that each risked a % of the same starting balance.

    Each strategy was run without compounding at ``balance``, so adding their profit
    and loss together gives the shared account. Also works out how much was at risk
    at once (every open trade's distance to its stop) and how alike the strategies' days were.
    """
    pnl_curves, per, risk_events = {}, [], []
    for sid, name, res in parts:
        pnl = res.equity - res.strategy.risk.account_size
        pnl_curves[sid] = pnl[~pnl.index.duplicated(keep="last")]
        t = res.trades
        m = res.metrics
        per.append({"id": sid, "name": name, "market": m["market"], "trades": m["trades"],
                    "pnl": float(pnl.iloc[-1]), "return_pct": float(pnl.iloc[-1]) / balance * 100,
                    "max_drawdown_pct": m["max_drawdown_pct"], "win_rate_pct": m.get("win_rate_pct")})
        if len(t):
            pv = res.strategy.risk.point_value
            for row in t.itertuples():
                amount = abs(row.entry_price - row.stop) * row.size * pv
                risk_events.append((pd.Timestamp(row.entry_time), amount, sid))
                risk_events.append((pd.Timestamp(row.exit_time), -amount, sid))
    grid_start = min(c.index[0] for c in pnl_curves.values())
    grid_end = max(c.index[-1] for c in pnl_curves.values())
    grid = pd.date_range(grid_start.floor("h"), grid_end.ceil("h"), freq="1h")
    aligned = pd.DataFrame({k: v.reindex(v.index.union(grid)).ffill().reindex(grid) for k, v in pnl_curves.items()}).fillna(0.0)
    equity = balance + aligned.sum(axis=1)
    dd = (equity / equity.cummax() - 1) * 100

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
    return {
        "per_strategy": per,
        "equity": equity,
        "drawdown": dd,
        "open_risk": open_risk,
        "metrics": {
            "start": grid[0].isoformat(), "end": grid[-1].isoformat(),
            "start_equity": balance, "end_equity": float(equity.iloc[-1]),
            "total_return_pct": (float(equity.iloc[-1]) / balance - 1) * 100,
            "max_drawdown_pct": float(dd.min()),
            "trades": int(sum(p["trades"] for p in per)),
            "max_open_risk_pct": float(open_risk.max()),
            "avg_open_risk_pct": float(open_risk[open_risk > 0].mean()) if (open_risk > 0).any() else 0.0,
            "time_with_risk_pct": float((open_risk > 0).mean() * 100),
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
        return {"key": self.key, "passed": self.passed, "title": self.title, "detail": self.detail}


def _max_drawdown(eq: np.ndarray) -> np.ndarray:
    """Worst drop from a peak, in % (negative), for each row of a 2-D array of balances."""
    peak = np.maximum.accumulate(eq, axis=-1)
    return ((eq / peak) - 1).min(axis=-1) * 100


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
        before = (trades["equity_after"] - trades["pnl"]).to_numpy(dtype=float)
        pct = trades["pnl"].to_numpy(dtype=float) / np.where(before > 0, before, np.nan)
        pct = np.nan_to_num(pct)
        perms = np.argsort(rng.random((N_SHUFFLES, n)), axis=1)
        start_eq = strategy.risk.account_size
        eq = start_eq * np.cumprod(1 + pct[perms], axis=1)
        eq = np.concatenate([np.full((N_SHUFFLES, 1), start_eq), eq], axis=1)
        dd = _max_drawdown(eq)
        actual = float(_max_drawdown(np.concatenate([[start_eq], start_eq * np.cumprod(1 + pct)]))[()])
        out["order"] = {"actual_dd": actual, "median_dd": float(np.median(dd)), "bad_dd": float(np.percentile(dd, 5)),
                        "runs": N_SHUFFLES}
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
