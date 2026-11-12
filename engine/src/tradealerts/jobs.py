"""Running the testing tools (backtests, luck checks...).

The self-hosted edition runs them right away in the request. The hosted edition runs
them in a pool of worker processes, at most ``per_owner`` at a time for each customer, so
one customer's long test queues behind their own, not everyone's.

Long jobs (the optimiser) use ``start`` instead of ``run``: the job runs in the
background, reports its progress through a ``Progress`` object, and can be cancelled.
The function given to ``start`` receives the ``Progress`` as its first argument.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Callable

log = logging.getLogger(__name__)

KEEP_SECONDS = 900  # how long a finished job's result stays available


class Busy(RuntimeError):
    """Too many tests at once; the message is shown to the user."""


class Cancelled(RuntimeError):
    """Raised inside a job when the user cancelled it."""


class Progress:
    """Step progress and the cancel flag, shared between the job and whoever watches it.

    ``state`` is a dict (or a manager proxy of one, across processes) and ``flag`` an
    ``Event`` (or its proxy). Both are picklable so they can travel into a pool worker.
    """

    def __init__(self, state, flag):
        self.state = state
        self.flag = flag

    def update(self, done: int, total: int, note: str | None = None) -> None:
        self.state["done"] = int(done)
        self.state["total"] = int(total)
        if note is not None:
            self.state["note"] = note

    @property
    def cancelled(self) -> bool:
        return self.flag.is_set()

    def check(self) -> None:
        """Raise ``Cancelled`` if the user asked to stop."""
        if self.cancelled:
            raise Cancelled("Stopped")


class _Job:
    def __init__(self, owner: str, progress: Progress):
        self.id = secrets.token_urlsafe(9)
        self.owner = owner
        self.progress = progress
        self.status = "running"      # running | done | cancelled | error
        self.result: Any = None
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None

    def finish(self, result=None, error: BaseException | None = None) -> None:
        if error is None:
            self.status, self.result = "done", result
        elif isinstance(error, Cancelled) or self.progress.cancelled:
            self.status = "cancelled"
        else:
            self.status, self.error = "error", str(error) or error.__class__.__name__
        self.finished = time.time()

    def public(self) -> dict:
        st = self.progress.state
        out = {"id": self.id, "status": self.status, "done": int(st.get("done", 0)), "total": int(st.get("total", 0)),
               "note": st.get("note"), "seconds": round((self.finished or time.time()) - self.started, 1)}
        if self.status == "done":
            out["result"] = self.result
        if self.status == "error":
            out["error"] = self.error
        return out


class _JobTable:
    """Bookkeeping shared by both runners: look up, report and cancel background jobs."""

    def __init__(self):
        self._jobs: dict[str, _Job] = {}
        self._jobs_lock = threading.Lock()

    def _add(self, job: _Job) -> None:
        with self._jobs_lock:
            cutoff = time.time() - KEEP_SECONDS
            for k in [k for k, j in self._jobs.items() if j.finished and j.finished < cutoff]:
                del self._jobs[k]
            self._jobs[job.id] = job

    def _get(self, owner: str | None, job_id: str) -> _Job | None:
        with self._jobs_lock:
            j = self._jobs.get(job_id)
        return j if j and j.owner == (owner or "-") else None

    def status(self, owner: str | None, job_id: str) -> dict | None:
        j = self._get(owner, job_id)
        return j.public() if j else None

    def cancel(self, owner: str | None, job_id: str) -> bool:
        j = self._get(owner, job_id)
        if not j:
            return False
        j.progress.flag.set()
        return True

    def running(self, owner: str | None) -> int:
        with self._jobs_lock:
            return sum(1 for j in self._jobs.values() if j.owner == (owner or "-") and j.status == "running")


def _call(fn: Callable, progress: Progress, *args):
    return fn(progress, *args)


class InlineJobs(_JobTable):
    """Self-hosted: ``run`` works in the request, ``start`` in a thread (the job itself may
    use a process pool for the heavy lifting)."""

    def run(self, owner: str | None, fn: Callable, *args) -> Any:
        return fn(*args)

    def start(self, owner: str | None, fn: Callable, *args) -> str:
        if self.running(owner):
            raise Busy("You already have an optimiser run going. Stop it or wait for it to finish.")
        job = _Job(owner or "-", Progress({"done": 0, "total": 0}, threading.Event()))
        self._add(job)

        def work():
            try:
                job.finish(_call(fn, job.progress, *args))
            except BaseException as exc:  # noqa: BLE001 - reported to the user through status()
                if not isinstance(exc, Cancelled):
                    log.warning("job %s failed: %s", job.id, exc)
                job.finish(error=exc)

        threading.Thread(target=work, name=f"job-{job.id}", daemon=True).start()
        return job.id

    def close(self) -> None:
        with self._jobs_lock:
            for j in self._jobs.values():
                j.progress.flag.set()


class PoolJobs(_JobTable):
    def __init__(self, workers: int, per_owner: int = 1, wait_seconds: float = 2.0, max_queue: int | None = None,
                 timeout: float = 600):
        import multiprocessing

        from . import compute

        super().__init__()
        self.workers = max(1, workers)
        # "spawn": a clean process, not a fork of a server with threads running
        self._mp = multiprocessing.get_context("spawn")
        self.pool = ProcessPoolExecutor(self.workers, mp_context=self._mp)
        self.per_owner = per_owner
        self.wait_seconds = wait_seconds
        self.max_queue = max_queue if max_queue is not None else self.workers * 4
        self.timeout = timeout
        self._lock = threading.Lock()
        self._owners: dict[str, threading.BoundedSemaphore] = {}
        self._queued = 0
        self._manager = None  # started on the first background job; shares progress across processes
        for _ in range(self.workers):
            self.pool.submit(compute.warm_up)

    def _slot(self, owner: str | None, wait: float) -> threading.BoundedSemaphore:
        with self._lock:
            sem = self._owners.setdefault(owner or "-", threading.BoundedSemaphore(self.per_owner))
            if self._queued >= self.max_queue:
                raise Busy("The server is busy running other tests. Try again in a minute.")
        if not sem.acquire(timeout=wait):
            raise Busy("You already have a test running. Wait for it to finish, then try again.")
        with self._lock:
            self._queued += 1
        return sem

    def _free(self, sem: threading.BoundedSemaphore) -> None:
        with self._lock:
            self._queued -= 1
        sem.release()

    def run(self, owner: str | None, fn: Callable, *args) -> Any:
        sem = self._slot(owner, self.wait_seconds)
        try:
            return self.pool.submit(fn, *args).result(timeout=self.timeout)
        finally:
            self._free(sem)

    def start(self, owner: str | None, fn: Callable, *args) -> str:
        """Queue ``fn(progress, *args)`` on one worker; the job runs its steps one after another."""
        with self._lock:
            if self._manager is None:
                self._manager = self._mp.Manager()
            manager = self._manager
        sem = self._slot(owner, 0.0)
        job = _Job(owner or "-", Progress(manager.dict({"done": 0, "total": 0}), manager.Event()))
        self._add(job)
        try:
            fut = self.pool.submit(_call, fn, job.progress, *args)
        except BaseException:
            self._free(sem)
            raise

        def done(f):
            try:
                job.finish(f.result())
            except BaseException as exc:  # noqa: BLE001
                job.finish(error=exc)
            finally:
                self._free(sem)

        fut.add_done_callback(done)
        watchdog = threading.Timer(self.timeout, job.progress.flag.set)  # a runaway job is asked to stop
        watchdog.daemon = True
        watchdog.start()
        return job.id

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self._manager is not None:
            self._manager.shutdown()
