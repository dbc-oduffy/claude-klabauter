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

GAP CLOSED 2026-08-21 (state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md): the
precedent above carries TWO more properties this module had not copied -- `script_path`
resolved against a fixed anchor rather than the calling process's own `os.getcwd()` (auto_push
uses `__file__`, itself immune to caller cwd; this module now uses `repo_root`, its own
explicit parameter, for the same reason), and the child's `PYTHONPATH` carrying that anchor's
directory so `import coordinator_core` inside the child resolves the INTENDED tree rather than
whatever this box's ambient editable install happens to be pinned to. Missing either one
produced a detached child that either could not find its own script or booted against the
wrong engine root -- both silently, both indistinguishable from success at this call site's own
`True`/`False` return. See `spawn_detached`'s and `_child_env`'s own docstrings.

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

# Generator-provenance declaration: every write in this module targets
# state/housekeeping-failures.log, which is gitignored (.gitignore:78,
# `*.log`) -- never a tracked repo artifact.
GENERATES = []

_FAILURES_LOG_RELPATH = ("state", "housekeeping-failures.log")
_FAILURES_ARCHIVE_RELPATH = ("state", "housekeeping-failures.archive.log")

# Bound on the retained archive. Drained records are appended, then the head is dropped
# until the file fits — a ring buffer in a flat file. Sized so a month of drains on a
# 50-70-session box stays resident without the file becoming a disk-space question.
_FAILURES_ARCHIVE_MAX_BYTES = 2 * 1024 * 1024


def housekeeping_failures_log_path(repo_root: str) -> Path:
    """Return the shared housekeeping-failures log path for `repo_root`.

    Single well-known location so C17b's reader and every `spawn_detached` writer agree on
    where to look without re-deriving the join in more than one place.
    """
    return Path(repo_root, *_FAILURES_LOG_RELPATH)


def housekeeping_failures_archive_path(repo_root: str) -> Path:
    """Return the retained-history counterpart of `housekeeping_failures_log_path`.

    The live log is deliver-once and drains to empty on read (`clear_failures_log`), so on
    its own it can answer "what failed just now" but never "how often does this fail" — a
    measurement gap that left the engine-churn breakage rate unanswerable from the record
    the question's own spec named as its primary source
    (`state/handoffs/2026-08-15-engine-runs-from-builds-not-the-live-tree.md`, AC-1).
    This path is where the drain lands so the rate stays derivable after the fact.

    Negative spec: this is NOT a second delivery channel. Nothing reads it to surface
    records to an EM — `read_failures_log` deliberately does not consult it, so the
    deliver-once contract the live log carries is unchanged by its existence.
    """
    return Path(repo_root, *_FAILURES_ARCHIVE_RELPATH)


def _resolve_python_exe() -> str | None:
    """Resolve the interpreter to respawn with: `sys.executable` first, falling back to
    `python3`/`python` on PATH. Mirrors `auto_push._resolve_python_exe`'s fallback order so
    the two proven-precedent and new shared helper never silently diverge on this policy.
    """
    return sys.executable or shutil.which("python3") or shutil.which("python")


def _windows_detached_flags() -> int:
    """Compose the Windows-only detached-process creation flags.

    Mirrors `auto_push._windows_detached_flags` verbatim: CREATE_NEW_PROCESS_GROUP |
    CREATE_NO_WINDOW, each looked up via `getattr` so the module
    still imports cleanly on POSIX (where these constants don't exist on `subprocess`).

    negative-spec -- DETACHED_PROCESS MUST NOT be reintroduced here, and ORing it with
    CREATE_NO_WINDOW is NOT a middle ground: Win32 documents CREATE_NO_WINDOW as IGNORED
    whenever DETACHED_PROCESS or CREATE_NEW_CONSOLE is also set. This function carried
    exactly that combination and so read as console-suppressed while behaving as bare
    DETACHED_PROCESS -- measured as 6 visible `conhost.exe` windows across 3 spawns,
    versus 0 once DETACHED_PROCESS was dropped.

    DETACHED_PROCESS leaves the child with no console, so every descendant allocates its
    own WINDOWED one; CREATE_NO_WINDOW gives it a WINDOWLESS console that the whole
    subtree inherits. Detached lifetime is unaffected and was measured, not assumed --
    see this module's `spawn_detached` docstring, whose outlive-the-parent contract this
    change preserves.

    Measurement: `state/audits/2026-08-21-detached-process-console-window-storm.md`.
    """
    flags = 0
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
        with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except OSError:
        pass


def _child_env(repo_root: str, env_extra: "dict | None" = None) -> dict:
    """Build the detached child's environment: `PYTHONPATH` with `repo_root` PREPENDED
    ahead of anything already there, everything else inherited from this process's own
    `os.environ`.

    Mirrors `auto_push._respawn_env`'s `_claude_klabauter_package_root()` injection -- the SAME
    defect class, the SAME proven fix. That precedent's own docstring names it exactly:
    "Both respawn call sites launch the child by resolved ABSOLUTE SCRIPT PATH, never
    `-m` ... Running the script directly puts only `<pkg>/hooks/` on `sys.path[0]`, so
    `coordinator_core` itself isn't importable regardless of cwd" -- fixed there by
    prepending the package's own containing directory to `PYTHONPATH`, not by switching
    to `-m`. This module's own prior form respawned by absolute script path but never
    carried this half of the precedent, which is exactly the gap this function closes:
    without it, `import coordinator_core` inside the child resolves via whichever
    finder in `sys.meta_path` answers first once `sys.path[0]` (the SCRIPT's own
    directory, not `repo_root`) comes up empty -- on this box, this box's editable
    install's own finder, unconditionally pointing at wherever `pip install -e` last
    ran, regardless of which `repo_root` this call meant to serve. `PathFinder` is
    checked before any editable-install finder in `sys.meta_path` (verified live,
    2026-08-21), so a `repo_root` reachable via `PYTHONPATH` wins the resolution before
    that finder is ever consulted -- deterministically, regardless of both the calling
    process's cwd and this box's ambient editable-install pin.

    `env_extra` adds child-only variables. It exists so a caller can hand the child a
    value WITHOUT writing to this process's own `os.environ`: an interpreter-global env
    write outlives the call, is inherited by every later subprocess this process spawns,
    and so silently mislabels an unrelated child (`warm.client._spawn_once`'s spawn-epoch
    stamp is the live case -- a stale one inherited by a differently-routed spawn would
    have produced a FABRICATED boot measurement in the file that exists to settle how
    long boot takes). The repo's own env-leak fixture catches the write; this parameter
    is the fix it points at.

    Scrubs `COORDINATOR_WARM_BOOT_WAIT_SECS`: a detached child outlives the
    hook that spawned it, so a hook-scoped "I am a hook, do not block" value
    becomes a lie the moment it crosses this boundary. `_hook_boot.py` sets
    it to 0 deliberately and unconditionally and must stay unchanged -- that
    is correct for a hook, not for the warm server or its descendants, which
    otherwise inherit 0 for their whole lifetime (confirmed live: PID 17188
    carried both `COORDINATOR_WARM_BOOT_WAIT_SECS=0` and `CLAUDE_PLUGIN_ROOT`,
    the latter set by the harness only for hook processes). Scrub here, not
    at the setter -- `_hook_boot.py` lives in a repo claude-klabauter does not own, and
    this function's parent-dies/child-persists boundary is where the value
    stops being true.
    """
    env = dict(os.environ)
    env.pop("COORDINATOR_WARM_BOOT_WAIT_SECS", None)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = repo_root + (os.pathsep + existing if existing else "")
    if env_extra:
        env.update(env_extra)
    return env


def spawn_detached(
    repo_root: str,
    script_path: str,
    args: Sequence[str] | None = None,
    env_extra: "dict | None" = None,
) -> bool:
    """Spawn `script_path` as a detached child process, fully disowned from the parent's
    stdio, on both POSIX and Windows -- the shared shape C17's other chunks (C2/C5/C14)
    reuse instead of hand-copying `auto_push.spawn_detached_push`'s Popen block.

    Respawns by the RESOLVED ABSOLUTE interpreter path against `script_path` (never `-m`),
    matching the proven precedent: a module-relative import can silently fail to resolve
    depending on the caller's cwd, and a detached child's failure is invisible, so the
    respawn form itself must not be the thing that breaks.

    THE FIX THIS ROW CARRIES (found 2026-08-21, state/handoffs/2026-08-21_103635_
    reaching-the-warm-engine.md): `script_path` is now resolved against `repo_root`
    (`Path(repo_root) / script_path` when relative; unchanged when already absolute --
    `Path.__truediv__` discards the left operand for an absolute right one, so this is a
    no-op for a caller that already passes an absolute path), NEVER against this
    process's own `os.getcwd()`. A relative `script_path` resolved via bare
    `os.path.abspath` depends on whichever cwd the CALLING process happens to have --
    for `warm.client._spawn_once` and `ops.session.warm_start.warm_start`, that is
    whichever of ~20 fleet repos the session issuing the call happens to be in, almost
    never `repo_root` itself. Confirmed live: from an unrelated cwd, the prior form
    resolved to a nonexistent path and the spawned interpreter exited(2) "can't open
    file" before a single line of this repo's code ran -- silently, because a detached
    child's stdio is DEVNULL'd by design and its exit code is never read (see this
    docstring's own "NEVER raises" paragraph). `cwd=repo_root` is now also passed to
    `Popen` explicitly, so the child's OWN relative-path assumptions (state/ writes,
    breadcrumbs) are anchored the same deterministic way, never to whatever cwd this
    process inherited. See `_child_env`'s own docstring for the second half of this
    fix (the `PYTHONPATH` injection) -- resolving the script path alone is not enough;
    even a correctly-found script still imported the WRONG `coordinator_core` without it
    (confirmed live: the resolved-path fix in isolation still fell through to this box's
    ambient editable-install pin and raised `UnstampedEngineRootError`).

    NEVER raises. Returns True if `Popen` was invoked without raising (the child's own
    eventual exit code is NOT observed here -- this call only asserts the spawn itself
    didn't fail), False if interpreter resolution, the resolved script's own existence, or
    `Popen` itself failed; every one of those three is appended to the shared
    housekeeping-failures log before returning False -- the script-existence check is new
    with this row (a single `Path.exists()` stat, paid once per spawn attempt, never on
    the warm dispatch hot path this function is not called from) and exists because the
    prior code had no way to distinguish "found the file, spawned it" from "resolved to
    nothing, spawned a doomed interpreter anyway" -- both returned `True`.

    Deliberate isolation boundary -- MUST NOT be converted to an in-process
    call, ever, under any future refactor of this arm. Mechanism: detached
    lifetime -- the entire point of this function is that the child MUST
    outlive the parent process (start_new_session on POSIX / an own-process-group,
    windowless spawn on Windows -- see `_windows_detached_flags` for why that is
    NOT DETACHED_PROCESS, stdio redirected to DEVNULL). An in-process call cannot
    outlive the interpreter that made it: the parent process exiting (or the
    caller's own process being reaped) would take the "detached" work down
    with it, defeating the reason this function exists. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    args = list(args) if args is not None else []
    resolved_path = Path(repo_root) / script_path
    script_path = str(resolved_path)
    python_exe = _resolve_python_exe()
    if not python_exe:
        _log_spawn_failure(
            repo_root, script_path, args, RuntimeError("no python interpreter resolvable")
        )
        return False
    if not resolved_path.is_file():
        _log_spawn_failure(
            repo_root, script_path, args,
            FileNotFoundError(f"resolved script does not exist: {resolved_path}"),
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
            cwd=repo_root,
            env=_child_env(repo_root, env_extra),
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
        with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except OSError:
        pass


#: Per-reader delivery cursor, one file per session, beside the log it tracks.
#: `.log`-suffixed so the existing `*.log` gitignore rule (.gitignore:78) covers
#: it without a second rule to keep in sync.
_CURSOR_RELPATH_TEMPLATE = ("state", "housekeeping-cursor-{key}.log")


def housekeeping_cursor_path(repo_root: str, cursor_key: str) -> Path:
    """Delivery cursor for one reader of the shared failures log."""
    safe = "".join(c for c in cursor_key if c.isalnum() or c in "-_")[:64] or "unkeyed"
    return Path(repo_root, _CURSOR_RELPATH_TEMPLATE[0],
                _CURSOR_RELPATH_TEMPLATE[1].format(key=safe))


def read_failures_log(repo_root: str, cursor_key: "str | None" = None) -> str:
    """Non-destructive peek of the shared housekeeping-failures log's content.

    Returns "" if the log is absent or unreadable. Never raises. Used by the
    orientation-cache regen (C17b's surfacing occasion) to decide whether a
    ``## Housekeeping`` section is warranted, WITHOUT clearing anything itself — a
    ``--check`` dry run must stay a true dry run.

    ``cursor_key``, when supplied, makes delivery PER-READER: only the bytes
    appended since that key last called `advance_failures_cursor` are returned.

    Why this exists rather than the drain it replaces: the log is one shared file
    and ~50 sessions read it, but the old contract cleared it on delivery, so the
    first session to regenerate its orientation cache consumed every record and
    the other ~49 never saw them. "Deliver each record exactly once" was written
    for one reader and deployed against fifty, which made surfacing a lottery —
    a record was almost always cleared by a session other than the one that would
    have displayed it. Per-reader cursors deliver each record exactly once TO EACH
    READER, which is what the contract meant, and the shared file stops being
    destructively read at all.
    """
    log_path = housekeeping_failures_log_path(repo_root)
    try:
        raw = log_path.read_bytes()
    except OSError:
        return ""
    if cursor_key is None:
        return raw.decode("utf-8", errors="replace")

    offset = _read_cursor(repo_root, cursor_key)
    # A shorter log than the cursor means it was trimmed (or replaced) underneath
    # us; restart from the beginning rather than silently delivering nothing --
    # over-delivering a record is arguable to a reader, a vanished one is not.
    if offset > len(raw):
        offset = 0
    return raw[offset:].decode("utf-8", errors="replace")


def _read_cursor(repo_root: str, cursor_key: str) -> int:
    try:
        return max(0, int(housekeeping_cursor_path(repo_root, cursor_key).read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def advance_failures_cursor(repo_root: str, cursor_key: str) -> None:
    """Mark the log delivered up to its current end for `cursor_key`.

    The delivery-completes-here half, and the exact counterpart of the drain this
    replaces: called only after a successful, non-``--check`` write that has
    already embedded the content into that reader's own cache. Best-effort — a
    cursor that fails to advance re-delivers, which is the safe direction.
    """
    log_path = housekeeping_failures_log_path(repo_root)
    try:
        size = log_path.stat().st_size
    except OSError:
        return
    cursor = housekeeping_cursor_path(repo_root, cursor_key)
    try:
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text(f"{size}\n", encoding="utf-8", newline="\n")
    except OSError:
        pass


#: Live-log cap. Past it the log is archived and emptied. Retention is now a SIZE
#: question rather than a delivery one: a record has to outlive every reader's next
#: orientation regen, and a cap does that where clear-on-delivery did not.
_FAILURES_LOG_MAX_BYTES = 256 * 1024


def trim_failures_log(repo_root: str) -> None:
    """Archive and empty the live log once it exceeds `_FAILURES_LOG_MAX_BYTES`.

    Replaces the delivery-triggered drain as the live log's only bound. Under the
    cap this is a stat call and nothing else, so it is safe to invoke from the
    orientation write path on the same 200ms budget.

    Never raises. A trim that does not happen costs disk, not correctness.
    """
    log_path = housekeeping_failures_log_path(repo_root)
    try:
        if log_path.stat().st_size <= _FAILURES_LOG_MAX_BYTES:
            return
    except OSError:
        return
    clear_failures_log(repo_root)


def clear_failures_log(repo_root: str) -> None:
    """Best-effort drain the shared housekeeping-failures log to empty, retaining history.

    NO LONGER A DELIVERY MECHANISM. It is the reset/trim primitive
    (`trim_failures_log` is its only routine caller); delivery is per-reader via
    `read_failures_log`'s `cursor_key` and `advance_failures_cursor`. Calling this
    to "deliver" records again would restore the lottery those two exist to end —
    see `read_failures_log`.

    Called by an orientation-cache write path ONLY after a successful, non-``--check``
    write that has already embedded the log's content into the cache (C17b) — delivers
    each failure record to the EM exactly once rather than repeating it at every future
    regen. There are THREE such call sites as of 2026-07-28 (the CLI trampoline, the RPC
    handler, and `patch_pinboard_only`'s own fast-path write in
    `coordinator_core.orientation.regenerate_cache`) — all honouring the identical
    contract. Never raises; a failure to clear is not itself logged (would be circular).

    The drained content is appended to `housekeeping_failures_archive_path` before the live
    log is emptied. Deliver-once is a property of DELIVERY, not of retention: truncating
    without retaining meant the log had no history at all, so no failure-rate question
    could ever be answered from it. Archive-append failure never blocks the clear — the
    deliver-once contract outranks retention when the two cannot both be honoured.
    """
    log_path = housekeeping_failures_log_path(repo_root)
    try:
        drained = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        drained = ""
    if drained.strip():
        try:
            _append_to_failures_archive(repo_root, drained)
        except Exception:
            # Belt to the archive helper's own braces: retention must never be able to
            # keep the live log from draining, whatever the failure mode.
            pass
    try:
        log_path.write_text("", encoding="utf-8", newline="\n")
    except OSError:
        pass


def _append_to_failures_archive(repo_root: str, drained: str) -> None:
    """Append `drained` to the retained failures archive, trimming it back under the cap.

    Best-effort throughout: this is instrumentation, and instrumentation that can fail a
    ceremony is worse than instrumentation that occasionally loses a record. Never raises.
    """
    archive_path = housekeeping_failures_archive_path(repo_root)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    block = f"--- drained {stamp} ---\n{drained.rstrip()}\n"
    try:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with archive_path.open("a", encoding="utf-8") as handle:
            handle.write(block)
    except OSError:
        return
    try:
        if archive_path.stat().st_size <= _FAILURES_ARCHIVE_MAX_BYTES:
            return
        retained = archive_path.read_text(encoding="utf-8", errors="replace")
        retained = retained[-_FAILURES_ARCHIVE_MAX_BYTES:]
        # Drop the partial leading block so the file always starts on a drain header.
        marker = retained.find("--- drained ")
        if marker > 0:
            retained = retained[marker:]
        archive_path.write_text(retained, encoding="utf-8", newline="\n")
    except OSError:
        pass
