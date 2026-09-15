const { invoke } = window.__TAURI__.core;
const { getCurrentWindow } = window.__TAURI__.window;

const REFRESH_MS = 5000;

const el = (id) => document.getElementById(id);

const STATUS_ORDER = ["done", "skipped", "failed", "needs_approval"];
const STATUS_COLORS = {
  done: "var(--green)",
  skipped: "var(--amber)",
  failed: "var(--red)",
  needs_approval: "var(--orange)",
};

function fmtTime(iso) {
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso ?? "—";
    return d.toLocaleString(undefined, {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso ?? "—";
  }
}

function updateClock() {
  el("clock").textContent = new Date().toLocaleTimeString(undefined, { hour12: false });
}

function setConnection(ok) {
  const conn = el("conn");
  conn.classList.toggle("ok", ok);
  conn.classList.toggle("bad", !ok);
  el("conn-label").textContent = ok ? "LINK ESTABLISHED" : "LINK DOWN";
}

function showError(msg) {
  const banner = el("error-banner");
  if (!msg) {
    banner.classList.remove("show");
    banner.textContent = "";
    return;
  }
  banner.textContent = msg;
  banner.classList.add("show");
}

function renderRing(counts, total) {
  const R = 95;
  const CIRC = 2 * Math.PI * R;
  let offset = 0;
  for (const key of STATUS_ORDER) {
    const segEl = el(`seg-${key === "needs_approval" ? "approval" : key}`);
    const n = counts[key] || 0;
    const frac = total > 0 ? n / total : 0;
    const len = frac * CIRC;
    segEl.style.strokeDasharray = `${len} ${CIRC - len}`;
    segEl.style.strokeDashoffset = -offset;
    offset += len;
  }
  el("ring-total").textContent = total;
}

function renderStats(counts) {
  el("count-done").textContent = counts.done || 0;
  el("count-skipped").textContent = counts.skipped || 0;
  el("count-failed").textContent = counts.failed || 0;
  el("count-approval").textContent = counts.needs_approval || 0;
}

function renderWorkers(tasks) {
  const counts = {};
  for (const t of tasks) {
    const w = t.worker || "unknown";
    counts[w] = (counts[w] || 0) + 1;
  }
  const max = Math.max(1, ...Object.values(counts));
  const wrap = el("worker-bars");
  wrap.innerHTML = "";
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) {
    wrap.innerHTML = '<div class="stat-label">no data yet</div>';
    return;
  }
  for (const [name, count] of entries) {
    const row = document.createElement("div");
    row.className = "worker-bar-row";
    row.innerHTML = `
      <span class="worker-bar-name">${name}</span>
      <span class="worker-bar-track"><span class="worker-bar-fill" style="width:${(count / max) * 100}%"></span></span>
      <span class="worker-bar-count">${count}</span>
    `;
    wrap.appendChild(row);
  }
}

function renderTable(tasks) {
  const body = el("log-body");
  if (!tasks.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="5">No task history yet</td></tr>';
    return;
  }
  body.innerHTML = tasks
    .slice(0, 200)
    .map((t) => {
      const status = t.status || "unknown";
      const worker = t.worker || "—";
      const summary = (t.summary || "").replace(/</g, "&lt;").slice(0, 220);
      const taskName = (t.task || "").replace(/</g, "&lt;");
      return `
        <tr>
          <td class="col-time">${fmtTime(t.timestamp)}</td>
          <td class="col-status"><span class="badge ${status}">${status.replace("_", " ")}</span></td>
          <td class="col-worker"><span class="worker-tag ${worker}">${worker}</span></td>
          <td class="col-task">${taskName}</td>
          <td class="col-summary">${summary}</td>
        </tr>
      `;
    })
    .join("");
}

async function refresh() {
  try {
    const resp = await invoke("fetch_tasks");
    if (!resp.ok) {
      setConnection(false);
      showError(resp.error || "Failed to reach jarvis-oracle server over SSH");
      return;
    }
    setConnection(true);
    showError(null);

    const counts = { done: 0, skipped: 0, failed: 0, needs_approval: 0 };
    for (const t of resp.tasks) {
      if (counts[t.status] !== undefined) counts[t.status]++;
    }
    const total = resp.tasks.length;

    renderRing(counts, total);
    renderStats(counts);
    renderWorkers(resp.tasks);
    renderTable(resp.tasks);

    el("last-sync").textContent = `synced ${new Date().toLocaleTimeString(undefined, { hour12: false })}`;
  } catch (e) {
    setConnection(false);
    showError(`Unexpected error: ${e}`);
  }

  try {
    const svc = await invoke("fetch_service_status");
    const val = el("svc-value");
    val.textContent = (svc.active || "unknown").toUpperCase();
    val.classList.toggle("bad", svc.active !== "active");
  } catch {
    /* non-fatal */
  }
}

let kiosk = false;
async function toggleKiosk() {
  kiosk = !kiosk;
  const win = getCurrentWindow();
  await win.setFullscreen(kiosk);
  await win.setDecorations(!kiosk);
  el("kiosk-btn").textContent = kiosk ? "EXIT KIOSK" : "KIOSK";
}

window.addEventListener("DOMContentLoaded", () => {
  updateClock();
  setInterval(updateClock, 1000);

  el("refresh-btn").addEventListener("click", refresh);
  el("kiosk-btn").addEventListener("click", toggleKiosk);

  window.addEventListener("keydown", (e) => {
    if (e.key === "F11" || (e.metaKey && e.key.toLowerCase() === "k")) {
      e.preventDefault();
      toggleKiosk();
    }
  });

  refresh();
  setInterval(refresh, REFRESH_MS);
});
