mod cpu;

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// The chat model is NOT bundled: the backend downloads it once (pinned URL,
/// SHA-256 + size + GGUF checks) into <app data>/models/. Must match
/// DEFAULT_MODEL.filename in python-backend/model_download.py.
const MODEL_FILE: &str = "qwen2.5-1.5b-instruct-q4_k_m.gguf";
/// llama-server may start after the backend (post-download), so its PID goes
/// in a file the backend's watchdog reads if the app dies without cleanup.
const LLAMA_PID_FILE: &str = "llama-server.pid";
const LLAMA_CTX: &str = "4096";

/// Handles to the sidecars, so we can stop them when the app exits.
struct Sidecars(Mutex<Vec<(String, CommandChild)>>);

struct Paths {
    data_dir: PathBuf,
    model: PathBuf,
    pid_file: PathBuf,
}

/// What the UI shows about the local model. Serialized for `llm_status`.
#[derive(Clone, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
enum LlmStatus {
    Starting,
    /// No verified model on disk yet; the UI offers the one-time download.
    NeedsModel,
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

/// Start llama-server once the model has been downloaded. Idempotent.
#[tauri::command]
fn start_llm(app: AppHandle) -> Result<LlmStatus, String> {
    let current = app.state::<Llm>().0.lock().unwrap().clone();
    match current {
        LlmStatus::UnsupportedCpu { .. } => return Ok(current),
        LlmStatus::Starting if llama_running(&app) => return Ok(current),
        _ => {}
    }
    if !app.state::<Paths>().model.exists() {
        return Err("The model has not been downloaded yet.".into());
    }
    spawn_llama(&app).map_err(|e| format!("Could not start the AI model: {e}"))?;
    Ok(app.state::<Llm>().0.lock().unwrap().clone())
}

fn llama_running(app: &AppHandle) -> bool {
    let state = app.state::<Sidecars>();
    let children = state.0.lock().unwrap();
    children.iter().any(|(n, _c)| {
        n == "llama-server" && {
            #[cfg(unix)]
            {
                pid_alive(_c.pid())
            }
            #[cfg(not(unix))]
            {
                true
            }
        }
    })
}

fn spawn_llama(app: &AppHandle) -> Result<u32, Box<dyn std::error::Error>> {
    let paths = app.state::<Paths>();
    let model = paths.model.to_str().ok_or("model path is not UTF-8")?;
    let (rx, child) = app
        .shell()
        .sidecar("aidesk-llama-server")?
        .args(["--model", model, "--host", "127.0.0.1", "--port", "8080", "--ctx-size", LLAMA_CTX])
        .spawn()?;
    drain_output(app.clone(), "llama-server", rx);
    let pid = child.pid();
    if let Err(e) = std::fs::write(&paths.pid_file, pid.to_string()) {
        log::warn!("could not write {}: {e}", paths.pid_file.display());
    }
    {
        let state = app.state::<Sidecars>();
        let mut s = state.0.lock().unwrap();
        s.retain(|(n, _)| n != "llama-server"); // drop a dead previous instance
        s.push(("llama-server".into(), child));
    }
    *app.state::<Llm>().0.lock().unwrap() = LlmStatus::Starting;
    Ok(pid)
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
        .invoke_handler(tauri::generate_handler![llm_status, start_llm])
        .setup(|app| {
            #[cfg(unix)]
            install_signal_handler(app.handle().clone());

            let data_dir = app.path().app_data_dir()?;
            std::fs::create_dir_all(data_dir.join("models"))?;
            let paths = Paths {
                model: data_dir.join("models").join(MODEL_FILE),
                pid_file: data_dir.join(LLAMA_PID_FILE),
                data_dir,
            };
            let _ = std::fs::remove_file(&paths.pid_file); // stale from a crash
            app.manage(paths);

            // llama-server first, so the backend can be told its PID. Only if
            // the CPU has every instruction-set extension it was built with;
            // otherwise it would die of SIGILL. The backend runs either way.
            // Without a downloaded model it is started later via start_llm.
            let missing = cpu::missing_features();
            let mut llama_pid = None;
            if !missing.is_empty() {
                let message = cpu::unsupported_message(&missing);
                log::error!("unsupported CPU: missing {}; not starting llama-server. {message}", missing.join(","));
                *app.state::<Llm>().0.lock().unwrap() = LlmStatus::UnsupportedCpu { missing, message };
            } else if app.state::<Paths>().model.exists() {
                llama_pid = Some(spawn_llama(app.handle())?);
            } else {
                log::info!("model not downloaded yet: {}", app.state::<Paths>().model.display());
                *app.state::<Llm>().0.lock().unwrap() = LlmStatus::NeedsModel;
            }

            // The backend watches our PID and exits (taking llama-server with it)
            // if we die without running stop_sidecars, e.g. on a crash or SIGKILL.
            let paths = app.state::<Paths>();
            let mut backend_args = vec![
                "--parent-pid".to_string(), std::process::id().to_string(),
                "--kill-pid-file".to_string(), paths.pid_file.to_string_lossy().into_owned(),
                "--data-dir".to_string(), paths.data_dir.to_string_lossy().into_owned(),
                "--model-path".to_string(), paths.model.to_string_lossy().into_owned(),
            ];
            if let Some(pid) = llama_pid {
                backend_args.extend(["--kill-on-exit".to_string(), pid.to_string()]);
            }
            let (backend_rx, backend) = app
                .shell()
                .sidecar("aidesk-backend")?
                .args(backend_args)
                .spawn()?;
            drain_output(app.handle().clone(), "backend", backend_rx);

            app.state::<Sidecars>().0.lock().unwrap().push(("backend".into(), backend));
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
