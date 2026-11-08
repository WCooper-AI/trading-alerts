"""Candle-by-candle trade simulation, shared by the backtester and live forward tests.

How trades are filled, modelled on a CFD/spread-bet broker:

* Prices in the data are mid prices. You buy at the ask (mid + half the spread)
  and sell at the bid (mid - half the spread). The spread is the candle's own
  spread when the data has one (e.g. OANDA), otherwise the strategy's setting.
* Signals are read on a candle's *close*; entries fill at the **next candle's open**.
* Stop and target levels are the price you'd actually get: a long's stop or
  target is hit when the *bid* reaches it, a short's when the *ask* does. A
  candle that opens beyond the stop fills at that open (gap risk).
* If a stop and target both fall inside one candle, finer candles (e.g. 1h inside
  4h) decide which came first. With no finer data the stop is assumed first.
* An exit rule (``exit.when``) closes at the next candle's open; ``max_bars``
  closes at the close of the Nth candle held (the entry candle counts as 1).
* Overnight funding is charged for each 22:00 UTC a position is held over, at
  ``costs.funding_pct`` a year on the position's value.

State is a plain dict so live runs can save it to disk between checks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from . import timeframes
from .risk import position_size
from .strategy import SkipTrade, Strategy

_CUT = 22 * 3600 * 10**9  # funding cut-off, 22:00 UTC, in ns
_DAY = 86400 * 10**9


def nights_between(a, b) -> int:
    """How many 22:00 UTC funding cut-offs fall in (a, b]."""
    av, bv = pd.Timestamp(a).value, pd.Timestamp(b).value
    return max(0, (bv - _CUT) // _DAY - (av - _CUT) // _DAY)


@dataclass
class Event:
    kind: str  # signal | opened | closed | skipped
    time: pd.Timestamp
    data: dict[str, Any] = field(default_factory=dict)


def new_state(strategy: Strategy) -> dict:
    return {
        "equity": strategy.risk.account_size,
        "position": None,
        "pending_entry": None,
        "pending_exit": False,
        "trades": 0,
        "half_spread": strategy.spread / 2,
    }


class Simulator:
    def __init__(self, strategy: Strategy, state: dict | None = None):
        self.s = strategy
        self.state = state if state is not None else new_state(strategy)
        self.bar = timeframes.to_timedelta(strategy.market.timeframe)

    # ------------------------------------------------------------------ helpers

    @property
    def position(self) -> dict | None:
        return self.state["position"]

    def _half_spread(self, spread: float | None) -> float:
        if spread is None or (isinstance(spread, float) and math.isnan(spread)) or not self.s.use_data_spread:
            return self.s.spread / 2
        return float(spread) / 2

    def _sizing_equity(self) -> float:
        return self.state["equity"] if self.s.risk.compound else self.s.risk.account_size

    def size_for(self, entry: float, stop: float) -> float:
        r = self.s.risk
        return position_size(
            self._sizing_equity(), r.risk_per_trade_pct, abs(entry - stop),
            r.point_value, r.size_step, r.min_size, r.max_size,
        )

    def unrealised(self, price: float, half_spread: float | None = None) -> float:
        """Open profit/loss if closed now at the bid (long) or ask (short)."""
        p = self.position
        if not p:
            return 0.0
        hs = self.state.get("half_spread", 0.0) if half_spread is None else half_spread
        exit_px = price - self.s.sign * hs
        return (exit_px - p["entry_price"]) * self.s.sign * p["size"] * self.s.risk.point_value

    def _accrue_funding(self, until, price: float) -> None:
        p = self.state["position"]
        if not p or not self.s.funding_pct:
            if p:
                p["funded_until"] = str(until)
            return
        nights = nights_between(p["funded_until"], until)
        if nights:
            cost = nights * price * p["size"] * self.s.risk.point_value * self.s.funding_pct / 100 / 365
            p["funding"] = p.get("funding", 0.0) + cost
            self.state["equity"] -= cost
        p["funded_until"] = str(until)

    def _close(self, time, exit_price: float, reason: str, exit_half_spread: float) -> Event:
        """Close at ``exit_price``, the actual bid (long) or ask (short) received.

        ``time`` is when the exit happened: a candle's start for exits at its open or
        inside it, its end for exits at its close (time limit, end of data)."""
        p = self.state["position"]
        s = self.s
        gross = (exit_price - p["entry_price"]) * s.sign * p["size"] * s.risk.point_value
        funding = p.get("funding", 0.0)
        pnl = gross - funding
        risk_amount = abs(p["entry_price"] - p["stop"]) * p["size"] * s.risk.point_value
        spread_cost = (p.get("entry_half_spread", 0.0) + exit_half_spread) * p["size"] * s.risk.point_value
        self.state["equity"] += gross  # funding was already taken night by night
        self.state["position"] = None
        self.state["pending_exit"] = False
        self.state["trades"] += 1
        trade = {
            "direction": s.direction,
            "signal_time": p["signal_time"],
            "entry_time": p["entry_time"],
            "entry_price": p["entry_price"],
            "exit_time": str(time),
            "exit_price": exit_price,
            "size": p["size"],
            "stop": p["stop"],
            "target": p["target"],
            "exit_reason": reason,
            "pnl": pnl,
            "r_multiple": pnl / risk_amount if risk_amount else 0.0,
            "bars_held": p["bars_held"],
            "equity_after": self.state["equity"],
            "funding": funding,
            "spread_cost": spread_cost,
        }
        return Event("closed", time, trade)

    def _levels(self, p: dict, o: float, h: float, l: float, hs: float, allow_gap: bool):
        """Which of stop/target this candle reaches, at the side of the book we'd exit on.

        Returns (reason, price) for a gap through a level at the open, otherwise
        (stop_hit, target_hit) booleans as ("hits", stop_hit, target_hit).
        """
        sign, stop, target = self.s.sign, p["stop"], p["target"]
        if sign > 0:  # long exits at the bid
            bo, bh, bl = o - hs, h - hs, l - hs
            if allow_gap and bo <= stop:
                return ("stop", bo)
            if allow_gap and target is not None and bo >= target:
                return ("target", bo)
            return ("hits", bl <= stop, target is not None and bh >= target)
        ao, ah, al = o + hs, h + hs, l + hs  # short exits at the ask
        if allow_gap and ao >= stop:
            return ("stop", ao)
        if allow_gap and target is not None and ao <= target:
            return ("target", ao)
        return ("hits", ah >= stop, target is not None and al <= target)

    def _resolve_exit(self, p, o, h, l, hs, opened_now, fine: Callable[[], Any] | None):
        res = self._levels(p, o, h, l, hs, allow_gap=not opened_now)
        if res[0] != "hits":
            return res
        _, stop_hit, target_hit = res
        if stop_hit and not target_hit:
            return ("stop", p["stop"])
        if target_hit and not stop_hit:
            return ("target", p["target"])
        if not stop_hit and not target_hit:
            return None
        # Both inside one candle: look at the finer candles to see which came first.
        rows = fine() if fine else None
        if rows is not None and len(rows):
            for k, r in enumerate(rows.itertuples(index=False)):
                sub = self._levels(p, r.open, r.high, r.low, hs, allow_gap=not (opened_now and k == 0))
                if sub[0] != "hits":
                    return sub
                if sub[1]:
                    return ("stop", p["stop"])  # stop first, or both in the same finer candle
                if sub[2]:
                    return ("target", p["target"])
        return ("stop", p["stop"])

    # ------------------------------------------------------------------ main step

    def step(
        self,
        time: pd.Timestamp,
        o: float, h: float, l: float, c: float,
        entry_signal: bool,
        exit_signal: bool,
        refs: dict[str, float | None],
        spread: float | None = None,
        fine: Callable[[], Any] | None = None,
    ) -> list[Event]:
        s, st, sign = self.s, self.state, self.s.sign
        hs = self._half_spread(spread)
        st["half_spread"] = hs
        events: list[Event] = []

        # 0. funding for nights since the last candle, up to this open
        if st["position"]:
            self._accrue_funding(time, o)

        # 1. exit rule from the previous candle -> out at this open
        if st["position"] and st["pending_exit"]:
            events.append(self._close(time, o - sign * hs, "exit_rule", hs))

        # 2. entry signal from the previous candle -> in at this open
        pend = st["pending_entry"]
        st["pending_entry"] = None
        if pend and not st["position"]:
            fill = o + sign * hs
            try:
                stop, target = s.plan(fill, pend["refs"])
                size = self.size_for(fill, stop)
                if size <= 0:
                    raise SkipTrade("position size rounds to zero at your risk settings")
            except SkipTrade as exc:
                events.append(Event("skipped", time, {"reason": str(exc)}))
            else:
                st["position"] = {
                    "signal_time": pend["signal_time"],
                    "entry_time": str(time),
                    "entry_price": fill,
                    "stop": stop,
                    "target": target,
                    "size": size,
                    "bars_held": 0,
                    "entry_half_spread": hs,
                    "funding": 0.0,
                    "funded_until": str(time),
                }
                events.append(Event("opened", time, {k: v for k, v in st["position"].items()
                                                     if k not in ("funded_until",)}))

        # 3. stop / target inside this candle
        p = st["position"]
        if p:
            opened_now = p["entry_time"] == str(time)
            hit = self._resolve_exit(p, o, h, l, hs, opened_now, fine)
            if hit:
                events.append(self._close(time, hit[1], hit[0], hs))

        # 4. time limit and exit rule
        p = st["position"]
        if p:
            p["bars_held"] += 1
            if s.max_bars and p["bars_held"] >= s.max_bars:
                self._accrue_funding(time + self.bar, c)
                events.append(self._close(time + self.bar, c - sign * hs, "max_bars", hs))
            elif exit_signal:
                st["pending_exit"] = True

        # 5. funding up to this candle's close for anything still open
        if st["position"]:
            self._accrue_funding(time + self.bar, c)

        # 6. new entry signal on this candle's close
        if entry_signal and not st["position"] and not st["pending_entry"]:
            info: dict[str, Any] = {"ref_price": c}
            try:
                stop, target = s.plan(c, refs)
                info.update(stop=stop, target=target, size=self.size_for(c, stop))
            except SkipTrade as exc:
                info.update(stop=None, target=None, size=0.0, warning=str(exc))
            st["pending_entry"] = {"signal_time": str(time), "refs": refs}
            events.append(Event("signal", time, info))

        return events

    def finish(self, time, price: float) -> list[Event]:
        """Close any open position at the last price (end of a backtest)."""
        if self.state["position"]:
            hs = self.state.get("half_spread", self.s.spread / 2)
            return [self._close(pd.Timestamp(time) + self.bar, price - self.s.sign * hs, "end_of_data", hs)]
        return []
