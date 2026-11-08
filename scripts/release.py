"""Make the files you hand out.

    python scripts/release.py

Writes to dist/:

* trading-alerts-selfhosted-<version>.zip  what self-hosted customers download: the engine,
  setup.sh / setup.ps1, Docker files and the desktop app's source.
"""

from __future__ import annotations

import io
import re
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", "target", "gen", "build", "dist", ".git", "tests",
             "workspace", "data", ".venv"}
SKIP_FILES = {".env"}
EXECUTABLE = {"setup.sh"}


def version() -> str:
    text = (ROOT / "engine" / "pyproject.toml").read_text()
    return re.search(r'^version = "([^"]+)"', text, re.M).group(1)


def files(base: Path):
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts[:-1] + (rel.parts[-1:] if p.is_dir() else ())):
            continue
        if p.is_file() and p.name not in SKIP_FILES and not p.name.endswith((".pyc", ".tmp")):
            yield p, rel


class Archive:
    """The same calls for a zip (customers on any system) and a tar.gz (the Linux server)."""

    def __init__(self, path: Path, prefix: str):
        self.prefix = prefix
        self.zip = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) if path.suffix == ".zip" else None
        self.tar = None if self.zip else tarfile.open(path, "w:gz")

    def add_bytes(self, name: str, data: bytes, executable: bool = False) -> None:
        arc = f"{self.prefix}/{name}"
        mode = 0o755 if executable else 0o644
        if self.zip:
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            self.zip.writestr(info, data)
        else:
            info = tarfile.TarInfo(arc)
            info.size, info.mode, info.mtime = len(data), mode, 1767225600
            self.tar.addfile(info, io.BytesIO(data))

    def add_tree(self, src: Path, dest: str) -> None:
        for p, rel in files(src):
            self.add_bytes(f"{dest}/{rel.as_posix()}", p.read_bytes(), executable=p.name in EXECUTABLE)

    def add_file(self, src: Path, dest: str) -> None:
        self.add_bytes(dest, src.read_bytes(), executable=src.name in EXECUTABLE)

    def close(self) -> None:
        (self.zip or self.tar).close()


def flat_compose(path: Path, dockerfile: str) -> bytes:
    """The compose file with its build context moved to the archive's own folder."""
    text = path.read_text()
    text = re.sub(r"context: \.\.[^\n]*", "context: .", text)
    text = re.sub(r"dockerfile: \S+", f"dockerfile: {dockerfile}", text)
    return text.encode()


def selfhosted(v: str) -> Path:
    out = DIST / f"trading-alerts-selfhosted-{v}.zip"
    a = Archive(out, f"trading-alerts-{v}")
    a.add_tree(ROOT / "engine", "engine")
    a.add_tree(ROOT / "selfhost" / "desktop", "desktop")
    for name in ("setup.sh", "setup.ps1", "Dockerfile", ".env.example", ".dockerignore", "README.md"):
        a.add_file(ROOT / "selfhost" / name, name)
    a.add_bytes("docker-compose.yml", flat_compose(ROOT / "selfhost" / "docker-compose.yml", "Dockerfile"))
    a.add_file(ROOT / "engine" / "DISCLAIMER.md", "DISCLAIMER.md")
    a.add_file(ROOT / "LICENSE", "LICENSE")
    a.close()
    return out


def main() -> int:
    v = version()
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()
    for path in (selfhosted(v),):
        print(f"{path.relative_to(ROOT)}  ({path.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
