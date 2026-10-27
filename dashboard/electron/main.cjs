// Electron main process — same role as src-tauri/src/lib.rs (reused as
// documentation of intent; this is a line-for-line port, not a redesign).
// Migrated from Tauri because Tauri renders through WKWebView on macOS,
// not Chromium, and a real, repeatable measurement (Chromium: 60fps for
// the hologram alone, high-30s/low-40s with the full panel UI layered on
// top) couldn't be reproduced or fixed from inside WKWebView — Electron
// bundles the same Chromium engine that measurement came from, trading
// bundle size/RAM for a known, consistent rendering ceiling.
const { app, BrowserWindow, ipcMain } = require("electron");
const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");

const HOME = os.homedir();
const SSH_KEY_DEFAULT = path.join(HOME, ".ssh", "jarvis-oracle.key");
const CONFIG_PATH = path.join(HOME, ".jarvis-dashboard", "config.json");
const VOICE_STATE_PATH = path.join(HOME, ".jarvis-dashboard", "voice_state.json");
const ROUTING_STATE_PATH = path.join(HOME, ".jarvis-dashboard", "routing_state.json");
// Written here, polled by always_on.py's DashboardCommandWatcher — the
// Dashboard has no other way to reach that separate process (no shared
// memory, no socket today), so this is the same one-way-local-file
// pattern the OTHER direction already uses (voice_state.json/
// routing_state.json), just reversed.
const COMMAND_PATH = path.join(HOME, ".jarvis-dashboard", "command.json");
const REMOTE_JARVIS_DIR = "/home/ubuntu/JARVIS";
const REMOTE_EXTRA_PATH = "/home/ubuntu/.local/bin";

function defaultConfig() {
  return {
    ssh_host: "163.192.207.84",
    ssh_user: "ubuntu",
    ssh_key_path: SSH_KEY_DEFAULT,
    remote_results_path: "~/JARVIS/task_results.jsonl",
    max_lines: 300,
  };
}

function loadConfig() {
  try {
    const raw = fs.readFileSync(CONFIG_PATH, "utf8");
    const cfg = JSON.parse(raw);
    return { ...defaultConfig(), ...cfg };
  } catch {
    const cfg = defaultConfig();
    try {
      fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true });
      fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2));
    } catch {
      /* best-effort, same as the Rust version */
    }
    return cfg;
  }
}

// Same flags as the old Rust Command::new("ssh") calls — execFile, not
// exec, so the remote command string is never interpreted by a local
// shell (only the remote sshd's shell sees it, same trust boundary as
// before).
function runSsh(cfg, remoteCmd, timeoutMs, connectTimeoutS) {
  return new Promise((resolve) => {
    execFile(
      "ssh",
      [
        "-i", cfg.ssh_key_path,
        "-o", `ConnectTimeout=${connectTimeoutS}`,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        `${cfg.ssh_user}@${cfg.ssh_host}`,
        remoteCmd,
      ],
      { timeout: timeoutMs, maxBuffer: 10 * 1024 * 1024 },
      (error, stdout, stderr) => {
        if (error) {
          resolve({ ok: false, error: stderr?.trim() || error.message, stdout: "" });
        } else {
          resolve({ ok: true, error: null, stdout });
        }
      }
    );
  });
}

function readJsonSafe(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

async function fetchTasks() {
  const cfg = loadConfig();
  const remoteCmd = `tail -n ${cfg.max_lines} ${cfg.remote_results_path}`;
  const res = await runSsh(cfg, remoteCmd, 10000, 6);
  if (!res.ok) return { ok: false, error: res.error || "SSH command failed with no error output", tasks: [] };
  const tasks = [];
  for (const line of res.stdout.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      tasks.push(JSON.parse(trimmed));
    } catch {
      /* skip unparsable lines, same as the Rust version */
    }
  }
  tasks.reverse();
  return { ok: true, error: null, tasks };
}

async function fetchServiceStatus() {
  const cfg = loadConfig();
  const res = await runSsh(cfg, "systemctl is-active jarvis-orchestrator", 10000, 6);
  if (!res.ok) return { ok: false, active: "unknown", error: res.error };
  return { ok: true, active: res.stdout.trim(), error: null };
}

// Reported live: always_on.py got killed mid-turn (crash, Ctrl+C) while
// voice_state.json said "thinking", and with no process left to ever
// write "idle" back, the Dashboard showed "THINKING / routing to the
// brain..." frozen indefinitely — looked exactly like a hung app, when
// the real problem was simpler (always_on.py wasn't running at all).
// Only non-idle states can go stale this way: idle is the quiescent
// state while waiting for a wake trigger, and legitimately persists for
// minutes/hours with no rewrite, so it's never treated as stale. Every
// other state (listening/thinking/speaking) is inherently transient —
// a real turn resolves within seconds — so one sitting unchanged past
// VOICE_STATE_STALE_S means the writer is gone, not still working.
const VOICE_STATE_STALE_S = 20;

function fetchVoiceState() {
  const parsed = readJsonSafe(VOICE_STATE_PATH);
  const state = parsed?.state || "idle";
  const updatedAt = parsed?.updated_at || 0;
  const ageS = Date.now() / 1000 - updatedAt;
  if (state !== "idle" && ageS > VOICE_STATE_STALE_S) {
    return { state: "idle", amplitude: 0, updated_at: updatedAt, stale: true };
  }
  return {
    state,
    amplitude: parsed?.amplitude || 0,
    updated_at: updatedAt,
  };
}

async function fetchBackgroundTasks() {
  const cfg = loadConfig();
  const remoteCmd =
    `export PATH="${REMOTE_EXTRA_PATH}:$PATH"; python3 -c ` +
    `"import sys; sys.path.insert(0, '${REMOTE_JARVIS_DIR}'); import jarvis_respond as jr; print(jr.list_background_tasks())"`;
  const res = await runSsh(cfg, remoteCmd, 15000, 8);
  if (!res.ok) return { ok: false, error: res.error || "SSH command failed with no error output", tasks: [] };
  try {
    const tasks = JSON.parse(res.stdout.trim());
    return { ok: true, error: null, tasks };
  } catch (e) {
    return { ok: false, error: `Couldn't parse task data: ${e.message}`, tasks: [] };
  }
}

async function fetchProjectsLog() {
  const cfg = loadConfig();
  const res = await runSsh(cfg, "cat ~/.jarvis_projects_log.md 2>/dev/null", 10000, 8);
  if (!res.ok) return { ok: false, error: res.error, content: "" };
  return { ok: true, error: null, content: res.stdout };
}

async function fetchRoutingState() {
  const cfg = loadConfig();
  const local = readJsonSafe(ROUTING_STATE_PATH) || {};
  const res = await runSsh(cfg, "cat ~/jarvis-workspace/route_state.json 2>/dev/null", 10000, 6);
  const remote = res.ok ? readJsonSafeFromString(res.stdout) || {} : {};

  const localTs = local.timestamp || 0;
  const remoteTs = remote.timestamp || 0;

  if (localTs > remoteTs) {
    return {
      tier: local.tier || "",
      label: local.label || "",
      detail: local.detail || "",
      status: local.status || "",
      timestamp: localTs,
      source: "local-mac",
    };
  }
  if (remoteTs > 0) {
    return {
      tier: remote.route || "",
      label: remote.label || "",
      detail: remote.preview || "",
      status: remote.status || "",
      timestamp: remoteTs,
      source: "server",
    };
  }
  return {
    tier: "unknown",
    label: "No routing activity yet",
    detail: "",
    status: "idle",
    timestamp: 0,
    source: "none",
  };
}

function readJsonSafeFromString(s) {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

// "The app should activate jarvis" plus Interrupt / Keep Listening —
// always_on.py's DashboardCommandWatcher polls this file for a new id
// (crypto.randomUUID() each click, so a repeated click of the same
// button is never mistaken for a stale, already-handled one) and acts on
// `command` accordingly. Fire-and-forget: there's no reply channel, same
// as the Mac's own clap/spacebar/trackpad-tap triggers, which also don't
// get an ack beyond Jarvis actually speaking.
function sendDashboardCommand(command) {
  const payload = { command, id: require("crypto").randomUUID(), ts: Date.now() / 1000 };
  fs.mkdirSync(path.dirname(COMMAND_PATH), { recursive: true });
  fs.writeFileSync(COMMAND_PATH, JSON.stringify(payload));
  return { ok: true };
}

let mainWindow = null;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 720,
    minHeight: 480,
    title: "J.A.R.V.I.S. — Status",
    backgroundColor: "#04070a",
    fullscreen: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
}

// Neither of these existed before — a renderer crash (OOM, a GPU driver
// issue, Three.js/the hologram hitting a bad WebGL state) left a
// silently dead window with no log line and no attempt to recover; a
// bug in this main process itself (not the renderer) just vanished into
// Electron's default handling with no trace of why. Both are real gaps
// for "the Electron app shouldn't have bugs at all" — this doesn't
// prevent a crash, but it means one is never silent or unrecoverable
// again.
app.on("render-process-gone", (_event, win, details) => {
  console.error(`[main] renderer process gone: reason=${details.reason} exitCode=${details.exitCode}`);
  if (details.reason === "clean-exit") return; // normal quit, not a crash
  if (win && !win.isDestroyed()) {
    win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
});

process.on("uncaughtException", (err) => {
  console.error("[main] uncaught exception in the main process:", err);
});

ipcMain.handle("invoke", async (_event, cmd) => {
  switch (cmd) {
    case "fetch_tasks": return fetchTasks();
    case "fetch_service_status": return fetchServiceStatus();
    case "fetch_voice_state": return fetchVoiceState();
    case "fetch_background_tasks": return fetchBackgroundTasks();
    case "fetch_projects_log": return fetchProjectsLog();
    case "fetch_routing_state": return fetchRoutingState();
    case "get_config": return loadConfig();
    default: throw new Error(`Unknown command: ${cmd}`);
  }
});

ipcMain.handle("send_dashboard_command", (_event, command) => sendDashboardCommand(command));

ipcMain.handle("set_fullscreen", (_event, value) => {
  mainWindow?.setFullScreen(!!value);
});
ipcMain.handle("set_decorations", (_event, value) => {
  // macOS doesn't support toggling the title bar on a live window the
  // way Tauri's setDecorations did cross-platform — fullscreen already
  // hides it visually, which is the actual kiosk behavior that mattered.
});

// Opt-in only (never on by default — a remote debugging port is a real
// attack surface) — set JARVIS_DASHBOARD_DEBUG_PORT to inspect the
// renderer via chrome://inspect or a raw CDP connection.
if (process.env.JARVIS_DASHBOARD_DEBUG_PORT) {
  app.commandLine.appendSwitch("remote-debugging-port", process.env.JARVIS_DASHBOARD_DEBUG_PORT);
}

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
