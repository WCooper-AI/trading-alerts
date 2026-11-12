"""Price-level alerts: "tell me when gold goes above 2,700".

Checked by the live checker on 5-minute candles, so a level touched briefly between
checks still counts. An alert fires once and switches itself off, or (repeat "every")
stays on and fires again after a cooldown. "cross" alerts need the price to come from the
other side of the level, so a market already past it doesn't fire straight away.
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
    """First candle after the alert was last checked (and after any cooldown) that reached the level.

    Touch alerts: the candle's high (or low) reached the level. Cross alerts: the previous candle
    closed on the other side of the level first, so only a genuine move through it counts.
    """
    since = _utc(alert.get("checked_to") or alert["created_at"])
    if alert.get("repeat") == "every" and alert.get("last_fired_at"):
        since = max(since, _utc(alert["last_fired_at"]) + pd.Timedelta(minutes=int(alert.get("cooldown_minutes") or 0)))
    price = float(alert["price"])
    above = alert["direction"] == "above"
    mask = bars["high"] >= price if above else bars["low"] <= price
    if alert.get("cross"):
        prev = bars["close"].shift(1)
        mask &= (prev < price) if above else (prev > price)
    mask &= bars.index + pd.Timedelta(minutes=5) > since
    if not mask.any():
        return None
    t = bars.index[mask.to_numpy().argmax()]
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
            verb = "crossed" if a.get("cross") else "reached"
            body = (f"{label} {verb} {price:,.5g} ({word} your level) around {t:%a %d %b %H:%M} UTC.\n"
                    f"Latest price {last:,.5g}." + (f"\nNote: {a['note']}" if a.get("note") else "")
                    + "\nPrice alert you set. Not financial advice.")
            errors = broadcast(notifiers, Message(f"{label} {word} {price:,.5g}", body, "high", ("bell",)))
            if errors:
                log.warning("Price alert %s: %s", a["id"], "; ".join(errors))
                store.add_event("", t.isoformat(), "alert_failed",
                                f"Alert not sent: {label} {word} {price:,.5g}\n" + "; ".join(errors), False)
            # triggered_price is the latest price when it fired (the level itself is "price")
            again = a.get("repeat") == "every"
            store.update_price_alert(a["id"], active=1 if again else 0, triggered_at=t.isoformat(), triggered_price=last,
                                     checked_to=now.isoformat(timespec="seconds"), last_fired_at=t.isoformat(),
                                     fired_count=int(a.get("fired_count") or 0) + 1)
            fired.append({**a, "triggered_at": t.isoformat()})
    return fired
