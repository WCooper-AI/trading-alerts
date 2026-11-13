"""Run a strategy over historical candles and measure the results."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import timeframes
from .data import find_jumps
from .simulator import Event, Simulator
from .strategy import Strategy


@dataclass
class BacktestResult:
    strategy: Strategy
    trades: pd.DataFrame
    equity: pd.Series
    metrics: dict
    skipped: list[Event] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def fine_lookup(fine: pd.DataFrame | None, bar: pd.Timedelta):
    """Function returning the finer candles inside the candle starting at ``t``."""
    if fine is None or fine.empty:
        return lambda t: None
    stamps = fine.index.as_unit("ns").asi8  # pandas 3 may store microseconds; compare in ns
    step = pd.Timedelta(bar).value

    def rows(t: pd.Timestamp) -> pd.DataFrame:
        tv = pd.Timestamp(t).value  # always ns
        a = np.searchsorted(stamps, tv, "left")
        b = np.searchsorted(stamps, tv + step, "left")
        return fine.iloc[a:b]

    return rows


def spreads_for(strategy: Strategy, df: pd.DataFrame) -> np.ndarray | None:
    if strategy.use_data_spread and "spread" in df.columns and df["spread"].notna().any():
        return df["spread"].to_numpy(dtype=float)
    return None


WARMUP_CANDLES = 300  # least history loaded before the test period (Strategy.warmup_candles may ask for more)


def warmup_days(timeframe: str, candles: int = WARMUP_CANDLES) -> int:
    bar = timeframes.to_timedelta(timeframe)
    # calendar days covering ``candles``, with room for nights, weekends and holidays
    span = bar * candles * 1.5
    return max(30, int(span / pd.Timedelta(days=1)) + 1)


def run_backtest(strategy: Strategy, df: pd.DataFrame, fine: pd.DataFrame | None = None,
                 notes: list[str] | None = None, start: pd.Timestamp | None = None,
                 ctx=None) -> BacktestResult:
    """Simulate ``strategy`` over ``df``.

    With ``start``, candles before it are only used to warm up indicators; trading
    and all results begin at ``start``.
    """
    if df.empty:
        raise ValueError("No price data to backtest on")
    bar = timeframes.to_timedelta(strategy.market.timeframe)
    prepared = strategy.prepare(df, ctx)  # on the full history, so indicators are warmed up
    first = 0
    if start is not None:
        first = int(np.searchsorted(df.index.as_unit("ns").asi8, pd.Timestamp(start).value, "left"))
        if first >= len(df):
            raise ValueError("No price data in the chosen period")
    sim = Simulator(strategy)
    get_fine = fine_lookup(fine, bar)
    spreads = spreads_for(strategy, df)
    trades, skipped = [], []
    equity = np.empty(len(df))
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    in_market = 0
    for i in range(first, len(df)):
        t = df.index[i]
        events = sim.step(
            t, o[i], h[i], l[i], c[i],
            bool(prepared.entry[i]), bool(prepared.exit[i]), prepared.refs_at(i),
            spread=None if spreads is None else spreads[i],
            fine=lambda t=t: get_fine(t),
            values=prepared.values_at(i),
        )
        for ev in events:
            if ev.kind == "closed":
                trades.append(ev.data)
            elif ev.kind == "skipped":
                skipped.append(ev)
        if sim.position:
            in_market += 1
        equity[i] = sim.state["equity"] + sim.unrealised(c[i])
    for ev in sim.finish(df.index[-1], c[-1]):
        trades.append(ev.data)
        equity[-1] = sim.state["equity"]

    trades_df = pd.DataFrame(trades)
    period = df.iloc[first:]
    eq = pd.Series(equity[first:], index=period.index, name="equity")
    metrics = compute_metrics(strategy, trades_df, eq, period, in_market)
    metrics["warmup_candles"] = first
    metrics["used_data_spread"] = spreads is not None
    metrics["used_finer_candles"] = fine is not None and not fine.empty

    all_notes = list(notes or [])
    need = strategy.lookback_candles
    if start is not None and first < need:
        all_notes.append(
            f"Your rules need {need} candles of history before they can be checked, but the price data "
            f"only has {first} before the test period, so trades may be missing near the start."
        )
    jumps = find_jumps(period, strategy.market.timeframe)
    if len(jumps):
        worst = jumps["gap"].idxmax()
        all_notes.append(
            f"The price data has {len(jumps)} unusual jump{'s' if len(jumps) != 1 else ''} between "
            f"candles (largest on {worst:%d %b %Y}). These are often futures contract rolls or bad "
            "data, so trades around those dates may not be realistic."
        )
    metrics["data_jumps"] = int(len(jumps))
    return BacktestResult(strategy, trades_df, eq, metrics, skipped, all_notes)


def compute_metrics(strategy: Strategy, trades: pd.DataFrame, equity: pd.Series, df: pd.DataFrame, bars_in_market: int) -> dict:
    start_eq = strategy.risk.account_size
    peak = equity.cummax()
    dd = (equity / peak - 1) * 100
    m = {
        "market": strategy.market.label,
        "timeframe": strategy.market.timeframe,
        "direction": strategy.direction,
        "start": str(df.index[0]),
        "end": str(df.index[-1]),
        "bars": len(df),
        "start_equity": start_eq,
        "end_equity": float(equity.iloc[-1]),
        "total_return_pct": (float(equity.iloc[-1]) / start_eq - 1) * 100,
        "max_drawdown_pct": float(dd.min()),
        "buy_and_hold_pct": (float(df["close"].iloc[-1]) / float(df["open"].iloc[0]) - 1) * 100,
        "time_in_market_pct": bars_in_market / len(df) * 100,
        "trades": int(len(trades)),
        "total_funding": float(trades["funding"].sum()) if len(trades) else 0.0,
        "total_spread_cost": float(trades["spread_cost"].sum()) if len(trades) else 0.0,
        "total_commission": float(trades["commission"].sum()) if len(trades) and "commission" in trades else 0.0,
        "total_borrow": float(trades["borrow"].sum()) if len(trades) and "borrow" in trades else 0.0,
    }
    if len(trades):
        pnl = trades["pnl"]
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        loss_sum = float(-losses.sum())
        m.update(
            win_rate_pct=len(wins) / len(pnl) * 100,
            avg_win=float(wins.mean()) if len(wins) else 0.0,
            avg_loss=float(losses.mean()) if len(losses) else 0.0,
            # None: no losing trades (nothing to divide by), never a division error
            profit_factor=float(wins.sum() / loss_sum) if loss_sum > 0 else None,
            expectancy=float(pnl.mean()),
            avg_r=float(trades["r_multiple"].mean()),
            avg_bars_held=float(trades["bars_held"].mean()),
            longest_losing_streak=streaks(pnl)[1],
            longest_winning_streak=streaks(pnl)[0],
            largest_win=float(pnl.max()),
            largest_loss=float(pnl.min()),
            avg_hold_hours=hold_hours(trades),
            exit_reasons=trades["exit_reason"].value_counts().to_dict(),
            r_histogram=histogram(trades["r_multiple"].to_numpy(dtype=float), R_EDGES),
            hold_histogram=hold_histogram(trades),
        )
    m.update(risk_ratios(equity, strategy.market.timeframe))
    m["longest_underwater_days"] = longest_underwater_days(equity)
    m["monthly_returns"] = monthly_returns(equity)
    return m


# --------------------------------------------------------------------------- deeper metrics

R_EDGES = [-3, -2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2, 3, 4]
HOLD_EDGES_HOURS = [1, 2, 4, 8, 24, 48, 96, 168]  # 1h ... 1 week


def streaks(pnl: pd.Series) -> tuple[int, int]:
    """(longest run of winning trades, longest run of losing trades), in trade order."""
    best_w = best_l = win = loss = 0
    for v in pnl:
        if v > 0:
            win, loss = win + 1, 0
        else:
            win, loss = 0, loss + 1
        best_w, best_l = max(best_w, win), max(best_l, loss)
    return best_w, best_l


def hold_times(trades: pd.DataFrame) -> pd.Series:
    """How long each trade was open, as hours (empty when the trades carry no times)."""
    if "entry_time" not in trades or "exit_time" not in trades:
        return pd.Series(dtype=float)
    entry = pd.to_datetime(trades["entry_time"], utc=True)
    exit_ = pd.to_datetime(trades["exit_time"], utc=True)
    return (exit_ - entry) / pd.Timedelta(hours=1)


def hold_hours(trades: pd.DataFrame) -> float:
    ht = hold_times(trades)
    return float(ht.mean()) if len(ht) else 0.0


def histogram(values: np.ndarray, edges: list[float]) -> list[dict]:
    """Counts per bucket: below the first edge, between each pair, and above the last."""
    bins = [-np.inf, *edges, np.inf]
    counts, _ = np.histogram(values, bins=bins)
    out = []
    for i, c in enumerate(counts):
        lo = None if i == 0 else edges[i - 1]
        hi = None if i == len(counts) - 1 else edges[i]
        out.append({"from": lo, "to": hi, "count": int(c)})
    return out


def hold_histogram(trades: pd.DataFrame) -> list[dict]:
    return histogram(hold_times(trades).to_numpy(dtype=float), HOLD_EDGES_HOURS)


def risk_ratios(equity: pd.Series, timeframe: str) -> dict:
    """Sharpe and Sortino ratios from candle-to-candle changes in the balance, annualised
    assuming the market trades about 252 days a year. None when there is too little
    variation to measure (e.g. no trades)."""
    out = {"sharpe": None, "sortino": None}
    if len(equity) < 3:
        return out
    r = equity.pct_change().dropna().to_numpy(dtype=float)
    if len(r) < 2:
        return out
    bar = timeframes.to_timedelta(timeframe)
    per_year = (pd.Timedelta(days=252) / bar) if bar <= pd.Timedelta(days=1) else (pd.Timedelta(days=365) / bar)
    scale = float(np.sqrt(per_year))
    mean = float(r.mean())
    sd = float(r.std(ddof=1))
    if sd > 0:
        out["sharpe"] = mean / sd * scale
    down = np.minimum(r, 0.0)
    dsd = float(np.sqrt((down ** 2).mean()))
    if dsd > 0:
        out["sortino"] = mean / dsd * scale
    return out


def longest_underwater_days(equity: pd.Series) -> float:
    """Longest stretch (days) the balance stayed below an earlier high."""
    if len(equity) < 2:
        return 0.0
    peak = equity.cummax()
    under = (equity < peak).to_numpy()
    idx = equity.index
    longest = pd.Timedelta(0)
    start = None
    for i, u in enumerate(under):
        if u and start is None:
            start = idx[i - 1] if i else idx[i]  # from the high just before the drop
        elif not u and start is not None:
            longest = max(longest, idx[i] - start)
            start = None
    if start is not None:
        longest = max(longest, idx[-1] - start)
    return float(longest / pd.Timedelta(days=1))


def monthly_returns(equity: pd.Series) -> list[dict]:
    """% change of the balance per calendar month, grouped by year, with a year-to-date column
    (the change over that year's months in the test)."""
    if len(equity) < 2:
        return []
    eq = equity.copy()
    if eq.index.tz is None:
        eq.index = eq.index.tz_localize("UTC")
    ends = eq.resample("ME").last().dropna()
    starts = ends.shift(1)
    starts.iloc[0] = float(eq.iloc[0])  # the first month starts from the opening balance
    pct = (ends / starts - 1) * 100
    out: dict[int, dict] = {}
    for stamp, v in pct.items():
        y = out.setdefault(int(stamp.year), {"year": int(stamp.year), "months": [None] * 12, "ytd": 0.0})
        y["months"][int(stamp.month) - 1] = float(v)
    for y in out.values():
        growth = 1.0
        for v in y["months"]:
            if v is not None:
                growth *= 1 + v / 100
        y["ytd"] = (growth - 1) * 100
    return [out[k] for k in sorted(out)]
