# Jarvis — client-side status (2026-09-15)

Everything here lives on this Mac, in `~/FRIDAY/JARVIS-local/`, separate from
the server's own `~/JARVIS/STATUS.md`. Git history in this folder has the
full diff-by-diff story if you want it.

## Design reference

I looked at the four images you dropped in `~/FRIDAY/` (`JARVIS.webp`,
`iron-man-jarvis-desktop-*.jpg`, `1520131677833.jpeg`, the Stark-hologram
`.webp`, `intro-import.jpg`) before building anything. Common language across
all of them: near-black background, glowing cyan/gold circular HUD gauges,
thin hairline corner brackets on every panel, angular radial dials, uppercase
monospace labels. The dashboard below follows that directly rather than
generic dark-mode styling.

## 1. Dashboard (`dashboard/`) — Tauri app, built and tested live

A native desktop app (Rust + a vanilla JS/Vite frontend, not a browser tab).

**What it does:**
- Rust backend shells out over SSH (`ssh -i ~/.ssh/jarvis-oracle.key ubuntu@163.192.207.84`)
  to `tail` the server's `task_results.jsonl` and `systemctl is-active
  jarvis-orchestrator`, every 5 seconds. No new server-side port or API —
  reuses the SSH access you already had set up. Config (host/user/key path)
  lives in `~/.jarvis-dashboard/config.json`, written with sane defaults on
  first launch, editable without a rebuild.
- Renders: a task-completion ring (done/skipped/failed/needs-approval), four
  stat cards, a worker-load bar (claude/codex/antigravity/unknown), and a
  scrolling task log table with time/status/worker/task/summary — all in the
  HUD style above.
- **A 3D animated avatar** (Three.js — a wireframe icosphere with three tilted
  orbiting rings and a particle shell, gyroscope-style) sits at the top as
  the hero element. It idles with a slow breathing pulse, and **reacts live**
  to `~/.jarvis-dashboard/voice_state.json`: idle / listening / thinking /
  speaking, with the speaking state driven by a real amplitude envelope
  extracted from the generated speech (not faked) — the mesh visibly deforms
  and brightens in sync with how loud Jarvis is talking at that instant. I
  tested this by hand-writing that state file and watching it react correctly
  (screenshots along the way confirmed idle vs. speaking visibly differ).
- **Kiosk mode**: `src-tauri/tauri.kiosk.conf.json` is a real, tested build
  variant — fullscreen, no window chrome, confirmed via screenshot to cover
  the entire display edge-to-edge. Also toggleable at runtime with `Cmd+K` /
  `F11` inside the windowed build. See `dashboard/README.md` for exact run
  commands (dev, kiosk dev, and full `.app`/`.dmg` builds).

**Tested, not just written:** I ran `npm run tauri dev`, brought the real
window forward, and screenshotted it three times — once showing live data
pulled from your actual server (the same 4 real tasks + my test tasks from
tonight's other Claude session were visible), once with a synthetic
"speaking" state to confirm the avatar reacts, and once in full kiosk mode.
I also caught and fixed a `THREE.Clock` deprecation warning and had to add
Vite as a build step (the vanilla template has no bundler by default, which
would have silently broken the `three` import — worth knowing if you extend
the frontend further).

## 2. Voice interface (`voice/`) — local, free, tested stage-by-stage

- **STT**: `whisper.cpp` (already on your Mac via Homebrew) + a downloaded
  `ggml-base.en.bin` model (not bundled in git — 148MB, gitignored, see
  "what you need" below). Confirmed real transcription quality against both
  the bundled JFK sample and a live 2-second mic recording (`ffmpeg` via
  `avfoundation` — your Mac's mic permission for Terminal was already
  granted, no prompt appeared).
- **TTS**: Piper, installed in `.venv`. **Switched to a British voice**
  (`en_GB-alan-medium`) per your request instead of the American one I
  grabbed first — confirmed it actually sounds right by generating and
  playing a test line out loud.
- **`jarvis_client.py`**: bridges to the server's existing `jarvis_respond.py`
  over SSH, piping the query through **stdin** rather than interpolating it
  into a shell command — a misheard transcription containing quotes or
  shell metacharacters can't do anything unexpected on the remote end. Tested
  live against the real server ("how many tasks have completed so far" got a
  real, correct answer back). Also strips stray ANSI codes that leak from the
  server's `ollama run` CLI output.
- **`jarvis_voice.py`**: full push-to-talk loop (Enter to start recording,
  Enter to stop) — mic → whisper → `jarvis_client` → piper → speakers. Also
  writes `voice_state.json` at every stage so the dashboard avatar can track
  it live if both are running at once.
- **What "tested" means here, precisely**: every stage was verified
  individually with real audio/real network calls (recording, transcription,
  the SSH round-trip, speech synthesis + playback, and the avatar reacting to
  a written state file). I could not verify one live human-spoken
  end-to-end turn, since that needs you actually talking into the mic — run
  `python3 voice/jarvis_voice.py` (inside `.venv`) and try it; I'd genuinely
  expect it to work first try, but say so if anything's off.

## 3. Phone layer (`phone/`) — scaffolded only, inert by design

Full Twilio integration code exists and is syntax-checked, but **cannot run
against Twilio** — `config.py.require_configured()` hard-refuses (raises
`SystemExit`) until real credentials are in `phone/.env`. No account was
created, no signup attempted.

**What you need to do to activate it** (all in `phone/.env.example`, copy to
`phone/.env` and fill in):
1. Sign up at https://www.twilio.com/try-twilio (needs your payment method —
   I did not and would not do this step).
2. From the Twilio Console: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`.
3. Buy a Voice-capable number in the Console → `TWILIO_PHONE_NUMBER`.
4. Your own number → `JARVIS_OWNER_PHONE_NUMBER` (inbound calls from anyone
   else get an automatic "this line is private" + hangup — see
   `twilio_server.py`).
5. Run `ngrok http 5005` (free tier is fine) → paste the https URL into
   `PUBLIC_BASE_URL`.
6. Point the number's "A call comes in" webhook (Console → your number →
   Voice Configuration) at `{PUBLIC_BASE_URL}/voice`, method POST.

Once that's done: `python3 phone/twilio_server.py` starts a **localhost-only**
Flask server (127.0.0.1:5005 — not exposed to the internet by anything I
wrote; ngrok is the only thing that would expose it, and that's a step you
run yourself). Inbound calls get `<Gather input="speech">` + Twilio's own STT
(not whisper — Twilio needs the transcript before it can call back into the
webhook), routed through the same `jarvis_client.ask_jarvis()` the voice
pipeline uses, spoken back with Twilio's `Polly.Brian` (closest built-in
match to the local Piper British voice, since serving Piper's own audio
over the phone would mean hosting files publicly). Every webhook request is
signature-validated against `X-Twilio-Signature` so nothing but real Twilio
can trigger it. `make_call.py` does outbound notification calls the same way.

## 4. OS control bridge (`os-bridge/os_control.py`) — SAFE_MODE on, verified

`open_app`, `close_app` (gentle AppleScript quit, not force-kill), `set_volume`,
`brightness_up`/`brightness_down` (macOS has no CLI for absolute brightness
%, so this simulates the physical key — relative steps only), `clear_cache`
(macOS `purge` — reclaims inactive RAM, **does not touch any file**, by
design, since file deletion was explicitly off-limits).

**`SAFE_MODE = True`** at the top, exactly as instructed — I left it on and
did not flip it. Every call logs the exact command it would have run to
`actions.log`, then returns without executing. I tested this directly:
called `open_app("Calculator")` and confirmed via `osascript` that Calculator
process list stayed empty — nothing ran, only the log line was written.
File deletion and app uninstalls aren't implemented at all, not even behind
the flag, per your instruction.

## What's genuinely done vs. waiting on you

| Piece | State |
|---|---|
| Dashboard UI + live SSH data pull | Done, tested against real server |
| 3D avatar + kiosk mode | Done, tested via screenshots |
| Whisper STT | Done, tested on real + sample audio |
| Piper TTS (British voice) | Done, tested — audio played aloud |
| Voice ↔ dashboard state sync | Done, tested |
| Full human-spoken round trip | Code complete, **needs you to try it live** |
| Twilio phone layer | Code complete, **inert until you add real credentials** |
| OS control bridge | Code complete, **SAFE_MODE on, waiting for you to review `actions.log` and flip it** |

## Questions / things I wasn't sure about (didn't guess)

- **Mic device index**: `voice/jarvis_voice.py` defaults to avfoundation
  audio device `1`, which enumerated as "MacBook Air Microphone" on this
  machine tonight. If you ever plug in an external mic, override with
  `JARVIS_AUDIO_DEVICE=<n>` (run `ffmpeg -f avfoundation -list_devices true
  -i ""` to see current indices — they can shift).
- **Whisper model size**: I picked `base.en` (148MB, fast, good enough on an
  M4) over `small`/`medium` for responsiveness. If you find accuracy lacking
  on accented or noisy speech, `WHISPER_MODEL` env var swaps it — just
  download a bigger `ggml-*.bin` into `voice/models/`.
- **Brightness control** has no absolute-percentage API on macOS without an
  extra tool (`brightness` via Homebrew, not installed tonight since it
  wasn't clearly in scope) — current implementation only does relative
  up/down steps. Say the word if you want precise percentage control and
  I'll add that tool.

## Next steps for you

1. Try the voice loop for real: `cd ~/FRIDAY/JARVIS-local/voice && source
   ../.venv/bin/activate && python3 jarvis_voice.py`
2. Try the dashboard: `cd ~/FRIDAY/JARVIS-local/dashboard && npm run tauri dev`
   (or the kiosk variant — see `dashboard/README.md`)
3. When ready for the phone layer: sign up for Twilio yourself, fill in
   `phone/.env`, run `ngrok http 5005`, set the webhook, then
   `python3 phone/twilio_server.py`.
4. Read `os-bridge/actions.log` after playing with `os_control.py` a bit,
   then flip `SAFE_MODE = False` yourself when you're comfortable.

— built overnight by Claude, 2026-09-15
