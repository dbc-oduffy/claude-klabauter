"""
coordinator_core.hooks.receiver_state_sensor — Stop/SubagentStop bookkeeping hook op:
thin wrapper over `session.receiver_state`, producing the receiver-state sibling file.

Purpose: the calling session's own PAUSED/PRODUCING/UNKNOWN verdict, and why — the
detection half of the receiver-state sensor DoE and this repo split at the transport
boundary (DoE owns hook registration + the stderr/exit-2 Stop-hook contract; this repo
owns detection). All ladder logic, the CPU cursor, and the sibling-file writer live in
`coordinator_core.session.receiver_state` — this module holds NO detection logic of its
own, matching `session_heartbeat.py`'s own "thin op over the library module" shape.

Write target: `.git/coordinator-sessions/<session_id>/receiver-state.json` — a NEW
per-session sibling file (module docstring of `session.receiver_state`; never
`meta.json`, never `state/`). Classification: MUTATING (it writes).

Input (flat scalar, via `hooks/_payload.py::field()`; "" treated as absent):
    session_id       — the coordinator session identifier whose OWN transcript/state
                        this invocation evaluates. Required; a missing session_id is a
                        silent no-op (mirrors session_heartbeat.py's own "no session_id
                        — nothing to stamp" branch).
    transcript_path   — this session's OWN transcript path. Required; without it there
                         is nothing to tail-read and the op writes an UNKNOWN verdict
                         with that reason.
    pid               — the CALLING session's own OS process id, for the CPU cursor leg
                         (`session.receiver_state.compute_cpu_cursor`). Optional — an
                         absent/unparseable pid simply skips the CPU cursor for this
                         invocation (the ladder verdict is still written).
    delegation_evidence — "true"/"false" flag (via `_payload.field()`'s bool
                         normalisation), OPTIONALLY supplied by the CALLER, for the
                         ladder's step 7 override; an absent/anything-other-than-"true"
                         value is treated as False. As of 2026-08-30 this op no longer
                         relies solely on the caller for this signal (root cause: no
                         caller ever populated it, so the override never fired in
                         production — state/audits/2026-08-30-group-em-classifier-
                         blindness.md). It now also computes its own cheap on-disk
                         signal from the subagent sidecar directory's mtimes
                         (`session.receiver_state.delegation_evidence_from_sidecar`)
                         and merges the two (`merge_delegation_evidence`) — either
                         source saying True, or the sidecar signal being unresolvable,
                         is enough to enable the override (fail toward PRODUCING).

Always returns `no_advisory()` — the product is the on-disk write side-effect, exactly
as `session_heartbeat.py` does. Never blocks a tool call; never raises into its caller
(fail-soft, matching `holder_evidence.py`'s own contract, per AC12).

All blocking I/O (the transcript tail read, the CPU-time read, the sibling-file
read/write) is wrapped in `asyncio.to_thread` — this handler's own body performs no
direct file or process I/O. `asyncio` is imported inside the handler, not at module
scope (import-budget discipline, matching every other hook op in this package).

Negative-spec:
    - Does NOT read or depend on `stable_pid` (AC13) — `pid` here is the caller-supplied
      OS pid used ONLY for a CPU-TIME sample, never fed to any liveness check. See
      `session.receiver_state`'s RAW-PID-LIVENESS floor restatement.
    - Does NOT open or read any subagent transcript to compute delegation evidence —
      only a directory listing plus each entry's `stat()` mtime (see Input above and
      `session.receiver_state.delegation_evidence_from_sidecar`'s own docstring for
      the exact bound: one `scandir` of this session's own subagents directory, never
      a wider tree walk).
    - Does NOT sleep, retry, or poll — a single point-in-time read + write per
      invocation (ipc.py DEC-2: no per-op dispatch-timeout override exists to spend on
      a slower posture).
    - DoE owns hooks.json and the transport shim; this repo ships no per-hook bin
      entrypoint for an mcp_tool-dispatched op (`install/gen_settings_hooks.py` skips
      them by construction) — nothing here registers a hook trigger.

Spec backlink: docs/plans/2026-08-14-receiver-state-sensor.md § C3
    (source memo: cross-repo/inbox/2026-08-14-doe-claude-em-receiver-state-sensor-seam.md)
"""

from __future__ import annotations

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.session import core as _session_core
from coordinator_core.session import receiver_state


def _run_sensor(
    session_id: str,
    transcript_path: str,
    pid_raw: str,
    delegation_evidence: bool,
    repo_root: "str | None",
) -> None:
    """Blocking body: tail-read + reduce the transcript, classify, sample the CPU
    cursor, and write the sibling file. Called ONLY via asyncio.to_thread() from the
    handler below — never invoked directly from async context.

    Fail-soft: any exception anywhere in this function is caught by the handler's own
    try/except (never propagated), matching AC12. This function itself also degrades
    gracefully at each step rather than raising where the underlying library functions
    already return None/False/[] for a failure.
    """
    cwd = repo_root or None
    now_epoch = _session_core.now_epoch()
    stamp_iso = _session_core.now_iso()

    if transcript_path:
        reduced_lines, _any_unparseable, _cap_reached = receiver_state.reduce_transcript_tail(
            transcript_path
        )
        transcript_mtime = _mtime_or_none(transcript_path)
    else:
        reduced_lines = []
        transcript_mtime = None

    sidecar_signal = receiver_state.delegation_evidence_from_sidecar(
        transcript_path or None, now_epoch=float(now_epoch)
    )
    merged_delegation_evidence = receiver_state.merge_delegation_evidence(
        delegation_evidence, sidecar_signal
    )

    ladder_verdict = receiver_state.classify(
        reduced_lines,
        now_epoch=float(now_epoch),
        transcript_mtime_epoch=transcript_mtime,
        delegation_evidence=merged_delegation_evidence,
    )

    cpu_cursor = None
    cpu_rate = None
    if pid_raw:
        try:
            pid = int(pid_raw)
        except ValueError:
            pid = None
        if pid is not None:
            cpu_cursor = receiver_state.compute_cpu_cursor(pid, now_epoch=float(now_epoch))
            if cpu_cursor is not None:
                previous_record = receiver_state.read_receiver_state(session_id, cwd)
                previous_cursor = _cursor_from_record(previous_record)
                cpu_rate = receiver_state.cpu_delta_rate(previous_cursor, cpu_cursor)

    final_verdict = receiver_state.resolve_verdict(ladder_verdict, cpu_rate=cpu_rate)

    receiver_state.write_receiver_state(
        session_id,
        verdict=final_verdict,
        cpu_cursor=cpu_cursor,
        stamp_iso=stamp_iso,
        cwd=cwd,
    )


def _mtime_or_none(path: str) -> "float | None":
    """os.stat().st_mtime, or None on any OSError. Blocking — see `_run_sensor`."""
    import os

    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _cursor_from_record(record: "dict | None"):
    """Recover the previously-persisted CpuCursor from a raw receiver-state record, or
    None when absent/malformed. Local helper — the shape it reads is exactly what
    `receiver_state.write_receiver_state` wrote."""
    if not isinstance(record, dict):
        return None
    raw_cursor = record.get("cpu_cursor")
    if not isinstance(raw_cursor, dict):
        return None
    try:
        return receiver_state.CpuCursor(
            cpu_seconds=float(raw_cursor["cpu_seconds"]),
            wall_clock_epoch=float(raw_cursor["wall_clock_epoch"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


@register_op("hooks.receiver_state_sensor")
async def _handler(params: dict, repo_root=None) -> dict:
    """Stop/SubagentStop bookkeeping op: write this session's receiver-state verdict.

    Reads session_id/transcript_path/pid/delegation_evidence from the flat-scalar
    input (see module docstring); resolves the session dir via `repo_root`; runs the
    entire blocking body (`_run_sensor`) inside `asyncio.to_thread`.

    Returns `no_advisory()` unconditionally — the product is the write side-effect.
    Never raises: any exception from `_run_sensor` is caught here and swallowed
    (fail-soft, AC12) — a broken sensor invocation must never surface as a tool-call
    failure.
    """
    import asyncio

    session_id = field(params, "session_id")
    if not session_id:
        return no_advisory()

    transcript_path = field(params, "transcript_path")
    pid_raw = field(params, "pid")
    delegation_evidence = field(params, "delegation_evidence") == "true"

    try:
        await asyncio.to_thread(
            _run_sensor, session_id, transcript_path, pid_raw, delegation_evidence, repo_root
        )
    except Exception:
        # Fail-soft (AC12): this op never blocks a tool call and never raises into
        # its caller. A failed sensor invocation simply leaves the sibling file at
        # its previous (or absent) state for this session.
        pass

    return no_advisory()
