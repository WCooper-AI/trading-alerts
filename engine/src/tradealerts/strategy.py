"""Strategy files: loading, validation and turning rules into signals."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import indicators, rules, timeframes

CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}


class StrategyError(ValueError):
    pass


class SkipTrade(Exception):
    """Raised when a signal can't become a trade (e.g. stop on the wrong side)."""


@dataclass
class Market:
    symbol: str
    label: str
    provider: str = "yahoo"
    timeframe: str = "1h"
    lookback_days: int | None = None
    tz: str = "UTC"
    candle_alignment: str = "new_york"

    def default_lookback(self) -> int:
        # Sources with shorter history (Yahoo) cap this themselves.
        mins = timeframes.minutes(self.timeframe)
        if mins < 60:
            return 180
        if mins < 1440:
            return 1095
        return 3650


@dataclass
class Risk:
    account_size: float = 10_000.0
    risk_per_trade_pct: float = 0.5
    point_value: float = 1.0
    size_step: float = 0.01
    min_size: float = 0.01
    max_size: float | None = None
    compound: bool = True
    currency: str = "GBP"

    @property
    def symbol(self) -> str:
        return CURRENCY_SYMBOLS.get(self.currency.upper(), self.currency + " ")


@dataclass
class Prepared:
    """Signals and reference values for every bar of a DataFrame."""

    index: pd.DatetimeIndex
    entry: np.ndarray
    exit: np.ndarray
    refs: dict[str, np.ndarray]
    values: dict[str, np.ndarray] = field(default_factory=dict)  # what the entry rule read, per bar

    def refs_at(self, i: int) -> dict[str, float | None]:
        return self._at(self.refs, i)

    def values_at(self, i: int) -> dict[str, float | None]:
        return self._at(self.values, i)

    @staticmethod
    def _at(arrays: dict[str, np.ndarray], i: int) -> dict[str, float | None]:
        out = {}
        for k, arr in arrays.items():
            v = float(arr[i])
            out[k] = None if np.isnan(v) else round(v, 6)
        return out


@dataclass
class Strategy:
    id: str
    name: str
    market: Market
    direction: str
    entry_spec: dict
    stop_spec: dict
    target_spec: dict | None = None
    max_bars: int | None = None
    exit_spec: dict | None = None
    risk: Risk = field(default_factory=Risk)
    spread: float = 0.0
    funding_pct: float = 0.0
    use_data_spread: bool = True
    commission: float = 0.0  # money per fill (entry and exit each)
    commission_pct: float = 0.0  # % of the trade's value per fill
    borrow_pct: float = 0.0  # yearly, short positions only, charged nightly like funding
    slippage: float = 0.0  # price points lost on each market fill, beyond the spread
    slippage_pct: float = 0.0  # % of the fill price lost on each market fill
    template: bool = False
    enabled: bool = True
    alerts_enabled: bool = True
    alert_priority: str = "default"
    source_hash: str = ""
    path: Path | None = None

    def __post_init__(self):
        self._entry = rules.compile_condition(self.entry_spec, "entry")
        self._exit = rules.compile_condition(self.exit_spec, "exit.when") if self.exit_spec else None
        self._reasons = [(label, rules.compile_operand(spec, "entry")) for label, spec in rules.reason_operands(self.entry_spec)]
        self._stop_level = self._level_operand(self.stop_spec, "exit.stop")
        self._target_level = (
            self._level_operand(self.target_spec, "exit.target") if self.target_spec else None
        )

    @property
    def sign(self) -> int:
        return 1 if self.direction == "long" else -1

    # ------------------------------------------------------------------ levels

    @staticmethod
    def _level_operand(spec: dict, path: str):
        if "level" in spec:
            return rules.compile_operand(spec["level"], f"{path}.level")
        return None

    @property
    def lookback_candles(self) -> int:
        """Candles of history the rules, stop and target need before all their values are known."""
        need = max(rules.max_lookback(self.entry_spec), rules.max_lookback(self.exit_spec))
        for spec in (self.stop_spec, self.target_spec):
            if spec and "atr" in spec:
                need = max(need, indicators.lookback("atr", {"period": int(spec.get("atr_period", 14))}))
            if spec and "level" in spec:
                need = max(need, rules.max_lookback(spec["level"]))
        return need

    @property
    def warmup_candles(self) -> int:
        """Candles to load before a test period so every indicator is ready on its first candle."""
        from .backtest import WARMUP_CANDLES

        return max(WARMUP_CANDLES, self.lookback_candles)

    def prepare(self, df: pd.DataFrame, ctx: "rules.Context | None" = None) -> Prepared:
        """Pass ``ctx`` to share one indicator cache across several strategies on the same data."""
        if ctx is None or ctx.df is not df:
            ctx = rules.Context(df)
        entry = rules.evaluate(self._entry, df, ctx).to_numpy()
        exit_ = (
            rules.evaluate(self._exit, df, ctx).to_numpy()
            if self._exit
            else np.zeros(len(df), dtype=bool)
        )
        refs: dict[str, np.ndarray] = {}
        for name, spec in (("stop", self.stop_spec), ("target", self.target_spec)):
            if spec and "atr" in spec:
                refs[f"{name}_atr"] = ctx.indicator(
                    "atr", {"period": int(spec.get("atr_period", 14))}
                ).to_numpy(dtype=float)
        if self._stop_level:
            refs["stop_level"] = _to_array(self._stop_level(ctx), df)
        if self._target_level:
            refs["target_level"] = _to_array(self._target_level(ctx), df)
        values = {label: _to_array(fn(ctx), df) for label, fn in self._reasons}
        return Prepared(df.index, entry, exit_, refs, values)

    def plan(self, entry_price: float, refs: dict[str, float | None]) -> tuple[float, float | None]:
        """Stop and target prices for a trade entered at ``entry_price``."""
        s = self.sign
        stop = self._level(self.stop_spec, "stop", entry_price, refs, -s)
        if stop is None or (stop - entry_price) * s >= 0:
            raise SkipTrade("stop is missing or on the wrong side of the entry price")
        target = None
        if self.target_spec:
            if "r_multiple" in self.target_spec:
                target = entry_price + s * float(self.target_spec["r_multiple"]) * abs(entry_price - stop)
            else:
                target = self._level(self.target_spec, "target", entry_price, refs, s)
            if target is None or (target - entry_price) * s <= 0:
                raise SkipTrade("target is missing or on the wrong side of the entry price")
        return stop, target

    @staticmethod
    def _level(spec, name, entry, refs, direction) -> float | None:
        if "atr" in spec:
            a = refs.get(f"{name}_atr")
            return None if a is None else entry + direction * float(spec["atr"]) * a
        if "percent" in spec:
            return entry * (1 + direction * float(spec["percent"]) / 100)
        if "points" in spec:
            return entry + direction * float(spec["points"])
        if "level" in spec:
            return refs.get(f"{name}_level")
        return None

    def describe_level(self, which: str) -> str:
        spec = self.stop_spec if which == "stop" else self.target_spec
        if not spec:
            return "none"
        if "atr" in spec:
            return f"{spec['atr']}× ATR({spec.get('atr_period', 14)})"
        if "percent" in spec:
            return f"{spec['percent']}%"
        if "points" in spec:
            return f"{spec['points']} pts"
        if "r_multiple" in spec:
            return f"{spec['r_multiple']}R"
        return "rule level"


def _to_array(value, df) -> np.ndarray:
    if isinstance(value, pd.Series):
        return value.reindex(df.index).to_numpy(dtype=float)
    return np.full(len(df), float(value))


# --------------------------------------------------------------------------- loading

_LEVEL_KEYS = {"atr", "percent", "points", "level"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "strategy"


def _require(d: dict, key: str, path: str):
    if key not in d or d[key] in (None, ""):
        raise StrategyError(f"{path}: '{key}' is required")
    return d[key]


def _check_keys(d: dict, allowed: set[str], path: str):
    extra = set(d) - allowed
    if extra:
        raise StrategyError(
            f"{path}: unknown key(s) {', '.join(sorted(extra))}. Allowed: {', '.join(sorted(allowed))}"
        )


def _whole(v) -> bool:
    """A real whole number: YAML ``true``/``false`` are ints in Python but not accepted."""
    return isinstance(v, int) and not isinstance(v, bool)


def _number(v, path, minimum=None, allow_none=False):
    if (v is None or v == "") and allow_none:
        return None
    if v is None or v == "":
        raise StrategyError(f"{path}: fill this in")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise StrategyError(f"{path} must be a number, got {v!r}")
    if not math.isfinite(v):
        raise StrategyError(f"{path} must be a finite number")
    if minimum is not None and v < minimum:
        raise StrategyError(f"{path} must be at least {minimum}, got {v}")
    return float(v)


def _level_spec(spec, path, allow_r=False):
    if not isinstance(spec, dict):
        raise StrategyError(f"{path} must be a mapping like {{atr: 2}} or {{percent: 1}}")
    allowed = _LEVEL_KEYS | ({"r_multiple"} if allow_r else set())
    kinds = [k for k in spec if k in allowed]
    if len(kinds) != 1:
        raise StrategyError(f"{path} needs exactly one of: {', '.join(sorted(allowed))}")
    kind = kinds[0]
    _check_keys(spec, {kind, "atr_period"} if kind == "atr" else {kind}, path)
    if kind != "level":
        _number(spec[kind], f"{path}.{kind}")
        if spec[kind] <= 0:
            raise StrategyError(f"{path}.{kind} must be greater than 0")
    if kind == "atr" and "atr_period" in spec:
        p = spec["atr_period"]
        if p is None or p == "":
            raise StrategyError(f"{path}.atr_period: fill this in")
        if not _whole(p) or p < 1:
            raise StrategyError(f"{path}.atr_period must be a whole number of 1 or more")
    return spec


def strategy_from_dict(data: dict, sid: str = "", path: Path | None = None, raw_text: str = "") -> Strategy:
    if not isinstance(data, dict):
        raise StrategyError("strategy file must be a YAML mapping")
    _check_keys(
        data,
        {"name", "description", "template", "enabled", "market", "direction", "entry", "exit",
         "risk", "costs", "alerts", "recipe"},
        "strategy",
    )
    name = str(_require(data, "name", "strategy"))
    sid = sid or _slug(name)

    m = _require(data, "market", "strategy")
    if not isinstance(m, dict):
        raise StrategyError("market must be a mapping")
    _check_keys(m, {"symbol", "label", "provider", "timeframe", "lookback_days", "tz", "candle_alignment"}, "market")
    if m.get("symbol") in (None, ""):
        raise StrategyError("market: choose a market")
    if "timeframe" in m and m["timeframe"] in (None, ""):
        raise StrategyError("market.timeframe: choose a candle size")
    tf = str(m.get("timeframe", "1h"))
    try:
        timeframes.to_timedelta(tf)
    except timeframes.TimeframeError as exc:
        raise StrategyError(f"market.timeframe: {exc}") from None
    provider = str(m.get("provider", "yahoo")).lower()
    if provider not in ("yahoo", "csv", "oanda", "auto", "dukascopy"):
        raise StrategyError("market.provider must be auto, dukascopy, yahoo, oanda or csv")
    alignment = str(m.get("candle_alignment", "new_york"))
    if alignment not in ("new_york", "utc"):
        raise StrategyError("market.candle_alignment must be new_york or utc")
    lookback = m.get("lookback_days")
    if lookback is not None and (not _whole(lookback) or lookback < 1):
        raise StrategyError("market.lookback_days must be a whole number of days")
    market = Market(
        symbol=str(_require(m, "symbol", "market")),
        label=str(m.get("label") or m["symbol"]),
        provider=provider,
        timeframe=tf,
        lookback_days=lookback,
        tz=str(m.get("tz", "UTC")),
        candle_alignment=alignment,
    )

    if "direction" in data and data["direction"] in (None, ""):
        raise StrategyError("direction: choose long (buy) or short (sell)")
    direction = str(data.get("direction", "long")).lower()
    if direction not in ("long", "short"):
        raise StrategyError("direction must be long or short")

    entry = _require(data, "entry", "strategy")
    ex = _require(data, "exit", "strategy")
    if not isinstance(ex, dict):
        raise StrategyError("exit must be a mapping")
    _check_keys(ex, {"stop", "target", "max_bars", "when"}, "exit")
    if ex.get("stop") in (None, "", {}):
        raise StrategyError("exit.stop: choose a stop loss")
    stop = _level_spec(ex["stop"], "exit.stop")
    target = _level_spec(ex["target"], "exit.target", allow_r=True) if ex.get("target") else None
    max_bars = ex.get("max_bars")
    if max_bars is not None and (not _whole(max_bars) or max_bars < 1):
        raise StrategyError("exit.max_bars must be a whole number of 1 or more")

    r = data.get("risk") or {}
    _check_keys(
        r,
        {"account_size", "risk_per_trade_pct", "point_value", "size_step", "min_size", "max_size",
         "compound", "currency"},
        "risk",
    )
    # Built-in markets know their own point value, typical spread and funding; a blank uses those.
    from .recipes import MARKETS_BY_KEY
    builtin = MARKETS_BY_KEY.get(market.symbol, {}) if provider == "auto" else {}

    def given(d: dict, key: str, fallback):
        return fallback if d.get(key) in (None, "") and key not in ("account_size", "risk_per_trade_pct") \
            else d.get(key, fallback)

    if "currency" in r and r["currency"] in (None, ""):
        raise StrategyError("risk.currency: choose a currency")
    risk = Risk(
        account_size=_number(r.get("account_size", 10_000), "risk.account_size", 0.01),
        risk_per_trade_pct=_number(r.get("risk_per_trade_pct", 0.5), "risk.risk_per_trade_pct", 0.0001),
        point_value=_number(given(r, "point_value", builtin.get("point_value", 1.0)), "risk.point_value", 1e-9),
        size_step=_number(given(r, "size_step", 0.01), "risk.size_step", 0),
        min_size=_number(given(r, "min_size", 0.01), "risk.min_size", 0),
        max_size=_number(r.get("max_size"), "risk.max_size", 0, allow_none=True),
        compound=bool(r.get("compound", True)),
        currency=str(r.get("currency") or "GBP"),
    )
    if risk.risk_per_trade_pct > 10:
        raise StrategyError("risk.risk_per_trade_pct above 10% is almost certainly a typo")

    costs = data.get("costs") or {}
    _check_keys(costs, {"spread", "funding_pct", "use_data_spread", "commission", "commission_pct", "borrow_pct",
                        "slippage", "slippage_pct"}, "costs")
    spread = _number(given(costs, "spread", builtin.get("spread", 0.0)), "costs.spread", 0)
    funding = _number(given(costs, "funding_pct", builtin.get("funding", 0.0)), "costs.funding_pct", 0)
    if funding > 100:
        raise StrategyError("costs.funding_pct is a yearly percentage; above 100 is almost certainly a typo")
    commission = _number(given(costs, "commission", builtin.get("commission", 0.0)), "costs.commission", 0)
    commission_pct = _number(given(costs, "commission_pct", builtin.get("commission_pct", 0.0)), "costs.commission_pct", 0)
    if commission_pct > 5:
        raise StrategyError("costs.commission_pct is a percentage of each fill's value; above 5 is almost certainly a typo")
    borrow = _number(given(costs, "borrow_pct", builtin.get("borrow", 0.0)), "costs.borrow_pct", 0)
    if borrow > 100:
        raise StrategyError("costs.borrow_pct is a yearly percentage; above 100 is almost certainly a typo")
    slippage = _number(given(costs, "slippage", builtin.get("slippage", 0.0)), "costs.slippage", 0)
    slippage_pct = _number(given(costs, "slippage_pct", builtin.get("slippage_pct", 0.0)), "costs.slippage_pct", 0)
    if slippage_pct > 5:
        raise StrategyError("costs.slippage_pct is a percentage of each fill's price; above 5 is almost certainly a typo")

    alerts = data.get("alerts") or {}
    _check_keys(alerts, {"enabled", "priority"}, "alerts")

    try:
        return Strategy(
            id=sid,
            name=name,
            market=market,
            direction=direction,
            entry_spec=entry,
            stop_spec=stop,
            target_spec=target,
            max_bars=max_bars,
            exit_spec=ex.get("when"),
            risk=risk,
            spread=spread,
            funding_pct=funding,
            use_data_spread=bool(costs.get("use_data_spread", True)),
            commission=commission,
            commission_pct=commission_pct,
            borrow_pct=borrow,
            slippage=slippage,
            slippage_pct=slippage_pct,
            template=bool(data.get("template", False)),
            enabled=bool(data.get("enabled", True)),
            alerts_enabled=bool(alerts.get("enabled", True)),
            alert_priority=str(alerts.get("priority", "default")),
            source_hash=hashlib.sha256(raw_text.encode()).hexdigest()[:12] if raw_text else "",
            path=path,
        )
    except rules.RuleError as exc:
        raise StrategyError(str(exc)) from None


def load_strategy(path: str | Path) -> Strategy:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StrategyError(f"{p.name}: invalid YAML: {exc}") from None
    try:
        return strategy_from_dict(data, sid=_slug(p.stem), path=p, raw_text=text)
    except StrategyError as exc:
        raise StrategyError(f"{p.name}: {exc}") from None


def load_dir(directory: str | Path) -> tuple[list[Strategy], list[str]]:
    """Load every *.yaml / *.yml in a folder. Returns (strategies, error messages)."""
    d = Path(directory)
    out, errors = [], []
    if not d.exists():
        return out, errors  # nothing created yet
    for p in sorted(list(d.glob("*.yaml")) + list(d.glob("*.yml"))):
        try:
            out.append(load_strategy(p))
        except StrategyError as exc:
            errors.append(str(exc))
    return out, errors


# --------------------------------------------------------------------------- files

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
KEY_ORDER = ["name", "description", "template", "enabled", "recipe", "market", "direction", "entry",
             "exit", "risk", "costs", "alerts"]
FILE_HEADER = (
    "# Trading Alerts strategy. Created with the web interface; you can also edit it by hand.\n"
)


def slug(text: str) -> str:
    return _slug(text)[:64].strip("-") or "strategy"


def strategy_files(directory: str | Path) -> dict[str, Path]:
    """Map strategy id -> file path for every YAML file in a folder."""
    d = Path(directory)
    out: dict[str, Path] = {}
    if d.exists():
        for p in sorted(list(d.glob("*.yaml")) + list(d.glob("*.yml"))):
            out.setdefault(_slug(p.stem), p)
    return out


def read_raw(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def dump_strategy(data: dict) -> str:
    ordered = {k: data[k] for k in KEY_ORDER if k in data}
    ordered.update({k: v for k, v in data.items() if k not in ordered})
    return FILE_HEADER + yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=100)


def upgrade_legacy_market(data: dict) -> bool:
    """Point a v0.1 recipe strategy at its built-in market. Returns True if ``data`` changed.

    Before 0.2, strategies made from a recipe stored the market's Yahoo symbol
    (``symbol: ^FTSE, provider: yahoo``). Built-in markets now use ``provider: auto`` with the
    market's key, so they follow Settings, Price data and pick up the market's costs.
    Only strategies whose market is still exactly the recipe's Yahoo market are changed.
    """
    from .recipes import MARKETS_BY_KEY

    recipe, market = data.get("recipe"), data.get("market")
    if not isinstance(recipe, dict) or not isinstance(market, dict):
        return False
    builtin = MARKETS_BY_KEY.get(recipe.get("market"))
    if not builtin or str(market.get("provider", "yahoo")).lower() != "yahoo" or market.get("symbol") != builtin["symbol"]:
        return False
    market["provider"] = "auto"
    market["symbol"] = builtin["key"]
    market.setdefault("label", builtin["label"])
    return True


def migrate_strategy_files(directory: str | Path) -> list[str]:
    """Upgrade v0.1 strategy files in a folder in place. Returns the ids changed."""
    changed = []
    for sid, p in strategy_files(directory).items():
        try:
            data = read_raw(p)
        except (OSError, yaml.YAMLError):
            continue  # unreadable files are reported when they're loaded
        if upgrade_legacy_market(data):
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(dump_strategy(data), encoding="utf-8")
            tmp.replace(p)
            changed.append(sid)
    return changed


def check_runtime(strategy: Strategy) -> None:
    """Evaluate the rules on synthetic data to catch errors YAML checks can't (e.g. bad time zones)."""
    from . import data as data_mod
    from .sample import synthetic_bars

    probe = synthetic_bars(days=20, freq="5min", seed=1)
    strategy.prepare(data_mod.resample_ohlcv(probe, strategy.market.timeframe))
