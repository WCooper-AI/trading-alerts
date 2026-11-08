"""App configuration (config.yaml) with ${ENV_VAR} and ${ENV_VAR:-default} substitution.

The hosted edition loads each customer's config with ``trusted=False``: environment
variables are never expanded (they'd leak the server's secrets into a customer's
webhook URL) and folder locations can't be moved out of the customer's own folder.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

PATH_KEYS = ("strategies_dir", "data_dir", "reports_dir", "state_db")
ALLOWED_KEYS = {*PATH_KEYS, "poll_minutes", "notifiers", "log_level", "data", "appearance", "report"}
DATA_SOURCES = ("dukascopy", "yahoo", "oanda")


class ConfigError(ValueError):
    pass


def expand_env(value):
    if isinstance(value, str):
        def repl(m):
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"Environment variable {name} is not set (used in config)")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def uses_env(value) -> bool:
    """True if a value (or anything inside it) contains a ${VAR} reference."""
    if isinstance(value, str):
        return bool(_ENV_RE.search(value))
    if isinstance(value, list):
        return any(uses_env(v) for v in value)
    if isinstance(value, dict):
        return any(uses_env(v) for v in value.values())
    return False


@dataclass
class AppConfig:
    base_dir: Path
    strategies_dir: Path
    data_dir: Path
    reports_dir: Path
    state_db: Path
    poll_minutes: float = 15
    notifiers: list[dict] = field(default_factory=lambda: [{"type": "console"}])
    log_level: str = "INFO"
    data: dict = field(default_factory=lambda: {"source": "dukascopy", "oanda_token": "", "oanda_environment": "practice"})
    report: dict = field(default_factory=dict)
    cache_dir: Path | None = None  # downloaded prices; shared between customers in the hosted edition
    uploads_dir: Path | None = None   # CSV files uploaded in the web interface
    csv_root: Path | None = None      # CSV price files may only be read from inside this folder
    csv_base_dir: Path | None = None  # relative CSV paths in strategies start here

    def __post_init__(self):
        if self.cache_dir is None:
            self.cache_dir = self.data_dir / "cache"
        if self.uploads_dir is None:
            self.uploads_dir = self.data_dir / "csv"
        if self.csv_root is None:
            self.csv_root = self.data_dir
        if self.csv_base_dir is None:
            self.csv_base_dir = self.base_dir


def read_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: settings must be a mapping")
    return data


def load_config(path: str | Path | None = None, *, must_exist: bool | None = None, trusted: bool = True,
                cache_dir: str | Path | None = None, min_poll_minutes: float = 0) -> AppConfig:
    """Read config.yaml (defaults if it doesn't exist yet, unless ``must_exist``).

    ``trusted=False`` is for files a customer controls on a shared server."""
    p = Path(path) if path else Path("config.yaml")
    if must_exist is None:
        must_exist = path is not None
    if must_exist and not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    data = read_raw(p)
    extra = set(data) - ALLOWED_KEYS
    if extra:
        raise ConfigError(f"{p}: unknown key(s) {', '.join(sorted(extra))}")
    if trusted:
        data = expand_env(data)
    else:
        data = {k: v for k, v in data.items() if k not in PATH_KEYS}
    base = p.resolve().parent

    def rel(key, default):
        v = Path(str(data.get(key, default)))
        return v if v.is_absolute() else base / v

    data_dir = rel("data_dir", "data")
    poll = data.get("poll_minutes", 15)
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or poll <= 0:
        raise ConfigError("poll_minutes must be a positive number")
    poll = max(float(poll), float(min_poll_minutes))
    return AppConfig(
        base_dir=base,
        strategies_dir=rel("strategies_dir", "strategies"),
        data_dir=data_dir,
        reports_dir=rel("reports_dir", "reports"),
        state_db=rel("state_db", data_dir / "state.db"),
        poll_minutes=poll,
        notifiers=data.get("notifiers") or [{"type": "console"}],
        log_level=str(data.get("log_level", "INFO")).upper(),
        data=data_settings(data.get("data") or {}),
        report=data.get("report") if isinstance(data.get("report"), dict) else {},
        cache_dir=Path(cache_dir) if cache_dir else None,
        uploads_dir=None if trusted else data_dir / "uploads",
        csv_root=None if trusted else data_dir / "uploads",
        csv_base_dir=None if trusted else data_dir / "uploads",
    )


def data_settings(d: dict, sources: tuple[str, ...] = DATA_SOURCES) -> dict:
    if not isinstance(d, dict):
        raise ConfigError("data must be a mapping")
    source = str(d.get("source", "dukascopy")).lower()
    if source not in sources:
        raise ConfigError(f"data.source must be one of: {', '.join(sources)}")
    env = str(d.get("oanda_environment", "practice")).lower()
    if env not in ("practice", "live"):
        raise ConfigError("data.oanda_environment must be practice or live")
    return {"source": source, "oanda_token": str(d.get("oanda_token") or ""), "oanda_environment": env}


_data_settings = data_settings  # old name, kept for anything importing it
