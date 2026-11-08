# Trading Alerts engine

The Python package (`tradealerts`) behind both editions of Trading Alerts: the backtester,
forward-test runner, alerts, SQLite storage and the web interface. The self-hosted edition
runs it for one person; the hosted service runs it for many customers (see `cloud/`).

## Layout

```
src/tradealerts/
  strategy.py rules.py indicators.py   strategy files, the rule language, indicators
  simulator.py backtest.py analysis.py how trades fill; backtests; compare settings, luck check
  compute.py jobs.py                   the testing tools as plain functions; running them in workers
  data.py cache.py                     price data (Dukascopy, Yahoo, OANDA, CSV) and caching
  runner.py                            live checks: forward tests, alerts, price alerts, weekly report
  workspace.py config.py state.py      one person's settings, folders and SQLite store
  notify.py news.py weekly.py          alert channels, the economic calendar, the weekly report
  web/                                 the JSON API (routes/, one module per area) and app factory
  static/                              the web interface: plain scripts, no build step
  templates/                           config and example strategies for `tradealerts init`
tests/                                 pytest
```

The web interface is split into plain scripts that load in order and share one scope
(`web/app.py` lists them in `SCRIPTS`). There's no bundler: edit a file and reload.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,yahoo]"
pytest
tradealerts serve        # http://localhost:8321
```

## Strategy file format

The web interface writes these files for you. This is the format if you prefer
to edit by hand; both stay in sync because the interface reads the same files.

```yaml
name: My pullback rule
template: false            # true = backtest only, never sends live alerts

market:
  symbol: "^GSPC"          # Yahoo symbol, or a CSV path with provider: csv
  label: US 500            # shown in alerts
  provider: yahoo
  timeframe: 4h            # 5m, 15m, 30m, 1h, 4h, 1d ...
  # lookback_days: 720     # history to load (defaults depend on timeframe)

direction: long            # or short

entry:                     # checked on each closed bar
  all:
    - left: {indicator: pct_from_high, period: 30}
      op: "<="
      right: -3
    - left: close
      op: ">"
      right: {indicator: sma, period: 200}

exit:
  stop: {atr: 2, atr_period: 14}
  target: {r_multiple: 2}
  max_bars: 30
  when:                    # optional exit rule
    left: {indicator: rsi}
    op: ">"
    right: 70

risk:
  account_size: 1000
  risk_per_trade_pct: 0.5
  point_value: 1           # money per point per 1 unit of size
  size_step: 0.01
  min_size: 0.01
  currency: GBP

costs:
  spread: 0.6              # points

alerts:
  enabled: true
  priority: high           # min, low, default, high, urgent
```

### Conditions

| Form | Meaning |
|---|---|
| `{left: A, op: OP, right: B}` | compare two operands. `OP`: `<` `<=` `>` `>=` `==` `crosses_above` `crosses_below` |
| `{all: [...]}` / `{any: [...]}` | every / at least one condition true |
| `{not: {...}}` | invert a condition |
| `{time_between: ["10:00", "15:30"], tz: America/New_York}` | bar start time in a window (start inclusive, end exclusive) |
| `{weekdays: [mon, tue, wed, thu, fri]}` | day of week (optional `tz`) |

### Operands

| Form | Meaning |
|---|---|
| `close`, `open`, `high`, `low`, `volume` | price fields |
| `1.5` | a number |
| `{field: close, shift: 1}` | previous bar's close |
| `{indicator: NAME, ...params}` | an indicator (below) |
| any mapping + `shift: n`, `scale: x`, `add: y` | n bars ago, multiplied by x, plus y |

### Indicators

| Name | Parameters (defaults) | Description |
|---|---|---|
| `sma` | period=20, source='close' | Simple moving average |
| `ema` | period=20, source='close' | Exponential moving average |
| `macd` / `macd_signal` / `macd_hist` | fast=12, slow=26, signal=9, source='close' | MACD line, signal line, histogram |
| `rsi` | period=14, source='close' | Wilder's RSI, 0–100 |
| `pct_change` | period=1, source='close' | % change over `period` bars |
| `zscore` | period=20, source='close' | Std devs from the rolling mean |
| `atr` | period=14 | Average true range, in points |
| `atr_pct` | period=14 | ATR as % of close |
| `true_range` | | True range of each bar |
| `volatility` | period=20, source='close' | Std dev of bar returns, % |
| `bollinger_upper` / `bollinger_lower` | period=20, std=2.0, source='close' | Bollinger bands |
| `keltner_upper` / `keltner_lower` | period=20, atr_period=10, multiplier=2.0 | Keltner channels |
| `highest` | period=20, source='high' | Highest value of the last `period` bars (incl. this one) |
| `lowest` | period=20, source='low' | Lowest value of the last `period` bars (incl. this one) |
| `pct_from_high` | period=20 | % the close is below the `period`-bar high (≤ 0) |
| `pct_from_low` | period=20 | % the close is above the `period`-bar low (≥ 0) |
| `opening_range_high` / `opening_range_low` | session_start='09:30', minutes=30, tz='America/New_York' | Session opening range; empty until the range has finished |

Tip: for a channel breakout compare against the *previous* bars, e.g.
`{indicator: highest, period: 55, shift: 1}`; otherwise the current bar is
part of its own high.

### Stops and targets

| Form | Stop | Target |
|---|---|---|
| `{atr: 2, atr_period: 14}` | 2 × ATR from entry | 2 × ATR from entry |
| `{percent: 1.5}` | 1.5% from entry | 1.5% from entry |
| `{points: 40}` | 40 points from entry | 40 points from entry |
| `{level: <operand>}` | a price from your rules (e.g. opening range low) | same |
| `{r_multiple: 2}` | – | 2 × the stop distance |

## How the simulation works

The backtester and forward tests share one engine, modelled on how a CFD or
spread-bet broker fills orders:

1. Rules are checked on a candle's **close**. Unfinished candles are never used.
2. Entries fill at the **next candle's open**, buying at the ask and selling at the
   bid. The spread is each candle's real bid/ask spread when the data has it
   (OANDA), otherwise the strategy's spread setting.
3. Stops and targets are the prices you'd actually get. A long position's stop or
   target triggers when the **bid** reaches it, a short's when the **ask** does. A
   candle that opens beyond the stop fills at that open (gap risk).
4. If a stop and a target both fall inside one candle, **finer candles** (1-hour
   inside 4-hour or daily) decide which happened first. Without finer data the
   stop is assumed first.
5. **Overnight funding** is charged for every night a trade is held (22:00 UTC
   cut-off, weekends included) at the strategy's yearly rate.
6. 4-hour and daily candles start at **17:00 New York**, like most CFD/FX broker
   charts, so signals line up with what you see at your broker. Change this per
   strategy if your broker uses midnight UTC.
7. An exit rule closes at the next open; "close after N candles" closes at the
   Nth candle's close. One position per strategy at a time.

Each backtest reports the spread and funding it paid, and flags unusual price
jumps in the data, such as futures contract rolls or bad ticks.

## Live mode details

- On the first run a strategy starts from the latest closed bar; history is not replayed.
- If the app was offline, missed bars are processed in order so the paper record
  stays correct, but old entry signals are logged without being pushed.
- Strategy files are reloaded on every check, so edits apply from the next bar.
- `tradealerts status --events 20` shows paper balances, open positions and recent events.
- `tradealerts reset <strategy-id>` clears a strategy's live state (paper trade history is kept).
- `tradealerts run --once` runs a single check, for cron or systemd timers.
- `tradealerts run` (without the web interface) does everything `serve` does in the
  background: forward tests, alerts, price alerts, news reminders and the weekly report.

## Price data

Choose the source for the built-in markets in **Settings, Price data**:

| Source | Account? | What you get |
|---|---|---|
| **Dukascopy** (default) | None, no token | CFD-style bid and ask prices with years of history, so each candle's real spread is used. Fetched with [dukascopy-python](https://github.com/Eghosa-Osayande/dukascopy) (MIT). The first download of a market takes a while; downloads are saved in `data/cache` and later only new candles are fetched. |
| **Yahoo Finance** | None, no token | Delayed exchange prices. About 2 years of hourly history, about 60 days below that. Index futures (ES=F, NQ=F) jump at each contract roll, and there's no spread data. Personal use only. |
| **OANDA** | Free practice account and API token | Real CFD bid/ask candles from a broker. Enter the token in Settings after choosing OANDA, or put `OANDA_TOKEN` in `.env` and `oanda_token: ${OANDA_TOKEN}` under `data:` in config.yaml. Some markets depend on your region. |

Dukascopy and Yahoo are free public feeds with no service guarantee, and their
terms generally cover personal use. If you distribute this tool, each user
downloads prices themselves for their own use.

In the full editor you can also pick any Dukascopy instrument by its
dukascopy-python name (e.g. `INSTRUMENT_IDX_ASIA_E_N225JAP`), a Yahoo symbol, an
OANDA instrument, or a CSV file.

### CSV files

Upload them in Settings, Your price files (or with the Upload button in a strategy's
Market), then pick "My own CSV file". Self-hosted, you can also put files anywhere inside
the data folder and type the path. Columns: a time column (`timestamp`, `time`, `date` or `datetime`), then
`open, high, low, close`, and optionally `volume` and `spread`. Times can be dates,
date-times, or Unix timestamps in seconds or milliseconds. Set the time zone if
they have none. Finer candles are combined into the strategy's candle size, and
also used to check the order of stops and targets.

Dukascopy history can also be exported to CSV with
[dukascopy-node](https://www.dukascopy-node.app), for example to use outside the app:

```bash
npx dukascopy-node -i usa500idxusd -from 2023-01-01 -to 2025-01-01 -t h1 -f csv --directory data
```

Instrument codes include `usa500idxusd`, `usatechidxusd`, `usa30idxusd`,
`deuidxeur`, `gbridxgbp`, `xauusd`, `xagusd`, `lightcmdusd`, `eurusd`,
`gbpusd` and `btcusd`. Dukascopy sometimes blocks automated downloads; if so,
use their historical data page in a browser.

## Commands

| Command | What it does |
|---|---|
| `tradealerts serve [--host H] [--port P] [--no-runner]` | web interface plus live checks |
| `tradealerts init [dir]` | create config, folders and example templates |
| `tradealerts new "My rule"` | blank strategy in `strategies/` |
| `tradealerts validate [files]` | check config and strategies for mistakes |
| `tradealerts backtest FILE [--years N] [--data CSV] [--from DATE] [--to DATE]` | backtest and write a report |
| `tradealerts run [--once]` | live checks and alerts |
| `tradealerts status [--events N]` | paper-trading summary |
| `tradealerts test-alert` | send a test notification |
| `tradealerts sample-data` | synthetic bars for offline testing |

