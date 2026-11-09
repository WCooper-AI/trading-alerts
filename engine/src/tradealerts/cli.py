"""Command line interface: tradealerts <command> [options]."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from importlib import resources
from pathlib import Path

import pandas as pd

from . import __version__
from . import data as data_mod
from .backtest import run_backtest
from .config import AppConfig, ConfigError
from .config import load_config as _load_config
from .notify import Message, broadcast, build_notifiers
from .report import format_summary, write_report
from .runner import load_market_data, run_cycle, run_forever
from .sample import synthetic_bars
from .state import StateStore
from .strategy import StrategyError, check_runtime, load_dir, load_strategy

TEMPLATES = resources.files("tradealerts") / "templates"


def load_config(path=None) -> AppConfig:
    """Settings for a command, with the news calendar read from that workspace's data folder."""
    from . import news

    cfg = _load_config(path)
    news.set_data_dir(cfg.data_dir)
    return cfg


def _copy_template(name: str, dest: Path, overwrite=False) -> bool:
    if dest.exists() and not overwrite:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes((TEMPLATES / name).read_bytes())
    return True


# --------------------------------------------------------------------------- commands


def cmd_init(args) -> int:
    root = Path(args.dir)
    root.mkdir(parents=True, exist_ok=True)
    made = []
    if _copy_template("config.yaml", root / "config.yaml"):
        made.append("config.yaml")
    for d in ("strategies", "data", "reports"):
        (root / d).mkdir(exist_ok=True)
    for f in (TEMPLATES / "strategies").iterdir():
        if f.name.endswith(".yaml") and _copy_template(f"strategies/{f.name}", root / "examples" / f.name):
            made.append(f"examples/{f.name}")
    print(f"Set up {root.resolve()}")
    for m in made:
        print(f"  + {m}")
    print(
        "\nNext steps:\n"
        "  1. Copy an example into strategies/ (or run: tradealerts new my-strategy)\n"
        "  2. Set your own rules and risk values\n"
        "  3. Backtest it: tradealerts backtest strategies/<file>.yaml\n"
        "  4. Set template: false, then start alerts: tradealerts run"
    )
    return 0


def cmd_new(args) -> int:
    import json

    from .strategy import slug

    cfg = load_config(args.config)
    dest = cfg.strategies_dir / f"{slug(args.name)}.yaml"
    if dest.exists():
        print(f"{dest} already exists", file=sys.stderr)
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    # JSON strings are valid YAML, so a name like "Buy: the dip" can't break the file
    text = (TEMPLATES / "blank.yaml").read_text(encoding="utf-8").replace("__NAME__", json.dumps(args.name))
    dest.write_text(text, encoding="utf-8")
    print(f"Created {dest}. Fill in your rules, then: tradealerts backtest {dest}")
    return 0


def cmd_validate(args) -> int:
    ok = True
    try:
        cfg = load_config(args.config)
        build_notifiers(cfg.notifiers)
        print(f"✓ config ({len(cfg.notifiers)} notifier(s))")
    except (ConfigError, ValueError, TypeError) as exc:
        print(f"✗ config: {exc}")
        return 1
    paths = [Path(p) for p in args.paths] if args.paths else None
    if paths:
        strategies, errors = [], []
        for p in paths:
            try:
                strategies.append(load_strategy(p))
            except StrategyError as exc:
                errors.append(str(exc))
    else:
        strategies, errors = load_dir(cfg.strategies_dir)
    for s in strategies:
        try:
            check_runtime(s)
        except Exception as exc:  # surface runtime errors such as bad time zones
            errors.append(f"{s.id}: {exc}")
            continue
        note = " (template: live alerts off)" if s.template else ""
        print(f"✓ {s.id}: {s.name}{note}")
    for e in errors:
        ok = False
        print(f"✗ {e}")
    if not strategies and not errors:
        print(f"No strategies found in {cfg.strategies_dir}")
    return 0 if ok else 1


def cmd_backtest(args) -> int:
    cfg = load_config(args.config)
    s = load_strategy(args.strategy)
    notes = []
    if args.data:
        raw = data_mod.CSVProvider(tz=args.tz).load(args.data)
        df = data_mod.resample_ohlcv(raw, s.market.timeframe, s.market.candle_alignment)
        fine = raw if len(raw) > len(df) else None
    else:
        if args.years:
            from .backtest import warmup_days
            s.market.lookback_days = int(args.years * 365.25) + warmup_days(s.market.timeframe, s.warmup_candles)
        df, fine = load_market_data(cfg, s)
        notes = data_mod.data_notes(s.market.symbol, s.market.provider, cfg.data)
    start = None
    if args.years:
        start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=round(args.years * 365.25))
    elif args.start:
        start = pd.Timestamp(args.start, tz="UTC")
    if args.end:
        df = df[df.index < pd.Timestamp(args.end, tz="UTC")]
    df = data_mod.closed_bars(df, s.market.timeframe)
    if df.empty or (start is not None and df.index[-1] < start):
        print("No data in that range.", file=sys.stderr)
        return 1
    res = run_backtest(s, df, fine, notes=notes, start=start)
    print(format_summary(res))
    if args.luck:
        from .analysis import luck_check
        print("\nLuck check (resampled trades, both halves, random price paths)...")
        out = luck_check(s, df, fine, start)
        for c in out["checks"]:
            mark = "  -  " if c["passed"] is None else ("PASS " if c["passed"] else "FAIL ")
            print(f"{mark}{c['title']}")
        if "order" in out:
            o = out["order"]
            print(f"     Worst drop with the same trades in other orders: typically {abs(o['median_dd']):.1f}%, "
                  f"1 in 20 orders {abs(o['bad_dd']):.1f}% or worse")
        print(f"Passed {out['passed']} of {out['total']}. Passing makes luck less likely; it doesn't predict the future.")
    if not args.no_report:
        stamp = pd.Timestamp.now().strftime("%Y%m%d-%H%M%S")
        out = write_report(res, Path(args.out) if args.out else cfg.reports_dir / f"{s.id}-{stamp}")
        print(f"\nReport: {out / 'report.html'}")
    return 0


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    logging.getLogger().setLevel(cfg.log_level)
    notifiers = build_notifiers(cfg.notifiers)
    if args.once:
        store = StateStore(cfg.state_db)
        try:
            results = run_cycle(cfg, store, notifiers)
        finally:
            store.close()
        for sid, events in results.items():
            print(f"{sid}: {len(events)} event(s)")
        return 0
    run_forever(args.config or "config.yaml")
    return 0


def cmd_serve(args) -> int:
    from .web import serve

    if args.desktop:
        _prepare_desktop_config(args.config)
    try:
        logging.getLogger().setLevel(_load_config(args.config, must_exist=False).log_level)
    except (ConfigError, ValueError):
        pass
    serve(args.config, args.host, args.port, run_live=not args.no_runner, no_auth=args.no_auth,
          desktop=args.desktop, parent_pid=args.parent_pid)
    return 0


def _prepare_desktop_config(config: str | None) -> None:
    """First launch of the desktop app: create its settings file and folders."""
    if not config:
        raise SystemExit("--desktop needs --config pointing at the app's data folder")
    p = Path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    for sub in ("strategies", "data"):
        (p.parent / sub).mkdir(exist_ok=True)
    if not p.exists():
        p.write_text((resources.files("tradealerts") / "templates" / "config.yaml").read_text(encoding="utf-8"),
                     encoding="utf-8")


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    if not cfg.state_db.exists():
        print("No live runs yet.")
        return 0
    store = StateStore(cfg.state_db)
    try:
        states, trades = store.all_states(), pd.DataFrame(store.trades())
        if not states:
            print("No live runs yet.")
        for sid, rec in sorted(states.items()):
            st = rec["state"]
            pos = st.get("position")
            line = f"{sid}: last bar {rec['cursor'][:16]}, paper balance {st['equity']:,.2f}"
            if pos:
                line += f", OPEN since {pos['entry_time'][:16]} @ {pos['entry_price']:,.2f} (stop {pos['stop']:,.2f})"
            elif st.get("pending_entry"):
                line += ", entry pending at next open"
            print(line)
            if not trades.empty:
                t = trades[trades.strategy_id == sid]
                if len(t):
                    wins = (t.pnl > 0).mean() * 100
                    print(f"    {len(t)} paper trades, win rate {wins:.0f}%, total P/L {t.pnl.sum():+,.2f}, avg {t.r_multiple.mean():+.2f}R")
        if args.events:
            print("\nRecent events:")
            for e in store.recent_events(args.events):
                print(f"  {e['bar_time'][:16]}  {e['strategy_id']}  {e['kind']}{'' if e['notified'] else ' (not sent)'}")
    finally:
        store.close()
    return 0


def cmd_reset(args) -> int:
    cfg = load_config(args.config)
    if not cfg.state_db.exists():
        print("No live state yet.")
        return 0
    store = StateStore(cfg.state_db)
    store.reset(args.strategy_id)
    store.close()
    print(f"Cleared live state for {args.strategy_id}. Paper trade history is kept.")
    return 0


def cmd_test_alert(args) -> int:
    cfg = load_config(args.config)
    errors = broadcast(
        build_notifiers(cfg.notifiers),
        Message("Trading Alerts test", "If you can read this, alerts are working.", "default", ("bell",)),
    )
    for e in errors:
        print(f"✗ {e}")
    print("Sent." if not errors else "Some notifiers failed.")
    return 1 if errors else 0


def cmd_sample_data(args) -> int:
    df = synthetic_bars(days=args.days, freq=args.freq, seed=args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out)
    print(f"Wrote {len(df)} synthetic bars to {out} (random data for testing, not real prices)")
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tradealerts",
        description="Backtest your own trading rules and get push alerts when they trigger.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-c", "--config", default=None, help="path to config.yaml (default: ./config.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create config, folders and example strategies")
    s.add_argument("dir", nargs="?", default=".")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("new", help="create a blank strategy in strategies/")
    s.add_argument("name")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("validate", help="check config and strategy files")
    s.add_argument("paths", nargs="*")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("backtest", help="test a strategy on historical data")
    s.add_argument("strategy")
    s.add_argument("--data", help="CSV of bars instead of the strategy's data provider")
    s.add_argument("--tz", default="UTC", help="time zone of naive CSV timestamps")
    s.add_argument("--years", type=float, help="test over the last N years, e.g. 5")
    s.add_argument("--from", dest="start", help="start date, e.g. 2025-01-01")
    s.add_argument("--to", dest="end", help="end date (exclusive)")
    s.add_argument("--out", help="report folder")
    s.add_argument("--no-report", action="store_true")
    s.add_argument("--luck", action="store_true", help="also run the luck check (takes up to a minute)")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("run", help="check strategies on every new bar and send alerts")
    s.add_argument("--once", action="store_true", help="run one check and exit (for cron)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("serve", help="open the web interface (also runs live checks)")
    s.add_argument("--host", default="127.0.0.1", help="address to listen on (default: localhost only)")
    s.add_argument("--port", type=int, default=8321)
    s.add_argument("--no-runner", action="store_true", help="web interface only, no live checks")
    s.add_argument("--no-auth", action="store_true",
                   help="skip the password (only if a reverse proxy already handles sign-in)")
    s.add_argument("--desktop", action="store_true", help=argparse.SUPPRESS)  # started by the desktop app
    s.add_argument("--parent-pid", type=int, default=None, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("status", help="show live paper-trading status")
    s.add_argument("--events", type=int, default=0, metavar="N", help="also show the last N events")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("reset", help="clear live state for one strategy")
    s.add_argument("strategy_id")
    s.set_defaults(func=cmd_reset)

    s = sub.add_parser("test-alert", help="send a test notification")
    s.set_defaults(func=cmd_test_alert)

    s = sub.add_parser("sample-data", help="write synthetic bars to CSV for offline testing")
    s.add_argument("--out", default="data/sample.csv")
    s.add_argument("--days", type=int, default=730)
    s.add_argument("--freq", default="1h")
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(func=cmd_sample_data)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, StrategyError, data_mod.DataError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
