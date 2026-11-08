"""Sign-in for the self-hosted edition: one password, signed session cookies."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from .common import err

COOKIE = "ta_session"
SESSION_DAYS = 7


class LoginThrottle:
    """Slows down repeated wrong passwords from one address (or for one account)."""

    MAX_TRACKED = 10_000

    def __init__(self, free_tries: int = 5):
        self.free = free_tries
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def locked_for(self, key: str) -> float:
        count, until = self._failures.get(key, (0, 0.0))
        return max(0.0, until - time.time())

    def failed(self, key: str) -> None:
        with self._lock:
            if len(self._failures) > self.MAX_TRACKED:  # don't grow without limit under attack
                now = time.time()
                self._failures = {k: v for k, v in self._failures.items() if v[1] > now}
            count, _ = self._failures.get(key, (0, 0.0))
            count += 1
            lock = 0.0 if count < self.free else min(900.0, 30.0 * 2 ** (count - self.free))
            self._failures[key] = (count, time.time() + lock)

    def ok(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class Auth:
    def __init__(self, password: str | None, secret_file: Path):
        self.password = password or None
        self.throttle = LoginThrottle()
        if self.password:
            secret_file.parent.mkdir(parents=True, exist_ok=True)
            if not secret_file.exists():
                secret_file.write_bytes(secrets.token_bytes(32))
                try:
                    os.chmod(secret_file, 0o600)
                except OSError:
                    pass
            # Changing the password signs everyone out.
            self.key = secret_file.read_bytes() + hashlib.sha256(self.password.encode()).digest()

    @property
    def enabled(self) -> bool:
        return self.password is not None

    def token(self) -> str:
        exp = str(int(time.time()) + SESSION_DAYS * 86400)
        sig = hmac.new(self.key, exp.encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def valid(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not token or "." not in token:
            return False
        exp, sig = token.split(".", 1)
        good = hmac.new(self.key, exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()

    # Older names, kept for anything that used them.
    def locked_for(self, ip: str) -> float:
        return self.throttle.locked_for(ip)

    def check_password(self, ip: str, attempt: str) -> bool:
        ok = hmac.compare_digest(
            hashlib.sha256(attempt.encode()).digest(), hashlib.sha256(self.password.encode()).digest()
        )
        (self.throttle.ok if ok else self.throttle.failed)(ip)
        return ok


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def router(auth: Auth) -> APIRouter:
    r = APIRouter()

    @r.post("/api/login")
    def login(request: Request, body: dict = Body(...)):
        if not auth.enabled:
            return {"signed_in": True}
        ip = request.client.host if request.client else "?"
        wait = auth.locked_for(ip)
        if wait:
            err(429, f"Too many attempts. Try again in {math.ceil(wait)} seconds.")
        if not auth.check_password(ip, str(body.get("password", ""))):
            err(401, "That password isn't right")
        resp = JSONResponse({"signed_in": True})
        resp.set_cookie(COOKIE, auth.token(), max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="strict", secure=is_https(request), path="/")
        return resp

    @r.post("/api/logout")
    def logout():
        resp = JSONResponse({"signed_in": False})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    return r
