"""Testing tools: backtest, compare settings (sweep), optimiser, luck check, strategies together."""

from __future__ import annotations

import copy
import os

from fastapi import APIRouter, Body, Depends, Request

from ... import __version__, analysis, compute, versions
from ...jobs import Busy
from ...recipes import RecipeError, build_strategy
from ...strategy import ID_RE, read_raw, strategy_from_dict
from ...workspace import Workspace
from ..common import CURRENCY_SYMBOLS, clean, ctx_of, err, workspace
from ..prices import load_period
from ..strategy_io import load, now_iso, strategy_path, validate

router = APIRouter()

MAX_SWEEP = 25


def _run(request: Request, ws: Workspace, fn, *args):
    try:
        return ctx_of(request).jobs.run(ws.owner, fn, *args)
    except Busy as exc:
        err(429, str(exc))
    except ValueError as exc:
        err(422, str(exc))


@router.post("/api/backtest")
def backtest(request: Request, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    s = validate(ws, body.get("data"))
    df, fine, test_start, notes = load_period(ctx_of(request), ws, s, body)
    out = _run(request, ws, compute.backtest, body["data"], df, fine, test_start, notes)
    out["meta"] = stamp(ws, s, body, out["metrics"])
    return out


@router.post("/api/signals/count")
def count_signals(request: Request, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    """How often the entry rules fired on past data, without running the trade simulator."""
    s = validate(ws, body.get("data"))
    df, _fine, test_start, notes = load_period(ctx_of(request), ws, s, body)
    p = s.prepare(df)
    mask = (df.index >= test_start) if test_start is not None else df.index == df.index
    entry = p.entry[mask]
    return {
        "signals": int(entry.sum()),
        "candles": int(mask.sum()),
        "start": df.index[mask][0].isoformat() if mask.any() else None,
        "end": df.index[mask][-1].isoformat() if mask.any() else None,
        "notes": notes,
    }


SOURCE_NAMES = {"dukascopy": "Dukascopy", "yahoo": "Yahoo Finance", "oanda": "OANDA", "csv": "CSV file"}


def stamp(ws: Workspace, s, body: dict, metrics: dict) -> dict:
    """Everything needed to run the same backtest again and get the same answer."""
    version = None
    sid = body.get("strategy_id")
    if isinstance(sid, str) and ID_RE.match(sid):
        with ws.store(create=False) as store:
            v = versions.version_with_rules(store, sid, body["data"]) if store else None
            version = v["version_no"] if v else None
    source = ws.config().data.get("source", "dukascopy") if s.market.provider == "auto" else s.market.provider
    return {
        "engine_version": __version__,
        "rules_hash": versions.rules_fingerprint(body["data"]),
        "version": version,                # saved version these exact rules match, if any
        "market": s.market.label,
        "symbol": s.market.symbol,
        "source": SOURCE_NAMES.get(source, source),
        "timeframe": s.market.timeframe,
        "start": metrics.get("start"),
        "end": metrics.get("end"),
        "warmup_candles": metrics.get("warmup_candles"),
        "spread": s.spread,
        "used_data_spread": metrics.get("used_data_spread"),
        "funding_pct": s.funding_pct,
        "slippage": s.slippage,
        "slippage_pct": s.slippage_pct,
        "tested_at": now_iso(),
    }


def set_path(data: dict, path: list, value) -> None:
    node = data
    for key in path[:-1]:
        if isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
            node = node[key]
        elif isinstance(node, dict) and isinstance(key, str) and key in node:
            node = node[key]
        else:
            err(422, "That setting isn't in this strategy any more")
    last = path[-1]
    if isinstance(node, dict) and isinstance(last, str):
        node[last] = value
    elif isinstance(node, list) and isinstance(last, int) and 0 <= last < len(node):
        node[last] = value
    else:
        err(422, "That setting isn't in this strategy any more")


def check_setting(setting: dict) -> None:
    kind = setting.get("kind")
    if kind not in ("knob", "path"):
        err(422, "Choose a setting to test")
    path = setting.get("path")
    if kind == "path" and (not isinstance(path, list) or not path or len(path) > 12):
        err(422, "Choose a setting to test")


def apply_setting(d: dict, setting: dict, v):
    """Set one setting to ``v`` in strategy dict ``d`` (in place). Returns an error message for
    a value the recipe rejects, else None."""
    if setting.get("kind") == "knob":
        r = d.get("recipe") or {}
        settings = dict(r.get("settings") or {})
        settings[str(setting.get("key"))] = v
        try:
            built = build_strategy(str(r.get("id", "")), str(r.get("market", "")), settings)
        except RecipeError as exc:
            return str(exc)
        for k in ("direction", "entry", "exit", "recipe"):
            d[k] = built[k]
        d["market"] = {**(d.get("market") or {}), "timeframe": built["market"]["timeframe"]}
    else:
        set_path(d, setting.get("path"), v)
    return None


def sweep_variants(data: dict, setting: dict, values: list) -> list:
    """(value, strategy dict or error text) for each value of one setting."""
    check_setting(setting)
    out = []
    for v in values:
        d = copy.deepcopy(data)
        problem = apply_setting(d, setting, v)
        out.append((v, problem if problem else d))
    return out


def check_values(values) -> None:
    if not isinstance(values, list) or not values:
        err(422, "Give at least one value for each setting")
    if len(values) > MAX_SWEEP:
        err(422, f"Compare at most {MAX_SWEEP} values of one setting. Use a bigger step.")
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            err(422, "Values must be numbers or options")


@router.post("/api/sweep")
def sweep_settings(request: Request, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    s = validate(ws, body.get("data"))
    values = body.get("values")
    if not isinstance(values, list) or len(values) < 2:
        err(422, "Give at least two values to compare")
    check_values(values)
    variants = sweep_variants(body["data"], body.get("setting") or {}, values)
    warmup = s.warmup_candles  # enough history for the variant with the longest indicators
    for _, d in variants:
        try:
            warmup = max(warmup, strategy_from_dict(copy.deepcopy(d)).warmup_candles)
        except Exception:  # an invalid variant is reported by the sweep itself
            pass
    df, fine, test_start, notes = load_period(ctx_of(request), ws, s, body, warmup=warmup)
    return _run(request, ws, compute.sweep, variants, df, fine, test_start, s.risk.symbol, notes)


@router.post("/api/luck")
def luck(request: Request, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    s = validate(ws, body.get("data"))
    df, fine, test_start, notes = load_period(ctx_of(request), ws, s, body)
    return _run(request, ws, compute.luck, body["data"], df, fine, test_start, notes)


@router.post("/api/portfolio")
def portfolio(request: Request, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    ids = body.get("ids")
    if not isinstance(ids, list) or len(ids) < 1:
        err(422, "Choose at least one strategy")
    if len(ids) > 12:
        err(422, "Choose 12 strategies at most")
    if len(set(map(str, ids))) != len(ids):
        err(422, "Each strategy can only be chosen once")
    balance = body.get("balance")
    if isinstance(balance, bool) or not isinstance(balance, (int, float)) or balance <= 0:
        err(422, "Fill in the starting balance")
    parts, notes, currencies = [], [], set()
    for sid in ids:
        if not isinstance(sid, str):
            err(422, "Unknown strategy")
        s = load(ws, sid)
        currencies.add(s.risk.currency)
        df, fine, test_start, n = load_period(ctx_of(request), ws, s, body)
        notes += [f"{s.name}: {x}" for x in n[:1]]
        parts.append((sid, s.name, read_raw(strategy_path(ws, sid)), df, fine, test_start))
    if len(currencies) > 1:
        err(422, "These strategies use different currencies. Choose ones in the same currency.")
    sym = CURRENCY_SYMBOLS.get(currencies.pop(), "")
    return _run(request, ws, compute.portfolio, parts, float(balance), sym, notes, _portfolio_options(body, ids))


def _portfolio_options(body: dict, ids: list[str]) -> dict:
    """Weighting and shared-account settings for /api/portfolio, checked with plain messages."""
    weighting = body.get("weighting") or "equal"
    if weighting not in analysis.WEIGHTING_MODES:
        err(422, "Choose a weighting: equal, inverse volatility or custom")
    weights = None
    if weighting == "custom":
        weights = body.get("weights")
        if not isinstance(weights, dict):
            err(422, "Fill in a share (%) for every chosen strategy")
        for sid in ids:
            v = weights.get(sid)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                err(422, "Fill in a share (%) of 0 or more for every chosen strategy")
        if sum(weights.get(sid, 0) for sid in ids) <= 0:
            err(422, "The shares must add up to more than 0")
        weights = {sid: float(weights[sid]) for sid in ids}
    shared = bool(body.get("shared"))
    max_open_trades = body.get("max_open_trades")
    if max_open_trades not in (None, ""):
        if isinstance(max_open_trades, bool) or not isinstance(max_open_trades, (int, float)) \
                or max_open_trades != int(max_open_trades) or max_open_trades < 1:
            err(422, "Most trades open at once must be a whole number of 1 or more")
        max_open_trades = int(max_open_trades)
    else:
        max_open_trades = None
    max_open_risk_pct = body.get("max_open_risk_pct")
    if max_open_risk_pct not in (None, ""):
        if isinstance(max_open_risk_pct, bool) or not isinstance(max_open_risk_pct, (int, float)) or max_open_risk_pct <= 0:
            err(422, "Most at risk at once must be a % above 0")
        max_open_risk_pct = float(max_open_risk_pct)
    else:
        max_open_risk_pct = None
    if not shared and (max_open_trades is not None or max_open_risk_pct is not None):
        err(422, "The caps only apply when the strategies share one account")
    return {"weighting": weighting, "weights": weights, "shared": shared,
            "max_open_trades": max_open_trades, "max_open_risk_pct": max_open_risk_pct}


# --------------------------------------------------------------------------- optimiser


def combos_cap(ctx) -> int:
    return analysis.MAX_COMBOS.get(getattr(ctx, "edition", "cloud"), analysis.MAX_COMBOS["cloud"])


def optimiser_variants(data: dict, settings: list[dict], combos: list[tuple]) -> list:
    """(values, strategy dict or error text) for each combination; settings are applied in order."""
    out = []
    for values in combos:
        d = copy.deepcopy(data)
        problem = None
        for setting, v in zip(settings, values):
            problem = apply_setting(d, setting["setting"], v)
            if problem:
                break
        out.append((values, problem if problem else d))
    return out


@router.post("/api/optimise")
def optimise(request: Request, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    """Start an optimiser run in the background. Returns ``{"job": id}``; poll ``GET /api/jobs/{id}``."""
    ctx = ctx_of(request)
    s = validate(ws, body.get("data"))
    settings = body.get("settings")
    if not isinstance(settings, list) or not settings:
        err(422, "Choose at least one setting to optimise")
    if len(settings) > analysis.MAX_OPT_SETTINGS:
        err(422, f"Optimise at most {analysis.MAX_OPT_SETTINGS} settings at once")
    axes = []
    for item in settings:
        if not isinstance(item, dict):
            err(422, "Choose a setting to test")
        check_setting(item.get("setting") or {})
        check_values(item.get("values"))
        axes.append(list(item["values"]))
    score = body.get("score") or "return"
    if score not in analysis.SCORES:
        err(422, "Choose how to rank the results")
    min_trades = body.get("min_trades", analysis.MIN_TRADES)
    if isinstance(min_trades, bool) or not isinstance(min_trades, int) or min_trades < 0:
        err(422, "The minimum number of trades must be a whole number")
    holdout = body.get("holdout_pct", 0) or 0
    if isinstance(holdout, bool) or not isinstance(holdout, (int, float)) or not 0 <= holdout <= analysis.MAX_HOLDOUT_PCT:
        err(422, f"Keep back between 0 and {analysis.MAX_HOLDOUT_PCT}% of the period")
    folds = body.get("walk_forward", 0) or 0
    if isinstance(folds, bool) or not isinstance(folds, int) or folds not in (0, *analysis.WALK_FORWARD_FOLDS):
        err(422, f"The walk-forward check uses {analysis.WALK_FORWARD_FOLDS[0]} to {analysis.WALK_FORWARD_FOLDS[-1]} stretches")
    try:
        combos = analysis.grid(axes, combos_cap(ctx))
    except ValueError as exc:
        err(422, str(exc))
    variants = optimiser_variants(body["data"], settings, combos)
    warmup = s.warmup_candles  # enough history for the variant with the longest indicators, per setting
    for item in settings:
        for v, d in sweep_variants(body["data"], item["setting"], item["values"]):
            try:
                warmup = max(warmup, strategy_from_dict(copy.deepcopy(d)).warmup_candles) if isinstance(d, dict) else warmup
            except Exception:  # an invalid variant is reported by the optimiser itself
                pass
    df, fine, test_start, notes = load_period(ctx, ws, s, body, warmup=warmup)
    workers = max(1, os.cpu_count() or 1) if ctx.edition == "selfhost" else 1
    extra = {"notes": notes, "currency": s.risk.symbol, "combinations": len(combos)}
    try:
        job = ctx.jobs.start(ws.owner, analysis.optimise, variants, df, fine, test_start, score, int(min_trades), workers, extra,
                             float(holdout), int(folds))
    except Busy as exc:
        err(429, str(exc))
    return {"job": job, "combinations": len(combos)}


@router.get("/api/jobs/{job_id}")
def job_status(request: Request, job_id: str, ws: Workspace = Depends(workspace)):
    st = ctx_of(request).jobs.status(ws.owner, job_id)
    if st is None:
        err(404, "That run has finished and been cleared, or never existed")
    return clean(st)


@router.post("/api/jobs/{job_id}/cancel")
def job_cancel(request: Request, job_id: str, ws: Workspace = Depends(workspace)):
    if not ctx_of(request).jobs.cancel(ws.owner, job_id):
        err(404, "That run has finished and been cleared, or never existed")
    return {"ok": True}
