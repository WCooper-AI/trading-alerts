"""How a market has tended to move: by hour, by session, around news.

Everything here describes past prices. Nothing in it says what a market will do
next, and the app shows it that way.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Session windows in UK time. Hourly candles, so each window is whole hours.
SESSIONS = [
    {"key": "asia", "label": "Asia", "start": 0, "end": 7, "tz": "Europe/London",
     "about": "Tokyo, Hong Kong and Sydney are open; Europe and the US are asleep."},
    {"key": "london", "label": "London", "start": 8, "end": 13, "tz": "Europe/London",
     "about": "Europe's markets open. Usually busier for European indices and currencies."},
    {"key": "new_york", "label": "New York", "start": 14, "end": 21, "tz": "Europe/London",
     "about": "US markets open at 14:30 UK time and most US news lands around then."},
]


def _num(x, digits: int = 4):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


def _pct(part, whole) -> float | None:
    return _num(100.0 * part / whole, 1) if whole else None


def _local(df: pd.DataFrame, tz: str) -> pd.DataFrame:
    out = df.copy()
    out.index = out.index.tz_convert(tz)
    return out


def hour_grid(df: pd.DataFrame) -> dict:
    """Average size of an hourly candle (high to low, as % of price) for each weekday and hour."""
    rng = (df["high"] - df["low"]) / df["open"] * 100
    frame = pd.DataFrame({"day": df.index.day_of_week, "hour": df.index.hour, "move": rng})
    piv = frame.pivot_table(index="day", columns="hour", values="move", aggfunc="mean")
    counts = frame.pivot_table(index="day", columns="hour", values="move", aggfunc="count")
    per_day = {d: counts.loc[d].sum() for d in piv.index}
    most = max(per_day.values()) if per_day else 0
    days = [d for d in range(7) if d in per_day and per_day[d] >= max(8, 0.3 * most)]
    cells = []
    for d in days:
        row = []
        for hr in range(24):
            v = piv.loc[d].get(hr) if hr in piv.columns else None
            n = int(counts.loc[d].get(hr, 0)) if hr in counts.columns and not pd.isna(counts.loc[d].get(hr)) else 0
            row.append(_num(v) if n >= 3 else None)
        cells.append(row)
    by_hour = frame.groupby("hour")["move"].mean()
    hours = [_num(by_hour.get(hr)) if hr in by_hour.index else None for hr in range(24)]
    valid = [(v, i) for i, v in enumerate(hours) if v is not None]
    return {
        "days": [DAYS[d] for d in days], "cells": cells, "by_hour": hours,
        "busiest": max(valid)[1] if valid else None, "quietest": min(valid)[1] if valid else None,
    }


def day_high_low(df: pd.DataFrame) -> dict:
    """For each day, which hour made the day's high and which made the low."""
    frame = pd.DataFrame({"date": df.index.date, "hour": df.index.hour, "high": df["high"].values, "low": df["low"].values})
    sizes = frame.groupby("date").size()
    frame = frame[frame["date"].isin(sizes[sizes >= 12].index)]  # skip half days
    if frame.empty:
        return {"days": 0, "high": [0] * 24, "low": [0] * 24}
    hi = frame.loc[frame.groupby("date")["high"].idxmax(), "hour"].value_counts()
    lo = frame.loc[frame.groupby("date")["low"].idxmin(), "hour"].value_counts()
    n = frame["date"].nunique()
    return {"days": int(n),
            "high": [_pct(int(hi.get(h, 0)), n) for h in range(24)],
            "low": [_pct(int(lo.get(h, 0)), n) for h in range(24)]}


def _ranges(bars: pd.DataFrame) -> pd.Series:
    return (bars["high"] - bars["low"]).dropna()


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = df.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return out


def typical_ranges(df: pd.DataFrame, now: pd.Timestamp) -> dict:
    """Usual day and week range (in price), and how much of that today and this week have used."""
    daily = _resample(df, "1D")
    counts = df.resample("1D").size()
    daily = daily[counts.reindex(daily.index).fillna(0) >= 12]
    weekly = _resample(df, "W-FRI")
    out = {}
    week_start = (now - pd.Timedelta(days=(now.day_of_week - 5) % 7)).normalize()  # the Saturday just gone
    for key, bars, cur_start in (("day", daily, now.normalize()), ("week", weekly, week_start)):
        done = bars[bars.index < cur_start]
        r = _ranges(done)
        if r.empty:
            continue
        pct = (r / done["open"].reindex(r.index) * 100)
        cur = df[df.index >= cur_start]
        latest = True
        if not len(cur):  # weekend or holiday: show the last one that traded instead
            cur, latest = df[df.index >= done.index[-1]] if key == "day" else df[df.index > done.index[-2]] if len(done) > 1 else df, False
            r, done = r.iloc[:-1], done.iloc[:-1]
            if r.empty:
                continue
        cur_range = float(cur["high"].max() - cur["low"].min()) if len(cur) else None
        med = float(r.median())
        out[key] = {
            "median": _num(med), "low": _num(r.quantile(0.25)), "high": _num(r.quantile(0.75)),
            "big": _num(r.quantile(0.9)), "median_pct": _num(pct.median(), 3), "count": int(len(r)),
            "current": _num(cur_range), "current_is_now": latest,
            "current_date": cur.index[0].isoformat() if len(cur) else None, "current_share": _pct(cur_range, med) if cur_range is not None else None,
            "current_rank": _pct(int((r < cur_range).sum()), len(r)) if cur_range is not None else None,
            "history": [[i.isoformat(), _num(v)] for i, v in r.tail(60 if key == "day" else 52).items()],
        }
    return out


def gaps(df: pd.DataFrame, typical_day: float | None) -> dict:
    """Jumps between one candle's close and the next one's open across a break in trading."""
    idx = df.index
    if len(df) < 3:
        return {"kinds": []}
    step = pd.Series(idx[1:] - idx[:-1], index=idx[1:])
    jump = (df["open"].iloc[1:].values - df["close"].iloc[:-1].values)
    frame = pd.DataFrame({"step": step.values, "jump": jump, "prev_close": df["close"].iloc[:-1].values}, index=idx[1:])
    frame = frame[frame["step"] > pd.Timedelta(hours=1)]
    out = []
    for key, label, mask in (("weekend", "After the weekend", frame["step"] >= pd.Timedelta(hours=36)),
                             ("daily", "After the daily break", frame["step"] < pd.Timedelta(hours=36))):
        part = frame[mask]
        if len(part) < 3:
            continue
        size = part["jump"].abs()
        # "filled": price came back to the old close within the next 24 candles
        filled = 0
        for t, row in part.iterrows():
            after = df.loc[t:].iloc[:24]
            if row["jump"] > 0 and (after["low"] <= row["prev_close"]).any():
                filled += 1
            elif row["jump"] < 0 and (after["high"] >= row["prev_close"]).any():
                filled += 1
            elif row["jump"] == 0:
                filled += 1
        noticeable = size > (0.1 * typical_day if typical_day else size.median())
        out.append({
            "key": key, "label": label, "count": int(len(part)),
            "noticeable": _pct(int(noticeable.sum()), len(part)),
            "median": _num(size.median()), "biggest": _num(size.max()),
            "biggest_when": part["jump"].abs().idxmax().isoformat(),
            "up": _pct(int((part["jump"] > 0).sum()), len(part)),
            "filled": _pct(filled, len(part)),
        })
    return {"kinds": out, "threshold": _num(0.1 * typical_day) if typical_day else None}


def news_moves(df: pd.DataFrame, events: pd.DataFrame, min_count: int = 3) -> list[dict]:
    """How big the candle containing each kind of news was, against that same hour on other days."""
    if events is None or events.empty or df.empty:
        return []
    rng = (df["high"] - df["low"])
    utc_idx = df.index.tz_convert("UTC")
    hour_of = pd.Series(df.index.hour, index=df.index)
    normal_by_hour = rng.groupby(hour_of).median()
    pos = pd.Series(np.arange(len(df)), index=utc_idx)
    rows = {}
    for _, ev in events.iterrows():
        t = pd.Timestamp(ev["time"]).floor("h")
        if t not in pos.index:
            continue
        i = int(pos[t]) if not isinstance(pos[t], pd.Series) else int(pos[t].iloc[0])
        bar_range = float(rng.iloc[i])
        normal = float(normal_by_hour.get(df.index[i].hour, np.nan))
        if not normal or np.isnan(normal):
            continue
        after = df.iloc[i:i + 4]
        key = (ev["currency"], ev["title"])
        r = rows.setdefault(key, {"ratios": [], "ranges": [], "after": [], "last": None})
        r["ratios"].append(bar_range / normal)
        r["ranges"].append(bar_range)
        r["after"].append(float(after["high"].max() - after["low"].min()))
        r["last"] = pd.Timestamp(ev["time"]).isoformat()
    out = []
    for (cur, title), r in rows.items():
        if len(r["ratios"]) < min_count:
            continue
        out.append({"title": title, "currency": cur, "count": len(r["ratios"]),
                    "times_normal": _num(np.median(r["ratios"]), 2),
                    "median_range": _num(np.median(r["ranges"])), "biggest": _num(max(r["ranges"])),
                    "four_hours": _num(np.median(r["after"])), "last": r["last"]})
    return sorted(out, key=lambda x: -(x["times_normal"] or 0))[:12]


def spread_by_hour(df: pd.DataFrame) -> dict | None:
    if "spread" not in df.columns or df["spread"].dropna().empty:
        return None
    s = df["spread"].dropna()
    s = s[s > 0]
    if s.empty:
        return None
    by = s.groupby(s.index.hour).median()
    wide = s.groupby(s.index.hour).quantile(0.9)
    hours = [_num(by.get(h)) if h in by.index else None for h in range(24)]
    valid = [(v, i) for i, v in enumerate(hours) if v is not None]
    return {"median": hours, "wide": [_num(wide.get(h)) if h in wide.index else None for h in range(24)],
            "overall": _num(s.median()), "widest": max(valid)[1] if valid else None,
            "tightest": min(valid)[1] if valid else None}


def sessions(df: pd.DataFrame) -> list[dict]:
    """Each session's usual range, its share of the day's range, and whether its
    first hour's direction held to the session's end."""
    frame = df.copy()
    frame["date"] = frame.index.date
    day_range = frame.groupby("date").apply(lambda g: g["high"].max() - g["low"].min() if len(g) >= 12 else np.nan)
    out = []
    for s in SESSIONS:
        part = frame[(frame.index.hour >= s["start"]) & (frame.index.hour <= s["end"])]
        groups = [g for _, g in part.groupby("date") if len(g) >= (s["end"] - s["start"])]
        if len(groups) < 10:
            continue
        ranges, shares, same, moved, first_moves = [], [], 0, 0, []
        for g in groups:
            r = float(g["high"].max() - g["low"].min())
            ranges.append(r)
            dr = day_range.get(g["date"].iloc[0])
            if dr and not np.isnan(dr):
                shares.append(r / dr)
            first = float(g["close"].iloc[0] - g["open"].iloc[0])
            rest = float(g["close"].iloc[-1] - g["close"].iloc[0])
            if first == 0 or rest == 0:
                continue
            moved += 1
            same += int((first > 0) == (rest > 0))
            first_moves.append(abs(first))
        out.append({
            "key": s["key"], "label": s["label"], "about": s["about"],
            "hours": f"{s['start']:02d}:00–{s['end'] + 1:02d}:00", "days": len(groups),
            "median_range": _num(np.median(ranges)), "share_of_day": _num(np.median(shares) * 100, 0) if shares else None,
            "carried_on": _pct(same, moved), "first_hour": _num(np.median(first_moves)) if first_moves else None,
        })
    return out


def compute(df: pd.DataFrame, events: pd.DataFrame | None = None, tz: str = "Europe/London",
            now: pd.Timestamp | None = None) -> dict:
    """All the stats for one market. ``df`` is hourly candles in UTC."""
    if df is None or df.empty:
        raise ValueError("No prices to work from")
    df = df[["open", "high", "low", "close"] + (["spread"] if "spread" in df.columns else [])].dropna(subset=["open", "close"])
    try:
        local = _local(df, tz)
    except Exception:
        tz = "UTC"
        local = _local(df, tz)
    now_local = (now or pd.Timestamp.now(tz="UTC")).tz_convert(tz)
    ranges = typical_ranges(local, now_local)
    sess_df = _local(df, "Europe/London")
    return {
        "tz": tz, "from": df.index[0].isoformat(), "to": df.index[-1].isoformat(), "candles": int(len(df)),
        "last_price": _num(df["close"].iloc[-1], 6),
        "grid": hour_grid(local),
        "high_low": day_high_low(local),
        "ranges": ranges,
        "gaps": gaps(df, (ranges.get("day") or {}).get("median")),
        "news": news_moves(local, events) if events is not None else [],
        "spread": spread_by_hour(local),
        "sessions": sessions(sess_df),
    }
