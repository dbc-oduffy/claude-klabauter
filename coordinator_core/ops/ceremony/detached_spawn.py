"""
coordinator_core.ops.ceremony.detached_spawn — one shared detached-CLI-spawn helper.

Purpose: `coordinator_core.hooks.auto_push.spawn_detached_push` proved the platform-correct
respawn-Popen shape (POSIX ``start_new_session=True`` / Windows detached creation flags,
``stdin=stdout=stderr=DEVNULL``, respawn by resolved absolute path, never ``-m``) for spawning
a detached housekeeping child that must outlive its parent without blocking it or holding open
its parent's pipes. C17's other chunks (C2/C5/C14) each need that exact same discipline for a
different target script; this module factors it into ONE call so the Windows-vs-POSIX flag
selection, stdio redirection, and interpreter-resolution fallback are get-right-once, not
hand-copied per call site and independently drift-prone.

Contract: `spawn_detached(script_path, args)` NEVER raises into its caller. Any failure to
resolve an interpreter, or any exception raised by `subprocess.Popen` itself, is caught and
appended to the shared housekeeping-failures log (below) — the spawn is fire-and-forget
best-effort; housekeeping is ambient, async, invisible, fail-loud only via the log, never via
an exception surfacing on the caller's (commit/session-critical) path.

Housekeeping-failures log convention (the write side; C17b builds the read/surface side
against this contract):

- Path: ``<repo_root>/state/housekeeping-failures.log`` — one shared, append-only log for
  every `spawn_detached` call site across the repo (NOT split per-caller), mirroring the
  single shared `.git/push-failures.log` precedent in `auto_push.py`.
- Format: ONE record per line, UTF-8, newline-terminated, fields in this exact order,
  space-separated, no field itself containing a raw newline (multi-line detail is escaped to
  literal ``\\n`` before being written):
  ``[<ISO-8601 UTC timestamp, %Y-%m-%dT%H:%M:%SZ>] SPAWN FAILED script=<script_path> args=<repr of the args tuple/list> :: <error class name>: <error message, \\n-escaped>``
- Example line:
  ``[2026-07-23T18:04:11Z] SPAWN FAILED script=/repo/coordinator_core/ops/foo_cli.py args=['--x', '1'] :: FileNotFoundError: [Errno 2] No such file or directory``
- Append mode (``"a"``), `encoding="utf-8"`. If the log write itself fails (e.g. `state/`
  unwritable), the failure is swallowed silently by `spawn_detached` too — this helper's
  contract is "never raise", full stop, even when its own forensics can't be recorded.

Negative-spec:
  - Does NOT convert any existing call site (`auto_push.spawn_detached_push` stays exactly
    as it is in this chunk) — helper + test only, per C17a's remit.
  - Does NOT retry a failed spawn — a failed spawn is recorded once and dropped; retry policy
    (if any) is the caller's concern, not this helper's.
  - Does NOT choose the child's own housekeeping-outcome log path or format — this module
    only defines the PARENT-side "I could not even spawn you" record. A successfully spawned
    child's own non-zero-exit/exception outcome is a separate C17b-consumed contract the
    child script itself is responsible for appending, to the SAME log path.
  - Does NOT ever become an in-process call. `spawn_detached`'s Popen is a deliberate
    isolation boundary, not an unconverted self-spawn: detachment (the child outliving the
    parent) is the entire reason this module exists. See `spawn_detached`'s own docstring and
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.

C17b (this module's second layer, added 2026-07-23) supplies that child-side contract plus
the read/clear half of the surfacing mechanism:

- `record_child_failure(repo_root, script_path, exit_code=..., exc=...)` — a spawned
  housekeeping CLI's own top-level error handling calls this on non-zero exit or an
  uncaught exception. Distinguishable verb ``CHILD FAILED`` (vs. the parent's
  ``SPAWN FAILED``) so a reader can tell "never even started" from "ran and failed" apart.
  Never raises; a no-op call (no exit_code, no exc) records nothing.
- `read_failures_log(repo_root)` — non-destructive peek of the log's current content, used
  by `coordinator_core.orientation.regenerate_cache.build_cache` to embed non-empty content
  into the orientation cache's ``## Housekeeping`` section (the surfacing occasion — see
  that module's docstring for why the orientation cache, not session boot's own stdout, is
  the reachable surface).
- `clear_failures_log(repo_root)` — best-effort truncate, called by EVERY orientation-cache
  write path (the CLI trampoline, the RPC handler, and `patch_pinboard_only`'s own fast-path
  write — `coordinator_core.orientation.regenerate_cache`) AFTER a successful (non-``--check``)
  write that embedded the log's content, so each failure record is delivered to the EM exactly
  once rather than repeating forever. `patch_pinboard_only` did not honour this until
  2026-07-28 (a third write path that delivered content without ever clearing — see that
  module's docstring, Defect 1 in
  cross-repo/inbox/2026-07-28-example-retrieval-repo-em-orientation-cache-housekeeping-flood.md); all
  three write paths now do.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

_FAILURES_LOG_RELPATH = ("state", "housekeeping-failures.log")


def housekeeping_failures_log_path(repo_root: str) -> Path:
    """Return the shared housekeeping-failures log path for `repo_root`.

    Single well-known location so C17b's reader and every `spawn_detached` writer agree on
    where to look without re-deriving the join in more than one place.
    """
    return Path(repo_root, *_FAILURES_LOG_RELPATH)


def _resolve_python_exe() -> str | None:
    """Resolve the interpreter to respawn with: `sys.executable` first, falling back to
    `python3`/`python` on PATH. Mirrors `auto_push._resolve_python_exe`'s fallback order so
    the two proven-precedent and new shared helper never silently diverge on this policy.
    """
    return sys.executable or shutil.which("python3") or shutil.which("python")


def _windows_detached_flags() -> int:
    """Compose the Windows-only detached-process creation flags.

    Mirrors `auto_push._windows_detached_flags` verbatim: DETACHED_PROCESS |
    CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, each looked up via `getattr` so the module
    still imports cleanly on POSIX (where these constants don't exist on `subprocess`).
    """
    flags = 0
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return flags


def _log_spawn_failure(repo_root: str, script_path: str, args: Sequence[str], exc: BaseException) -> None:
    """Best-effort append a spawn-failure record to the shared housekeeping-failures log.

    Swallows its own OSError -- this is the last-resort forensics path for a helper whose
    entire contract is "never raise"; a failure to write the log must not become a second,
    louder failure.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    detail = f"{type(exc).__name__}: {exc}".replace("\n", "\\n").replace("\r", "\\r")
    line = f"[{timestamp}] SPAWN FAILED script={script_path} args={list(args)!r} :: {detail}\n"
    log_path = housekeeping_failures_log_path(repo_root)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def spawn_detached(repo_root: str, script_path: str, args: Sequence[str] | None = None) -> bool:
    """Spawn `script_path` as a detached child process, fully disowned from the parent's
    stdio, on both POSIX and Windows -- the shared shape C17's other chunks (C2/C5/C14)
    reuse instead of hand-copying `auto_push.spawn_detached_push`'s Popen block.

    Respawns by the RESOLVED ABSOLUTE interpreter path against `script_path` (never `-m`),
    matching the proven precedent: a module-relative import can silently fail to resolve
    depending on the caller's cwd, and a detached child's failure is invisible, so the
    respawn form itself must not be the thing that breaks.

    NEVER raises. Returns True if `Popen` was invoked without raising (the child's own
    eventual exit code is NOT observed here -- this call only asserts the spawn itself
    didn't fail), False if interpreter resolution or `Popen` itself failed; both a resolution
    failure and a `Popen` exception are appended to the shared housekeeping-failures log
    before returning False.

    Deliberate isolation boundary -- MUST NOT be converted to an in-process
    call, ever, under any future refactor of this arm. Mechanism: detached
    lifetime -- the entire point of this function is that the child MUST
    outlive the parent process (start_new_session on POSIX / DETACHED_PROCESS
    on Windows, stdio redirected to DEVNULL). An in-process call cannot
    outlive the interpreter that made it: the parent process exiting (or the
    caller's own process being reaped) would take the "detached" work down
    with it, defeating the reason this function exists. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    args = list(args) if args is not None else []
    script_path = os.path.abspath(script_path)
    python_exe = _resolve_python_exe()
    if not python_exe:
        _log_spawn_failure(
            repo_root, script_path, args, RuntimeError("no python interpreter resolvable")
        )
        return False

    popen_kwargs: dict = {}
    if hasattr(os, "fork"):
        # POSIX: start_new_session=True detaches the child from the parent's session so it
        # survives the parent returning -- the fork-free equivalent of os.setsid().
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = _windows_detached_flags()

    try:
        subprocess.Popen(
            [python_exe, script_path, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **popen_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 -- helper contract is "never raise into caller"
        _log_spawn_failure(repo_root, script_path, args, exc)
        return False
    return True


def record_child_failure(
    repo_root: str,
    script_path: str,
    *,
    exit_code: int | None = None,
    exc: BaseException | None = None,
) -> None:
    """Best-effort append a CHILD-side outcome record to the shared housekeeping-failures
    log — the counterpart to `_log_spawn_failure`'s parent-side "I could not even spawn
    you" record (module docstring). A spawned housekeeping CLI calls this from its own
    top-level error handling when IT (not its parent's spawn attempt) exits non-zero or
    raises, so a failure downstream of a successful spawn is not silently lost.

    Distinguishable from the parent-side record by its own ``CHILD FAILED`` verb (vs.
    ``SPAWN FAILED``) so a reader — human or the orientation-cache surfacing pass — can
    tell "never even started" from "ran and failed" apart at a glance.

    No-op (records nothing) if neither a non-zero `exit_code` nor `exc` is supplied —
    nothing to record. NEVER raises, matching this module's whole-file contract.
    """
    if exc is None and not exit_code:
        return
    script_path = os.path.abspath(script_path)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if exc is not None:
        detail = f"{type(exc).__name__}: {exc}".replace("\n", "\\n").replace("\r", "\\r")
    else:
        detail = f"non-zero exit {exit_code}"
    line = f"[{timestamp}] CHILD FAILED script={script_path} :: {detail}\n"
    log_path = housekeeping_failures_log_path(repo_root)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def read_failures_log(repo_root: str) -> str:
    """Non-destructive peek of the shared housekeeping-failures log's current content.

    Returns "" if the log is absent or unreadable. Never raises. Used by the
    orientation-cache regen (C17b's surfacing occasion) to decide whether a
    ``## Housekeeping`` section is warranted, WITHOUT clearing anything itself — a
    ``--check`` dry run must stay a true dry run. See `clear_failures_log` for the
    delivery-completes-here half.
    """
    log_path = housekeeping_failures_log_path(repo_root)
    try:
        return log_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def clear_failures_log(repo_root: str) -> None:
    """Best-effort truncate the shared housekeeping-failures log to empty.

    Called by an orientation-cache write path ONLY after a successful, non-``--check``
    write that has already embedded the log's content into the cache (C17b) — delivers
    each failure record to the EM exactly once rather than repeating it at every future
    regen. There are THREE such call sites as of 2026-07-28 (the CLI trampoline, the RPC
    handler, and `patch_pinboard_only`'s own fast-path write in
    `coordinator_core.orientation.regenerate_cache`) — all honouring the identical
    contract. Never raises; a failure to clear is not itself logged (would be circular).
    """
    log_path = housekeeping_failures_log_path(repo_root)
    try:
        log_path.write_text("", encoding="utf-8")
    except OSError:
        pass
