"""Price data: loading, normalising and resampling OHLCV candles.

Every provider returns candles as a DataFrame with a sorted, unique, UTC
DatetimeIndex (the candle's *start* time) and float columns open, high, low,
close, volume, plus an optional ``spread`` column (ask minus bid, in price units)
when the source has bid and ask prices.

``fetch_bars`` also returns the finer candles a coarser timeframe was built from
(or fetched separately), so the simulator can tell which of a stop or target was
hit first inside one candle.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import timeframes

OHLCV = ["open", "high", "low", "close", "volume"]

# Where candles longer than an hour start. Most CFD and FX brokers start the
# trading day at 17:00 New York time, so 4-hour and daily candles line up with
# what you see on their charts. "utc" starts them at midnight UTC.
ALIGNMENTS = {"new_york": ("America/New_York", 17), "utc": ("UTC", 0)}
DEFAULT_ALIGNMENT = "new_york"


class DataError(RuntimeError):
    pass


def normalise(df: pd.DataFrame, assume_tz: str = "UTC") -> pd.DataFrame:
    """Lower-case columns, UTC index, sorted, de-duplicated, numeric, junk candles removed."""
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "adj close" in df.columns and "close" not in df.columns:
        df = df.rename(columns={"adj close": "close"})
    missing = [c for c in ["open", "high", "low", "close"] if c not in df.columns]
    if missing:
        raise DataError(f"Price data is missing columns: {', '.join(missing)}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    cols = OHLCV + (["spread"] if "spread" in df.columns else [])
    df = df[cols]
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize(assume_tz)
    df.index = df.index.tz_convert("UTC")
    df.index.name = "time"
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    disordered = int((df.index[1:] <= df.index[:-1]).sum()) if len(df) >= 2 else 0
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.astype(float)
    if not np.isfinite(df[OHLCV].to_numpy()).all():
        raise DataError("Price data contains non-finite (infinite) OHLCV values.")
    if not (df["volume"] >= 0).all():
        raise DataError("Price data contains negative volume.")
    if "spread" in df.columns:
        sp = df["spread"].dropna()
        if not (np.isfinite(sp).all() and (sp >= 0).all()):
            raise DataError("Price data has spreads that are negative or not a number. "
                            "Spread is ask minus bid, so it must be 0 or more")
    # Repair candles whose high/low don't contain the open/close (bad ticks in free feeds),
    # and keep a count so data-quality warnings can mention it.
    top = df[["open", "high", "low", "close"]].max(axis=1)
    bottom = df[["open", "high", "low", "close"]].min(axis=1)
    repaired = (df["high"] != top) | (df["low"] != bottom)
    df["high"], df["low"] = top, bottom
    # Drop placeholder candles (no movement, no volume) in feeds that otherwise report volume:
    # free feeds insert them for closed sessions and they create false signals.
    if df["volume"].sum() > 0:
        flat = (df["high"] == df["low"]) & (df["volume"] == 0)
        df = df[~flat]
        repaired = repaired[~flat]
    df.attrs["repaired_bars"] = int(repaired.sum())
    df.attrs["disordered_bars"] = disordered
    return df


def infer_bar_length(df: pd.DataFrame) -> pd.Timedelta | None:
    if len(df) < 2:
        return None
    diffs = df.index.to_series().diff().dropna()
    return diffs.mode().iloc[0] if not diffs.empty else None


def resample_ohlcv(df: pd.DataFrame, timeframe: str, alignment: str = DEFAULT_ALIGNMENT) -> pd.DataFrame:
    """Aggregate candles up to ``timeframe``. Candles are labelled by their start time."""
    target = timeframes.to_timedelta(timeframe)
    current = infer_bar_length(df)
    if current is not None and current > target:
        raise DataError(
            f"Data candles are {current} long, which is longer than the strategy's "
            f"{timeframe}. Use finer data or a longer timeframe."
        )
    if current is not None and current == target:
        return df
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if "spread" in df.columns:
        agg["spread"] = "mean"
    tz, start_hour = ALIGNMENTS.get(alignment, ALIGNMENTS[DEFAULT_ALIGNMENT])
    hours = target / pd.Timedelta(hours=1)
    if hours > 1 and tz != "UTC":
        local = df.tz_convert(tz)
        if target >= pd.Timedelta(days=1):
            offset = pd.Timedelta(hours=start_hour)
        else:
            offset = pd.Timedelta(hours=start_hour % hours)
        rule = f"{int(round(hours))}h"  # fixed-length hours, so the offset applies to daily candles too
        out = local.resample(rule, label="left", closed="left", origin="start_day", offset=offset).agg(agg)
        out = out.tz_convert("UTC")
    else:
        out = df.resample(timeframes.to_pandas_rule(timeframe), label="left", closed="left",
                          origin="start_day").agg(agg)
    out = out.dropna(subset=["open", "high", "low", "close"])
    out.attrs.update(df.attrs)
    return out


def closed_bars(df: pd.DataFrame, timeframe: str, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Drop any candle that hasn't finished yet, so rules only see completed candles."""
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    length = timeframes.to_timedelta(timeframe)
    return df[df.index + length <= now]


def find_jumps(df: pd.DataFrame, timeframe: str, threshold_atr: float = 4.0) -> pd.DataFrame:
    """Candles that open far from the previous close without a weekend or holiday in between.

    In futures data these are usually contract rolls; in any feed they can be bad
    data. Either way, trades around them may not be realistic.
    """
    if len(df) < 30:
        return df.iloc[0:0]
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(20, min_periods=10).median()
    gap = (df["open"] - prev_close).abs()
    step = df.index.to_series().diff()
    normal_step = step <= timeframes.to_timedelta(timeframe) * 1.5
    jumps = df[(gap > threshold_atr * atr) & normal_step & atr.notna()]
    return jumps.assign(gap=gap[jumps.index])


# --------------------------------------------------------------------------- providers


class Provider:
    """Base class: ``fetch_bars`` returns (candles, finer candles or None)."""

    def fetch_bars(self, symbol: str, timeframe: str, lookback_days: int,
                   alignment: str = DEFAULT_ALIGNMENT) -> tuple[pd.DataFrame, pd.DataFrame | None]:
        raise NotImplementedError

    def fetch(self, symbol: str, timeframe: str, lookback_days: int,
              alignment: str = DEFAULT_ALIGNMENT) -> pd.DataFrame:
        return self.fetch_bars(symbol, timeframe, lookback_days, alignment)[0]


def _with_fine(raw: pd.DataFrame, timeframe: str, alignment: str) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    bars = resample_ohlcv(raw, timeframe, alignment)
    fine = raw if len(raw) > len(bars) else None
    return bars, fine


class CSVProvider(Provider):
    """Reads candles from a CSV file. ``symbol`` is the file path.

    Expected columns: a time column (timestamp/time/date/datetime) plus open, high,
    low, close and optionally volume and spread. Times can be dates, date-times, or
    Unix timestamps in seconds or milliseconds (as written by dukascopy-node).
    Naive date-times are read in ``tz`` (default UTC).
    """

    TIME_COLUMNS = ("timestamp", "time", "datetime", "date")

    def __init__(self, base_dir: str | Path = ".", tz: str = "UTC", allowed_root: str | Path | None = None):
        self.base_dir = Path(base_dir)
        self.tz = tz
        self.allowed_root = Path(allowed_root).resolve() if allowed_root else None

    def load(self, path: str | Path) -> pd.DataFrame:
        p = Path(path)
        if not p.is_absolute():
            p = self.base_dir / p
        if self.allowed_root is not None:
            resolved = p.resolve()
            if resolved != self.allowed_root and self.allowed_root not in resolved.parents:
                raise DataError(f"CSV files must be inside the data folder ({self.allowed_root})")
        if not p.exists():
            raise DataError(f"CSV file not found: {p}")
        raw = pd.read_csv(p)
        cols = {c.lower().strip(): c for c in raw.columns}
        time_col = next((cols[c] for c in self.TIME_COLUMNS if c in cols), None)
        if time_col is None:
            raise DataError(f"{p} needs a time column named one of: {', '.join(self.TIME_COLUMNS)}")
        t = raw[time_col]
        if pd.api.types.is_numeric_dtype(t):
            unit = "ms" if t.abs().max() > 1e11 else "s"
            idx = pd.to_datetime(t, unit=unit, utc=True)
        else:
            idx = pd.to_datetime(t, utc=False)
        raw = raw.drop(columns=[time_col])
        raw.index = pd.DatetimeIndex(idx)
        return normalise(raw, assume_tz=self.tz)

    def fetch_bars(self, symbol, timeframe, lookback_days, alignment=DEFAULT_ALIGNMENT):
        df = self.load(symbol)
        if lookback_days and not df.empty:
            df = df[df.index >= df.index[-1] - pd.Timedelta(days=lookback_days)]
        return _with_fine(df, timeframe, alignment)


class YahooProvider(Provider):
    """Free delayed data from Yahoo Finance via the ``yfinance`` package.

    Yahoo limits intraday history: about 60 days for candles under 1h and about
    730 days for 1h. Longer timeframes are built from 1h candles. Futures symbols
    (ending =F) jump at each contract roll, which CFD prices don't do.
    Yahoo's data is for personal use; check their terms for anything else.
    """

    _MINUTE_INTERVALS = [1, 2, 5, 15, 30]

    def _plan(self, timeframe: str, lookback_days: int) -> tuple[str, int]:
        mins = timeframes.minutes(timeframe)
        if mins >= 1440 and mins % 1440 == 0:
            return "1d", lookback_days
        if mins >= 60 and mins % 60 == 0:
            return "60m", min(lookback_days, 729)
        base = max(i for i in self._MINUTE_INTERVALS if mins % i == 0)
        limit = 7 if base == 1 else 59
        return f"{base}m", min(lookback_days, limit)

    def fetch_bars(self, symbol, timeframe, lookback_days, alignment=DEFAULT_ALIGNMENT):
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - depends on install
            raise DataError("The Yahoo data source needs yfinance: pip install 'tradealerts[yahoo]'") from exc
        interval, days = self._plan(timeframe, lookback_days)
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(days=days)
        raw = yf.Ticker(symbol).history(start=start, end=end, interval=interval, auto_adjust=False, actions=False)
        if raw is None or raw.empty:
            raise DataError(f"Yahoo returned no data for {symbol!r} ({interval}, {days}d)")
        return _with_fine(normalise(raw), timeframe, alignment)


class OandaProvider(Provider):
    """CFD and FX candles from OANDA's v20 REST API, with bid and ask prices.

    Needs an API token from a (free) OANDA practice or live account. Candles are
    fetched at the strategy's own size where OANDA has it, aligned to 17:00 New York
    like OANDA's charts, and the bid/ask gap is kept as a per-candle spread.
    """

    HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}
    GRANULARITY = {5: "M5", 15: "M15", 30: "M30", 60: "H1", 120: "H2", 240: "H4", 1440: "D"}
    PAGE = 5000

    def __init__(self, token: str, environment: str = "practice", opener=None):
        if not token:
            raise DataError("Add your OANDA API token in Settings, Price data")
        if environment not in self.HOSTS:
            raise DataError("OANDA environment must be practice or live")
        self.token = token
        self.base = self.HOSTS[environment]
        self._open = opener or urllib.request.urlopen

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept-Datetime-Format": "RFC3339",
            "User-Agent": "tradealerts",
        })
        for attempt in range(3):
            try:
                with self._open(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
                try:
                    message = json.loads(body).get("errorMessage", body)
                except (ValueError, AttributeError):
                    message = body
                if exc.code == 401:
                    raise DataError("OANDA rejected the API token. Check it in Settings, Price data") from None
                if exc.code == 429 and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise DataError(f"OANDA error {exc.code}: {message or exc.reason}") from None
            except urllib.error.URLError as exc:
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise DataError(f"Couldn't reach OANDA: {exc.reason}") from None
        raise DataError("OANDA didn't respond")  # pragma: no cover

    @staticmethod
    def parse(candles: list[dict]) -> pd.DataFrame:
        rows, idx = [], []
        for c in candles:
            if not c.get("complete", True):
                continue
            mid = c.get("mid")
            bid, ask = c.get("bid"), c.get("ask")
            if mid is None and bid and ask:
                mid = {k: (float(bid[k]) + float(ask[k])) / 2 for k in "ohlc"}
            if mid is None:
                continue
            spread = np.nan
            if bid and ask:
                # crossed quotes (ask below bid) happen now and then: treat as no spread
                spread = max(0.0, ((float(ask["o"]) - float(bid["o"])) + (float(ask["c"]) - float(bid["c"]))) / 2)
            idx.append(pd.Timestamp(c["time"]))
            rows.append({"open": float(mid["o"]), "high": float(mid["h"]), "low": float(mid["l"]),
                         "close": float(mid["c"]), "volume": float(c.get("volume", 0)), "spread": spread})
        df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
        if df.empty:
            return pd.DataFrame(columns=OHLCV + ["spread"], index=pd.DatetimeIndex([], tz="UTC"))
        return normalise(df)

    def _candles(self, instrument: str, granularity: str, start: pd.Timestamp, alignment: str) -> pd.DataFrame:
        frames, cursor, now = [], start, pd.Timestamp.now(tz="UTC")
        tz, hour = ALIGNMENTS.get(alignment, ALIGNMENTS[DEFAULT_ALIGNMENT])
        while cursor < now:
            data = self._get(f"/v3/instruments/{instrument}/candles", {
                "price": "MBA", "granularity": granularity, "count": self.PAGE,
                "from": cursor.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
                "alignmentTimezone": tz, "dailyAlignment": hour,
            })
            candles = data.get("candles", [])
            if not candles:
                break
            frames.append(self.parse(candles))
            last = pd.Timestamp(candles[-1]["time"])
            if len(candles) < self.PAGE or last <= cursor:
                break
            cursor = last + pd.Timedelta(seconds=1)
        if not frames:
            raise DataError(f"OANDA returned no candles for {instrument} ({granularity})")
        df = pd.concat(frames)
        return df[~df.index.duplicated(keep="last")].sort_index()

    def fetch_bars(self, symbol, timeframe, lookback_days, alignment=DEFAULT_ALIGNMENT):
        mins = timeframes.minutes(timeframe)
        start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=lookback_days)
        gran = self.GRANULARITY.get(mins)
        if gran is None:
            # No native size (e.g. 3h): build it from 1h or smaller candles.
            base = max(m for m in self.GRANULARITY if m <= mins and mins % m == 0)
            raw = self._candles(symbol, self.GRANULARITY[base], start, alignment)
            return _with_fine(raw, timeframe, alignment)
        bars = self._candles(symbol, gran, start, alignment)
        fine = None
        if mins >= 120 and lookback_days <= 1100:
            fine = self._candles(symbol, "H1", start, alignment)
        elif mins == 60 and lookback_days <= 120:
            fine = self._candles(symbol, "M15", start, alignment)
        return bars, fine


class _TimeoutRequests:
    """Stands in for ``requests`` inside dukascopy-python, which calls
    ``requests.get`` with no timeout: a stalled connection would hang the chart forever."""

    TIMEOUT = (10, 30)  # seconds to connect, seconds between bytes

    def __init__(self, real):
        self._real = real

    def get(self, *args, **kwargs):
        kwargs.setdefault("timeout", self.TIMEOUT)
        return self._real.get(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _add_timeouts() -> None:
    import sys

    try:
        import requests
    except ImportError:  # pragma: no cover
        return
    for name, mod in list(sys.modules.items()):
        if name.startswith("dukascopy_python") and getattr(mod, "requests", None) is requests:
            mod.requests = _TimeoutRequests(requests)


class DukascopyProvider(Provider):
    """Free CFD-style prices from Dukascopy's public chart feed, via dukascopy-python.

    No account or token. Bid and ask candles are both fetched, so the mid price
    and each candle's real spread are known. Downloads are saved under the data
    folder and only new candles are fetched next time, because the feed is slow.
    ``symbol`` is a dukascopy-python instrument constant name (e.g.
    INSTRUMENT_IDX_AMERICA_E_SANDP_500) or a raw Dukascopy code (e.g. E_SandP-500).
    """

    NATIVE = {5: "INTERVAL_MIN_5", 15: "INTERVAL_MIN_15", 30: "INTERVAL_MIN_30", 60: "INTERVAL_HOUR_1"}
    CHUNK = pd.Timedelta(days=180)
    _locks: dict[str, Any] = {}
    _memo: dict[str, Any] = {}  # path -> (file stamp, frame, covered_from), shared by all instances
    stale = False  # set when the last download fell back to saved prices

    def __init__(self, cache_dir: str | Path | None = None, lib=None):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._lib = lib

    # ------------------------------------------------------------ library

    def lib(self):
        if self._lib is None:
            # 4.x has the classic API at the top level; 5.x moved it to .compat.
            # (5.0.0 fails to import on Python < 3.14, hence the broad except.)
            try:
                import dukascopy_python as duka
                from dukascopy_python import instruments as ins
                if not hasattr(duka, "fetch"):
                    from dukascopy_python import compat as duka
                    from dukascopy_python.compat import instruments as ins
            except Exception as exc:  # pragma: no cover - depends on install
                raise DataError(
                    "The Dukascopy data source needs dukascopy-python 4.x: "
                    "pip install 'dukascopy-python>=4.0,<5'"
                ) from exc
            _add_timeouts()
            self._lib = (duka, ins)
        return self._lib

    def instrument(self, symbol: str) -> str:
        _, ins = self.lib()
        return getattr(ins, symbol) if symbol.startswith("INSTRUMENT_") and hasattr(ins, symbol) else symbol

    # ------------------------------------------------------------ fetching

    MAX_FAILURES = 16  # per download, so an outage can't keep us retrying for ages

    @staticmethod
    def _is_network_error(exc: Exception) -> bool:
        if isinstance(exc, (OSError, TimeoutError, urllib.error.URLError)):
            return True
        try:
            import requests
            return isinstance(exc, requests.exceptions.RequestException)
        except ImportError:  # pragma: no cover
            return False

    def _fetch_piece(self, inst, interval, side, start, end, failures, min_span=pd.Timedelta(days=1)) -> list[pd.DataFrame]:
        """Fetch one range. The library can fail on ranges with no data (holidays,
        feed gaps), so on such an error split the range and try the halves.
        Network errors stop straight away."""
        duka, _ = self.lib()
        try:
            df = duka.fetch(inst, getattr(duka, interval), side, start.to_pydatetime(), end.to_pydatetime(),
                            max_retries=2)
            return [df] if df is not None and len(df) else []
        except Exception as exc:
            if self._is_network_error(exc):
                raise DataError(f"Couldn't reach Dukascopy ({type(exc).__name__}). Check your internet "
                                "connection, or try again in a minute.") from None
            failures[0] += 1
            if failures[0] > self.MAX_FAILURES:
                raise DataError("Dukascopy's feed kept failing for this market. Try again later, "
                                "or switch Price data in Settings.") from None
            if end - start <= min_span:
                return []
            mid = start + (end - start) / 2
            return (self._fetch_piece(inst, interval, side, start, mid, failures, min_span)
                    + self._fetch_piece(inst, interval, side, mid, end, failures, min_span))

    def _fetch_range(self, inst, interval, side, start, end) -> pd.DataFrame:
        frames, cur, failures = [], start, [0]
        while cur < end:
            nxt = min(cur + self.CHUNK, end)
            frames += self._fetch_piece(inst, interval, side, cur, nxt, failures)
            cur = nxt
        if not frames:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        df = pd.concat(frames)
        df.columns = [str(c).lower() for c in df.columns]
        df.index = pd.to_datetime(df.index, utc=True)
        return normalise(df)

    def _cache_path(self, inst: str, interval: str, side: str) -> Path | None:
        if not self.cache_dir:
            return None
        safe = "".join(ch if ch.isalnum() else "_" for ch in inst)
        return self.cache_dir / f"dukascopy_{safe}_{interval}_{side}.csv.gz"

    def _side(self, inst: str, interval: str, side: str, start: pd.Timestamp, now: pd.Timestamp) -> pd.DataFrame:
        path = self._cache_path(inst, interval, side)
        if path is None:
            return self._fetch_range(inst, interval, side, start, now)
        import threading
        lock = self._locks.setdefault(str(path), threading.Lock())
        with lock:
            meta_path = path.with_suffix(".json")
            cached, covered_from = self._read_cache(path, meta_path)
            parts, changed = [], False
            if cached is None or cached.empty:
                parts.append(self._fetch_range(inst, interval, side, start, now))
                covered_from, changed = start, True
            else:
                if start < covered_from:
                    parts.append(self._fetch_range(inst, interval, side, start, covered_from))
                    covered_from, changed = start, True
                parts.append(cached)
                try:
                    fresh = self._fetch_range(inst, interval, side, cached.index[-1] - pd.Timedelta(hours=2), now)
                except DataError:
                    # The feed is down or slow: show what we already have rather than nothing.
                    self.stale = True
                    fresh = None
                if fresh is not None and len(fresh):
                    parts.append(fresh)
                    changed = True
            parts = [p for p in parts if p is not None and len(p)]
            if not parts:
                return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
            df = pd.concat(parts) if len(parts) > 1 else parts[0]
            df = df[~df.index.duplicated(keep="last")].sort_index()
            df = df[df.index < now]
            if changed and (cached is None or len(df) != len(cached) or not df.index.equals(cached.index)
                            or not df.tail(3).equals(cached.tail(3))):
                path.parent.mkdir(parents=True, exist_ok=True)
                import os
                import threading
                # unique names: several processes may refresh the same shared cache at once
                tag = f".{os.getpid()}.{threading.get_ident()}.tmp"
                tmp = path.with_name(path.name + tag)
                df.to_csv(tmp, compression="gzip")
                tmp.replace(path)
                meta_tmp = meta_path.with_name(meta_path.name + tag)
                meta_tmp.write_text(json.dumps({"from": covered_from.isoformat()}))
                meta_tmp.replace(meta_path)
                self._memo[str(path)] = (self._stamp(path), df, covered_from)
            return df[df.index >= start]

    @staticmethod
    def _stamp(path: Path):
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)

    def _read_cache(self, path: Path, meta_path: Path):
        """The saved download for this market, kept in memory between calls so the
        live chart doesn't re-read a big file every refresh."""
        if not (path.exists() and meta_path.exists()):
            return None, None
        try:
            stamp = self._stamp(path)
            hit = self._memo.get(str(path))
            if hit and hit[0] == stamp:
                return hit[1], hit[2]
            cached = pd.read_csv(path, index_col=0, parse_dates=True)
            cached.index = pd.to_datetime(cached.index, utc=True)
            covered_from = pd.Timestamp(json.loads(meta_path.read_text())["from"])
        except (ValueError, KeyError, OSError):
            return None, None
        self._memo[str(path)] = (stamp, cached, covered_from)
        return cached, covered_from

    def _candles(self, symbol: str, minutes: int, start: pd.Timestamp) -> pd.DataFrame:
        from concurrent.futures import ThreadPoolExecutor

        duka, _ = self.lib()
        inst = self.instrument(symbol)
        interval = self.NATIVE[minutes]
        now = pd.Timestamp.now(tz="UTC").floor("min")
        with ThreadPoolExecutor(max_workers=2) as pool:
            bid_f = pool.submit(self._side, inst, interval, duka.OFFER_SIDE_BID, start, now)
            ask_f = pool.submit(self._side, inst, interval, duka.OFFER_SIDE_ASK, start, now)
            bid = bid_f.result()
            try:
                ask = ask_f.result()
            except Exception:
                ask = None
        if bid.empty:
            raise DataError(
                f"Dukascopy returned no prices for {symbol}. Their free feed is sometimes slow or "
                "briefly unavailable, so try again in a minute."
            )
        if ask is None or ask.empty:
            return bid
        both = bid.merge(ask, how="inner", left_index=True, right_index=True, suffixes=("_bid", "_ask"))
        if both.empty:
            return bid
        out = pd.DataFrame(index=both.index)
        for k in ("open", "high", "low", "close"):
            out[k] = (both[f"{k}_bid"] + both[f"{k}_ask"]) / 2
        out["volume"] = both["volume_bid"]
        out["spread"] = (((both["open_ask"] - both["open_bid"]) + (both["close_ask"] - both["close_bid"])) / 2).clip(lower=0)
        return normalise(out)

    def fetch_bars(self, symbol, timeframe, lookback_days, alignment=DEFAULT_ALIGNMENT):
        mins = timeframes.minutes(timeframe)
        start = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=lookback_days)).floor("h")
        if mins in self.NATIVE:
            bars = self._candles(symbol, mins, start)
            fine = None
            if mins == 60 and lookback_days <= 120:
                fine = self._candles(symbol, 15, start)
            return bars, fine
        if mins > 60 and mins % 60 == 0:
            return _with_fine(self._candles(symbol, 60, start), timeframe, alignment)
        base = max(m for m in self.NATIVE if mins % m == 0 and m < mins)
        return _with_fine(self._candles(symbol, base, start), timeframe, alignment)


class MarketProvider(Provider):
    """Markets picked from the built-in list (``provider: auto``), fetched from
    whichever data source is chosen in Settings."""

    def __init__(self, data_cfg: dict | None, cache_dir: str | Path | None = None):
        self.cfg = data_cfg or {}
        self.cache_dir = cache_dir

    def resolve(self, key: str) -> tuple[Provider, str]:
        from .recipes import MARKETS_BY_KEY

        market = MARKETS_BY_KEY.get(key)
        if not market:
            raise DataError(f"Unknown market {key!r}")
        source = self.cfg.get("source", "dukascopy")
        if source == "dukascopy":
            return DukascopyProvider(self.cache_dir), market["dukascopy"]
        if source == "oanda":
            if not market.get("oanda"):
                raise DataError(f"{market['label']} isn't available from OANDA here; switch Price data to Yahoo")
            return OandaProvider(self.cfg.get("oanda_token", ""), self.cfg.get("oanda_environment", "practice")), market["oanda"]
        return YahooProvider(), market["symbol"]

    stale = False

    def fetch_bars(self, symbol, timeframe, lookback_days, alignment=DEFAULT_ALIGNMENT):
        provider, real = self.resolve(symbol)
        out = provider.fetch_bars(real, timeframe, lookback_days, alignment)
        self.stale = bool(getattr(provider, "stale", False))
        return out


def get_provider(name: str, base_dir: str | Path = ".", tz: str = "UTC",
                 allowed_root: str | Path | None = None, data_cfg: dict | None = None,
                 cache_dir: str | Path | None = None) -> Provider:
    name = (name or "yahoo").lower()
    data_cfg = data_cfg or {}
    if cache_dir is None and allowed_root:
        cache_dir = Path(allowed_root) / "cache"
    if name == "auto":
        return MarketProvider(data_cfg, cache_dir)
    if name == "dukascopy":
        return DukascopyProvider(cache_dir)
    if name == "yahoo":
        return YahooProvider()
    if name == "oanda":
        return OandaProvider(data_cfg.get("oanda_token", ""), data_cfg.get("oanda_environment", "practice"))
    if name == "csv":
        return CSVProvider(base_dir=base_dir, tz=tz, allowed_root=allowed_root)
    raise DataError(f"Unknown data provider {name!r}. Available: auto, dukascopy, yahoo, oanda, csv")


def provider_for(cfg, name: str, tz: str = "UTC") -> Provider:
    """The provider for a market in a workspace, with its own folders for caching and CSV files."""
    return get_provider(name, base_dir=cfg.csv_base_dir, tz=tz, allowed_root=cfg.csv_root,
                        data_cfg=cfg.data, cache_dir=cfg.cache_dir)


def data_notes(symbol: str, provider: str, data_cfg: dict | None = None) -> list[str]:
    """Plain-language caveats about where the prices came from."""
    data_cfg = data_cfg or {}
    source = data_cfg.get("source", "dukascopy") if provider == "auto" else provider
    real = symbol
    if provider == "auto":
        from .recipes import MARKETS_BY_KEY
        m = MARKETS_BY_KEY.get(symbol, {})
        real = m.get("symbol", symbol)
    notes = []
    if source == "yahoo":
        notes.append("Prices are from Yahoo Finance, which can differ from your broker's CFD prices.")
        if real.endswith("=F"):
            notes.append("Yahoo's futures prices jump at each quarterly contract roll; CFD prices don't. "
                         "For CFD-style prices, switch Price data to Dukascopy in Settings.")
    return notes
