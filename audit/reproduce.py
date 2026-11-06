"""Offline reproductions: extract real functions without import-time side effects.
Run: python3 audit/reproduce.py (from JARVIS-local).
Assertions document current defects; these are audit probes, not desired behavior.
"""
import ast
import json
import pathlib
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = pathlib.Path(__file__).resolve().parents[1]

def extract(relative, names, env):
    tree = ast.parse((ROOT / relative).read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), relative, 'exec'), env)
    return env

with tempfile.TemporaryDirectory() as d:
    base = pathlib.Path(d)
    old = base / 'camera.jpg'
    old.write_bytes(b'old image')
    env = extract('voice/vision.py', {'capture_photo'}, dict(Path=pathlib.Path, subprocess=SimpleNamespace(run=Mock(return_value=SimpleNamespace(returncode=1))), CAPTURE_PATH=str(old), CAMERA_DEVICE='0'))
    assert env['capture_photo']() == str(old)
    print('CONFIRMED: failed camera capture accepts stale image')

    import re as _re
    lessons = base / 'lessons.md'
    lessons.write_text('# About Jarvis\n\n## old\n### Corrections\nAlways preserve this important rule\n' + ''.join(f'\n## recent{i}\n### Corrections\nNone this session\n' for i in range(9)))
    env = extract(
        'voice/learning.py', {'_recent_digest', '_extract_correction'},
        dict(
            LEARNING_LOG=lessons, ABOUT_JARVIS='about', LESSONS_IN_CONTEXT=8,
            MAX_CONTEXT_CHARS=4000, MAX_CORRECTIONS_CHARS=3000, re=_re,
            _CORRECTIONS_RE=_re.compile(r"### Corrections\s*\n(.*?)(?=\n### |\Z)", _re.DOTALL),
        ),
    )
    assert 'important rule' in env['_recent_digest'](), 'a correction more than LESSONS_IN_CONTEXT sessions back must still survive'
    print('FIXED: a correction from beyond the recent-sessions window is no longer silently evicted')

    runs = []
    def run_local(*args):
        runs.append(args)
        return SimpleNamespace(returncode=1, stderr='failure after partial work') if len(runs) == 1 else SimpleNamespace(returncode=0, stdout='done')
    env = extract('voice/local_claude.py', {'ask_local_claude'}, dict(_run_local_claude=run_local))
    env['ask_local_claude']('perform action', 'session')
    assert len(runs) == 1, 'a non-session-missing failure must not be silently reissued as a new session'
    print('FIXED: generic resume failure (not "session not found") no longer reissues as a new session')

    runs2 = []
    def run_local_missing(*args):
        runs2.append(args)
        return SimpleNamespace(returncode=1, stderr='Session not found: session') if len(runs2) == 1 else SimpleNamespace(returncode=0, stdout='done')
    env2 = extract('voice/local_claude.py', {'ask_local_claude'}, dict(_run_local_claude=run_local_missing))
    env2['ask_local_claude']('perform action', 'session')
    assert len(runs2) == 2, 'a genuine "session not found" must still fall back to minting a new session'
    print('CONFIRMED STILL WORKS: genuine session-missing resume failure still falls back correctly')

    local = base / 'source.py'
    local.write_text('new original')
    backups = base / 'backups'
    backups.mkdir()
    def download(remote, target):
        target.write_text('old remote output')
    env = extract(
        'voice/file_bridge.py', {'fix_file', '_hash_file'},
        dict(
            Path=pathlib.Path, MAX_FILE_BYTES=524288, BACKUPS_DIR=backups,
            shutil=__import__('shutil'), time=__import__('time'), uuid=__import__('uuid'),
            hashlib=__import__('hashlib'),
            REMOTE_INCOMING='/incoming', REMOTE_FIXED='/fixed', _ssh_run=Mock(),
            _scp_up=Mock(), ask_jarvis=Mock(return_value="The server didn't respond in time."),
            _scp_down=download, log_activity=Mock(),
            _is_transport_failure=lambda s: s.startswith((
                "The server didn't respond in time.", "I couldn't reach the server:",
                "Something went wrong on the server side:",
            )),
            subprocess=subprocess,
        ),
    )
    env['fix_file'](local)
    assert local.read_text() == 'new original', 'a failed generation must never touch the local file at all'
    print('FIXED: failed generation now leaves the local file untouched instead of downloading stale remote output')

    class Response:
        status_code = 200
        def json(self):
            return {'choices': [{'message': {'content': 'NONE: do not select LOCAL or CLOUD'}}]}
    env = extract('voice/command_router.py', {'classify_command'}, dict(GROQ_API_KEY='mock', GROQ_URL='mock', GROQ_MODEL='mock', _SYSTEM_PROMPT='', requests=SimpleNamespace(post=Mock(return_value=Response()))))
    assert env['classify_command']('hello') is None, 'an off-spec multi-word answer must be treated as a failed classification, not guessed'
    print('FIXED: off-spec multi-label answer now falls through to None (regex fallback) instead of matching CLOUD by substring')

    env = extract(
        'voice/always_on.py', {'DashboardCommandWatcher'},
        dict(
            json=json, time=__import__('time'), threading=threading, subprocess=subprocess,
            DASHBOARD_COMMAND_PATH=base / 'command.json', DASHBOARD_COMMAND_POLL_S=0.01,
            DASHBOARD_CONSUMED_IDS_FILE=base / 'command.consumed.json', DASHBOARD_COMMAND_MAX_AGE_S=30,
            MOBILE_COMMAND_REMOTE_PATH='/x', MOBILE_COMMAND_POLL_S=999,
            SSH_KEY='', SSH_USER='', SSH_HOST='', _SSH_MULTIPLEX_OPTS=[],
            _dashboard_interrupt_event=threading.Event(), _dashboard_keep_listening_event=threading.Event(),
        ),
    )
    Watcher = env['DashboardCommandWatcher']
    (base / 'command.json').write_text(json.dumps({'command': 'interrupt', 'id': 'replay-me', 'ts': __import__('time').time()}))
    w1 = Watcher()
    data = json.loads((base / 'command.json').read_text())
    w1._last_seen_id_local = data.get('id')
    w1._persist_consumed_ids()
    w2 = Watcher()  # simulated process restart, same command.json still on disk
    assert w2._last_seen_id_local == 'replay-me', 'a restart must not forget an already-consumed command id'
    print('FIXED: process restart no longer re-dispatches a command it already consumed before restarting')

    env = extract('voice/voice_state.py', {'write_state'}, dict(STATE_PATH=base/'state.json', json=json, time=__import__('time'), mobile_sync=SimpleNamespace(push_async=Mock())))
    # Deterministic interleaving of the fixed temporary filename used by two writers.
    temp = (base/'state.json').with_suffix('.json.tmp')
    temp.write_text('writer A')
    temp.write_text('writer B')
    temp.replace(base/'state.json')
    try:
        temp.replace(base/'state.json')
    except FileNotFoundError:
        print('CONFIRMED: shared temp pathname cannot support overlapping writers')
    else:
        raise AssertionError('expected missing temporary file')

    # always_on.py triggers reflect_on_session both at a conversation's
    # natural end and again as a shutdown safety net, often moments
    # later with the same (or an even earlier) since_ts -- simulating
    # exactly that: one real reflection, then an immediate second call
    # covering the same already-reflected window. Also covers F03 (watermark
    # must not overshoot into never-read activity), F04 (two concurrent
    # processes must not duplicate a lesson), F05 (the 4th ask_jarvis
    # failure string), and F16 (a failed attempt must not duplicate the
    # raw transcript archive on retry).
    import fcntl as _fcntl, threading as _threading, time
    activity_log = base / 'activity.log'
    activity_log.write_text(json.dumps({"timestamp": 100.0, "type": "conversation", "user": "hi", "assistant": "hello", "tier": "simple"}) + "\n")
    conv_log = base / 'conversation_log.md'
    learning_log = base / 'learning_log.md'
    watermark_file = base / '.last_reflected_ts'
    lock_file = base / '.reflection.lock'
    ask_calls = []

    def fake_ask_jarvis(instruction, **kwargs):
        ask_calls.append(instruction)
        return "### Summary\ntest\n\n### Corrections\nNone this session\n\n### Other lessons\nNothing further.\n"

    def make_env(ask_jarvis_fn):
        return extract(
            'voice/learning.py',
            {'reflect_on_session', '_session_entries', '_load_reflection_watermark', '_save_reflection_watermark',
             '_format_entries', '_append_conversation_log', '_ensure_learning_log_preamble',
             '_is_transport_failure'},
            dict(
                json=json, time=__import__('time'), fcntl=_fcntl, ACTIVITY_LOG=activity_log,
                CONVERSATION_LOG=conv_log, LEARNING_LOG=learning_log,
                REFLECTION_WATERMARK_FILE=watermark_file, REFLECTION_LOCK_FILE=lock_file,
                ABOUT_JARVIS='about', ask_jarvis=ask_jarvis_fn, sync_lessons_now=Mock(),
                _ASK_JARVIS_FAILURE_PREFIXES=(
                    "I couldn't reach", "The server didn't",
                    "Something went wrong on the server side", "The server gave me an empty response.",
                ),
            ),
        )

    env = make_env(fake_ask_jarvis)
    env['reflect_on_session'](since_ts=0.0)
    assert len(ask_calls) == 1, 'the first, real reflection must actually run'
    first_log_text = learning_log.read_text()
    assert first_log_text.count('\n## ') == 1, 'exactly one dated entry after the first reflection'

    env['reflect_on_session'](since_ts=0.0)  # the shutdown safety net, moments later, same since_ts
    assert len(ask_calls) == 1, 'a second call covering an already-reflected window must be a no-op, not re-ask Claude'
    assert learning_log.read_text().count('\n## ') == 1, 'must not have written a duplicate lesson entry'
    print('FIXED: a second reflection covering an already-reflected window is now a clean no-op, not a duplicate')

    # F03: an entry logged AFTER the first reflection's own entries, but
    # before the NEXT reflection call, must still be picked up -- the old
    # bug advanced the watermark to time.time() ("now", well past this
    # entry's own timestamp of 150), silently excluding it forever even
    # though it was never actually read by the first reflection either.
    with activity_log.open('a') as f:
        f.write(json.dumps({"timestamp": 150.0, "type": "conversation", "user": "later", "assistant": "reply"}) + "\n")
    env['reflect_on_session'](since_ts=0.0)
    assert len(ask_calls) == 2, 'activity logged between reflections must still be read by the next one, not skipped'
    assert 'later' in ask_calls[-1], 'the skipped entry\'s own content must actually reach the model'
    print('FIXED: the watermark advances only to the last entry actually read, not to wall-clock "now" -- later activity is not silently skipped')

    # F16: a failed reflection (transport error) must not duplicate the raw
    # transcript archive when the SAME window is retried and succeeds.
    with activity_log.open('a') as f:
        f.write(json.dumps({"timestamp": 200.0, "type": "conversation", "user": "third", "assistant": "reply"}) + "\n")
    calls = {"n": 0}
    def flaky_ask_jarvis(instruction, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "The server gave me an empty response."  # F05's 4th failure string
        return "### Summary\nok\n\n### Corrections\nNone this session\n\n### Other lessons\nNothing further.\n"
    env2 = make_env(flaky_ask_jarvis)
    before_watermark = _fcntl and (watermark_file.read_text() if watermark_file.exists() else None)
    env2['reflect_on_session'](since_ts=0.0)  # fails -- the 4th failure string must be recognized, watermark must not move
    assert (watermark_file.read_text() if watermark_file.exists() else None) == before_watermark, 'a failed reflection must not advance the watermark'
    env2['reflect_on_session'](since_ts=0.0)  # retries the SAME window, now succeeds
    assert calls["n"] == 2
    assert conv_log.read_text().count('third') == 1, 'a failed-then-retried reflection must not duplicate the transcript archive'
    print('FIXED: the 4th ask_jarvis failure string is recognized, and a failed-then-retried reflection no longer duplicates the transcript archive')

    # F04: two reflection PROCESSES racing (simulated here as two threads,
    # each with its own open fd on the lock file -- flock blocks across
    # file descriptors, including within one process, the same as it
    # would across two real OS processes) must not both write a lesson
    # for the same window. Thread A holds the lock through a slow
    # (simulated) model call; thread B, started while A is still inside
    # it, must BLOCK on the lock rather than run concurrently -- by the
    # time B gets in, A has already advanced the watermark, so B finds
    # nothing left to do.
    with activity_log.open('a') as f:
        f.write(json.dumps({"timestamp": 300.0, "type": "conversation", "user": "fourth", "assistant": "reply"}) + "\n")
    race_calls = {"n": 0}
    a_started = _threading.Event()
    def racing_ask_jarvis(instruction, **kwargs):
        race_calls["n"] += 1
        a_started.set()
        time.sleep(0.3)  # stand-in for the real ~10-120s model call
        return "### Summary\nrace\n\n### Corrections\nNone this session\n\n### Other lessons\nNothing further.\n"
    env3 = make_env(racing_ask_jarvis)
    errors = []
    def worker():
        try:
            env3['reflect_on_session'](since_ts=0.0)
        except Exception as e:
            errors.append(e)
    t_a = _threading.Thread(target=worker)
    t_a.start()
    assert a_started.wait(timeout=3), 'thread A never reached its model call'
    t_b = _threading.Thread(target=worker)  # started while A is still "working" -- must block on the lock, not race
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)
    assert not errors, f'concurrent reflections must not raise: {errors}'
    assert race_calls["n"] == 1, f'the lock must block thread B until A finishes, at which point the watermark already covers the window -- got {race_calls["n"]} real model calls, expected exactly 1'
    print('FIXED: two reflection processes racing on the same window are serialized by the lock into exactly one real reflection')

    import re as _re2
    env = extract(
        'voice/jarvis_client.py', {'_clean'},
        dict(
            _ANSI_RE=_re2.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r"),
            _SOURCES_BLOCK_RE=_re2.compile(r"\n\n[ \t]*sources?:.*$", _re2.IGNORECASE | _re2.DOTALL),
            _MD_LINK_RE=_re2.compile(r"\[([^\]]+)\]\(https?://[^\)]+\)"),
            _BARE_URL_RE=_re2.compile(r"https?://\S+"),
            _BACKTICK_RE=_re2.compile(r"`([^`]*)`"),
            _MD_EMPHASIS_RE=_re2.compile(r"(\*{1,2})([^*]+)\1"),
        ),
    )
    real_content = env['_clean']('Use source: config.json. Never overwrite production.')
    assert real_content == 'Use source: config.json. Never overwrite production.', real_content
    footer_stripped = env['_clean']('Here is the answer.\n\nSources: [1](https://a.com)')
    assert footer_stripped == 'Here is the answer.', footer_stripped
    print('FIXED: "source:" embedded mid-sentence no longer destroys real content, while a real trailing citations block is still stripped')

    logged = []
    env = extract('voice/always_on.py', {'_record_local_turn_logged'}, dict(record_local_turn=lambda *a, **k: False, log_activity=lambda e: logged.append(e)))
    env['_record_local_turn_logged']('sid-1', 'hi', 'hello')
    assert len(logged) == 1 and logged[0]['type'] == 'cross_tier_sync_failed', logged
    logged.clear()
    env2 = extract('voice/always_on.py', {'_record_local_turn_logged'}, dict(record_local_turn=lambda *a, **k: True, log_activity=lambda e: logged.append(e)))
    env2['_record_local_turn_logged']('sid-1', 'hi', 'hello')
    assert len(logged) == 0, 'a successful sync must not log anything extra'
    print('FIXED: a failed local-to-shared memory sync is now logged instead of silently discarded')

    # F17: fix_file must not silently discard a concurrent user edit. The
    # mocked ask_jarvis call stands in for the real ~180s server round
    # trip -- as a side effect, it edits local_path itself, simulating the
    # user continuing to work on the file while that call was in flight.
    import hashlib as _hashlib
    file_bridge_env = dict(
        Path=pathlib.Path, shutil=__import__('shutil'), time=__import__('time'), uuid=__import__('uuid'),
        hashlib=_hashlib, json=json, subprocess=subprocess,
        BACKUPS_DIR=base/'backups', REMOTE_INCOMING='/incoming', REMOTE_FIXED='/fixed',
        MAX_FILE_BYTES=524288, ACTIVITY_LOG=base/'fb_activity.log',
        _ASK_JARVIS_FAILURE_PREFIXES=("The server didn't respond in time.", "I couldn't reach the server:", "Something went wrong on the server side:"),
    )
    (base/'backups').mkdir(exist_ok=True)

    conflict_file = base / 'conflict.py'
    conflict_file.write_text('ORIGINAL')
    def ask_jarvis_with_concurrent_edit(instruction, **kwargs):
        conflict_file.write_text('USER EDITED DURING WAIT')  # the "concurrent edit" this fix guards against
        return 'Fixed a real bug.'
    env = extract(
        'voice/file_bridge.py', {'fix_file', '_hash_file', '_is_transport_failure', 'log_activity'},
        dict(file_bridge_env, _ssh_run=Mock(), _scp_up=Mock(),
             _scp_down=lambda remote, local: local.write_text('FIXED CONTENT'),
             ask_jarvis=ask_jarvis_with_concurrent_edit),
    )
    result = env['fix_file'](conflict_file)
    assert conflict_file.read_text() == 'USER EDITED DURING WAIT', 'a concurrent edit must never be silently overwritten'
    fix_sibling = base / 'conflict.jarvis-fix.py'
    assert fix_sibling.exists() and fix_sibling.read_text() == 'FIXED CONTENT', 'the fix must be saved to a sibling file instead'
    assert 'while I was working on it' in result, result
    print('FIXED: a concurrent edit during fix_file is detected and preserved, with the fix saved to a sibling file instead of overwriting it')

    clean_file = base / 'clean.py'
    clean_file.write_text('ORIGINAL')
    env = extract(
        'voice/file_bridge.py', {'fix_file', '_hash_file', '_is_transport_failure', 'log_activity'},
        dict(file_bridge_env, _ssh_run=Mock(), _scp_up=Mock(),
             _scp_down=lambda remote, local: local.write_text('FIXED CONTENT'),
             ask_jarvis=lambda instruction, **k: 'Fixed a real bug.'),
    )
    env['fix_file'](clean_file)
    assert clean_file.read_text() == 'FIXED CONTENT', 'the normal, no-conflict path must still apply the fix as before'
    print('CONFIRMED STILL WORKS: the non-conflict fix_file path is unaffected by the new hash check')

    # Found during round 2's own review loop: the new cross_tier_sync_failed
    # diagnostic entry (F08) has neither "tier"/"user"/"assistant" (which
    # _format_entries expects) nor "user"/"assistant" (which
    # _recent_activity_text expects) -- both formatters used to render it
    # as a blank, meaningless "You: | Jarvis:" line, in one case feeding
    # noise into the reflection prompt, in the other defeating the whole
    # point of logging it (the spoken "read me the logs" recovery path).
    env = extract('voice/learning.py', {'_format_entries'}, {})
    diag_entry = {'type': 'cross_tier_sync_failed', 'session_id': 'x', 'direction': 'local_to_shared'}
    real_entry = {'type': 'conversation', 'tier': 'simple', 'user': 'hi', 'assistant': 'hello'}
    formatted = env['_format_entries']([diag_entry, real_entry])
    assert 'sync_failed' not in formatted and 'You:  |' not in formatted, formatted
    assert formatted == '[simple] You: hi | Jarvis: hello', formatted
    print('FIXED: the diagnostic sync-failure entry is skipped in the reflection transcript instead of appearing as a blank line')

    env = extract('voice/always_on.py', {'_recent_activity_text'}, dict(json=json, ACTIVITY_LOG=base/'ra_activity.log', LOG_SUMMARY_LINES=50))
    (base/'ra_activity.log').write_text(json.dumps({'when': 't1', 'type': 'cross_tier_sync_failed', 'direction': 'local_to_shared'}) + '\n')
    spoken = env['_recent_activity_text']()
    assert 'background memory sync failed (local_to_shared)' in spoken, spoken
    assert 'You:' not in spoken, spoken
    print('FIXED: "read me the logs" now speaks the sync failure meaningfully instead of a blank You:/Jarvis: line')

    # server/jarvis_respond.py's escalate_to_claude had the exact same
    # unsafe-retry pattern already fixed in local_claude.py (round 1) --
    # found during a fresh bug-hunt pass, never fixed here before. A
    # nonzero exit for ANY reason (not just a genuinely missing session)
    # used to get blindly reissued as a brand-new session on the CLOUD
    # tier, which has real Bash/Write/Calendar/Gmail access -- repeating
    # a real action after a mid-task crash is not safe to assume.
    def make_escalate_env(run_claude_fn, poll_task_fn, start_bg_fn):
        return extract(
            'server/jarvis_respond.py', {'escalate_to_claude', '_is_session_missing_error'},
            dict(
                _load_transcript=lambda sid: [], _format_transcript=lambda h: '',
                _run_claude=run_claude_fn, start_background_task=start_bg_fn,
                _poll_task=poll_task_fn, _auto_sync_completed_task=Mock(),
                BACKGROUND_TASK_POLL_S=5,
                _SESSION_MISSING_MARKERS=("session not found", "no conversation found"),
            ),
        )

    # Synchronous path (allow_background=False): a genuine mid-task crash
    # (not "session not found") must NOT trigger a second, fresh attempt.
    sync_calls = []
    def crashing_run_claude(full_input, extra_args):
        sync_calls.append(extra_args)
        return SimpleNamespace(returncode=1, stderr='Error: tool execution failed after writing output.txt', stdout='partial result')
    env = make_escalate_env(crashing_run_claude, None, None)
    result = env['escalate_to_claude']('do something', session_id='11111111-1111-1111-1111-111111111111', allow_background=False)
    assert len(sync_calls) == 1, f'a non-session-missing crash must not be retried as a new session, got {len(sync_calls)} attempts'
    print('FIXED (sync path): a mid-task crash unrelated to session lookup is no longer blindly reissued as a new session')

    # Synchronous path: a GENUINE session-not-found must still retry correctly.
    sync_calls2 = []
    def missing_session_run_claude(full_input, extra_args):
        sync_calls2.append(extra_args)
        if extra_args and extra_args[0] == '--resume':
            return SimpleNamespace(returncode=1, stderr='Error: No conversation found with session ID: x', stdout='')
        return SimpleNamespace(returncode=0, stderr='', stdout='fresh session answer')
    env = make_escalate_env(missing_session_run_claude, None, None)
    result = env['escalate_to_claude']('do something', session_id='22222222-2222-2222-2222-222222222222', allow_background=False)
    assert len(sync_calls2) == 2 and result == 'fresh session answer', (sync_calls2, result)
    print('CONFIRMED STILL WORKS (sync path): a genuine session-not-found still falls back to a fresh session correctly')

    # Background path (allow_background=True, the default for real voice
    # turns and the phone's /api/chat): same distinction.
    bg_starts = []
    def crashing_start_bg(full_input, extra_args, session_id=None, auto_sync=True):
        bg_starts.append(extra_args)
        return 'task-1'
    def crashing_poll(task_id, max_wait_s):
        return True, 1, 'Error: tool execution failed after writing output.txt'
    env = make_escalate_env(None, crashing_poll, crashing_start_bg)
    env['escalate_to_claude']('do something', session_id='33333333-3333-3333-3333-333333333333', allow_background=True)
    assert len(bg_starts) == 1, f'a non-session-missing background crash must not start a second background task, got {len(bg_starts)}'
    print('FIXED (background path): a mid-task crash unrelated to session lookup no longer starts a redundant second background task')

    bg_starts2 = []
    def missing_session_start_bg(full_input, extra_args, session_id=None, auto_sync=True):
        bg_starts2.append(extra_args)
        return f'task-{len(bg_starts2)}'
    def missing_session_poll(task_id, max_wait_s):
        if task_id == 'task-1':
            return True, 1, 'Error: No conversation found with session ID: x'
        return True, 0, 'fresh session answer'
    env = make_escalate_env(None, missing_session_poll, missing_session_start_bg)
    result = env['escalate_to_claude']('do something', session_id='44444444-4444-4444-4444-444444444444', allow_background=True)
    assert len(bg_starts2) == 2 and result == 'fresh session answer', (bg_starts2, result)
    print('CONFIRMED STILL WORKS (background path): a genuine session-not-found still starts a real fresh-session retry')

    # _append_transcript was an unguarded read-modify-write: a background
    # task's completion and a brand-new voice/chat turn for the SAME
    # session can genuinely race (and mobile_api.py's Flask app serves
    # concurrent requests on separate threads in one process, so two
    # /api/chat calls for the same session race too) -- one write used to
    # silently clobber the other instead of both landing.
    import threading as _threading2
    sessions_dir = base / 'sessions'
    sessions_dir.mkdir(exist_ok=True)
    sid = 'race-session'
    env = extract(
        'server/jarvis_respond.py', {'_append_transcript', '_load_transcript', '_transcript_path', '_transcript_lock_path'},
        dict(json=json, fcntl=__import__('fcntl'), SESSIONS_DIR=sessions_dir),
    )
    errors = []
    def append_worker(label):
        try:
            env['_append_transcript'](sid, f'turn from {label}', f'answer from {label}', 'simple')
        except Exception as e:
            errors.append(e)
    threads = [_threading2.Thread(target=append_worker, args=(f'writer{i}',)) for i in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=10)
    assert not errors, f'concurrent transcript appends must not raise: {errors}'
    final_history = env['_load_transcript'](sid)
    assert len(final_history) == 2, f'both concurrent appends must survive, not clobber each other -- got {len(final_history)} entries: {final_history}'
    print('FIXED: two concurrent transcript appends for the same session no longer silently clobber one another')

    # jarvis_voice.py's local-whisper fallback: found via a fresh bug-hunt
    # pass + direct reproduction that whisper-cli, given --prompt text,
    # can hallucinate a plausible phrase from pure silence instead of
    # returning empty -- and separately had zero guard against a
    # hung/missing binary crashing the whole always-on process.
    import wave as _wave, struct as _struct
    silent_wav = str(base / 'silent.wav')
    with _wave.open(silent_wav, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(_struct.pack('<16000h', *([0] * 16000)))
    loud_wav = str(base / 'loud.wav')
    with _wave.open(loud_wav, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(_struct.pack('<16000h', *([6000, -6000] * 8000)))

    def hallucinating_run(*args, **kwargs):
        # Stands in for the real whisper-cli: on genuine silence it would
        # still "succeed" (returncode 0) but hallucinate text lifted from
        # the --prompt -- this is what _is_near_silence must intercept
        # BEFORE subprocess.run is ever called at all.
        raise AssertionError('whisper-cli must never be invoked for near-silent input')

    env = extract(
        'voice/jarvis_voice.py', {'_transcribe_local', '_is_near_silence'},
        dict(
            wave=_wave, struct=_struct, subprocess=SimpleNamespace(run=hallucinating_run, TimeoutExpired=subprocess.TimeoutExpired),
            WHISPER_BIN='whisper-cli', WHISPER_MODEL='model.bin', WHISPER_PROMPT='Jarvis, are you there?',
            LOCAL_STT_SILENCE_RMS=400.0,
        ),
    )
    assert env['_is_near_silence'](silent_wav) is True
    assert env['_is_near_silence'](loud_wav) is False
    assert env['_transcribe_local'](silent_wav) == '', 'near-silent input must never reach whisper-cli, let alone trust its hallucinated output'
    print('FIXED: near-silent input is intercepted before whisper-cli can hallucinate text from it')

    def crashing_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd='whisper-cli', timeout=60)
    env2 = extract(
        'voice/jarvis_voice.py', {'_transcribe_local', '_is_near_silence'},
        dict(
            wave=_wave, struct=_struct, subprocess=SimpleNamespace(run=crashing_run, TimeoutExpired=subprocess.TimeoutExpired),
            WHISPER_BIN='whisper-cli', WHISPER_MODEL='model.bin', WHISPER_PROMPT='Jarvis, are you there?',
            LOCAL_STT_SILENCE_RMS=400.0,
        ),
    )
    assert env2['_transcribe_local'](loud_wav) == '', 'a hung/crashed whisper-cli must degrade this one turn, not crash the whole process'
    print('FIXED: a hung or missing whisper-cli binary no longer crashes the whole always-on process')

    class _StuckPlayer:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): raise subprocess.TimeoutExpired(cmd='afplay', timeout=timeout)
        def kill(self): _StuckPlayer.killed = True
    _StuckPlayer.killed = False
    env = extract(
        'voice/jarvis_voice.py', {'_play_wav_with_avatar_sync'},
        dict(
            subprocess=SimpleNamespace(Popen=lambda *a, **k: _StuckPlayer(), TimeoutExpired=subprocess.TimeoutExpired),
            time=__import__('time'), write_state=Mock(),
            _is_valid_wav=lambda p: True, _amplitude_envelope=lambda p: [],
            CHUNK_MS=20,
        ),
    )
    env['_play_wav_with_avatar_sync']('/fake.wav')
    assert _StuckPlayer.killed, 'a stuck afplay must be killed, not left to raise an uncaught TimeoutExpired'
    print('FIXED: a stuck afplay process (e.g. a Bluetooth audio route change) no longer crashes the whole process')

    env = extract(
        'voice/mic_device.py', {'list_avfoundation_audio_devices', 'resolve_audio_device'},
        dict(os=__import__('os'), re=__import__('re'), subprocess=SimpleNamespace(run=Mock(side_effect=FileNotFoundError('ffmpeg not found')), TimeoutExpired=subprocess.TimeoutExpired)),
    )
    assert env['list_avfoundation_audio_devices']() == []
    assert env['resolve_audio_device']() == '1', 'must fall back to the existing last-resort guess, not crash at import time'
    print('FIXED: a missing/hung ffmpeg no longer crashes mic device resolution (called at jarvis_voice.py import time)')

    # phone/twilio_server.py's _caller_is_owner() -- guarded, not a hard
    # dependency of this suite: Flask isn't installed for plain `python3`
    # in every environment (it's a server-side/scaffold-only dependency),
    # so skip cleanly rather than fail the whole suite over an optional
    # dependency for a still-inert scaffold feature.
    try:
        from flask import Flask as _Flask, request as _request
        env = extract(
            'phone/twilio_server.py', {'_caller_is_owner'},
            dict(request=_request, config=SimpleNamespace(JARVIS_OWNER_PHONE_NUMBER='+15551234567')),
        )
        _app = _Flask(__name__)
        with _app.test_request_context('/voice', method='POST', data={'From': '+15551234567', 'To': '+15559999999'}):
            assert env['_caller_is_owner']() is True, 'inbound call from the owner must still be recognized'
        with _app.test_request_context('/voice', method='POST', data={'From': '+15559999999', 'To': '+15551234567'}):
            assert env['_caller_is_owner']() is True, 'an outbound call Jarvis placed TO the owner must now be recognized too'
        with _app.test_request_context('/voice', method='POST', data={'From': '+15550000000', 'To': '+15559999999'}):
            assert env['_caller_is_owner']() is False, 'a call involving neither the owner number must still fail the check'
        print('FIXED: an outbound call Jarvis itself placed to the owner is no longer rejected as "not the owner"')
    except ImportError:
        print('SKIPPED: phone/twilio_server.py owner-check probe (flask not installed in this environment)')
