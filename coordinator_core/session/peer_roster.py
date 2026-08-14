"""
coordinator_core.session.peer_roster — live cwd-filtered peer roster.

Purpose: the resolver (`coordinator_core.session.reachability`) answers "how
do I reach the session that holds THIS uuid." Nothing answered "who else is
working in this repo right now" — the question the PM actually asked next
(2026-08-13, spinning this off the resolver's own baton). This module is a
sibling READ over the same `harness_registry.snapshot()`: no second parser,
no duplicated ref/address derivation, no durable file.

Spec backlink: `state/handoffs/2026-08-13-live-peer-roster.md` §§ 1-3.

Negative-spec (Anti-scope, same handoff):
    - No durable roster file, cache, or published registry — `snapshot()`
      is read fresh, once, per `build_roster()` call, and nothing here
      persists a result across calls.
    - Never imports or calls `coordinator_core.session.liveness.
      session_live()` — a roster row is a live-registry CANDIDATE, not a
      reachability guarantee, and this module never claims otherwise.
    - Never reads `~/.claude/teams/`.
    - Never re-derives the `name`/`ref` rules — every row's `address`/`name`/
      `ref` comes from `reachability.resolve_candidates()`, unchanged.
    - No fleet orchestration: this module only reads and filters a snapshot;
      it schedules, assigns, and supervises nothing.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import List, Optional

from coordinator_core.session import harness_registry
from coordinator_core.session import reachability


@dataclass(frozen=True)
class PeerRow:
    """One live session, filtered into the roster and resolved to its own
    `SendMessage` address.

    `address`/`name`/`ref` come from `reachability.resolve_candidates()` —
    `None` for a session whose record lacks a usable `name`/
    `messaging_socket_path` (same degrade-to-`None` contract as
    `reachability.Candidate`, never a guessed or bare-UUID address).
    `status` is display-only (`harness_registry.RegistryRecord.status`'s own
    negative-spec) — never treated as a liveness signal here or by any
    caller. `running_seconds` is a plain number (`time.time() -
    start_epoch`), not formatted prose. `is_self` is `True` exactly for the
    row identified as the caller's own session -- see `build_roster`'s
    docstring for why that row is never silently dropped or silently
    unmarked, and for the two-signal resolution behind it.

    `self_determination` is `"resolved"` when the calling session's own
    identity WAS determined this call (whether or not that session's row
    survives into THIS roster's cwd filter -- a resolved-but-filtered-out
    self is a legitimate outcome, not a defect), or `"unresolved"` when
    neither resolution signal could name it. Carried on every row (not a
    side-channel) so a consumer holding just the rows can still tell "you
    are not in this list because your session is outside this repo" apart
    from "we could not work out which row is you" without a second call --
    see `build_roster`'s docstring.
    """

    session_id: str
    address: Optional[str]
    name: Optional[str]
    ref: Optional[str]
    cwd: Optional[str]
    status: Optional[str]
    running_seconds: float
    is_self: bool
    self_determination: str


def _normalize_path(path: str) -> str:
    """Resolve symlinks, absolutize, and normalize-case a path for
    containment comparison.

    `os.path.realpath` resolves symlinks on BOTH sides before comparison —
    a harness-reported `cwd` and a caller-supplied `repo_root` may be
    normalized differently upstream (e.g. macOS `/tmp` is itself a symlink
    to `/private/tmp`), and comparing an unresolved path against a resolved
    one silently drops a live peer from the roster (Review: code-reviewer —
    P2). `realpath` already implies `abspath`; `normpath` is kept for
    belt-and-braces on any residual `..`/`.` segments it leaves. Finally,
    `os.path.normcase` is a no-op on POSIX and lowercases on Windows — the
    one cross-platform-correct way to compare two `cwd`-shaped strings for
    containment without assuming either side's platform (Windows is
    first-class here, per CLAUDE.md).
    """
    return os.path.normcase(os.path.normpath(os.path.realpath(path)))


def _cwd_within_repo(cwd: Optional[str], repo_root: str) -> bool:
    """True if `cwd` IS `repo_root`, or a subdirectory of it.

    A session's harness-reported `cwd` may be a subdirectory of the repo
    root (a session that `/cd`'d into a subdir), so this is path
    containment, not string equality — both sides normalized first (§ 6 of
    the spec handoff).
    """
    if not cwd:
        return False
    norm_cwd = _normalize_path(cwd)
    norm_root = _normalize_path(repo_root)
    return norm_cwd == norm_root or norm_cwd.startswith(norm_root + os.sep)


def build_roster(
    repo_root: Optional[str] = None, *, raise_on_failure: bool = False
) -> List[PeerRow]:
    """Return every live session whose `cwd` is within `repo_root`.

    `repo_root` defaults to `os.getcwd()` (the CALLING process's own
    directory) when omitted — never an unfiltered dump of the whole
    snapshot (spec § 2: "Do not collapse these into an unfiltered dump").
    Pass a sibling repo's absolute path for the "who is in repo X" view.

    Reads `harness_registry.snapshot()` exactly once. Ref-widening and
    name-collision detection run over the WHOLE snapshot via
    `reachability.resolve_candidates()` BEFORE this function's cwd filter
    is applied — filtering the resolved rows afterward is the only order
    that keeps every row's ref/address identical to what an unfiltered
    caller would see for that same session (spec § "Critical", same
    invariant `resolve_candidates()`'s own docstring pins).

    Self-handling: the harness excludes the calling session from its own
    `ListAgents` view, but `snapshot()` includes it (verified live,
    2026-08-13) -- so the calling session's own row, when its `cwd` also
    matches the filter, is marked `is_self=True` explicitly, never silently
    dropped and never silently left indistinguishable from a peer.

    Self resolution uses TWO independent signals, mirroring
    `reachability.resolve_address`'s own `own_session` classification
    (same rationale, same fallback):

      1. `harness_registry.self_record()` -- the primary, pid-keyed signal.
      2. `reachability._socket_env_self_match` -- `CLAUDE_CODE_MESSAGING_SOCKET`
         compared, as an opaque string, against a snapshot record's own
         `messaging_socket_path`. A second, independent signal because
         signal 1 can decline for a session whose `CLAUDE_PID` IS correctly
         set: measured live 2026-08-13 from inside this very session,
         `_resolve_claude_pid_from_env()` returned `env-miss:name-mismatch`
         -- the expected-name check (`comm == "claude"`) compares against
         this build's ACTUAL process name, which on this box (Claude Code
         2.1.231) is the literal version string `"2.1.231"` (confirmed via
         both `psutil.Process(pid).name()` and `ps -o comm=`; the on-disk
         binary itself is `~/.local/share/claude/versions/2.1.231`, not a
         `claude`-named executable) -- see
         `state/subagent-share/cfc55b39-d5fb-4f2f-b347-3df9c34a83e9/self-record-report.md`
         for the full finding, corroborated independently by a sibling
         `posix-parent-miss:name-mismatch` capture
         (`state/bug-backlog/2026-08-13-stable-pid-capture-can-miss-leaving-a-li-3800b09f19b5.yaml`).
         This module does NOT reimplement that comparison -- it imports the
         one, already-reviewed helper `reachability` already exports for
         exactly this purpose, rather than growing a second divergent
         parser of the same env var.

    Neither signal firing (both `self_record()` declining AND no snapshot
    record's `messaging_socket_path` matching `CLAUDE_CODE_MESSAGING_SOCKET`)
    is a real, expected outcome -- e.g. a headless/non-interactive caller
    with neither env var set -- and is never collapsed into "no self in this
    repo": every returned row's `self_determination` is `"unresolved"` in
    that case (`"resolved"` otherwise), so a consumer can always tell the
    two apart. See `PeerRow.self_determination`'s own docstring.

    Degrades to an empty list on any internal failure -- an
    absent/unreadable registry, a `self_record()` failure -- rather than
    raising; same advisory-read discipline as `reachability.resolve_address`.
    Note: an empty-list degrade collapses `self_determination` along with
    everything else -- a caller needing to distinguish "no live sessions in
    this repo" from "self undeterminable" needs a non-empty roster to read
    the field from; this mirrors the existing degrade-to-`[]` contract and is
    not a new gap this change introduces.

    `raise_on_failure` (default `False`, additive/backward-compatible --
    every existing caller, e.g. `coordinator_core.ops.session_peer_roster`'s
    op veneer, is unaffected and keeps the original never-raise degrade)
    exists ONLY so a caller that itself already wraps this call in its own
    `except Exception` can tell "no live peers in this repo" (a genuine
    empty snapshot/filter result, `[]`) apart from "the registry itself was
    unreadable" (an internal exception) -- the two outcomes this function's
    own internal `except Exception: return []` otherwise collapses
    indistinguishably at the CLI boundary
    (`coordinator/bin/session-reachability-cli.py`'s exit-code table already
    maps a raised exception to `_TRANSPORT_FAIL`, but never saw one because
    this function swallowed it first). When `True`, the internal
    `harness_registry.snapshot()` failure and a `self_record()` failure are
    RE-RAISED to the caller instead of degraded to `[]`/`None`; every other
    empty-roster path (an empty snapshot, or a snapshot with no row inside
    `repo_root`) still returns `[]` exactly as before -- those are not
    failures.
    """
    try:
        snapshot = harness_registry.snapshot()
    except Exception:
        if raise_on_failure:
            raise
        return []

    if not snapshot:
        return []

    effective_root = repo_root if repo_root else os.getcwd()

    try:
        self_info = harness_registry.self_record()
    except Exception:
        if raise_on_failure:
            raise
        self_info = None
    self_sid = self_info[0] if self_info is not None else None

    if self_sid is None:
        for candidate_sid in snapshot:
            try:
                matched = reachability._socket_env_self_match(candidate_sid, snapshot)
            except Exception:
                matched = False
            if matched:
                self_sid = candidate_sid
                break

    self_determination = "resolved" if self_sid is not None else "unresolved"

    candidates_by_sid = {
        candidate.session_id: candidate
        for candidate in reachability.resolve_candidates(snapshot)
    }

    now = time.time()
    rows: List[PeerRow] = []
    for sid, record in snapshot.items():
        if not _cwd_within_repo(record.cwd, effective_root):
            continue

        candidate = candidates_by_sid.get(sid)
        address = candidate.address if candidate is not None else None
        # `record.name` fallback here is a no-op, not a looser degrade rule
        # than `address`/`ref`: `_resolve_one` only ever returns `None` for
        # `candidate` when `record.name` is itself falsy, so this branch
        # can't surface a name that `candidate.name` would have withheld
        # (Review: code-reviewer — P3).
        name = candidate.name if candidate is not None else record.name
        ref = candidate.ref if candidate is not None else None
        running_seconds = max(0.0, now - record.start_epoch)

        rows.append(
            PeerRow(
                session_id=sid,
                address=address,
                name=name,
                ref=ref,
                cwd=record.cwd,
                status=record.status,
                running_seconds=running_seconds,
                is_self=(sid == self_sid),
                self_determination=self_determination,
            )
        )
    return rows
