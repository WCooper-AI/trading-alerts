"""Helpers shared by the web interface's routes: JSON cleaning, errors, settings mapping."""

from __future__ import annotations

import copy
import math
import re
from typing import Any

import pandas as pd
from fastapi import HTTPException, Request

from ..workspace import Workspace

CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}

MARKET_PRESETS = [
    {"label": "US 500", "symbol": "ES=F", "note": "S&P 500 futures, nearly 24h like most CFDs"},
    {"label": "US 500 (cash hours)", "symbol": "^GSPC", "note": "S&P 500 index, US session only"},
    {"label": "US 100", "symbol": "NQ=F", "note": "Nasdaq 100 futures"},
    {"label": "Wall Street 30", "symbol": "YM=F", "note": "Dow futures"},
    {"label": "Germany 40", "symbol": "^GDAXI", "note": "DAX index"},
    {"label": "UK 100", "symbol": "^FTSE", "note": "FTSE 100 index"},
    {"label": "Netherlands 25", "symbol": "^AEX", "note": "AEX index"},
    {"label": "Gold", "symbol": "GC=F", "note": "Gold futures"},
    {"label": "Silver", "symbol": "SI=F", "note": "Silver futures"},
    {"label": "Oil (WTI)", "symbol": "CL=F", "note": "Crude oil futures"},
    {"label": "EUR/USD", "symbol": "EURUSD=X", "note": "Forex"},
    {"label": "GBP/USD", "symbol": "GBPUSD=X", "note": "Forex"},
    {"label": "Bitcoin", "symbol": "BTC-USD", "note": "Crypto, 24/7"},
]
TIMEFRAMES = ["5m", "15m", "30m", "1h", "2h", "4h", "1d"]
TIMEZONES = ["UTC", "Europe/London", "Europe/Berlin", "America/New_York", "America/Chicago",
             "Asia/Tokyo", "Asia/Hong_Kong", "Australia/Sydney"]
INDICATOR_LABELS = {
    "sma": "Simple moving average", "ema": "Exponential moving average", "macd": "MACD line",
    "macd_signal": "MACD signal line", "macd_hist": "MACD histogram", "rsi": "RSI",
    "pct_change": "% change", "zscore": "Z-score", "true_range": "True range", "atr": "ATR",
    "atr_pct": "ATR %", "volatility": "Volatility %", "bollinger_upper": "Upper Bollinger band",
    "bollinger_lower": "Lower Bollinger band", "keltner_upper": "Upper Keltner channel",
    "keltner_lower": "Lower Keltner channel", "highest": "Highest", "lowest": "Lowest",
    "pct_from_high": "% below recent high", "pct_from_low": "% above recent low",
    "opening_range_high": "Opening range high", "opening_range_low": "Opening range low",
}
INDICATOR_SHORT = {
    "sma": "SMA", "ema": "EMA", "macd": "MACD", "macd_signal": "MACD signal", "macd_hist": "MACD hist",
    "rsi": "RSI", "pct_change": "% change", "zscore": "Z-score", "true_range": "TR", "atr": "ATR",
    "atr_pct": "ATR %", "volatility": "Volatility", "bollinger_upper": "Upper BB",
    "bollinger_lower": "Lower BB", "keltner_upper": "Upper Keltner", "keltner_lower": "Lower Keltner",
    "highest": "Highest", "lowest": "Lowest", "pct_from_high": "% from high", "pct_from_low": "% from low",
    "opening_range_high": "Opening range high", "opening_range_low": "Opening range low",
}


def clean(v: Any) -> Any:
    """Make values JSON-safe (no NaN/inf, no numpy types)."""
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return v


def points(series: pd.Series, max_points: int = 600) -> list:
    s = series.dropna()
    if len(s) > max_points:
        step = math.ceil(len(s) / max_points)
        s = pd.concat([s.iloc[::step], s.iloc[-1:]])
        s = s[~s.index.duplicated(keep="last")]
    return [[t.isoformat(), round(float(v), 4)] for t, v in s.items()]


def err(status: int, message: str):
    raise HTTPException(status_code=status, detail=message)


def utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def workspace(request: Request) -> Workspace:
    """FastAPI dependency: the workspace this request works on."""
    return request.app.state.ctx.workspace(request)


def ctx_of(request: Request):
    return request.app.state.ctx


def number(body: dict, key: str, label: str, *, required=True, minimum: float | None = 0, exclusive=True,
           integer=False):
    """A number from a JSON body, with a plain-English error when it isn't one."""
    v = body.get(key)
    if v in (None, ""):
        if required:
            err(422, f"Fill in {label}")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and int(v) != v):
        err(422, f"{label[0].upper() + label[1:]} must be a {'whole ' if integer else ''}number")
    if minimum is not None and (v <= minimum if exclusive else v < minimum):
        word = "above" if exclusive else "at least"
        err(422, f"{label[0].upper() + label[1:]} must be {word} {minimum:g}")
    return int(v) if integer else float(v)


# --------------------------------------------------------------------------- settings mapping

NOTIFIER_DEFAULTS = {
    "console": {"enabled": True},
    "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": "", "token": ""},
    "discord": {"enabled": False, "webhook_url": ""},
    "webhook": {"enabled": False, "url": ""},
    "email": {"enabled": False, "host": "", "port": "587", "security": "starttls", "username": "", "password": "",
              "sender": "", "to": ""},
}
SECRET_KEYS = {"password", "token"}  # never sent back to the browser; blank on save keeps the saved one
REPORT_DEFAULTS = {"weekly": False, "day": "sun", "hour": 18, "tz": ""}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def settings_from_raw(raw: dict, ws: Workspace | None = None) -> dict:
    notifiers = copy.deepcopy(NOTIFIER_DEFAULTS)
    seen = set()
    for spec in raw.get("notifiers") or []:
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("type", "")).lower()
        if kind in notifiers and kind not in seen:
            seen.add(kind)
            entry = notifiers[kind]
            entry["enabled"] = spec.get("enabled", True) is not False
            for k in entry:
                if k != "enabled" and k in spec and spec[k] is not None:
                    entry[k] = "" if k in SECRET_KEYS else str(spec[k])
            for k in SECRET_KEYS & set(entry):
                entry[f"{k}_set"] = bool(spec.get(k))
    if raw.get("notifiers") and "console" not in seen:
        notifiers["console"]["enabled"] = False
    if ws is not None and not ws.limits.console_notifier:
        notifiers.pop("console")
    d = raw.get("data") or {}
    # The token itself never goes back to the browser; an empty field on save keeps it.
    data = {"source": str(d.get("source", "dukascopy")), "oanda_token": "",
            "oanda_token_set": bool(d.get("oanda_token")),
            "oanda_environment": str(d.get("oanda_environment", "practice"))}
    rep = {**REPORT_DEFAULTS, **(raw.get("report") if isinstance(raw.get("report"), dict) else {})}
    poll = raw.get("poll_minutes", 15)
    if ws is not None and isinstance(poll, (int, float)):
        poll = max(poll, ws.limits.min_poll_minutes)
    return {"poll_minutes": poll, "notifiers": notifiers, "data": data, "report": rep}


def raw_from_settings(raw: dict, settings: dict, ws: Workspace | None = None) -> dict:
    limits = ws.limits if ws is not None else None
    out = dict(raw)
    poll = settings.get("poll_minutes", 15)
    low = limits.min_poll_minutes if limits else 1
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not low <= poll <= 1440:
        err(422, f"Check interval must be between {low:g} and 1440 minutes")
    out["poll_minutes"] = poll
    notifiers = []
    given = settings.get("notifiers") or {}
    if not isinstance(given, dict):
        err(422, "Alert settings must be an object")
    old_specs = {str(x.get("type", "")).lower(): x for x in raw.get("notifiers") or [] if isinstance(x, dict)}
    for kind, defaults in NOTIFIER_DEFAULTS.items():
        if kind == "console" and limits and not limits.console_notifier:
            continue
        entry = {**defaults, **(given.get(kind) or {})}
        for k in SECRET_KEYS & set(defaults):
            if not str(entry.get(k) or "").strip():
                entry[k] = old_specs.get(kind, {}).get(k, "")
        spec: dict = {"type": kind}
        if not entry.get("enabled"):
            spec["enabled"] = False
        for k in defaults:
            if k != "enabled":
                v = str(entry.get(k) or "").strip()
                if v:
                    spec[k] = v
        if entry.get("enabled"):
            if kind == "ntfy" and not spec.get("topic"):
                err(422, "Add an ntfy topic, or turn ntfy off")
            if kind == "discord" and not spec.get("webhook_url"):
                err(422, "Add a Discord webhook URL, or turn Discord off")
            if kind == "webhook" and not spec.get("url"):
                err(422, "Add a webhook URL, or turn the webhook off")
            if kind == "email" and not (spec.get("host") and spec.get("to")):
                err(422, "Add the email server and the address to send to, or turn email off")
            if kind == "email" and not str(spec.get("port", "587")).isdigit():
                err(422, "The email port must be a number, such as 587")
        for k in ("server", "webhook_url", "url"):
            if spec.get(k) and not re.match(r"^https?://", spec[k]) and not spec[k].startswith("${"):
                err(422, "Web addresses for alerts must start with https:// (or http://)")
        notifiers.append(spec)
    out["notifiers"] = notifiers
    d = settings.get("data") or {}
    source = str(d.get("source", "dukascopy"))
    env = str(d.get("oanda_environment", "practice"))
    sources = limits.sources if limits else ("dukascopy", "yahoo", "oanda")
    if source not in sources:
        names = {"dukascopy": "Dukascopy", "yahoo": "Yahoo", "oanda": "OANDA"}
        err(422, "Price data source must be " + " or ".join(names.get(s, s) for s in sources))
    if env not in ("practice", "live"):
        err(422, "OANDA account type must be practice or live")
    token = str(d.get("oanda_token") or "").strip() or str((raw.get("data") or {}).get("oanda_token") or "")
    if source == "oanda" and not token:
        err(422, "Add your OANDA API token, or switch to another price data source")
    rep = settings.get("report") or {}
    if rep.get("day") not in DAYS:
        err(422, "Choose a day for the weekly report")
    hour = rep.get("hour")
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        err(422, "The report hour must be between 0 and 23")
    tz = str(rep.get("tz") or "")
    if tz:
        import zoneinfo
        try:
            zoneinfo.ZoneInfo(tz)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            err(422, "Unknown time zone for the weekly report")
    out["report"] = {"weekly": bool(rep.get("weekly")), "day": rep["day"], "hour": hour}
    if tz:
        out["report"]["tz"] = tz
    out["data"] = {"source": source, "oanda_environment": env}
    if token:
        out["data"]["oanda_token"] = token
    return out


# --------------------------------------------------------------------------- appearance

ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_APPEARANCE = {"theme": "auto", "accent": "#0a84ff", "glass": True, "density": "comfortable"}


def appearance_from_raw(raw: dict) -> dict:
    a = raw.get("appearance") if isinstance(raw.get("appearance"), dict) else {}
    out = dict(DEFAULT_APPEARANCE)
    if a.get("theme") in ("auto", "light", "dark"):
        out["theme"] = a["theme"]
    if isinstance(a.get("accent"), str) and ACCENT_RE.match(a["accent"]):
        out["accent"] = a["accent"].lower()
    if isinstance(a.get("glass"), bool):
        out["glass"] = a["glass"]
    if a.get("density") in ("comfortable", "compact"):
        out["density"] = a["density"]
    return out


def checked_appearance(given: dict) -> dict:
    if not isinstance(given, dict):
        err(422, "Appearance must be an object")
    out = {}
    if "theme" in given:
        if given["theme"] not in ("auto", "light", "dark"):
            err(422, "Theme must be auto, light or dark")
        out["theme"] = given["theme"]
    if "accent" in given:
        if not isinstance(given["accent"], str) or not ACCENT_RE.match(given["accent"]):
            err(422, "Accent colour must look like #0a84ff")
        out["accent"] = given["accent"].lower()
    if "density" in given:
        if given["density"] not in ("comfortable", "compact"):
            err(422, "Density must be comfortable or compact")
        out["density"] = given["density"]
    if "glass" in given:
        if not isinstance(given["glass"], bool):
            err(422, "Glass must be true or false")
        out["glass"] = given["glass"]
    return out
