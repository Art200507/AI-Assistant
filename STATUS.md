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

### Always-on mode (`voice/always_on.py`) — clap or long spacebar hold

No keypress needed to start a turn:
- **Clap detection**: one persistent `ffmpeg` stream, checked every ~20ms
  for a sharp amplitude transient well above the room's ambient noise floor
  (edge-triggered, with a cooldown so one clap doesn't fire twice). Tunable
  via `JARVIS_CLAP_RATIO` / `JARVIS_CLAP_ABS_MIN`. Run
  `python3 always_on.py --calibrate` for a live numeric meter to tune these
  against your actual room instead of guessing — it marks
  `<-- WOULD TRIGGER` in real time as you clap.
- **Long spacebar hold**: a global keyboard listener (`pynput`) fires once
  the spacebar's been held ≥`JARVIS_LONG_PRESS_SECONDS` (default 0.6s)
  *while still held* — a quick tap does nothing. This is global (works even
  if the terminal isn't the focused window), which means macOS will ask you
  to grant your terminal app Accessibility permission the first time
  (System Settings → Privacy & Security → Accessibility).
- **Either trigger** leads to the same flow: Jarvis speaks a short
  acknowledgment, *then* starts recording your actual question, stopping on
  ~1s of silence, then the normal transcribe → ask → speak path.
- Startup runs a **mic sanity check** (prints avg/peak signal level) and
  warns explicitly if the stream looks silent, rather than just hanging
  forever waiting for a clap that can never register. This is what caught
  the mic-device-instability bug described above.
- Also fixed tonight: Ctrl+C wasn't reliably killing the background
  `ffmpeg` process, which could hold the mic device and starve the *next*
  run of audio entirely — added explicit SIGINT/SIGTERM handlers plus a
  terminate-then-kill fallback; verified clean process exit on interrupt.

### Personality — dry, witty, flawlessly polite British, rarely a tiny joke

Two pieces, both tested:
- **Wake acknowledgment** (`always_on.py`, `_WAKE_ACK_POOL`): rotates
  through ~10 short JARVIS-flavored lines ("At your service, sir.", "Go
  ahead, sir.", etc.) instead of one fixed robotic phrase. Two of the ten
  are a tiny dry joke and an understated compliment respectively, so they
  surface roughly 1-in-10 draws each — "rarely," not every turn. Pin a
  single fixed phrase instead with `JARVIS_WAKE_ACK`.
- **Actual answers**: this is the one place tonight I touched a file on
  the *server*, not just this Mac — flagging that clearly since the server
  side is technically another session's territory. `jarvis_respond.py`'s
  `escalate_to_claude()` (the path real reasoning questions take) previously
  called `claude -p <your question>` with zero persona — just bare Claude.
  It now prepends a persona instruction (dry, understated British wit, rare
  small jokes, rare genuine compliments, addresses you as "sir", plain
  spoken prose since it's read aloud — no markdown/lists unless you
  explicitly ask for code). The `answer_lookup()` path (small local Ollama
  model, task-log questions) got a much lighter touch — politer tone only,
  deliberately no jokes, since a 3B model asked to be witty *and* factually
  accurate about your task log tends to just become inaccurate. Change is
  isolated to those two functions; `classify()`, the orchestrator, and
  everything the other session built are untouched. Verified live: a
  reasoning question got a genuinely well-observed, in-character answer
  addressed "sir"; the lookup path still answers plainly (when the small
  model isn't just being flaky, see below).
- **Known pre-existing quirk, not something I broke**: the lookup path's
  answers are inconsistent run-to-run — asked "how many tasks are done"
  three times in a row tonight and got three different phrasings, one of
  them briefly claiming no tasks existed at all before self-correcting on
  retry. That's `llama3.2:3b`'s inherent unreliability at exact fact
  retrieval, not a regression from tonight's changes (confirmed by testing
  before and after). If this bothers you, the fix is a better lookup model
  or grounding the prompt more strictly — say the word and I'll take a pass.

**Update, later the same night — feedback round two.** Tried it live and
reported: no real back-and-forth (had to re-trigger every single turn),
jokes still too frequent, and generally slow. Fixed:

- **Real conversation flow**: `always_on.py` no longer returns to
  standby after one answer. After speaking, it listens for a follow-up for
  up to `JARVIS_FOLLOWUP_WAIT_S` (default 7s) with no wake trigger needed —
  keep talking and it keeps responding. Silence for that long ends the
  conversation quietly (no "conversation over" announcement, it just goes
  back to standby) and you clap / hold spacebar again to start a fresh one.
  The wake acknowledgment now only plays once, at the very start of a
  conversation, not on every follow-up turn.
- **Jokes, tightened further**: previously ~1-in-10 *per wake trigger*.
  Since wake triggers now only happen once per conversation (not once per
  turn) that alone cut exposure a lot. On top of that, dropped the actual
  odds to 5% and moved the persona instruction on the server from
  "occasionally/rarely" to an explicit "~once every ten *conversations*,
  never twice in the same one, most conversations get zero" — the vaguer
  wording apparently wasn't restrictive enough for Claude to follow tightly.
- **Latency — found two real, separate causes, both fixed**:
  1. `jarvis_client.py` opened a brand new SSH connection for every single
     question (~0.44s of pure handshake overhead each time). It now reuses
     one connection (SSH `ControlMaster`/`ControlPersist`) across a whole
     conversation — measured ~0.44s → ~0.07s per turn after the first.
  2. Bigger one: the server's `ask_ollama()` shelled out to the `ollama run`
     CLI, which measured **~1.15s** per call just for CLI startup — and
     lookup questions make *two* such calls (classify, then answer). Switched
     it to Ollama's local HTTP API instead (same model, same machine,
     `localhost:11434`) — measured **~0.28s**, about 4x faster, and it also
     stopped being the source of the stray ANSI/spinner codes I was
     patching around client-side. Measured before/after on real questions:
     9.3s and 11.5s → 1.7s and 3.6s. A `claude -p` reasoning question is
     still bounded by however long Claude actually takes to think — that
     part isn't compressible from this side.
  - I did not touch `orchestrator.py`, `classify()`'s routing logic, or
    anything else on the server — both server-side edits tonight are
    scoped to `jarvis_respond.py`'s `ask_ollama()` and `escalate_to_claude()`.

**TTS naturalness**: asked you directly rather than guessing, since the real
options (macOS built-in / Kokoro / ElevenLabs) trade off setup effort,
speed, quality, and free-vs-paid differently. You picked: switch to the
free macOS voice now, decide later on Kokoro/ElevenLabs.

- Switched `jarvis_voice.py`'s `speak()` from Piper to the system `say`
  command (voice: `Daniel`, en_GB, male — set via `JARVIS_SAY_VOICE`;
  `JARVIS_TTS_ENGINE=piper` reverts to Piper). Same amplitude-envelope trick
  as before still works (`say` can output real WAV via
  `--file-format=WAVE --data-format=LEI16@22050`), so the dashboard avatar
  still reacts to actual loudness.
- **I introduced a real bug doing this, then fixed it**: I initially set
  the voice to `"Daniel (Enhanced)"`, assuming a higher-quality tier was
  already installed (found asset files on disk that looked like evidence of
  it, and a hash comparison that seemed to confirm it — both misleading).
  It isn't actually a registered voice on this Mac (`say -v '?'` only lists
  plain `Daniel`), and critically: **`say -v <unrecognized name>` does not
  error — it silently plays the system default voice instead.** That
  default turned out to be a female US voice, which is what you heard. Now
  using the verified `Daniel` (confirmed via `say -v '?'`, confirmed by
  listening), and added a startup check (`_validate_say_voice()`) that
  prints a loud warning if `JARVIS_SAY_VOICE` is ever set to something not
  actually installed, instead of silently substituting something else.
- **If you want the genuinely higher-quality Enhanced/Premium Daniel**: that
  requires you to download it yourself — System Settings → Accessibility →
  Spoken Content → System Voice → (i) info button → find Daniel → Download.
  Once installed, it'll appear as its own exact line in `say -v '?'` (I
  can't predict the exact string macOS will give it) — copy that string
  into `JARVIS_SAY_VOICE` and it'll be picked up automatically, with the
  new startup check confirming it's real.

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

- **Mic device index — this bit me tonight, worth knowing.** avfoundation
  device indices aren't stable; opening Teams added a "Microsoft Teams
  Audio" virtual device that bumped the real mic from index 1 to index 0.
  A script hardcoded to index 1 then silently recorded pure silence from
  the Teams virtual device — no error, just zero signal, which is exactly
  what "claps aren't triggering" looks like. Fixed by adding
  `voice/mic_device.py`, which picks the real microphone **by name** every
  run (skips anything with "teams"/"virtual"/"aggregate"/"blackhole" in the
  name) — both `jarvis_voice.py` and `always_on.py` use it now instead of a
  hardcoded index. `JARVIS_AUDIO_DEVICE` still overrides it if you ever need
  to force a specific device. Run `python3 voice/mic_device.py` any time to
  see what it would currently pick.
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
