"""A small, safe rule language for YAML strategies (no eval, no code execution).

Conditions
----------
    {left: <operand>, op: <op>, right: <operand>}
        op is one of  <  <=  >  >=  ==  crosses_above  crosses_below
    {all: [<condition>, ...]}      every condition true
    {any: [<condition>, ...]}      at least one true
    {not: <condition>}
    {time_between: ["10:00", "15:30"], tz: "America/New_York"}
        bar *start* time, local to tz, start inclusive / end exclusive
    {weekdays: [mon, tue, wed, thu, fri]}

Operands
--------
    close | open | high | low | volume          a price field
    1.5                                         a number
    {field: close, shift: 1}                    previous bar's close
    {indicator: rsi, period: 14}                any indicator plus its parameters
    ... and on any dict operand:  shift: n  (n bars ago),  scale: x (multiply),  add: y
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd

from . import indicators

OPS = ("<", "<=", ">", ">=", "==", "crosses_above", "crosses_below")
_OPERAND_MODIFIERS = ("shift", "scale", "add")
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class RuleError(ValueError):
    def __init__(self, message: str, path: str = ""):
        super().__init__(f"{path}: {message}" if path else message)


Series = pd.Series
Evaluator = Callable[["Context"], Series]


class Context:
    """Holds the price data and caches indicator results for one evaluation."""

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self._cache: dict[tuple, Series] = {}

    def indicator(self, name: str, params: dict) -> Series:
        key = (name, tuple(sorted(params.items())))
        if key not in self._cache:
            self._cache[key] = indicators.compute(self.df, name, **params)
        return self._cache[key]


# --------------------------------------------------------------------------- operands


def compile_operand(spec: Any, path: str = "operand") -> Callable[[Context], Series | float]:
    if spec is None or spec == "":
        raise RuleError("fill this in: choose a price, a number or an indicator", path)
    if isinstance(spec, bool):
        raise RuleError("true/false isn't a valid operand", path)
    if isinstance(spec, (int, float)):
        value = float(spec)
        return lambda ctx: value
    if isinstance(spec, str):
        if spec not in indicators.PRICE_FIELDS:
            raise RuleError(
                f"unknown field {spec!r}. Use one of {', '.join(indicators.PRICE_FIELDS)} "
                f"or {{indicator: ...}}",
                path,
            )
        return lambda ctx: ctx.df[spec]
    if not isinstance(spec, dict):
        raise RuleError(f"expected a field name, number or mapping, got {spec!r}", path)

    spec = dict(spec)
    shift = spec.pop("shift", 0)
    scale = spec.pop("scale", 1.0)
    add = spec.pop("add", 0.0)
    if not isinstance(shift, int) or shift < 0:
        raise RuleError("shift must be a whole number of bars, 0 or more", path)
    for name, v in (("scale", scale), ("add", add)):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise RuleError(f"{name} must be a number", path)

    if "field" in spec:
        field = spec.pop("field")
        if spec:
            raise RuleError(f"unexpected keys with field: {', '.join(spec)}", path)
        if field not in indicators.PRICE_FIELDS:
            raise RuleError(f"unknown field {field!r}", path)
        base: Callable[[Context], Series] = lambda ctx: ctx.df[field]
    elif "indicator" in spec:
        name = spec.pop("indicator")
        params = spec
        if not name:
            raise RuleError("choose an indicator", path)
        if name not in indicators.REGISTRY:
            raise RuleError(
                f"unknown indicator {name!r}. Available: {', '.join(sorted(indicators.REGISTRY))}",
                path,
            )
        allowed = indicators.params_for(name)
        unknown = set(params) - set(allowed)
        if unknown:
            raise RuleError(
                f"indicator {name!r} doesn't take {', '.join(sorted(unknown))}; "
                f"its parameters are {', '.join(allowed) or 'none'}",
                path,
            )
        blank = [k for k, v in params.items() if v is None or v == ""]
        if blank:
            raise RuleError(f"fill in {' and '.join(blank)} for {name}", path)
        base = lambda ctx: ctx.indicator(name, params)
    else:
        raise RuleError("a mapping operand needs 'indicator' or 'field'", path)

    def evaluate(ctx: Context) -> Series:
        s = base(ctx)
        if shift:
            s = s.shift(shift)
        if scale != 1.0:
            s = s * scale
        if add:
            s = s + add
        return s

    return evaluate


def _as_series(value, index) -> Series:
    if isinstance(value, pd.Series):
        return value
    return pd.Series(float(value), index=index)


# --------------------------------------------------------------------------- conditions


def _parse_hhmm(text: str, path: str) -> int:
    try:
        h, m = (int(x) for x in str(text).split(":"))
        if not (0 <= h <= 23 and 0 <= m < 60) and (h, m) != (24, 0):
            raise ValueError
    except ValueError:
        raise RuleError(f"times must look like '09:30', got {text!r}", path) from None
    return h * 60 + m


def compile_condition(spec: Any, path: str = "condition") -> Evaluator:
    if not isinstance(spec, dict):
        raise RuleError(f"expected a mapping, got {spec!r}", path)

    keys = set(spec)
    if keys == {"all"} or keys == {"any"}:
        kind = "all" if "all" in spec else "any"
        items = spec[kind]
        if not isinstance(items, list) or not items:
            raise RuleError(f"'{kind}' needs a non-empty list of conditions", path)
        parts = [compile_condition(c, f"{path}.{kind}[{i}]") for i, c in enumerate(items)]

        def combine(ctx: Context) -> Series:
            results = [_tri(p(ctx)) for p in parts]
            out = results[0]
            for r in results[1:]:
                out = (out & r) if kind == "all" else (out | r)
            return out

        return combine

    if keys == {"not"}:
        inner = compile_condition(spec["not"], f"{path}.not")
        # Unknown stays unknown: "not (RSI > 70)" isn't true while RSI is still warming up.
        return lambda ctx: ~_tri(inner(ctx))

    if "time_between" in keys:
        extra = keys - {"time_between", "tz"}
        if extra:
            raise RuleError(f"unexpected keys: {', '.join(sorted(extra))}", path)
        window = spec["time_between"]
        if isinstance(window, list) and any(t in (None, "") for t in window):
            raise RuleError("fill in both times of the time window", path)
        if not isinstance(window, list) or len(window) != 2:
            raise RuleError("time_between needs two times, e.g. ['10:00', '15:30']", path)
        start, end = (_parse_hhmm(t, path) for t in window)
        tz = spec.get("tz", "UTC")
        if tz in (None, ""):
            raise RuleError("choose a time zone for the time window", path)
        _check_tz(tz, path)

        def in_window(ctx: Context) -> Series:
            local = ctx.df.index.tz_convert(tz)
            mod = np.asarray(local.hour * 60 + local.minute)
            ok = (mod >= start) & (mod < end) if start <= end else (mod >= start) | (mod < end)
            return pd.Series(ok, index=ctx.df.index)

        return in_window

    if "weekdays" in keys:
        if keys != {"weekdays"} and keys != {"weekdays", "tz"}:
            raise RuleError("weekdays only takes an optional tz", path)
        days = spec["weekdays"]
        if isinstance(days, list) and not days:
            raise RuleError("pick at least one day", path)
        try:
            wanted = {_WEEKDAYS[str(d).lower()[:3]] for d in days}
        except (KeyError, TypeError):
            raise RuleError("weekdays must be a list like [mon, tue, wed]", path) from None
        tz = spec.get("tz", "UTC") or "UTC"
        _check_tz(tz, path)
        return lambda ctx: pd.Series(
            np.isin(ctx.df.index.tz_convert(tz).day_of_week, list(wanted)), index=ctx.df.index
        )

    if keys == {"avoid_news"}:
        spec_n = spec["avoid_news"]
        if not isinstance(spec_n, dict):
            raise RuleError("avoid_news needs settings like {before: 30, after: 30, currencies: [USD]}", path)
        extra = set(spec_n) - {"before", "after", "currencies", "impact"}
        if extra:
            raise RuleError(f"avoid_news: unexpected keys {', '.join(sorted(extra))}", path)
        before, after = spec_n.get("before"), spec_n.get("after")
        for name, v in (("minutes before", before), ("minutes after", after)):
            if v is None or v == "":
                raise RuleError(f"fill in the {name} news to stay out", path)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 1440:
                raise RuleError(f"{name} news must be between 0 and 1440", path)
        currencies = spec_n.get("currencies") or []
        if not isinstance(currencies, list) or not currencies:
            raise RuleError("choose which currencies' news to avoid, e.g. [USD]", path)
        impact = str(spec_n.get("impact", "high")).lower()
        if impact not in ("high", "medium", "low"):
            raise RuleError("impact must be high, medium or low", path)

        def clear_of_news(ctx: Context) -> Series:
            from . import news
            from .data import infer_bar_length
            bar = infer_bar_length(ctx.df) or pd.Timedelta(hours=1)
            return ~news.blocked(ctx.df.index, bar, float(before), float(after), [str(c) for c in currencies], impact)

        return clear_of_news

    if keys == {"left", "op", "right"}:
        op = spec["op"]
        if op in (None, ""):
            raise RuleError("choose how to compare the two sides", path)
        if op not in OPS:
            raise RuleError(f"unknown op {op!r}. Use one of {', '.join(OPS)}", path)
        left = compile_operand(spec["left"], f"{path}.left")
        right = compile_operand(spec["right"], f"{path}.right")

        def compare(ctx: Context) -> Series:
            idx = ctx.df.index
            a, b = _as_series(left(ctx), idx), _as_series(right(ctx), idx)
            valid = a.notna() & b.notna()
            if op == "<":
                out = a < b
            elif op == "<=":
                out = a <= b
            elif op == ">":
                out = a > b
            elif op == ">=":
                out = a >= b
            elif op == "==":
                out = a == b
            else:
                pa, pb = a.shift(1), b.shift(1)
                valid = valid & pa.notna() & pb.notna()
                out = (a > b) & (pa <= pb) if op == "crosses_above" else (a < b) & (pa >= pb)
            # Where either side isn't known yet (indicator warming up), the answer is unknown, not false.
            return out.astype("boolean").mask(~valid, pd.NA)

        return compare

    raise RuleError(
        "a condition needs left/op/right, all, any, not, time_between, weekdays or avoid_news "
        f"(got keys: {', '.join(sorted(keys))})",
        path,
    )


def _tri(s: Series) -> Series:
    """As a three-valued (true / false / unknown) series, so groups combine unknowns correctly."""
    return s if str(s.dtype) == "boolean" else s.astype("boolean")


def _check_tz(tz, path: str) -> None:
    import zoneinfo

    try:
        zoneinfo.ZoneInfo(str(tz))
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise RuleError(f"unknown time zone {tz!r}; use a name like Europe/London or America/New_York", path) from None


def max_lookback(spec: Any) -> int:
    """Candles of history a condition (or operand) needs before every part of it is known."""
    if isinstance(spec, list):
        return max((max_lookback(x) for x in spec), default=0)
    if not isinstance(spec, dict):
        return 0
    if "indicator" in spec or "field" in spec:
        params = {k: v for k, v in spec.items() if k not in ("indicator", "field", *_OPERAND_MODIFIERS)}
        base = indicators.lookback(spec["indicator"], params) if "indicator" in spec else 1
        return base + int(spec.get("shift", 0) or 0)
    if {"left", "op", "right"} <= set(spec):
        crosses = 1 if spec["op"] in ("crosses_above", "crosses_below") else 0
        return max(max_lookback(spec["left"]), max_lookback(spec["right"]), 1) + crosses
    return max((max_lookback(v) for k, v in spec.items() if k in ("all", "any", "not")), default=0)


def evaluate(spec_or_evaluator, df: pd.DataFrame, ctx: Context | None = None) -> Series:
    """True where the condition holds. Unknown (e.g. indicators still warming up) counts as false."""
    fn = spec_or_evaluator if callable(spec_or_evaluator) else compile_condition(spec_or_evaluator)
    ctx = ctx or Context(df)
    out = _tri(fn(ctx)).reindex(df.index)
    return out.fillna(False).astype(bool)
