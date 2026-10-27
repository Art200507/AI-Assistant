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

#[derive(Debug, Clone, Serialize, Deserialize)]
struct VoiceState {
    #[serde(default = "default_voice_state_label")]
    state: String,
    #[serde(default)]
    amplitude: f32,
    #[serde(default)]
    updated_at: f64,
}

fn default_voice_state_label() -> String {
    "idle".into()
}

impl Default for VoiceState {
    fn default() -> Self {
        VoiceState {
            state: default_voice_state_label(),
            amplitude: 0.0,
            updated_at: 0.0,
        }
    }
}

fn voice_state_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    PathBuf::from(format!("{home}/.jarvis-dashboard/voice_state.json"))
}

#[tauri::command]
fn fetch_voice_state() -> VoiceState {
    let path = voice_state_path();
    match fs::read_to_string(&path) {
        Ok(contents) => serde_json::from_str(&contents).unwrap_or_default(),
        Err(_) => VoiceState::default(),
    }
}

// --- Real background tasks (the conversational "Claude, build me X" task
// system — jarvis_respond.py's start_background_task/TASKS_DIR), NOT the
// orchestrator's task_results.jsonl above, which is a separate, unrelated
// system that stays empty in normal use. This is what actually answers
// "why does the app show no tasks" — the old panel was pointed at the
// wrong data source entirely.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct BgTask {
    task_id: String,
    #[serde(default)]
    user_input: String,
    #[serde(default)]
    session_id: Option<String>,
    #[serde(default)]
    started_at: Option<f64>,
    #[serde(default)]
    status: String,
    #[serde(default)]
    exit_code: Option<i64>,
}

#[derive(Debug, Clone, Serialize)]
struct BgTasksResponse {
    ok: bool,
    error: Option<String>,
    tasks: Vec<BgTask>,
}

const REMOTE_JARVIS_DIR: &str = "/home/ubuntu/JARVIS";
const REMOTE_EXTRA_PATH: &str = "/home/ubuntu/.local/bin";

#[tauri::command]
fn fetch_background_tasks() -> BgTasksResponse {
    let cfg = load_config();
    let remote_cmd = format!(
        "export PATH=\"{REMOTE_EXTRA_PATH}:$PATH\"; python3 -c \"import sys; sys.path.insert(0, '{REMOTE_JARVIS_DIR}'); import jarvis_respond as jr; print(jr.list_background_tasks())\""
    );

    let output = Command::new("ssh")
        .arg("-i")
        .arg(&cfg.ssh_key_path)
        .arg("-o")
        .arg("ConnectTimeout=8")
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
            match serde_json::from_str::<Vec<BgTask>>(stdout.trim()) {
                Ok(tasks) => BgTasksResponse {
                    ok: true,
                    error: None,
                    tasks,
                },
                Err(e) => BgTasksResponse {
                    ok: false,
                    error: Some(format!("Couldn't parse task data: {e}")),
                    tasks: vec![],
                },
            }
        }
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            BgTasksResponse {
                ok: false,
                error: Some(if stderr.trim().is_empty() {
                    "SSH command failed with no error output".into()
                } else {
                    stderr
                }),
                tasks: vec![],
            }
        }
        Err(e) => BgTasksResponse {
            ok: false,
            error: Some(format!("Failed to run ssh: {e}")),
            tasks: vec![],
        },
    }
}

// --- Permanent cross-session project record (~/.jarvis_projects_log.md on
// the server — see jarvis_respond.py's PROJECTS_LOG_FILE). Session
// transcripts reset every always_on.py restart; this file is the one
// durable record of what's actually been built across all of them.
#[derive(Debug, Clone, Serialize)]
struct ProjectsLogResponse {
    ok: bool,
    error: Option<String>,
    content: String,
}

#[tauri::command]
fn fetch_projects_log() -> ProjectsLogResponse {
    let cfg = load_config();
    let output = Command::new("ssh")
        .arg("-i")
        .arg(&cfg.ssh_key_path)
        .arg("-o")
        .arg("ConnectTimeout=8")
        .arg("-o")
        .arg("BatchMode=yes")
        .arg("-o")
        .arg("StrictHostKeyChecking=accept-new")
        .arg(format!("{}@{}", cfg.ssh_user, cfg.ssh_host))
        .arg("cat ~/.jarvis_projects_log.md 2>/dev/null")
        .output();

    match output {
        Ok(out) if out.status.success() => ProjectsLogResponse {
            ok: true,
            error: None,
            content: String::from_utf8_lossy(&out.stdout).to_string(),
        },
        Ok(out) => ProjectsLogResponse {
            ok: false,
            error: Some(String::from_utf8_lossy(&out.stderr).to_string()),
            content: String::new(),
        },
        Err(e) => ProjectsLogResponse {
            ok: false,
            error: Some(format!("Failed to run ssh: {e}")),
            content: String::new(),
        },
    }
}

// --- Live routing "bird's eye view": which tier handled the most recent
// turn (local / fast-Groq / cloud-Claude / lookup). Two independent
// sources because "local" mode never reaches the server at all:
//   - server writes ~/jarvis-workspace/route_state.json on every call to
//     respond() (covers lookup/simple/reasoning — see
//     jarvis_respond.py's _write_route_state)
//   - this Mac writes ~/.jarvis-dashboard/routing_state.json only when
//     routing to local Claude (see routing_state.py / always_on.py)
// Whichever file has the newer timestamp is the current route — a real
// merge of two genuinely separate event streams, not a guess.
#[derive(Debug, Clone, Deserialize, Default)]
struct RemoteRouteRaw {
    #[serde(default)]
    route: String,
    #[serde(default)]
    label: String,
    #[serde(default)]
    preview: String,
    #[serde(default)]
    status: String,
    #[serde(default)]
    timestamp: f64,
}

#[derive(Debug, Clone, Deserialize, Default)]
struct LocalRoutingRaw {
    #[serde(default)]
    tier: String,
    #[serde(default)]
    label: String,
    #[serde(default)]
    detail: String,
    #[serde(default)]
    status: String,
    #[serde(default)]
    timestamp: f64,
}

#[derive(Debug, Clone, Serialize, Default)]
struct RoutingState {
    tier: String,
    label: String,
    detail: String,
    status: String,
    timestamp: f64,
    source: String,
}

fn routing_state_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    PathBuf::from(format!("{home}/.jarvis-dashboard/routing_state.json"))
}

#[tauri::command]
fn fetch_routing_state() -> RoutingState {
    let cfg = load_config();

    let local_raw: LocalRoutingRaw = fs::read_to_string(routing_state_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();

    let remote_raw: RemoteRouteRaw = Command::new("ssh")
        .arg("-i")
        .arg(&cfg.ssh_key_path)
        .arg("-o")
        .arg("ConnectTimeout=6")
        .arg("-o")
        .arg("BatchMode=yes")
        .arg("-o")
        .arg("StrictHostKeyChecking=accept-new")
        .arg(format!("{}@{}", cfg.ssh_user, cfg.ssh_host))
        .arg("cat ~/jarvis-workspace/route_state.json 2>/dev/null")
        .output()
        .ok()
        .filter(|o| o.status.success())
        .and_then(|o| serde_json::from_slice(&o.stdout).ok())
        .unwrap_or_default();

    if local_raw.timestamp > remote_raw.timestamp {
        RoutingState {
            tier: local_raw.tier,
            label: local_raw.label,
            detail: local_raw.detail,
            status: local_raw.status,
            timestamp: local_raw.timestamp,
            source: "local-mac".into(),
        }
    } else if remote_raw.timestamp > 0.0 {
        RoutingState {
            tier: remote_raw.route,
            label: remote_raw.label,
            detail: remote_raw.preview,
            status: remote_raw.status,
            timestamp: remote_raw.timestamp,
            source: "server".into(),
        }
    } else {
        RoutingState {
            tier: "unknown".into(),
            label: "No routing activity yet".into(),
            detail: String::new(),
            status: "idle".into(),
            timestamp: 0.0,
            source: "none".into(),
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            fetch_tasks,
            fetch_service_status,
            fetch_voice_state,
            fetch_background_tasks,
            fetch_projects_log,
            fetch_routing_state,
            get_config
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
