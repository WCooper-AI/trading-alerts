"""Beginner "ideas": well-known rule patterns with a few plain-language settings.

A recipe turns a market plus a handful of choices into a normal strategy dict,
so everything downstream (validation, backtests, live alerts) is unchanged.
Every setting starts blank so people choose their own values. Each knob's
``example`` is a neutral textbook value used only by tests and the docs; it is
never sent to the interface and isn't a recommendation.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------- markets

# spread: a rough typical CFD spread in price units, as a starting value to check with a broker.
#   Ignored when the price data has its own bid/ask (OANDA).
# point_value: money per 1.0 price move for 1 unit of size, so that size reads as "£ per point".
# funding: rough yearly overnight funding %, a starting value to check with a broker.
# symbol / oanda: the market's name at each data source (chosen in Settings).
MARKETS: list[dict[str, Any]] = [
    {"key": "us500", "news": ["USD"], "label": "US 500", "symbol": "ES=F", "dukascopy": "INSTRUMENT_IDX_AMERICA_E_SANDP_500", "oanda": "SPX500_USD", "funding": 6.0, "group": "Indices", "spread": 0.5, "point_value": 1,
     "note": "The 500 biggest US companies (S&P 500)"},
    {"key": "us100", "news": ["USD"], "label": "US 100", "symbol": "NQ=F", "dukascopy": "INSTRUMENT_IDX_AMERICA_E_NQ_100", "oanda": "NAS100_USD", "funding": 6.0, "group": "Indices", "spread": 1.5, "point_value": 1,
     "note": "Big US tech companies (Nasdaq 100)"},
    {"key": "us30", "news": ["USD"], "label": "Wall Street 30", "symbol": "YM=F", "dukascopy": "INSTRUMENT_IDX_AMERICA_E_D_J_IND", "oanda": "US30_USD", "funding": 6.0, "group": "Indices", "spread": 2.5, "point_value": 1,
     "note": "The Dow Jones"},
    {"key": "de40", "news": ["EUR"], "label": "Germany 40", "symbol": "^GDAXI", "dukascopy": "INSTRUMENT_IDX_EUROPE_E_DAAX", "oanda": "DE30_EUR", "funding": 6.0, "group": "Indices", "spread": 1.5, "point_value": 1,
     "note": "Germany's DAX"},
    {"key": "uk100", "news": ["GBP"], "label": "UK 100", "symbol": "^FTSE", "dukascopy": "INSTRUMENT_IDX_EUROPE_E_FUTSEE_100", "oanda": "UK100_GBP", "funding": 6.0, "group": "Indices", "spread": 1.0, "point_value": 1,
     "note": "The FTSE 100"},
    {"key": "nl25", "news": ["EUR"], "label": "Netherlands 25", "symbol": "^AEX", "dukascopy": "INSTRUMENT_IDX_EUROPE_NLD_IDX_EUR", "oanda": "NL25_EUR", "funding": 6.0, "group": "Indices", "spread": 0.3, "point_value": 1,
     "note": "The Dutch AEX"},
    {"key": "gold", "news": ["USD"], "label": "Gold", "symbol": "GC=F", "dukascopy": "INSTRUMENT_FX_METALS_XAU_USD", "oanda": "XAU_USD", "funding": 6.0, "group": "Commodities", "spread": 0.4, "point_value": 1,
     "note": "Gold, in US dollars per ounce"},
    {"key": "silver", "news": ["USD"], "label": "Silver", "symbol": "SI=F", "dukascopy": "INSTRUMENT_FX_METALS_XAG_USD", "oanda": "XAG_USD", "funding": 6.0, "group": "Commodities", "spread": 0.03, "point_value": 1,
     "note": "Silver, in US dollars per ounce"},
    {"key": "oil", "news": ["USD"], "label": "Oil (WTI)", "symbol": "CL=F", "dukascopy": "INSTRUMENT_CMD_ENERGY_E_LIGHT", "oanda": "WTICO_USD", "funding": 6.0, "group": "Commodities", "spread": 0.03, "point_value": 1,
     "note": "US crude oil"},
    {"key": "eurusd", "news": ["EUR", "USD"], "label": "EUR/USD", "symbol": "EURUSD=X", "dukascopy": "INSTRUMENT_FX_MAJORS_EUR_USD", "oanda": "EUR_USD", "funding": 2.0, "group": "Currencies", "spread": 0.00008,
     "point_value": 10000, "note": "Euro against the dollar. Size is per pip"},
    {"key": "gbpusd", "news": ["GBP", "USD"], "label": "GBP/USD", "symbol": "GBPUSD=X", "dukascopy": "INSTRUMENT_FX_MAJORS_GBP_USD", "oanda": "GBP_USD", "funding": 2.0, "group": "Currencies", "spread": 0.0001,
     "point_value": 10000, "note": "Pound against the dollar. Size is per pip"},
    {"key": "btc", "news": ["USD"], "label": "Bitcoin", "symbol": "BTC-USD", "dukascopy": "INSTRUMENT_VCCY_BTC_USD", "oanda": "BTC_USD", "funding": 15.0, "group": "Crypto", "spread": 40, "point_value": 1,
     "note": "Trades 24 hours a day, 7 days a week"},
]
MARKETS_BY_KEY = {m["key"]: m for m in MARKETS}
MARKETS_BY_SYMBOL = {m["symbol"]: m for m in MARKETS}


class RecipeError(ValueError):
    pass


class MissingSettings(RecipeError):
    def __init__(self, labels: list[str]):
        self.labels = labels
        super().__init__("Still to fill in: " + ", ".join(labels))


# --------------------------------------------------------------------------- shared settings

STOPS = {"tight": 1.5, "normal": 2.0, "wide": 3.0}
TARGETS = {"none": None, "1": 1.0, "1.5": 1.5, "2": 2.0, "3": 3.0}
TIMEFRAME_LABELS = {"5m": "Every 5 minutes", "15m": "Every 15 minutes", "1h": "Every hour",
                    "4h": "Every 4 hours", "1d": "Once a day"}

COMMON_KNOBS = [
    {"key": "stop", "label": "How much room to give a trade", "kind": "select", "example": "normal",
     "options": [{"value": "tight", "label": "A little (tight stop)"},
                 {"value": "normal", "label": "Normal"},
                 {"value": "wide", "label": "A lot (wide stop)"}],
     "help": "The stop loss closes a losing trade. It's set from how much the market usually moves (ATR), "
             "so it adapts to quiet and busy markets."},
    {"key": "target", "label": "When to take profit", "kind": "select", "example": "2",
     "options": [{"value": "1", "label": "When the gain equals the risk"},
                 {"value": "1.5", "label": "At 1.5 times the risk"},
                 {"value": "2", "label": "At 2 times the risk"},
                 {"value": "3", "label": "At 3 times the risk"},
                 {"value": "none", "label": "No fixed target"}],
     "help": "Risk is what you'd lose if the stop is hit. A target of 2 times the risk aims to win twice that."},
    {"key": "avoid_news", "label": "Stay out for 30 minutes either side of big news (such as the US jobs report)",
     "kind": "toggle", "example": False,
     "help": "Skips signals near high-impact news for this market's currencies. Spreads widen and prices jump then."},
    {"key": "max_hold", "label": "Close a trade after this many candles, if still open", "kind": "number",
     "example": None, "min": 1, "max": 1000, "step": 1, "optional": True,
     "help": "Leave blank to hold until the stop, target or exit rule closes it."},
]


@dataclass
class Recipe:
    id: str
    name: str
    short_name: str
    sell_name: str
    summary: str
    how: str
    timeframes: list[str]
    default_timeframe: str
    knobs: list[dict]
    build: Callable[[dict, str], dict]
    common: list[str] = field(default_factory=lambda: ["stop", "target", "max_hold"])
    target_default: str = "2"

    def all_knobs(self) -> list[dict]:
        common = []
        for k in COMMON_KNOBS:
            if k["key"] in self.common or k["key"] == "avoid_news":
                k = copy.deepcopy(k)
                if k["key"] == "target":
                    k["example"] = self.target_default
                common.append(k)
        return self.knobs + common

    def public(self) -> dict:
        knobs = [{**{k: v for k, v in knob.items() if k != "example"}, "default": None} for knob in self.all_knobs()]
        return {
            "id": self.id, "name": self.name, "sell_name": self.sell_name, "summary": self.summary,
            "how": self.how, "timeframes": [{"value": t, "label": TIMEFRAME_LABELS[t]} for t in self.timeframes],
            "knobs": knobs,
        }


def _cmp(left, op, right):
    return {"left": left, "op": op, "right": right}


FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "crosses_above": "crosses_below", "crosses_below": "crosses_above"}


def _op(op: str, sign: int) -> str:
    return op if sign > 0 else FLIP[op]


# --------------------------------------------------------------------------- the recipes


def _build_dip(s: dict, direction: str) -> dict:
    sign = 1 if direction == "long" else -1
    if sign > 0:
        rules = [_cmp({"indicator": "pct_from_high", "period": s["lookback"]}, "<=", -s["dip_pct"])]
    else:
        rules = [_cmp({"indicator": "pct_from_low", "period": s["lookback"]}, ">=", s["dip_pct"])]
    if s["trend_filter"]:
        rules.append(_cmp("close", _op(">", sign), {"indicator": "sma", "period": 200, "source": "close"}))
    return {"entry": {"all": rules}}


def _build_trend(s: dict, direction: str) -> dict:
    sign = 1 if direction == "long" else -1
    hi = {"indicator": "highest", "period": s["breakout"], "source": "high", "shift": 1}
    lo = {"indicator": "lowest", "period": s["breakout"], "source": "low", "shift": 1}
    ex_lo = {"indicator": "lowest", "period": s["exit_after"], "source": "low", "shift": 1}
    ex_hi = {"indicator": "highest", "period": s["exit_after"], "source": "high", "shift": 1}
    entry = _cmp("close", ">", hi) if sign > 0 else _cmp("close", "<", lo)
    leave = _cmp("close", "<", ex_lo) if sign > 0 else _cmp("close", ">", ex_hi)
    return {"entry": {"all": [entry]}, "when": {"all": [leave]}}


def _build_rsi(s: dict, direction: str) -> dict:
    sign = 1 if direction == "long" else -1
    level = s["level"] if sign > 0 else 100 - s["level"]
    rules = [_cmp({"indicator": "rsi", "period": s["rsi_period"], "source": "close"}, _op("crosses_above", sign), level)]
    if s["trend_filter"]:
        rules.append(_cmp("close", _op(">", sign), {"indicator": "sma", "period": 200, "source": "close"}))
    return {"entry": {"all": rules}}


def _build_ma(s: dict, direction: str) -> dict:
    if s["fast"] >= s["slow"]:
        raise RecipeError("The short average needs fewer candles than the long average")
    sign = 1 if direction == "long" else -1
    fast = {"indicator": s["ma_type"], "period": s["fast"], "source": "close"}
    slow = {"indicator": s["ma_type"], "period": s["slow"], "source": "close"}
    return {"entry": {"all": [_cmp(fast, _op("crosses_above", sign), slow)]},
            "when": {"all": [_cmp(fast, _op("crosses_below", sign), slow)]}}


SESSIONS = {
    "us": {"label": "New York open (US markets)", "start": "09:30", "close": "16:00", "tz": "America/New_York"},
    "london": {"label": "London open (UK markets)", "start": "08:00", "close": "16:30", "tz": "Europe/London"},
    "frankfurt": {"label": "Frankfurt open (German markets)", "start": "09:00", "close": "17:30", "tz": "Europe/Berlin"},
}


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _mins(text: str) -> int:
    h, m = (int(x) for x in text.split(":"))
    return h * 60 + m


def _build_orb(s: dict, direction: str) -> dict:
    sign = 1 if direction == "long" else -1
    sess = SESSIONS[s["session"]]
    start, close = _mins(sess["start"]), _mins(sess["close"])
    range_end = start + int(s["range_minutes"])
    until = _mins(s["entry_until"])
    if not range_end < until <= close - 30:
        raise RecipeError(
            f"'Only enter until' must be after {_hhmm(range_end)} and by {_hhmm(close - 30)} {sess['tz'].split('/')[-1].replace('_', ' ')} time"
        )
    rng = {"session_start": sess["start"], "minutes": int(s["range_minutes"]), "tz": sess["tz"]}
    hi = {"indicator": "opening_range_high", **rng}
    lo = {"indicator": "opening_range_low", **rng}
    entry = [
        _cmp("close", "crosses_above", hi) if sign > 0 else _cmp("close", "crosses_below", lo),
        {"time_between": [_hhmm(range_end), _hhmm(until)], "tz": sess["tz"]},
        {"weekdays": ["mon", "tue", "wed", "thu", "fri"]},
    ]
    # flatten before the close: the signal on the bar starting 30 min before the close exits 15 min before it
    return {
        "entry": {"all": entry},
        "stop": {"level": lo if sign > 0 else hi},
        "when": {"all": [{"time_between": [_hhmm(close - 30), _hhmm(close - 15)], "tz": sess["tz"]}]},
    }


RECIPES: list[Recipe] = [
    Recipe(
        id="dip", name="Buy the dip", short_name="dip", sell_name="Sell the rally",
        summary="Buy after the price falls a set amount from its recent high.",
        how="Waits for the price to drop by the percentage you choose from its highest point of the last "
            "few candles, then buys. Optionally it only buys while the long-term trend is still up. "
            "The idea is that short drops in a rising market often recover.",
        timeframes=["1h", "4h", "1d"], default_timeframe="4h",
        knobs=[
            {"key": "dip_pct", "label": "Buy after a drop of", "kind": "number", "unit": "%", "example": 3,
             "min": 0.2, "max": 30, "step": 0.1,
             "help": "Measured from the highest price in the period below."},
            {"key": "lookback", "label": "From the highest price of the last", "kind": "number", "unit": "candles",
             "example": 30, "min": 3, "max": 300, "step": 1, "integer": True},
            {"key": "trend_filter", "label": "Only when the long-term trend agrees (price above its 200-candle average)",
             "kind": "toggle", "example": True},
        ],
        build=_build_dip,
    ),
    Recipe(
        id="trend", name="Follow the trend", short_name="trend", sell_name="Follow the trend down",
        summary="Buy when the price breaks above its recent highs, and stay in while it keeps going.",
        how="Buys when the price closes above its highest point of the last few candles, and exits "
            "when it closes below a shorter-term low. The idea is that strong moves often keep going.",
        timeframes=["4h", "1d"], default_timeframe="1d", target_default="none",
        knobs=[
            {"key": "breakout", "label": "Buy when the price beats the high of the last", "kind": "number",
             "unit": "candles", "example": 20, "min": 3, "max": 300, "step": 1, "integer": True},
            {"key": "exit_after", "label": "Exit when it falls below the low of the last", "kind": "number",
             "unit": "candles", "example": 10, "min": 2, "max": 200, "step": 1, "integer": True},
        ],
        build=_build_trend,
    ),
    Recipe(
        id="rsi", name="Bounce from oversold", short_name="rsi", sell_name="Fade an overbought market",
        summary="Buy when a sharp fall starts to turn back up.",
        how="Uses RSI, a 0 to 100 gauge of how hard the price has been falling or rising. It buys when RSI "
            "climbs back above the level you choose after dropping below it. The idea is that sharp "
            "sell-offs often bounce.",
        timeframes=["1h", "4h", "1d"], default_timeframe="4h",
        knobs=[
            {"key": "level", "label": "Buy when RSI climbs back above", "kind": "number", "example": 30,
             "min": 5, "max": 50, "step": 1, "integer": True,
             "help": "Lower numbers wait for bigger falls. When selling, the mirror level is used (e.g. 70)."},
            {"key": "rsi_period", "label": "RSI measured over", "kind": "number", "unit": "candles", "example": 14,
             "min": 2, "max": 100, "step": 1, "integer": True},
            {"key": "trend_filter", "label": "Only when the long-term trend agrees (price above its 200-candle average)",
             "kind": "toggle", "example": False},
        ],
        build=_build_rsi,
    ),
    Recipe(
        id="ma", name="Moving average crossover", short_name="ma", sell_name="Moving average crossover (sell)",
        summary="Buy when the short-term average price rises above the long-term one.",
        how="Tracks two averages of the price, one short and one long. It buys when the short one crosses "
            "above the long one and exits when it crosses back below. A classic way to catch changes in trend.",
        timeframes=["1h", "4h", "1d"], default_timeframe="1d", target_default="none",
        knobs=[
            {"key": "fast", "label": "Short average over", "kind": "number", "unit": "candles", "example": 20,
             "min": 2, "max": 200, "step": 1, "integer": True},
            {"key": "slow", "label": "Long average over", "kind": "number", "unit": "candles", "example": 50,
             "min": 3, "max": 400, "step": 1, "integer": True},
            {"key": "ma_type", "label": "Type of average", "kind": "select", "example": "ema",
             "options": [{"value": "ema", "label": "Exponential (reacts faster)"},
                         {"value": "sma", "label": "Simple"}]},
        ],
        build=_build_ma,
    ),
    Recipe(
        id="orb", name="Opening range breakout", short_name="orb", sell_name="Opening range breakdown",
        summary="Trade a break of the first few minutes' range after a market opens. Closes before the end of the day.",
        how="Marks the highest and lowest price in the first minutes after the market opens. If the price "
            "then breaks above that range during the morning, it buys, with the stop at the bottom of the "
            "range. Any open trade is closed shortly before the market shuts. For short-term trading.",
        timeframes=["5m", "15m"], default_timeframe="15m", common=["target"], target_default="1.5",
        knobs=[
            {"key": "session", "label": "Which market open", "kind": "select", "example": "us",
             "options": [{"value": k, "label": v["label"]} for k, v in SESSIONS.items()]},
            {"key": "range_minutes", "label": "Opening range length", "kind": "select", "example": "30",
             "options": [{"value": "15", "label": "First 15 minutes"}, {"value": "30", "label": "First 30 minutes"},
                         {"value": "60", "label": "First hour"}]},
            {"key": "entry_until", "label": "Only enter until (local time at that market)", "kind": "time",
             "example": "12:00"},
        ],
        build=_build_orb,
    ),
]
RECIPES_BY_ID = {r.id: r for r in RECIPES}


# --------------------------------------------------------------------------- settings + build

BASE_LABELS = {"direction": "Buy or sell", "timeframe": "How often to check", "balance": "Practice balance",
               "risk_pct": "Risk on each trade", "currency": "Currency"}


def _blank(v) -> bool:
    return v is None or v == ""


def example_settings(recipe_id: str, **overrides) -> dict:
    """Complete settings from the knobs' textbook examples. For tests and docs only."""
    recipe = RECIPES_BY_ID[recipe_id]
    out = {k["key"]: k.get("example") for k in recipe.all_knobs()}
    out.update(direction="long", timeframe=recipe.default_timeframe, balance=1000, risk_pct=1.0, currency="GBP")
    out.update(overrides)
    return out


def normalise_settings(recipe: Recipe, given: dict) -> dict:
    """Check the settings. Blank ones are listed together in one 'Still to fill in' message."""
    given = given or {}
    out: dict[str, Any] = {}
    missing: list[str] = []
    for knob in recipe.all_knobs():
        key, kind = knob["key"], knob["kind"]
        v = given.get(key)
        if kind == "toggle":
            out[key] = bool(v)
            continue
        if _blank(v):
            if knob.get("optional"):
                out[key] = None
            else:
                missing.append(knob["label"])
            continue
        if kind == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    raise RecipeError(f"'{knob['label']}' needs a number") from None
            if not knob["min"] <= v <= knob["max"]:
                raise RecipeError(f"'{knob['label']}' must be between {knob['min']} and {knob['max']}")
            out[key] = int(round(v)) if knob.get("integer") or knob["key"] == "max_hold" else float(v)
        elif kind == "select":
            allowed = [o["value"] for o in knob["options"]]
            v = str(v)
            if v not in allowed:
                raise RecipeError(f"Choose one of the options for '{knob['label']}'")
            out[key] = v
        elif kind == "time":
            try:
                _mins(str(v))
            except ValueError:
                raise RecipeError(f"'{knob['label']}' must be a time like 12:00") from None
            out[key] = str(v)
    base = {k: given.get(k) for k in BASE_LABELS}
    for k, label in BASE_LABELS.items():
        if _blank(base[k]):
            missing.append(label)
    if not _blank(base["direction"]) and base["direction"] not in ("long", "short"):
        raise RecipeError("Direction must be long or short")
    tf = base["timeframe"]
    if not _blank(tf) and tf not in recipe.timeframes:
        raise RecipeError(f"{recipe.name} works on: {', '.join(TIMEFRAME_LABELS[t].lower() for t in recipe.timeframes)}")
    for k, lo, hi in (("balance", 10, 100_000_000), ("risk_pct", 0.05, 10)):
        v = base[k]
        if _blank(v):
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise RecipeError(f"{BASE_LABELS[k]} must be between {lo:g} and {hi:g}")
    if not _blank(base["currency"]) and base["currency"] not in ("GBP", "USD", "EUR"):
        raise RecipeError("Currency must be GBP, USD or EUR")
    if missing:
        raise MissingSettings(missing)
    out.update(direction=base["direction"], timeframe=tf, balance=base["balance"], risk_pct=base["risk_pct"],
               currency=base["currency"])
    return out


def build_strategy(recipe_id: str, market_key: str, settings: dict) -> dict:
    recipe = RECIPES_BY_ID.get(recipe_id)
    if not recipe:
        raise RecipeError(f"Unknown idea {recipe_id!r}")
    market = MARKETS_BY_KEY.get(market_key)
    if not market:
        raise RecipeError(f"Unknown market {market_key!r}")
    if recipe.id == "orb" and market["key"] in ("btc", "eurusd", "gbpusd", "gold", "silver", "oil"):
        raise RecipeError("Opening range breakout needs a market with an opening time, such as a stock index")
    s = normalise_settings(recipe, settings)
    parts = recipe.build(s, s["direction"])
    if s.get("avoid_news"):
        parts["entry"]["all"].append({"avoid_news": {"before": 30, "after": 30, "currencies": market["news"]}})

    exit_: dict[str, Any] = {"stop": parts.get("stop") or {"atr": STOPS[s["stop"]], "atr_period": 14}}
    target = TARGETS[s["target"]]
    if target:
        exit_["target"] = {"r_multiple": target}
    if s.get("max_hold"):
        exit_["max_bars"] = s["max_hold"]
    if parts.get("when"):
        exit_["when"] = parts["when"]

    name = recipe.name if s["direction"] == "long" else recipe.sell_name
    return {
        "name": f"{name} on {market['label']}",
        "recipe": {"id": recipe.id, "market": market["key"], "settings": s},
        "market": {"symbol": market["key"], "label": market["label"], "provider": "auto",
                   "timeframe": s["timeframe"]},
        "direction": s["direction"],
        "entry": parts["entry"],
        "exit": exit_,
        "risk": {"account_size": s["balance"], "risk_per_trade_pct": s["risk_pct"],
                 "point_value": market["point_value"], "size_step": 0.01, "min_size": 0.01,
                 "currency": s["currency"]},
        "costs": {"spread": market["spread"], "funding_pct": market["funding"]},
    }


RULE_KEYS = ("market", "direction", "entry", "exit")


def still_matches_recipe(data: dict) -> bool:
    """True if a saved strategy's rules are exactly what its recipe produces.

    When someone edits the full rules by hand the strategy stops being a recipe
    and is shown in the advanced editor from then on.
    """
    r = data.get("recipe")
    if not isinstance(r, dict):
        return False
    try:
        built = build_strategy(r.get("id", ""), r.get("market", ""), r.get("settings") or {})
    except RecipeError:
        return False
    for key in ("direction", "entry", "exit"):
        if built[key] != data.get(key):
            return False
    m, bm = data.get("market") or {}, built["market"]
    if m.get("timeframe") != bm["timeframe"]:
        return False
    if m.get("provider") == "auto":
        return m.get("symbol") == bm["symbol"]
    # strategies saved before markets followed the Price data setting
    market = MARKETS_BY_KEY.get(r.get("market", ""), {})
    return m.get("provider") == "yahoo" and m.get("symbol") == market.get("symbol")
