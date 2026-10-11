mod cpu;

use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Handles to the sidecars, so we can stop them when the app exits.
struct Sidecars(Mutex<Vec<(String, CommandChild)>>);

/// What the UI shows about the local model. Serialized for `llm_status`.
#[derive(Clone, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
enum LlmStatus {
    Starting,
    /// CPU check failed; llama-server was never started.
    UnsupportedCpu { missing: Vec<String>, message: String },
    /// llama-server exited on its own.
    Exited { message: String },
}
struct Llm(Mutex<LlmStatus>);

#[tauri::command]
fn llm_status(llm: tauri::State<'_, Llm>) -> LlmStatus {
    llm.0.lock().unwrap().clone()
}

/// Forward a sidecar's stdout/stderr to the app log, and note when it exits.
fn drain_output(app: AppHandle, name: &'static str, mut rx: tauri::async_runtime::Receiver<CommandEvent>) {
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                    log::info!(target: "sidecar", "[{name}] {}", String::from_utf8_lossy(&line).trim_end());
                }
                CommandEvent::Terminated(p) => {
                    log::warn!(target: "sidecar", "[{name}] exited code={:?} signal={:?}", p.code, p.signal);
                    if name == "llama-server" {
                        // Second line of defense behind the CPU check: name
                        // SIGILL explicitly instead of a generic crash.
                        let message = if p.signal == Some(libc_sigill()) {
                            log::error!("llama-server died of SIGILL: the CPU lacks an instruction it was built with");
                            "The built-in AI model stopped: this processor lacks an instruction it needs (illegal instruction).".to_string()
                        } else {
                            format!("The built-in AI model stopped (code {:?}, signal {:?}). See the app log.", p.code, p.signal)
                        };
                        *app.state::<Llm>().0.lock().unwrap() = LlmStatus::Exited { message };
                    }
                }
                CommandEvent::Error(e) => log::error!(target: "sidecar", "[{name}] error: {e}"),
                _ => {}
            }
        }
    });
}

fn libc_sigill() -> i32 {
    #[cfg(unix)]
    return libc::SIGILL;
    #[cfg(not(unix))]
    return 4;
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
        .manage(Llm(Mutex::new(LlmStatus::Starting)))
        .invoke_handler(tauri::generate_handler![llm_status])
        .setup(|app| {
            #[cfg(unix)]
            install_signal_handler(app.handle().clone());

            let model_path = app.path().resource_dir()?.join("tiny-model.gguf");

            // llama-server first, so the backend can be told its PID. Only if
            // the CPU has every instruction-set extension it was built with;
            // otherwise it would die of SIGILL. The backend runs either way.
            let missing = cpu::missing_features();
            let mut llama = None;
            if missing.is_empty() {
                let (llama_rx, child) = app
                    .shell()
                    .sidecar("aidesk-llama-server")?
                    .args(["--model", model_path.to_str().unwrap(), "--host", "127.0.0.1", "--port", "8080"])
                    .spawn()?;
                drain_output(app.handle().clone(), "llama-server", llama_rx);
                llama = Some(child);
            } else {
                let message = cpu::unsupported_message(&missing);
                log::error!("unsupported CPU: missing {}; not starting llama-server. {message}", missing.join(","));
                *app.state::<Llm>().0.lock().unwrap() = LlmStatus::UnsupportedCpu { missing, message };
            }

            // The backend watches our PID and exits (taking llama-server with it)
            // if we die without running stop_sidecars, e.g. on a crash or SIGKILL.
            let mut backend_args = vec!["--parent-pid".to_string(), std::process::id().to_string()];
            if let Some(l) = &llama {
                backend_args.extend(["--kill-on-exit".to_string(), l.pid().to_string()]);
            }
            let (backend_rx, backend) = app
                .shell()
                .sidecar("aidesk-backend")?
                .args(backend_args)
                .spawn()?;
            drain_output(app.handle().clone(), "backend", backend_rx);

            let state = app.state::<Sidecars>();
            let mut s = state.0.lock().unwrap();
            s.push(("backend".into(), backend));
            if let Some(l) = llama {
                s.push(("llama-server".into(), l));
            }
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
