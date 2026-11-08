"""Dukascopy provider, tested against a stand-in for dukascopy-python's classic API."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tradealerts import data as data_mod
from tradealerts.recipes import MARKETS


class FakeDuka:
    """Mimics dukascopy_python(.compat).fetch: one offer side, UTC index, OHLCV columns."""

    INTERVAL_MIN_5, INTERVAL_MIN_15, INTERVAL_MIN_30, INTERVAL_HOUR_1 = "5MIN", "15MIN", "30MIN", "1HOUR"
    OFFER_SIDE_BID, OFFER_SIDE_ASK = "B", "A"
    STEP = {"5MIN": "5min", "15MIN": "15min", "30MIN": "30min", "1HOUR": "1h"}

    def __init__(self, broken=None, half_spread=0.25, offline=False):
        self.offline = offline
        self.calls = []
        self.broken = broken  # (start, end) range where the feed errors, like a holiday gap
        self.half = half_spread

    def fetch(self, instrument, interval, side, start, end, max_retries=7):
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        self.calls.append((instrument, interval, side, start, end))
        if self.offline:
            raise ConnectionError("Failed to establish a new connection")
        if self.broken and start < self.broken[1] and end > self.broken[0]:
            raise IndexError("list index out of range")  # what the library does on empty ranges
        idx = pd.date_range(start.ceil(self.STEP[interval]), end, freq=self.STEP[interval], inclusive="left")
        idx = idx[idx.dayofweek < 5]
        t = (idx - pd.Timestamp(0, tz="UTC")).total_seconds().to_numpy() / 3600.0
        mid = 5000 + 50 * np.sin(t / 50.0)
        shift = -self.half if side == "B" else self.half
        df = pd.DataFrame({"open": mid + shift, "high": mid + 3 + shift, "low": mid - 3 + shift,
                           "close": mid + 1 + shift, "volume": 1.0}, index=idx)
        df.index.name = "timestamp"
        return df


def provider(tmp_path=None, **kw):
    fake = FakeDuka(**kw)
    ins = SimpleNamespace(INSTRUMENT_IDX_AMERICA_E_SANDP_500="E_SandP-500")
    p = data_mod.DukascopyProvider(cache_dir=tmp_path, lib=(fake, ins))
    return p, fake


def test_mid_price_and_real_spread_from_bid_and_ask():
    p, fake = provider()
    bars, fine = p.fetch_bars("INSTRUMENT_IDX_AMERICA_E_SANDP_500", "1h", 20)
    assert {c[0] for c in fake.calls} == {"E_SandP-500"}           # constant name resolved
    assert {c[2] for c in fake.calls} == {"B", "A"}                 # both sides fetched
    assert bars["spread"].median() == pytest.approx(0.5)
    t = bars.index[10]
    hrs = (t - pd.Timestamp(0, tz="UTC")).total_seconds() / 3600.0
    assert bars.loc[t, "open"] == pytest.approx(5000 + 50 * np.sin(hrs / 50.0))  # mid
    assert fine is not None and data_mod.infer_bar_length(fine) == pd.Timedelta("15min")


def test_four_hour_candles_are_built_from_hourly_with_fine_data():
    p, _ = provider()
    bars, fine = p.fetch_bars("E_SandP-500", "4h", 30)
    assert data_mod.infer_bar_length(bars) == pd.Timedelta("4h")
    assert data_mod.infer_bar_length(fine) == pd.Timedelta("1h")
    assert {t.hour for t in bars.index} <= {1, 2, 5, 6, 9, 10, 13, 14, 17, 18, 21, 22}  # 17:00 New York grid
    assert "spread" in bars.columns


def test_a_failing_gap_is_skipped_without_losing_the_rest():
    now = pd.Timestamp.now(tz="UTC")
    gap = (now - pd.Timedelta(days=40), now - pd.Timedelta(days=39))
    p, fake = provider(broken=gap)
    bars, _ = p.fetch_bars("E_SandP-500", "1h", 200)
    assert len(bars) > 1500
    in_gap = bars[(bars.index >= gap[0]) & (bars.index < gap[1])]
    assert len(in_gap) == 0 and len(fake.calls) > 4              # range was split around the gap


def test_downloads_are_cached_and_only_new_candles_fetched(tmp_path):
    p, fake = provider(tmp_path)
    first, _ = p.fetch_bars("E_SandP-500", "1h", 300)
    first_calls = len(fake.calls)
    assert len(list(tmp_path.glob("dukascopy_E_SandP_500_*.csv.gz"))) >= 2  # bid and ask
    fake.calls.clear()
    second, _ = p.fetch_bars("E_SandP-500", "1h", 300)
    assert len(second) == pytest.approx(len(first), abs=2)
    # only the recent tail is requested again, once per side
    assert len(fake.calls) <= 2 < first_calls
    assert all(c[3] >= first.index[-1] - pd.Timedelta(hours=3) for c in fake.calls)
    # asking for more history fetches just the older part
    fake.calls.clear()
    longer, _ = p.fetch_bars("E_SandP-500", "1h", 400)
    assert longer.index[0] < first.index[0]
    assert min(c[3] for c in fake.calls) < first.index[0]


def test_no_data_at_all_is_explained():
    now = pd.Timestamp.now(tz="UTC")
    p, _ = provider(broken=(now - pd.Timedelta(days=400), now + pd.Timedelta(days=1)))
    with pytest.raises(data_mod.DataError, match="no prices|kept failing"):
        p.fetch_bars("E_SandP-500", "1h", 10)


def test_every_built_in_market_has_a_dukascopy_name():
    assert all(m["dukascopy"].startswith("INSTRUMENT_") for m in MARKETS)
    prov, sym = data_mod.MarketProvider({}).resolve("us500")  # default source
    assert isinstance(prov, data_mod.DukascopyProvider) and sym == "INSTRUMENT_IDX_AMERICA_E_SANDP_500"


def test_real_library_has_the_instrument_names():
    pytest.importorskip("dukascopy_python")
    p = data_mod.DukascopyProvider()
    duka, ins = p.lib()
    assert hasattr(duka, "fetch") and hasattr(duka, "INTERVAL_HOUR_1") and duka.OFFER_SIDE_ASK
    for m in MARKETS:
        assert hasattr(ins, m["dukascopy"]), m["dukascopy"]


def test_offline_fails_fast_with_a_clear_message():
    p, fake = provider(offline=True)
    with pytest.raises(data_mod.DataError, match="Couldn't reach Dukascopy"):
        p.fetch_bars("E_SandP-500", "1h", 1000)
    assert len(fake.calls) <= 2  # no splitting on network errors


def test_a_feed_that_keeps_failing_gives_up():
    now = pd.Timestamp.now(tz="UTC")
    p, fake = provider(broken=(now - pd.Timedelta(days=3000), now + pd.Timedelta(days=1)))
    with pytest.raises(data_mod.DataError):
        p.fetch_bars("E_SandP-500", "1h", 1000)
    assert len(fake.calls) <= 2 * (data_mod.DukascopyProvider.MAX_FAILURES + 2)
