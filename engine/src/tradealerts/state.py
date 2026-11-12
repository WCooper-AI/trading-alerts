"""SQLite storage for live runs: forward tests, simulator state, paper trades, equity and events."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_state (
    strategy_id TEXT PRIMARY KEY,
    cursor      TEXT,
    state       TEXT NOT NULL,
    source_hash TEXT,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    name TEXT,
    market TEXT,
    timeframe TEXT,
    currency TEXT,
    start_balance REAL NOT NULL,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    source_hash TEXT,
    strategy_yaml TEXT
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    run_id INTEGER,
    direction TEXT, signal_time TEXT, entry_time TEXT, entry_price REAL,
    exit_time TEXT, exit_price REAL, size REAL, stop REAL, target REAL,
    exit_reason TEXT, pnl REAL, r_multiple REAL, bars_held INTEGER, equity_after REAL,
    funding REAL, spread_cost REAL,
    entry_reason TEXT, exit_values TEXT, mae_r REAL, mfe_r REAL
);
CREATE TABLE IF NOT EXISTS run_equity (
    run_id INTEGER NOT NULL,
    bar_time TEXT NOT NULL,
    equity REAL NOT NULL,
    PRIMARY KEY (run_id, bar_time)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    run_id INTEGER,
    bar_time TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    label TEXT,
    direction TEXT NOT NULL,
    price REAL NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    checked_to TEXT,
    triggered_at TEXT,
    triggered_price REAL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_time TEXT,
    exit_price REAL,
    size REAL NOT NULL,
    point_value REAL NOT NULL DEFAULT 1,
    fees REAL NOT NULL DEFAULT 0,
    currency TEXT,
    strategy_id TEXT,
    paper_trade_id INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_notes (
    key TEXT PRIMARY KEY,
    time TEXT NOT NULL,
    currency TEXT,
    title TEXT,
    note TEXT,
    alert_minutes INTEGER,
    alerted_at TEXT
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    hash TEXT NOT NULL,
    yaml TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (strategy_id, version_no)
);
CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_strategy ON runs (strategy_id, stopped_at);
CREATE INDEX IF NOT EXISTS idx_trades_run ON paper_trades (run_id);
"""

TRADE_COLS = [
    "direction", "signal_time", "entry_time", "entry_price", "exit_time", "exit_price", "size",
    "stop", "target", "exit_reason", "pnl", "r_multiple", "bars_held", "equity_after",
    "funding", "spread_cost", "entry_reason", "exit_values", "mae_r", "mfe_r",
]
JSON_COLS = ("entry_reason", "exit_values")


def _now() -> str:
    # milliseconds, so "what's new since I last looked" can't miss an event from the same second
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self._migrate()
        self.db.executescript(SCHEMA)

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        for table, col, kind in (("paper_trades", "run_id", "INTEGER"), ("events", "run_id", "INTEGER"),
                                 ("paper_trades", "funding", "REAL"), ("paper_trades", "spread_cost", "REAL"),
                                 ("runs", "source_hash", "TEXT"), ("runs", "strategy_yaml", "TEXT"),
                                 ("paper_trades", "mae_r", "REAL"), ("paper_trades", "mfe_r", "REAL"),
                                 ("paper_trades", "entry_reason", "TEXT"), ("paper_trades", "exit_values", "TEXT")):
            cols = [r[1] for r in self.db.execute(f"PRAGMA table_info({table})")]
            if cols and col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        self.db.commit()

    def close(self):
        self.db.close()

    # ------------------------------------------------------------------ simulator state

    def load(self, strategy_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT cursor, state, source_hash FROM strategy_state WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()
        if not row:
            return None
        return {"cursor": row["cursor"], "state": json.loads(row["state"]), "source_hash": row["source_hash"]}

    def save(self, strategy_id: str, cursor: str, state: dict, source_hash: str = "") -> None:
        self.db.execute(
            "INSERT INTO strategy_state (strategy_id, cursor, state, source_hash, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(strategy_id) DO UPDATE SET "
            "cursor = excluded.cursor, state = excluded.state, source_hash = excluded.source_hash, "
            "updated_at = excluded.updated_at",
            (strategy_id, cursor, json.dumps(state), source_hash, _now()),
        )
        self.db.commit()

    def reset(self, strategy_id: str) -> None:
        self.db.execute("DELETE FROM strategy_state WHERE strategy_id = ?", (strategy_id,))
        self.db.commit()

    def all_states(self) -> dict[str, dict]:
        return {
            r["strategy_id"]: {"cursor": r["cursor"], "state": json.loads(r["state"]), "updated_at": r["updated_at"]}
            for r in self.db.execute("SELECT * FROM strategy_state")
        }

    # ------------------------------------------------------------------ forward-test runs

    def active_run(self, strategy_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM runs WHERE strategy_id = ? AND stopped_at IS NULL ORDER BY id DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
        return dict(row) if row else None

    def active_runs(self) -> dict[str, dict]:
        return {r["strategy_id"]: dict(r) for r in self.db.execute(
            "SELECT * FROM runs WHERE stopped_at IS NULL ORDER BY id")}

    def start_run(self, strategy_id: str, *, name: str, market: str, timeframe: str, currency: str,
                  start_balance: float, started_at: str | None = None, keep_state: bool = False,
                  source_hash: str = "", strategy_yaml: str = "") -> dict:
        """Start a new forward test. Any running one for the strategy is finished first.

        ``source_hash`` and ``strategy_yaml`` are the strategy file it runs on, kept with the run
        so it can be re-tested later even if the strategy is edited or deleted."""
        self.stop_run(strategy_id, stopped_at=started_at)
        if not keep_state:
            self.db.execute("DELETE FROM strategy_state WHERE strategy_id = ?", (strategy_id,))
        cur = self.db.execute(
            "INSERT INTO runs (strategy_id, name, market, timeframe, currency, start_balance, started_at, "
            "source_hash, strategy_yaml) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (strategy_id, name, market, timeframe, currency, start_balance, started_at or _now(),
             source_hash or None, strategy_yaml or None),
        )
        self.db.commit()
        return self.run(cur.lastrowid)

    def stop_run(self, strategy_id: str, stopped_at: str | None = None) -> dict | None:
        run = self.active_run(strategy_id)
        if not run:
            return None
        self.db.execute("UPDATE runs SET stopped_at = ? WHERE id = ?", (stopped_at or _now(), run["id"]))
        self.db.execute("DELETE FROM strategy_state WHERE strategy_id = ?", (strategy_id,))
        self.db.commit()
        return self.run(run["id"])

    def set_run_snapshot(self, run_id: int, source_hash: str, strategy_yaml: str) -> None:
        self.db.execute("UPDATE runs SET source_hash = ?, strategy_yaml = ? WHERE id = ?",
                        (source_hash, strategy_yaml, run_id))
        self.db.commit()

    def run(self, run_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def runs(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY id DESC")]

    def delete_run(self, run_id: int) -> None:
        for table in ("run_equity", "paper_trades", "events"):
            self.db.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        self.db.execute("DELETE FROM runs WHERE id = ? AND stopped_at IS NOT NULL", (run_id,))
        self.db.commit()

    def add_equity(self, run_id: int, bar_time: str, equity: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO run_equity (run_id, bar_time, equity) VALUES (?, ?, ?)",
            (run_id, bar_time, equity),
        )

    def equity(self, run_id: int) -> list[tuple[str, float]]:
        return [(r["bar_time"], r["equity"]) for r in self.db.execute(
            "SELECT bar_time, equity FROM run_equity WHERE run_id = ? ORDER BY bar_time", (run_id,))]

    def commit(self) -> None:
        self.db.commit()

    # ------------------------------------------------------------------ trades and events

    def add_trade(self, strategy_id: str, trade: dict, run_id: int | None = None) -> None:
        cols = ", ".join(["strategy_id", "run_id"] + TRADE_COLS)
        marks = ", ".join("?" * (len(TRADE_COLS) + 2))
        self.db.execute(
            f"INSERT INTO paper_trades ({cols}) VALUES ({marks})",
            [strategy_id, run_id] + [json.dumps(trade.get(c) or {}) if c in JSON_COLS else trade.get(c) for c in TRADE_COLS],
        )
        self.db.commit()

    def add_event(self, strategy_id: str, bar_time: str, kind: str, message: str, notified: bool,
                  run_id: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO events (strategy_id, run_id, bar_time, kind, message, notified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (strategy_id, run_id, bar_time, kind, message, int(notified), _now()),
        )
        self.db.commit()

    def trades(self, strategy_id: str | None = None, run_id: int | None = None) -> list[dict]:
        q, args = "SELECT * FROM paper_trades WHERE 1=1", []
        if strategy_id:
            q += " AND strategy_id = ?"
            args.append(strategy_id)
        if run_id is not None:
            q += " AND run_id = ?"
            args.append(run_id)
        return [self._trade_row(r) for r in self.db.execute(q + " ORDER BY id", args)]

    @staticmethod
    def _trade_row(r) -> dict:
        d = dict(r)
        for c in JSON_COLS:
            try:
                d[c] = json.loads(d[c]) if d.get(c) else {}
            except (TypeError, ValueError):
                d[c] = {}
        return d

    def paper_trade(self, trade_id: int) -> dict | None:
        r = self.db.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------------ price alerts

    def price_alerts(self, market: str | None = None, active_only: bool = False) -> list[dict]:
        q, args = "SELECT * FROM price_alerts WHERE 1=1", []
        if market:
            q += " AND market = ?"
            args.append(market)
        if active_only:
            q += " AND active = 1"
        return [dict(r) for r in self.db.execute(q + " ORDER BY id DESC", args)]

    def add_price_alert(self, market: str, label: str, direction: str, price: float, note: str = "") -> dict:
        cur = self.db.execute(
            "INSERT INTO price_alerts (market, label, direction, price, note, created_at, checked_to) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (market, label, direction, price, note, _now(), _now()))
        self.db.commit()
        return self.price_alert(cur.lastrowid)

    def price_alert(self, alert_id: int) -> dict | None:
        r = self.db.execute("SELECT * FROM price_alerts WHERE id = ?", (alert_id,)).fetchone()
        return dict(r) if r else None

    def update_price_alert(self, alert_id: int, **fields) -> None:
        allowed = {"direction", "price", "note", "checked_to", "triggered_at", "triggered_price", "active"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        self.db.execute(f"UPDATE price_alerts SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
                        [*sets.values(), alert_id])
        self.db.commit()

    def delete_price_alert(self, alert_id: int) -> None:
        self.db.execute("DELETE FROM price_alerts WHERE id = ?", (alert_id,))
        self.db.commit()

    # ------------------------------------------------------------------ news notes and reminders

    def news_notes(self) -> dict[str, dict]:
        return {r["key"]: dict(r) for r in self.db.execute("SELECT * FROM news_notes")}

    def save_news_note(self, key: str, time: str, currency: str, title: str, note: str,
                       alert_minutes: int | None) -> dict:
        old = self.db.execute("SELECT * FROM news_notes WHERE key = ?", (key,)).fetchone()
        alerted = old["alerted_at"] if old and old["alert_minutes"] == alert_minutes else None
        if not note and alert_minutes is None:
            self.db.execute("DELETE FROM news_notes WHERE key = ?", (key,))
        else:
            self.db.execute(
                "INSERT INTO news_notes (key, time, currency, title, note, alert_minutes, alerted_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET time = excluded.time, note = excluded.note, "
                "alert_minutes = excluded.alert_minutes, alerted_at = excluded.alerted_at",
                (key, time, currency, title, note, alert_minutes, alerted))
        self.db.commit()
        return {"key": key, "note": note, "alert_minutes": alert_minutes, "alerted_at": alerted}

    def due_news_reminders(self, now_iso: str = "") -> list[dict]:
        """Reminders not sent yet (the caller decides which are due)."""
        rows = self.db.execute("SELECT * FROM news_notes WHERE alert_minutes IS NOT NULL AND alerted_at IS NULL")
        return [dict(r) for r in rows]

    def mark_news_reminded(self, key: str, when: str) -> None:
        self.db.execute("UPDATE news_notes SET alerted_at = ? WHERE key = ?", (when, key))
        self.db.commit()

    # ------------------------------------------------------------------ trade journal

    JOURNAL_COLS = ("market", "direction", "entry_time", "entry_price", "exit_time", "exit_price", "size",
                    "point_value", "fees", "currency", "strategy_id", "paper_trade_id", "notes")

    def journal(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM journal ORDER BY entry_time DESC, id DESC")]

    def journal_entry(self, entry_id: int) -> dict | None:
        r = self.db.execute("SELECT * FROM journal WHERE id = ?", (entry_id,)).fetchone()
        return dict(r) if r else None

    def save_journal(self, entry: dict, entry_id: int | None = None) -> int:
        cols = self.JOURNAL_COLS
        vals = [entry.get(c) for c in cols]
        if entry_id is None:
            cur = self.db.execute(
                f"INSERT INTO journal ({', '.join(cols)}, created_at) VALUES ({', '.join('?' * len(cols))}, ?)",
                [*vals, _now()])
            entry_id = cur.lastrowid
        else:
            self.db.execute(f"UPDATE journal SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?", [*vals, entry_id])
        self.db.commit()
        return entry_id

    def delete_journal(self, entry_id: int) -> None:
        self.db.execute("DELETE FROM journal WHERE id = ?", (entry_id,))
        self.db.commit()

    # ------------------------------------------------------------------ restore

    TABLES = ("strategy_state", "runs", "paper_trades", "run_equity", "events", "price_alerts", "journal", "news_notes",
              "strategy_versions", "settings_kv")

    def replace_from(self, src: str | Path) -> dict[str, int]:
        """Replace everything with the rows of another Trading Alerts database (a backup).

        Rows are copied table by table into this database's own schema, so a backup
        from an older version (or a damaged or hostile file) can't bring anything else in.
        """
        con = sqlite3.connect(f"file:{Path(src).as_posix()}?mode=ro", uri=True)
        try:
            con.execute("PRAGMA trusted_schema = OFF")
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("The database in the backup is damaged")
            have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {"runs", "paper_trades"} <= have:
                raise ValueError("The database in the backup isn't from Trading Alerts")
            rows: dict[str, tuple[list[str], list]] = {}
            for table in self.TABLES:
                if table not in have:
                    continue
                mine = [r[1] for r in self.db.execute(f"PRAGMA table_info({table})")]
                theirs = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                cols = [c for c in mine if c in theirs]
                if cols:
                    rows[table] = (cols, con.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall())
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"The database in the backup can't be read: {exc}") from None
        finally:
            con.close()
        counts = {}
        with self.db:  # one transaction: all or nothing
            for table in self.TABLES:
                self.db.execute(f"DELETE FROM {table}")
            for table, (cols, data) in rows.items():
                self.db.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                    data)
                counts[table] = len(data)
        return counts

    def events_page(self, kinds: list[str] | None = None, strategy_id: str | None = None,
                    before: str | None = None, after: str | None = None, limit: int = 50) -> list[dict]:
        """Events newest first, optionally only some kinds, one strategy, or older than ``before``
        / newer than ``after`` (by when they were recorded)."""
        q, args = "SELECT * FROM events WHERE 1=1", []
        if kinds:
            q += f" AND kind IN ({', '.join('?' * len(kinds))})"
            args += kinds
        if strategy_id:
            q += " AND strategy_id = ?"
            args.append(strategy_id)
        if before:
            q += " AND created_at < ?"
            args.append(before)
        if after:
            q += " AND created_at > ?"
            args.append(after)
        q += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(q, args)]

    def recent_events(self, limit: int = 20, run_id: int | None = None) -> list[dict]:
        if run_id is not None:
            rows = self.db.execute("SELECT * FROM events WHERE run_id = ? ORDER BY id DESC LIMIT ?", (run_id, limit))
        else:
            rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ strategy versions

    def add_version(self, strategy_id: str, hash_: str, text: str, note: str, keep: int | None = None) -> dict:
        """Keep ``text`` as the strategy's next version, unless it's the same as the latest one.
        With ``keep``, only that many of the newest versions are kept."""
        latest = self.latest_version(strategy_id)
        if latest and latest["hash"] == hash_:
            return latest
        no = (latest["version_no"] if latest else 0) + 1
        self.db.execute(
            "INSERT INTO strategy_versions (strategy_id, version_no, hash, yaml, saved_at, note) VALUES (?, ?, ?, ?, ?, ?)",
            (strategy_id, no, hash_, text, _now(), note),
        )
        if keep:
            self.db.execute("DELETE FROM strategy_versions WHERE strategy_id = ? AND version_no <= ?",
                            (strategy_id, no - keep))
        self.db.commit()
        return self.version(strategy_id, no)

    def latest_version(self, strategy_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM strategy_versions WHERE strategy_id = ? ORDER BY version_no DESC LIMIT 1",
                              (strategy_id,)).fetchone()
        return dict(row) if row else None

    def version(self, strategy_id: str, version_no: int) -> dict | None:
        row = self.db.execute("SELECT * FROM strategy_versions WHERE strategy_id = ? AND version_no = ?",
                              (strategy_id, version_no)).fetchone()
        return dict(row) if row else None

    def version_by_hash(self, strategy_id: str, hash_: str) -> dict | None:
        row = self.db.execute("SELECT * FROM strategy_versions WHERE strategy_id = ? AND hash = ? "
                              "ORDER BY version_no DESC LIMIT 1", (strategy_id, hash_)).fetchone()
        return dict(row) if row else None

    def versions(self, strategy_id: str) -> list[dict]:
        """Newest first, without the file text."""
        return [dict(r) for r in self.db.execute(
            "SELECT strategy_id, version_no, hash, saved_at, note FROM strategy_versions "
            "WHERE strategy_id = ? ORDER BY version_no DESC", (strategy_id,))]

    def delete_versions(self, strategy_id: str) -> None:
        self.db.execute("DELETE FROM strategy_versions WHERE strategy_id = ?", (strategy_id,))
        self.db.commit()

    # ------------------------------------------------------------------ small settings

    def get_value(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings_kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_value(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO settings_kv (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.db.commit()
