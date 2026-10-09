use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Handles to the sidecars, so we can stop them when the app exits.
struct Sidecars(Mutex<Vec<(String, CommandChild)>>);

/// Forward a sidecar's stdout/stderr to the app log, and note when it exits.
fn drain_output(name: &'static str, mut rx: tauri::async_runtime::Receiver<CommandEvent>) {
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                    log::info!(target: "sidecar", "[{name}] {}", String::from_utf8_lossy(&line).trim_end());
                }
                CommandEvent::Terminated(p) => {
                    log::warn!(target: "sidecar", "[{name}] exited code={:?} signal={:?}", p.code, p.signal);
                }
                CommandEvent::Error(e) => log::error!(target: "sidecar", "[{name}] error: {e}"),
                _ => {}
            }
        }
    });
}

#[cfg(unix)]
fn pid_alive(pid: u32) -> bool {
    // Signal 0 = existence check. The shell plugin reaps children, so an exited
    // child stops matching shortly after it dies.
    unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
}

/// Graceful stop: SIGTERM all sidecars, wait up to 3 s, then SIGKILL stragglers.
/// SIGTERM matters for the backend: PyInstaller's one-file bootloader forwards
/// it to the Python child, whereas SIGKILL to the bootloader would orphan it.
fn stop_sidecars(app: &AppHandle) {
    let Some(state) = app.try_state::<Sidecars>() else { return };
    let mut children = state.0.lock().unwrap();
    if children.is_empty() {
        return;
    }
    #[cfg(unix)]
    {
        for (name, c) in children.iter() {
            log::info!("stopping sidecar {name} (pid {}) with SIGTERM", c.pid());
            unsafe { libc::kill(c.pid() as libc::pid_t, libc::SIGTERM) };
        }
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline && children.iter().any(|(_, c)| pid_alive(c.pid())) {
            std::thread::sleep(Duration::from_millis(50));
        }
    }
    for (name, c) in children.drain(..) {
        #[cfg(unix)]
        if !pid_alive(c.pid()) {
            continue;
        }
        log::warn!("sidecar {name} did not exit in time; killing");
        let _ = c.kill();
    }
}

/// Turn SIGTERM/SIGINT/SIGHUP (logout, `kill`, Ctrl-C) into a normal app exit,
/// so RunEvent::Exit runs and the sidecars are stopped. Without this, GTK
/// dies on SIGTERM immediately and the cleanup never runs.
#[cfg(unix)]
fn install_signal_handler(app: AppHandle) {
    use signal_hook::consts::{SIGHUP, SIGINT, SIGTERM};
    let mut signals = signal_hook::iterator::Signals::new([SIGTERM, SIGINT, SIGHUP])
        .expect("signal handler");
    std::thread::spawn(move || {
        if let Some(sig) = signals.forever().next() {
            log::info!("received signal {sig}; exiting");
            app.exit(0);
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // Always on (not just debug): writes to the platform log dir, e.g.
        // ~/.local/share/<identifier>/logs on Linux. Needed to debug field issues.
        .plugin(
            tauri_plugin_log::Builder::default()
                .level(log::LevelFilter::Info)
                .build(),
        )
        .manage(Sidecars(Mutex::new(Vec::new())))
        .setup(|app| {
            #[cfg(unix)]
            install_signal_handler(app.handle().clone());

            let model_path = app.path().resource_dir()?.join("tiny-model.gguf");

            // llama-server first, so the backend can be told its PID.
            let (llama_rx, llama) = app
                .shell()
                .sidecar("aidesk-llama-server")?
                .args(["--model", model_path.to_str().unwrap(), "--host", "127.0.0.1", "--port", "8080"])
                .spawn()?;
            let llama_pid = llama.pid();
            drain_output("llama-server", llama_rx);

            // The backend watches our PID and exits (taking llama-server with it)
            // if we die without running stop_sidecars, e.g. on a crash or SIGKILL.
            let (backend_rx, backend) = app
                .shell()
                .sidecar("aidesk-backend")?
                .args([
                    "--parent-pid".to_string(), std::process::id().to_string(),
                    "--kill-on-exit".to_string(), llama_pid.to_string(),
                ])
                .spawn()?;
            drain_output("backend", backend_rx);

            let state = app.state::<Sidecars>();
            let mut s = state.0.lock().unwrap();
            s.push(("backend".into(), backend));
            s.push(("llama-server".into(), llama));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            stop_sidecars(handle);
        }
    });
}
