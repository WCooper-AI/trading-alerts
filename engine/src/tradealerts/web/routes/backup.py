"""Backup and restore: everything in one zip, to move between computers, servers and the hosted service."""

from __future__ import annotations

import io
import re
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

import pandas as pd
import yaml
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ... import __version__
from ...config import PATH_KEYS, uses_env
from ...strategy import strategy_files
from ...workspace import Workspace
from ..common import err, workspace
from ..strategy_io import enforce_running_limit

router = APIRouter()

MAX_BACKUP = 200 * 1024 * 1024
MAX_UNPACKED = 600 * 1024 * 1024
STRATEGY_NAME = re.compile(r"^strategies/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml$")
README = "trading-alerts-backup.txt"


def make_backup(ws: Workspace) -> bytes:
    c = ws.config()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(README,
                   f"Trading Alerts {__version__} backup, made {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC.\n"
                   "Restore it from Settings, Backup. It contains your settings (including alert and API tokens), "
                   "strategies, forward tests, journal, alerts and notes. Keep it private.\n")
        if ws.config_path.exists():
            z.write(ws.config_path, "config.yaml")
        for p in strategy_files(c.strategies_dir).values():
            z.write(p, f"strategies/{p.name}")
        if c.state_db.exists():
            with tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp) / "state.db"
                src, dst = sqlite3.connect(c.state_db), sqlite3.connect(copy)
                try:
                    src.backup(dst)  # consistent copy even while the checker is running
                finally:
                    dst.close()
                    src.close()
                z.write(copy, "data/state.db")
        if ws.trusted:  # the hosted service's news calendar is shared, not the customer's
            ev = c.data_dir / "news_events.csv"
            if ev.exists():
                z.write(ev, "data/news_events.csv")
    return buf.getvalue()


@router.get("/api/backup")
def backup(ws: Workspace = Depends(workspace)):
    name = f"trading-alerts-backup-{pd.Timestamp.now():%Y-%m-%d}.zip"
    return Response(make_backup(ws), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _settings_from_backup(ws: Workspace, raw: dict) -> dict:
    """A backup's settings, made safe for this workspace."""
    if not isinstance(raw, dict):
        err(422, "config.yaml in the backup is damaged")
    if ws.trusted:
        return raw
    out = {k: v for k, v in raw.items() if k not in PATH_KEYS and k != "log_level"}
    # Values read from the server's environment (${...}) only made sense on the old server.
    out["notifiers"] = [n for n in (out.get("notifiers") or []) if isinstance(n, dict) and not uses_env(n)
                        and (ws.limits.console_notifier or str(n.get("type")) != "console")]
    data = out.get("data") if isinstance(out.get("data"), dict) else {}
    if uses_env(data) or data.get("source") not in ws.limits.sources:
        data = {"source": ws.limits.sources[0]}
    out["data"] = data
    poll = out.get("poll_minutes", 15)
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or poll < ws.limits.min_poll_minutes:
        out["poll_minutes"] = max(15, ws.limits.min_poll_minutes)
    return out


@router.post("/api/restore")
async def restore(request: Request, ws: Workspace = Depends(workspace)):
    """Replace everything with a backup. The current state is saved first."""
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BACKUP:
            err(413, "That backup is too big")
    try:
        z = zipfile.ZipFile(io.BytesIO(bytes(raw)))
        infos = z.infolist()
    except zipfile.BadZipFile:
        err(422, "That isn't a Trading Alerts backup (not a zip file)")
    names = [i.filename for i in infos]
    if README not in names:
        err(422, "That zip isn't a Trading Alerts backup")
    if sum(i.file_size for i in infos) > MAX_UNPACKED:
        err(413, "That backup unpacks to more than this server accepts")
    allowed = {README, "config.yaml", "data/state.db"} | ({"data/news_events.csv"} if ws.trusted else set())
    for n in names:
        if n.endswith("/"):
            continue
        if n in ("data/news_events.csv",):
            continue  # skipped below when not wanted
        if n not in allowed and not STRATEGY_NAME.match(n):
            err(422, f"Unexpected file in the backup: {n}")
    # check it all before changing anything
    strategies = {}
    for n in names:
        if STRATEGY_NAME.match(n):
            b = z.read(n)
            if len(b) > 200_000:
                err(422, f"{n} in the backup is too big to be a strategy")
            try:
                yaml.safe_load(b.decode("utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError):
                err(422, f"{n} in the backup is damaged")
            strategies[Path(n).name] = b
    limit = ws.limits.max_strategies
    if limit is not None and len(strategies) > limit:
        err(422, f"The backup has {len(strategies)} strategies; this plan allows {limit}.")
    settings = None
    if "config.yaml" in names:
        try:
            settings = _settings_from_backup(ws, yaml.safe_load(z.read("config.yaml").decode("utf-8")) or {})
        except (yaml.YAMLError, UnicodeDecodeError):
            err(422, "config.yaml in the backup is damaged")
    with tempfile.TemporaryDirectory() as tmp:
        db_copy = None
        if "data/state.db" in names:
            db_copy = Path(tmp) / "state.db"
            db_copy.write_bytes(z.read("data/state.db"))
            try:
                con = sqlite3.connect(f"file:{db_copy.as_posix()}?mode=ro", uri=True)
                ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                con.close()
            except sqlite3.DatabaseError:
                ok = False
            if not ok:
                err(422, "The database in the backup is damaged")
        # safety copy of what's there now
        c = ws.config()
        keep = c.data_dir / "backups"
        keep.mkdir(parents=True, exist_ok=True)
        for old in sorted(keep.glob("before-restore-*.zip"))[:-4]:  # keep the last five
            old.unlink(missing_ok=True)
        safety = keep / f"before-restore-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        safety.write_bytes(make_backup(ws))
        if settings is not None:
            ws.write_config(settings)
        c = ws.config()
        c.strategies_dir.mkdir(parents=True, exist_ok=True)
        for p in strategy_files(c.strategies_dir).values():
            p.unlink()
        for name, b in strategies.items():
            (c.strategies_dir / name).write_bytes(b)
        if db_copy is not None:
            with ws.store() as store:  # rows go into this version's own tables
                try:
                    store.replace_from(db_copy)
                except ValueError as exc:
                    err(422, str(exc))
        if ws.trusted and "data/news_events.csv" in names:
            c.data_dir.mkdir(parents=True, exist_ok=True)
            (c.data_dir / "news_events.csv").write_bytes(z.read("data/news_events.csv"))
    if not ws.trusted:
        # Strategies this service can't run (e.g. a CSV path or price source from the self-hosted
        # app) come in as drafts, so they're never checked live until fixed.
        from fastapi import HTTPException

        from ...strategy import read_raw
        from ..strategy_io import validate, write
        for p in strategy_files(ws.config().strategies_dir).values():
            data = read_raw(p)
            try:
                validate(ws, data)
            except HTTPException:
                if data.get("template") is not True:
                    data["template"] = True
                    write(p, data)
    stopped = enforce_running_limit(ws)
    return {"restored": True, "strategies": len(strategies), "stopped_over_limit": stopped,
            "safety_copy": str(safety) if ws.trusted else safety.name}
