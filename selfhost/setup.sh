#!/usr/bin/env bash
# Trading Alerts setup for Linux and macOS.
#
#   ./setup.sh             asks what you'd like to do
#   ./setup.sh --web       install, then open it in your browser
#   ./setup.sh --service   install and keep it running in the background (starts when you log in)
#   ./setup.sh --desktop   build the desktop app (needs Rust and Node.js)
#   ./setup.sh --docker    run it in Docker (for a home server or VPS)
#   ./setup.sh --remove-service
#
# Your strategies and settings go in ~/TradingAlerts (change with --folder PATH).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$HERE/engine"; [ -d "$ENGINE" ] || ENGINE="$HERE/../engine"
DESKTOP="$HERE/desktop"
FOLDER="${TRADEALERTS_FOLDER:-$HOME/TradingAlerts}"
PORT="${TRADEALERTS_PORT:-8321}"
case "$(uname -s)" in
  Darwin) OS=mac; APPDIR="$HOME/Library/Application Support/Trading Alerts" ;;
  *)      OS=linux; APPDIR="${XDG_DATA_HOME:-$HOME/.local/share}/trading-alerts" ;;
esac
VENV="$APPDIR/venv"
TA="$VENV/bin/tradealerts"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
say()  { printf '%s\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local a; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }

# --------------------------------------------------------------------------- the engine

find_python() {
  for p in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  return 1
}

install_engine() {
  [ -f "$ENGINE/pyproject.toml" ] || fail "Can't find the engine folder next to this script ($ENGINE)."
  local py
  if ! py="$(find_python)"; then
    say "Trading Alerts needs Python 3.10 or newer."
    if [ "$OS" = mac ]; then say "Install it from https://www.python.org/downloads/ (or: brew install python), then run this again."
    else say "Install it with your package manager, e.g.  sudo apt install python3 python3-venv  or  sudo pacman -S python"; fi
    exit 1
  fi
  if [ ! -x "$VENV/bin/python" ]; then
    bold "Setting up Python packages (one-off, takes a minute or two)…"
    mkdir -p "$APPDIR"
    "$py" -m venv "$VENV" || fail "Couldn't create a Python environment. On Debian/Ubuntu: sudo apt install python3-venv"
  fi
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet --upgrade "${ENGINE}[yahoo]"
  if [ ! -f "$FOLDER/config.yaml" ]; then
    "$TA" init "$FOLDER" >/dev/null
    say "Your strategies and settings will be kept in $FOLDER"
  fi
  # A launcher you can run any time: ~/.local/bin/trading-alerts
  mkdir -p "$HOME/.local/bin"
  cat > "$HOME/.local/bin/trading-alerts" <<EOF
#!/usr/bin/env bash
# Start Trading Alerts and open it in your browser (installed by setup.sh).
cd "$FOLDER" && exec "$TA" serve --port $PORT "\$@"
EOF
  chmod +x "$HOME/.local/bin/trading-alerts"
}

open_browser() {
  local url="http://localhost:$PORT"
  for _ in $(seq 1 60); do
    if curl -fsS "$url/api/session" >/dev/null 2>&1; then break; fi
    sleep 0.5
  done
  if [ "$OS" = mac ]; then open "$url"; else (xdg-open "$url" >/dev/null 2>&1 &) || true; fi
}

# --------------------------------------------------------------------------- choices

do_web() {
  install_engine
  bold "Starting Trading Alerts at http://localhost:$PORT (this computer only). Press Ctrl+C to stop."
  say "Next time, start it with:  trading-alerts   (or ./setup.sh --web)"
  open_browser &
  cd "$FOLDER" && exec "$TA" serve --port "$PORT"
}

do_service() {
  install_engine
  if [ "$OS" = mac ]; then
    local plist="$HOME/Library/LaunchAgents/uk.william-cooper.tradealerts.plist"
    mkdir -p "$(dirname "$plist")" "$FOLDER/logs"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>uk.william-cooper.tradealerts</string>
  <key>ProgramArguments</key><array>
    <string>$TA</string><string>serve</string><string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$FOLDER</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$FOLDER/logs/trading-alerts.log</string>
  <key>StandardErrorPath</key><string>$FOLDER/logs/trading-alerts.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/uk.william-cooper.tradealerts" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
  else
    command -v systemctl >/dev/null 2>&1 || fail "This needs systemd. Start it yourself with: trading-alerts"
    local unit="$HOME/.config/systemd/user/trading-alerts.service"
    mkdir -p "$(dirname "$unit")"
    cat > "$unit" <<EOF
[Unit]
Description=Trading Alerts (backtests, forward tests and alerts)
After=network-online.target

[Service]
WorkingDirectory=$FOLDER
ExecStart=$TA serve --port $PORT
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now trading-alerts.service
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
      say ""
      say "It runs while you're logged in. To keep it running after you log out (e.g. on a home server):"
      say "  sudo loginctl enable-linger $USER"
    fi
  fi
  bold "Trading Alerts is running in the background at http://localhost:$PORT"
  say "It starts again by itself when you log in. Remove it with: ./setup.sh --remove-service"
  open_browser
}

do_remove_service() {
  if [ "$OS" = mac ]; then
    launchctl bootout "gui/$(id -u)/uk.william-cooper.tradealerts" >/dev/null 2>&1 || true
    rm -f "$HOME/Library/LaunchAgents/uk.william-cooper.tradealerts.plist"
  else
    systemctl --user disable --now trading-alerts.service >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/trading-alerts.service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  say "The background service is removed. Your strategies and settings in $FOLDER are untouched."
}

need_desktop_tools() {
  local missing=0
  if ! command -v cargo >/dev/null 2>&1; then
    say "- Rust isn't installed."
    if ask "  Install Rust now (from rustup.rs, into your home folder)?"; then
      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
      # shellcheck disable=SC1091
      . "$HOME/.cargo/env"
    else missing=1; fi
  fi
  if ! command -v npm >/dev/null 2>&1; then
    say "- Node.js isn't installed. Get it from https://nodejs.org (version 20 or newer), or your package manager."
    missing=1
  fi
  if [ "$OS" = linux ] && ! pkg-config --exists webkit2gtk-4.1 2>/dev/null; then
    say "- The WebKitGTK libraries the window needs aren't installed. For example:"
    say "    Ubuntu/Debian: sudo apt install libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev build-essential"
    say "    Fedora:        sudo dnf install webkit2gtk4.1-devel libappindicator-gtk3-devel librsvg2-devel"
    say "    Arch/Manjaro:  sudo pacman -S --needed webkit2gtk-4.1 libappindicator-gtk3 librsvg base-devel"
    missing=1
  fi
  if [ "$OS" = mac ] && ! xcode-select -p >/dev/null 2>&1; then
    say "- Apple's command line tools are needed: xcode-select --install"
    missing=1
  fi
  [ "$missing" = 0 ] || fail "Install the missing pieces above, then run ./setup.sh --desktop again."
}

do_desktop() {
  [ -d "$DESKTOP" ] || fail "The desktop folder isn't next to this script."
  install_engine
  need_desktop_tools
  bold "Building the desktop app. The first build takes 5 to 15 minutes…"
  "$VENV/bin/python" -m pip install --quiet pyinstaller
  ( cd "$DESKTOP" && PATH="$VENV/bin:$PATH" npm ci --no-audit --no-fund && PATH="$VENV/bin:$PATH" npm run build )
  bold "Done. Your installers are in:"
  say "  $DESKTOP/src-tauri/target/release/bundle/"
  if [ "$OS" = linux ]; then say "Install the .deb (Debian/Ubuntu), the .rpm (Fedora), or run the .AppImage directly."
  else say "Open the .dmg and drag Trading Alerts into Applications."; fi
}

do_docker() {
  command -v docker >/dev/null 2>&1 || fail "Docker isn't installed. See https://docs.docker.com/engine/install/"
  local compose_dir="$HERE"
  [ -f "$compose_dir/docker-compose.yml" ] || fail "docker-compose.yml isn't next to this script."
  if [ ! -f "$compose_dir/.env" ]; then
    local pw
    pw="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 || true)"
    printf 'TRADEALERTS_PASSWORD=%s\n' "$pw" > "$compose_dir/.env"
    bold "Your web interface password is: $pw"
    say "(It's saved in $compose_dir/.env. Change it there and restart to choose your own.)"
  fi
  mkdir -p "$compose_dir/workspace"
  ( cd "$compose_dir" && docker compose up -d --build )
  bold "Running at http://localhost:$PORT (this machine only). Put Caddy or another HTTPS proxy in front to use it remotely; see README.md."
}

menu() {
  bold "Trading Alerts setup"
  say ""
  say "  1) Use it in your web browser (starts now, on this computer only)"
  say "  2) Keep it running in the background, so forward tests and alerts carry on (starts when you log in)"
  say "  3) Build the desktop app (needs Rust and Node.js; about 10 minutes)"
  say "  4) Run it in Docker (for a home server or VPS)"
  say "  5) Remove the background service"
  say ""
  local c; read -r -p "Choose 1-5: " c
  case "$c" in
    1) do_web ;; 2) do_service ;; 3) do_desktop ;; 4) do_docker ;; 5) do_remove_service ;;
    *) fail "Choose a number from 1 to 5." ;;
  esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    --folder) FOLDER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --web) ACTION=web; shift ;;
    --service) ACTION=service; shift ;;
    --desktop) ACTION=desktop; shift ;;
    --docker) ACTION=docker; shift ;;
    --remove-service) ACTION=remove; shift ;;
    --install-only) ACTION=install; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) fail "Unknown option $1 (see ./setup.sh --help)" ;;
  esac
done
case "${ACTION:-menu}" in
  web) do_web ;; service) do_service ;; desktop) do_desktop ;; docker) do_docker ;;
  remove) do_remove_service ;; install) install_engine; say "Installed. Start it with: trading-alerts" ;; *) menu ;;
esac
