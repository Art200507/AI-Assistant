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
        'voice/file_bridge.py', {'fix_file'},
        dict(
            Path=pathlib.Path, MAX_FILE_BYTES=524288, BACKUPS_DIR=backups,
            shutil=__import__('shutil'), time=__import__('time'), uuid=__import__('uuid'),
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
    # covering the same already-reflected window.
    activity_log = base / 'activity.log'
    activity_log.write_text(json.dumps({"timestamp": 100.0, "type": "conversation", "user": "hi", "assistant": "hello", "tier": "simple"}) + "\n")
    conv_log = base / 'conversation_log.md'
    learning_log = base / 'learning_log.md'
    watermark_file = base / '.last_reflected_ts'
    ask_calls = []

    def fake_ask_jarvis(instruction, **kwargs):
        ask_calls.append(instruction)
        return "### Summary\ntest\n\n### Corrections\nNone this session\n\n### Other lessons\nNothing further.\n"

    env = extract(
        'voice/learning.py',
        {'reflect_on_session', '_session_entries', '_load_reflection_watermark', '_save_reflection_watermark',
         '_format_entries', '_append_conversation_log', '_ensure_learning_log_preamble'},
        dict(
            json=json, time=__import__('time'), ACTIVITY_LOG=activity_log,
            CONVERSATION_LOG=conv_log, LEARNING_LOG=learning_log,
            REFLECTION_WATERMARK_FILE=watermark_file, ABOUT_JARVIS='about',
            ask_jarvis=fake_ask_jarvis, sync_lessons_now=Mock(),
        ),
    )
    env['reflect_on_session'](since_ts=0.0)
    assert len(ask_calls) == 1, 'the first, real reflection must actually run'
    first_log_text = learning_log.read_text()
    assert first_log_text.count('\n## ') == 1, 'exactly one dated entry after the first reflection'

    env['reflect_on_session'](since_ts=0.0)  # the shutdown safety net, moments later, same since_ts
    assert len(ask_calls) == 1, 'a second call covering an already-reflected window must be a no-op, not re-ask Claude'
    assert learning_log.read_text().count('\n## ') == 1, 'must not have written a duplicate lesson entry'
    print('FIXED: a second reflection covering an already-reflected window is now a clean no-op, not a duplicate')
