# Trading Alerts desktop app

A [Tauri 2](https://tauri.app) shell around the Python engine. The window shows the
same interface as `tradealerts serve`; the engine runs in the background on
`127.0.0.1` only and stops when the app quits.

```
desktop/
  src-tauri/          Rust app: starts the engine, window, tray icon
  splash/             shown while the engine starts (or if it can't)
  engine/             PyInstaller build of the engine, so buyers don't need Python
  app-icon.png        source for the icons (npm run icons regenerates them)

The GitHub workflow .github/workflows/selfhost-desktop.yml builds installers for all three systems.
```

## Build it the easy way

`setup.sh --desktop` (Linux, macOS) or `setup.ps1 -Desktop` (Windows), from the folder above
this one, checks for Rust and Node.js, installs the engine and builds the installer.

## Run it from source (Linux, e.g. Manjaro)

You need Rust, Node and the WebKitGTK libraries, plus the engine installed with pipx.
From the folder that holds `engine/`:

```bash
sudo pacman -S --needed rustup nodejs npm webkit2gtk-4.1 base-devel curl wget file openssl \
    appmenu-gtk-module libappindicator-gtk3 librsvg xdotool
rustup default stable
pipx install --force "./engine[yahoo]"   # puts `tradealerts` on your PATH

cd desktop
npm install
npm run dev                          # first build takes a few minutes
```

In development the app runs the `tradealerts` command from your PATH (or the program
in `$TRADEALERTS_ENGINE`). Its data folder is `~/.local/share/uk.william-cooper.tradealerts/`
on Linux, `~/Library/Application Support/uk.william-cooper.tradealerts/` on a Mac and
`%APPDATA%\uk.william-cooper.tradealerts\` on Windows. The engine's log is `engine.log` there.

## Build installers

```bash
# from the folder that holds engine/, in a virtual environment (pipx can't install PyInstaller next to it)
python -m venv .venv && . .venv/bin/activate
pip install "./engine[yahoo]" pyinstaller
cd desktop && npm run build      # builds the engine, then the app with the engine inside
```

Installers land in `src-tauri/target/release/bundle/` (`.deb`, `.rpm`, `.AppImage` on
Linux; `.msi` and setup `.exe` on Windows; `.dmg` on a Mac). PyInstaller can only build
for the system it runs on, so use the GitHub workflow to get all three: run
`.github/workflows/selfhost-desktop.yml` from the Actions tab (or push a tag like `v0.2.0`).
The installers appear as downloads on the run.

Before selling it, sign the Windows and macOS builds, or buyers see "unknown developer"
warnings: an Apple Developer account for macOS notarisation, and a code-signing
certificate for Windows. The workflow has the macOS settings ready to switch on.

## How it behaves

- Starts the engine on port 47821 (or the next free one), then loads the interface.
- Closing the window while forward tests or alerts are running keeps the app in the
  tray (with a one-time notification saying so). Otherwise closing quits.
- Opening the app again brings the existing window back instead of starting a second engine.
- Quitting asks the engine to finish its current check, and stops it after 5 seconds
  at most. If the app crashes, the engine notices and stops itself.
- Downloads (backups, the journal CSV, exported strategies) are saved to the Downloads
  folder, with a notification saying where. Links to other websites open in the browser.
