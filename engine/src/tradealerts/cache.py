"""A small in-memory cache for downloaded prices, shared by the web interface and the live checker."""

from __future__ import annotations

import threading
import time
from typing import Any


class DataCache:
    """Keeps downloaded price data for a few minutes so repeated requests are quick.

    Two requests for the same data at once share one download instead of both
    hitting the (slow) feed, and ``get_or_last`` falls back to the last good copy
    when a download fails, so a live chart keeps its candles through a blip."""

    KEEP_LAST = 6 * 3600  # seconds an expired copy may stand in after a failed download
    MAX_ITEMS = 200

    def __init__(self, ttl: float = 600):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._items: dict[tuple, tuple[float, Any]] = {}
        self._loading: dict[tuple, threading.Lock] = {}

    def _fresh(self, key):
        hit = self._items.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit
        return None

    def get(self, key: tuple, loader):
        return self.get_or_last(key, loader, keep_last=False)[0]

    def get_or_last(self, key: tuple, loader, keep_last: bool = True):
        """Returns (value, age in seconds if it's an old copy standing in, else None)."""
        with self._lock:
            hit = self._fresh(key)
            if hit:
                return hit[1], None
            key_lock = self._loading.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:  # someone else may have just finished the same download
                hit = self._fresh(key)
                if hit:
                    return hit[1], None
            try:
                value = loader()
            except Exception:
                with self._lock:
                    old = self._items.get(key)
                if keep_last and old and time.monotonic() - old[0] < self.KEEP_LAST:
                    return old[1], time.monotonic() - old[0]
                raise
            with self._lock:
                self._items[key] = (time.monotonic(), value)
                if len(self._items) > self.MAX_ITEMS:  # drop the oldest so memory stays bounded
                    for k in sorted(self._items, key=lambda k: self._items[k][0])[: self.MAX_ITEMS // 4]:
                        self._items.pop(k, None)
                        self._loading.pop(k, None)
            return value, None
