"""Reading, checking and saving strategy files inside a workspace."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from .. import rules
from ..strategy import (
    ID_RE, Strategy, StrategyError, check_runtime, dump_strategy, load_strategy, read_raw, slug, strategy_files,
    strategy_from_dict,
)
from ..workspace import Workspace
from .common import err

SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,99}\.csv$")


def strategy_path(ws: Workspace, sid: str) -> Path:
    if not ID_RE.match(sid):
        err(404, "No strategy with that id")
    p = strategy_files(ws.config().strategies_dir).get(sid)
    if not p:
        err(404, "No strategy with that id")
    return p


def validate(ws: Workspace, data: Any, sid: str = "draft") -> Strategy:
    if not isinstance(data, dict):
        err(422, "Strategy must be an object")
    m = data.get("market") if isinstance(data.get("market"), dict) else {}
    provider = str(m.get("provider", "yahoo")).lower()
    if provider in ("dukascopy", "yahoo", "oanda") and provider not in ws.limits.sources:
        names = {"dukascopy": "Dukascopy", "yahoo": "Yahoo", "oanda": "OANDA"}
        err(422, f"{names[provider]} prices aren't available here. Choose one of the markets in the list.")
    if not ws.limits.csv_paths and provider == "csv":
        name = str(m.get("symbol") or "")
        if not SAFE_FILE.match(name):
            err(422, "Choose one of the CSV files you've uploaded")
        if not (ws.config().csv_root / name).is_file():
            err(422, f"There's no uploaded file called {name}. Upload it under Markets, My CSV files.")
    try:
        s = strategy_from_dict(copy.deepcopy(data), sid=sid)
        check_runtime(s)
    except StrategyError as exc:
        err(422, str(exc))
    except (rules.RuleError, ValueError, KeyError, TypeError) as exc:
        err(422, f"Rule problem: {exc}")
    except Exception as exc:  # e.g. an unusual time zone
        err(422, f"Rule problem: {exc}")
    return s


def save_new(ws: Workspace, data: dict) -> str:
    c = ws.config()
    c.strategies_dir.mkdir(parents=True, exist_ok=True)
    existing = strategy_files(c.strategies_dir)
    if ws.limits.max_strategies is not None and len(existing) >= ws.limits.max_strategies:
        err(409, f"You have {len(existing)} strategies, the most this plan allows. Delete one you don't need first.")
    base = slug(str(data.get("name", "strategy")))
    sid, n = base, 2
    while sid in existing or (c.strategies_dir / f"{sid}.yaml").exists():
        sid, n = f"{base}-{n}", n + 1
    write(c.strategies_dir / f"{sid}.yaml", data)
    return sid


def write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump_strategy(data), encoding="utf-8")
    tmp.replace(path)


def alerts_on(data: dict) -> bool:
    return data.get("template") is False and data.get("enabled", True) is not False


def running_ids(ws: Workspace) -> set[str]:
    """Strategies with a running forward test or alerts switched on (alerts always run a test)."""
    c = ws.config()
    files = strategy_files(c.strategies_dir)
    out = set()
    with ws.store(create=False) as store:
        if store is not None:
            out |= {sid for sid in store.active_runs() if sid in files}
    for sid, p in files.items():
        try:
            if alerts_on(read_raw(p)):
                out.add(sid)
        except Exception:
            continue
    return out


def check_running_limit(ws: Workspace, sid: str) -> None:
    """Refuse to start one more forward test (or switch on one more set of alerts) over the plan's limit."""
    limit = ws.limits.max_running
    if limit is None:
        return
    running = running_ids(ws)
    if sid not in running and len(running) >= limit:
        err(409, f"You already have {len(running)} forward tests or alerts running, the most this plan allows. "
                 "Stop one first.")


def load(ws: Workspace, sid: str) -> Strategy:
    try:
        return load_strategy(strategy_path(ws, sid))
    except StrategyError as exc:
        err(422, str(exc))


def enforce_running_limit(ws: Workspace) -> list[str]:
    """After a restore: switch off alerts and stop forward tests beyond the plan's limit. Returns what was stopped."""
    limit = ws.limits.max_running
    if limit is None:
        return []
    c = ws.config()
    files = strategy_files(c.strategies_dir)
    stopped = []
    with ws.store() as store:
        active = [sid for sid in store.active_runs() if sid in files]
        keep = set(active[:limit])
        for sid in active[limit:]:
            store.stop_run(sid)
            stopped.append(sid)
        for sid, p in files.items():
            data = read_raw(p)
            if alerts_on(data) and sid not in keep:
                if len(keep) < limit:
                    keep.add(sid)
                    continue
                data["enabled"] = False
                write(p, data)
                stopped.append(sid)
    return sorted(set(stopped))
