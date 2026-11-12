"""Reference data, the guided ideas and strategy files: list, read, save, import, export, delete."""

from __future__ import annotations

import shutil
import time
from importlib import resources
from typing import Any

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response

from ... import indicators, rules
from ...recipes import MARKETS, RECIPES, RecipeError, build_strategy, preview_strategy, still_matches_recipe
from ...strategy import (ID_RE, StrategyError, dump_strategy, load_strategy, read_raw, strategy_files,
                         upgrade_legacy_market)
from ...workspace import Workspace
from ..common import (
    INDICATOR_LABELS, INDICATOR_SHORT, MARKET_PRESETS, TIMEFRAMES, TIMEZONES, clean, ctx_of, err, workspace,
)
from ..runs_view import run_health, run_series, run_summary
from ... import versions
from ..strategy_io import (alerts_on, check_running_limit, record_version, save, save_new, strategy_path, validate,
                          write)

TEMPLATES = resources.files("tradealerts") / "templates"

router = APIRouter()


def tidy_recipe(data: Any) -> Any:
    """Drop the recipe link once the rules have been edited by hand."""
    if isinstance(data, dict) and "recipe" in data and not still_matches_recipe(data):
        data = dict(data)
        data.pop("recipe")
    return data


# ---------------------------------------------------------------- reference data


@router.get("/api/meta")
def meta(ws: Workspace = Depends(workspace)):
    inds = []
    for name in sorted(indicators.REGISTRY, key=lambda n: INDICATOR_LABELS.get(n, n)):
        fn = indicators.REGISTRY[name]
        params = indicators.params_for(name)
        inds.append({
            "name": name,
            "label": INDICATOR_LABELS.get(name, name),
            "short": INDICATOR_SHORT.get(name, name),
            "description": (fn.__doc__ or "").strip().split("\n")[0],
            "params": clean(params),
            "types": {k: type(v).__name__ for k, v in params.items()},
        })
    return {
        "indicators": inds,
        "fields": list(indicators.PRICE_FIELDS),
        "ops": list(rules.OPS),
        "timeframes": TIMEFRAMES,
        "presets": MARKET_PRESETS,
        "timezones": TIMEZONES,
        "currencies": ["GBP", "USD", "EUR"],
        "priorities": ["min", "low", "default", "high", "urgent"],
        "limits": ws.limits.public(),
    }


@router.get("/api/examples")
def examples():
    out = []
    for f in sorted((TEMPLATES / "strategies").iterdir(), key=lambda f: f.name):
        if f.name.endswith(".yaml"):
            d = yaml.safe_load(f.read_text(encoding="utf-8"))
            out.append({"file": f.name[:-5], "name": d.get("name"), "description": d.get("description", "")})
    return out


@router.get("/api/examples/{name}")
def example(name: str):
    if not ID_RE.match(name.replace("_", "-")):
        err(404, "No example with that name")
    f = TEMPLATES / "strategies" / f"{name}.yaml"
    if not f.is_file():
        err(404, "No example with that name")
    return yaml.safe_load(f.read_text(encoding="utf-8"))


@router.get("/api/recipes")
def recipes():
    return {"recipes": [r.public() for r in RECIPES], "markets": MARKETS}


@router.post("/api/recipes/preview")
def preview_recipe(body: dict = Body(...)):
    """The sentence-while-you-type version of a guided strategy: blanks stay blank."""
    try:
        return clean(preview_strategy(str(body.get("recipe", "")), str(body.get("market", "")), body.get("settings") or {}))
    except RecipeError as exc:
        err(422, str(exc))


@router.post("/api/recipes/build")
def build_recipe(body: dict = Body(...), ws: Workspace = Depends(workspace)):
    try:
        data = build_strategy(str(body.get("recipe", "")), str(body.get("market", "")), body.get("settings") or {})
    except RecipeError as exc:
        err(422, str(exc))
    validate(ws, data)
    return data


# ---------------------------------------------------------------- strategies


@router.get("/api/strategies")
def list_strategies(request: Request, ws: Workspace = Depends(workspace)):
    c = ws.config()
    runner = ctx_of(request).runner_status(ws)
    with ws.store(create=False) as store:
        items = []
        files = strategy_files(c.strategies_dir)
        for sid, path in files.items():
            item: dict[str, Any] = {"id": sid, "file": path.name}
            try:
                item["data"] = read_raw(path)
                load_strategy(path)
                item["error"] = None
            except Exception as exc:
                item.setdefault("data", {})
                item["error"] = str(exc)
            if store:
                versions.sync(store, sid, path, ws.limits.max_versions)
            run = store.active_run(sid) if store else None
            if run:
                summary = run_summary(store, run, with_series=False)
                summary["health"] = run_health(summary, files, runner)
                item["live"] = {"run": summary}
            else:
                item["live"] = {}
            if run:
                eq = store.equity(run["id"])
                item["spark"] = run_series(run, eq, eq[-1][1] if eq else run["start_balance"], 40)
            items.append(item)
        events = store.recent_events(1) if store else []
    return clean({"strategies": items, "last_event": events[0] if events else None})


@router.get("/api/strategies/{sid}")
def get_strategy(sid: str, ws: Workspace = Depends(workspace)):
    p = strategy_path(ws, sid)
    data = read_raw(p)
    try:
        load_strategy(p)
        problem = None
    except StrategyError as exc:
        problem = str(exc)
    run = None
    version = None
    with ws.store(create=False) as store:
        if store is not None:
            v = versions.sync(store, sid, p, ws.limits.max_versions)
            version = v["version_no"] if v else None
            active = store.active_run(sid)
            run = run_summary(store, active, with_series=False) if active else None
    return clean({"id": sid, "file": p.name, "data": data, "error": problem, "run": run, "version": version})


@router.post("/api/validate")
def validate_draft(body: dict = Body(...), ws: Workspace = Depends(workspace)):
    try:
        validate(ws, body.get("data"))
    except HTTPException as exc:
        return {"ok": False, "error": exc.detail}
    return {"ok": True, "error": None}


@router.post("/api/yaml")
def to_yaml(body: dict = Body(...)):
    """The editor's data as the YAML it would be saved as (Advanced: Edit YAML)."""
    data = body.get("data")
    if not isinstance(data, dict):
        err(422, "Nothing to show")
    return {"yaml": dump_strategy(data)}


@router.post("/api/yaml/parse")
def from_yaml(body: dict = Body(...), ws: Workspace = Depends(workspace)):
    """YAML typed in the editor, back to data; also checks the rules. Never raises for user mistakes."""
    text = body.get("yaml")
    if not isinstance(text, str) or len(text) > 200_000:
        return {"ok": False, "error": "Nothing to read", "data": None}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        return {"ok": False, "error": f"Not valid YAML{where}: {getattr(exc, 'problem', exc)}", "data": None}
    if not isinstance(data, dict):
        return {"ok": False, "error": "A strategy is a set of named settings (name, market, entry, ...)", "data": None}
    try:
        validate(ws, data)
    except HTTPException as exc:
        return {"ok": False, "error": exc.detail, "data": data}
    return {"ok": True, "error": None, "data": data}


@router.post("/api/strategies", status_code=201)
def create_strategy(request: Request, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    data = tidy_recipe(body.get("data"))
    validate(ws, data)
    if alerts_on(data):
        check_running_limit(ws, "")
    sid = save_new(ws, data)
    record_version(ws, sid, "Created")
    if alerts_on(data):
        ctx_of(request).wake(ws)
    return {"id": sid}


@router.get("/api/strategies/{sid}/export")
def export_strategy(sid: str, ws: Workspace = Depends(workspace)):
    """The strategy's rules as a file, without any results or account details."""
    data = read_raw(strategy_path(ws, sid))
    data.pop("enabled", None)
    data["template"] = True
    text = "# Trading Alerts strategy. Import it from the Strategies page.\n" + dump_strategy(data)
    return Response(text, media_type="application/x-yaml",
                    headers={"Content-Disposition": f'attachment; filename="{sid}.yaml"'})


@router.post("/api/strategies/import", status_code=201)
def import_strategy(body: dict = Body(...), ws: Workspace = Depends(workspace)):
    """From an exported file (yaml text) or a share link (data). Always arrives as a draft."""
    if isinstance(body.get("yaml"), str):
        if len(body["yaml"]) > 200_000:
            err(422, "That file is too big to be a strategy")
        try:
            data = yaml.safe_load(body["yaml"])
        except yaml.YAMLError:
            err(422, "That file isn't a Trading Alerts strategy (it isn't valid YAML)")
    else:
        data = body.get("data")
    if not isinstance(data, dict) or "entry" not in data or "market" not in data:
        err(422, "That isn't a Trading Alerts strategy")
    data = dict(data)
    upgrade_legacy_market(data)  # files exported before 0.2
    data.pop("enabled", None)
    data["template"] = True
    data["name"] = str(data.get("name") or "Imported strategy")[:120]
    data = tidy_recipe(data)
    validate(ws, data)
    sid = save_new(ws, data)
    record_version(ws, sid, "Imported")
    return {"id": sid}


@router.put("/api/strategies/{sid}")
def save_strategy(sid: str, request: Request, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    """Save a strategy. ``on_running`` (keep / restart) says what happens to a running forward
    test when the trading rules changed; without it the app is asked to choose (409)."""
    p = strategy_path(ws, sid)
    data = tidy_recipe(body.get("data"))
    validate(ws, data, sid)
    was_on = alerts_on(read_raw(p))
    out = save(ws, sid, data, body.get("on_running"))
    if (alerts_on(data) and not was_on) or out["forward_test"] == "restarted":
        ctx_of(request).wake(ws)  # start its forward test now rather than at the next check
    return out


@router.get("/api/strategies/{sid}/versions")
def strategy_versions(sid: str, ws: Workspace = Depends(workspace)):
    p = strategy_path(ws, sid)
    with ws.store() as store:
        current = versions.sync(store, sid, p, ws.limits.max_versions)
        return clean({"versions": store.versions(sid), "current": current["version_no"] if current else None,
                      "kept": ws.limits.max_versions})


@router.get("/api/strategies/{sid}/versions/{no}")
def strategy_version(sid: str, no: int, ws: Workspace = Depends(workspace)):
    strategy_path(ws, sid)
    with ws.store(create=False) as store:
        v = store.version(sid, no) if store else None
    if not v:
        err(404, "That version isn't kept any more")
    return clean({**v, "data": versions.parse(v["yaml"])})


@router.post("/api/strategies/{sid}/versions/{no}/restore")
def restore_version(sid: str, no: int, request: Request, body: dict = Body(default={}), ws: Workspace = Depends(workspace)):
    """Make an old version current again. It's saved as a new version, so nothing is lost."""
    p = strategy_path(ws, sid)
    with ws.store(create=False) as store:
        v = store.version(sid, no) if store else None
    if not v:
        err(404, "That version isn't kept any more")
    data = versions.parse(v["yaml"])
    current = read_raw(p)
    for k in ("template", "enabled"):  # alerts stay as they are now
        data.pop(k, None)
        if k in current:
            data[k] = current[k]
    validate(ws, data, sid)
    out = save(ws, sid, data, body.get("on_running"), note=f"Restored version {no}")
    if out["forward_test"] == "restarted":
        ctx_of(request).wake(ws)
    return out


@router.delete("/api/strategies/{sid}")
def delete_strategy(sid: str, ws: Workspace = Depends(workspace)):
    p = strategy_path(ws, sid)
    c = ws.config()
    trash = c.data_dir / "deleted-strategies"
    trash.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), trash / f"{sid}-{time.strftime('%Y%m%d-%H%M%S')}.yaml")
    with ws.store(create=False) as store:
        if store is not None:
            store.stop_run(sid)
            store.reset(sid)
            store.delete_versions(sid)  # forward tests keep their own copy of the rules they ran on
    return {"deleted": sid}


@router.post("/api/strategies/{sid}/reset")
def reset_strategy(sid: str, ws: Workspace = Depends(workspace)):
    strategy_path(ws, sid)
    with ws.store() as store:
        store.reset(sid)
    return {"reset": sid}
