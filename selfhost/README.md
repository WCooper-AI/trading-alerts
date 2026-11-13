# Altius (self-hosted)

Backtesting, forward testing and phone alerts for **trading rules you write yourself**,
running on your own computer or server.

Build a strategy in the web interface, backtest it on historical data, then leave it
running. Each time a candle closes it checks your rules, pushes an alert to your phone
(ntfy, Telegram, Discord, Slack, email or any webhook) when they trigger, and keeps a paper-trading record
so you can see how the rules do live before you risk money.

> Not financial advice. You choose every rule and number; the included examples are
> format templates with placeholder values. See [DISCLAIMER.md](DISCLAIMER.md).

## Getting started

You need **Python 3.11 or newer** ([python.org](https://www.python.org/downloads/)).

**Linux and macOS:** open a terminal in this folder and run

```bash
./setup.sh
```

**Windows:** right-click `setup.ps1` and choose *Run with PowerShell*.

The setup script installs everything into its own folder (nothing system-wide) and asks
what you'd like to do:

| Choice | What you get |
|---|---|
| **1. Use it in your browser** | Starts now at http://localhost:8321, on this computer only. Next time run `trading-alerts` (Windows: the *Start Trading Alerts* shortcut it makes). |
| **2. Keep it running in the background** | Starts by itself when you log in, so forward tests and alerts carry on with no window open. Open http://localhost:8321 whenever you like. |
| **3. Build the desktop app** | A normal app with its own window and tray icon, and an installer you can keep. Needs Rust and Node.js (the script tells you how to get them); the first build takes about 10 minutes. |
| **4. Run it in Docker** | For a home server or VPS that's always on. Sets a password for you. |

Your strategies, settings and results live in a `TradingAlerts` folder in your home folder
(the desktop app keeps its own; move between them with Settings, Backup).

Forward tests and alerts only run while Altius is running. A computer that sleeps
can't send alerts, so for alerts around the clock use choice 2 on an always-on machine, or
Docker on a server.

### Then, in the app

1. **Create a strategy**: pick a market, pick an idea (buy the dip, follow the trend,
   bounce from oversold, moving average crossover or opening range breakout) and adjust a
   few plain-language settings.
2. **Test on past prices** over the last 1, 2, 3, 5 or 10 years. The results are written
   out in sentences, for example "£1,000 would have become £1,131".
3. **Start a forward test**, and **turn on alerts** if you like what you see. The first
   time, it walks you through getting alerts on your phone with the free ntfy app.

Experienced users can choose **Build from scratch** for the full rule builder.

## Features

- **Guided setup for beginners.** Five well-known trading ideas, each with a few
  plain-language settings, results written in sentences, and phone alerts set up
  in the same dialog.
- **Full rule builder for everyone else.** Build rules from dropdowns, see them
  written out as a plain-English sentence, and backtest unsaved changes with one click.
- **Rules without code.** Combine 20+ indicators with all / any / none groups,
  crossovers, time-of-day windows and weekday filters.
- **Honest backtests.** Signals use closed candles only, entries fill at the next
  open, a stop and target hit in the same bar count as a stop, gaps fill at the
  open, and spread is charged. The test suite checks for lookahead bias.
- **Risk-based sizing.** Size each trade so the stop loses a set % of the account,
  rounded to your broker's size step.
- **Forward tests with a dashboard.** Start one with a button and watch how a
  strategy does on live prices from that moment, with or without phone alerts.
  It uses the same engine as the backtester, so the results are directly comparable.
- **Optimiser.** Test a grid of settings in the background (for example the RSI level
  from 20 to 40) and see the results as a heatmap. A hold-out period and anchored
  walk-forward run across the whole grid, ranking uses training data only, and it warns
  you when you may be picking the luckiest value.
- **Train/test split.** Keep 10–50% of the history back on any backtest and see the
  results on data the settings never saw.
- **Luck check.** Is a backtest's profit real or luck? Resamples the trades thousands
  of times, checks both halves of the period, and runs the rules on random price
  paths built from the market's own candles.
- **Live-vs-backtest range check.** Shows whether a forward test sits below, within or
  above the range the strategy's own backtest produced over stretches of the same
  length. Positions, not a score.
- **Slippage and stock costs.** Slippage on every market fill, plus commission,
  commission % and borrow cost for stocks, on top of spread and overnight funding.
- **Live markets page.** Candlestick charts that refresh every 30 seconds, with a
  strategy's signals and paper trades marked, and **price alerts** ("tell me when
  gold goes above 2,700"): click the chart to set one.
- **Trade journal.** Log the trades you actually place, link each to the forward-test
  trade it came from, and see your slippage. **Log real trade** pre-fills an entry from
  a paper trade. Exports to CSV.
- **Test strategies together.** Several strategies on one shared balance: combined
  result, most money at risk at once, and how alike their daily results are.
- **News filter.** A rule (and a guided-setup switch) to stay out around high-impact
  news. The US jobs report is built in; the weekly Forex Factory calendar is saved
  automatically so history builds up, and you can add your own events (Settings).
- **Weekly report** by email (your own SMTP account) or your other alert channels,
  and as a printable page you can save as PDF.
- **Share strategies.** Export the rules as a file, or copy a share link; imports
  always arrive as drafts, and results are never included.
- **Backup and restore** everything in one zip, to move between the desktop app and
  a server. A safety copy is made before every restore. **Redacted backups**
  (`/api/backup?redact=true`) strip passwords, API tokens, webhook addresses and ntfy
  topics so you can share a setup safely.
- **Your own values.** Every setting starts blank; nothing is suggested for you.
- **Chart Desk interface** with light and dark themes and a choice of accent colour.
- **Desktop app, browser or server.** Use it in your browser, use the desktop app
  (Windows, macOS, Linux; for now only the Linux `.deb` and `.AppImage` are attached to
  releases, Windows and macOS you build yourself), or run it 24/7 in Docker on a home server or VPS. Everything
  stays on your own machine: no cloud account and no outside requests from the browser.
  Works with public ntfy.sh or your own ntfy server.
- **Your own price files.** Upload CSV files of candles and test strategies on them.


## Putting it on a server

`./setup.sh --docker` (or `docker compose up -d --build` after copying `.env.example` to
`.env` and setting `TRADEALERTS_PASSWORD`) runs it in one container. Everything lives in
`./workspace`. The port is published on 127.0.0.1 only: keep it private and put a reverse
proxy with HTTPS in front. With Caddy:

```
alerts.example.com {
    reverse_proxy 127.0.0.1:8321
}
```

The web interface asks for `TRADEALERTS_PASSWORD`. It refuses to listen on a public address
without one unless you pass `--no-auth`, which is only for when your proxy already handles
sign-in. Sessions are signed cookies (HttpOnly, SameSite=Strict), repeated wrong passwords
are slowed down, and the pages use a strict Content Security Policy.

## Phone alerts with ntfy

1. Install the ntfy app (Android or iOS) and subscribe to a long, random topic name.
2. In **Settings**, turn on ntfy, enter the topic (and server/token if you self-host), and save.
3. Press **Send test alert**.

On public ntfy.sh anyone who guesses your topic can read it, so make it long and random, or
self-host ntfy with an access token.

## Reference

The strategy file format, every indicator, how the simulation fills trades, price data
sources, CSV files and the command line are described in [engine/README.md](engine/README.md).
