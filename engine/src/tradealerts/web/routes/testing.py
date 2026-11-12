"""Testing tools: backtest, compare settings (sweep), luck check, strategies together."""

from __future__ import annotations

import copy

from fastapi import APIRouter, Body, Depends, Request

from ... import __version__, compute, versions
from ...jobs import Busy
from ...recipes import RecipeError, build_strategy
from ...strategy import ID_RE, read_raw, strategy_from_dict
from ...workspace import Workspace
from ..common import CURRENCY_SYMBOLS, ctx_of, err, workspace
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


def sweep_variants(data: dict, setting: dict, values: list) -> list:
    """(value, strategy dict or error text) for each value of one setting."""
    kind = setting.get("kind")
    if kind not in ("knob", "path"):
        err(422, "Choose a setting to test")
    path = setting.get("path")
    if kind == "path" and (not isinstance(path, list) or not path or len(path) > 12):
        err(422, "Choose a setting to test")
    out = []
    for v in values:
        d = copy.deepcopy(data)
        if kind == "knob":
            r = d.get("recipe") or {}
            settings = dict(r.get("settings") or {})
            settings[str(setting.get("key"))] = v
            try:
                built = build_strategy(str(r.get("id", "")), str(r.get("market", "")), settings)
            except RecipeError as exc:
                out.append((v, str(exc)))
                continue
            for k in ("direction", "entry", "exit", "recipe"):
                d[k] = built[k]
            d["market"] = {**(d.get("market") or {}), "timeframe": built["market"]["timeframe"]}
        else:
            set_path(d, path, v)
        out.append((v, d))
    return out


@router.post("/api/sweep")
def sweep_settings(request: Request, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    s = validate(ws, body.get("data"))
    values = body.get("values")
    if not isinstance(values, list) or len(values) < 2:
        err(422, "Give at least two values to compare")
    if len(values) > MAX_SWEEP:
        err(422, f"Compare at most {MAX_SWEEP} values at once. Use a bigger step.")
    for v in values:
        if not isinstance(v, (int, float, str, bool)):
            err(422, "Values must be numbers or options")
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
    return _run(request, ws, compute.portfolio, parts, float(balance), sym, notes)
