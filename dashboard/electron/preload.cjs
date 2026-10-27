// Exposes the exact same shape the frontend already used for Tauri
// (window.__TAURI__.core.invoke / window.__TAURI__.window.getCurrentWindow)
// so src/main.js needed only a two-line change, not a rewrite, to run
// under Electron instead.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("__JARVIS_BRIDGE__", {
  invoke: (cmd) => ipcRenderer.invoke("invoke", cmd),
  setFullscreen: (value) => ipcRenderer.invoke("set_fullscreen", value),
  setDecorations: (value) => ipcRenderer.invoke("set_decorations", value),
  sendCommand: (command) => ipcRenderer.invoke("send_dashboard_command", command),
});
