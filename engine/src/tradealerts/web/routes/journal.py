"""The trade journal: trades you actually placed, linked to the forward-test trades they came from."""

from __future__ import annotations

import csv
import io

import pandas as pd
from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response

from ...recipes import MARKETS_BY_KEY
from ...workspace import Workspace
from ..common import clean, err, number, workspace

router = APIRouter()

MOODS = ("calm", "confident", "anxious", "bored", "frustrated", "rushed")


def split_tags(s) -> list[str]:
    """"breakout, News ,breakout" -> ["breakout", "news"]: lower-case, trimmed, de-duplicated, in order."""
    seen, out = set(), []
    for t in str(s or "").replace(";", ",").split(","):
        t = t.strip().lower()[:30]
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def journal_view(e: dict, paper: dict | None) -> dict:
    sign = 1 if e["direction"] == "long" else -1
    out = dict(e)
    out["tags"] = split_tags(e.get("tags"))
    out["followed_plan"] = None if e.get("followed_plan") is None else bool(e["followed_plan"])
    pv = e.get("point_value") or 1
    if e.get("exit_price") is not None:
        out["pnl"] = (e["exit_price"] - e["entry_price"]) * sign * e["size"] * pv - (e.get("fees") or 0)
    else:
        out["pnl"] = None
    if paper:
        out["paper"] = {k: paper.get(k) for k in ("id", "entry_time", "entry_price", "exit_time", "exit_price",
                                                   "pnl", "strategy_id", "exit_reason")}
        out["entry_slippage"] = (e["entry_price"] - paper["entry_price"]) * sign
        if e.get("exit_price") is not None and paper.get("exit_price") is not None:
            out["exit_slippage"] = (paper["exit_price"] - e["exit_price"]) * sign
        else:
            out["exit_slippage"] = None
        slip = out["entry_slippage"] + (out["exit_slippage"] or 0)
        out["slippage_cost"] = slip * e["size"] * pv
    return out


def checked_journal(body: dict) -> dict:
    market = str(body.get("market") or "").strip()[:80]
    if not market:
        err(422, "Choose or type the market")
    if body.get("direction") not in ("long", "short"):
        err(422, "Choose buy or sell")
    out = {"market": market, "direction": body["direction"]}

    def when(key, label, required=True):
        v = body.get(key)
        if v in (None, ""):
            if required:
                err(422, f"Fill in {label}")
            return None
        try:
            t = pd.Timestamp(str(v))
        except ValueError:
            err(422, f"{label[0].upper() + label[1:]} must be a date and time")
        if pd.isna(t):
            err(422, f"{label[0].upper() + label[1:]} must be a date and time")
        return (t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")).isoformat()

    out["entry_time"] = when("entry_time", "when you opened it")
    out["entry_price"] = number(body, "entry_price", "the entry price")
    out["size"] = number(body, "size", "the size")
    out["exit_time"] = when("exit_time", "when you closed it", required=False)
    out["exit_price"] = number(body, "exit_price", "the exit price", required=False)
    if (out["exit_time"] is None) != (out["exit_price"] is None):
        err(422, "Fill in both the exit time and price, or leave both blank while the trade is open")
    if out["exit_time"] and pd.Timestamp(out["exit_time"]) < pd.Timestamp(out["entry_time"]):
        err(422, "The exit is before the entry")
    pv = number(body, "point_value", "value per point", required=False)
    out["point_value"] = pv if pv is not None else float(MARKETS_BY_KEY.get(market, {}).get("point_value", 1))
    out["fees"] = number(body, "fees", "fees", required=False, minimum=0, exclusive=False) or 0.0
    out["currency"] = body.get("currency") if body.get("currency") in ("GBP", "USD", "EUR") else "GBP"
    out["notes"] = str(body.get("notes") or "")[:1000]
    tags = body.get("tags")
    out["tags"] = ",".join(split_tags(",".join(map(str, tags)) if isinstance(tags, list) else tags)[:12]) or None
    out["setup"] = str(body.get("setup") or "").strip()[:60] or None
    fp = body.get("followed_plan")
    if fp not in (None, "", True, False):
        err(422, "Followed the plan must be yes, no or blank")
    out["followed_plan"] = None if fp in (None, "") else (1 if fp else 0)
    mood = body.get("mood")
    if mood not in (None, "", *MOODS):
        err(422, "Choose a mood from the list, or leave it blank")
    out["mood"] = mood or None
    pt = body.get("paper_trade_id")
    out["paper_trade_id"] = int(pt) if isinstance(pt, int) and not isinstance(pt, bool) else None
    out["strategy_id"] = None
    return out


def _rows(store) -> list[dict]:
    return [journal_view(e, store.paper_trade(e["paper_trade_id"]) if e.get("paper_trade_id") else None)
            for e in store.journal()]


def _summary(rows: list[dict]) -> dict:
    closed = [r for r in rows if r["pnl"] is not None]
    linked = [r for r in rows if "entry_slippage" in r]
    exits = [r["exit_slippage"] for r in linked if r.get("exit_slippage") is not None]
    return {
        "trades": len(rows), "closed": len(closed), "open": len(rows) - len(closed),
        "pnl": sum(r["pnl"] for r in closed),
        "wins": sum(1 for r in closed if r["pnl"] > 0),
        "linked": len(linked),
        "avg_entry_slippage": sum(r["entry_slippage"] for r in linked) / len(linked) if linked else None,
        "avg_exit_slippage": sum(exits) / len(exits) if exits else None,
        "slippage_cost": sum(r["slippage_cost"] for r in linked) if linked else None,
    }


def _matches(r: dict, tag: str, setup: str, mood: str, followed_plan: str) -> bool:
    if tag and tag.lower() not in r["tags"]:
        return False
    if setup and (r.get("setup") or "").lower() != setup.lower():
        return False
    if mood and (r.get("mood") or "") != mood:
        return False
    if followed_plan in ("yes", "no") and r.get("followed_plan") is not (followed_plan == "yes"):
        return False
    return True


def _breakdown(rows: list[dict], key) -> list[dict]:
    """P/L and win rate for each value of a field (a list-valued field counts the trade under each value)."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["pnl"] is None:
            continue
        vals = key(r)
        for v in (vals if isinstance(vals, list) else [vals]):
            if v is not None and v != "":
                groups.setdefault(str(v), []).append(r)
    out = [{"value": v, "trades": len(g), "pnl": sum(r["pnl"] for r in g), "wins": sum(1 for r in g if r["pnl"] > 0)}
           for v, g in groups.items()]
    return sorted(out, key=lambda b: (-b["trades"], b["value"]))


def _equity(rows: list[dict]) -> list[list]:
    """Running P/L after each closed trade, oldest first: [[exit_time, cumulative], ...]."""
    closed = sorted((r for r in rows if r["pnl"] is not None), key=lambda r: (r["exit_time"], r["id"]))
    total, out = 0.0, []
    for r in closed:
        total += r["pnl"]
        out.append([r["exit_time"], total])
    return out


@router.get("/api/journal")
def journal(tag: str = Query(""), setup: str = Query(""), mood: str = Query(""), followed_plan: str = Query(""),
            ws: Workspace = Depends(workspace)):
    """Entries plus a summary. With tag/setup/mood/followed_plan the entries, summary and equity curve are
    filtered; the breakdowns always cover every closed trade so you can compare."""
    with ws.store(create=False) as store:
        rows = _rows(store) if store else []
    shown = [r for r in rows if _matches(r, tag, setup, mood, followed_plan)]
    plan = lambda r: None if r["followed_plan"] is None else ("yes" if r["followed_plan"] else "no")  # noqa: E731
    return clean({"entries": shown, "summary": _summary(shown), "equity": _equity(shown),
                  "filter": {"tag": tag, "setup": setup, "mood": mood, "followed_plan": followed_plan},
                  "breakdowns": {"tags": _breakdown(rows, lambda r: r["tags"]),
                                 "setup": _breakdown(rows, lambda r: r.get("setup")),
                                 "mood": _breakdown(rows, lambda r: r.get("mood")),
                                 "followed_plan": _breakdown(rows, plan)},
                  "moods": list(MOODS)})


def _cell(v) -> str:
    """CSV cell, defused so a spreadsheet won't run text like =HYPERLINK(...) as a formula."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") and not _is_number(s) else s


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


@router.get("/api/journal.csv")
def journal_csv(ws: Workspace = Depends(workspace)):
    with ws.store(create=False) as store:
        rows = _rows(store) if store else []
    cols = ["entry_time", "market", "direction", "entry_price", "exit_time", "exit_price", "size", "point_value",
            "fees", "pnl", "currency", "entry_slippage", "exit_slippage", "tags", "setup", "followed_plan", "mood", "notes"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        r = {**r, "tags": " ".join(r["tags"]), "followed_plan": "" if r["followed_plan"] is None else ("yes" if r["followed_plan"] else "no")}
        w.writerow([_cell(r.get(c)) for c in cols])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="trade-journal.csv"'})


def _link(store, e: dict) -> None:
    if e["paper_trade_id"]:
        p = store.paper_trade(e["paper_trade_id"])
        e["strategy_id"] = p["strategy_id"] if p else None
        if not p:
            e["paper_trade_id"] = None


@router.post("/api/journal", status_code=201)
def add_journal(body: dict = Body(...), ws: Workspace = Depends(workspace)):
    e = checked_journal(body)
    with ws.store() as store:
        _link(store, e)
        return {"id": store.save_journal(e)}


@router.put("/api/journal/{entry_id}")
def edit_journal(entry_id: int, body: dict = Body(...), ws: Workspace = Depends(workspace)):
    e = checked_journal(body)
    with ws.store() as store:
        if not store.journal_entry(entry_id):
            err(404, "No journal entry with that id")
        _link(store, e)
        store.save_journal(e, entry_id)
    return {"id": entry_id}


@router.delete("/api/journal/{entry_id}")
def delete_journal(entry_id: int, ws: Workspace = Depends(workspace)):
    with ws.store() as store:
        store.delete_journal(entry_id)
    return {"deleted": entry_id}


@router.get("/api/paper-trades")
def paper_trades(market: str = "", ws: Workspace = Depends(workspace)):
    """Recent forward-test trades, to link real trades to."""
    with ws.store(create=False) as store:
        if store is None:
            return {"trades": []}
        runs = {r["id"]: r for r in store.runs()}
        want = MARKETS_BY_KEY.get(market, {}).get("label", market) if market else None
        out = []
        for t in reversed(store.trades()):
            run = runs.get(t["run_id"]) or {}
            label = run.get("market") or ""
            if want and label != want:
                continue
            out.append({**t, "strategy_name": run.get("name") or t["strategy_id"], "market_label": label})
            if len(out) >= 100:
                break
    return clean({"trades": out})
