"""Running the testing tools (backtests, luck checks...).

The self-hosted edition runs them right away in the request. The hosted edition runs
them in a pool of worker processes, at most ``per_owner`` at a time for each customer, so
one customer's long test queues behind their own, not everyone's.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Callable

log = logging.getLogger(__name__)


class Busy(RuntimeError):
    """Too many tests at once; the message is shown to the user."""


class InlineJobs:
    def run(self, owner: str | None, fn: Callable, *args) -> Any:
        return fn(*args)

    def close(self) -> None:
        pass


class PoolJobs:
    def __init__(self, workers: int, per_owner: int = 1, wait_seconds: float = 2.0, max_queue: int | None = None,
                 timeout: float = 600):
        import multiprocessing

        from . import compute

        self.workers = max(1, workers)
        # "spawn": a clean process, not a fork of a server with threads running
        self.pool = ProcessPoolExecutor(self.workers, mp_context=multiprocessing.get_context("spawn"))
        self.per_owner = per_owner
        self.wait_seconds = wait_seconds
        self.max_queue = max_queue if max_queue is not None else self.workers * 4
        self.timeout = timeout
        self._lock = threading.Lock()
        self._owners: dict[str, threading.BoundedSemaphore] = {}
        self._queued = 0
        for _ in range(self.workers):
            self.pool.submit(compute.warm_up)

    def run(self, owner: str | None, fn: Callable, *args) -> Any:
        with self._lock:
            sem = self._owners.setdefault(owner or "-", threading.BoundedSemaphore(self.per_owner))
            if self._queued >= self.max_queue:
                raise Busy("The server is busy running other tests. Try again in a minute.")
        if not sem.acquire(timeout=self.wait_seconds):
            raise Busy("You already have a test running. Wait for it to finish, then try again.")
        with self._lock:
            self._queued += 1
        try:
            return self.pool.submit(fn, *args).result(timeout=self.timeout)
        finally:
            with self._lock:
                self._queued -= 1
            sem.release()

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
