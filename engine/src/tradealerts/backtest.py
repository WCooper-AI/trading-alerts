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
                 notes: list[str] | None = None, start: pd.Timestamp | None = None) -> BacktestResult:
    """Simulate ``strategy`` over ``df``.

    With ``start``, candles before it are only used to warm up indicators; trading
    and all results begin at ``start``.
    """
    if df.empty:
        raise ValueError("No price data to backtest on")
    bar = timeframes.to_timedelta(strategy.market.timeframe)
    prepared = strategy.prepare(df)  # on the full history, so indicators are warmed up
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
    }
    if len(trades):
        pnl = trades["pnl"]
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        streak = longest = 0
        for v in pnl:
            streak = streak + 1 if v <= 0 else 0
            longest = max(longest, streak)
        m.update(
            win_rate_pct=len(wins) / len(pnl) * 100,
            avg_win=float(wins.mean()) if len(wins) else 0.0,
            avg_loss=float(losses.mean()) if len(losses) else 0.0,
            profit_factor=float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None,  # None: no losing trades
            expectancy=float(pnl.mean()),
            avg_r=float(trades["r_multiple"].mean()),
            avg_bars_held=float(trades["bars_held"].mean()),
            longest_losing_streak=int(longest),
            exit_reasons=trades["exit_reason"].value_counts().to_dict(),
        )
    return m
