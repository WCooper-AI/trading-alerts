"""Backtest output: terminal summary, CSVs and a self-contained HTML report."""

from __future__ import annotations

import html
import math
from pathlib import Path

import pandas as pd

from .backtest import BacktestResult

MIN_TRADES_FOR_CONFIDENCE = 30


def _money(v: float, sym: str) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}{sym}{abs(v):,.2f}"


def summary_rows(res: BacktestResult) -> list[tuple[str, str]]:
    m, sym = res.metrics, res.strategy.risk.symbol
    rows = [
        ("Market", f"{m['market']} · {m['timeframe']} · {m['direction']}"),
        ("Period", f"{m['start'][:16]} → {m['end'][:16]} ({m['bars']} bars)"),
        ("Start / end equity", f"{_money(m['start_equity'], sym)} → {_money(m['end_equity'], sym)}"),
        ("Total return", f"{m['total_return_pct']:+.2f}%"),
        ("Max drawdown", f"{m['max_drawdown_pct']:.2f}%"),
        ("Buy and hold (same period)", f"{m['buy_and_hold_pct']:+.2f}%"),
        ("Time in market", f"{m['time_in_market_pct']:.1f}%"),
        ("Trades", str(m["trades"])),
    ]
    if m["trades"]:
        pf = m["profit_factor"]
        rows += [
            ("Win rate", f"{m['win_rate_pct']:.1f}%"),
            ("Average win / loss", f"{_money(m['avg_win'], sym)} / {_money(m['avg_loss'], sym)}"),
            ("Profit factor", "No losses" if pf is None else f"{pf:.2f}"),
            ("Average trade", f"{_money(m['expectancy'], sym)} ({m['avg_r']:+.2f}R)"),
            ("Largest win / loss", f"{_money(m['largest_win'], sym)} / {_money(m['largest_loss'], sym)}"),
            ("Average bars held", f"{m['avg_bars_held']:.1f} ({m['avg_hold_hours']:.1f} hours)"),
            ("Longest winning / losing streak", f"{m['longest_winning_streak']} / {m['longest_losing_streak']}"),
            ("Sharpe / Sortino (annualised)", f"{_ratio(m.get('sharpe'))} / {_ratio(m.get('sortino'))}"),
            ("Longest time below a previous high", f"{m.get('longest_underwater_days', 0):.1f} days"),
            ("Exit reasons", ", ".join(f"{k}: {v}" for k, v in m["exit_reasons"].items())),
            ("Spread paid", _money(m.get("total_spread_cost", 0.0), sym)
             + (" (from the data's bid/ask)" if m.get("used_data_spread") else "")),
            ("Overnight funding paid", _money(m.get("total_funding", 0.0), sym)),
        ]
    return rows


def _ratio(v) -> str:
    return "–" if v is None else f"{v:.2f}"


def warnings_for(res: BacktestResult) -> list[str]:
    out = []
    n = res.metrics["trades"]
    if n == 0:
        out.append("No trades. Your entry rule never triggered on this data.")
    elif n < MIN_TRADES_FOR_CONFIDENCE:
        out.append(
            f"Only {n} trades. That's too few to tell skill from luck; test on more data or markets."
        )
    if res.skipped:
        out.append(f"{len(res.skipped)} signal(s) were skipped (e.g. position size rounded to zero).")
    out.extend(getattr(res, "notes", []) or [])
    if not res.strategy.funding_pct and res.metrics.get("avg_bars_held", 0) and res.metrics["trades"]:
        out.append("Overnight funding is set to 0%. Brokers charge it on positions held overnight; "
                   "set it under costs for a more realistic result.")
    out.append(
        "Backtests use past prices. Real fills can differ through slippage and fast markets. "
        "Past results don't predict future results."
    )
    return out


def format_summary(res: BacktestResult) -> str:
    rows = summary_rows(res)
    width = max(len(k) for k, _ in rows)
    lines = [f"Backtest: {res.strategy.name}", "=" * (len(res.strategy.name) + 10)]
    lines += [f"{k.ljust(width)}  {v}" for k, v in rows]
    lines += [""] + [f"! {w}" for w in warnings_for(res)]
    return "\n".join(lines)


def _svg_line(series: pd.Series, width=760, height=220, colour="#2563eb", baseline=None) -> str:
    s = series.dropna()
    if len(s) < 2:
        return ""
    if len(s) > 1500:
        s = s.iloc[:: math.ceil(len(s) / 1500)]
    lo, hi = float(s.min()), float(s.max())
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)
    span = hi - lo or 1.0
    pad = 8
    xs = [pad + i * (width - 2 * pad) / (len(s) - 1) for i in range(len(s))]
    ys = [pad + (hi - v) * (height - 2 * pad) / span for v in s]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    base = ""
    if baseline is not None:
        by = pad + (hi - baseline) * (height - 2 * pad) / span
        base = f'<line x1="{pad}" x2="{width - pad}" y1="{by:.1f}" y2="{by:.1f}" stroke="#94a3b8" stroke-dasharray="4 4"/>'
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">{base}'
        f'<polyline fill="none" stroke="{colour}" stroke-width="1.6" points="{pts}"/></svg>'
    )


def write_report(res: BacktestResult, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res.trades.to_csv(out / "trades.csv", index=False)
    res.equity.to_csv(out / "equity.csv")
    (out / "summary.txt").write_text(format_summary(res) + "\n", encoding="utf-8")

    eq = res.equity
    dd = (eq / eq.cummax() - 1) * 100
    rows = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in summary_rows(res)
    )
    warns = "".join(f"<li>{html.escape(w)}</li>" for w in warnings_for(res))
    trade_rows = ""
    if len(res.trades):
        cols = ["entry_time", "entry_price", "exit_time", "exit_price", "size", "exit_reason", "pnl", "r_multiple"]
        head = "".join(f"<th>{c}</th>" for c in cols)
        body = ""
        for _, t in res.trades.tail(200).iterrows():
            cells = []
            for col in cols:
                v = t[col]
                if isinstance(v, float):
                    v = f"{v:,.2f}"
                elif col.endswith("_time"):
                    v = str(v)[:16]
                cells.append(f'<td class="{"neg" if col == "pnl" and t["pnl"] < 0 else ""}">{html.escape(str(v))}</td>')
            body += "<tr>" + "".join(cells) + "</tr>"
        trade_rows = f"<h2>Trades (last 200)</h2><table class='trades'><tr>{head}</tr>{body}</table>"

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest: {html.escape(res.strategy.name)}</title>
<style>
body{{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;color:#0f172a}}
h1{{font-size:1.5rem;margin-bottom:.2rem}} h2{{font-size:1.1rem;margin-top:2rem}}
table{{border-collapse:collapse;width:100%}} th,td{{text-align:left;padding:.3rem .5rem;border-bottom:1px solid #e2e8f0}}
th{{font-weight:600;width:40%}} .trades th,.trades td{{font-size:.8rem;width:auto}} .neg{{color:#b91c1c}}
.warn{{background:#fef9c3;border:1px solid #fde047;border-radius:8px;padding:.6rem 1rem}}
.muted{{color:#64748b;font-size:.85rem}}
</style></head><body>
<h1>{html.escape(res.strategy.name)}</h1>
<p class="muted">Backtest of a rule set you defined. Simulated results, not a forecast.</p>
<table>{rows}</table>
<div class="warn"><ul>{warns}</ul></div>
<h2>Equity</h2>{_svg_line(eq, baseline=res.strategy.risk.account_size)}
<h2>Drawdown (%)</h2>{_svg_line(dd, colour="#dc2626", baseline=0)}
{trade_rows}
</body></html>"""
    (out / "report.html").write_text(page, encoding="utf-8")
    return out
