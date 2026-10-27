// Mobile/PWA equivalent of electron/preload.cjs — exposes the exact same
// window.__JARVIS_BRIDGE__ shape (invoke/setFullscreen/setDecorations/
// sendCommand) so main.js, avatar.js etc. need zero changes to run here
// instead of inside Electron. Same seam that already carried this app
// through the Tauri -> Electron move; this is the third transport behind
// it, not a new design.
//
// Reported live: "the buttons don't work on phone" — the real cause was
// window.prompt() for the one-time token entry. iOS Safari is documented
// to not reliably show prompt()/alert()/confirm() dialogs once a page is
// running in standalone mode (added to the home screen) — it can silently
// no-op. That meant every API call went out with no token, got a 401,
// and nothing told the user why — clicking a button just did nothing,
// indistinguishable from the button itself being broken. Replaced with a
// real in-page form (same DOM either way, standalone or a Safari tab),
// plus visible error text so a future failure is never silent again.
(function () {
  const API_BASE = "https://163-192-207-84.sslip.io/api";
  const TOKEN_KEY = "jarvis_api_token";

  function showError(msg) {
    const banner = document.getElementById("error-banner");
    if (!banner) return; // main.js hasn't rendered the shell yet — fine, errors after that point are what matter
    banner.textContent = msg;
    banner.classList.add("show");
  }

  let tokenPromise = null;

  function askForToken() {
    if (tokenPromise) return tokenPromise;
    tokenPromise = new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.id = "token-overlay";
      overlay.innerHTML = `
        <div class="token-box">
          <div class="token-title">JARVIS API TOKEN</div>
          <div class="token-sub">One-time entry — stored on this device only.</div>
          <input type="text" id="token-input" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="paste token here" />
          <button id="token-submit">CONNECT</button>
        </div>
      `;
      document.body.appendChild(overlay);
      const input = overlay.querySelector("#token-input");
      const submit = () => {
        const value = input.value.trim();
        if (!value) return;
        localStorage.setItem(TOKEN_KEY, value);
        overlay.remove();
        tokenPromise = null;
        resolve(value);
      };
      overlay.querySelector("#token-submit").addEventListener("click", submit);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") submit();
      });
      input.focus();
    });
    return tokenPromise;
  }

  async function getToken() {
    const stored = localStorage.getItem(TOKEN_KEY);
    if (stored) return stored;
    return askForToken();
  }

  // GET-only endpoints — every invoke() call main.js actually makes.
  const GET_ENDPOINTS = {
    fetch_routing_state: "/routing_state",
    fetch_background_tasks: "/background_tasks",
    fetch_service_status: "/service_status",
    fetch_projects_log: "/projects_log",
    fetch_voice_state: "/voice_state",
  };

  async function apiFetch(path, options = {}) {
    const token = await getToken();
    let resp;
    try {
      resp = await fetch(API_BASE + path, {
        ...options,
        headers: {
          Authorization: `Bearer ${token}`,
          ...(options.body ? { "Content-Type": "application/json" } : {}),
          ...options.headers,
        },
      });
    } catch (e) {
      showError(`Can't reach JARVIS: ${e.message}`);
      throw e;
    }
    if (resp.status === 401) {
      // Bad/stale token — clear it so the next call re-prompts instead of
      // failing forever on a token that's never going to work, and say so
      // instead of leaving the click looking like it did nothing.
      localStorage.removeItem(TOKEN_KEY);
      showError("JARVIS token rejected — enter it again on your next tap.");
    }
    // Any other non-2xx (502/503 when the small Oracle server is busy or
    // restarting, 404, etc.) used to fall straight through to resp.json()
    // below — which throws an uncaught SyntaxError on a non-JSON error
    // body (an nginx/proxy error page isn't JSON). Button handlers with
    // no .catch of their own (Activate/Interrupt/Keep Listening) turned
    // that into "tap it, nothing happens, no error, looks broken."
    if (!resp.ok && resp.status !== 401) {
      showError(`JARVIS server error (${resp.status}) — try again in a moment.`);
    }
    if (!resp.ok) {
      throw new Error(`request to ${path} failed: ${resp.status}`);
    }
    try {
      return await resp.json();
    } catch (e) {
      showError("JARVIS sent back something unreadable — try again.");
      throw e;
    }
  }

  async function invoke(cmd) {
    const path = GET_ENDPOINTS[cmd];
    if (!path) throw new Error(`mobile bridge: unknown command ${cmd}`);
    return apiFetch(path);
  }

  async function sendCommand(command) {
    return apiFetch("/command", { method: "POST", body: JSON.stringify({ command }) });
  }

  // "Even if the Mac is off, I should be able to reach Jarvis from
  // anywhere" — this is the actual answer: talks straight to
  // jarvis_respond.py on the server, never touches the Mac or
  // always_on.py at all, so it works whether or not that machine is even
  // powered on. Can take up to ~25s for a reasoning-tier question (same
  // background-task poll window every other path already waits on) —
  // main.js shows a thinking state for exactly that reason, not a bug if
  // a reply takes a few seconds.
  const CHAT_SESSION_KEY = "jarvis_chat_session_id";

  function chatSessionId() {
    let id = localStorage.getItem(CHAT_SESSION_KEY);
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem(CHAT_SESSION_KEY, id);
    }
    return id;
  }

  async function chat(message) {
    return apiFetch("/chat", {
      method: "POST",
      body: JSON.stringify({ message, session_id: chatSessionId() }),
    });
  }

  // No real window chrome to control in a browser tab/home-screen PWA —
  // safe no-ops rather than throwing, same graceful-degradation approach
  // as everywhere else state might not be reachable.
  async function setFullscreen() {}
  async function setDecorations() {}

  window.__JARVIS_BRIDGE__ = { invoke, setFullscreen, setDecorations, sendCommand, chat };
})();
