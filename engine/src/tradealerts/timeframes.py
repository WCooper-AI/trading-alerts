"""Timeframe strings like "15m", "1h", "4h", "1d"."""

from __future__ import annotations

import re

import pandas as pd

_TF_RE = re.compile(r"^\s*(\d+)\s*(m|h|d)\s*$", re.IGNORECASE)
_UNIT_RULE = {"m": "min", "h": "h", "d": "D"}


class TimeframeError(ValueError):
    pass


def _parse(tf: str) -> tuple[int, str]:
    m = _TF_RE.match(str(tf))
    if not m:
        raise TimeframeError(
            f"Invalid timeframe {tf!r}. Use a number plus m, h or d, e.g. 15m, 1h, 4h, 1d."
        )
    n, unit = int(m.group(1)), m.group(2).lower()
    if n <= 0:
        raise TimeframeError(f"Timeframe must be positive: {tf!r}")
    return n, unit


def to_timedelta(tf: str) -> pd.Timedelta:
    n, unit = _parse(tf)
    return pd.Timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})


def to_pandas_rule(tf: str) -> str:
    n, unit = _parse(tf)
    return f"{n}{_UNIT_RULE[unit]}"


def minutes(tf: str) -> int:
    return int(to_timedelta(tf).total_seconds() // 60)
