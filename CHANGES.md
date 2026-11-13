# Changes in 0.3.1 (reliability patch)

- **RSI and ATR use exact Wilder smoothing.** The first value is the average of the first
  `period` values, then Wilder's recursion, matching TradingView and TA-Lib (checked against
  the StockCharts RSI table). Values differ slightly from 0.3.0 in the first few dozen candles.
- **Intraday Sharpe and Sortino suit session-limited markets.** Bars per year come from the
  candles actually seen per weekday (capped at 24 h), so hourly US stocks count about 7 bars
  a day, not 24. FX, crypto, daily and longer timeframes are unchanged.
- **Monte Carlo adds trade results when compounding is off.** With `risk: compound: false`
  the reshuffled paths add each trade's money result instead of compounding its % gain.
- Candles with an impossible high/low are still repaired, but now counted: `repaired_bars`
  in backtest metrics and a "repaired" data-quality warning.
- Spreads that are negative or infinite are rejected; crossed broker quotes count as 0.
- Strategy numbers must be finite (`.nan` / `.inf` rejected), and whole-number fields no
  longer accept `true`.

# Changes in 0.2

## Self-hosted setup

`setup.sh` (Linux, macOS) and `setup.ps1` (Windows) install everything into their own folder
and offer: use it in the browser, keep it running in the background (systemd user service,
launchd agent or a Windows logon task), build the desktop app, or run it in Docker.

## Bugs fixed

**Rules and backtests**
- "None of these" / "not" conditions counted as true while an indicator was still warming
  up. For example "not RSI above 70" was true for the first 14 candles, and "not above the
  opening range high" was true every morning before the range had finished. Unknown now
  stays unknown, so those candles don't signal.
- A time window or weekday rule with a misspelt time zone was only caught when the rules
  ran; it's now caught when you save. Times like 24:30 are no longer accepted.
- RSI read 100 (overbought) on completely flat prices; it's now 50.
- Exits by "close after N candles" and at the end of a backtest were stamped at the start
  of the candle although they fill at its close, so charts showed them one candle early.

**Desktop app**
- Downloads (backups, the journal CSV, exported strategies) didn't save from the desktop
  window. They now go to the Downloads folder, with a notification.
- "View this week's report" tried to open a new window, which the desktop window blocks.
  It now opens in the same window; links to other websites open in the browser.
- Confirmation boxes (delete, stop a forward test, unsaved changes, restore) used the
  browser's built-in `confirm()`, which isn't reliable in desktop webviews. They're now the
  app's own dialogs, on every edition.

**Interface**
- Signing in again after a session expired started an extra status refresh each time.
- Delete and Duplicate failed silently on errors; the alerts switch showed alerts as on
  when saving had actually failed.
- The News calendar section was written but never shown in Settings. It's back.
- There was no way to sign out when a password is set. There's now a Sign out button.
- The weekly report went out at the server's time (UTC in Docker); it now has a time zone.
- You can upload CSV price files (Settings, Your price files, or Upload in a strategy's
  Market) instead of copying them into the data folder.

**Security and data**
- The ntfy access token was sent back to the browser on the Settings page; like the email
  password, it now stays on the server ("Saved. Leave blank to keep it").
- Alert addresses must now be web addresses (https:// or http://).
- Journal CSV export: notes starting with `=`, `+`, `-` or `@` could run as formulas when
  opened in a spreadsheet. They're now defused.
- Journal: the "exit before entry" check compared text instead of times.
- Restore copied the backup's database file over the live one. It now copies the rows into
  the current tables (older backups keep working, damaged or foreign files are refused),
  checks the zip's unpacked size, and keeps the last five safety copies instead of all.
- The live checker and the web page could write the news calendar at the same moment and
  corrupt it, and a failing calendar feed was retried on every check. Writes are now locked
  and atomic, and a failed fetch waits an hour.
- Two processes refreshing the same downloaded-price file used the same temporary name.
- The failed-password tracker could grow without limit under attack.

**Command line**
- `tradealerts new "Buy: the dip"` wrote a broken file (names with colons or quotes).
- `tradealerts run` didn't send news reminders or the weekly report, or update the news
  calendar, which `tradealerts serve` does. It now does all of them.
- `tradealerts reset` created an empty database when there wasn't one.

**Builds**
- The desktop workflow used GitHub's `macos-13` runner, retired in December 2025. Intel Mac
  builds now use `macos-15-intel`.

## Clean-up

- `web.py` (2,000 lines) is now `web/`: the app factory, shared helpers, and one module of
  routes per area (`routes/strategies.py`, `runs.py`, `testing.py`, `markets.py`, ...).
- `app.js` (4,500 lines) is now 15 scripts by area (`util.js`, `editor.js`, `dashboard.js`,
  ...), still with no build step.
- Every request works on a *workspace* (one person's settings, strategies and data), which
  is what lets the same engine serve one person or many.
- Loading settings no longer changes global state (the news folder is set explicitly).
- The testing tools (`compute.py`) are plain functions, so the hosted service can run them
  in worker processes.
- 17 new tests (191 in total).
