"""Strategy history: every saved version of a strategy file, and what a forward test was run on.

A version is the strategy file's exact text. Its hash is the same ``source_hash`` the
strategy carries when loaded, so a backtest's stamp, a forward test's snapshot and the
History tab all point at the same thing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

# The parts of a strategy that change what it trades. Name, description and alerts on/off don't.
RULE_KEYS = ("market", "direction", "entry", "exit", "risk", "costs")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12] if text else ""


def rules_changed(old: dict | None, new: dict | None) -> bool:
    old, new = old or {}, new or {}
    return any(old.get(k) != new.get(k) for k in RULE_KEYS)


def parse(text: str) -> dict:
    try:
        data = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def record(store, sid: str, path: Path, note: str, keep: int | None = None) -> dict | None:
    """Save the file's current text as a new version, unless it's the same as the latest."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    return store.add_version(sid, text_hash(text), text, note, keep=keep)


def sync(store, sid: str, path: Path, keep: int | None = None) -> dict | None:
    """Notice a file changed outside the app (edited by hand) and keep that as a version too."""
    latest = store.latest_version(sid)
    note = "Edited outside the app" if latest else "Earliest version on record"
    return record(store, sid, path, note, keep)


def sync_run(store, run: dict, strategy_text: str, now: str) -> None:
    """Keep a running forward test's snapshot in step with its strategy file.

    Saves in the app update the snapshot themselves; this catches changes made any other
    way. When the trading rules changed, the run's history says so.
    """
    h = text_hash(strategy_text)
    if not h or run.get("source_hash") == h:
        return
    if run.get("source_hash") and rules_changed(parse(run.get("strategy_yaml") or ""), parse(strategy_text)):
        store.add_event(run["strategy_id"], now, "strategy_saved",
                        "Rules changed outside the app\nThe new rules apply from the next candle.", False, run["id"])
    store.set_run_snapshot(run["id"], h, strategy_text)
