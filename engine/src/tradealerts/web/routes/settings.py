"""Settings: alert channels, price data, how often to check, appearance and the weekly report."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from fastapi.responses import Response

from ... import data as data_mod
from ... import weekly
from ...config import ConfigError, data_settings, expand_env
from ...notify import Message, broadcast, build_notifiers
from ...workspace import Workspace
from ..strategy_io import now_iso
from ..common import (
    appearance_from_raw, checked_appearance, clean, err, raw_from_settings, settings_from_raw, workspace,
)

router = APIRouter()


def notifiers_for(ws: Workspace, specs: list[dict], expand: bool = False):
    """Alert channels from settings. ``expand`` for raw settings that may still hold ${VARS}."""
    try:
        return build_notifiers(expand_env(specs) if expand and ws.trusted else specs)
    except (ConfigError, ValueError, TypeError) as exc:
        err(422, f"Alert settings problem: {exc}")


@router.get("/api/settings")
def get_settings(ws: Workspace = Depends(workspace)):
    return settings_from_raw(ws.raw_config(), ws)


SECTION_NAMES = {"poll_minutes": "how often prices are checked", "notifiers": "where alerts are sent",
                 "report": "the weekly report", "data": "price data"}


@router.put("/api/settings")
def put_settings(body: dict = Body(...), ws: Workspace = Depends(workspace)):
    old = ws.raw_config()
    new = raw_from_settings(old, body, ws)
    notifiers_for(ws, new["notifiers"], expand=True)
    ws.write_config(new)
    changed = [name for key, name in SECTION_NAMES.items() if old.get(key) != new.get(key)]
    if changed:
        with ws.store() as store:
            text = ", ".join(changed)
            store.add_event("", now_iso(), "settings_changed", f"Settings changed\nChanged {text}.", False)
    return settings_from_raw(new, ws)


@router.put("/api/appearance")
def put_appearance(body: dict = Body(...), ws: Workspace = Depends(workspace)):
    raw = ws.raw_config()
    new = dict(raw)
    new["appearance"] = {**appearance_from_raw(raw), **checked_appearance(body)}
    ws.write_config(new)
    return new["appearance"]


@router.post("/api/test-alert")
def test_alert(ws: Workspace = Depends(workspace)):
    notifiers = notifiers_for(ws, ws.config().notifiers)
    errors = broadcast(notifiers, Message(
        "Trading Alerts test", "If you can read this, alerts are reaching you.", "default", ("bell",)
    ))
    return {"sent_to": [n.name for n in notifiers], "errors": errors}


@router.post("/api/data/test")
def test_data(body: dict = Body(default={}), ws: Workspace = Depends(workspace)):
    """Fetch a few days of US 500 (or the given market) to check the price data source works."""
    c = ws.config()
    try:
        if body.get("data"):
            given = dict(body["data"])
            if not given.get("oanda_token"):
                given["oanda_token"] = c.data.get("oanda_token", "")  # use the saved one
            data_cfg = data_settings(given, ws.limits.sources)
        else:
            data_cfg = c.data
    except ConfigError as exc:
        err(422, str(exc))
    market = str(body.get("market") or "us500")
    try:
        df, _ = data_mod.MarketProvider(data_cfg, c.cache_dir).fetch_bars(market, "1h", 5)
    except data_mod.DataError as exc:
        err(422, str(exc))
    except Exception as exc:
        err(502, f"Couldn't load prices: {exc}")
    if df.empty:
        err(422, "The data source returned no prices")
    spread = float(df["spread"].median()) if "spread" in df.columns and df["spread"].notna().any() else None
    return clean({"candles": len(df), "last": df.index[-1].isoformat(), "last_close": float(df["close"].iloc[-1]),
                  "spread": spread, "source": data_cfg["source"]})


# ---------------------------------------------------------------- weekly report


@router.get("/api/report")
def report_json(days: int = 7, ws: Workspace = Depends(workspace)):
    with ws.store() as store:
        return clean(weekly.build(store, max(1, min(days, 90))))


@router.get("/api/report.html")
def report_page(days: int = 7, ws: Workspace = Depends(workspace)):
    with ws.store() as store:
        page = weekly.as_html(weekly.build(store, max(1, min(days, 90))), standalone=True)
    return Response(page, media_type="text/html")


@router.post("/api/report/send")
def report_send(ws: Workspace = Depends(workspace)):
    notifiers = notifiers_for(ws, ws.config().notifiers)
    with ws.store() as store:
        errors = weekly.send(store, notifiers)
    return {"sent_to": [n.name for n in notifiers], "errors": errors}
