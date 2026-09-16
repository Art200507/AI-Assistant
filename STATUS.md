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

**Update, round three.** Three asks: full shell/web access for voice
conversation, shorter answers, and (again) TTS — the macOS voice didn't
land. All three landed on the server's `jarvis_respond.py` plus a new local
TTS engine.

- **Full Bash/Web access for the conversational path — your explicit
  choice, after being told the real risk**: this is voice-triggered and
  Whisper can mishear you; "no sudo" doesn't prevent a misheard phrase from
  deleting files, changing configs, or making network calls as your user.
  You picked full access anyway. `escalate_to_claude()` now runs with
  `--allowedTools "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch"
  --dangerously-skip-permissions` (the skip-permissions flag is *required*
  for headless tool calls to actually execute — without it they just hang,
  which is why the very first Write attempt days ago failed silently).
  Scoped to a new `~/jarvis-workspace/` directory on the server, kept
  separate from `~/JARVIS` (the orchestrator's own territory, which
  auto-commits on its own schedule — didn't want voice-triggered Bash/git
  activity colliding with that). Verified live: it correctly reported its
  own cwd, confirmed it wasn't a git repo, and answered a real web search.
- **GitHub access, so it can create/push repos of its own** — installed
  `gh` (GitHub CLI) on the server and started the device-auth flow.
  **This needs you** — if you haven't finished it: open
  `https://github.com/login/device` and enter the code from the terminal
  (a fresh one was issued if the first expired — check the conversation for
  the current code). Once `gh auth status` shows logged in, the last step
  is `gh auth setup-git` so plain `git push` also authenticates, which I'll
  run once you confirm login succeeded.
- **Shorter answers**: `JARVIS_PERSONA` on the server now explicitly says
  this is a live spoken conversation, default to 2-4 sentences, lead with
  the actual answer, only go longer when the material genuinely can't fit
  (real multi-step reasoning, code). Verified live — a question that
  previously got a 3-paragraph answer now gets 3 sentences that still
  actually answer it.
- **TTS, again — macOS's Daniel wasn't good enough for you.** Set up
  Kokoro (local, open-weights, noticeably more natural than both Piper and
  macOS). Real friction along the way, worth knowing about:
  - `kokoro-onnx`'s latest model release (`model-files-v1.1`) is NOT
    compatible with `kokoro-onnx==0.4.7` (the current pip package) — throws
    an ONNX dtype error on every voice. Had to drop back to the
    `model-files-v1.0` release's `kokoro-v1.0.onnx` (325MB) to get working
    synthesis. If you ever `pip install --upgrade kokoro-onnx`, the model
    files may need re-pairing to whatever version that release expects.
  - Generated and played all 4 British-male voices (`bm_daniel`, `bm_fable`,
    `bm_george`, `bm_lewis`) live so you could pick by ear rather than me
    guessing — you picked **`bm_daniel`**.
  - Real tradeoff, measured: ~2.8s to synthesize a 4-second sentence, vs
    macOS's near-instant. The sentence-pipelined playback (built two rounds
    ago) hides some of this for multi-sentence answers but the first
    sentence's synthesis time is unavoidable added latency. `macos` and
    `piper` are both still one env var away (`JARVIS_TTS_ENGINE`) if this
    trade isn't worth it once you've lived with it.

**Update, round four.** Sources being read aloud, no way to interrupt,
question intonation sounding flat, and continued frustration with speed —
plus confirming Ollama is in fact doing its job.

- **Sources/citations no longer spoken**: a WebSearch answer used to come
  back with a trailing "Sources: [x](url), [y](url)" block that got read
  aloud verbatim, URLs and all. `jarvis_client.py`'s `_clean()` now strips
  the whole sources block, converts any markdown link to just its visible
  text (never speaks a raw URL), and drops backticks too. Tested against
  the exact response that triggered this.
- **Barge-in — talk over Jarvis and it stops, immediately**: real feature,
  not a toggle. A background thread watches the mic *while* Jarvis is
  speaking; several consecutive loud chunks (not just one, so a click/pop
  doesn't falsely trigger) kills playback mid-sentence and the rest of what
  you're saying gets captured as your actual turn — no need to wait it out
  or re-trigger. Had a real bug here I caught by testing rather than
  shipping blind: my first threshold guess (1200) made Jarvis interrupt
  *itself* constantly, because its own voice leaking from the speakers back
  into the mic during playback measured up to 1365 RMS — sat right in my
  threshold's range. Measured the actual echo level directly (p50 372, p90
  828, p99 1227, max 1365) and moved the threshold to 2000 for real
  headroom. Confirmed fixed: a full sentence now plays to completion
  without self-interrupting. **Not yet confirmed**: that 2000 is low enough
  to actually catch you interrupting at normal talking volume — I can't
  test that side myself (would need an actual human voice at the mic).
  Try it; if it doesn't trigger when you talk over it, `JARVIS_BARGE_IN_RMS`
  is the knob, lower it a bit.
- **Question intonation sounding flat**: looked into it rather than
  guessing. Confirmed the "?" reaches Kokoro intact (none of the cleaning
  regexes touch punctuation), and `Kokoro.create()` exposes no
  prosody/intonation parameter at all — just text, voice, speed, language.
  Whatever rising/falling inflection it produces is baked into the model's
  training, not something tunable from the outside. Generated a genuine
  question and a matched statement back to back so you can judge the
  difference yourself. If this matters a lot, it's a real point in favor of
  ElevenLabs (which does proper prosody) over any local model — Kokoro,
  Piper, and macOS's Daniel are all going to have similar flatness, it's
  not specific to this one voice.
- **On "why isn't Ollama doing anything / is it even active" — it is,
  confirmed, this isn't a bug**: Ollama handles two things — classifying
  every question as lookup-vs-reasoning, and fully answering lookup
  questions about the task log (already sped up 4x this session via its
  HTTP API instead of shelling out to the CLI). It was never going to
  handle real conversation — a local 3B model attempting the personality,
  reasoning, and reliability you're asking for would be a worse experience
  than Claude, not a faster acceptable one; that's exactly the kind of
  quality-vs-speed tradeoff already causing friction with Kokoro's voice.
  The genuine bottleneck for reasoning questions is Claude's actual
  response time — real thinking, over the network, bounded by however long
  that takes. I already pulled the levers available without changing that
  architecture (SSH connection reuse, Ollama's speed, sentence-pipelined
  TTS so playback starts before the full answer is even synthesized). The
  one remaining lever — true token-by-token streaming so it starts talking
  mid-generation — needs switching off the Claude Pro subscription to
  direct API billing, which I flagged as your call to make, not mine to
  assume, a few rounds back.

**Update, round five — a real conversation transcript, diagnosed and fixed.**
You pasted a full back-and-forth and asked me to find the problems myself
rather than list them for me. Found two real bugs and confirmed one thing
was working as designed:

- **The big one, now fixed: zero conversation memory.** Every turn was
  being sent to Claude as a completely fresh, isolated call — no memory of
  anything said earlier in the same conversation. This is why `"and
  model."` (continuing "what mode... and model [are you using]?") got a
  nonsense generic reply, and why asking "do you remember what I asked?"
  got an honest "no, this is the start of our conversation" — Claude was
  telling the truth about the architecture. Root cause: I'd built
  turn-taking continuity at the *audio* layer (stays listening, no
  re-trigger needed) but never threaded actual conversation history into
  the backend calls. Fixed properly: `claude -p` supports real session
  continuity via `--session-id`/`--resume` (verified directly — a fresh
  session correctly remembered "42" after being told it, a nonexistent
  session ID fails cleanly rather than silently starting fresh). Generate
  one UUID per voice conversation, in `always_on.py`, resume it (falling
  back to creating it fresh if this is the first Claude call of that
  conversation), pass it through as JSON over stdin instead of raw text.
  **Verified against the exact failure from your transcript**: re-asked
  "What mode are you using?" → "and model." → "Do you remember what I just
  asked you?" as a real 3-turn session. It now answers the fragment
  correctly and accurately recalls both prior questions. Also moved the
  persona from being prepended to your message every time to
  `--append-system-prompt` (the correct mechanism — adds to Claude's
  built-in system prompt instead of just being extra text in the user
  turn).
- **Real bug, probably fixed, not fully verified: "stand down" got
  captured as just "down."** Best explanation: real words often start
  quiet and ramp up in volume, so the soft onset of "stand" likely sat
  under the RMS threshold before crossing it partway through the word —
  a known failure mode of any hard-threshold voice detector, not specific
  to this implementation. Added a pre-roll buffer: the last 160ms before
  voice is detected is always kept and prepended once it crosses the
  threshold, so a quiet onset isn't lost. Logic-verified and integration
  tested; the actual acoustic fix needs you trying it for real to confirm.
- **Not a bug, confirmed by design**: declining to guess at the weather
  without knowing your location. Your call whether you want real weather
  capability — would need your location passed in somehow.
- **Minor, low priority**: "what mode are you using" got a slightly hollow
  answer once ("standard mode, no toggles") — there's no real "mode"
  concept in this system, so that's Claude making something plausible-but-
  empty out of an ill-defined question. Not worth engineering around.

**Update, round six — two more real fixes.**

- **Session scope was wrong.** Round five's conversation-memory fix minted
  a new session UUID on every wake trigger, so memory reset any time there
  was a pause long enough to end one "conversation." Moved it up a level:
  one UUID generated once when `always_on.py` starts, printed at startup,
  reused for every wake trigger for the life of that process. Only
  restarting the script starts fresh. A full context window is left to
  Claude Code's own autocompact rather than us minting a new session on
  purpose — a fresh session would just throw away the memory this whole
  fix was for.
- **Silence-length cutoff replaced with an explicit stop word.** Any fixed
  pause threshold is wrong for someone part of the time — too short cuts
  you off mid-thought, too long feels laggy. Now: say "Jarvis" to end your
  turn, like a radio sign-off. A pause is just a checkpoint, not a hard
  stop — whatever's been said gets transcribed and checked for a trailing
  "Jarvis"; if it's not there, recording continues regardless of how long
  the pause was. A silence fallback (4s) still exists purely for when you
  forget to say it. Unit-tested the matching logic directly (correctly
  ignores "Jarvis" appearing mid-sentence, only triggers when it's the
  literal last word) and ran the full state machine end-to-end without
  crashing. **Honest gap**: I could not cleanly verify the full
  acoustic path (spoken "...Jarvis" → correctly detected) myself. My only
  test proxy — playing synthesized audio through the speakers and
  re-capturing it via the mic (the same trick that worked for measuring
  barge-in echo levels) — produces audio degraded enough by the
  speaker→air→mic round trip that Whisper hallucinated unrelated text
  instead of transcribing it accurately, on a phrase that should have been
  easy. That's a real limitation of bounced audio as a test signal, not
  necessarily a bug in the detection logic — but it means this one
  genuinely needs your real voice to confirm before I'd call it done. If
  "Jarvis" doesn't reliably end your turn, tell me exactly what happened
  (what you said, what it transcribed) and I'll dig in with real data
  instead of another synthetic proxy.

**Update, round seven — 3-tier model routing (Gemini Flash added).**

- **Architecture clarified**: cloud-Claude (on the Oracle server) has never
  had, and still doesn't have, any access to this Mac. Two fully separate
  machines; only text crosses over SSH.
- **Gemini Flash wired in as a fast/cheap tier for simple questions**
  ("how are you", "what does X mean", "what's the weather") — this was the
  user's explicit ask to stop paying Claude-latency for things that don't
  need it. Key provided, tested directly against the API first: it
  authenticates fine, but the linked Google project currently has **zero
  prepaid credits** — `gemini-2.5-flash` is deprecated (had to switch to
  `gemini-3.6-flash`), and calls fail with `RESOURCE_EXHAUSTED` until
  credits are added at ai.studio/projects. That's a billing step for the
  user, not something to work around. `ask_gemini()` returns `None` on any
  failure and `answer_simple()` falls back to Claude cleanly — nothing
  breaks in the meantime, it's just not faster yet. Key stored server-side
  at `~/.jarvis_gemini_key` (not in the git repo).
- **Shared cross-model transcript** — added because introducing a third,
  stateless model right after fixing conversation memory would have broken
  it again through the side door: Gemini has no memory of its own, so a
  "simple" turn handled by Gemini would vanish from Claude's awareness the
  moment the conversation crossed tiers. Every turn (whichever tier
  answers it) gets appended to `~/jarvis-workspace/sessions/<uuid>.json`;
  Gemini calls get the recent transcript prepended as context, Claude
  calls get any Gemini-only turns injected (its own `--resume` history
  already covers turns it handled itself).
- **Caught and fixed a real regression before it mattered**: the first
  version of the 3-way classifier prompt broke task-log lookups — "how
  many tasks failed" started getting routed to "reasoning" instead of
  "lookup" (100% reproducible, verified directly against Ollama). The
  2-way prompt it replaced got this right every time. Root cause: vague
  category descriptions gave the 3B model too much room to confuse
  adjacent categories. Fixed with explicit trigger words and concrete
  examples per category, verified 100% consistent across 8 test queries
  spanning all three routes before deploying. **Known minor imperfection**:
  "can you search the web for the news" classifies as "simple" rather than
  "reasoning" — doesn't matter today since Gemini falls back to Claude
  anyway, but once Gemini is actually working, a request needing
  WebSearch could get routed to a tier with no tool access. Small
  follow-up needed once billing is sorted.
- **Not yet addressed**: the broader "make it more like OpenClaw, more
  plugins/features" ask. Flagged rather than guessed at — that's a big
  enough decision (which integrations, what permissions, what's actually
  worth building) that it deserves its own focused pass rather than being
  squeezed in alongside everything else tonight.

**Update, round eight — Groq replaces Gemini as the fast tier, now
actually working.** You provided a Groq key. Tested it directly before
wiring anything in (same discipline as Gemini): `llama-3.3-70b-versatile`
doesn't exist on this account, `openai/gpt-oss-20b` does. First real
response came back empty — it's a reasoning model, and a small
`max_tokens` let it burn the whole budget on internal chain-of-thought
before ever writing the actual answer (`finish_reason: "length"`). Fixed
with `max_tokens: 300` + `reasoning_effort: "low"`; confirmed real content
at ~0.09-0.27s completion time. Wired in as the active fast tier
(`ask_groq`, `openai/gpt-oss-20b`), Gemini's code left in place but dormant
for whenever that billing gets sorted — swapping back is a one-line change
in `answer_simple`. Verified through the full pipeline, not just the raw
API: "how are you doing" / "what does renaissance mean" / "what's the
capital of France" all answered correctly in 1.3-1.7s, down from ~12s
falling back to Claude before this. Also caught and fixed a small
formatting issue while checking the output: Groq's model uses markdown
emphasis (`*word*`, `**word**`) despite being told not to — added
stripping for it in `jarvis_client._clean()` alongside the existing
sources/links/backticks cleanup, so TTS doesn't get asterisks read as
literal characters (or just silently garbled).

**Update, round nine — real hallucination bugs, not just slowness.** User
tested via the text-only path (`jarvis_client.py`) and caught the Groq tier
confidently fabricating things: asked "what model are you using," got
"OpenAI's GPT-4 architecture" (false — it's Claude, or Groq's gpt-oss-20b
depending on tier, never GPT-4); asked for a status report, got entirely
invented data — specific fake CPU%, memory%, uptime, and a fake "security
patch 1.2" scheduled update, none of which exist anywhere in this system.
Confirmed the root cause directly rather than guessing: Groq's gpt-oss-20b
has a strong baked-in self-identity from its own training that overrides
the JARVIS_PERSONA system prompt specifically for self-referential
questions, and has zero grounding data for system-status questions so it
just invents plausible-sounding numbers.

Fixed with three changes, each verified against the real pipeline before
calling it done:
1. **Identity questions hard-routed to Claude**, bypassing classify()
   entirely — Claude self-reports accurately ("Sonnet 5, model ID
   claude-sonnet-5") every time; Groq has been shown not to. Regex also
   catches Whisper's phonetic mangling of "pre-prompted" ("prepromted").
2. **Status-report requests also hard-routed to Claude** — asked the user
   directly whether they wanted the fast-but-shallow version or the
   slow-but-actually-verified version (Claude checking real git status and
   session logs vs. a quick log paraphrase); they chose thorough. Also
   hard-routed rather than left to classify(), because classify() alone
   tested inconsistently for this exact phrase (5/5 "lookup" in one
   isolated batch, "reasoning" in the live run the decision was based on)
   — a preference this explicit shouldn't ride on classifier sampling
   variance. Deliberately narrow regex — specific counts like "how many
   tasks failed" are NOT caught by this, and correctly stay on the fast
   path (verified: 1.3s vs. ~15s for the broad version).
3. **Explicit anti-fabrication instruction** added to the shared persona —
   "never invent technical details you don't have, say so plainly instead"
   — applies to every tier as a general safety net beyond just these two
   hard-routed cases.
4. Smaller: `answer_lookup()` now tries Groq first (grounded in the real
   task log, same latency class as Ollama's HTTP API, noticeably better
   synthesis quality) before falling back to Ollama — this is what "good
   status reports" from a smart model actually needed for the queries that
   *do* stay on the fast lookup path.

**Update, round ten — local file access. "This was the main piece of the
whole system... we just cut it off."** Fair callout. Everything until now
let cloud-Claude read/write files, but only on the server — it genuinely
cannot reach this Mac's filesystem, full stop, no workaround changes that.
What was actually missing was a *local* component: something on this Mac
that reads a local file, hands it to cloud-Claude, and applies the result
back here.

Built `voice/file_bridge.py`. Design choice worth explaining: voice-
dictating an exact file path is unreliable (Whisper mangling
`/Users/atharva/Projects/...`), so this uses a drop-folder instead —
`inbox/` at the project root. Put a file there, say "check the inbox" or
"fix that file" (detected locally in `always_on.py` via `_FILE_FIX_RE`,
*before* anything goes to the server — has to be local, since only this
Mac can read that folder). Flow: backs up the original first
(`backups/`, timestamped, never overwritten — this can never lose your
original even if a "fix" is wrong) → uploads to
`~/jarvis-workspace/incoming/` on the server → asks Claude (which already
has real Bash/Read/Write there) to find and fix actual bugs, not invent
style changes → downloads the result to a temp file → only replaces the
original on a clean download → logs the whole thing to `activity.log`
(file, backup path, timestamp, what Claude said it changed).

Tested end-to-end twice with real bugs I planted on purpose (zero-division
guards missing, an off-by-logic bug that zeroed instead of updated a
running max) — both found and correctly fixed, originals safely backed up,
activity logged both times. Not a demo — ran the actual pipeline, checked
the actual resulting file on disk, read back the actual backup content to
confirm the original wasn't lost.

**Known limits, not yet addressed**: only one file at a time (whatever's
newest in the inbox), no way to point at a whole folder/project, 512KB
cap (refuses rather than silently uploading something huge). If you want
this to scale to "fix this whole project," that's a bigger scope — say so
and we'll design that properly rather than stretching this in an ad hoc
way.

**Update, round eleven — "just do everything we talked about."** Couldn't
literally do everything (a real chunk of the brainstorm — Canvas/LMS, Anki,
Obsidian, smart home — needs facts or credentials only the user has: which
LMS, whether they even use those tools, an actual vault/API location. Built
what was genuinely ready with zero new input needed.

**Google Calendar — wired in, confirmed genuinely working, not assumed.**
`claude mcp list` on the server already showed Slack/Gmail/Calendar/Indeed
as connected (tied to the user's own claude.ai account — nothing I set up).
Didn't trust "connected" at face value given the hallucination bugs found
two rounds ago — traced the actual tool-use events
(`--output-format=stream-json`) and confirmed a real
`mcp__claude_ai_Google_Calendar__list_events` call returned real Google API
data (the account's real email as calendar owner, real timezone). Full CRUD
included (create/update/delete/respond) — worth knowing delete_event is
in there, it can remove a real event if asked to.

**Found and fixed a real, subtle behavioral bug along the way**: adding
`Bash` to `--allowedTools` alongside the calendar MCP tool made Claude stop
bothering to even try `ToolSearch` to discover it — it would just say "I
don't have calendar access" without attempting the tool at all. Worked
fine with a short, calendar-only tool list; broke the instant Bash joined
the list. Fixed by explicitly telling it in the system prompt that these
specific MCP tools exist and to check for them before ever claiming no
access — confirmed fixed through the *actual* pipeline (`jarvis_client.py`,
full tool list, full persona), not just an isolated retest.

**Gmail — wired in, but not functional yet, and that's on the user, not
code.** The live trace showed `"connector isn't authorized with the
necessary scope yet"` — needs reauthorizing in claude.ai connector
settings (their click, not scriptable from here). Deliberately read-only
in the allowed list (search/get threads & messages, list labels/drafts) —
no send/reply/forward. That split was flagged as needing its own explicit
decision in the last brainstorm and hasn't been revisited since, so it
stayed conservative. Verified the persona correctly refuses a send
request ("I'm unable to send emails") rather than attempting or
hallucinating success.

**Still needs the user for the rest of the brainstorm**: which LMS
(Canvas/Blackboard/other) + personal access token, whether they use
Obsidian and where the vault lives, whether Anki/Zotero are part of their
workflow, whether they have any smart home devices at all. None of these
can be guessed correctly — wrong guesses here mean wasted building, not
just wasted asking.

**Update, round twelve (2026-09-16) — Claude's OAuth session expired,
Groq's identity fixed at the source, TTS switched for speed.**

- **Claude's server-side login expired overnight** — `Failed to authenticate:
  OAuth session expired and could not be refreshed`. Not caused by anything
  built here; the CLI's own session just logged out, breaking every
  `escalate_to_claude()` call. Re-authenticated via `claude auth login
  --claudeai` (explicitly NOT `--console`, which would silently switch to
  paid API billing — the thing guarded against since the very start of this
  project). Confirmed after: `authMethod: "claude.ai"`, `subscriptionType:
  "pro"`, no `ANTHROPIC_API_KEY` in the environment. Broke in under 24
  hours of prior working use, which is unusually fast for a normal
  expiration — flagged to the user as worth watching for a recurring
  pattern, not something I could fully diagnose from here (it's Anthropic-
  side token refresh behavior).
- **Fixed Groq's identity hallucination at the source instead of routing
  around it.** Two rounds ago, "what model are you" got hard-routed to
  Claude because Groq lied about being "OpenAI's GPT-4." User's explicit
  instruction this round: Groq should handle everything reasonably
  possible, Claude only for genuinely complex tasks — so routing identity
  questions away from Groq now works against the actual goal. Real fix:
  told Groq the truth explicitly in its system prompt ("you are running on
  Groq's cloud infrastructure, using openai/gpt-oss-20b — say exactly
  that"). Tested directly against the raw API first, then through the full
  pipeline: now answers honestly and fast (1.5-2.4s, not routed to
  Claude's slower path). Removed the now-dead `_IDENTITY_RE` hard-route.
- **TTS switched from Kokoro to Piper for speed** — measured real
  component-level latency rather than guessing: Whisper transcription
  ~0.75s, SSH+classify+Groq ~1.5-2.4s, Kokoro synthesis ~2.73s even for a
  short sentence (fixed per-call cost, not length-proportional) — total
  ~5-5.5s per turn. That's the actual floor with Kokoro; user wanted
  faster given the fast-Groq conversation style now in place. Measured
  Piper (already installed, was the original TTS before Kokoro, never
  actually got live user feedback on its quality) at ~0.72s — nearly 4x
  faster, cutting the expected total to ~2.9s. User's explicit choice
  after seeing the real numbers side by side (say ~0.5s/basic quality,
  Piper ~0.72s/decent quality, Kokoro ~2.73s/best quality, ElevenLabs
  would only add network latency — no speed win at all). Kokoro and macOS
  `say` both still available via `JARVIS_TTS_ENGINE` if quality ever
  matters more than speed again.

— built overnight by Claude, 2026-09-15
