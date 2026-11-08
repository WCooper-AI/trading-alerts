"""What the routes need from the edition they run in: whose workspace a request is for,
the live checker, the job runner and the shared price caches."""

from __future__ import annotations

from fastapi import Request

from ..cache import DataCache
from ..jobs import InlineJobs
from ..workspace import Workspace


class AppContext:
    edition = "selfhost"

    def __init__(self, runner=None, jobs=None, desktop: bool = False):
        self.runner = runner
        self.jobs = jobs or InlineJobs()
        self.desktop = desktop
        self.cache = DataCache()             # backtests: the same history several times in a row
        self.live_cache = DataCache(ttl=20)  # live charts refresh every 30 seconds
        self.stats_cache = DataCache(ttl=900)
        self.shutdown = None                 # set by serve() for the desktop app
        self.extra_scripts: list[str] = []
        self.news_busy = False

    # Overridden by each edition.
    def workspace(self, request: Request) -> Workspace:  # pragma: no cover - interface
        raise NotImplementedError

    def session(self, request: Request) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    # ------------------------------------------------------------------ live checker

    def runner_status(self, ws: Workspace) -> dict:
        return self.runner.status(ws.key) if self.runner else {"enabled": False}

    def wake(self, ws: Workspace) -> None:
        if self.runner:
            self.runner.wake(ws.key)

    def run_now(self, ws: Workspace) -> bool:
        return self.runner.run_now(key=ws.key)

    def no_runner_reason(self) -> str:
        return "Live checks are off because the server was started with --no-runner"


class SelfHostContext(AppContext):
    """One workspace, optionally behind one password."""

    def __init__(self, ws: Workspace, auth, runner=None, desktop: bool = False):
        super().__init__(runner=runner, desktop=desktop)
        self.ws = ws
        self.auth = auth

    def workspace(self, request: Request) -> Workspace:
        return self.ws

    def signed_in(self, request: Request) -> bool:
        from .auth import COOKIE
        return self.auth.valid(request.cookies.get(COOKIE))

    def session(self, request: Request) -> dict:
        from .. import __version__
        return {"edition": self.edition, "auth_required": self.auth.enabled, "signed_in": self.signed_in(request),
                "version": __version__, "desktop": self.desktop, "limits": self.ws.limits.public()}
