"""The weekly report: how each forward test did, your journal, and the week ahead.

Sent through your alert channels (email gets the full formatted version), and
available any time as a printable page at /report (print it to save a PDF).
"""

from __future__ import annotations

import html
from pathlib import Path

import pandas as pd

from . import news
from .notify import Message, broadcast
from .state import StateStore

SYM = {"GBP": "£", "USD": "$", "EUR": "€"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _money(v: float, sym: str, signed: bool = False) -> str:
    sign = ("+" if v >= 0 else "-") if signed else ("-" if v < 0 else "")
    return f"{sign}{sym}{abs(v):,.2f}"


def build(store: StateStore, days: int = 7, now: pd.Timestamp | None = None) -> dict:
    now = _utc(now) if now is not None else pd.Timestamp.now(tz="UTC")
    since = now - pd.Timedelta(days=days)
    tests = []
    for run in store.runs():
        start = _utc(run["started_at"])
        stopped = _utc(run["stopped_at"]) if run.get("stopped_at") else None
        if start > now or (stopped is not None and stopped < since):
            continue
        eq = store.equity(run["id"])
        balance = eq[-1][1] if eq else run["start_balance"]
        before = run["start_balance"]
        for t, v in eq:
            if _utc(t) <= since:
                before = v
        trades = [t for t in store.trades(run_id=run["id"]) if t.get("exit_time") and _utc(t["exit_time"]) > since]
        tests.append({
            "name": run.get("name") or run["strategy_id"], "market": run.get("market") or "",
            "sym": SYM.get(run.get("currency") or "GBP", ""), "active": stopped is None,
            "balance": balance, "start_balance": run["start_balance"],
            "week_change": balance - before, "total_change": balance - run["start_balance"],
            "trades": len(trades), "wins": sum(1 for t in trades if (t.get("pnl") or 0) > 0),
            "best": max((t.get("pnl") or 0 for t in trades), default=None),
            "worst": min((t.get("pnl") or 0 for t in trades), default=None),
        })
    journal = [e for e in store.journal() if e.get("exit_time") and _utc(e["exit_time"]) > since]
    jpnl = 0.0
    for e in journal:
        sign = 1 if e["direction"] == "long" else -1
        jpnl += (e["exit_price"] - e["entry_price"]) * sign * e["size"] * (e.get("point_value") or 1) - (e.get("fees") or 0)
    alerts = [a for a in store.price_alerts() if a.get("triggered_at") and _utc(a["triggered_at"]) > since]
    try:
        ahead = news.upcoming(7, impacts=("high",), since_hours=0)
    except Exception:
        ahead = []
    return {
        "from": since.isoformat(), "to": now.isoformat(), "days": days, "tests": tests,
        "journal": {"closed": len(journal), "pnl": jpnl, "wins": sum(1 for e in journal
                    if (e["exit_price"] - e["entry_price"]) * (1 if e["direction"] == "long" else -1) > 0),
                    "currency": SYM.get((journal[0].get("currency") if journal else "GBP") or "GBP", "")},
        "price_alerts": [{"label": a.get("label") or a["market"], "direction": a["direction"], "price": a["price"],
                          "when": a["triggered_at"]} for a in alerts],
        "news": ahead[:15],
    }


def as_text(r: dict) -> str:
    lines = [f"Your week, {pd.Timestamp(r['from']):%d %b} to {pd.Timestamp(r['to']):%d %b}", ""]
    if r["tests"]:
        lines.append("Forward tests:")
        for t in r["tests"]:
            lines.append(f"- {t['name']}: {_money(t['week_change'], t['sym'], True)} this week, "
                         f"{t['trades']} trade{'s' if t['trades'] != 1 else ''} closed "
                         f"(balance {_money(t['balance'], t['sym'])}, {_money(t['total_change'], t['sym'], True)} since the start)"
                         + ("" if t["active"] else ", now stopped"))
    else:
        lines.append("No forward tests ran this week.")
    j = r["journal"]
    if j["closed"]:
        lines += ["", f"Journal: {j['closed']} real trades closed, {_money(j['pnl'], j['currency'], True)}, {j['wins']} won."]
    if r["price_alerts"]:
        lines += ["", "Price alerts hit: " + ", ".join(f"{a['label']} {a['direction']} {a['price']:,.5g}" for a in r["price_alerts"])]
    if r["news"]:
        lines += ["", "High-impact news this coming week:"]
        for e in r["news"][:8]:
            lines.append(f"- {pd.Timestamp(e['time']):%a %d %b %H:%M} UTC  {e['currency']}  {e['title']}")
    lines += ["", "Paper trading results from your own rules. Not financial advice."]
    return "\n".join(lines)


def as_html(r: dict, standalone: bool = False) -> str:
    e = html.escape
    rows = "".join(
        f"<tr><td>{e(t['name'])}<div class='sub'>{e(t['market'])}{'' if t['active'] else ' · stopped'}</div></td>"
        f"<td class='num {'gain' if t['week_change'] >= 0 else 'loss'}'>{e(_money(t['week_change'], t['sym'], True))}</td>"
        f"<td class='num'>{t['trades']}</td><td class='num'>{e(_money(t['balance'], t['sym']))}</td>"
        f"<td class='num {'gain' if t['total_change'] >= 0 else 'loss'}'>{e(_money(t['total_change'], t['sym'], True))}</td></tr>"
        for t in r["tests"])
    news_rows = "".join(
        f"<tr><td>{e(pd.Timestamp(n['time']).strftime('%a %d %b %H:%M'))} UTC</td><td>{e(n['currency'])}</td><td>{e(n['title'])}</td>"
        f"<td class='num'>{e(n.get('forecast') or '')}</td></tr>" for n in r["news"])
    j = r["journal"]
    body = f"""
<h1>Your trading week</h1>
<p class="muted">{pd.Timestamp(r['from']):%d %B} to {pd.Timestamp(r['to']):%d %B %Y}</p>
<h2>Forward tests</h2>
{"<table><tr><th>Strategy</th><th class='num'>This week</th><th class='num'>Trades</th><th class='num'>Balance</th><th class='num'>Since start</th></tr>" + rows + "</table>" if rows else "<p>No forward tests ran this week.</p>"}
<h2>Journal</h2>
<p>{f"{j['closed']} real trades closed: {e(_money(j['pnl'], j['currency'], True))}, {j['wins']} won." if j['closed'] else "No real trades closed this week."}</p>
{"<h2>Price alerts hit</h2><ul>" + "".join(f"<li>{e(a['label'])} {e(a['direction'])} {a['price']:,.5g}</li>" for a in r['price_alerts']) + "</ul>" if r["price_alerts"] else ""}
<h2>High-impact news this coming week</h2>
{"<table><tr><th>When</th><th>Currency</th><th>Event</th><th class='num'>Forecast</th></tr>" + news_rows + "</table>" if news_rows else "<p>None in the calendar yet.</p>"}
<p class="foot">Paper trading results from rules you set yourself. Past results don't predict future ones. Not financial advice.</p>
"""
    css = """body{font-family:-apple-system,Segoe UI,sans-serif;color:#0d1220;max-width:760px;margin:2rem auto;padding:0 1rem;line-height:1.5}
h1{margin:0}h2{margin-top:1.8rem;font-size:1.15rem}.muted,.sub,.foot{color:#5d6677}.sub{font-size:.85em}
table{width:100%;border-collapse:collapse;font-size:.95rem}th,td{text-align:left;padding:.45rem .4rem;border-bottom:1px solid #e3e6ee}
.num{text-align:right}.gain{color:#1c8a45}.loss{color:#d1242f}.foot{margin-top:2rem;font-size:.85rem}"""
    if standalone:
        return ("<!DOCTYPE html><html lang='en-GB'><head><meta charset='utf-8'><title>Trading Alerts weekly report</title>"
                "<link rel='stylesheet' href='/static/report.css'></head><body><p class='noprint'><button id='print'>Print or save as PDF</button>"
                " <a href='/'>Back to the app</a></p>" + body + "<script src='/static/report.js'></script></body></html>")
    return f"<html><body><style>{css}</style>{body}</body></html>"


def send(store: StateStore, notifiers, days: int = 7) -> list[str]:
    r = build(store, days)
    title = f"Your trading week to {pd.Timestamp(r['to']):%d %b}"
    return broadcast(notifiers, Message(title, as_text(r), "default", ("bar_chart",), html=as_html(r)))


def due(cfg_report: dict, marker: Path, now: pd.Timestamp | None = None) -> bool:
    """True once a week, at or after the chosen local day and hour, if not already sent."""
    if not cfg_report.get("weekly"):
        return False
    tz = cfg_report.get("tz") or None  # "Sunday 6pm" in your time zone (default: this computer's)
    if now is None:
        now = pd.Timestamp.now(tz=tz) if tz else pd.Timestamp.now()
    elif tz:
        now = pd.Timestamp(now).tz_localize("UTC").tz_convert(tz) if pd.Timestamp(now).tzinfo is None \
            else pd.Timestamp(now).tz_convert(tz)
    day = DAYS.index(cfg_report.get("day", "sun"))
    if now.day_of_week != day or now.hour < int(cfg_report.get("hour", 18)):
        return False
    stamp = now.strftime("%Y-%m-%d")
    return not (marker.exists() and marker.read_text().strip() == stamp)
