"""Position sizing from a fixed fraction of the account at risk."""

from __future__ import annotations

import math


def position_size(
    equity: float,
    risk_pct: float,
    stop_distance: float,
    point_value: float = 1.0,
    size_step: float = 0.01,
    min_size: float = 0.0,
    max_size: float | None = None,
) -> float:
    """Size so that hitting the stop loses about ``risk_pct`` % of ``equity``.

    ``stop_distance`` is in price points; ``point_value`` is how much one unit of
    size gains or loses per point (e.g. 1.0 for a £1-per-point spread bet).
    Rounded *down* to ``size_step``. Returns 0 if below ``min_size``.
    """
    if equity <= 0 or risk_pct <= 0 or stop_distance <= 0 or point_value <= 0:
        return 0.0
    raw = equity * risk_pct / 100.0 / (stop_distance * point_value)
    if max_size is not None:
        raw = min(raw, max_size)
    if size_step and size_step > 0:
        steps = math.floor(raw / size_step + 1e-9)
        size = round(steps * size_step, 10)
    else:
        size = raw
    return size if size >= min_size and size > 0 else 0.0
