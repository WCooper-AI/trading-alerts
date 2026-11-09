"""Strategy history: every saved version of a strategy file, and what a forward test was run on.

A version is the strategy file's exact text. Its hash is the same ``source_hash`` the
strategy carries when loaded, so a forward test's snapshot and the History tab point at the
same file. A backtest's stamp uses ``rules_fingerprint`` instead: the trading rules alone, so
renaming a strategy or how the editor lays the file out doesn't change it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

# The parts of a strategy that change what it trades. Name, description and alerts on/off don't.
RULE_KEYS = ("market", "direction", "entry", "exit", "risk", "costs")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12] if text else ""


def _tidy(v):
    """Drop blanks, so an empty section and a missing one count as the same."""
    if isinstance(v, dict):
        out = {k: _tidy(x) for k, x in v.items()}
        return {k: x for k, x in out.items() if x not in (None, "", {}, [])}
    if isinstance(v, list):
        return [_tidy(x) for x in v]
    return v


def rules_fingerprint(data: dict | None) -> str:
    """A short code for the trading rules alone: the same rules give the same code, whatever the
    strategy is called or however the file is laid out."""
    rules = _tidy({k: (data or {}).get(k) for k in RULE_KEYS})
    return hashlib.sha256(json.dumps(rules, sort_keys=True, default=str).encode()).hexdigest()[:12]


def version_with_rules(store, sid: str, data: dict) -> dict | None:
    """The newest saved version of ``sid`` with exactly these trading rules."""
    want = rules_fingerprint(data)
    for v in store.versions(sid):
        full = store.version(sid, v["version_no"])
        if rules_fingerprint(parse(full["yaml"])) == want:
            return full
    return None


def rules_changed(old: dict | None, new: dict | None) -> bool:
    return rules_fingerprint(old) != rules_fingerprint(new)


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
