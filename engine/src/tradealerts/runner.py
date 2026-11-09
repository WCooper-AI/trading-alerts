"""Live mode: check your rules on each newly closed bar, send alerts, paper-trade them."""

from __future__ import annotations

import logging
import math
import time
from typing import Callable

import pandas as pd

from . import data as data_mod
from . import timeframes, versions
from .config import AppConfig
from .notify import Message, Notifier, broadcast
from .backtest import fine_lookup, spreads_for
from .simulator import Event, Simulator, new_state
from .state import StateStore
from .strategy import Strategy, load_dir

log = logging.getLogger(__name__)

DISCLAIMER_LINE = "Automated alert from rules you set. Not financial advice."


def _p(v) -> str:
    return "—" if v is None else f"{v:,.2f}"


def _time(ts) -> str:
    return pd.Timestamp(ts).strftime("%a %d %b %H:%M UTC")


def load_market_data(cfg: AppConfig, strategy: Strategy):
    """Candles for a strategy, plus finer candles when available: (candles, fine)."""
    m = strategy.market
    provider = data_mod.provider_for(cfg, m.provider, m.tz)
    return provider.fetch_bars(m.symbol, m.timeframe, m.lookback_days or m.default_lookback(), m.candle_alignment)


def _split(loaded):
    return loaded if isinstance(loaded, tuple) else (loaded, None)


def format_event(strategy: Strategy, ev: Event, fresh: bool) -> Message | None:
    s, d = strategy, ev.data
    sym = s.risk.symbol
    arrow = "▲" if s.direction == "long" else "▼"
    label = s.market.label
    bar_end = pd.Timestamp(ev.time) + timeframes.to_timedelta(s.market.timeframe)
    if ev.kind == "signal":
        if not fresh:
            return None
        lines = [
            f"Your rule \"{s.name}\" triggered on the {s.market.timeframe} bar that closed {_time(bar_end)}.",
            f"Reference price: {_p(d['ref_price'])}",
            f"Stop: {_p(d.get('stop'))} ({s.describe_level('stop')})",
            f"Target: {_p(d.get('target'))} ({s.describe_level('target')})",
        ]
        if d.get("size"):
            risk_amt = abs(d["ref_price"] - d["stop"]) * d["size"] * s.risk.point_value
            lines.append(f"Size at your risk settings: {d['size']:g} per point (≈{sym}{risk_amt:,.2f} at risk)")
        if d.get("warning"):
            lines.append(f"Note: {d['warning']}")
        lines += ["Paper trade will open at the next bar's open.", DISCLAIMER_LINE]
        return Message(
            title=f"{arrow} {label}: {s.name} ({s.direction})",
            body="\n".join(lines),
            priority=s.alert_priority,
            tags=("chart_with_upwards_trend",) if s.direction == "long" else ("chart_with_downwards_trend",),
        )
    suffix = "" if fresh else " (catch-up)"
    if ev.kind == "opened":
        return Message(
            title=f"{label}: paper {s.direction} opened{suffix}",
            body=(
                f"{s.name}\nEntry {_p(d['entry_price'])} · stop {_p(d['stop'])} · target {_p(d['target'])}\n"
                f"Size {d['size']:g} per point"
            ),
            priority="low",
            tags=("memo",),
        )
    if ev.kind == "closed":
        pnl = d["pnl"]
        return Message(
            title=f"{label}: paper trade closed by {d['exit_reason'].replace('_', ' ')}{suffix}",
            body=(
                f"{s.name}\n{_p(d['entry_price'])} → {_p(d['exit_price'])} after {d['bars_held']} bars\n"
                f"P/L {'+' if pnl >= 0 else '-'}{sym}{abs(pnl):,.2f} ({d['r_multiple']:+.2f}R) · "
                f"paper balance {sym}{d['equity_after']:,.2f}"
            ),
            priority="default",
            tags=("white_check_mark",) if pnl > 0 else ("x",),
        )
    if ev.kind == "skipped":
        return Message(
            title=f"{label}: signal skipped{suffix}",
            body=f"{s.name}: {d['reason']}",
            priority="low",
            tags=("warning",),
        )
    return None


def is_live(strategy: Strategy) -> bool:
    """Alerts are switched on for this strategy."""
    return not strategy.template and strategy.enabled


def _utc(ts) -> pd.Timestamp:
    t = pd.Timestamp.now(tz="UTC") if ts is None else pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def ensure_run(store: StateStore, strategy: Strategy, now=None) -> dict | None:
    """The strategy's running forward test. Turning alerts on starts one automatically."""
    run = store.active_run(strategy.id)
    text = _file_text(strategy)
    if run is None and is_live(strategy):
        rec = store.load(strategy.id)
        run = store.start_run(
            strategy.id, name=strategy.name, market=strategy.market.label, timeframe=strategy.market.timeframe,
            currency=strategy.risk.currency,
            start_balance=rec["state"]["equity"] if rec else strategy.risk.account_size,
            started_at=_utc(now).isoformat(timespec="seconds"), keep_state=rec is not None,
            source_hash=versions.text_hash(text), strategy_yaml=text,
        )
    elif run is not None and text:
        versions.sync_run(store, run, text, _utc(now).isoformat(timespec="seconds"))
        run = store.run(run["id"])
    if run is not None and strategy.path is not None:
        versions.sync(store, strategy.id, strategy.path)
    return run


def _file_text(strategy: Strategy) -> str:
    try:
        return strategy.path.read_text(encoding="utf-8") if strategy.path else ""
    except OSError:
        return ""


def process_strategy(
    cfg: AppConfig,
    strategy: Strategy,
    store: StateStore,
    notifiers: list[Notifier],
    now: pd.Timestamp | None = None,
    loader: Callable[[AppConfig, Strategy], pd.DataFrame] = load_market_data,
) -> list[Event]:
    """Advance a strategy's forward test over any candles that closed since the last check.

    Only candles that close after the test started count. Alerts are sent only if
    the strategy has alerts switched on; otherwise the test runs silently.
    """
    now = _utc(now)
    run = ensure_run(store, strategy, now)
    if run is None:
        return []
    raw, fine = _split(loader(cfg, strategy))
    df = data_mod.closed_bars(raw, strategy.market.timeframe, now)
    if df.empty:
        log.warning("%s: no closed candles yet", strategy.id)
        return []
    tf = timeframes.to_timedelta(strategy.market.timeframe)

    rec = store.load(strategy.id)
    if rec is None:
        # New forward test: begin with the first candle that closes after it started.
        started = _utc(run["started_at"])
        if started > now:
            return []  # clock behind the start time: nothing to count yet
        before = df.index[df.index + tf <= started]
        cursor = before[-1] if len(before) else df.index[0] - tf
        state = new_state(strategy)
        state["equity"] = run["start_balance"]
        log.info("%s: forward test %s starts after %s", strategy.id, run["id"], cursor)
    else:
        cursor = pd.Timestamp(rec["cursor"])
        state = rec["state"]
        if strategy.source_hash and rec["source_hash"] and rec["source_hash"] != strategy.source_hash:
            log.info("%s: strategy file changed; new rules apply from the next bar", strategy.id)

    new_pos = [i for i, t in enumerate(df.index) if t > cursor]
    if not new_pos:
        if rec is None:
            store.save(strategy.id, str(cursor), state, strategy.source_hash)
        return []

    send = is_live(strategy) and strategy.alerts_enabled
    prepared = strategy.prepare(df)
    sim = Simulator(strategy, state)
    get_fine = fine_lookup(fine, tf)
    spreads = spreads_for(strategy, df)
    last = len(df) - 1
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    all_events: list[Event] = []
    for i in new_pos:
        t = df.index[i]
        for ev in sim.step(t, o[i], h[i], l[i], c[i], bool(prepared.entry[i]),
                           bool(prepared.exit[i]), prepared.refs_at(i),
                           spread=None if spreads is None else spreads[i], fine=lambda t=t: get_fine(t)):
            all_events.append(ev)
            if ev.kind == "closed":
                store.add_trade(strategy.id, ev.data, run["id"])
            msg = format_event(strategy, ev, fresh=(i == last))
            notified = False
            errors = []
            if msg and send:
                errors = broadcast(notifiers, msg)
                notified = not errors
            store.add_event(strategy.id, str(t), ev.kind, msg.body if msg else "", notified, run["id"])
            if errors:
                store.add_event(strategy.id, str(t), "alert_failed",
                                f"Alert not sent: {msg.title}\n" + "; ".join(errors), False, run["id"])
        store.add_equity(run["id"], str(t + tf), sim.state["equity"] + sim.unrealised(c[i]))
    sim.state["mark"] = {"time": str(df.index[last] + tf), "close": float(c[last])}
    store.commit()
    store.save(strategy.id, str(df.index[-1]), sim.state, strategy.source_hash)
    return all_events


def run_cycle(cfg: AppConfig, store: StateStore, notifiers: list[Notifier],
              now: pd.Timestamp | None = None, loader=load_market_data,
              problems: dict[str, str] | None = None, price_loader=None) -> dict[str, list[Event]]:
    """One check of every strategy that has a running forward test or alerts switched on.

    ``problems`` (if given) is updated with each strategy's latest error, and cleared
    for strategies that checked fine, so the app can show which ones need attention."""
    strategies, errors = load_dir(cfg.strategies_dir)
    for e in errors:
        log.error("Skipping strategy: %s", e)
    if problems is not None:
        for sid in set(problems) - {s.id for s in strategies}:
            problems.pop(sid, None)
    running = store.active_runs()
    results: dict[str, list[Event]] = {}
    for s in strategies:
        if not is_live(s) and s.id not in running:
            continue
        try:
            results[s.id] = process_strategy(cfg, s, store, notifiers, now, loader)
            if problems is not None:
                problems.pop(s.id, None)
        except Exception as exc:  # one bad market shouldn't stop the others
            log.exception("%s: check failed: %s", s.id, exc)
            if problems is not None:
                problems[s.id] = str(exc) or exc.__class__.__name__
    try:
        from .pricealerts import check_price_alerts, load_recent
        check_price_alerts(cfg, store, notifiers, now, loader=price_loader or load_recent)
    except Exception as exc:
        log.exception("Price alerts check failed: %s", exc)
    return results


GRACE_SECONDS = 45  # wait this long after a candle closes so the feed has it


def seconds_until_next_check(poll_minutes: float, grace_seconds: int = GRACE_SECONDS, now: float | None = None) -> float:
    """Wait until just after the next poll boundary so freshly closed bars are available."""
    now = time.time() if now is None else now
    period = poll_minutes * 60
    nxt = math.floor(now / period) * period + period + grace_seconds
    return max(1.0, nxt - now)


def _slot(poll_minutes: float, now: float, grace_seconds: int = GRACE_SECONDS) -> int:
    """Which poll period ``now`` falls in, counting from just after each boundary."""
    return math.floor((now - grace_seconds) / (poll_minutes * 60))


def cached_loader(cache):
    """A market data loader that shares downloads between strategies (and customers) in one check."""

    def load(cfg: AppConfig, strategy: Strategy):
        m = strategy.market
        key = ("bars", m.provider, cfg.data.get("source"), m.symbol, m.timeframe,
               m.lookback_days or m.default_lookback(), m.tz, m.candle_alignment,
               str(cfg.cache_dir), str(cfg.csv_base_dir) if m.provider == "csv" else "",
               cfg.data.get("oanda_token") if "oanda" in (m.provider, cfg.data.get("source")) else "")
        return cache.get(key, lambda: load_market_data(cfg, strategy))

    return load


def pd_today(tz: str | None = None) -> str:
    return pd.Timestamp.now(tz=tz).strftime("%Y-%m-%d") if tz else pd.Timestamp.now().strftime("%Y-%m-%d")


class LiveRunner:
    """Runs live checks in a background thread, alongside the web interface.

    ``workspaces`` is one workspace (or a config path) for the self-hosted edition, or a
    function returning the workspaces to check (the hosted edition: every customer with
    an active subscription). Settings and strategy files are re-read before every check,
    so edits made in the web interface apply from the next check without a restart.
    """

    def __init__(self, workspaces, data_cache=None):
        import threading

        from .cache import DataCache
        from .workspace import Workspace

        if callable(workspaces):
            self._list = workspaces
        else:
            ws = workspaces if isinstance(workspaces, Workspace) else Workspace(workspaces)
            self._list = lambda: [ws]
        self.cache = data_cache or DataCache(ttl=60)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._cond = threading.Condition()
        self._force: set[str] = set()
        self._slots: dict[str, int] = {}
        self._thread = threading.Thread(target=self._loop, name="live-runner", daemon=True)
        self.cycles = 0
        self.running = False
        self.last_run: str | None = None
        self.next_run: str | None = None
        self.last_error: str | None = None   # a problem with the checker itself
        self.errors: dict[str, str] = {}     # workspace -> its last check's error
        self.last_results: dict[str, dict[str, int]] = {}
        self.problems_by: dict[str, dict[str, str]] = {}  # workspace -> strategy id -> what went wrong

    # Kept for the single-workspace edition and older callers.
    @property
    def problems(self) -> dict[str, str]:
        items = list(self.problems_by.values())
        return items[0] if len(items) == 1 else {k: v for d in items for k, v in d.items()}

    def start(self) -> "LiveRunner":
        import threading

        self._thread.start()
        threading.Thread(target=self._reminders, name="news-reminders", daemon=True).start()
        return self

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout)

    def wake(self, key: str | None = None) -> None:
        """Start a check soon (of one workspace, or all), without waiting for it."""
        self._force.add(key or "*")
        self._wake.set()

    def run_now(self, timeout: float = 120, key: str | None = None) -> bool:
        """Trigger a check immediately and wait for it to finish."""
        with self._cond:
            target = self.cycles + 1
            self._force.add(key or "*")
            self._wake.set()
            return self._cond.wait_for(lambda: self.cycles >= target, timeout)

    def status(self, key: str | None = None) -> dict:
        if key is None:
            keys = list(self.problems_by)
            key = keys[0] if len(keys) == 1 else None
        return {
            "enabled": self._thread.is_alive(),
            "checking": self.running,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "last_error": (self.errors.get(key) if key else None) or self.last_error,
            "last_results": self.last_results.get(key, {}) if key else {},
            "problems": dict(self.problems_by.get(key, {})) if key else {},
        }

    # ------------------------------------------------------------------ one pass

    def check_workspace(self, ws) -> None:
        """Check one workspace's forward tests, alerts and price alerts, and send its weekly report."""
        from .notify import build_notifiers

        cfg = ws.config()
        problems = self.problems_by.setdefault(ws.key, {})
        notifiers = build_notifiers(cfg.notifiers)
        with ws.store() as store:
            results = run_cycle(cfg, store, notifiers, problems=problems, loader=cached_loader(self.cache),
                                price_loader=self._price_loader)
            self.last_results[ws.key] = {k: len(v) for k, v in results.items()}
            try:
                from . import weekly
                rep = cfg.report
                marker = cfg.data_dir / "report.sent"
                if rep and weekly.due(rep, marker):
                    weekly.send(store, notifiers)
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text(pd_today(rep.get("tz")))
            except Exception as exc:
                log.warning("Weekly report not sent: %s", exc)

    def _price_loader(self, cfg, market: str, days: int = 3):
        from .pricealerts import load_recent

        key = ("price-alerts", cfg.data.get("source"), market, days, str(cfg.cache_dir),
               cfg.data.get("oanda_token") if cfg.data.get("source") == "oanda" else "")
        return self.cache.get(key, lambda: load_recent(cfg, market, days))

    def _due(self, ws, now: float) -> tuple[bool, float]:
        """(due now?, poll minutes) for one workspace."""
        try:
            poll = ws.config().poll_minutes
        except Exception:
            poll = 15.0
        slot = _slot(poll, now)
        forced = "*" in self._force or ws.key in self._force
        return forced or self._slots.get(ws.key) != slot, poll

    def cycle(self) -> None:
        """One pass: check every workspace that's due."""
        from . import news

        now = time.time()
        workspaces = self._list()
        checked = False
        for ws in workspaces:
            due, poll = self._due(ws, now)
            if not due:
                continue
            self._slots[ws.key] = _slot(poll, now)
            self._force.discard(ws.key)
            checked = True
            try:
                self.check_workspace(ws)
                self.errors.pop(ws.key, None)
            except Exception as exc:
                log.exception("Live check failed for %s", ws.owner or ws.config_path)
                self.errors[ws.key] = str(exc) or exc.__class__.__name__
        self._force.discard("*")
        if checked:
            try:
                news.update_from_feed()  # about twice a day; builds up a news history for backtests
            except Exception as exc:
                log.info("News calendar not updated: %s", exc)

    def _next_wait(self) -> float:
        now = time.time()
        waits = []
        for ws in self._list():
            try:
                poll = ws.config().poll_minutes
            except Exception:
                poll = 15.0
            nxt = (_slot(poll, now) + 1) * poll * 60 + GRACE_SECONDS
            waits.append(nxt - now)
        return max(1.0, min(waits + [60.0]))  # look again within a minute: settings or customers change

    def _loop(self) -> None:  # pragma: no cover - timing loop, exercised via run_now in tests
        while not self._stop.is_set():
            self._wake.clear()
            self.running = True
            wait = 60.0
            try:
                self.cycle()
                self.last_error = None
            except Exception as exc:
                log.exception("Live check failed")
                self.last_error = str(exc)
            finally:
                self.running = False
                self.last_run = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
                try:
                    wait = self._next_wait()
                except Exception:
                    wait = 60.0
                self.next_run = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=wait)).isoformat(timespec="seconds")
                with self._cond:
                    self.cycles += 1
                    self._cond.notify_all()
            self._wake.wait(wait)

    def _reminders(self) -> None:  # pragma: no cover - timing loop
        """News reminders need to be on time, so they're checked every 30 seconds, separately."""
        from . import news
        from .notify import build_notifiers

        while not self._stop.wait(30):
            for ws in self._list():
                try:
                    cfg = ws.config()
                    with ws.store(create=False) as store:
                        if store is not None and store.due_news_reminders():
                            news.send_reminders(store, build_notifiers(cfg.notifiers))
                except Exception as exc:
                    log.warning("News reminders: %s", exc)

    def run_blocking(self) -> None:  # pragma: no cover - loop
        """For ``tradealerts run``: the same checks as the web interface's, without the web interface."""
        import threading

        threading.Thread(target=self._reminders, name="news-reminders", daemon=True).start()
        try:
            self._loop()
        except KeyboardInterrupt:
            self._stop.set()


def run_forever(cfg_or_path, notifiers=None) -> None:  # pragma: no cover - loop
    path = cfg_or_path.base_dir / "config.yaml" if isinstance(cfg_or_path, AppConfig) else cfg_or_path
    from .workspace import Workspace

    for sid in Workspace(path).migrate():
        log.info("%s: updated to follow Settings, Price data", sid)
    log.info("Live checks running. Ctrl+C to stop.")
    LiveRunner(path).run_blocking()
