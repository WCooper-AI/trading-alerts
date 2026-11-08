"""Build the engine the desktop app ships with: a self-contained folder with Python,
pandas and Trading Alerts inside, so buyers don't need Python installed.

    pip install "./engine[yahoo]" pyinstaller     # in a virtual environment
    python desktop/engine/build_engine.py         # or: setup.sh --desktop, which does both

Output: desktop/engine/dist/tradealerts-engine/ (bundled into the app by tauri.bundle.json).
PyInstaller can only build for the system it runs on, so build on each OS (the GitHub
workflow .github/workflows/selfhost-desktop.yml does Windows, macOS and Linux).
"""
from pathlib import Path

import PyInstaller.__main__

HERE = Path(__file__).resolve().parent

PyInstaller.__main__.run([
    str(HERE / "engine_main.py"),
    "--name", "tradealerts-engine",
    "--onedir",            # starts much faster than --onefile, which unpacks itself on every launch
    "--noconfirm",
    "--clean",
    "--console",           # the desktop app hides the console window and logs to engine.log
    "--distpath", str(HERE / "dist"),
    "--workpath", str(HERE / "build"),
    "--specpath", str(HERE / "build"),
    "--collect-data", "tradealerts",
    "--collect-submodules", "tradealerts",   # includes tradealerts.web.routes (imported by name)
    "--collect-submodules", "uvicorn",
    "--collect-all", "dukascopy_python",
    "--collect-all", "yfinance",
    # Not used by the engine, but pulled in if they happen to be installed alongside it.
    *[arg for mod in ("tkinter", "matplotlib", "scipy", "PIL", "IPython", "pytest", "pkg_resources", "setuptools")
      for arg in ("--exclude-module", mod)],
])
print("Engine built in", HERE / "dist" / "tradealerts-engine")
