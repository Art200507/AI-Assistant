use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Config {
    ssh_host: String,
    ssh_user: String,
    ssh_key_path: String,
    remote_results_path: String,
    #[serde(default = "default_max_lines")]
    max_lines: usize,
}

fn default_max_lines() -> usize {
    300
}

impl Default for Config {
    fn default() -> Self {
        let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
        Config {
            ssh_host: "163.192.207.84".into(),
            ssh_user: "ubuntu".into(),
            ssh_key_path: format!("{home}/.ssh/jarvis-oracle.key"),
            remote_results_path: "~/JARVIS/task_results.jsonl".into(),
            max_lines: default_max_lines(),
        }
    }
}

fn config_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    PathBuf::from(format!("{home}/.jarvis-dashboard/config.json"))
}

fn load_config() -> Config {
    let path = config_path();
    if let Ok(contents) = fs::read_to_string(&path) {
        if let Ok(cfg) = serde_json::from_str::<Config>(&contents) {
            return cfg;
        }
    }
    let cfg = Config::default();
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(json) = serde_json::to_string_pretty(&cfg) {
        let _ = fs::write(&path, json);
    }
    cfg
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct TaskResult {
    task: String,
    instruction: String,
    status: String,
    summary: String,
    #[serde(default)]
    worker: Option<String>,
    timestamp: String,
}

#[derive(Debug, Clone, Serialize)]
struct FetchResponse {
    ok: bool,
    error: Option<String>,
    tasks: Vec<TaskResult>,
    fetched_at: String,
}

#[tauri::command]
fn get_config() -> Config {
    load_config()
}

#[tauri::command]
fn fetch_tasks() -> FetchResponse {
    let cfg = load_config();
    let now = unix_now();

    let remote_cmd = format!("tail -n {} {}", cfg.max_lines, cfg.remote_results_path);

    let output = Command::new("ssh")
        .arg("-i")
        .arg(&cfg.ssh_key_path)
        .arg("-o")
        .arg("ConnectTimeout=6")
        .arg("-o")
        .arg("BatchMode=yes")
        .arg("-o")
        .arg("StrictHostKeyChecking=accept-new")
        .arg(format!("{}@{}", cfg.ssh_user, cfg.ssh_host))
        .arg(remote_cmd)
        .output();

    match output {
        Ok(out) if out.status.success() => {
            let stdout = String::from_utf8_lossy(&out.stdout);
            let mut tasks: Vec<TaskResult> = Vec::new();
            for line in stdout.lines() {
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                match serde_json::from_str::<TaskResult>(line) {
                    Ok(t) => tasks.push(t),
                    Err(_) => continue,
                }
            }
            tasks.reverse(); // newest first
            FetchResponse {
                ok: true,
                error: None,
                tasks,
                fetched_at: now,
            }
        }
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            FetchResponse {
                ok: false,
                error: Some(if stderr.trim().is_empty() {
                    "SSH command failed with no error output".into()
                } else {
                    stderr
                }),
                tasks: vec![],
                fetched_at: now,
            }
        }
        Err(e) => FetchResponse {
            ok: false,
            error: Some(format!("Failed to run ssh: {e}")),
            tasks: vec![],
            fetched_at: now,
        },
    }
}

#[derive(Debug, Clone, Serialize)]
struct ServiceStatus {
    ok: bool,
    active: String,
    error: Option<String>,
}

#[tauri::command]
fn fetch_service_status() -> ServiceStatus {
    let cfg = load_config();
    let output = Command::new("ssh")
        .arg("-i")
        .arg(&cfg.ssh_key_path)
        .arg("-o")
        .arg("ConnectTimeout=6")
        .arg("-o")
        .arg("BatchMode=yes")
        .arg("-o")
        .arg("StrictHostKeyChecking=accept-new")
        .arg(format!("{}@{}", cfg.ssh_user, cfg.ssh_host))
        .arg("systemctl is-active jarvis-orchestrator")
        .output();

    match output {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
            ServiceStatus {
                ok: true,
                active: stdout,
                error: None,
            }
        }
        Err(e) => ServiceStatus {
            ok: false,
            active: "unknown".into(),
            error: Some(format!("{e}")),
        },
    }
}

fn unix_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let dur = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::from_secs(0));
    dur.as_secs().to_string()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            fetch_tasks,
            fetch_service_status,
            get_config
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
