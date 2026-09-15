# J.A.R.V.I.S. Dashboard

A Tauri desktop app that shows live task status from the cloud orchestrator
(pulled over SSH from `~/JARVIS/task_results.jsonl` on the server), with a
3D animated avatar that reacts in real time to the local voice pipeline
(`../voice/jarvis_voice.py`) via `~/.jarvis-dashboard/voice_state.json`.

## Run it (windowed, for development)

```
npm install
npm run tauri dev
```

## Run it in kiosk mode (fullscreen, no chrome — for a spare monitor/tablet)

```
npm run tauri dev -- --config src-tauri/tauri.kiosk.conf.json
```

Press `Cmd+K` or `F11` inside the app to toggle kiosk fullscreen at runtime
instead of relaunching.

## Build a real .app / .dmg

```
npm run tauri build                                             # normal window
npm run tauri build -- --config src-tauri/tauri.kiosk.conf.json  # kiosk variant
```

Output lands in `src-tauri/target/release/bundle/`.

## Config

First launch writes `~/.jarvis-dashboard/config.json` with defaults pointing
at the known server (163.192.207.84, key `~/.ssh/jarvis-oracle.key`). Edit
that file if the server IP, SSH key path, or username ever changes — no
rebuild needed, it's read at runtime.
