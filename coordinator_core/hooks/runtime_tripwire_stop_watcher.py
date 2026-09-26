"""
coordinator_core.hooks.runtime_tripwire_stop_watcher — engine-side landing
of DoE-claude's `coordinator/hooks/scripts/runtime-tripwire-stop-watcher.py`
(Runtime Tripwire asyncRewake L2 backstop).

STOOD DOWN — carried forward from the source script's own docstring, and
still true post-port: this module registers NO `hooks.<name>` op (no
`register_op` call, not listed in `coordinator_core/hooks/__init__.py`'s
`_EAGER_HOOK_MODULES` — that file is out of this chunk's declared footprint,
and there is nothing to wire it to: DoE-claude's own `coordinator/hooks/
hooks.json` carries no `Stop` registration for this script either, "Stood
down 2026-07-31 per PM ruling; reversible, comment-only mention remains in
hooks.json"). A bug found here is latent, not live — fixing one changes no
session's behaviour until a future, PM-ruled restoration re-registers it
(restore procedure: `stop-dispatch.py`, unchanged by this port). This port
exists so the code lives in claude-klabauter rather than DoE-claude, per this plan's
own "DoE holds no scripts" mandate — it does not reactivate the watcher.

Self-contained (no engine op exists for this hook, same as the source
script's own "no claude-klabauter op exists for this hook" note at port time — this
module carries the full decision logic directly, no thin-stub/engine
split). Runs as a standalone CLI entry point (`python3 -m coordinator_core.
hooks.runtime_tripwire_stop_watcher` or `--watch ...` for the detached
child) exactly like the source script did — reading `sys.stdin`/argv, never
a `params["payload"]` dict — because that CLI shape is the actual mechanism
here: Step 8's detached re-launch (`_spawn_detached`) execs THIS FILE again
with `--watch <args>`, and a real, separately-scheduled OS process is the
only way an asyncRewake wake can outlive the parent hook invocation. Wrapping
that in a payload-dict-in/response-out `hooks.*` op contract would not
change this fact — the detached child still needs a real argv to exec — so
this module keeps its original CLI shape rather than adopting one it has no
use for while stood down. Restoration is a hooks.json-registration decision
(PM call), not a shape decision this chunk makes for it.

Three sibling-import adaptations (class 1, same displacement class already
landed for `worktree_isolation_strip.py`/`foreground_dispatch_strip.py` at
W4-C3/W4-C4), each a direct import of the already-landed claude-klabauter-engine
equivalent in place of the source's own `sys.path`-inserted, same-directory
DoE sibling:
  - `_session_hub.session_id_is_real` -> `coordinator_core.hooks.support.
    session_hub.session_id_is_real` (byte-identical contract).
  - `_git_common_dir.resolve_git_common_dir` -> `coordinator_core.hooks.
    support.git_common_dir.resolve_git_common_dir` (byte-identical
    contract, same "empty string means skip" fail-open shape this module's
    own callers already expect).
  - `_git_root()`'s spawn-fallback form (`git rev-parse --show-toplevel`
    subprocess) is REPLACED, not merely deferred, with `coordinator_core.
    git.repo_root.show_toplevel` — the engine's zero-spawn-only walk
    primitive. `show_toplevel`'s own docstring documents this exact
    fallback never having won a case the walk had not already produced
    (measured 2026-08-19); `worktree_isolation_strip.py`'s W4-C4 adaptation
    made and recorded the identical call on the identical question.
  - `resolve_wiki_citation` (from the source's own `_message_envelope`
    sibling) is DROPPED, not adapted: grepping the source script's own body
    for any call site beyond its own import/fallback-definition lines finds
    none — it is an unused import in the source, carrying no behaviour to
    port.

Everything else — the loop-guard invariant, the PID-lock mechanics
(Windows-safe `_pid_alive`, never raw `kill -0`), the detached-launch
mechanics (`start_new_session=True` on POSIX; `CREATE_NO_WINDOW |
CREATE_NEW_PROCESS_GROUP` with best-effort `CREATE_BREAKAWAY_FROM_JOB` on
Windows, never bare `DETACHED_PROCESS`), the dispatch-row parsing, the
threshold computation, the wake-condition recheck, and the lock cleanup on
every exit path — is a byte-faithful port; see each function's own
docstring/comments (carried verbatim from the source) for the per-mechanism
rationale.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md (chunk
W4-C13); docs/plans/2026-06-15-runtime-tripwire-idle-em-layered-fix.md §
C2a; docs/wiki/runtime-tripwire.md § L2; DoE-claude `coordinator/hooks/
scripts/runtime-tripwire-stop-watcher.py` (source, 594 lines).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks.support.git_common_dir import resolve_git_common_dir as _resolve_git_common_dir
from coordinator_core.hooks.support.session_hub import session_id_is_real

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def runtime_threshold_minutes(model: str) -> int:
    m = model or ""
    opus_default = int(os.environ.get("RUNTIME_TRIPWIRE_OPUS_MIN", "25"))
    sonnet_default = int(os.environ.get("RUNTIME_TRIPWIRE_SONNET_MIN", "12"))
    haiku_default = int(os.environ.get("RUNTIME_TRIPWIRE_HAIKU_MIN", "10"))
    if "[1m]" in m or "-1m" in m:
        return opus_default
    if "opus" in m:
        return opus_default
    if "sonnet" in m:
        return sonnet_default
    if "haiku" in m:
        return haiku_default
    return opus_default


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.wintypes.DWORD()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return bool(ok) and exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False
        return True


def _read_stdin(timeout: float = 2.0) -> str:
    box = {"data": ""}

    def _read() -> None:
        try:
            box["data"] = sys.stdin.read()
        except Exception:
            box["data"] = ""

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    return box["data"]


def _git_root() -> str:
    try:
        return show_toplevel(os.getcwd()) or ""
    except Exception:
        return ""


def _agent_completed(completion_log: Path, agent_id: str) -> bool:
    """Mirrors bash: grep -q "\"agentId\":[[:space:]]*\"<id>\"" COMPLETION_LOG."""
    if not completion_log.is_file():
        return False
    try:
        text = completion_log.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    pattern = r'"agentId"\s*:\s*"' + re.escape(agent_id) + r'"'
    return re.search(pattern, text) is not None


def _parse_dispatch_row(line: str) -> Optional[tuple]:
    fields = line.rstrip("\n").split("\t")
    agent_id = fields[0] if len(fields) > 0 else ""
    model = fields[1] if len(fields) > 1 else ""
    dispatched_at_raw = fields[3] if len(fields) > 3 else ""
    if not agent_id:
        return None
    if not re.fullmatch(r"[0-9]+", dispatched_at_raw or ""):
        return None
    dispatched_at = int(dispatched_at_raw)
    if dispatched_at == 0:
        return None
    return (agent_id, model, dispatched_at)


def main() -> int:
    try:
        return _main_impl()
    except Exception:
        return 0


def _main_impl() -> int:
    hook_input_raw = _read_stdin(2.0)

    stop_hook_active = False
    payload: dict = {}
    try:
        parsed = json.loads(hook_input_raw) if hook_input_raw.strip() else {}
        if isinstance(parsed, dict):
            payload = parsed
            stop_hook_active = bool(payload.get("stop_hook_active", False))
    except Exception:
        m = re.search(r'"stop_hook_active"\s*:\s*(true|false)', hook_input_raw)
        stop_hook_active = bool(m and m.group(1) == "true")

    if stop_hook_active:
        return 0

    # --- Step 2: GIT_ROOT discovery ---
    git_root = _git_root()
    if not git_root:
        return 0

    # --- Step 3: SESSION_ID extraction ---
    session_id = ""
    v = payload.get("session_id")
    if isinstance(v, str) and v:
        session_id = v
    if not session_id:
        m = re.search(r'"session_id"\s*:\s*"([^"]*)"', hook_input_raw)
        if m:
            session_id = m.group(1)
    if not session_id:
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        return 0
    if not session_id_is_real(session_id):
        return 0

    common_dir = _resolve_git_common_dir(git_root)
    if not common_dir:
        return 0
    sessions_dir = Path(common_dir) / "coordinator-sessions"
    lock_dir = sessions_dir / session_id
    lock = lock_dir / "stop-watcher.pid"

    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        sys.stderr.write(f"RUNTIME TRIPWIRE: lock dir creation failed ({lock_dir}): {exc}\n")

    if lock.is_file():
        try:
            existing_pid_text = lock.read_text(encoding="utf-8", errors="replace").strip()
            existing_pid = int(existing_pid_text) if existing_pid_text else 0
        except Exception:
            existing_pid = 0
        if _pid_alive(existing_pid):
            return 0

    dispatch_file = sessions_dir / session_id / "dispatched-agents.txt"
    completion_log = sessions_dir / "logs" / "agent-audit.jsonl"

    if not dispatch_file.is_file():
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass
        return 0

    max_track_minutes = int(os.environ.get("RUNTIME_TRIPWIRE_MAX_TRACK_MIN", "90"))
    now = int(time.time())

    earliest_agent_id = ""
    earliest_model = ""
    earliest_dispatched_at = 0

    try:
        lines = dispatch_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        lines = []

    for line in lines:
        row = _parse_dispatch_row(line)
        if row is None:
            continue
        agent_id, model, dispatched_at = row
        elapsed_min = (now - dispatched_at) // 60
        if elapsed_min >= max_track_minutes:
            continue
        if _agent_completed(completion_log, agent_id):
            continue
        if earliest_dispatched_at == 0 or dispatched_at < earliest_dispatched_at:
            earliest_agent_id = agent_id
            earliest_model = model
            earliest_dispatched_at = dispatched_at

    if not earliest_agent_id:
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass
        return 0

    # --- Step 7: compute SLEEP_SEC ---
    threshold_min = runtime_threshold_minutes(earliest_model)
    threshold_sec = threshold_min * 60
    target_epoch = earliest_dispatched_at + threshold_sec
    sleep_sec = max(0, target_epoch - now)

    watch_argv = [
        sys.executable,
        os.path.abspath(__file__),
        "--watch",
        str(lock),
        str(dispatch_file),
        str(completion_log),
        earliest_agent_id,
        earliest_model,
        str(earliest_dispatched_at),
        str(max_track_minutes),
        str(sleep_sec),
    ]

    child_pid = _spawn_detached(watch_argv)
    if child_pid is not None:
        try:
            lock.write_text(str(child_pid), encoding="utf-8", newline="\n")
        except Exception as exc:
            sys.stderr.write(f"RUNTIME TRIPWIRE: lock write failed ({lock}): {exc}\n")

    return 0


def _spawn_detached(argv: list) -> Optional[int]:
    common_kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    try:
        if os.name == "nt":
            flags = _NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
            try:
                proc = subprocess.Popen(
                    argv,
                    creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB,
                    **common_kwargs,
                )
            except OSError:
                proc = subprocess.Popen(argv, creationflags=flags, **common_kwargs)
        else:
            proc = subprocess.Popen(argv, start_new_session=True, **common_kwargs)
        return proc.pid
    except Exception:
        return None


def _watch_main(args: list) -> int:
    try:
        (
            lock_s,
            dispatch_file_s,
            completion_log_s,
            earliest_agent_id,
            earliest_model,
            earliest_dispatched_at_s,
            max_track_minutes_s,
            sleep_sec_s,
        ) = args
    except ValueError:
        return 0

    lock = Path(lock_s)
    dispatch_file = Path(dispatch_file_s)
    completion_log = Path(completion_log_s)
    earliest_dispatched_at = int(earliest_dispatched_at_s)
    max_track_minutes = int(max_track_minutes_s)
    sleep_sec = int(sleep_sec_s)

    try:
        if sleep_sec > 0:
            time.sleep(sleep_sec)

        recheck_now = int(time.time())
        still_tracked = False

        if dispatch_file.is_file():
            try:
                lines = dispatch_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                lines = []
            for line in lines:
                row = _parse_dispatch_row(line)
                if row is None:
                    continue
                agent_id, _model, dispatched_at = row
                if agent_id != earliest_agent_id:
                    continue
                recheck_elapsed = (recheck_now - dispatched_at) // 60
                if recheck_elapsed >= max_track_minutes:
                    break
                if _agent_completed(completion_log, earliest_agent_id):
                    break
                still_tracked = True
                break

        if not still_tracked:
            _rm_lock(lock)
            return 0

        wake_now = int(time.time())
        elapsed_min = (wake_now - earliest_dispatched_at) // 60
        sys.stderr.write(
            "RUNTIME TRIPWIRE (L2 asyncRewake watcher) -- agent past threshold:\n"
        )
        sys.stderr.write(f"  {earliest_agent_id} | {earliest_model} | {elapsed_min} min elapsed\n")
        sys.stderr.write(
            "Agent owns the wrap judgment; you (EM) hold authority. Assess: let it "
            "finish, TaskStop, or plan a successor.\n"
        )
        sys.stderr.flush()

        _rm_lock(lock)
        return 2
    except Exception:
        _rm_lock(lock)
        return 0


def _rm_lock(lock: Path) -> None:
    try:
        lock.unlink(missing_ok=True)
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--watch":
        sys.exit(_watch_main(sys.argv[2:]))
    sys.exit(main())
