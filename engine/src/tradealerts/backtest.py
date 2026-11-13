"""Run a strategy over historical candles and measure the results."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import timeframes
from .data import find_jumps
from .markethours import trades_24_7
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
    # With a train/test split: trades, equity and metrics above are the in-sample part,
    # these the out-of-sample part (a fresh account from ``split_date``).
    oos_trades: pd.DataFrame | None = None
    oos_equity: pd.Series | None = None
    oos_metrics: dict | None = None
    split_date: pd.Timestamp | None = None


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


MIN_SPLIT_PCT, MAX_SPLIT_PCT = 50, 90


def split_index(first: int, n: int, split_pct: float) -> int:
    """Index of the first out-of-sample candle when the test period [first, n) is split."""
    if isinstance(split_pct, bool) or not MIN_SPLIT_PCT <= split_pct <= MAX_SPLIT_PCT:
        raise ValueError(f"The in-sample part must be {MIN_SPLIT_PCT}-{MAX_SPLIT_PCT}% of the period")
    cut = first + int(round((n - first) * split_pct / 100))
    if cut - first < 2 or n - cut < 2:
        raise ValueError("The test period is too short to split into in-sample and out-of-sample parts")
    return cut


def _simulate(strategy: Strategy, df: pd.DataFrame, prepared, a: int, b: int, get_fine, spreads):
    """One fresh account trading candles a..b-1; open trades are closed at the last one."""
    sim = Simulator(strategy)
    trades, skipped = [], []
    equity = np.empty(b - a)
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    in_market = 0
    for i in range(a, b):
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
        equity[i - a] = sim.state["equity"] + sim.unrealised(c[i])
    for ev in sim.finish(df.index[b - 1], c[b - 1]):
        trades.append(ev.data)
        equity[-1] = sim.state["equity"]
    eq = pd.Series(equity, index=df.index[a:b], name="equity")
    return pd.DataFrame(trades), skipped, eq, in_market


def run_backtest(strategy: Strategy, df: pd.DataFrame, fine: pd.DataFrame | None = None,
                 notes: list[str] | None = None, start: pd.Timestamp | None = None,
                 ctx=None, split_pct: float | None = None) -> BacktestResult:
    """Simulate ``strategy`` over ``df``.

    With ``start``, candles before it are only used to warm up indicators; trading
    and all results begin at ``start``. With ``split_pct`` (50-90), the test period is
    split in two: the first ``split_pct`` percent is the in-sample part (the result's
    trades, equity and metrics) and the rest is traded again from a fresh account as the
    out-of-sample part. Indicators are worked out once over the whole history, so the
    out-of-sample part starts with them warmed up and nothing from after a candle is used.
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
    cut = len(df) if split_pct is None else split_index(first, len(df), split_pct)
    get_fine = fine_lookup(fine, bar)
    spreads = spreads_for(strategy, df)
    trades_df, skipped, eq, in_market = _simulate(strategy, df, prepared, first, cut, get_fine, spreads)
    period = df.iloc[first:cut]
    metrics = compute_metrics(strategy, trades_df, eq, period, in_market)
    metrics["warmup_candles"] = first
    metrics["used_data_spread"] = spreads is not None
    metrics["used_finer_candles"] = fine is not None and not fine.empty
    split = {}
    if split_pct is not None:
        oos_trades, oos_skipped, oos_eq, oos_in = _simulate(strategy, df, prepared, cut, len(df), get_fine, spreads)
        skipped += oos_skipped
        oos_metrics = compute_metrics(strategy, oos_trades, oos_eq, df.iloc[cut:], oos_in)
        split = {"oos_trades": oos_trades, "oos_equity": oos_eq, "oos_metrics": oos_metrics,
                 "split_date": df.index[cut]}
        metrics["split_pct"] = float(split_pct)
        oos_metrics["split_pct"] = float(split_pct)

    all_notes = list(notes or [])
    need = strategy.lookback_candles
    if start is not None and first < need:
        all_notes.append(
            f"Your rules need {need} candles of history before they can be checked, but the price data "
            f"only has {first} before the test period, so trades may be missing near the start."
        )
    jumps = find_jumps(df.iloc[first:], strategy.market.timeframe)
    if len(jumps):
        worst = jumps["gap"].idxmax()
        all_notes.append(
            f"The price data has {len(jumps)} unusual jump{'s' if len(jumps) != 1 else ''} between "
            f"candles (largest on {worst:%d %b %Y}). These are often futures contract rolls or bad "
            "data, so trades around those dates may not be realistic."
        )
    metrics["data_jumps"] = int(len(jumps))
    return BacktestResult(strategy, trades_df, eq, metrics, skipped, all_notes, **split)


def compute_metrics(strategy: Strategy, trades: pd.DataFrame, equity: pd.Series, df: pd.DataFrame, bars_in_market: int) -> dict:
    start_eq = strategy.risk.account_size
    peak = equity.cummax().clip(lower=start_eq)
    dd = (equity / peak - 1) * 100
    m = {
        "market": strategy.market.label,
        "timeframe": strategy.market.timeframe,
        "direction": strategy.direction,
        "start": str(df.index[0]),
        "end": str(df.index[-1]),
        "bars": len(df),
        "repaired_bars": int(df.attrs.get("repaired_bars", 0) or 0),
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
        "total_slippage": float(trades["slippage_cost"].sum()) if len(trades) and "slippage_cost" in trades else 0.0,
        "sample_warning": sample_warning(len(trades)),
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
    m.update(risk_ratios(equity, strategy.market.timeframe, symbol=strategy.market.symbol))
    m["sharpe_ci"] = sharpe_ci(trades, equity, strategy.market.timeframe, m["sharpe"], symbol=strategy.market.symbol)
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


def bars_per_year(index: pd.Index, timeframe: str, symbol: str = "") -> float:
    """Candles in a year, for annualising. Daily and longer: 252 trading days (365 for 24/7
    markets or weekly and up). Intraday: trading days times the candles actually seen per
    session day, capped at a full 24 hours' worth, so a market open 6.5 h a day isn't counted
    as trading 24 h. 24/7 markets (crypto) use 365 days."""
    bar = timeframes.to_timedelta(timeframe)
    if bar > pd.Timedelta(days=1):
        return pd.Timedelta(days=365) / bar
    is_24_7 = bool(symbol and trades_24_7(symbol))
    trading_days = 365 if is_24_7 else 252
    full = pd.Timedelta(days=1) / bar
    if bar < pd.Timedelta(days=1) and isinstance(index, pd.DatetimeIndex) and len(index) >= 2:
        first, last = index[0].date(), index[-1].date()
        if is_24_7:
            days = max(1, (last - first).days + 1)
        else:
            days = int(np.busday_count(first, last + pd.Timedelta(days=1)))
        if days > 0:
            full = min(full, len(index) / days)
    return trading_days * full


def risk_ratios(equity: pd.Series, timeframe: str, symbol: str = "") -> dict:
    """Sharpe and Sortino ratios from candle-to-candle changes in the balance, annualised
    according to the market's trading schedule (see ``bars_per_year``). None when
    there is too little variation to measure (e.g. no trades)."""
    out = {"sharpe": None, "sortino": None}
    if len(equity) < 3:
        return out
    r = equity.pct_change().dropna().to_numpy(dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return out
    scale = float(np.sqrt(bars_per_year(equity.index, timeframe, symbol=symbol)))
    mean = float(r.mean())
    sd = float(r.std(ddof=1))
    if sd > 0:
        out["sharpe"] = mean / sd * scale
    down = np.minimum(r, 0.0)
    dsd = float(np.sqrt((down ** 2).mean()))
    if dsd > 0:
        out["sortino"] = mean / dsd * scale
    return out


MIN_TRADES_FOR_CI = 10


def sample_warning(trades: int) -> str | None:
    """A plain warning when there are too few trades for the results to mean much."""
    if trades < 10:
        return "Critically low trade sample (< 10). Performance metrics are statistically meaningless."
    if trades < 30:
        return "Small trade sample (< 30). High variance; results cannot reliably prove an edge."
    return None


def t_crit_95(df: int) -> float:
    """Two-sided 95% Student's t critical value (Cornish-Fisher expansion, within 0.001 for df >= 9)."""
    z, v = 1.959963984540054, float(df)
    return (z + (z**3 + z) / (4 * v) + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * v**2)
            + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * v**3))


def sharpe_ci(trades: pd.DataFrame, equity: pd.Series, timeframe: str, sharpe: float | None, symbol: str = "") -> list[float] | None:
    """95% range for the headline Sharpe ratio, driven by the number of trades.

    The headline ratio is measured candle by candle, but its uncertainty comes from how few
    independent trades produced those candles. So: a Student's t range on the mean trade
    return (each trade's profit as a fraction of the balance before it, n - 1 degrees of
    freedom), carried into the candle-based ratio. The trades' summed return spread over
    N candles moves the mean candle return by n * d / N for a shift d in the mean trade,
    giving half-width t * sd_trade * sqrt(n) / (N * sd_candle) * sqrt(candles a year).
    None under 10 trades, or when there's no Sharpe ratio to put a range around."""
    n = len(trades)
    if sharpe is None or n < MIN_TRADES_FOR_CI or "equity_after" not in trades:
        return None
    before = trades["equity_after"] - trades["pnl"]
    tr = (trades["pnl"] / before.where(before > 0)).dropna().to_numpy(dtype=float)
    r = equity.pct_change().dropna().to_numpy(dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return None
    if len(tr) < MIN_TRADES_FOR_CI or len(r) < 2:
        return None
    sd_t, sd_c = float(tr.std(ddof=1)), float(r.std(ddof=1))
    if not (sd_t > 0 and sd_c > 0):
        return None
    scale = float(np.sqrt(bars_per_year(equity.index, timeframe, symbol=symbol)))
    half = t_crit_95(len(tr) - 1) * sd_t * np.sqrt(len(tr)) / (len(r) * sd_c) * scale
    return [float(sharpe - half), float(sharpe + half)]


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
