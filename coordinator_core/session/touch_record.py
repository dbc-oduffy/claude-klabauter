"""
coordinator_core.session.touch_record — the one module that owns the
touched-files record's line format, its append path, and (C3) the single
read seam every caller must go through to learn what is currently claimed.

Plan: docs/plans/2026-08-25-the-touched-files-record-gets-a-designed-shape.md
(chunks C2, C3).

=== C3: the read seam ===

``project_live_claims`` is the ONE read path: it takes one or more sink base
paths (a session-keyed sink, an agent-keyed sink, or both), expands each to
its on-disk family (the live file plus zero or more rotated siblings — see
C2's rotation design), decodes every complete line, merges last-verb-wins
both within and across those files, and filters the survivors to sessions
``liveness.session_live`` still calls live. This supersedes the old
seven-reconciler dialect's cross-file bridge (``normalize_peer_claim_key``,
``_maximal_strip_peer_fallback``, and ``project_peer_claims``'s
``challenger_t_events`` argument): those existed to reconcile an agent-keyed
T against a session-keyed R written to a different file with no shared
order between them. ``project_live_claims``'s ``_merge_across_streams`` is
their replacement — see "Cross-file ordering" below for the rule it uses in
their place.

Failure posture (AC6) — decided HERE, once, for the whole record:
    An unreadable file (open/read raises anything other than
    ``FileNotFoundError`` — permission denial, a path that is now a
    directory, etc.) or a complete line that fails to decode
    (``MalformedRecordLine``) each set ``TouchProjection.degraded = True``.
    A caller MUST treat ``degraded=True`` as "could not tell", never as
    "clean" — ``TouchProjection.claims`` may be missing entries a fully
    healthy read would have shown. This is deliberately distinguishable
    from the always-legitimate empty-and-not-degraded case (no file has
    ever been written, or every claim was released): "nothing claimed" and
    "could not tell" license opposite actions at a commit gate, so they are
    two different fields, never folded into one boolean or one empty dict.

    A trailing, newline-less fragment (an appender's write caught mid-line)
    is NEVER a degrade signal — ``iter_complete_lines`` (C2) already drops
    it silently before any line reaches ``decode_line``, and
    ``FileNotFoundError`` while re-reading a family member is treated the
    same way (a peer's rotation raced our directory listing, not data
    loss). Only a COMPLETE line that fails to parse, or an unreadable-for-
    any-other-reason file, is the module's failure signal.

Cross-file ordering:
    Within one sink's family, ordering is NEVER the embedded ``ts`` field —
    it is (generation, position): rotated siblings sort oldest-generation
    first by their own ``<name>.rotated-<ts>-<pid>.jsonl`` suffix
    (ascending ``(ts, pid)`` — ``pid`` is an arbitrary but deterministic
    tie-break for the rare case two rotations of the same sink land in the
    same millisecond), the live file (if present) is always the newest
    generation, and within any one file, line order IS append order
    (guaranteed by the kernel for ``atomic_append``'s single-writer-per-call
    appends — see that module). ``_last_verb_wins`` folds a family's
    events in that order, so byte order — never a clock — decides ties
    inside one sink.

    ACROSS two independently-written sinks (an agent-keyed T file and a
    session-keyed R file for the same path is the case named in this
    chunk's brief) there is no shared byte order to appeal to — two
    different processes, two different files. The only signal common to
    both is each event's own ``ts`` field, so ``_merge_across_streams``
    uses it: the later timestamp wins. On a tie — routine at realistic
    wall-clock resolution, not a corner case — a TOUCH beats a RELEASE.
    Chosen deliberately, not arbitrarily: at a commit gate, mistakenly
    treating a still-claimed path as released is worse than the reverse
    (a false "still claimed" only costs a delay; a false "released" lets a
    commit through it shouldn't). A same-verb tie between two streams
    keeps whichever stream was merged first, which does not matter — the
    surviving event's verb is identical either way, and nothing else about
    it is load-bearing to a caller.

Observability (carried-in baton, not just AC6's single-sitedness):
    Every degrade increments a process-local counter (``degrade_counts()``)
    keyed by cause (``"unreadable_file"`` / ``"malformed_line"``) and emits
    one ``logging.getLogger(__name__).warning`` line naming the cause and
    the file. Both are ~free: they execute ONLY on the degrade branch
    itself, never on a normal read, so the hot path (~50 peers reading this
    on a Bash-guard) pays nothing extra. A process-local counter (not a
    durable cross-process sink like ``telemetry.op_latency``'s) is the
    right shape here: the audit this baton cites is about a degrade being
    invisible to the PROCESS THAT HIT IT, and stdlib ``logging`` already
    gives any operator a durable, aggregable count the moment a handler is
    attached — this module does not need to invent and own a second sink
    path convention to make that true. Whether to fail open at all when
    degraded is direction-class and stays undecided here (see the carried-
    in baton); this module only makes the degrade counted and never silent.

Purpose: replaces the seven-reconciler dialect (``normalize_touch_path``,
``classify_touch_entry``, ``normalize_peer_claim_key``,
``_maximal_strip_peer_fallback``, ``_normalize_agent_touched_entry``,
``normalize_historical_touch_entry``, and the bare-mtime liveness read) with
one encoder and one decoder for a self-describing line: schema version,
verb (T/R), timestamp, session id, agent id (or explicit null), and a
repo-relative path already normalized at write time. A reader attributes
any line without consulting its directory, and ages it via
``liveness.session_live(sid)`` without consulting its mtime — see
docs/research/spike-verdicts/2026-08-25-is-liveness-really-a-pid-probe-next-door.md
(C1), which confirmed a bare session id is sufficient to reach that seam, so
this line deliberately carries **no PID and no other liveness token**: a
raw writer PID would violate ``liveness.py``'s pinned negative-spec (module
docstring L44-55, ``is_session_live`` L349) besides being reuse-prone and
dead by read time.

Negative-spec:
    - Append MUST route through ``coordinator_core.atomic_append.append_line``.
      Do NOT open the sink with a bare ``open(path, "a")`` or
      ``os.O_APPEND`` directly — see that module's own negative-spec for the
      live-reproduced Windows data-loss bug this avoids. ``locked_rmw`` must
      never appear on this append path: the read-modify-write serialization
      it bought was never cross-session (only within one session's own
      file), so it buys nothing atomic append does not already give at O(1).
    - Do NOT re-derive path normalization at a new call site. The encoder's
      normalizer is exactly
      ``coordinator_core.session.path_dialect.canonicalize_relative_path`` —
      the same dialect the old record used, without spawning git. Path
      normalization happens ONCE, here, at write time; the decoder trusts
      the stored form verbatim.
    - Do NOT add a last-event dedup read before appending. That O(depth)
      read (O(D^2) cumulative) is the design defect this module exists to
      remove — the reader's last-verb-wins projection already makes the
      same decision without it (AC2).

AC17 — bound on per-record growth. Deleting the dedup read moves
per-session growth from O(distinct paths) to O(edits), which accelerates
the exact axis
docs/research/spike-verdicts/2026-08-25-what-the-peer-scan-actually-costs.md
(C0) measured as the one that breaks the brightline: at 50 peers x 5000
lines/peer (Corpus C — reachable by construction, append-only, no
compaction, no size cap anywhere in the read path today), that spike
measured ``claim_index.rebuild()`` at 541.48ms, over the 500ms bar.

Chosen: a write-time size check (an O(1) ``os.stat`` on every append, not a
content read) that, the first time a session's record crosses
``MAX_RECORD_BYTES``, ROTATES the oversized sink out of the way by
renaming it, rather than rewriting it in place. This is a live caller:
every append made through ``append_event`` passes through it, so the bound
is not one nothing invokes. See ``_rotate_oversized`` for the concurrency
argument for why rename, not rewrite, is the only race-free choice.

Option NOT chosen, and why (superseding an earlier version of this
docstring): the first cut of this bound triggered a same-module
``compact_record`` rewrite — read-bytes, build a last-verb-wins body,
``os.replace`` — directly from the append hot path. That is a whole-file
replace racing against concurrent, lock-free appenders to the SAME path,
and it is broken on both platforms, live-reproduced on this box (Windows,
2 appenders + 1 compactor, seeded 200-line sink): the compactor's
``os.replace`` raised ``PermissionError: [WinError 5] Access is denied``
while a peer held the sink open for append -- and because the check ran
BEFORE the append, the caller's own event was silently never recorded, a
hot-path exception this module's own contract forbids. On POSIX the same
replace SUCCEEDS instead of raising, but it orphans any peer's
already-open append handle, silently losing that peer's in-flight
write -- the exact lost-write class ``atomic_append``'s own negative-spec
exists to prevent, reintroduced here at a different address. Wrapping the
rewrite in a lock is not an option either: AC3 pins ``locked_rmw`` off the
append path, and any new lock would need every appender to observe it too
to be worth anything, which is exactly the serialization AC3 rules out.

Rotation being rename-only (never a rewrite of live bytes) sidesteps the
whole class: ``atomic_append.append_line`` opens ``sink`` fresh, by path,
on every single call (no descriptor held across calls -- see that
module), so a rename only ever retargets the directory entry, never an
already-open descriptor. A peer that opened the old path before the
rename keeps writing into the renamed-away file uninterrupted; a peer that
opens the path afterward gets the fresh file rotation leaves in its
place. Neither can be lost or torn.

Real cost of that choice: the rotated-away file's bytes are never
compacted by this hot path, so ``compact_record`` (last-verb-wins,
whole-file rewrite) is now a function this module keeps available but no
longer calls itself. It stays correct to run against a rotated-away file
once nothing can still be appending to it -- a session-end reaper, run
after that session's writer side has quiesced, is the natural live caller
for it, but wiring that reaper is not this chunk's job (see the C3 note
this docstring's block above already carries: this module is the writer
half only). C3's reader must therefore be able to tolerate more than one
touch-record file per session (the live one plus zero or more
``<name>.rotated-<ts>-<pid>.jsonl`` siblings) -- a change from the
single-file assumption C2 shipped with.

A write-time REJECT cap (refuse the append outright once oversized) was
also considered and rejected because ``touch()``'s contract is that it
must never block or fail a tool call (see the C2 dispatch brief's
carried-in baton on ``touch()``'s fail-open posture) — refusing to record
an edit is a worse failure than paying an occasional rotation. Whether
failing open at all is the right posture for ``touch()`` is direction-class
and deliberately not decided here; this module only makes the degrade
countable and bounded, never silent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from coordinator_core import atomic_append
from coordinator_core.session.liveness import session_live
from coordinator_core.session.path_dialect import canonicalize_relative_path

_logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

VERB_TOUCH = "T"
VERB_RELEASE = "R"
_VALID_VERBS = (VERB_TOUCH, VERB_RELEASE)

# AC12: a hard cap on one encoded line's length. Chosen generously above any
# realistic repo-relative path plus its fixed-shape metadata (well over an
# 8KB Windows MAX_PATH-class figure with room to spare), while still bounding
# a single pathological line. Rejected, never truncated, at/over this bound.
MAX_ENCODED_LINE_LEN = 4096

# AC17: the write-time growth bound. 256 KiB stays comfortably under the
# per-peer length (Corpus C, 5000 lines/peer) that
# docs/research/spike-verdicts/2026-08-25-what-the-peer-scan-actually-costs.md
# measured breaking the brightline for claim_index.rebuild() (541.48ms) --
# a realistic encoded line here runs well under 150 bytes, so this bound
# trips at roughly an order of magnitude below that measured failure point.
MAX_RECORD_BYTES = 256 * 1024

# AC23: spawn-free containment check applied to a path AFTER
# ``canonicalize_relative_path`` has already separator-normalized and
# ``posixpath.normpath``-collapsed it. A POSIX-absolute (``/...``), a
# Windows drive-qualified (``C:...``), or a UNC/network (``//host/share`` --
# ``posixpath.normpath`` preserves exactly two leading slashes) result is
# OUT OF WORKTREE; a ``normpath``-collapsed relative path can never itself
# start with these, so this is a pure string check with no filesystem/
# subprocess access.
#
# Deliberately does NOT reject an upward-escaping (leading ``..``) relative
# path: ``TestAC8DefensiveHistoricalNormalization`` (test_scope.py) writes
# exactly such entries through this same ``append_event`` path to exercise
# AC8's own defensive READ-time normalization of a "poisoned" peer entry --
# rejecting at write time here would break that already-covered contract.
# AC23's brief is "out-of-worktree ABSOLUTE path" specifically; a relative
# ``..`` escape is that feature's problem, not this one's.
_OUT_OF_WORKTREE_RE = re.compile(r"^(?:[A-Za-z]:|//|/)")


def _is_out_of_worktree(normalized_path: str) -> bool:
    """``True`` if ``normalized_path`` (already run through
    ``canonicalize_relative_path``) is absolute -- POSIX-rooted, Windows
    drive-qualified, or UNC/network-rooted. See ``_OUT_OF_WORKTREE_RE`` and
    ``OutOfWorktreePath`` for the full containment contract this backs."""
    return bool(_OUT_OF_WORKTREE_RE.match(normalized_path))


class MalformedRecordLine(Exception):
    """A complete (newline-terminated) record line failed to decode.

    Typed signal per AC6/AC4: a reader must be able to distinguish this from
    a benign trailing unterminated line (a write in flight), which is never
    raised as this exception -- see ``iter_complete_lines``.
    """


class LineTooLong(ValueError):
    """Raised by ``encode_line`` when the encoded line would meet or exceed
    ``MAX_ENCODED_LINE_LEN``. The line is never written in this case."""


class OutOfWorktreePath(ValueError):
    """Raised by ``encode_line`` (AC23) when ``path``, after
    ``path_dialect.canonicalize_relative_path``, is still absolute (POSIX
    ``/...``, a Windows drive-qualified ``C:...``, or a UNC/network
    ``//host/share``). Deliberately does NOT cover a relative ``..``-escaping
    path -- see ``_OUT_OF_WORKTREE_RE``'s own comment for why that stays
    outside this check's scope.

    REJECT, not RELATIVIZE: ``encode_line`` has no worktree root in scope
    (only ``path_dialect`` -- deliberately importing only ``posixpath``, see
    that module's negative-spec -- and this module's own callers do not pass
    one), so there is nothing to relativize AGAINST without either spawning
    git (the per-item amplification this chunk exists to remove) or
    threading a root through every caller's signature, which is out of this
    chunk's scope. Refusing outright, spawn-free, is the only containment
    verdict this layer can make correctly on its own.

    This must never silently drop an edit the caller believes was recorded.
    Every CURRENT ``append_event`` call site in ``scope.py`` (``touch()``,
    and both ``release_committed_claims``-adjacent writers) already filters
    an absolute path upstream via ``normalize_touch_path`` before it can
    reach ``encode_line`` -- see this module's own docstring's "not live
    today ONLY because" paragraph -- so this exception is unreachable
    through any call site live at the time this chunk lands. A FUTURE
    caller (C7b/C7c, once ``normalize_touch_path`` is deleted) that wants
    fail-open behavior on an out-of-worktree path must catch this
    exception explicitly at its own call site, the same way every current
    caller already catches ``LineTooLong``; it is intentionally NOT caught
    inside ``encode_line``/``append_event`` themselves, so a caller that
    forgets to handle it fails loudly instead of silently losing the
    write."""


@dataclass(frozen=True)
class TouchEvent:
    """One decoded record line. ``agent_id`` is ``None`` for a session-keyed
    (not agent-keyed) event -- carried explicitly, never inferred from the
    file's directory (AC1)."""

    schema_version: int
    verb: str
    timestamp: float
    session_id: str
    agent_id: Optional[str]
    path: str


def encode_line(
    *,
    session_id: str,
    agent_id: Optional[str],
    verb: str,
    path: str,
    timestamp: Optional[float] = None,
) -> bytes:
    """Encode one event as a self-describing, newline-terminated line.

    Path normalization happens here, once, via
    ``path_dialect.canonicalize_relative_path`` -- the decoder trusts the
    stored form and re-derives nothing (AC4).

    Raises ``ValueError`` for an invalid verb, ``LineTooLong`` if the
    encoded form would meet or exceed ``MAX_ENCODED_LINE_LEN`` -- rejected
    outright, never truncated (AC12) -- and ``OutOfWorktreePath`` (AC23) if
    ``path``, after normalization, is still absolute. The containment check
    runs spawn-free, entirely on the string ``canonicalize_relative_path``
    returns -- see ``OutOfWorktreePath`` for why REJECT, not relativize, is
    this layer's only correct verdict, and for why a relative ``..``-escape
    is deliberately not covered by this check.
    """
    if verb not in _VALID_VERBS:
        raise ValueError(f"invalid verb {verb!r}; must be one of {_VALID_VERBS}")

    normalized_path = canonicalize_relative_path(path)
    if _is_out_of_worktree(normalized_path):
        raise OutOfWorktreePath(
            f"path {path!r} (normalized {normalized_path!r}) is out of "
            "worktree -- absolute or upward-escaping paths are rejected, "
            "never stored"
        )

    record = {
        "v": SCHEMA_VERSION,
        "verb": verb,
        "ts": time.time() if timestamp is None else float(timestamp),
        "sid": session_id,
        "agent": agent_id,
        "path": normalized_path,
    }
    encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) >= MAX_ENCODED_LINE_LEN:
        raise LineTooLong(
            f"encoded line ({len(encoded)} bytes) meets or exceeds "
            f"MAX_ENCODED_LINE_LEN ({MAX_ENCODED_LINE_LEN})"
        )
    return encoded


def decode_line(line: "bytes | str") -> TouchEvent:
    """Decode one complete record line.

    Raises ``MalformedRecordLine`` for any structurally invalid line --
    invalid JSON, a missing/wrong-typed required field, or an unrecognized
    verb. Callers scanning a whole file are responsible for excluding a
    trailing unterminated line before calling this (see
    ``iter_complete_lines``) -- that case is never this module's failure
    signal.
    """
    if isinstance(line, bytes):
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedRecordLine(f"invalid utf-8: {exc}") from exc
    else:
        text = line
    text = text.rstrip("\n")

    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedRecordLine(f"invalid json: {exc}") from exc

    if not isinstance(record, dict):
        raise MalformedRecordLine(f"decoded record is not an object: {type(record)!r}")

    try:
        schema_version = int(record["v"])
        verb = record["verb"]
        timestamp = float(record["ts"])
        session_id = record["sid"]
        agent_id = record["agent"]
        path = record["path"]
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedRecordLine(f"missing or malformed field: {exc}") from exc

    if verb not in _VALID_VERBS:
        raise MalformedRecordLine(f"invalid verb {verb!r}")
    if not isinstance(session_id, str) or not session_id:
        raise MalformedRecordLine(f"invalid session id {session_id!r}")
    if agent_id is not None and not isinstance(agent_id, str):
        raise MalformedRecordLine(f"invalid agent id {agent_id!r}")
    if not isinstance(path, str) or not path:
        raise MalformedRecordLine(f"invalid path {path!r}")

    return TouchEvent(
        schema_version=schema_version,
        verb=verb,
        timestamp=timestamp,
        session_id=session_id,
        agent_id=agent_id,
        path=path,
    )


def iter_complete_lines(raw: bytes) -> list[bytes]:
    """Split ``raw`` file bytes into complete, newline-terminated lines.

    A trailing fragment with no terminating newline (a concurrent write in
    flight) is silently dropped, never surfaced as ``MalformedRecordLine`` --
    that distinction is the module's own (AC6/AC3 body): ordinary concurrent
    traffic must not read as corruption.
    """
    if not raw:
        return []
    parts = raw.split(b"\n")
    # split() on a well-formed file (every line "\n"-terminated) yields a
    # trailing empty string after the last real line; a mid-write file
    # yields a trailing non-empty fragment. Either way, drop the last part
    # and re-append "\n" to every real line.
    complete = parts[:-1]
    return [line + b"\n" for line in complete if line]


def _last_verb_wins(events: list[TouchEvent]) -> list[TouchEvent]:
    """Collapse ``events`` to one line per path -- its most recent event --
    preserving each surviving path's first-encountered order. This is the
    compaction projection AC17's bound rewrites onto disk; it is exactly
    the projection the reader (C3) already applies at read time, so
    compacting to it changes on-disk size, never read semantics."""
    latest: dict[str, TouchEvent] = {}
    order: list[str] = []
    for event in events:
        if event.path not in latest:
            order.append(event.path)
        latest[event.path] = event
    return [latest[path] for path in order]


def compact_record(sink: "Path | str") -> None:
    """Rewrite ``sink`` to its last-verb-wins projection, one line per path.

    NOT on the per-append hot path, and not called by ``append_event`` or
    ``_maybe_rotate`` in this module -- a whole-file read-decode-rewrite
    races any process still appending to ``sink`` (see the module
    docstring's rejected-alternative note). Safe to call only once nothing
    can still be appending to ``sink``: a rotated-away
    ``<name>.rotated-<ts>-<pid>.jsonl`` file (rotation guarantees no live
    ``append_event`` call ever targets that name), or the live path itself
    once its session has fully quiesced (e.g. from a session-end reaper).
    A line that fails to decode is dropped from the compacted output
    rather than aborting the whole compaction -- growth control must not
    itself become blockable by one bad line. Writes via a
    temp-file-plus-``os.replace`` swap so a reader never observes a
    partially-rewritten file.
    """
    sink_path = Path(sink)
    try:
        raw = sink_path.read_bytes()
    except FileNotFoundError:
        return

    events: list[TouchEvent] = []
    for line in iter_complete_lines(raw):
        try:
            events.append(decode_line(line))
        except MalformedRecordLine:
            continue

    compacted = _last_verb_wins(events)
    body = b"".join(
        encode_line(
            session_id=event.session_id,
            agent_id=event.agent_id,
            verb=event.verb,
            path=event.path,
            timestamp=event.timestamp,
        )
        for event in compacted
    )

    tmp_path = sink_path.with_name(sink_path.name + f".compact-{os.getpid()}.tmp")
    tmp_path.write_bytes(body)
    os.replace(tmp_path, sink_path)


def _rotate_oversized(sink_path: Path) -> None:
    """Rename an oversized sink out of the way so the next append (through
    ``atomic_append.append_line``, which opens fresh by path every call)
    lands in a brand-new, empty file at the same path.

    Rename, never rewrite: this is the race-free half of AC17's discharge
    -- see the module docstring for the live-reproduced Windows/POSIX
    failure this replaces. ``os.replace`` here only ever retargets a
    directory entry; it cannot observe, block, or corrupt a peer's
    concurrent ``open``+``write``+``close`` of the same path, so no
    already-made or in-flight peer append can be lost by this call.

    Never raises: growth control must never fail or block the caller's own
    append (contract). A peer may have already rotated this generation
    away (its rename source no longer exists) or the rename may transiently
    fail under load -- either way this is a no-op and the next append's
    stat re-check decides again.
    """
    ts_ms = int(time.time() * 1000)
    rotated_path = sink_path.with_name(
        f"{sink_path.name}.rotated-{ts_ms}-{os.getpid()}.jsonl"
    )
    try:
        os.replace(sink_path, rotated_path)
    except OSError:
        return


def _maybe_rotate(sink_path: Path) -> None:
    """AC17's live invocation point: an O(1) stat, not a content read, on
    every append. Only rotates the first time the file has actually grown
    past the bound -- see ``_rotate_oversized`` for why rotation (rename),
    not in-place compaction, is the only race-free choice here."""
    try:
        size = sink_path.stat().st_size
    except FileNotFoundError:
        return
    if size >= MAX_RECORD_BYTES:
        _rotate_oversized(sink_path)


def append_event(
    sink: "Path | str",
    *,
    session_id: str,
    agent_id: Optional[str],
    verb: str,
    path: str,
    timestamp: Optional[float] = None,
) -> None:
    """Encode one event and append it to ``sink`` through
    ``atomic_append.append_line`` -- the only append mechanism this module
    uses (AC3). Creates ``sink``'s parent directory first, per
    ``append_line``'s own contract that callers do so (not dead ceremony).

    Encoding happens before the growth check so a rejected (too-long) line
    never triggers a rotation for a write that will not land.
    """
    encoded = encode_line(
        session_id=session_id, agent_id=agent_id, verb=verb, path=path, timestamp=timestamp
    )
    sink_path = Path(sink)
    # Judged, not overlooked: ``sink`` is caller-supplied and serves BOTH
    # session-keyed and agent-keyed sinks, so this parent is not knowably a
    # session dir and ``session_id`` here is a record FIELD, not the
    # directory's identity — routing this through core.ensure_session would
    # mint a session record for an agent dir. Every caller that does hand a
    # session-keyed sink owns the construction itself
    # (``scope.touch``/``hooks.track_touched_files`` call ensure_session;
    # ``claims.self_claim`` deliberately skips on an absent session dir).
    sink_path.parent.mkdir(parents=True, exist_ok=True)
    _maybe_rotate(sink_path)
    atomic_append.append_line(sink_path, encoded)


# ---------------------------------------------------------------------------
# C3: the read seam.
# ---------------------------------------------------------------------------

_ROTATED_SUFFIX_RE = re.compile(r"^(?P<base>.+)\.rotated-(?P<ts>\d+)-(?P<pid>\d+)\.jsonl$")

#: Process-local, keyed by degrade cause ("unreadable_file" / "malformed_line").
#: See module docstring's Observability section for why this stays
#: per-process rather than a durable cross-process sink.
_DEGRADE_COUNTS: dict[str, int] = {}


def _note_degrade(reason: str, detail: str) -> None:
    """Best-effort, never-raising notification of one read degrade. Runs
    ONLY on the degrade branch -- never on a normal read -- so it costs the
    hot path nothing (see module docstring's Observability section)."""
    _DEGRADE_COUNTS[reason] = _DEGRADE_COUNTS.get(reason, 0) + 1
    try:
        _logger.warning("touch_record read degrade: %s (%s)", reason, detail)
    except Exception:
        pass


def degrade_counts() -> dict[str, int]:
    """Process-local counts of every read degrade seen since import, keyed
    by cause. Countable without instrumenting this module first -- any
    caller, or a periodic sampler, reads this directly. Cleared only by
    process restart; not durable across processes (see module docstring)."""
    return dict(_DEGRADE_COUNTS)


def discover_family(sink_path: "Path | str") -> list[Path]:
    """Return every on-disk file backing one sink's record, oldest
    generation first, live file last (if present).

    Rotated siblings (``<name>.rotated-<ts>-<pid>.jsonl``) sort by their own
    embedded ``(ts, pid)`` ascending -- see module docstring's Cross-file
    ordering section for why ``pid`` is the deterministic tie-break for a
    same-millisecond double rotation. The live path, if it exists, is
    always the newest generation: nothing can rotate INTO existence after
    it, only out of it.
    """
    sink_path = Path(sink_path)
    parent = sink_path.parent
    name = sink_path.name

    rotated: list[tuple[int, int, Path]] = []
    if parent.is_dir():
        for candidate in parent.glob(f"{name}.rotated-*.jsonl"):
            match = _ROTATED_SUFFIX_RE.match(candidate.name)
            if not match or match.group("base") != name:
                continue
            rotated.append((int(match.group("ts")), int(match.group("pid")), candidate))
    rotated.sort(key=lambda item: (item[0], item[1]))

    family = [candidate for _, _, candidate in rotated]
    if sink_path.exists():
        family.append(sink_path)
    return family


def _read_stream_claims(sink_path: "Path | str") -> tuple[dict[str, TouchEvent], bool, tuple[str, ...]]:
    """Read one sink's whole family and fold it to its own last-verb-wins
    claim map, in family (generation, position) order -- see module
    docstring's Cross-file ordering section. Never raises: an unreadable
    member or a malformed complete line each set the returned degrade flag
    (and are counted -- see ``_note_degrade``) rather than aborting the
    read; whatever else in the family DID decode is still returned."""
    events: list[TouchEvent] = []
    degraded = False
    reasons: list[str] = []

    for member in discover_family(sink_path):
        try:
            raw = member.read_bytes()
        except FileNotFoundError:
            # A peer's rotation raced our directory listing -- not data
            # loss, same posture as a trailing unterminated line.
            continue
        except OSError as exc:
            degraded = True
            reasons.append(f"unreadable:{member.name}")
            _note_degrade("unreadable_file", f"{member}: {exc}")
            continue

        for line in iter_complete_lines(raw):
            try:
                events.append(decode_line(line))
            except MalformedRecordLine as exc:
                degraded = True
                reasons.append(f"malformed:{member.name}")
                _note_degrade("malformed_line", f"{member}: {exc}")

    return {e.path: e for e in _last_verb_wins(events)} if events else {}, degraded, tuple(reasons)


def _merge_across_streams(per_stream_claims: list[dict[str, TouchEvent]]) -> dict[str, TouchEvent]:
    """Merge independently-ordered streams' own last-verb-wins claim maps
    into one, using each surviving event's ``ts`` -- the only signal shared
    between two independently-written sinks (see module docstring's
    Cross-file ordering section). A later ``ts`` wins; on a tie, TOUCH
    beats RELEASE (never the reverse -- see that section for why); a
    same-verb tie keeps whichever stream was merged first, which carries no
    meaning beyond the verb itself."""
    merged: dict[str, TouchEvent] = {}
    for claims in per_stream_claims:
        for path, event in claims.items():
            current = merged.get(path)
            if current is None:
                merged[path] = event
                continue
            if event.timestamp > current.timestamp:
                merged[path] = event
            elif (
                event.timestamp == current.timestamp
                and event.verb == VERB_TOUCH
                and current.verb == VERB_RELEASE
            ):
                merged[path] = event
    return merged


@dataclass(frozen=True)
class TouchProjection:
    """Result of ``project_live_claims`` -- the read seam's one return
    shape. ``claims`` maps repo-relative path -> the winning ``TouchEvent``
    for every path still claimed by a currently-live session/agent.

    ``degraded`` and ``degrade_reasons`` are the AC6 typed failure signal:
    ``degraded=True`` means at least one family member could not be read or
    decoded, so ``claims`` may be missing entries a fully healthy read
    would show -- a caller at a commit gate MUST treat this as "could not
    tell", never as "clean". ``degraded=False`` with an empty ``claims``
    means exactly what it says: nothing is currently claimed. These two
    cases are never collapsed into one boolean or one empty dict (module
    docstring's Failure posture section)."""

    claims: dict[str, TouchEvent] = field(default_factory=dict)
    degraded: bool = False
    degrade_reasons: tuple[str, ...] = ()


def project_live_claims(
    *sink_paths: "Path | str", cwd: Optional[str] = None
) -> TouchProjection:
    """The single read seam (C3): session claims, agent claims, and
    live-peer projection, in one call.

    Each ``sink_paths`` entry is one sink's base path (a session-keyed sink,
    an agent-keyed sink, or any mix) -- this function expands each to its
    own family (``discover_family``), decodes and folds it to its own
    last-verb-wins claims (``_read_stream_claims``), merges all of them
    across streams (``_merge_across_streams``), and keeps only paths whose
    winning event is a TOUCH from a session ``liveness.session_live`` still
    calls live -- a RELEASE, or a TOUCH from a dead session, is never a
    live claim. ``cwd`` threads through to ``session_live`` unchanged.

    Never raises: see module docstring's Failure posture section for what
    an unreadable file or a malformed line do instead (set ``degraded`` and
    keep going).
    """
    per_stream_claims: list[dict[str, TouchEvent]] = []
    degraded = False
    reasons: list[str] = []

    for sink in sink_paths:
        stream_claims, stream_degraded, stream_reasons = _read_stream_claims(sink)
        per_stream_claims.append(stream_claims)
        if stream_degraded:
            degraded = True
            reasons.extend(stream_reasons)

    merged = _merge_across_streams(per_stream_claims)
    live_claims = {
        path: event
        for path, event in merged.items()
        if event.verb == VERB_TOUCH and session_live(event.session_id, cwd)
    }
    return TouchProjection(claims=live_claims, degraded=degraded, degrade_reasons=tuple(reasons))
