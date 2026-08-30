"""
coordinator_core.ops.workflow_fire.fire — headless ``claude -p`` firing
mechanism plus the on-disk fire registry.

Purpose: fires exactly ONE detached ``claude -p`` child process per
emitted workflow script, resolves ``--plugin-dir`` the way the
``claude-doe`` shim does (never hardcoded), bounds the child with
``--max-turns``, confirms liveness (or an immediate non-zero exit) before
returning, and records the fire in a JSON registry under
``<git-common-dir>/coordinator-sessions/workflow-fires/`` — the run handle
callers receive IS this registry record, not an in-memory pid a later,
cold-spawned invocation of this engine could never see again.

Every flag choice here is a measured fact, not a preference — see
docs/research/spike-verdicts/2026-08-18-claude-klabauter-fires-an-emitted-workflow.md:
  - probe 4: ``--bg`` is refused alongside ``-p`` by design (``--bg`` starts
    an attachable interactive session; an engine must never do that). Fire
    via ordinary detached ``Popen``, never ``--bg``.
  - probes 2 vs 3: a headless session does NOT inherit the coordinator
    plugin roster from the operator's interactive environment; omitting
    ``--plugin-dir`` makes every ``coordinator:*`` phase fail inside the
    fired workflow. Resolution failure is fail-loud here, not a silently
    omitted flag. Omitting ``--plugin-dir`` would ALSO remove the inner
    per-agent guard layer described below — a second, independent reason
    that flag stays fail-loud rather than optional.
  - probe 1 vs the driver-cost note: the driver only calls one tool once,
    so a cheap model (``haiku``) is correct; the workflow's own agents'
    ``model:`` fields, not this driver, control the real work's cost tier.

``--allowedTools`` scope (two-probe live finding, post-spike): this flag
grants the fired SESSION, not just the driver — every agent the workflow
spawns inherits it. A grant of bare ``Workflow`` let the driver call the
Workflow tool but then refused every spawned phase's own tool use (a
``coordinator:executor``'s Write/Edit, a commit phase's Bash), so the
workflow did nothing while still exiting 0. ``_SESSION_ALLOWED_TOOLS`` is
therefore deliberately wide — it is the OUTER gate only, kept simple and
permissive on purpose. Two INNER gates already enforce least privilege
one level down and are NOT re-derived or narrowed here:
  1. each phase's own ``agentType`` carries its own tool list from that
     agent's definition (a reviewer gets a reviewer's tools and behaves as
     it always does);
  2. coordinator's own guard layer (``block_reviewer_bash_outside_allowlist``
     and the rest) loads inside the child BECAUSE ``--plugin-dir`` is
     passed, and still governs there.
A workflow freely mixes executors, reviewers, and committers per its own
plan — there is no narrower, workflow-specific outer grant to derive, and
this module does not attempt one.

Reporting hazard: a fired child that exits 0 with ``terminal_reason:
completed`` and an empty ``permission_denials`` array is NOT proof any
phase did work — a per-phase tool refusal is invisible at the driver's own
summary. Read that warning as a CLASS, on a corrected basis: the original
measurement was taken while ``_SESSION_ALLOWED_TOOLS`` was still the bare
``Workflow`` grant, and widening it fixed that cause — a 2026-08-19 sweep
of 4,736 transcripts found not one outer-grant refusal across ten fires
and seventy agents. What keeps the class live is coordinator's own inner
guard layer, which does still refuse individual tool calls inside a fired
run. ``fire_status`` does not detect refusals; the per-phase truth is the
harness's workflow journal, reachable via the ``driver_session_id`` this
module records (see ``fire_status``'s docstring for the reader move, and
docs/research/2026-08-19-what-a-fired-runs-driver-summary-cannot-tell-you.md
for the measurements and for why the engine records the pointer rather
than reading that store itself).

Spec backlink: docs/plans/2026-08-18-claude-klabauter-fires-the-workflows-it-emits.md
§ C4

Negative-spec:
  - Does NOT spawn one child per phase — one child per FIRED WORKFLOW; the
    workflow's own agents run inside that one process (spike verdict, "What
    the measurements establish"). A per-phase seam is exactly what the
    concurrency-cap and load-norm reasoning here forecloses.
  - Does NOT use ``--bg`` — structurally incompatible with ``-p`` (probe 4).
  - Does NOT use ``--dangerously-skip-permissions`` — an explicit, wide
    allowlist is the shape here, not a permissions bypass.
  - Does NOT derive a per-workflow ``--allowedTools`` grant from the
    script's declared ``agentType``s — considered and rejected (see module
    docstring); least privilege is enforced one level down, not here.
  - Does NOT hardcode a plugin-dir path — always resolved fresh (see
    ``resolve_plugin_dir``), and a resolution failure raises rather than
    degrading to an unflagged, silently-broken fire.
  - Does NOT leave the concurrency-cap check-then-write open to two
    concurrent cold-spawned invocations both passing it -- the check and
    the slot reservation happen under a short-lived cross-process lock
    (``_acquire_cap_lock``); see ``fire_workflow``'s own docstring for the
    exact guarantee and its bounded, self-healing (not permanent) failure
    modes.
  - Does NOT wait on the child, poll it to completion, or capture its full
    output — it confirms the child did not die at spawn, then returns. A
    caller that wants terminal state polls back in via
    ``workflow.fire_status``, which is itself best-effort (see its
    docstring) because a cold-spawned engine invocation never retains the
    ``Popen`` handle across process boundaries.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.session.core import sessions_dir
from coordinator_core.warm import skew
from coordinator_core.warm.engine_root import current_engine_clone

# Runtime fire-registry/log state under <git-common-dir>/coordinator-sessions
# -- not a repo artifact (mirrors session/core.py, session/claims.py).
GENERATES = []

#: Driver model: the child calls one tool once, so the cheap tier is
#: correct (spike verdict: $0.53 Opus vs $0.04-0.17 haiku, identical work).
DEFAULT_MODEL = "haiku"

#: Low turn cap -- near-free insurance against a wedged child burning
#: tokens invisibly for work that is structurally one tool call.
DEFAULT_MAX_TURNS = 3

#: EM-set default, not a measured value (see this chunk's brief) -- trivial
#: to move, deliberately a named constant rather than inlined at call sites.
DEFAULT_CONCURRENCY_CAP = 3

#: Session-wide grant for the fired child -- the OUTER gate only. Every
#: agent the fired workflow spawns inherits this, so it must be wide
#: enough to not be the binding constraint for any ordinary phase shape
#: (executor write/edit, committer bash, reviewer read/grep). See module
#: docstring's "``--allowedTools`` scope" section for the two-probe live
#: finding this replaced (bare ``Workflow`` silently refused every spawned
#: phase's own tool use) and for why this is deliberately NOT narrowed
#: per-workflow -- least privilege is enforced one level down, by each
#: phase's own ``agentType`` and by coordinator's own guard layer (loaded
#: because ``--plugin-dir`` is passed), never re-derived here.
_SESSION_ALLOWED_TOOLS = ("Workflow", "Read", "Write", "Edit", "Bash", "Grep", "Glob", "ToolSearch")

_PROMPT_TEMPLATE = (
    "Call the Workflow tool exactly once, with scriptPath set to {script_path!r}, "
    "and no other tool."
)

#: Windows console-subprocess discipline: guards a bare CREATE_NO_WINDOW
#: reference that would raise ValueError on macOS/Linux.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

#: negative-spec -- DETACHED_PROCESS MUST NOT be used on this spawn path. It is
#: deliberately absent as a symbol, not merely unused: the regression guard
#: `coordinator_core/tests/test_no_detached_process_spawn.py` forbids the name
#: outright in live engine code, so there is nothing to accidentally re-OR in.
#:
#: DETACHED_PROCESS gives the child NO console. `claude.exe` is a
#: console-subsystem binary, so every descendant of a detached fire -- each
#: hook interpreter, each `git` probe -- has no console to inherit and
#: allocates its OWN `conhost.exe`, WITH a visible window. A fired run
#: therefore became a window factory: one flash per descendant spawn, at
#: roughly 20 spawns per tool call, for the whole life of the driver session.
#:
#: CREATE_NO_WINDOW instead gives the child a console that HAS no window, and
#: that windowless console is inherited by the entire subtree -- so one flag on
#: the root spawn silences every descendant.
#:
#: Do NOT "fix" this by ORing the two together. Win32 documents CREATE_NO_WINDOW
#: as IGNORED when combined with DETACHED_PROCESS or CREATE_NEW_CONSOLE, and
#: that combination was measured here as indistinguishable from bare
#: DETACHED_PROCESS -- 6 console windows across 3 spawns, versus 0 for
#: CREATE_NO_WINDOW alone. Two sibling sites carried exactly that
#: no-op spelling and read as suppressed while flashing.
#:
#: Detached LIFETIME is unaffected and was measured, not assumed: Windows does
#: not reap children on parent exit, so a CREATE_NO_WINDOW child outlives a
#: hard-killed parent identically to a DETACHED_PROCESS one. Ctrl-C isolation
#: is carried by CREATE_NEW_PROCESS_GROUP, which is unchanged.
#:
#: Measurement: `state/audits/2026-08-21-detached-process-console-window-storm.md`.

#: The fired driver runs the Workflow tool as a background task and, by
#: default, terminates any still-running background work after 600s --
#: which kills the workflow itself, mid-wave, leaving earlier waves
#: committed and later ones silently undone. Observed live: fire
#: 5df351a6 lost four of five wave-1 chunks this way. ``0`` means wait
#: indefinitely; the turn cap and the workflow's own budget remain the
#: real bounds.
_BG_WAIT_CEILING_ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"


def build_fire_env(base: Optional[dict] = None) -> dict:
    """Child environment for a fired driver: inherited, plus an uncapped
    background-task wait.

    Negative spec: never narrows the inherited environment -- the child
    needs the operator's PATH, plugin roster, and coordinator settings
    root. An explicit operator-set ceiling is respected, not overridden.
    """
    env = dict(os.environ if base is None else base)
    env.setdefault(_BG_WAIT_CEILING_ENV, "0")
    return env


class PluginDirResolutionError(RuntimeError):
    """Raised when ``--print-plugin-dir`` cannot be resolved.

    Fail-loud by design (spike verdict, probes 2 vs 3): a silently omitted
    ``--plugin-dir`` flag makes every ``coordinator:*`` phase fail inside
    the fired workflow, indistinguishably from a healthy fire until the
    child's own log is read.
    """


class ScriptNotFoundError(ValueError):
    """Raised when ``script_path`` does not exist on disk at fire time."""


class ConcurrencyCapExceededError(RuntimeError):
    """Raised when the registry's live-fire count is already at the cap."""


class ChildSpawnFailedError(RuntimeError):
    """Raised when the spawned child cannot be confirmed live (immediate
    non-zero exit, or the binary/flag was refused at spawn)."""


def _native_plugin_dir() -> Optional[str]:
    """Resolve the coordinator plugin root in-process, no subprocess spawn.

    House precedent: ``coordinator_core.bash_guards.commit_tripwires.
    _resolve_doe_coordinator_root`` (same ``coordinator_doe_root()`` join,
    same ``os.path.isdir`` existence gate). Primary path here rather than a
    fallback -- an in-process resolution is real cost saved under the
    50-70 concurrent-LLM load norm, where a subprocess-per-resolution
    would not be. Returns ``None`` (never raises) on any resolution
    failure; ``resolve_plugin_dir`` decides what a ``None`` means.
    """
    try:
        from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root
    except Exception:
        return None
    try:
        doe_root = coordinator_doe_root()
    except Exception:
        return None
    if not doe_root:
        return None
    candidate = os.path.join(doe_root, "coordinator")
    return candidate if os.path.isdir(candidate) else None


def _shim_plugin_dir(shim_bin: str = "claude-doe") -> Optional[str]:
    """Subprocess fallback -- invokes the ``claude-doe`` SHIM, never bare
    ``claude``. ``--print-plugin-dir`` is a shim flag; the raw ``claude``
    binary a bare ``shutil.which("claude")`` resolves to on Windows does
    not understand it (fails loud with "unknown option", not silently).
    Returns ``None`` (never raises) on any failure -- this is a fallback,
    not the authoritative failure signal.
    """
    try:
        result = subprocess.run(
            [shim_bin, "--print-plugin-dir"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    plugin_dir = result.stdout.strip()
    if result.returncode != 0 or not plugin_dir:
        return None
    return plugin_dir


def resolve_plugin_dir() -> str:
    """Resolve the coordinator plugin root.

    Primary path: the native, in-process ``coordinator_doe_root()``
    resolver (``_native_plugin_dir``) -- no subprocess spawn. Falls back to
    shelling out to the ``claude-doe`` SHIM (never bare ``claude`` --
    ``--print-plugin-dir`` is a shim flag the raw binary does not
    understand) only if the native path fails; the shim binary name is
    fixed (``"claude-doe"``, see ``_shim_plugin_dir``) and is not a
    parameter here, since no caller has ever needed to vary it and a
    decorative parameter that looked like it controlled resolution was
    itself a defect (see removed ``claude_bin`` param -- reviewed away).
    Raises ``PluginDirResolutionError`` when BOTH paths fail to yield an
    existing directory -- never returns a guessed or empty path (spike
    verdict, probes 2 vs 3: a silently omitted ``--plugin-dir`` flag makes
    every ``coordinator:*`` phase fail inside the fired workflow).

    Negative-spec: does NOT claim the two resolution paths are verified
    byte-identical -- that comparison was never run in this diff's test
    suite (only "resolves to some directory named ``coordinator``" is
    asserted); say so rather than overclaim.
    """
    native = _native_plugin_dir()
    if native:
        return native

    shim = _shim_plugin_dir()
    if shim:
        return shim

    raise PluginDirResolutionError(
        "could not resolve the coordinator plugin dir: neither the native "
        "coordinator_doe_root() resolver nor the claude-doe shim's "
        "--print-plugin-dir yielded an existing directory"
    )


def build_fire_command(
    script_path: str,
    plugin_dir: str,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    claude_bin: str = "claude",
) -> list:
    """Build the argv for the detached firing child.

    ``-p`` is never omitted -- pinned by test, since that flag is the
    entire reason the interactive direct-child invariant
    (docs/reference/interactive-launch-chain.md) stays out of scope: a
    ``-p`` child never attaches a console.
    """
    return [
        claude_bin,
        "-p",
        _PROMPT_TEMPLATE.format(script_path=script_path),
        "--allowedTools",
        *_SESSION_ALLOWED_TOOLS,
        "--plugin-dir",
        plugin_dir,
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--output-format",
        "json",
    ]


def _publish_lag_message(cwd: Optional[str] = None) -> Optional[str]:
    """DR-335, call site (a): an emitted run executes the published mirror,
    so a stale mirror means the fired child runs stale code. Two bounded
    git calls at most (`skew.publish_lag`'s own contract) -- fires are rare
    and already expensive, so this is free relative to the spawn itself.
    Never raises; `None` covers both "cannot tell" and "below threshold" --
    `fire_workflow` does not distinguish them, per DR-335's negative spec
    that a lag between rounds is expected, not a defect to report on.
    """
    try:
        source_root = show_toplevel(cwd) or cwd or os.getcwd()
        lag = skew.publish_lag(current_engine_clone(), Path(source_root))
    except Exception:
        return None
    if lag is None:
        return None
    return skew.publish_lag_message(lag)


def _registry_dir(cwd: Optional[str] = None) -> Path:
    base = sessions_dir(cwd)
    if not base:
        raise RuntimeError("workflow_fire: not inside a git repository (sessions_dir resolved empty)")
    return Path(base) / "workflow-fires"


def _record_path(registry_dir: Path, fire_id: str) -> Path:
    return registry_dir / f"{fire_id}.json"


def _write_record(path: Path, record: dict) -> dict:
    """Persist ``record`` annotated, and return exactly what landed on disk
    -- so a caller never has to annotate separately to hold the same dict
    the file holds (Review: coordinator:code-reviewer, double-annotate)."""
    record = _annotate_record(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return record


def _read_record(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _list_records(registry_dir: Path) -> list:
    if not registry_dir.is_dir():
        return []
    records = []
    for entry in registry_dir.glob("*.json"):
        record = _read_record(entry)
        if record is not None:
            records.append(record)
    return records


#: Cross-process mutex guarding the count-then-reserve window in
#: ``fire_workflow`` -- an O_EXCL lockfile (portable to Windows, unlike
#: ``fcntl``/``msvcrt``-specific locking). Held only around the cap check
#: + reservation write, never around plugin-dir resolution or the spawn
#: itself, so lock hold time stays short under the 50-70 concurrent-LLM
#: load norm.
_CAP_LOCK_NAME = ".concurrency-cap.lock"
_CAP_LOCK_ACQUIRE_TIMEOUT_S = 5.0
_CAP_LOCK_POLL_INTERVAL_S = 0.02
#: A lock file older than this is presumed to belong to a crashed holder
#: (this module never holds the lock across a subprocess spawn or any
#: other slow operation) -- reclaimed rather than wedging every future
#: fire permanently.
_CAP_LOCK_STALE_AGE_S = 10.0

#: A "pending" reservation record older than this is presumed to belong to
#: a fire that crashed between reserving a slot and either spawning or
#: discarding the reservation -- reclaimed as "exited" so a crash cannot
#: permanently occupy a concurrency-cap slot.
_PENDING_RECLAIM_TIMEOUT_S = 30.0


def _acquire_cap_lock(registry_dir: Path) -> Path:
    """Block until the concurrency-cap lockfile is exclusively created.

    Uses ``os.open(..., O_CREAT | O_EXCL)`` as the mutex primitive -- the
    create is atomic across processes on both Windows and POSIX, unlike a
    read-then-write check. A lock file older than
    ``_CAP_LOCK_STALE_AGE_S`` is reclaimed (unlinked and retried) rather
    than honored, so a holder that crashed while holding the lock cannot
    wedge every subsequent fire forever -- it stalls other fires for at
    most that long, self-healing rather than permanently.
    """
    registry_dir.mkdir(parents=True, exist_ok=True)
    lock_path = registry_dir / _CAP_LOCK_NAME
    deadline = time.time() + _CAP_LOCK_ACQUIRE_TIMEOUT_S
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return lock_path
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > _CAP_LOCK_STALE_AGE_S:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.time() >= deadline:
                raise RuntimeError(
                    f"workflow_fire: timed out waiting for the concurrency-cap "
                    f"lock at {lock_path} -- a holder may be wedged"
                )
            time.sleep(_CAP_LOCK_POLL_INTERVAL_S)


def _release_cap_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


def _discard_reservation(record_path: Path) -> None:
    """Best-effort removal of a "pending" reservation record -- called
    when the fire that reserved the slot fails before ever reaching
    "running", so the slot does not stay occupied for
    ``_PENDING_RECLAIM_TIMEOUT_S`` when it could be freed immediately."""
    try:
        record_path.unlink()
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe -- see module docstring's negative-spec:
    a cold-spawned engine can only ask "does this pid currently exist",
    never recover the real exit code of an already-reaped child."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _log_size_bytes(log_path: str) -> Optional[int]:
    """Cheap, honest, NON-authoritative signal -- current byte size of the
    child's log file. Never parsed for content (see ``fire_status``
    docstring on why a content parse is out of scope). A ``0`` here is a
    real, useful flag ("nothing has been captured at all yet"); a non-zero
    size is NOT evidence of success -- a fully-refused workflow that still
    printed its ``--output-format json`` summary produces a non-trivial
    log too (the live finding this module's docstring describes). Returns
    ``None`` if the log cannot be stat'd (not yet created, removed)."""
    try:
        return Path(log_path).stat().st_size
    except OSError:
        return None


#: The harness's own banner when it terminates still-running background
#: work at its wait ceiling. That banner is the ONLY discriminator for this
#: flavour of truncation: a run cut short this way still prints a terminal
#: result envelope carrying ``is_error: false`` (measured 2026-08-19 on
#: fires 5df351a6 and 1eb8711b -- both truncated, both reporting a clean
#: driver result).
#:
#: Matched at LINE START and paired with ``terminating``, never as a bare
#: substring anywhere in the window (Review: coordinator:code-reviewer).
#: The loose form was self-referentially unsafe: this module's own source
#: now contains the banner text verbatim, so a fired workflow that so much
#: as printed this file -- a review or fix pass over ``fire.py`` -- would
#: have classified its own clean run as truncated. Residual, accepted: a
#: log line that begins with the banner text and quotes the word
#: ``terminating`` still matches. The real banner is written unindented on
#: its own line, so quoted mentions in agent output (indented, bulleted, or
#: mid-sentence) no longer reach it.
_TRUNCATION_BANNER = "Background tasks still running after"
_TRUNCATION_CONFIRM = "terminating"


def _has_truncation_banner(text: str) -> bool:
    """True when the harness's background-work truncation banner appears as
    its own log line -- see ``_TRUNCATION_BANNER`` for why a bare substring
    match was not safe here."""
    for line in text.splitlines():
        if line.startswith(_TRUNCATION_BANNER) and _TRUNCATION_CONFIRM in line:
            return True
    return False

#: Bounded head+tail window read from a child's log when classifying its
#: outcome -- the truncation banner lands at the head (stderr, ahead of the
#: driver's stdout) and the terminal envelope at the tail, so neither end
#: alone suffices. Never a whole-file read: a log is unbounded in principle
#: and this runs inside every registry sweep.
_LOG_SCAN_WINDOW_BYTES = 64 * 1024

#: Bumped whenever the derived annotation set changes shape. A record
#: stamped with an older version is re-classified ONCE on its next refresh
#: and re-stamped -- which is how a record settled by an earlier version
#: picks up a field that version never wrote, without a migration pass and
#: without re-reading its log on every subsequent sweep.
_ANNOTATION_VERSION = 2

_STALENESS_NOTE = (
    "`state`, `exit_code` and `outcome` are only valid as of `status_checked_at` -- "
    "this file is not written when the child exits. Refresh with workflow.fire_status(<fire_id>)."
)

_EXIT_CODE_NOTE = (
    "unrecoverable: the child is spawned detached with no retained handle, so no parent "
    "survives to read its exit status. Read `outcome` instead."
)


def _iso(epoch_seconds) -> Optional[str]:
    """UTC ISO-8601 rendering of an epoch float, for a human reading the raw
    record. Returns ``None`` for a missing or unparseable value."""
    try:
        return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _read_log_window(log_path: str) -> Optional[str]:
    """Head + tail of a child's log, each capped at
    ``_LOG_SCAN_WINDOW_BYTES``. Returns ``None`` if the log cannot be read.

    Negative-spec: NOT the permission-denial log parser ``fire_status``'s
    docstring rules out of scope -- this reads two bounded windows and looks
    only for the driver's own terminal envelope and truncation banner, never
    for per-phase tool refusals.
    """
    try:
        with open(log_path, "rb") as handle:
            head = handle.read(_LOG_SCAN_WINDOW_BYTES)
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            tail = b""
            if size > _LOG_SCAN_WINDOW_BYTES:
                handle.seek(max(size - _LOG_SCAN_WINDOW_BYTES, len(head)))
                tail = handle.read()
    except OSError:
        return None
    # Decoded separately, then joined in str space (Review:
    # coordinator:code-reviewer). Splicing a separator into the BYTES put a
    # synthetic boundary mid-stream, where a multi-byte sequence straddling
    # it decoded to a replacement character under errors="replace". This
    # removes that synthetic seam; it does NOT make the read lossless -- a
    # character split by the window boundary itself still costs one
    # replacement char at that chunk's edge, which is inherent to reading a
    # bounded window and is confined to the edges rather than the middle.
    return (
        head.decode("utf-8", errors="replace")
        + "\n"
        + tail.decode("utf-8", errors="replace")
    )


def _terminal_envelope(text: str) -> Optional[dict]:
    """The child's own ``--output-format json`` result envelope, or ``None``.

    Its presence is the child's last act, and the only in-band evidence that
    the driver reached its own end rather than being killed mid-run. Its
    ``session_id`` is the one field lifted onto the record (see
    ``_annotate_record``) -- every other field in it describes the DRIVER,
    which is exactly the thing a reader must not mistake for the workflow.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "result":
            return parsed
    return None


def _classify_outcome(record: dict) -> tuple:
    """Classify a fire as ``clean`` / ``truncated`` / ``unknown`` from the
    child's own log, returning ``(outcome, basis)``.

    This answers the question the exit code cannot answer here: the spawn is
    detached (``DETACHED_PROCESS`` / ``start_new_session``) and no ``Popen``
    handle survives across process boundaries, so a real exit status is
    unrecoverable once the child is reaped -- ``_refresh_record_state`` can
    only infer death from a dead pid. The reachable signals are the two the
    child itself writes: the harness's background-work truncation banner,
    and the driver's terminal result envelope.

    Returns ``(outcome, basis, driver_session_id)``. ``clean`` means the
    DRIVER finished, never that the workflow's phases did
    work -- a fully-refused run exits 0 with a clean envelope too (see
    ``fire_status``'s docstring for that separate, still-live hazard).

    The session id is ``None`` whenever no envelope was recovered.
    """
    if record.get("state") != "exited":
        return "unknown", "the run has not been observed to end", None
    text = _read_log_window(record.get("log_path", ""))
    if text is None:
        return "unknown", "the child's log could not be read", None
    envelope = _terminal_envelope(text)
    session_id = (envelope or {}).get("session_id")
    if _has_truncation_banner(text):
        return (
            "truncated",
            "the driver terminated still-running background work at its wait ceiling",
            session_id,
        )
    if envelope is not None:
        return "clean", "the driver wrote its own terminal result envelope", session_id
    return "truncated", "the child is dead and never wrote a terminal result envelope", None


def _annotate_record(record: dict) -> dict:
    """Make a raw record self-describing: staleness, outcome, and why
    ``exit_code`` is null.

    Applied on every write and every refresh, so a reader who opens
    ``<git-common-dir>/coordinator-sessions/workflow-fires/<id>.json``
    directly -- the obvious move when checking your own run, and the one
    that misled three sessions on 2026-08-19 -- sees from the record alone
    that ``state`` is a timestamped observation, not a current fact.
    Returns an annotated copy, ``==``-equal to its input when nothing
    changed, so callers keying on identity do not churn writes.

    A terminal ``outcome`` is computed once, at the transition, and reused
    thereafter -- the log read never repeats on later sweeps, except for the
    single re-classification a ``_ANNOTATION_VERSION`` bump forces.
    """
    annotated = dict(record)
    annotated["_readme"] = _STALENESS_NOTE
    annotated["status_checked_at_iso"] = _iso(
        record.get("status_checked_at", record.get("reserved_at"))
    )
    # An ``unknown`` on an ALREADY-EXITED record is terminal, not pending:
    # the child is reaped and its log will never become readable, so
    # re-attempting the open on every sweep buys nothing and is paid by
    # every future fire (``count_live_fires`` walks the whole registry).
    # Review: coordinator:code-reviewer. An ``unknown`` on a record that has
    # not yet been observed to end stays unsettled, which is the case that
    # must keep re-checking.
    outcome_now = annotated.get("outcome")
    settled = outcome_now in ("clean", "truncated") or (
        outcome_now == "unknown" and annotated.get("state") == "exited"
    )
    current = annotated.get("_annotation_version") == _ANNOTATION_VERSION
    if not (settled and current):
        outcome, basis, driver_session_id = _classify_outcome(annotated)
        annotated["outcome"] = outcome
        annotated["outcome_basis"] = basis
        if driver_session_id:
            annotated["driver_session_id"] = driver_session_id
    annotated["_annotation_version"] = _ANNOTATION_VERSION
    if annotated.get("state") == "exited" and annotated.get("exit_code") is None:
        annotated["exit_code_note"] = _EXIT_CODE_NOTE
    else:
        annotated.pop("exit_code_note", None)
    return annotated


def _refresh_record_state(record: dict) -> dict:
    """Reclaim stale state before counting/reporting it.

    Two independent self-heals live here: a "running" record whose pid is
    dead becomes "exited" (unchanged from before); a "pending" reservation
    (see ``fire_workflow``'s atomic reserve-before-spawn) older than
    ``_PENDING_RECLAIM_TIMEOUT_S`` also becomes "exited" -- this is what
    keeps a process that crashed between reserving a cap slot and either
    spawning or discarding the reservation from wedging the concurrency
    cap permanently.
    """
    if record.get("state") == "pending":
        reserved_at = record.get("reserved_at", 0)
        if time.time() - reserved_at > _PENDING_RECLAIM_TIMEOUT_S:
            record = dict(record)
            record["state"] = "exited"
            record["status_checked_at"] = time.time()
        annotated = _annotate_record(record)
        return annotated if annotated != record else record
    current_log_size = _log_size_bytes(record.get("log_path", ""))
    needs_update = current_log_size != record.get("log_size_bytes")
    died = record.get("state") == "running" and not _pid_alive(record["pid"])
    if died or needs_update:
        record = dict(record)
        record["log_size_bytes"] = current_log_size
        if died:
            record["state"] = "exited"
            record["exit_code"] = None
        record["status_checked_at"] = time.time()
    annotated = _annotate_record(record)
    return annotated if annotated != record else record


def count_live_fires(cwd: Optional[str] = None) -> int:
    """Refresh and count registry entries currently "running" or
    "pending" (a reserved-but-not-yet-spawned slot, see ``fire_workflow``)
    -- both occupy a concurrency-cap slot."""
    registry_dir = _registry_dir(cwd)
    live = 0
    for record in _list_records(registry_dir):
        refreshed = _refresh_record_state(record)
        if refreshed is not record:
            _write_record(_record_path(registry_dir, refreshed["fire_id"]), refreshed)
        if refreshed.get("state") in ("running", "pending"):
            live += 1
    return live


#: Bounded liveness-confirmation window: total wall time this module spends
#: polling a freshly-spawned child before deciding it is alive. Short and
#: bounded on purpose -- a local process spawn either fails immediately
#: (bad binary/flag) or is running well within this window.
_LIVENESS_POLL_INTERVAL_S = 0.05
_LIVENESS_POLL_ATTEMPTS = 6


def fire_workflow(
    script_path: str,
    cwd: Optional[str] = None,
    claude_bin: str = "claude",
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    concurrency_cap: int = DEFAULT_CONCURRENCY_CAP,
) -> dict:
    """Fire one detached ``claude -p`` child for ``script_path``.

    Returns the fire registry record (the run handle) -- never a bare pid.

    ``cwd`` DOES NOT SET THE CHILD'S WORKING DIRECTORY. It selects which
    repo's bookkeeping this fire is recorded against, and nothing else:
    ``_registry_dir``, ``count_live_fires``, and ``_publish_lag_message``
    are its only three readers. It is deliberately NOT in ``popen_kwargs``
    -- the spawned child inherits the FIRING process's cwd, so firing with
    ``cwd=<sibling repo>`` still starts a child sitting in the caller's own
    repo.

    Negative-spec: do not "fix" this by threading ``cwd`` into
    ``subprocess.Popen``. A tool the child runs would then resolve its repo
    from a directory the firing session never chose, which is the ambiguity
    the repo-identity gate exists to refuse. Anything the child must target
    outside the firing repo is passed to it EXPLICITLY -- see
    ``coordinator/bin/coordinator-tasks-mirror.py``'s ``--repo-root``, which
    exists precisely because there is no correct root to infer from a cwd
    that is the wrong repo by construction.

    The parameter name has now misled two readers into assuming it is the
    child's working directory (2026-08-30, claude-klabauter-em and
    doe-claude-e8, independently). It is documented rather than renamed
    because it is a published engine seam; a rename is a breaking change to
    every caller and belongs in its own plan.
    Raises ``ScriptNotFoundError``, ``PluginDirResolutionError``,
    ``ConcurrencyCapExceededError``, or ``ChildSpawnFailedError`` before
    ever returning a handle for a child that cannot be confirmed live.

    Concurrency-cap guarantee: the live-count read and the slot reservation
    (a "pending" registry record) happen while ``_acquire_cap_lock`` holds
    a cross-process O_EXCL lockfile, so two concurrently-invoked, cold-
    spawned ``workflow.fire`` calls cannot both observe ``live < cap`` and
    both proceed -- each sees the other's reservation once it has run.
    Everything after the lock is released (plugin-dir resolution, the
    actual spawn) runs unlocked, so lock hold time stays short. If
    resolution or spawning then fails, the reservation is discarded
    (``_discard_reservation``) so the slot frees immediately rather than
    waiting out the reclaim timeout. What this does NOT guarantee: a
    process that crashes between reserving and discarding/confirming
    leaves a stale "pending" record that occupies its slot for up to
    ``_PENDING_RECLAIM_TIMEOUT_S`` before self-healing to "exited" (see
    ``_refresh_record_state``) -- bounded and self-healing, not a
    permanent wedge, but not instantaneous either. A lock holder that
    crashes while holding the lock similarly stalls other fires for up to
    ``_CAP_LOCK_STALE_AGE_S`` before the lockfile is reclaimed.
    """
    script = Path(script_path)
    if not script.is_file():
        raise ScriptNotFoundError(f"workflow.fire: script_path does not exist: {script_path!r}")

    registry_dir = _registry_dir(cwd)
    fire_id = uuid.uuid4().hex
    record_path = _record_path(registry_dir, fire_id)

    lock_path = _acquire_cap_lock(registry_dir)
    try:
        live = count_live_fires(cwd)
        if live >= concurrency_cap:
            raise ConcurrencyCapExceededError(
                f"workflow.fire: {live} fire(s) already in flight, at or above cap {concurrency_cap}"
            )
        _write_record(record_path, {
            "fire_id": fire_id,
            "state": "pending",
            "reserved_at": time.time(),
        })
    finally:
        _release_cap_lock(lock_path)

    try:
        plugin_dir = resolve_plugin_dir()
    except PluginDirResolutionError:
        _discard_reservation(record_path)
        raise

    log_path = registry_dir / "logs" / f"{fire_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    command = build_fire_command(
        str(script), plugin_dir, model=model, max_turns=max_turns, claude_bin=claude_bin
    )

    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "env": build_fire_env(),
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    log_handle = open(log_path, "wb")
    try:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **popen_kwargs,
        )
    except OSError as exc:
        _discard_reservation(record_path)
        raise ChildSpawnFailedError(f"workflow.fire: failed to spawn {claude_bin!r}: {exc}") from exc
    finally:
        log_handle.close()

    exit_code = None
    for _ in range(_LIVENESS_POLL_ATTEMPTS):
        exit_code = process.poll()
        if exit_code is not None:
            break
        time.sleep(_LIVENESS_POLL_INTERVAL_S)

    started_at = time.time()
    if exit_code is not None and exit_code != 0:
        _discard_reservation(record_path)
        raise ChildSpawnFailedError(
            f"workflow.fire: child exited immediately with code {exit_code} "
            f"(bad plugin dir, missing binary, or a refused flag) -- see {log_path}"
        )

    state = "running" if exit_code is None else "exited"
    publish_lag_message = _publish_lag_message(cwd)
    record = {
        "fire_id": fire_id,
        "pid": process.pid,
        "script_path": str(script),
        "plugin_dir": plugin_dir,
        "claude_bin": claude_bin,
        "model": model,
        "max_turns": max_turns,
        "command": command,
        "started_at": started_at,
        "log_path": str(log_path),
        "state": state,
        "exit_code": exit_code,
        "status_checked_at": started_at,
        "log_size_bytes": _log_size_bytes(str(log_path)),
        "publish_lag_message": publish_lag_message,
    }
    return _write_record(record_path, record)


def fire_status(fire_id: str, cwd: Optional[str] = None) -> Optional[dict]:
    """Read and refresh a fire's current state from the registry.

    Returns ``None`` if no record exists for ``fire_id``. See
    ``_pid_alive``'s docstring for the best-effort caveat: a dead pid
    reports "exited" with ``exit_code: None`` (unknown), never a recovered
    real exit code, once the child has already been reaped.

    IMPORTANT -- neither ``state: "exited"`` nor ``outcome: "clean"`` means
    the fired workflow's phases did any work. Both describe the DRIVER. A
    live measurement showed a child exiting 0 with the harness's own
    ``terminal_reason: completed`` and an EMPTY ``permission_denials``
    array while every phase inside it had been refused (see module
    docstring's "Reporting hazard" section for that finding's corrected
    basis). A per-phase tool refusal is invisible at the driver's own
    summary, and this function does not attempt to detect one -- that is a
    content parse of the per-agent transcripts, deliberately out of scope
    (docs/research/2026-08-19-what-a-fired-runs-driver-summary-cannot-tell-you.md
    § Ruling).

    The reader move, in order. Take ``outcome`` for whether the run
    FINISHED, and treat the driver's own ``is_error`` /
    ``permission_denials`` as driver-scope only. Then, for whether the
    phases did WORK, ask git first -- a fired session's commits carry its
    ``Session-Id`` trailer, so ``git log --all --grep "Session-Id:
    <driver_session_id>"`` names exactly what the run landed, from this
    repo's own data plane and at the cost of one command. Read a zero there
    as a question, never a verdict: a phase that correctly found nothing to
    commit produces the same zero (two of the ten fires measured
    2026-08-19). Only when that is still ambiguous, open the harness's
    workflow journal for ``driver_session_id`` -- one ``started`` and one
    ``result`` entry per agent, carrying each agent's actual return value.
    That journal is the authoritative per-phase record; on those same ten
    fires its started-vs-returned counts agreed with ``outcome`` on every
    row. The engine records the id and does not read that store: it lives
    under the harness's private, unversioned transcript layout, which is
    not a dependency this op will take (same § Ruling).

    ``log_size_bytes`` (see ``_log_size_bytes``) remains a cheap red flag
    at ``0`` ("nothing was ever captured"), and NOT proof of success at any
    non-zero value -- a fully-refused run still prints its JSON summary.
    """
    registry_dir = _registry_dir(cwd)
    path = _record_path(registry_dir, fire_id)
    record = _read_record(path)
    if record is None:
        return None
    refreshed = _refresh_record_state(record)
    if refreshed is not record:
        _write_record(path, refreshed)
    return refreshed
