# Server source (mirror)

These four files are the actual "brain" — they run on the Oracle Cloud server (not the Mac), and until now existed only there, outside version control entirely. Both independent architecture audits of this project flagged that gap (a clean clone couldn't reproduce or review the real routing/retrieval/task logic at all).

This directory is a **mirror**, kept current by hand: edit here, then deploy with `scp` to `~/JARVIS/` on the server, `py_compile` there, swap the file in, and restart the relevant systemd service (`jarvis-mobile-api.service` for `jarvis_respond.py`/`rag.py`/`mobile_api.py`). It is **not** a self-contained deployable package — there's no setup script, and the files assume the server's existing directory layout (`~/jarvis-workspace/`, `~/.claude_oauth_token`, etc.) and already-installed dependencies (`waitress`, `scikit-learn`, the `claude` CLI). Before editing anything here, re-pull a fresh copy from the live server first — this mirror can drift stale between sessions.

- `jarvis_respond.py` — the router: fast tier (Groq), cloud reasoning tier (`claude` CLI), background task lifecycle, shared cross-tier conversation transcript.
- `rag.py` — TF-IDF knowledge-base retrieval over the `knowledge/` drop-folder.
- `mobile_api.py` — the Flask API backing the iOS PWA (`jarvis-mobile-api.service`).
- `orchestrator.py` — a separate, currently dormant task-queue automation service, not part of the active workflow.
