import { createAvatar } from "./avatar.js";

// electron/preload.js exposes this under the same invoke(cmd) shape the
// Tauri build used, so this is the only place that needed to change when
// the app moved off Tauri/WKWebView onto Electron/Chromium.
// chat is mobile-only — electron/preload.cjs doesn't define it (the
// Electron app IS the Mac, it talks to Jarvis by voice; chat exists
// specifically for when you're away from the Mac entirely).
//
// This used to destructure window.__JARVIS_BRIDGE__ directly with no
// guard — if the bridge script (preload.cjs / mobile-bridge.js) ever
// failed to load (bad deploy, cache drift), this line threw and NOTHING
// below it in the module ran: a fully static, dead page with zero
// visible error, only a devtools console message. Same bug class that's
// bitten this project before (a missing DOM element halting all
// subsequent script execution). No-op stand-ins + a visible banner
// instead, so a broken bridge degrades to "buttons don't work, here's
// why" rather than a silent blank page.
const BRIDGE_MISSING_MSG = "JARVIS bridge failed to load — reload the page. (window.__JARVIS_BRIDGE__ is undefined)";
const bridge = window.__JARVIS_BRIDGE__ || (() => {
  console.error(BRIDGE_MISSING_MSG);
  const banner = document.createElement("div");
  banner.className = "error-banner show";
  banner.textContent = BRIDGE_MISSING_MSG;
  document.body.appendChild(banner);
  const noop = async () => {};
  return { invoke: noop, setFullscreen: noop, setDecorations: noop, sendCommand: noop, chat: noop };
})();
const { invoke, setFullscreen, setDecorations, sendCommand, chat } = bridge;

const REFRESH_MS = 5000;
// Electron's fetch_voice_state is a local file read (near-free) — 150ms
// feels responsive for the avatar's live amplitude. The PWA's is a real
// authenticated HTTPS round-trip to the remote server (mobile-bridge.js's
// apiFetch) — reusing the same 150ms there meant the phone fired ~6-7
// authenticated requests/second at a small Oracle server, forever, any
// time the page was open: real battery/data drain and server load for
// state that doesn't change anywhere near that fast. `chat` only exists
// on the mobile bridge (see the comment above), so its presence is
// already this file's established way to tell the two apart.
const IS_MOBILE_BRIDGE = typeof chat === "function";
const VOICE_POLL_MS = IS_MOBILE_BRIDGE ? 2000 : 150;
// SSH connections aren't cheap (~0.4s cold each) and this one isn't
// multiplexed like the Python voice client's — polling too fast would
// stack up overlapping SSH connections against the server for no real
// benefit, since routing decisions don't change faster than a voice turn.
const ROUTING_POLL_MS = 3000;

const STATE_LABELS = {
  idle: "IDLE",
  listening: "LISTENING",
  thinking: "THINKING",
  speaking: "SPEAKING",
};

const STATE_SUB = {
  idle: "voice link idle — say the word",
  listening: "capturing audio…",
  thinking: "routing to the brain…",
  speaking: "responding",
};

const el = (id) => document.getElementById(id);

const STATUS_ORDER = ["done", "running", "failed"];

// Maps the real route name the server/local-mac actually used (see
// jarvis_respond.py's _write_route_state / routing_state.py) to the node
// id it should light up in the "bird's eye view" flow diagram.
const ROUTE_NODE = {
  local: "node-local",
  simple: "node-fast",
  reasoning: "node-cloud",
  lookup: "node-lookup",
};
const ALL_ROUTE_NODES = ["node-local", "node-fast", "node-cloud", "node-lookup"];

function fmtAge(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

let routingPollInFlight = false;
async function pollRoutingState() {
  if (document.hidden || routingPollInFlight) return;
  routingPollInFlight = true;
  try {
    const rs = await invoke("fetch_routing_state");
    for (const id of ALL_ROUTE_NODES) {
      el(id)?.classList.remove("active");
    }
    const activeNode = ROUTE_NODE[rs.tier];
    if (activeNode) el(activeNode)?.classList.add("active");
    if (avatar) avatar.setRouting(rs.tier);

    el("route-current-label").textContent = rs.label || "No routing activity yet";
    el("route-current-detail").textContent = rs.detail ? `"${rs.detail}"` : "";
    const ageS = rs.timestamp ? Date.now() / 1000 - rs.timestamp : null;
    el("route-current-age").textContent = ageS !== null ? `${rs.source === "local-mac" ? "this Mac" : "server"} · ${fmtAge(ageS)}` : "";
  } catch {
    /* routing state not available yet — leave the diagram unlit */
  } finally {
    routingPollInFlight = false;
  }
}

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
    const segEl = el(`seg-${key}`);
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
  el("count-running").textContent = counts.running || 0;
  el("count-failed").textContent = counts.failed || 0;
}

function renderWorkers(tasks) {
  const counts = {};
  for (const t of tasks) {
    const s = t.session_id ? t.session_id.slice(0, 8) : "unknown";
    counts[s] = (counts[s] || 0) + 1;
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
    body.innerHTML = '<div class="empty-row">No background tasks yet</div>';
    return;
  }
  body.innerHTML = tasks
    .slice(0, 200)
    .map((t) => {
      const status = t.status || "unknown";
      const session = t.session_id ? t.session_id.slice(0, 8) : "—";
      // & must be escaped first — escaping < alone left a literal "&" in
      // the output, which the browser's HTML parser then reads as the
      // start of its OWN entity (e.g. "&lt;" typed by the user became
      // "&amp;lt;" -> rendered as "&lt;" literally, or "&&" sequences
      // could merge/vanish) — logged request text didn't match what was
      // actually asked.
      const request = (t.user_input || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").slice(0, 200);
      const iso = t.started_at ? new Date(t.started_at * 1000).toISOString() : null;
      return `
        <div class="task-row">
          <div class="task-row-top">
            <span class="badge ${status}">${status}</span>
            <span class="task-time">${iso ? fmtTime(iso) : "—"}</span>
          </div>
          <div class="task-request">${request}</div>
          <div class="task-session">${session}</div>
        </div>
      `;
    })
    .join("");
}

// Single-flight + backgrounded-tab guards, shared shape across all three
// pollers below. Without these: (1) if a call ever takes longer than the
// poll interval (slow network, server hiccup), a second overlapping call
// could start and its response — arriving in unpredictable order — could
// repaint the UI with older data than what's already showing; (2) on
// mobile especially, a backgrounded PWA tab kept polling the remote
// server at full rate with the screen off, burning battery/data for a
// view nobody's looking at.
let refreshInFlight = false;

async function refresh() {
  if (document.hidden || refreshInFlight) return;
  refreshInFlight = true;
  try {
    try {
      const resp = await invoke("fetch_background_tasks");
      if (!resp.ok) {
        setConnection(false);
        showError(resp.error || "Failed to reach jarvis-oracle server over SSH");
        return;
      }
      setConnection(true);
      showError(null);

      const counts = { done: 0, running: 0, failed: 0 };
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

    try {
      const log = await invoke("fetch_projects_log");
      el("projects-log").textContent = log.ok
        ? log.content.trim() || "No projects recorded yet."
        : `Couldn't load the project log: ${log.error || "unknown error"}`;
    } catch (e) {
      el("projects-log").textContent = `Couldn't load the project log: ${e}`;
    }
  } finally {
    refreshInFlight = false;
  }
}

let kiosk = false;
async function toggleKiosk() {
  kiosk = !kiosk;
  await setFullscreen(kiosk);
  await setDecorations(!kiosk);
  // kiosk-btn doesn't exist in mobile.html (no real window chrome in a
  // browser tab/home-screen PWA to kiosk-ify) — optional, not missing.
  const btn = el("kiosk-btn");
  if (btn) btn.textContent = kiosk ? "EXIT KIOSK" : "KIOSK";
}

// Same CSS transition that "assembles" the instruments on first load
// (body.ready) runs in reverse here — hiding the HUD is the same visual
// language as showing it, not a separate hide/show implementation.
function toggleFocus() {
  const active = document.body.classList.toggle("focused");
  const btn = el("focus-btn");
  btn.textContent = active ? "⛶ INSTRUMENTS" : "⛶ FOCUS";
  btn.setAttribute("aria-pressed", String(active));
}

let avatar = null;
let lastVoiceState = null;

let voicePollInFlight = false;
async function pollVoiceState() {
  if (document.hidden || voicePollInFlight) return;
  voicePollInFlight = true;
  try {
    const vs = await invoke("fetch_voice_state");
    const state = vs.state || "idle";
    const amp = vs.amplitude || 0;
    if (avatar) avatar.setState(state, amp);
    if (state !== lastVoiceState) {
      lastVoiceState = state;
      el("avatar-state").textContent = STATE_LABELS[state] || state.toUpperCase();
      el("avatar-sub").textContent = STATE_SUB[state] || "";
    }
  } catch {
    /* voice pipeline may not be running yet — avatar just stays idle */
  } finally {
    voicePollInFlight = false;
  }
}

window.addEventListener("DOMContentLoaded", () => {
  updateClock();
  setInterval(updateClock, 1000);

  avatar = createAvatar(el("avatar-canvas"));

  el("refresh-btn").addEventListener("click", refresh);
  el("kiosk-btn")?.addEventListener("click", toggleKiosk);
  el("focus-btn").addEventListener("click", toggleFocus);

  // Mobile-only button (doesn't exist in the Electron build's index.html)
  // — "80% hologram, buttons, that's it, processes if I extend or
  // request that": everything except Voice Control starts hidden (see
  // .mobile-extra in styles.css); toggling body.mobile-expanded reveals
  // it. A body class, not inline styles — each panel type needs a
  // different display value when shown (flex vs block), which the CSS
  // already encodes per-panel; JS doesn't need to know or guess which.
  el("mobile-more-btn")?.addEventListener("click", () => {
    const btn = el("mobile-more-btn");
    const expanded = document.body.classList.toggle("mobile-expanded");
    btn.setAttribute("aria-pressed", String(expanded));
    btn.textContent = expanded ? "▲ LESS" : "▼ MORE";
  });

  // Guarded on `chat` existing — undefined in the Electron build, where
  // there's no chat-form in the DOM at all to wire up.
  if (chat && el("chat-form")) {
    const history = el("chat-history");
    const input = el("chat-input");
    const sendBtn = el("chat-send");

    function appendMessage(role, text) {
      const hint = history.querySelector(".chat-hint");
      if (hint) hint.remove();
      const div = document.createElement("div");
      div.className = `chat-msg ${role}`;
      div.textContent = text;
      history.appendChild(div);
      history.scrollTop = history.scrollHeight;
      return div;
    }

    el("chat-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      sendBtn.disabled = true;
      appendMessage("user", text);
      // Reasoning-tier replies can take up to ~25s (same background-task
      // poll window everything else waits on) — this placeholder is why
      // that's not a bug, it's a real "still working" signal.
      const thinking = appendMessage("thinking", "…thinking");
      try {
        const resp = await chat(text);
        thinking.remove();
        appendMessage("jarvis", resp.ok ? resp.answer : `Error: ${resp.error}`);
      } catch (err) {
        thinking.remove();
        appendMessage("jarvis", `Couldn't reach Jarvis: ${err.message}`);
      } finally {
        sendBtn.disabled = false;
        input.focus();
      }
    });
  }

  // Reaches always_on.py via DashboardCommandWatcher (a local file it
  // polls — the Dashboard has no other channel to that separate
  // process). Fire-and-forget, same as every other wake trigger.
  // sendCommand() rejects on a server/network error (see mobile-bridge.js's
  // apiFetch, which already shows a visible error banner before rejecting)
  // — these three had zero handling at all, so that rejection went out as
  // an unhandled promise, invisible outside devtools: tap the button, a
  // server hiccup eats it silently, nothing on screen ever says why.
  el("activate-btn").addEventListener("click", () => sendCommand("activate").catch(() => {}));
  el("interrupt-btn").addEventListener("click", () => sendCommand("interrupt").catch(() => {}));
  el("keep-listening-btn").addEventListener("click", () => sendCommand("keep_listening").catch(() => {}));

  document.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      avatar?.setManualMode(btn.dataset.mode);
      document.querySelectorAll(".mode-btn").forEach((b) => b.classList.toggle("selected", b === btn));
      document.querySelectorAll(".swatch").forEach((b) => b.classList.remove("selected"));
    });
  });
  document.querySelectorAll(".swatch").forEach((btn) => {
    btn.addEventListener("click", () => {
      avatar?.setManualColor(btn.dataset.color);
      document.querySelectorAll(".swatch").forEach((b) => b.classList.toggle("selected", b === btn));
    });
  });
  el("energy-slider").addEventListener("input", (e) => {
    avatar?.setManualEnergy(Number(e.target.value) / 100);
    el("energy-value").textContent = `${e.target.value}%`;
  });
  el("expansion-slider").addEventListener("input", (e) => {
    avatar?.setManualExpansion(Number(e.target.value) / 100);
    el("expansion-value").textContent = `${e.target.value}%`;
  });
  el("auto-btn").addEventListener("click", () => {
    avatar?.clearOverrides();
    document.querySelectorAll(".mode-btn, .swatch").forEach((b) => b.classList.remove("selected"));
    el("energy-slider").value = 55;
    el("energy-value").textContent = "55%";
    el("expansion-slider").value = 12;
    el("expansion-value").textContent = "12%";
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "F11" || (e.metaKey && e.key.toLowerCase() === "k")) {
      e.preventDefault();
      toggleKiosk();
    }
    if (e.key === "Escape") toggleFocus();
  });

  refresh();
  setInterval(refresh, REFRESH_MS);
  setInterval(pollVoiceState, VOICE_POLL_MS);

  pollRoutingState();
  setInterval(pollRoutingState, ROUTING_POLL_MS);

  // Double rAF: let the initial opacity:0 state actually paint first, so
  // the .instrument transition animates instead of snapping straight to
  // visible — the "assembling" effect only reads as assembly if there's
  // a real frame boundary between hidden and the transition starting.
  requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.add("ready")));
});
