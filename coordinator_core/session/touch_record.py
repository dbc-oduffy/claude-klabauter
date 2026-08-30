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
    first by their own ``<name>.rotated-<ts>-<pid>-<seq>.jsonl`` suffix
    (ascending ``(ts, pid, seq)``). ``pid`` tie-breaks two PROCESSES whose
    rotations of the same sink land in the same millisecond. ``seq`` -- a
    process-local counter, see ``_ROTATE_SEQ`` -- is not a tie-break at all
    but a uniqueness component: ONE process crossing the bound twice inside
    a millisecond used to build the identical filename twice, and
    ``os.replace`` onto an existing path overwrites, so the first rotated
    generation was destroyed with its events in it (reproduced 2026-08-29:
    5 of 160 concurrent appends lost, every writer exiting 0). ``seq`` is
    absent on generations rotated before that counter existed and reads as
    0. The live file (if present) is always the newest
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

import hashlib
import json
import logging
import itertools
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core import atomic_append
from coordinator_core.session.liveness import session_live
from coordinator_core.session.path_dialect import canonicalize_relative_path

_logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: AC7: the record's filename, owned HERE and nowhere else, plus the one
#: constructor every caller uses to turn a claimant directory into its sink
#: path. Before this existed, four sites outside this module each spelled the
#: literal themselves -- ``bash_guards/check_test_suite_invocation``,
#: ``bash_guards/dispatch_checks``, ``session/stable_pid_watch`` and
#: ``claim_index``'s own private ``_TOUCHED_FILENAME`` -- which is how a
#: filename change becomes a silent partial repoint: a missed site does not
#: fail, it reads an absent file and reports "no claims", the fail-open shape
#: this whole workstream exists to close.
RECORD_FILENAME = "touch-record.jsonl"

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
    file's directory (AC1).

    ``content_hash`` (C10, plan ``2026-08-27-a-pathspec-is-not-a-scope``) is
    the whole-file ``sha256`` hex digest this session's own write produced,
    recorded alongside a TOUCH so a later commit-time check can compare it
    against disk-now to detect a foreign edit landing inside an owned file
    -- see ``docs/research/2026-08-27-hunk-level-ownership-spike.md`` for why
    a content hash (not ``size+mtime``, rejected there as empirically
    false-negative on same-tick interleaved writes) is the mechanism, and
    why it is spawn-free and well under the brightline. ``None`` for any
    event that never had a hash computed for it (a RELEASE, a pre-C10
    historical line, or a hash the caller could not compute) -- a missing
    hash is never itself a degrade signal at the record layer; a commit-time
    consumer (C11) that needs one and finds ``None`` is the one that must
    treat that as "could not confirm ownership", per this module's existing
    degrade posture (see module docstring's Failure posture section) -- this
    module only carries the field, it does not decide how an absent hash is
    used."""

    schema_version: int
    verb: str
    timestamp: float
    session_id: str
    agent_id: Optional[str]
    path: str
    content_hash: Optional[str] = None


def record_carries_content(record_path: "Path | str") -> bool:
    """True only if ``record_path`` exists AND holds at least one non-blank
    line -- the SSOT for "this dir has a real touch record", as distinct from
    "a file by that name is present".

    Lives here, with the record format, because two consumers need the same
    answer and both previously asked ``Path.exists()`` instead:
    ``ops.session.legacy_touch_corpus_drain_check`` (the gate on removing the
    legacy union-read) and ``ops.session.legacy_touch_corpus_migrate`` (the
    drain itself). ``exists()`` is wrong for both in the SAME direction: the
    migration creates its sink before writing into it, so a run that creates
    the file and writes nothing leaves a zero-byte record that reads as
    "drained" to the gate AND as "already_drained" to the migration -- the
    drain cannot repair what it half-did, and the gate reports green over it.
    Measured 2026-08-27 on claude-klabauter: eight sessions, 167 legacy claims
    stranded against empty siblings, gate reporting zero undrained.

    Unreadable (OSError, undecodable bytes) returns False -- both callers
    must fail toward "work still to do", never toward a green gate, since the
    loss they guard is unrecoverable claim destruction.

    Deliberately content-only: it does NOT validate schema or parse events.
    A record holding malformed lines is still a record; whether its lines
    decode is ``decode_line``'s question, and a stricter predicate here would
    silently reclassify a populated record as undrained and invite a second
    migration over it.
    """
    try:
        with open(record_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    return True
    except OSError:
        return False
    return False


def sink_path(claimant_dir: "Path | str") -> Path:
    """The record sink inside *claimant_dir* — a session dir or an agent dir.

    AC7's single record-path constructor. Takes the directory, returns the
    live sink; ``discover_family`` takes it from there when a caller needs
    the rotated generations too. Deliberately does NOT check existence: a
    claimant with no sink yet is a normal state, and conflating "no file"
    with "no path" is what a caller must stay able to distinguish.
    """
    return Path(claimant_dir) / RECORD_FILENAME


def compute_content_hash(file_path: "Path | str") -> Optional[str]:
    """The C10 fingerprint: a whole-file ``sha256`` hex digest, read and
    hashed in-process -- ~0.148ms/call measured against a 267KB file
    (``docs/research/2026-08-27-hunk-level-ownership-spike.md``), zero
    subprocess spawns. NOT ``size+mtime``: that candidate was tested and
    rejected in the same spike -- two same-size writes issued back to back
    land on the same ``st_mtime_ns`` on this filesystem, reporting no change
    on exactly the interleaved-writers shape this workstream exists to
    close.

    Returns ``None`` (never raises) if ``file_path`` cannot be read --
    deleted between the caller's write and this call, a permission denial,
    a race. This mirrors this module's own existing degrade posture
    (module docstring's Failure posture section): an unreadable file is
    "could not confirm", not "no content" -- a caller MUST NOT treat a
    ``None`` return as license to skip recording a TOUCH or to treat the
    path as unowned; it is the caller's job (C11) to propagate this as a
    degrade signal, the same way ``TouchProjection.degraded`` already does
    for a read-side failure, never as silence.
    """
    try:
        data = Path(file_path).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def encode_line(
    *,
    session_id: str,
    agent_id: Optional[str],
    verb: str,
    path: str,
    timestamp: Optional[float] = None,
    content_hash: Optional[str] = None,
) -> bytes:
    """Encode one event as a self-describing, newline-terminated line.

    Path normalization happens here, once, via
    ``path_dialect.canonicalize_relative_path`` -- the decoder trusts the
    stored form and re-derives nothing (AC4).

    ``content_hash`` (C10) is carried straight through into the encoded
    record's ``"hash"`` field when given, and OMITTED (not written as
    ``null``) when ``None`` -- keeps a pre-C10 line and a hash-less RELEASE
    byte-identical to what this module already wrote, rather than growing
    every line's minimum size on a field most events do not carry.

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
    if content_hash is not None:
        record["hash"] = content_hash
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

    # C10: optional, absent on any pre-C10 line, a RELEASE, or a hash the
    # writer could not compute -- absence is never malformed here (see
    # ``TouchEvent.content_hash``'s docstring for who decides what an
    # absent hash means downstream).
    content_hash = record.get("hash")
    if content_hash is not None and not isinstance(content_hash, str):
        raise MalformedRecordLine(f"invalid content hash {content_hash!r}")

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
        content_hash=content_hash,
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
            content_hash=event.content_hash,
        )
        for event in compacted
    )

    tmp_path = sink_path.with_name(sink_path.name + f".compact-{os.getpid()}.tmp")
    tmp_path.write_bytes(body)
    os.replace(tmp_path, sink_path)


#: Process-local rotation counter -- the third component of a rotated
#: filename, and the reason two rotations by the SAME process inside one
#: millisecond cannot collide.
#:
#: `ts_ms` + `pid` was not unique. The module's cross-file ordering note
#: already reasoned about a same-millisecond double rotation and picked `pid`
#: as the tie-break, which settles it for two DIFFERENT processes -- but one
#: process crossing the bound twice inside a millisecond produced the IDENTICAL
#: name both times, and `os.replace` onto an existing path overwrites it. The
#: first rotated generation was destroyed, silently, with its events in it.
#:
#: Reproduced 2026-08-29 by `test_concurrent_append_across_growth_control_
#: loses_no_line`, which lost 5 of 160 appends on ~1 run in 9 (every child
#: exiting 0). It is rare in production only because `MAX_RECORD_BYTES` is
#: 256KiB; it is not impossible there, and the lost bytes are touch claims,
#: so the visible symptom is a file quietly dropping out of the safe-commit
#: offer -- the exact defect class this record substrate exists to prevent.
#:
#: A counter rather than random bytes: `discover_family` sorts generations by
#: their embedded key, and a monotonic counter keeps a single process's own
#: rotations in true chronological order within a millisecond, where random
#: tokens would order them arbitrarily.
_ROTATE_SEQ = itertools.count()


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
        f"{sink_path.name}.rotated-{ts_ms}-{os.getpid()}-{next(_ROTATE_SEQ)}.jsonl"
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
    content_hash: Optional[str] = None,
) -> None:
    """Encode one event and append it to ``sink`` through
    ``atomic_append.append_line`` -- the only append mechanism this module
    uses (AC3). Creates ``sink``'s parent directory first, per
    ``append_line``'s own contract that callers do so (not dead ceremony).

    ``content_hash`` (C10) threads straight through to ``encode_line`` --
    this function does not compute it itself; a caller wanting the on-disk
    fingerprint for a TOUCH computes it via ``compute_content_hash`` (or its
    own equivalent) and passes the digest in. Recording only -- this chunk
    does not wire a caller (C11's job); ``touch()`` continuing to call this
    without ``content_hash`` is unchanged behavior, not a regression.

    Encoding happens before the growth check so a rejected (too-long) line
    never triggers a rotation for a write that will not land.
    """
    encoded = encode_line(
        session_id=session_id,
        agent_id=agent_id,
        verb=verb,
        path=path,
        timestamp=timestamp,
        content_hash=content_hash,
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

#: `seq` is OPTIONAL: files rotated before the counter existed are on disk
#: right now under the two-component name and must keep being discovered. A
#: missing group reads as 0, which orders a legacy generation before any
#: same-(ts, pid) generation written after -- the correct order, since the
#: legacy one was written first.
_ROTATED_SUFFIX_RE = re.compile(
    r"^(?P<base>.+)\.rotated-(?P<ts>\d+)-(?P<pid>\d+)(?:-(?P<seq>\d+))?\.jsonl$"
)

#: Process-local, keyed by degrade cause ("unreadable_file" / "malformed_line").
#: See module docstring's Observability section for why this stays
#: per-process rather than a durable cross-process sink.
_DEGRADE_COUNTS: dict[str, int] = {}


def append_touch_claims(
    paths: "Iterable[str]",
    session_id: str,
    root: "Path | str",
) -> None:
    """Append one ``VERB_TOUCH`` per repo-relative entry in ``paths`` to
    ``session_id``'s own sink under ``root``. Returns ``None`` always, raises
    never.

    Exists because two callers had hand-copied the identical tail -- build
    ``<root>/.git/coordinator-sessions/<sid>``, ``sink_path`` it, then loop
    ``append_event`` swallowing per-path failures: ``bash_guards.
    dispatch_checks._rm_flush_touch`` (deletions, C9 2026-08-27) and
    ``bash_guards.write_claim_record.record_write_claims`` (writes,
    2026-08-30). The copies had already begun to drift in their exception
    handling, which is the drift this collapses.

    FAILS TOWARD RECORDING NOTHING, NEVER TOWARD RAISING. Both callers sit on
    the PreToolUse guard hot path, where a recording failure must never turn
    an otherwise-ALLOWED command into a denied one. ``session_id`` and
    ``root`` are the CALLER's already-resolved values -- this never re-derives
    either, and adds no spawn and no directory walk.

    Entries are repo-relative POSIX paths; relpath conversion belongs to the
    caller, which is the half the two sites legitimately differ on.
    """
    if not session_id or not root:
        return
    try:
        sid_dir = os.path.join(str(root), ".git", "coordinator-sessions", session_id)
        sink = sink_path(sid_dir)
        for rel in paths:
            if not rel:
                continue
            try:
                append_event(
                    sink,
                    session_id=session_id,
                    agent_id=None,
                    verb=VERB_TOUCH,
                    path=rel,
                )
            except Exception:
                continue
    except Exception:
        return


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

    Rotated siblings (``<name>.rotated-<ts>-<pid>-<seq>.jsonl``) sort by their
    own embedded ``(ts, pid, seq)`` ascending -- see module docstring's
    Cross-file ordering section for why ``pid`` is the deterministic tie-break
    between two PROCESSES rotating in the same millisecond, and
    ``_ROTATE_SEQ`` for why one process rotating twice in that millisecond
    needs a third component rather than a tie-break. ``seq`` is absent on
    generations rotated before that counter existed and reads as 0. The live path, if it exists, is
    always the newest generation: nothing can rotate INTO existence after
    it, only out of it.
    """
    sink_path = Path(sink_path)
    parent = sink_path.parent
    name = sink_path.name

    # ONE `os.scandir` of the parent, not `is_dir()` + `glob()` + `exists()`.
    # This is a corpus-walk hot path -- `scope.compute_scope`, `claim_index`
    # and `stable_pid_watch` each call it once PER CLAIMANT, and the engine
    # serves those per request rather than per process, so the cost recurs at
    # dispatch rate rather than at startup. Measured on the live hub
    # (480 claimants, 2026-08-26): 18.6ms -> 10.2ms, 45% off, byte-identical
    # output on every claimant. The glob was the bulk of it and was searching
    # for something that does not exist -- zero rotated generations were
    # present anywhere in the hub at the time of measuring, because rotation
    # only fires past `MAX_RECORD_BYTES`, so the overwhelmingly common case is
    # a directory scan that matches nothing. `scandir` answers "what is
    # actually here" once and lets the prefix test reject non-candidates
    # before the regex runs.
    #
    # Equivalence is exact, not approximate, and the three seams that could
    # have made it approximate:
    #   - a missing / non-directory parent raises here where `is_dir()`
    #     returned False, so those two errors are caught and mean the same
    #     "no family" they meant before; every OTHER `OSError` still
    #     propagates exactly as `glob` let it;
    #   - `entry.is_file() or entry.is_dir()` reproduces `exists()` rather
    #     than narrowing it -- `exists()` is true for a DIRECTORY carrying
    #     the sink's name and false for a broken symlink, and so is this;
    #   - the live entry appended is `sink_path` itself, never
    #     `entry.path`, so the caller's own separator dialect survives
    #     (a raw `a/b` argument must not come back normalised to `a\\b`).
    rotated: list[tuple[int, int, int, Path]] = []
    live_exists = False
    rotated_prefix = f"{name}.rotated-"
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                entry_name = entry.name
                if entry_name == name:
                    live_exists = entry.is_file() or entry.is_dir()
                    continue
                if not entry_name.startswith(rotated_prefix):
                    continue
                match = _ROTATED_SUFFIX_RE.match(entry_name)
                if not match or match.group("base") != name:
                    continue
                rotated.append(
                    (
                        int(match.group("ts")),
                        int(match.group("pid")),
                        int(match.group("seq") or 0),
                        Path(entry.path),
                    )
                )
    except (FileNotFoundError, NotADirectoryError):
        return []
    rotated.sort(key=lambda item: (item[0], item[1], item[2]))

    family = [candidate for _, _, _, candidate in rotated]
    if live_exists:
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


# ---------------------------------------------------------------------------
# C2: commit-time reconciliation for a write that never passed through the
# Write/Edit hook (a Bash heredoc, `sed -i`, `python3 -c`, etc.) and so was
# never recorded as a TOUCH by this module's own `append_event`.
#
# Design constraint (plan docstring, carried in verbatim): this must not cost
# a spawn or a filesystem walk PER BASH INVOCATION -- Bash fires constantly,
# so intercepting at write time was rejected. Instead this reconciles at
# COMMIT time, over the small residue Check 5 (``dispatch_checks.
# check_validate_commit``) already computes as "staged but not in this
# session's claim projection" -- that residue is already the product of a
# `git diff --cached` Check 5 runs anyway, so this module adds no git spawn
# and no directory walk of its own: one `os.stat` per already-narrowed
# candidate path, nothing more.
#
# Attribution rule: a staged, unclaimed path is attributed to THIS session
# if its mtime falls at or after this session's own `started_at` -- the
# session-lifetime lower bound `session.core.init` stamps once, on first
# start, into `<session_dir>/started_at`. A path last modified before this
# session existed cannot be this session's write under any mechanism, so the
# window is a sound (never a probabilistic) filter, not a heuristic score.
# It is deliberately NOT an upper bound beyond "now": a peer could still
# write the same path after reconciliation runs, but that later write earns
# its OWN touch record at ITS OWN commit-time reconciliation (or its own
# Write/Edit hook), and `_merge_across_streams`' later-``ts``-wins rule
# already resolves that case the same way it resolves any other two-writer
# race on this record.
#
# Negative-spec: this does NOT attempt hunk-level attribution (two sessions
# both touching the same file) -- that is the C5 spike's unbuilt, unproven
# territory (docs/plans/2026-08-27-a-pathspec-is-not-a-scope.md § C5), and a
# path already carrying a live TOUCH claim from ANY session (including a
# peer) is never a candidate a caller should pass here in the first place --
# this function trusts its caller's `candidate_paths` are already the
# staged-but-unclaimed residue, and does not re-derive that residue itself
# (no second read of `project_live_claims` inside this function -- the
# caller already has that answer, and re-deriving it here would be exactly
# the second dialect of the same read this module's own docstring already
# forbids for the append path).
# ---------------------------------------------------------------------------


def session_started_at_epoch(session_dir: "Path | str") -> Optional[float]:
    """Read ``<session_dir>/started_at`` (the file ``session.core.init``
    stamps once, on first start, with ``now_iso()``) and return it as epoch
    seconds -- the lower bound of "this session's own mtime window".

    Returns ``None`` if the file is absent or unreadable, or if its content
    fails to parse as an ISO timestamp (``core.iso_to_epoch`` itself returns
    ``0`` for a parse failure, which is indistinguishable from "midnight
    1970" and therefore NOT a safe window bound here -- ``0`` is folded to
    ``None`` rather than returned, so a caller cannot mistake "unparseable"
    for "the epoch"). A caller MUST treat ``None`` as "no window available",
    never as "any mtime qualifies" -- see ``reconcile_untouched_bash_writes``,
    which returns an empty attribution list rather than guessing.

    Lazy, per-call import of ``session.core`` (mirrors
    ``check_validate_commit``'s own lazy import of ``session.scope`` for the
    same reason: this is a commit-time-only read, never the hot Write/Edit
    or Bash-guard path this module's append side serves).
    """
    from coordinator_core.session.core import iso_to_epoch

    started_path = Path(session_dir) / "started_at"
    try:
        text = started_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    epoch = iso_to_epoch(text)
    return float(epoch) if epoch else None


def reconcile_untouched_bash_writes(
    sink: "Path | str",
    *,
    session_id: str,
    agent_id: Optional[str],
    session_dir: "Path | str",
    candidate_paths: "list[str]",
    cwd: Optional[str] = None,
    timestamp: Optional[float] = None,
) -> list[str]:
    """Backfill a TOUCH record for every path in ``candidate_paths`` whose
    current on-disk mtime falls at or after this session's own
    ``started_at`` window (see ``session_started_at_epoch``) -- the
    commit-time closure for a write that reached disk through Bash rather
    than the Write/Edit hook.

    ``candidate_paths`` are repo-relative paths the CALLER has already
    narrowed to "staged, and not already a live claim for any session" (the
    residue Check 5 computes) -- this function does no git spawn and no
    directory listing of its own; it costs exactly one ``os.stat`` per
    candidate, resolved against ``cwd`` when given (matching the caller's
    own git invocation cwd) or the process cwd otherwise.

    Returns the list of paths actually attributed (a TOUCH event appended
    for each, via ``append_event`` -- the ONE append mechanism, per this
    module's own negative-spec). Fails toward attributing NOTHING, never
    toward guessing: an unreadable ``candidate_paths`` entry (stat raises)
    is skipped, not attributed, and an absent/unparseable session window
    (``session_started_at_epoch`` returns ``None``) short-circuits the whole
    call to an empty list before any stat runs.
    """
    started_at = session_started_at_epoch(session_dir)
    if started_at is None:
        return []

    base = Path(cwd) if cwd else None
    attributed: list[str] = []
    for candidate in candidate_paths:
        target = (base / candidate) if base else Path(candidate)
        try:
            mtime = target.stat().st_mtime
        except OSError:
            continue
        if mtime < started_at:
            continue
        # C12 (plan 2026-08-27-a-pathspec-is-not-a-scope): fingerprint the
        # path being attributed here, same as the C10 field this reconciler
        # was already stamping a TOUCH for -- without this, a Bash-written
        # file the session genuinely owns would carry a claim with no hash,
        # and C11's comparator treats "no hash" as "not demonstrable",
        # silently reopening the third granularity for every Bash write.
        # Computed from the SAME resolved `target` the mtime check above
        # already stat'd (no second path-join dialect); `None` (deleted
        # between the mtime check and this read) passes straight through to
        # `append_event`, same fail-open posture as `compute_content_hash`'s
        # own contract.
        content_hash = compute_content_hash(target)
        append_event(
            sink,
            session_id=session_id,
            agent_id=agent_id,
            verb=VERB_TOUCH,
            path=candidate,
            timestamp=timestamp,
            content_hash=content_hash,
        )
        attributed.append(candidate)
    return attributed


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
