"""Live charts, market statistics, price alerts and your own CSV price files."""

from __future__ import annotations

import os
import zoneinfo

import pandas as pd
from fastapi import APIRouter, Body, Depends, Request

from ... import compute, markethours, timeframes
from ... import data as data_mod
from ...recipes import MARKETS, MARKETS_BY_KEY
from ...workspace import Workspace
from ..common import clean, ctx_of, err, workspace
from ..strategy_io import SAFE_FILE, load

router = APIRouter()


@router.get("/api/market-stats")
def market_stats(request: Request, market: str, years: int = 2, tz: str = "Europe/London",
                 ws: Workspace = Depends(workspace)):
    """How a built-in market has tended to move: by hour, session, news. History only."""
    from ... import marketstats, news

    mk = MARKETS_BY_KEY.get(market)
    if not mk:
        err(404, "Choose one of the built-in markets")
    years = max(1, min(int(years), 5))
    try:
        zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        tz = "Europe/London"
    c = ws.config()
    provider = data_mod.provider_for(c, "auto")
    source = c.data.get("source", "dukascopy")

    def build():
        df, _ = provider.fetch_bars(market, "1h", years * 365 + 7, "new_york")
        if df.empty:
            raise data_mod.DataError("No prices for that market yet")
        ev = news.events(df.index[0], df.index[-1])
        if len(ev):
            ev = ev[(ev["impact"] == "high") & (ev["currency"].isin(mk.get("news", [])))]
        out = marketstats.compute(df, ev, tz=tz)
        out.update(market=market, label=mk["label"], source=source, years=years,
                   stale=bool(getattr(provider, "stale", False)))
        return out

    try:
        key = (market, years, tz, source, str(c.cache_dir),
               c.data.get("oanda_token") if source == "oanda" else "")
        out, _ = ctx_of(request).stats_cache.get_or_last(key, build)
    except (data_mod.DataError, ValueError) as exc:
        err(422, str(exc))
    except Exception as exc:
        err(502, f"Couldn't work out the stats: {exc}")
    return clean(out)


@router.get("/api/candles")
def candles(request: Request, market: str = "", strategy: str = "", timeframe: str = "", bars: int = 200,
            ws: Workspace = Depends(workspace)):
    """Recent candles for the live chart, including the one still forming."""
    c = ws.config()
    bars = max(20, min(int(bars), 600))
    s = None
    if strategy:
        s = load(ws, strategy)
        m = s.market
        provider_name, symbol, tf, label, align, tz = m.provider, m.symbol, timeframe or m.timeframe, m.label, \
            m.candle_alignment, m.tz
    else:
        mk = MARKETS_BY_KEY.get(market)
        if not mk:
            err(404, "Choose one of the built-in markets")
        provider_name, symbol, tf, label, align, tz = "auto", market, timeframe or "15m", mk["label"], "new_york", "UTC"
    try:
        bar = timeframes.to_timedelta(tf)
    except timeframes.TimeframeError as exc:
        err(422, str(exc))
    days = max(3, int((bar * bars * 1.7) / pd.Timedelta(days=1)) + 3)
    provider = data_mod.provider_for(c, provider_name, tz)

    def fetch():
        frames = provider.fetch_bars(symbol, tf, days, align)
        return frames[0], bool(getattr(provider, "stale", False))

    try:
        source = c.data.get("source") if provider_name == "auto" else provider_name
        key = (provider_name, source, symbol, tf, days, align, str(c.cache_dir),
               str(c.csv_base_dir) if provider_name == "csv" else "",
               c.data.get("oanda_token") if source == "oanda" else "")
        (df, stale), old_age = ctx_of(request).live_cache.get_or_last(key, fetch)
    except data_mod.DataError as exc:
        err(422, str(exc))
    except Exception as exc:
        err(502, f"Couldn't load prices: {exc}")
    if df.empty:
        err(422, "No prices for that market yet")
    stale = stale or old_age is not None
    shut = markethours.closed_until(symbol)
    closed_until = shut[0] if shut else None
    out = {"market": market or (s.market.symbol if s else ""), "label": label, "timeframe": tf,
           "candles": compute.candles_out(df, bars), "updated": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
           "forming": bool(df.index[-1] + bar > pd.Timestamp.now(tz="UTC")),
           "spread": float(df["spread"].iloc[-1]) if "spread" in df.columns and pd.notna(df["spread"].iloc[-1]) else None,
           "marks": [], "alerts": [], "stale": stale,
           "closed_until": closed_until.isoformat() if closed_until is not None else None}
    first = df.index[-min(bars, len(df))]
    with ws.store(create=False) as store:
        if s is not None:
            closed = data_mod.closed_bars(df, tf)
            out["marks"] = compute.signal_marks(s, closed, bars)
            out["indicators"] = compute.chart_indicators(s, df, min(bars, len(df)))
            run = store.active_run(strategy) if store else None
            if run:
                out["marks"] += compute.trade_marks(store.trades(run_id=run["id"]), first)
                pos = (store.load(strategy) or {}).get("state", {}).get("position")
                if pos:
                    out["marks"].append({"time": pos["entry_time"], "price": pos["entry_price"],
                                         "kind": "buy" if s.direction == "long" else "sell", "label": "Open paper trade"})
                    out["lines"] = [{"price": pos["stop"], "label": "Stop", "kind": "stop"}] + (
                        [{"price": pos["target"], "label": "Target", "kind": "target"}] if pos.get("target") else [])
        if provider_name == "auto" and store is not None:
            out["alerts"] = store.price_alerts(market=symbol, active_only=True)
    return clean(out)


# ---------------------------------------------------------------- price alerts


def checked_alert(body: dict) -> dict:
    direction = body.get("direction")
    if direction not in ("above", "below"):
        err(422, "Choose above or below")
    price = body.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        err(422, "Fill in the price")
    return {"direction": direction, "price": float(price), "note": str(body.get("note") or "")[:200]}


@router.get("/api/price-alerts")
def list_price_alerts(market: str = "", ws: Workspace = Depends(workspace)):
    with ws.store(create=False) as store:
        alerts = store.price_alerts(market=market or None) if store else []
    return clean({"alerts": alerts, "poll_minutes": ws.config().poll_minutes})


@router.post("/api/price-alerts", status_code=201)
def add_price_alert(body: dict = Body(...), ws: Workspace = Depends(workspace)):
    mk = MARKETS_BY_KEY.get(str(body.get("market")))
    if not mk:
        err(422, "Choose a market")
    a = checked_alert(body)
    with ws.store() as store:
        limit = ws.limits.max_price_alerts
        if limit is not None and len(store.price_alerts(active_only=True)) >= limit:
            err(409, f"You have {limit} price alerts waiting, the most this plan allows. Remove one first.")
        return clean(store.add_price_alert(mk["key"], mk["label"], a["direction"], a["price"], a["note"]))


@router.put("/api/price-alerts/{alert_id}")
def edit_price_alert(alert_id: int, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    with ws.store() as store:
        old = store.price_alert(alert_id)
        if not old:
            err(404, "No alert with that id")
        a = checked_alert(body)
        active = bool(body.get("active", True))
        limit = ws.limits.max_price_alerts
        if active and not old["active"] and limit is not None and len(store.price_alerts(active_only=True)) >= limit:
            err(409, f"You have {limit} price alerts waiting, the most this plan allows. Remove one first.")
        now = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
        # editing or switching back on re-arms it from now
        store.update_price_alert(alert_id, **a, active=1 if active else 0, checked_to=now,
                                 triggered_at=None, triggered_price=None)
        return clean(store.price_alert(alert_id))


@router.delete("/api/price-alerts/{alert_id}")
def delete_price_alert(alert_id: int, ws: Workspace = Depends(workspace)):
    with ws.store() as store:
        store.delete_price_alert(alert_id)
    return {"deleted": alert_id}


# ---------------------------------------------------------------- your own CSV files


def _uploads(ws: Workspace):
    c = ws.config()
    folder = c.uploads_dir
    try:
        rel = folder.resolve().relative_to(c.csv_base_dir.resolve())
    except ValueError:
        rel = None
    return folder, rel


@router.get("/api/uploads")
def list_uploads(ws: Workspace = Depends(workspace)):
    folder, rel = _uploads(ws)
    files = []
    if folder.is_dir():
        for p in sorted(folder.glob("*.csv")):
            if p.is_file():
                st = p.stat()
                symbol = p.name if rel is None or str(rel) == "." else f"{rel.as_posix()}/{p.name}"
                files.append({"name": p.name, "symbol": symbol, "bytes": st.st_size,
                              "modified": pd.Timestamp(st.st_mtime, unit="s", tz="UTC").isoformat()})
    return {"files": files, "max_files": ws.limits.max_uploads, "max_mb": ws.limits.max_upload_mb}


@router.post("/api/uploads/{name}", status_code=201)
async def upload_csv(name: str, request: Request, ws: Workspace = Depends(workspace)):
    """Save a CSV of candles (sent as the raw request body) to use as a market."""
    if not SAFE_FILE.match(name):
        err(422, "Name the file with letters, numbers, spaces, dots or dashes, ending in .csv")
    folder, _ = _uploads(ws)
    limit = int(ws.limits.max_upload_mb * 1024 * 1024)
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > limit:
            err(413, f"That file is bigger than {ws.limits.max_upload_mb:g} MB")
    existing = [p for p in folder.glob("*.csv")] if folder.is_dir() else []
    if ws.limits.max_uploads is not None and name not in {p.name for p in existing} \
            and len(existing) >= ws.limits.max_uploads:
        err(409, f"You have {len(existing)} files, the most this plan allows. Delete one first.")
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{name}.{os.getpid()}.part"
    tmp.write_bytes(bytes(raw))
    try:
        df = data_mod.CSVProvider(base_dir=folder, allowed_root=folder).load(tmp.name)
    except (data_mod.DataError, ValueError, KeyError, UnicodeDecodeError) as exc:
        tmp.unlink(missing_ok=True)
        err(422, f"That file can't be read as candles: {exc}")
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        err(422, f"That file can't be read: {exc}")
    if df.empty:
        tmp.unlink(missing_ok=True)
        err(422, "That file has no candles in it")
    tmp.replace(folder / name)
    bar = data_mod.infer_bar_length(df)
    return clean({"name": name, "candles": len(df), "first": df.index[0], "last": df.index[-1],
                  "bar_minutes": int(bar.total_seconds() // 60) if bar is not None else None})


@router.delete("/api/uploads/{name}")
def delete_upload(name: str, ws: Workspace = Depends(workspace)):
    if not SAFE_FILE.match(name):
        err(404, "No file with that name")
    folder, _ = _uploads(ws)
    (folder / name).unlink(missing_ok=True)
    return {"deleted": name}


@router.get("/api/markets")
def markets():
    return {"markets": MARKETS}
