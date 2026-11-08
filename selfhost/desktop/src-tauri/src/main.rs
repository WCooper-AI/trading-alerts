// Trading Alerts desktop app.
//
// A small native shell around the same engine that `tradealerts serve` runs. On launch it
// starts the engine on a free local port (bound to this computer only), shows a splash
// screen until the engine answers, then loads the interface in a native window.
//
// Forward tests and alerts need the engine running, so closing the window while any are
// running keeps the app in the system tray. Quit from the tray (or Cmd+Q on a Mac) stops
// everything. The engine also stops by itself if this app disappears.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::DownloadEvent;
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

const FIRST_PORT: u16 = 47821;
const START_TIMEOUT: Duration = Duration::from_secs(120);

#[derive(Default)]
struct Engine {
    child: Mutex<Option<Child>>,
    port: Mutex<Option<u16>>,
    told_about_tray: Mutex<bool>,
}

fn data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    fs::create_dir_all(&dir).map_err(|e| format!("Couldn't create {}: {e}", dir.display()))?;
    Ok(dir)
}

/// Same port as last time when it's free, so the interface keeps its saved preferences.
fn pick_port() -> u16 {
    for p in FIRST_PORT..FIRST_PORT + 50 {
        if TcpListener::bind(("127.0.0.1", p)).is_ok() {
            return p;
        }
    }
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(FIRST_PORT)
}

/// The engine bundled with the app; when running from source, $TRADEALERTS_ENGINE or
/// `tradealerts` from PATH (e.g. a pipx install).
fn engine_command(app: &AppHandle) -> Command {
    let exe = if cfg!(windows) { "tradealerts-engine.exe" } else { "tradealerts-engine" };
    if let Ok(dir) = app.path().resource_dir() {
        let bundled = dir.join("engine").join(exe);
        if bundled.exists() {
            return Command::new(bundled);
        }
    }
    match std::env::var("TRADEALERTS_ENGINE") {
        Ok(path) if !path.is_empty() => Command::new(path),
        _ => Command::new("tradealerts"),
    }
}

fn start_engine(app: &AppHandle, port: u16) -> Result<Child, String> {
    let dir = data_dir(app)?;
    let log_path = dir.join("engine.log");
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| format!("Couldn't open {}: {e}", log_path.display()))?;
    let err_log = log.try_clone().map_err(|e| e.to_string())?;
    let mut cmd = engine_command(app);
    cmd.arg("--config")
        .arg(dir.join("config.yaml"))
        .arg("serve")
        .arg("--desktop")
        .arg("--port")
        .arg(port.to_string())
        .arg("--parent-pid")
        .arg(std::process::id().to_string())
        .current_dir(&dir)
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(err_log);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console window
    }
    cmd.spawn().map_err(|e| {
        format!(
            "Couldn't start the Trading Alerts engine ({e}). If you're running from source, install it \
             first (pipx install .) or set TRADEALERTS_ENGINE."
        )
    })
}

/// Minimal HTTP/1.1 request to the local engine. Returns the response body.
fn http(port: u16, method: &str, path: &str) -> Option<String> {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut s = TcpStream::connect_timeout(&addr, Duration::from_millis(800)).ok()?;
    s.set_read_timeout(Some(Duration::from_secs(3))).ok()?;
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Requested-With: tradealerts\r\n\
         Content-Length: 0\r\nConnection: close\r\n\r\n"
    );
    s.write_all(req.as_bytes()).ok()?;
    let mut buf = String::new();
    s.read_to_string(&mut buf).ok()?;
    if !buf.starts_with("HTTP/1.1 2") {
        return None;
    }
    buf.split_once("\r\n\r\n").map(|(_, body)| body.to_string())
}

fn engine_port(app: &AppHandle) -> Option<u16> {
    *app.state::<Engine>().port.lock().unwrap()
}

/// True when forward tests or alerts are running, so closing the window shouldn't stop them.
fn work_running(app: &AppHandle) -> bool {
    let Some(port) = engine_port(app) else { return false };
    let Some(body) = http(port, "GET", "/api/status") else { return false };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&body) else { return false };
    let n = |k: &str| v.get(k).and_then(|x| x.as_u64()).unwrap_or(0);
    n("running_tests") > 0 || n("live_strategies") > 0
}

fn show_error(app: &AppHandle, message: &str) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let js = format!(
            "window.showError && window.showError({})",
            serde_json::to_string(message).unwrap_or_default()
        );
        // The splash may still be loading: try a few times.
        for _ in 0..5 {
            let _ = w.eval(&js);
            thread::sleep(Duration::from_millis(400));
        }
    }
}

fn boot(app: AppHandle) {
    let port = pick_port();
    let child = match start_engine(&app, port) {
        Ok(c) => c,
        Err(e) => return show_error(&app, &e),
    };
    {
        let state = app.state::<Engine>();
        *state.child.lock().unwrap() = Some(child);
        *state.port.lock().unwrap() = Some(port);
    }
    let log = data_dir(&app)
        .map(|d| d.join("engine.log").display().to_string())
        .unwrap_or_default();
    let started = Instant::now();
    loop {
        if http(port, "GET", "/api/session").is_some() {
            break;
        }
        let exited = {
            let state = app.state::<Engine>();
            let mut guard = state.child.lock().unwrap();
            guard.as_mut().and_then(|c| c.try_wait().ok().flatten())
        };
        if let Some(status) = exited {
            return show_error(&app, &format!("The engine stopped while starting ({status}). Details: {log}"));
        }
        if started.elapsed() > START_TIMEOUT {
            return show_error(&app, &format!("The engine didn't start within two minutes. Details: {log}"));
        }
        thread::sleep(Duration::from_millis(200));
    }
    if let Some(w) = app.get_webview_window("main") {
        if let Ok(url) = Url::parse(&format!("http://127.0.0.1:{port}/")) {
            let _ = w.navigate(url);
        }
    }
}

fn stop_engine(app: &AppHandle) {
    let state = app.state::<Engine>();
    let port = *state.port.lock().unwrap();
    let mut guard = state.child.lock().unwrap();
    if let Some(mut child) = guard.take() {
        if let Some(port) = port {
            let _ = http(port, "POST", "/api/shutdown"); // finish the current check cleanly
            let deadline = Instant::now() + Duration::from_secs(5);
            while Instant::now() < deadline {
                if let Ok(Some(_)) = child.try_wait() {
                    return;
                }
                thread::sleep(Duration::from_millis(100));
            }
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// Pages the window may show itself: the splash screen and the local engine. Anything else
/// (help pages, ntfy, OANDA) opens in the normal browser instead.
fn is_inside(app: &AppHandle, url: &Url) -> bool {
    match url.scheme() {
        "tauri" | "asset" | "about" | "data" | "blob" => true,
        "http" | "https" => {
            let local = matches!(url.host_str(), Some("127.0.0.1") | Some("localhost") | Some("tauri.localhost"));
            local && (url.host_str() == Some("tauri.localhost") || url.port() == engine_port(app))
        }
        _ => false,
    }
}

fn open_outside(app: &AppHandle, url: &Url) {
    if matches!(url.scheme(), "http" | "https" | "mailto") {
        let _ = app.opener().open_url(url.as_str(), None::<&str>);
    }
}

/// Downloads (backups, journal CSV, exported strategies) go to the Downloads folder,
/// with a notification saying where, since a webview has no download bar of its own.
fn save_download(app: &AppHandle, event: DownloadEvent<'_>) -> bool {
    match event {
        DownloadEvent::Requested { url, destination } => {
            let dir = app.path().download_dir().unwrap_or_else(|_| std::env::temp_dir());
            let suggested = destination
                .file_name()
                .map(|n| n.to_string_lossy().to_string())
                .filter(|n| !n.is_empty())
                .or_else(|| url.path_segments().and_then(|mut s| s.next_back().map(|x| x.to_string())))
                .unwrap_or_else(|| "trading-alerts-download".into());
            *destination = unique_path(&dir, &suggested);
            true
        }
        DownloadEvent::Finished { path, success, .. } => {
            let (title, body) = match (success, path) {
                (true, Some(p)) => ("Download saved", format!("Saved to {}", p.display())),
                _ => ("Download failed", "The file couldn't be saved.".to_string()),
            };
            let _ = app.notification().builder().title(title).body(body).show();
            true
        }
        _ => true,
    }
}

fn unique_path(dir: &std::path::Path, name: &str) -> PathBuf {
    let clean: String = name.chars().map(|c| if "/\\:*?\"<>|".contains(c) { '_' } else { c }).collect();
    let first = dir.join(&clean);
    if !first.exists() {
        return first;
    }
    let (stem, ext) = match clean.rsplit_once('.') {
        Some((s, e)) => (s.to_string(), format!(".{e}")),
        None => (clean.clone(), String::new()),
    };
    (1..1000)
        .map(|i| dir.join(format!("{stem} ({i}){ext}")))
        .find(|p| !p.exists())
        .unwrap_or(first)
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn main() {
    let app = tauri::Builder::default()
        // Opening the app again just brings the window back: only one engine at a time.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Engine::default())
        .setup(|app| {
            let nav = app.handle().clone();
            let dl = app.handle().clone();
            let popup = app.handle().clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Trading Alerts")
                .inner_size(1320.0, 880.0)
                .min_inner_size(420.0, 560.0)
                .on_navigation(move |url| {
                    if is_inside(&nav, url) {
                        return true;
                    }
                    open_outside(&nav, url);
                    false
                })
                .on_new_window(move |url, _features| {
                    // target="_blank" links: the engine's own pages open in the window, the rest outside
                    if is_inside(&popup, &url) {
                        if let Some(w) = popup.get_webview_window("main") {
                            let _ = w.navigate(url);
                        }
                    } else {
                        open_outside(&popup, &url);
                    }
                    tauri::webview::NewWindowResponse::Deny
                })
                .on_download(move |_webview, event| save_download(&dl, event))
                .build()?;

            let open = MenuItem::with_id(app, "open", "Open Trading Alerts", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit (stops forward tests and alerts)", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &quit])?;
            let mut tray = TrayIconBuilder::with_id("tray")
                .tooltip("Trading Alerts")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open" => show_main(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main(tray.app_handle());
                    }
                });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;

            let handle = app.handle().clone();
            thread::spawn(move || boot(handle));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let app = window.app_handle();
                if work_running(app) {
                    api.prevent_close();
                    let _ = window.hide();
                    let state = app.state::<Engine>();
                    let mut told = state.told_about_tray.lock().unwrap();
                    if !*told {
                        *told = true;
                        let _ = app
                            .notification()
                            .builder()
                            .title("Trading Alerts is still running")
                            .body("Your forward tests and alerts keep going in the background. Quit from the tray icon to stop them.")
                            .show();
                    }
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while starting Trading Alerts");

    app.run(|app, event| {
        if let RunEvent::Exit = event {
            stop_engine(app);
        }
    });
}
