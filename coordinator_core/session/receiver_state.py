"""
coordinator_core.session.receiver_state — receiver-state sensor: PAUSED / PRODUCING /
UNKNOWN verdict ladder + CPU cursor, and the per-session sibling-file writer for both.

PURE IN-PROCESS LIBRARY, not an IPC op — it self-registers nothing and touches none of
the shared op-registry files (same posture as ``session/liveness.py``'s "PURE IN-PROCESS
LIBRARY" header). ``hooks/receiver_state_sensor.py`` is the thin op wrapper that calls
into this module and owns the five-surface registration.

Purpose: answer, for a session other than itself, "is that session paused, and why" —
distinct from ``session/liveness.py``, which answers only alive-vs-dead and reports a
session sitting untouched at a permission prompt for forty minutes as fully live. This
module reads ONE session's own transcript structure (never another session's) plus a
CPU-time cursor for that same session, and writes a verdict to a NEW per-session sibling
file under ``.git/coordinator-sessions/<sid>/`` — never into ``meta.json``, never into
``state/`` (see Negative-spec).

Three parts, ported from the DoE spike's ``probe-paused.py:114-148`` with two explicit,
named departures (the UNKNOWN-override fix and the CPU cursor replacing a blocking
12s-apart double-sample):

    (a) BOUNDED TAIL READ — ``_read_tail_lines`` mirrors
        ``hooks/subagent_arrival_check.py::_read_last_nonempty_line``'s shape (chunked
        reads back from EOF, hard byte cap, never a whole-file read) but returns
        MULTIPLE trailing lines rather than one, because the verdict ladder's
        control-line walk-back (see (c)) needs to see more than the literal last line.
        This is a deliberate, minimal generalisation of the same shape, not a fourth
        transcript reader — the two functions share every constant and every failure
        posture, differing only in how many trailing lines are returned. Not refactored
        into a shared helper across the two files: ``subagent_arrival_check.py`` is
        hot-path and out of scope for this plan (see the plan's C1 body).

    (b) STRUCTURAL REDUCTION — ``_reduce_line`` extracts ONLY: ``type``, ``subtype``,
        ``timestamp``, ``message.stop_reason``, tool NAMES from
        ``content[].type == "tool_use"``, the literal sentinel string
        ``"__TOOL_RESULT__"`` for any ``content[].type == "tool_result"`` block,
        ``isSidechain``, and ``pendingBackgroundAgentCount``. The line is parsed with
        plain ``json.loads`` (no ``object_pairs_hook`` — filtering at every nesting
        level a transcript line can contain would add real complexity for no privacy
        gain over the shape below); the fully-parsed dict is local to ``_reduce_line``
        and never assigned to a name that outlives the call, so the raw `message`
        content blocks and any prose field (`text`, `content` string bodies, etc.) do
        not escape the function's scope. The returned ``_ReducedLine`` dataclass has no
        field capable of holding message text at all (see AC9 and
        ``test_receiver_state.py``'s privacy assertion, which this shape is designed
        for — not bolted on after).

    (c) VERDICT LADDER — ``classify`` — first-match-wins, ported from
        ``probe-paused.py:114-148``:
          1. system/away_summary                          -> PAUSED:away
          2. assistant + stop_reason == "tool_use":
               tool in {AskUserQuestion, ExitPlanMode}     -> PAUSED:asking-human
               else                                        -> PRODUCING:tool-in-flight,
                 downgraded to PAUSED:tool-unanswered when transcript mtime age > grace
          3. system/stop_hook_summary|turn_duration        -> PAUSED:turn-ended
          4. assistant + stop_reason == "end_turn"         -> PAUSED:turn-ended
          5. user: result-sentinel present                 -> PRODUCING:mid-turn
                   else                                     -> PRODUCING:turn-starting
          6. anything else                                 -> UNKNOWN:unmodelled (AC4:
             the unmodelled ``type``/``subtype`` pair is recorded in the returned verdict
             and, from there, in the written sibling file — never silently swallowed)
          7. override: verdict is PAUSED **or UNKNOWN**, and live delegation evidence
             exists                                        -> PRODUCING:delegated

        Step 7's "or UNKNOWN" is OUR FIX, not the spike's. The spike gates its
        delegation override on a "PAUSED" prefix only, so an UNKNOWN verdict is never
        rescued by live-subagent evidence — this fired for real in the spike's own run
        (one session with a live subagent and a 3.5s-old transcript was still returned
        UNKNOWN). AC3 requires the override apply to both.

        Two details ported verbatim because they are load-bearing and easy to lose:
        isSidechain lines are filtered out before anything else in the tail (a
        sidechain record is a different logical stream, not the session's own last
        turn), and the walk-back skips untimestamped CONTROL lines
        (``{mode, permission-mode, last-prompt, queue-operation}``) to find the real
        last substantive line.

        ``_TOOL_UNANSWERED_GRACE_SECONDS`` (step 2's 90s) is an undocumented bare
        literal in the spike whose "measured" provenance is not traceable to any
        preserved dataset. It is named here as a module constant with an HONEST
        docstring: inherited-unverified, not a measurement this module claims.

    (d) SIBLING-FILE WRITER — ``write_receiver_state`` — whole-file atomic replace into
        ``.git/coordinator-sessions/<sid>/receiver-state.json``, modeled on
        ``session/grant.py``'s WHOLE-FILE shape (create-or-overwrite via
        ``tempfile.mkstemp`` + ``os.replace`` in the session dir; no cross-process lock,
        no merge with prior content) rather than ``session/shape.py``'s merge-style
        writer — a receiver-state verdict is a single point-in-time snapshot, not an
        accumulating record, so there is nothing to merge. The directory is resolved
        through the existing seam, ``session/core.py::session_dir`` — never rebuilt by
        hand. Records: ``verdict``, ``reason``, the CPU cursor pair from part (e) below,
        a stamp, and (AC4) the unmodelled ``type``/``subtype`` when the ladder fell
        through to UNKNOWN.

    (e) CPU CURSOR — ``compute_cpu_cursor`` — one non-blocking ``cpu_times()`` read per
        invocation. NOT a fixed-window blocking sample: the spike blocks for 12 seconds
        between two ``ps`` snapshots, which has no home here (the calling hook runs on a
        5s timeout, per-op dispatch-timeout overrides were retired by decision — ipc.py
        DEC-2 — and 50-70 concurrent sessions parking 50-70 threads asleep at once is not
        something anything else in this tree does). Instead: read ``cpu_seconds`` once,
        persist ``(cpu_seconds, wall_clock)`` into the sibling file, and on the NEXT
        invocation compute the delta against the PREVIOUS stamp, normalised by elapsed
        wall time (the interval between invocations is now variable, unlike the spike's
        fixed 12s window — this is exactly why the spike's absolute constant cannot carry
        over even in principle). ``psutil`` is a declared dependency but is NEVER
        imported at module scope here — every access goes through the lazy accessor at
        ``session/core.py::_psutil()``, matching that module's own discipline. No
        ``cpu_times()`` call exists anywhere else in this tree; this is the first.

        TIEBREAK ONLY (AC7): ``resolve_verdict`` (the module-level entry point tying (c)
        and (e) together) only consults the CPU cursor to resolve an UNKNOWN ladder
        verdict — it NEVER overturns a confident PAUSED/PRODUCING verdict from the
        ladder. The ladder is pure file structure and therefore load-independent; CPU
        time is not, and on a contended box its error direction is false-PAUSED on
        genuinely-working sessions (a descheduled-but-working session accrues less CPU
        in its sampling window). Where the two disagree and the ladder was confident,
        the ladder wins and the disagreement is recorded in the verdict's ``reason``.

        Threshold: ``_CPU_FLOOR_UNDERIVED`` is a **placeholder constant explicitly
        marked underived** — see its own docstring. It is NOT seeded with the spike's
        0.15 (see Anti-scope in the plan: that figure rests on five PRODUCING samples on
        a quiet 30-session box, and this machine runs 50-70). Until a follow-up chunk
        (C4, NOT part of this dispatch) derives a real threshold from local samples under
        representative load, the CPU leg is GATED OFF (``_CPU_LEG_ENABLED = False``) —
        every CPU-tiebreak consult is a documented no-op, and UNKNOWN stays UNKNOWN
        rather than being resolved off an unvalidated number.

Fail-soft throughout (AC12): a missing, unreadable, or garbage transcript yields
UNKNOWN, never a raise — this module and its caller (the hook op) never propagate an
exception out of ``resolve_verdict`` / ``write_receiver_state``.

Does NOT read or depend on ``stable_pid`` (AC13) — that field is not stamping reliably
on this machine (see the plan's Out of scope) and this module routes around it entirely,
reading only the transcript and the CALLING PROCESS's own ``cpu_times()`` (never a PID
liveness check — see RAW-PID-LIVENESS floor below).

RAW-PID-LIVENESS floor (docs/wiki/coordinator-tripwires.md; restated here per
``session/liveness.py``'s own instruction to keep this distinction explicit at every call
site that reads process info): this module's CPU-time read is a STATE signal, not a
LIVENESS verdict. It never calls ``ps -p`` / ``kill -0`` / ``psutil.pid_exists`` on a
stored PID, and it never feeds ``core.stable_pid_alive``. Do not mistake the
``cpu_times()`` call in part (e) for a liveness check — it answers "how busy has this
process been", not "is this process alive".

Negative-spec:
    - Do NOT write to ``meta.json`` — its single-writer invariant is guarded by prose
      only (``session_heartbeat.py``'s negative-spec); a second producer buys clobber
      risk on the liveness-critical file for no gain. This module owns exactly one
      artifact: ``receiver-state.json``, a NEW sibling file.
    - Do NOT write to ``state/`` — session-runtime/liveness layer only
      (``.git/coordinator-sessions/``), same confinement as every bookkeeping op in
      ``ipc.py``'s inventory.
    - Do NOT ``sleep`` anywhere on any path — see part (e) above and ipc.py DEC-2.
    - Do NOT treat the CPU signal as co-equal, independent corroboration alongside
      ``PRODUCING:delegated``: subagent-sidecar mtime (which drives that verdict) is
      also what drives the session's own CPU consumption, so the two are correlated
      evidence dressed as corroboration, not two independent signals. The genuinely
      independent pair this module treats as tiebreak-worthy is the ladder's
      ``stop_reason`` branches vs the CPU cursor.
    - Do NOT treat the transcript line-type vocabulary as closed — an unmodelled type
      (e.g. ``attachment``, which broke the spike's ladder once in a 30-session run) is
      expected, not exceptional; it resolves to UNKNOWN:unmodelled and is recorded, not
      silently absorbed into an existing arm.
    - Do NOT hold message TEXT anywhere in this module's return values — see part (b);
      a test asserts this structurally (a distinctive sentinel fed into fixture prose
      must not survive the reduction).

Spec backlink: docs/plans/2026-08-14-receiver-state-sensor.md § C1, C2
    (source memo: cross-repo/inbox/2026-08-14-doe-claude-em-receiver-state-sensor-seam.md)
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
from typing import Any, Optional

from coordinator_core.session import core as _session_core

# ---------------------------------------------------------------------------
# Bounded tail read — mirrors hooks/subagent_arrival_check.py's constants and
# chunked-read shape exactly (see module docstring (a)).
# ---------------------------------------------------------------------------
# Generator-provenance declaration (generator_provenance.py). write_receiver_state
# writes only `.git/coordinator-sessions/<sid>/receiver-state.json` -- git-internal
# session-hub sibling state, never a tracked repo artifact.
GENERATES = []

_TAIL_CHUNK_BYTES = 8192
_TAIL_MAX_CHUNKS = 32  # bounds the tail read at ~256KB from EOF
_TAIL_CAP_BYTES = _TAIL_MAX_CHUNKS * _TAIL_CHUNK_BYTES

# Upper bound on how many trailing lines the ladder's control-line walk-back
# may inspect once the tail bytes are split into lines. Generous relative to
# the handful of control lines the walk-back is documented to skip past, while
# still bounding the work done per invocation.
_MAX_WALKBACK_LINES = 64

# Control-line "type" values that carry no timestamp and must be walked back
# past to find the real last substantive line (module docstring (c)).
_UNTIMESTAMPED_CONTROL_TYPES = frozenset(
    {"mode", "permission-mode", "last-prompt", "queue-operation"}
)

# Step 2's grace window before an in-flight tool call downgrades from
# PRODUCING:tool-in-flight to PAUSED:tool-unanswered. Inherited from the
# spike's probe-paused.py:114-148 as a bare literal with no traceable
# measurement basis — named here honestly as INHERITED-UNVERIFIED, not as a
# calibrated constant. Do not cite this as measured.
_TOOL_UNANSWERED_GRACE_SECONDS = 90

# Tools whose stop_reason=="tool_use" arm resolves to PAUSED:asking-human
# rather than PRODUCING:tool-in-flight (module docstring (c), step 2).
_ASKING_HUMAN_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Literal sentinel recorded for a tool_result content block — never the
# block's actual result payload (module docstring (b); AC9).
_TOOL_RESULT_SENTINEL = "__TOOL_RESULT__"

# ---------------------------------------------------------------------------
# CPU cursor — gated behind an explicitly-underived constant (module
# docstring (e)). C4 (a separate, later dispatch) derives a real threshold
# from local samples taken under this machine's actual 50-70 session load and
# replaces this pair. Anti-scope: do NOT seed this with the spike's 0.15 — see
# the plan's Anti-scope section for why porting it is actively unsafe here.
# ---------------------------------------------------------------------------
_CPU_LEG_ENABLED = False
_CPU_FLOOR_UNDERIVED = None  # type: Optional[float]

_SIBLING_FILENAME = "receiver-state.json"


# ---------------------------------------------------------------------------
# (a) Bounded tail read
# ---------------------------------------------------------------------------


def _read_tail_lines(path: str, *, max_lines: int = _MAX_WALKBACK_LINES) -> tuple[list[str], bool]:
    """Return (trailing non-empty lines of `path`, cap_reached), tailing from EOF.

    Same chunked-read shape and byte cap as
    hooks/subagent_arrival_check.py::_read_last_nonempty_line — grows backward from EOF
    in `_TAIL_CHUNK_BYTES` steps, never a full-file read, capped at `_TAIL_CAP_BYTES`
    (~256KB). Differs only in returning up to `max_lines` trailing lines instead of one,
    because the verdict ladder's control-line walk-back needs more than the literal last
    line to find the real last substantive record.

    Returns ([], False) on: file absent, not a regular file, unreadable for any OSError
    reason, or present-but-empty-of-non-blank-lines. Never raises.

    `cap_reached` True means the tail read hit the byte cap without finding enough
    newlines to satisfy `max_lines` — callers must not treat the returned lines as
    exhaustive in that case (there may be earlier lines this call could not see), but the
    lines that WERE recovered are still real, complete lines (a partial FIRST line at the
    start of the buffer is dropped, never yielded as a fragment).
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            if file_size == 0:
                return [], False

            buf = b""
            pos = file_size
            chunks_read = 0
            while pos > 0 and chunks_read < _TAIL_MAX_CHUNKS:
                read_size = min(_TAIL_CHUNK_BYTES, pos)
                pos -= read_size
                fh.seek(pos)
                buf = fh.read(read_size) + buf
                chunks_read += 1
                # Stop once enough newlines are present to have a decent chance at
                # max_lines complete lines, OR the whole file has been consumed.
                if buf.count(b"\n") > max_lines or pos == 0:
                    break
    except OSError:
        return [], False

    cap_reached = pos > 0 and chunks_read >= _TAIL_MAX_CHUNKS

    text = buf.decode("utf-8", errors="replace")
    raw_lines = text.splitlines()
    # The first line in the buffer may be a fragment (the chunk boundary landed
    # mid-line) unless the read reached the true start of the file (pos == 0).
    if pos > 0 and raw_lines:
        raw_lines = raw_lines[1:]
    lines = [ln.strip() for ln in raw_lines if ln.strip()]
    if not lines:
        return [], cap_reached
    return lines[-max_lines:], cap_reached


# ---------------------------------------------------------------------------
# (b) Structural reduction
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _ReducedLine:
    """The ENTIRE set of fields this module ever reads out of one transcript line.

    No field here is capable of holding message prose — `tool_names` carries only
    `content[].type == "tool_use"` block NAMES, and `has_tool_result` is a bare boolean
    standing in for any `tool_result` block's presence (AC9: "structurally incapable of
    holding prose"). There is no `text`, no `content`, no raw `message` field anywhere on
    this dataclass — that absence is the privacy guarantee, not a filter applied to a
    richer structure that happens to be discarded downstream.
    """

    type: str
    subtype: str
    timestamp: str
    stop_reason: str
    is_sidechain: bool
    pending_background_agent_count: int
    tool_names: tuple[str, ...]
    tool_result_markers: tuple[str, ...]
    parse_ok: bool

    @property
    def has_tool_result(self) -> bool:
        """True iff any content block was a tool_result — derived from the sentinel
        marker tuple rather than a separate stored boolean, so there is exactly one
        place (`tool_result_markers`) that records this fact."""
        return bool(self.tool_result_markers)


def _reduce_line(raw_line: str) -> Optional[_ReducedLine]:
    """Parse one raw JSONL line and reduce it to `_ReducedLine`, extracting structure
    ONLY — never holding message text.

    Returns None when `raw_line` is not valid JSON or does not parse to a JSON object;
    this is the module's own "unparseable" signal, distinct from `_ReducedLine`'s
    `parse_ok` field (which records whether the record's nested `message` sub-object, if
    any, parsed as an object — a record can be valid top-level JSON while its `message`
    is malformed).

    Reduction happens as this function returns — the parsed `record` dict (which DOES
    contain prose, e.g. `content[].text`) is entirely local to this function and is
    never assigned into a name the caller can see; only the fields copied onto
    `_ReducedLine` escape this function's scope.
    """
    try:
        record: Any = json.loads(raw_line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None

    line_type = record.get("type")
    line_type = line_type if isinstance(line_type, str) else ""
    subtype = record.get("subtype")
    subtype = subtype if isinstance(subtype, str) else ""
    timestamp = record.get("timestamp")
    timestamp = timestamp if isinstance(timestamp, str) else ""
    is_sidechain = bool(record.get("isSidechain", False))
    pending_count = record.get("pendingBackgroundAgentCount", 0)
    pending_count = pending_count if isinstance(pending_count, int) else 0

    message = record.get("message")
    stop_reason = ""
    tool_names: list[str] = []
    tool_result_markers: list[str] = []
    parse_ok = True
    if isinstance(message, dict):
        raw_stop_reason = message.get("stop_reason")
        stop_reason = raw_stop_reason if isinstance(raw_stop_reason, str) else ""
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "tool_use":
                    name = block.get("name")
                    if isinstance(name, str):
                        tool_names.append(name)
                elif block_type == "tool_result":
                    # The literal sentinel, never the block's own (possibly
                    # prose-bearing) `content`/`output` payload (AC9).
                    tool_result_markers.append(_TOOL_RESULT_SENTINEL)
    elif message is not None:
        parse_ok = False

    return _ReducedLine(
        type=line_type,
        subtype=subtype,
        timestamp=timestamp,
        stop_reason=stop_reason,
        is_sidechain=is_sidechain,
        pending_background_agent_count=pending_count,
        tool_names=tuple(tool_names),
        tool_result_markers=tuple(tool_result_markers),
        parse_ok=parse_ok,
    )


# ---------------------------------------------------------------------------
# (c) Verdict ladder
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Verdict:
    """A single classify() outcome. `verdict` is always one of the bare tags "PAUSED",
    "PRODUCING", or "UNKNOWN" — the reason tag lives in the separate `reason` field, NOT
    appended onto `verdict` as "PAUSED:<reason-tag>". Every construction site (and the
    written sibling-file record, which stores `verdict` and `reason` as separate JSON
    fields) follows this split-field shape; `verdict` never contains transcript
    prose."""

    verdict: str
    reason: str
    unmodelled_type: str = ""
    unmodelled_subtype: str = ""


def _select_last_substantive_line(reduced_lines: list[_ReducedLine]) -> Optional[_ReducedLine]:
    """Filter isSidechain lines, then walk back past untimestamped control lines to find
    the real last substantive line. Returns None if nothing survives the filter."""
    candidates = [ln for ln in reduced_lines if not ln.is_sidechain]
    for ln in reversed(candidates):
        if ln.type in _UNTIMESTAMPED_CONTROL_TYPES:
            continue
        return ln
    return None


def _has_live_delegation_evidence(delegation_evidence: bool) -> bool:
    """Thin pass-through — kept as its own function so the ladder's step 7 reads as a
    named predicate rather than a bare parameter, and so a future richer delegation
    check (subagent-sidecar mtime lookup) has a single call site to grow into. That
    lookup itself is NOT this module's job (Anti-scope: correlated-not-independent —
    see module docstring); the caller supplies the boolean."""
    return bool(delegation_evidence)


def classify(
    reduced_lines: list[_ReducedLine],
    *,
    now_epoch: float,
    transcript_mtime_epoch: Optional[float],
    delegation_evidence: bool,
) -> Verdict:
    """First-match-wins verdict ladder, ported from probe-paused.py:114-148.

    `reduced_lines` — trailing reduced lines, OLDEST FIRST (caller's responsibility to
    order them this way; `_read_tail_lines` returns them in on-disk order already).
    `now_epoch` — caller-supplied "now", so this function is deterministic/testable.
    `transcript_mtime_epoch` — the transcript file's on-disk mtime, used ONLY by step 2's
    grace-window downgrade; None (mtime unavailable) is treated as "no age evidence",
    which keeps the PRODUCING:tool-in-flight verdict rather than downgrading it (fails
    toward NOT assuming staleness, since there is nothing to measure staleness against).
    `delegation_evidence` — True when the caller has independently confirmed live
    subagent delegation for this session; this function does not derive it.

    Never raises: an empty/all-filtered `reduced_lines` resolves to UNKNOWN:no-substantive-line.
    """
    last = _select_last_substantive_line(reduced_lines)
    if last is None:
        verdict = Verdict("UNKNOWN", "no substantive line survived the isSidechain filter and control-line walk-back")
    else:
        verdict = _classify_one(last, now_epoch=now_epoch, transcript_mtime_epoch=transcript_mtime_epoch)

    is_paused_or_unknown = verdict.verdict.startswith("PAUSED") or verdict.verdict.startswith("UNKNOWN")
    if is_paused_or_unknown and _has_live_delegation_evidence(delegation_evidence):
        # Step 7 override — OUR FIX over the spike (module docstring (c)): applies to
        # UNKNOWN as well as PAUSED. Preserve the fallen-through UNKNOWN detail (AC4)
        # even though the verdict itself is overridden, so the sibling file still
        # records what the ladder actually saw.
        return Verdict(
            "PRODUCING",
            f"delegated (overrides {verdict.verdict}: {verdict.reason})",
            unmodelled_type=verdict.unmodelled_type,
            unmodelled_subtype=verdict.unmodelled_subtype,
        )
    return verdict


def _classify_one(
    last: _ReducedLine, *, now_epoch: float, transcript_mtime_epoch: Optional[float]
) -> Verdict:
    """Steps 1-6 of the ladder, applied to the single last-substantive reduced line."""
    if last.type == "system" and last.subtype == "away_summary":
        return Verdict("PAUSED", "away")

    if last.type == "assistant" and last.stop_reason == "tool_use":
        if any(name in _ASKING_HUMAN_TOOLS for name in last.tool_names):
            return Verdict("PAUSED", "asking-human")
        if transcript_mtime_epoch is not None:
            age = now_epoch - transcript_mtime_epoch
            if age > _TOOL_UNANSWERED_GRACE_SECONDS:
                return Verdict(
                    "PAUSED",
                    f"tool-unanswered ({age:.1f}s since transcript mtime, "
                    f"grace {_TOOL_UNANSWERED_GRACE_SECONDS}s)",
                )
        return Verdict("PRODUCING", "tool-in-flight")

    if last.type == "system" and last.subtype in ("stop_hook_summary", "turn_duration"):
        return Verdict("PAUSED", "turn-ended")

    if last.type == "assistant" and last.stop_reason == "end_turn":
        return Verdict("PAUSED", "turn-ended")

    if last.type == "user":
        if last.has_tool_result:
            return Verdict("PRODUCING", "mid-turn")
        return Verdict("PRODUCING", "turn-starting")

    return Verdict(
        "UNKNOWN",
        f"unmodelled line type={last.type!r} subtype={last.subtype!r}",
        unmodelled_type=last.type,
        unmodelled_subtype=last.subtype,
    )


# ---------------------------------------------------------------------------
# (e) CPU cursor — non-blocking per-invocation delta, tiebreak only
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CpuCursor:
    """One invocation's CPU-time sample pair, as persisted to the sibling file."""

    cpu_seconds: float
    wall_clock_epoch: float


def read_cpu_times_for_pid(pid: int) -> Optional[float]:
    """Return `pid`'s total CPU seconds (user + system) via the lazy psutil accessor, or
    None on any failure (process gone, psutil absent, permission denied).

    Blocking (touches /proc or a platform equivalent) — callers MUST invoke this via
    asyncio.to_thread(), never directly from an async handler. This module itself never
    awaits; the op wrapper (hooks/receiver_state_sensor.py) owns the to_thread wrapping.

    Never raises. NOT a liveness check (see module docstring's RAW-PID-LIVENESS floor
    restatement) — a None return here means "no CPU sample available", not "process
    dead"; callers must not conflate the two.
    """
    psutil_mod = _session_core._psutil()
    if psutil_mod is None:
        return None
    try:
        proc = psutil_mod.Process(pid)
        times = proc.cpu_times()
    except Exception:
        return None
    try:
        return float(times.user) + float(times.system)
    except (AttributeError, TypeError, ValueError):
        return None


def compute_cpu_cursor(pid: int, *, now_epoch: float) -> Optional[CpuCursor]:
    """One non-blocking-per-call cpu_times() read, packaged as the (cpu_seconds,
    wall_clock) pair this module persists. Returns None when the read failed (see
    `read_cpu_times_for_pid`).

    "Non-blocking" here means "does not sleep" — cpu_times() itself is a blocking
    syscall/proc-read like any other, which is why callers route it through
    asyncio.to_thread (see that function's own docstring). This function makes exactly
    ONE such read; it never samples twice or waits between samples (that is the entire
    point of the cursor replacing the spike's blocking double-sample — see module
    docstring (e)).
    """
    cpu_seconds = read_cpu_times_for_pid(pid)
    if cpu_seconds is None:
        return None
    return CpuCursor(cpu_seconds=cpu_seconds, wall_clock_epoch=now_epoch)


def cpu_delta_rate(previous: Optional[CpuCursor], current: CpuCursor) -> Optional[float]:
    """CPU-seconds-per-wall-second, normalised over the interval between two cursors.

    Returns None when there is no previous cursor to diff against (first-ever
    invocation for this session), or when the wall-clock interval is non-positive
    (clock skew, or two invocations landing in the same epoch second) — a rate is not
    computable in either case, and this function never divides by zero or returns a
    negative/undefined rate.
    """
    if previous is None:
        return None
    wall_delta = current.wall_clock_epoch - previous.wall_clock_epoch
    if wall_delta <= 0:
        return None
    cpu_delta = current.cpu_seconds - previous.cpu_seconds
    if cpu_delta < 0:
        # A CPU-seconds regression means the counter source changed underneath us
        # (process restart reusing the same pid, or a bogus previous sample) — not a
        # negative rate, no signal.
        return None
    return cpu_delta / wall_delta


def resolve_cpu_tiebreak(rate: Optional[float]) -> Optional[str]:
    """Map a CPU delta-rate to a tiebreak verdict tag, or None when the CPU leg is
    disabled/unavailable/inconclusive.

    TIEBREAK ONLY (AC7) — this function's return value is consulted by
    `resolve_verdict` ONLY when the ladder itself produced UNKNOWN; it must never be
    used to overturn a confident PAUSED/PRODUCING ladder verdict, and this function
    itself has no way to overturn anything — it only ever proposes.

    Currently ALWAYS returns None: `_CPU_LEG_ENABLED` is False (see that constant's
    docstring) until C4 (a separate, later dispatch) derives a real threshold from
    local samples under representative load. This is not a no-op bug — it is the
    documented, deliberate state of the gate until that derivation lands.
    """
    if not _CPU_LEG_ENABLED or _CPU_FLOOR_UNDERIVED is None or rate is None:
        return None
    if rate >= _CPU_FLOOR_UNDERIVED:
        return "PRODUCING:cpu-tiebreak"
    return "PAUSED:cpu-tiebreak"


def resolve_verdict(
    ladder_verdict: Verdict,
    *,
    cpu_rate: Optional[float],
) -> Verdict:
    """Apply the CPU tiebreak (AC7: UNKNOWN-only) on top of the ladder's own verdict.

    Never overturns a PAUSED/PRODUCING ladder verdict — only an UNKNOWN one may be
    resolved by the CPU signal, and only while `_CPU_LEG_ENABLED` is True (currently
    never — see `resolve_cpu_tiebreak`). Any disagreement between a confident ladder
    verdict and what the CPU signal WOULD have said is not computed here at all
    (nothing to disagree with when the ladder already won); this function's contract
    is purely "may I resolve an UNKNOWN", not "does the CPU signal agree with a
    confident verdict".
    """
    if not ladder_verdict.verdict.startswith("UNKNOWN"):
        return ladder_verdict
    tiebreak = resolve_cpu_tiebreak(cpu_rate)
    if tiebreak is None:
        return ladder_verdict
    tag, _, reason_tag = tiebreak.partition(":")
    return Verdict(
        tag,
        f"{reason_tag} resolved ladder UNKNOWN ({ladder_verdict.reason})",
        unmodelled_type=ladder_verdict.unmodelled_type,
        unmodelled_subtype=ladder_verdict.unmodelled_subtype,
    )


# ---------------------------------------------------------------------------
# (d) Sibling-file writer + reader
# ---------------------------------------------------------------------------

_SAFE_SID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _sibling_path(sid: str, cwd: Optional[str] = None) -> Optional[str]:
    """Resolve `.git/coordinator-sessions/<sid>/receiver-state.json`, or None if the
    session dir is unresolvable (not in a git repo) or `sid` is not a safe path
    component. Every public read/write function in this module routes through here —
    the single location-naming seam, matching session/grant.py's own convention."""
    if not sid or not _SAFE_SID_RE.match(sid):
        return None
    sdir = _session_core.session_dir(sid, cwd)
    if not sdir:
        return None
    return os.path.join(sdir, _SIBLING_FILENAME)


def read_receiver_state(sid: str, cwd: Optional[str] = None) -> Optional[dict]:
    """Raw reader — the previously-written receiver-state record for `sid`, or None if
    absent/unreadable/malformed. No liveness check; this is the raw artifact, primarily
    useful so `compute_cpu_cursor`'s caller can recover the PREVIOUS cursor to diff
    against (see `cpu_delta_rate`). Never raises."""
    path = _sibling_path(sid, cwd)
    if path is None or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return record


def write_receiver_state(
    sid: str,
    *,
    verdict: Verdict,
    cpu_cursor: Optional[CpuCursor],
    stamp_iso: str,
    cwd: Optional[str] = None,
) -> bool:
    """Whole-file atomic replace of `.git/coordinator-sessions/<sid>/receiver-state.json`
    — modeled on session/grant.py's WHOLE-FILE writer shape (tempfile.mkstemp + os.replace
    in the session dir; create-or-overwrite; no merge with prior content, no
    cross-process lock), NOT session/shape.py's merge-style writer. A receiver-state
    verdict is a single point-in-time snapshot with exactly one writer (this session's
    own sensor invocation), so there is nothing to merge.

    Blocking (tempfile + os.replace) — callers MUST invoke this via asyncio.to_thread(),
    never directly from an async handler.

    Returns True on success; False on ANY infra failure (session id unresolvable/unsafe,
    session dir uncreatable, write/replace failure). Never raises.
    """
    path = _sibling_path(sid, cwd)
    if path is None:
        return False
    sdir = os.path.dirname(path)
    try:
        os.makedirs(sdir, exist_ok=True)
    except OSError:
        return False

    record: dict[str, Any] = {
        "schema_version": 1,
        "session_id": sid,
        "verdict": verdict.verdict,
        "reason": verdict.reason,
        "stamped_at": stamp_iso,
    }
    if verdict.unmodelled_type:
        record["unmodelled_type"] = verdict.unmodelled_type
        record["unmodelled_subtype"] = verdict.unmodelled_subtype
    if cpu_cursor is not None:
        record["cpu_cursor"] = {
            "cpu_seconds": cpu_cursor.cpu_seconds,
            "wall_clock_epoch": cpu_cursor.wall_clock_epoch,
        }

    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{_SIBLING_FILENAME}.", dir=sdir)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
            fh.write("\n")
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        return False
    return True


# ---------------------------------------------------------------------------
# Public entry point: transcript path -> reduced lines
# ---------------------------------------------------------------------------


def reduce_transcript_tail(transcript_path: str) -> tuple[list[_ReducedLine], bool, bool]:
    """Read + reduce the trailing lines of `transcript_path`.

    Returns (reduced_lines oldest-first, any_unparseable, cap_reached). `reduced_lines`
    is [] when the file is absent/unreadable/empty or every trailing line failed to
    parse — callers treat that as ladder input yielding UNKNOWN via
    `_select_last_substantive_line` returning None, never a raise.

    Blocking (file I/O) — callers MUST invoke this via asyncio.to_thread(), never
    directly from an async handler.
    """
    raw_lines, cap_reached = _read_tail_lines(transcript_path)
    reduced: list[_ReducedLine] = []
    any_unparseable = False
    for raw_line in raw_lines:
        line = _reduce_line(raw_line)
        if line is None:
            any_unparseable = True
            continue
        reduced.append(line)
    return reduced, any_unparseable, cap_reached
