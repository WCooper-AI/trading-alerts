"""Testing tools: backtest, compare settings (sweep), luck check, strategies together."""

from __future__ import annotations

import copy

from fastapi import APIRouter, Body, Depends, Request

from ... import compute
from ...jobs import Busy
from ...recipes import RecipeError, build_strategy
from ...strategy import read_raw, strategy_from_dict
from ...workspace import Workspace
from ..common import CURRENCY_SYMBOLS, ctx_of, err, workspace
from ..prices import load_period
from ..strategy_io import load, strategy_path, validate

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
    return _run(request, ws, compute.backtest, body["data"], df, fine, test_start, notes)


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
