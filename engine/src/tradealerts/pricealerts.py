"""Price-level alerts: "tell me when gold goes above 2,700".

Checked by the live checker on 5-minute candles, so a level touched briefly between
checks still counts. Each alert fires once, then switches itself off.
"""

from __future__ import annotations

import logging

import pandas as pd

from . import data as data_mod
from .notify import Message, Notifier, broadcast
from .recipes import MARKETS_BY_KEY
from .state import StateStore

log = logging.getLogger(__name__)


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def load_recent(cfg, market: str, days: int = 3) -> pd.DataFrame:
    provider = data_mod.MarketProvider(cfg.data, cfg.cache_dir)
    df, _ = provider.fetch_bars(market, "5m", days)
    return df


def hit(alert: dict, bars: pd.DataFrame) -> tuple[pd.Timestamp, float] | None:
    """First candle after the alert was last checked that reached the level."""
    since = _utc(alert.get("checked_to") or alert["created_at"])
    recent = bars[bars.index + pd.Timedelta(minutes=5) > since]
    if recent.empty:
        return None
    price = float(alert["price"])
    mask = recent["high"] >= price if alert["direction"] == "above" else recent["low"] <= price
    if not mask.any():
        return None
    t = recent.index[mask.to_numpy().argmax()]
    return t, price


def check_price_alerts(cfg, store: StateStore, notifiers: list[Notifier], now=None, loader=load_recent) -> list[dict]:
    """Check every active alert. Returns the ones that fired."""
    now = pd.Timestamp.now(tz="UTC") if now is None else _utc(now)
    fired = []
    alerts = store.price_alerts(active_only=True)
    by_market: dict[str, list[dict]] = {}
    for a in alerts:
        by_market.setdefault(a["market"], []).append(a)
    for market, items in by_market.items():
        try:
            bars = loader(cfg, market)
        except Exception as exc:
            log.warning("Price alerts for %s: couldn't load prices: %s", market, exc)
            continue
        bars = bars[bars.index <= now]
        for a in items:
            got = hit(a, bars)
            if got is None:
                store.update_price_alert(a["id"], checked_to=now.isoformat(timespec="seconds"))
                continue
            t, price = got
            label = a.get("label") or MARKETS_BY_KEY.get(market, {}).get("label", market)
            word = "above" if a["direction"] == "above" else "below"
            last = float(bars["close"].iloc[-1])
            body = (f"{label} reached {price:,.5g} ({word} your level) around {t:%a %d %b %H:%M} UTC.\n"
                    f"Latest price {last:,.5g}." + (f"\nNote: {a['note']}" if a.get("note") else "")
                    + "\nPrice alert you set. Not financial advice.")
            errors = broadcast(notifiers, Message(f"{label} {word} {price:,.5g}", body, "high", ("bell",)))
            if errors:
                log.warning("Price alert %s: %s", a["id"], "; ".join(errors))
                store.add_event("", t.isoformat(), "alert_failed",
                                f"Alert not sent: {label} {word} {price:,.5g}\n" + "; ".join(errors), False)
            # triggered_price is the latest price when it fired (the level itself is "price")
            store.update_price_alert(a["id"], active=0, triggered_at=t.isoformat(), triggered_price=last,
                                     checked_to=now.isoformat(timespec="seconds"))
            fired.append({**a, "triggered_at": t.isoformat()})
    return fired
