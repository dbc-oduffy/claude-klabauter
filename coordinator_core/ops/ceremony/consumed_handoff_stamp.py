"""
coordinator_core.ops.ceremony.consumed_handoff_stamp — Consumed-handoff
ship-stamp + R1-R4 ship-drift correctness (break-class core, C5).

Purpose: this is the correctness-critical seam of the wsc_tail rebuild. It
closes the ship-drift bug class the retired `2026-07-14-wsc-chain-terminal-
ship-drift-fix.md` plan set out to fix (same-session receipt-freeze TOCTOU +
cross-session shared-worktree races silently reverting `chain_terminal` /
under-stamping a real predecessor's `deployment_state`) by collapsing
resolve+commit into one in-process pass (R1) and re-deriving the consumed-
handoff set to be stamped via a fresh liveness re-check run again
immediately before the stamp write. (That re-check ran in-lock when this
module was written; since DEC-3's jettison it runs UNLOCKED — see the
DEC-3 note below and `redrive_consumed_set`.)

ONE call site, driven by the C9 orchestrator (not built by this chunk):
`post_commit_stamp_and_ship()` — runs AFTER the main ceremony commit lands
and AFTER `run_commit_pipeline`'s own internal `ceremony_lock` has already
released (wsc-tail-sub-2s-invoke-budget DEC-3, PM-jettisoned 2026-07-22 —
this function has never acquired the lock itself; its caller no longer holds
one either, by design; see Negative-spec). Re-derives the
consumed-handoff set fresh against current disk (R1/AC6 cross-session
liveness re-check — the closes-the-race step), applies R3's future-dated
rejection, applies the R4(a) live-children guard, and — for each eligible
candidate — stamps `shipped_in: <committed_sha>` (the REAL post-commit SHA;
Position A: no branch-tip fallback, no Session-Id sibling-correction walk)
THEN flips `deployment_state -> shipped`, both landing in ONE explicit
follow-up commit (AC17) so the op never exits with the stamp left as an
unswept dirty working-tree edit.

DEVIATION FROM THE PLAN'S LITERAL C5 BODY TEXT (documented, in-scope
correction — see Negative-spec "stamp-before-ship ordering" below): the plan
body describes R4 as "(b) handoff.transition._ship (deployment_state->shipped
only) [pre-commit]" then "(c) ... handoff.stamp ... post-commit". That literal
pre-commit-ship/post-commit-stamp split is schema-INFEASIBLE for any handoff
created on or after 2026-05-29: `coordinator_core/frontmatter/schema_validate.py
_cf_shipped_in_required` (Rule A3a-2) requires `shipped_in` to already be
present at the SAME MOMENT `deployment_state` becomes `shipped` for such
handoffs — so a bare pre-commit `_ship()` call (no `shipped_in` yet, since the
real `committed_sha` does not exist pre-commit) hard-fails schema validation
and never writes. This was verified directly against `handoff_transition._ship`
in this chunk's own test suite before the fix (see git history of this file).
The existing shipped op `coordinator_core/ops/handoff_ship_archive.py` already
demonstrates the schema-safe ordering for exactly this situation (its own
docstring: "Step 1: stamp shipped_in ... Step 2: ship transition") — this
module mirrors that precedent: stamp first, ship second, both post-commit,
both in the one follow-up commit. This is a corrected, schema-compliant
realization of Position A's actual goals (real-sha stamp, no branch-tip
fallback, no sibling-correction) — not a reversal of them. Flagged for
reviewer ratification; the plan's C5 body text and C9's orchestrator design
note should be updated to match (no separate pre-commit ship-only mutation
exists in this module).

R1 (AC6, single-pass + liveness re-check): `redrive_consumed_set()` is the
choke point that NARROWS (does not close — see `redrive_consumed_set`'s own
docstring) the cross-session shared-worktree race — a concurrent session
mutating/archiving a handoff between the ceremony's initial resolve and this
(no longer locked) stamp step. It is NOT a cache of the initial resolve; it is a
fresh call into `coordinator_core.ops.ceremony.resolver.find_all_consumed_
handoffs()` (C5's promoted public resolver contract) against current disk.

R2 (AC7, loud-report): `StampOutcome.empty_consumed_set` is True when
`chain_terminal` was True but the RE-DERIVED (post-commit, in-lock) consumed
set came back empty — a distinct EM-visible signal, never a non-zero exit or
`failed_critical`. A legitimate single-session close (chain_terminal False,
or chain_terminal True with a genuinely non-empty re-derived set) never sets
this flag.

R3 (AC8, plausibility guard): `reject_future_dated()` rejects a candidate
whose FILENAME date/datetime prefix postdates
`now() + _FILENAME_TZ_AMBIGUITY + _FUTURE_DATE_SKEW`. Deliberately compares
against wall-clock now(), NEVER against the session's `started_at` (a
session can legitimately run past midnight and pick up a same-day-later
handoff — started_at is not the plausibility bound). An unparseable filename
prefix is a graceful-skip (treated as accepted, not rejected) — this guard
exists to catch obviously-wrong future dates (a mis-generated or hand-typed
filename), NOT to police timezone offset or clock drift. Filename timestamp
prefixes are producer-dependent and this guard cannot know which producer
wrote a given filename: example-doctrine-repo's `/handoff` and `/spinoff` skills
(`coordinator/skills/handoff/SKILL.md`, ~line 146) stamp LOCAL wall-clock
time via `$(date +%Y-%m-%d)_$(date +%H%M%S)`, while this repo's own
`coordinator_core/ops/handoff_author_fork.py` `_fork_handoff_filename`
(~line 315) stamps UTC. A local-time filename written on any machine east
of UTC (up to UTC+14) reads as "future" relative to a naive-UTC `now()`
even though it is not actually future-dated — `_FILENAME_TZ_AMBIGUITY`
absorbs that legitimate producer spread on top of the small
`_FUTURE_DATE_SKEW` clock-skew allowance, so the guard still catches gross
future-dating (weeks/months) without punishing a correctly-local-timestamped
filename. See
`cross-repo/inbox/2026-07-23-claude-central-em-wsc-tail-stamp-ship-silent-skip.md`
for the confirmed real-world case this fixes.

R4 (AC9, Position A): `_live_children_guard()` branches on `exit_code`
authoritatively (0=has live children / 1=safe / 2=indeterminate,
`handoff.has_live_children`'s own contract) and RETAINS (does NOT stamp) on
BOTH exit_code 0 (real live children) AND exit_code 2 (indeterminate,
fail-closed) — never keys off the `referenced` field, which is deliberately
ABSENT on exit_code=2 (see `coordinator_core/ops/handoff_children.py`
`_indeterminate()` docstring) so a `referenced`-only gate would fail OPEN.
`_live_children_guard()` is called TWICE per candidate (2026-07-28, ceremony-
lock-hold-resurrection Row 7): once as a pre-lock filter in the main loop
below, once again inside the stamp write's own `locked_rmw` lock hold
(`_stamp_with_live_children_recheck`) immediately before the write lands —
closing the window where a successor could be authored between the two.
Row 6's companion fix (`_ship_with_cas`) makes the stamp/ship pair safe
against an interleaved peer write via a CAS on the exact text the stamp
write produced, without merging the two writes into one lock acquisition.

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C5.
Spec backlink (Row 6/Row 7 fix): 2026-07-28 handoff-write-cas spinoff,
`docs/research/2026-07-28-is-the-jettisoned-ceremony-lock-outer-ho.md` § (b).

Negative-spec (hard-won):
  - Stamp-before-ship ordering is LOAD-BEARING, not incidental: `handoff.stamp`
    (shipped_in) MUST be called before `handoff.transition` verb=ship for any
    handoff dated on/after 2026-05-29 — reversing the order reintroduces the
    schema-validation hard-fail this module's design deviation exists to
    avoid (see module docstring "DEVIATION" paragraph above).
  - Does NOT acquire `ceremony_lock` itself, and its caller no longer holds
    one either as of wsc-tail-sub-2s-invoke-budget DEC-3 (PM ruling
    2026-07-22, jettisoning `wsc_tail`'s outer lock hold): `post_commit_
    stamp_and_ship()` runs UNLOCKED by design, post-commit, after `run_
    commit_pipeline`'s own internal lock has already released. The AC6
    liveness re-check (`redrive_consumed_set()`) still re-derives fresh
    against current disk immediately before the stamp write — it was never
    a cache of the initial resolve — but no longer runs inside a
    cross-session-serializing critical section; see DEC-3's "what this
    jettisons" note in the plan for the residual-safety-floor rationale
    (per-file RMW locking, git's own index.lock, DR-215 spawn-per-call
    rarity of true concurrent same-tree ceremonies).
    CAVEAT — DEC-3's third floor-premise is REFUTED. Concurrent same-tree
    ceremonies are NOT rare: >=2 distinct sessions committed to this tree on
    26 of 26 active days (2026-07-03..28), and claude-klabauter 143c70a3 falsified the
    same DR-215 serial-execution premise live. The first two floors (per-file
    RMW, index.lock) hold as stated; the third does not. This does NOT argue
    for resurrecting the outer hold — see
    docs/research/2026-07-28-is-the-jettisoned-ceremony-lock-outer-ho.md,
    which finds every silent-corruption path survives that hold anyway.
  - Does NOT re-derive `committed_sha` from a racy late `git rev-parse HEAD`
    — `committed_sha` is a required caller-supplied parameter, captured by
    the orchestrator's C4 stage/commit/push step at the moment of the real
    commit (Position A, no branch-tip fallback).
  - Does NOT implement C4's full push-with-retry (fetch/rebase-onto/retry
    loop) for the follow-up commit's push — this module issues one direct
    `git push` via C1's `git_native.push()` and reports the outcome
    (`follow_up_pushed`); a future integration point for the orchestrator to
    route this follow-up push through C4's shared retry pipeline once C4
    lands is intentionally left open (AC17 only requires the follow-up
    commit's push to happen inside `ceremony_lock` via C1's Windows-safe
    helper — it does not require the full retry pipeline for this small
    single-file mutation).
  - Does NOT stamp non-chain-terminal sessions — `chain_terminal=False`
    short-circuits both entry points to a no-op outcome.
  - Does NOT implement the P2/P3 legacy composite (branch-tip-fallback
    `shipped_in` + Session-Id sibling-correction) the OLD bash
    `coordinator-handoff-archive.sh --stamp-only` helper carried — Position A
    (PM-ratified 2026-07-15) explicitly rejects both; see module docstring
    above and the cited review sidecar.
"""

from __future__ import annotations
import sys

import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import get_op_handler
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony.commit_pipeline import PUSH_MODE_SYNC
from coordinator_core.ops.ceremony.resolver import find_all_consumed_handoffs
from coordinator_core.ops.fleet._common import main_worktree_root

# Import side-effect: registers "handoff.has_live_children" and "handoff.stamp"
# in the ipc op-registry so get_op_handler() below resolves via a direct
# registry hit rather than its lazy-import fallback (get_op_handler()
# self-resolves a MISS since 2026-07-25, so this pre-import is belt-and-braces,
# not strictly required for correctness).
#
# The R4(a) pre-lock guard (`_live_children_guard`, below) still calls
# "handoff.has_live_children" through the public op-registry contract. The
# stamp/ship writes themselves do NOT (2026-07-28, Row 6/Row 7 CAS fix):
# `build_stamp_mutate`/`build_ship_mutate` are PUBLIC mutate-closure builders
# `handoff_stamp.py`/`handoff_transition.py` factored out precisely so this
# module can compose them with its own in-lock re-verification inside a
# SINGLE `locked_rmw` call each (`_stamp_with_live_children_recheck`,
# `_ship_with_cas` below) — routing that composition through the async
# op-registry handlers would mean a SECOND, separate `locked_rmw` acquisition
# per write, which is exactly the non-atomic gap Row 6/Row 7 exist to close.
import coordinator_core.ops.handoff_children  # noqa: F401
import coordinator_core.ops.handoff_stamp  # noqa: F401
from coordinator_core.ops.handoff_children import CONCLUSION_EDGE_KINDS
from coordinator_core.ops.handoff_stamp import build_stamp_mutate
from coordinator_core.ops.handoff_transition import _PathNotContained, _resolve_path, build_ship_mutate

_LOG = logging.getLogger(__name__)

#: R3 plausibility-guard skew slack — a filename timestamp within this window
#: of wall-clock now() is NOT treated as implausibly future-dated (clock skew
#: / same-second race tolerance). Deliberately small — this guard exists to
#: catch gross future-dating (a mis-generated or hand-typed filename), not to
#: police sub-minute clock drift.
_FUTURE_DATE_SKEW = timedelta(minutes=5)

#: R3 plausibility-guard timezone-ambiguity allowance — filename date/time
#: prefixes are producer-dependent and the guard cannot know, from the
#: filename alone, which producer wrote a given one: example-doctrine-repo's
#: `/handoff` and `/spinoff` skills (`coordinator/skills/handoff/SKILL.md`,
#: ~line 146) stamp LOCAL wall-clock time (`$(date +%Y-%m-%d)_$(date
#: +%H%M%S)`), while this repo's own `handoff_author_fork.py`
#: `_fork_handoff_filename` (~line 315) stamps UTC. Real UTC offsets span
#: -12h..+14h, so a legitimately local-time filename from a machine up to
#: UTC+14 can read as up to 14h "future" relative to a naive-UTC now() even
#: though it is not future-dated at all — this allowance absorbs that full
#: span on top of `_FUTURE_DATE_SKEW`'s clock-skew slack. Confirmed
#: real-world case (BST, UTC+1, still tripped the un-widened 5-minute bound):
#: `cross-repo/inbox/2026-07-23-claude-central-em-wsc-tail-stamp-ship-silent-skip.md`.
_FILENAME_TZ_AMBIGUITY = timedelta(hours=14)

#: Matches the two live handoff filename shapes seen on disk:
#:   YYYY-MM-DD_HHMMSS_<rest>.md   (full datetime prefix)
#:   YYYY-MM-DD-<rest>.md          (date-only prefix, implicit midnight)
_FULL_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(\d{2})_")
_DATE_ONLY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-")


# ---------------------------------------------------------------------------
# R3 — future-dated filename plausibility guard (AC8)
# ---------------------------------------------------------------------------


def _parse_filename_timestamp(relpath: str) -> Optional[datetime]:
    """Extract a UTC-naive datetime from a handoff filename's date/datetime
    prefix, or None when the prefix is unparseable (graceful-skip, AC8).

    Tries the full ``YYYY-MM-DD_HHMMSS_`` prefix first, then falls back to
    the date-only ``YYYY-MM-DD-`` prefix (implicit midnight). Returns None
    (never raises) for any filename that matches neither shape or contains
    an out-of-range date/time component.
    """
    name = Path(relpath).name
    m = _FULL_TS_RE.match(name)
    if m:
        y, mo, d, h, mi, s = (int(g) for g in m.groups())
        try:
            return datetime(y, mo, d, h, mi, s)
        except ValueError:
            print(f"skip: _parse_filename_timestamp: return datetime(y, mo, d, h, mi, s) failed: {sys.exc_info()[1]}", file=sys.stderr)
            return None
    m = _DATE_ONLY_RE.match(name)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return datetime(y, mo, d)
        except ValueError:
            print(f"skip: _parse_filename_timestamp: return datetime(y, mo, d) failed: {sys.exc_info()[1]}", file=sys.stderr)
            return None
    return None


def reject_future_dated(
    relpaths: list[str],
    *,
    now: Optional[datetime] = None,
    skew: timedelta = _FUTURE_DATE_SKEW,
    tz_ambiguity: timedelta = _FILENAME_TZ_AMBIGUITY,
) -> tuple[list[str], list[str]]:
    """Split relpaths into (accepted, rejected) per R3 (AC8).

    A path is REJECTED only when its filename timestamp prefix parses AND
    that timestamp is strictly later than ``now() + tz_ambiguity + skew``.
    Unparseable prefixes are a graceful-skip — accepted, not rejected (this
    guard is a plausibility check on well-formed filenames, not a
    filename-format enforcement gate). Deliberately compared against
    wall-clock ``now()``, never against the closing session's
    ``started_at`` (see module docstring R3).

    ``tz_ambiguity`` (default ``_FILENAME_TZ_AMBIGUITY``) exists because
    filename date/time prefixes are producer-dependent (LOCAL time from
    example-doctrine-repo's `/handoff`+`/spinoff` skills vs. UTC from this repo's own
    `handoff_author_fork.py`) — see the module-level constant's docstring
    for the full rationale and the confirmed real-world case it fixes.
    ``skew`` (default ``_FUTURE_DATE_SKEW``) remains the separate, small
    clock-drift/same-second-race allowance — the two are deliberately NOT
    folded into one magic number.

    ``now`` defaults to the current UTC wall-clock time (naive, matching the
    naive parse in ``_parse_filename_timestamp``) — injectable for tests.
    """
    if now is None:
        # Naive UTC now(), matching the naive parse in _parse_filename_timestamp
        # (filenames carry no timezone marker to be aware of).
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    bound = now + tz_ambiguity + skew

    accepted: list[str] = []
    rejected: list[str] = []
    for relpath in relpaths:
        ts = _parse_filename_timestamp(relpath)
        if ts is None:
            accepted.append(relpath)  # graceful-skip: unparseable, not rejected
            continue
        if ts > bound:
            rejected.append(relpath)
        else:
            accepted.append(relpath)
    return accepted, rejected


# ---------------------------------------------------------------------------
# R1 (AC6) — liveness re-check / re-derive the consumed-handoff set
# ---------------------------------------------------------------------------


def redrive_consumed_set(
    worktree_root: Path, session_id: str
) -> list[tuple[str, dict[str, Any]]]:
    """Re-derive the FULL consumed-handoff set for ``session_id`` fresh
    against current disk (R1/AC6).

    This is the choke point that narrows the cross-session shared-worktree
    race: called again immediately before the stamp write, so a concurrent
    session's mutation or archival of a handoff between the ceremony's
    INITIAL resolve and this re-derive is picked up rather than acted on
    stale data.

    UNLOCKED since DEC-3 (wsc-tail-sub-2s-invoke-budget, PM 2026-07-22).
    This re-derive was designed to run inside the caller's held
    `ceremony_lock`; that outer hold is jettisoned and NO lock is held here
    now. It therefore NARROWS the window rather than closing it, and the
    residual gap is not uniformly benign:
      - a peer ARCHIVING a candidate between re-derive and stamp fails loud
        (pathspec miss -> `StampOutcome.errors`);
      - a peer AUTHORING a successor that names a candidate as predecessor
        in that same window is SILENT — R4's live-children guard is stale by
        construction, and no lock can close it, because handoff authoring
        never acquires `ceremony_lock` at all.
    Full interleaving analysis (12 rows, 4 silent):
    docs/research/2026-07-28-is-the-jettisoned-ceremony-lock-outer-ho.md
    section (b), rows 7 and 9. It recommends AGAINST resurrecting the outer
    hold — every silent row survives it. Delegates to the promoted public
    resolver contract
    (`coordinator_core.ops.ceremony.resolver.find_all_consumed_handoffs`)
    — the SAME implementation the initial resolve used, so the
    two calls can only ever disagree because disk state itself changed
    between them (which is exactly the race this function exists to catch),
    never because of a definitional drift between two different scanners.
    """
    return find_all_consumed_handoffs(worktree_root, session_id)


# ---------------------------------------------------------------------------
# R4(a) — handoff.has_live_children guard (Position A)
# ---------------------------------------------------------------------------


def _already_shipped_and_archived(relpath: str, fm: dict[str, Any]) -> bool:
    """True when a consumed-handoff candidate was already shipped AND swept
    to ``archive/handoffs/`` before this ceremony's stamp pass ran.

    `handoff.stamp`'s own containment guard deliberately refuses any path
    outside ``state/handoffs/`` — mutating an archived handoff is out of
    scope (see `handoff_stamp.py`'s docstring, 2026-07-08 op-family
    path-containment sweep). An async archival sweep can legitimately race
    ahead of `/workstream-complete`: an operator stamps `deployment_state:
    shipped` + `shipped_in` directly via `archive-stamp-cli ship-handoff`,
    the sweep archives the file to `archive/handoffs/YYYY-MM/`, and only
    THEN does the ceremony run and resolve it as a consumed handoff. In that
    shape the stamp this ceremony wants to apply is already on disk — there
    is nothing to do, so this is a no-op, not a failure. Calling the stamp
    verb anyway would only reproduce its containment refusal for no reason.

    ``relpath`` is a wire-id string (`wire_paths.rel_id` — always
    forward-slash, see that module's docstring), so this is a plain string
    prefix check, not a filesystem path comparison — correct on Windows and
    macOS alike without touching `pathlib`.
    """
    if not relpath.startswith("archive/handoffs/"):
        return False
    return fm.get("deployment_state") == "shipped" and bool(fm.get("shipped_in"))


async def _live_children_guard(
    handoff_abs_path: str, *, repo_root: Path
) -> tuple[bool, dict]:
    """Return (retain, raw_reply) for one consumed-handoff candidate.

    ``retain`` is True (do NOT stamp/ship — a live successor still names
    this handoff, or the check was indeterminate) on exit_code 0 (real live
    children) AND exit_code 2 (indeterminate, fail-closed) — NEVER on the
    `referenced` field alone, which `handoff.has_live_children` deliberately
    omits on exit_code=2 (see module docstring R4).

    An UNEXPECTED exception raised by the handler (anything other than a
    clean exit-code return) is caught here and translated into the same
    exit_code=2 fail-closed shape — this is the ONE call site for
    "handoff.has_live_children", reached from both the pre-lock filter and
    the in-lock recheck (`_stamp_with_live_children_recheck`), so guarding
    it here covers both without duplicating the try/except at either call
    site. Only `Exception` is caught: `asyncio.CancelledError` (Python
    3.8+, `BaseException`, not `Exception`) and `KeyboardInterrupt`/
    `SystemExit` (also `BaseException`) are never caught by `except
    Exception` and continue to propagate uninterrupted — a bare `except:`
    or `except BaseException:` would swallow those and is deliberately not
    used. The reply carries `crashed: True` plus the exception's type/repr
    for a caller inspecting the raw reply dict directly — that detail is
    NOT currently surfaced through `StampOutcome`, which records only the
    path in `skipped_indeterminate` (`list[str]`), so a crashed-handler
    indeterminate is indistinguishable from an ordinary exit-2 one to
    either caller of this function today.
    """
    handler = get_op_handler("handoff.has_live_children")
    if handler is None:
        # Fail-closed: treat as indeterminate/retain if the op somehow isn't
        # registered (should be unreachable — this module imports the
        # registering module at import time — but never silently ship on a
        # missing dependency).
        return True, {
            "exit_code": 2,
            "error": "handoff.has_live_children not registered",
        }
    try:
        # This is the WSC tail — the writer that actually stamps `shipped_in:
        # <sha>` and flips `deployment_state -> shipped`, in place, post-commit,
        # no `git mv`. It answers the conclusion question ("may this workstream
        # CONCLUDE?"), not the archival question — see dag.CONTINUATION_
        # EDGE_KINDS for why the spinoff edge (`forked_from`) is excluded
        # from that question. Must agree with `workstream_complete`'s leg-B
        # advisory gate (`_dispatch_has_live_children` / `_LEG_B_EDGE_KINDS`)
        # — otherwise that advisory gate could say "a live spinoff does not
        # block the close" while THIS writer, the one that actually performs
        # the close, still retains on that same spinoff (example-cockpit-repo-em,
        # 2026-08-05, cross-repo/inbox/2026-08-05-example-cockpit-repo-em-wsc-leg-
        # b-counts-spinoffs-as-live-children.md). Archival protection is NOT
        # weakened by this — `fleet.archive_completed_handoffs` runs its own
        # `reverse_membership` guard at the op's archival default (all three
        # edge kinds; see `ops/fleet/archive_handoffs.py::_is_terminal`'s
        # Check 3), entirely independent of this call.
        reply = await handler(
            {"candidate": handoff_abs_path, "edge_kinds": CONCLUSION_EDGE_KINDS},
            repo_root,
        )
    except Exception as exc:
        return True, {
            "exit_code": 2,
            "crashed": True,
            "error": f"handoff.has_live_children raised {type(exc).__name__}: {exc}",
            "exception_type": type(exc).__name__,
        }
    exit_code = reply.get("exit_code")
    retain = exit_code in (0, 2)
    return retain, reply


# ---------------------------------------------------------------------------
# Row 7 (in-lock live-children re-check) + Row 6 (stamp/ship pair CAS) —
# 2026-07-28 ceremony-lock-hold-resurrection spinoff, rows 6/7 of
# docs/research/2026-07-28-is-the-jettisoned-ceremony-lock-outer-ho.md § (b).
# Neither window is closeable by re-acquiring `ceremony_lock` (that hold was
# PM-ratified permanently dead on 2026-07-28; see module docstring CAVEAT
# above) -- the interleaving peer in both rows is a handoff AUTHOR, which
# never takes that lock. Both fixes instead narrow the gap at the write
# itself, using `locked_rmw`'s existing re-read-then-abort mechanism
# (`MutateAbort`) rather than any new locking primitive.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StampAttempt:
    """Outcome of one in-lock stamp attempt (Row 7 CAS).

    Four mutually exclusive shapes: `retain_live`, `retain_indeterminate`,
    `ok` (the write landed or was an idempotent no-op), or all three False
    with `error` set. A retain means the write never happened (`MutateAbort`,
    no disk change). `error` names why the write attempt failed for a
    reason unrelated to the live-children recheck (lock timeout, malformed
    frontmatter, I/O).

    ``result_text`` is `locked_rmw`'s own return value: the file's fresh
    on-disk text the instant this call returns successfully, whether a real
    write happened or the idempotent no-op path was taken. This is Row 6's
    expected-state anchor for the immediately-following ship attempt --
    None whenever ``ok`` is False.
    """

    ok: bool = False
    applied: bool = False
    retain_live: bool = False
    retain_indeterminate: bool = False
    error: Optional[str] = None
    result_text: Optional[str] = None


async def _stamp_with_live_children_recheck(
    relpath: str, handoff_abs_path: str, committed_sha: str, *, repo_root: Path
) -> _StampAttempt:
    """Row 7: close the gap between the pre-lock `_live_children_guard` call
    (in the caller's loop, above) and the stamp write actually landing.

    Composes `handoff_stamp.build_stamp_mutate`'s pure mutate closure with a
    SECOND call to the SAME `_live_children_guard` the pre-lock filter
    already calls -- there is exactly one live-children check
    implementation, invoked twice: once as a cheap early-out before any lock
    is taken, once here, immediately before the write, with the target
    file's lock already held by THIS `locked_rmw` call. If a successor was
    authored in the window between those two calls, this second call sees
    it and raises `MutateAbort` -- the lock releases cleanly, nothing is
    written, and the caller routes the retain into
    `StampOutcome.skipped_live_children` / `skipped_indeterminate`, the same
    buckets the pre-lock retain path already used.

    The recheck runs via `asyncio.run()` inside the mutate callable. That
    callable executes on a plain `ThreadPoolExecutor` thread (`locked_rmw`
    itself is reached via `asyncio.to_thread`), which never has a running
    event loop of its own -- nesting a fresh loop there is safe; it is not
    re-entering the loop that is awaiting this coroutine.

    Mirrors `handoff_stamp._handler`'s own containment guard (resolved path
    must be under ``state/handoffs/``) -- this function calls
    `build_stamp_mutate` directly rather than routing through `_handler`
    (see the module-level import-side-effect comment for why), so it must
    replicate that check itself rather than silently losing it.
    """
    import asyncio

    worktree_root = main_worktree_root(repo_root)
    resolved = contained_path(
        Path(handoff_abs_path), [worktree_root / "state" / "handoffs"]
    )
    if resolved is None:
        return _StampAttempt(
            error=f"handoff_path escapes state/handoffs/: {handoff_abs_path!r}"
        )

    stamp_mutate, stamp_state = build_stamp_mutate(
        relpath, committed_sha, "ship-commit", force=False
    )
    _retain_reply: list[Optional[dict]] = [None]

    def _combined_mutate(old_text: str) -> str:
        retain, reply = asyncio.run(
            _live_children_guard(handoff_abs_path, repo_root=repo_root)
        )
        if retain:
            _retain_reply[0] = reply
            raise MutateAbort("row7-recheck-retain")
        return stamp_mutate(old_text)

    try:
        result_text = await asyncio.to_thread(
            locked_rmw, resolved, _combined_mutate, repo_root=repo_root
        )
    except LockTimeout as exc:
        return _StampAttempt(error=f"stamp lock timeout: {exc}")
    except MutateAbort as exc:
        reply = _retain_reply[0]
        if reply is not None:
            if reply.get("exit_code") == 0:
                return _StampAttempt(retain_live=True)
            return _StampAttempt(retain_indeterminate=True)
        # A MutateAbort not raised by the recheck -- the stamp mutate's own
        # abort path (e.g. no parseable frontmatter). Surfaced as an error,
        # matching the pre-refactor stamp_reply.get("error") shape.
        return _StampAttempt(
            error=str(exc.args[0]) if exc.args else "stamp mutate aborted"
        )
    except OSError as exc:
        return _StampAttempt(error=f"stamp I/O error: {exc}")

    return _StampAttempt(
        ok=True, applied=bool(stamp_state["applied"][0]), result_text=result_text
    )


async def _ship_with_cas(
    relpath: str, expected_text: str, worktree_root: Path, repo_root: Path
) -> dict:
    """Row 6: make the stamp-then-ship pair safe against an interleaved peer
    write, without merging the two into one lock acquisition (schema Rule
    A3a-2 requires `shipped_in` to land BEFORE `deployment_state: shipped`
    -- see module docstring Negative-spec "Stamp-before-ship ordering"; a
    combined write would still need to apply the two field-writes in that
    order, so splitting them buys nothing and this function does not
    attempt it).

    ``expected_text`` is the exact text `_stamp_with_live_children_recheck`
    observed on disk the instant its own write completed (`locked_rmw`'s
    return value). This ship attempt re-reads the file fresh under ITS OWN
    lock (same as `handoff_transition._ship` always did) and compares that
    fresh read against ``expected_text`` BEFORE applying the
    deployment_state flip: any mismatch means a peer wrote this handoff in
    the gap between the stamp's lock release and this lock's acquisition,
    and the write aborts via `MutateAbort` rather than silently proceeding
    on top of content this ceremony never saw. The peer's write is
    therefore never lost -- it stays on disk untouched, and the abort is
    reported through `StampOutcome.errors` by the caller, the same
    reporting path an ordinary ship failure already used before this fix.

    Returns the same envelope shape as `handoff_transition._ship`
    (exit_code/applied/message/error) so the caller needs no special-casing
    for the CAS-abort path versus a genuine ship failure.
    """
    import asyncio

    try:
        path = _resolve_path(relpath, worktree_root)
    except _PathNotContained as exc:
        return {"exit_code": 1, "applied": False, "error": f"ship: {exc}"}

    ship_mutate, ship_state = build_ship_mutate(relpath)

    def _combined_mutate(old_text: str) -> str:
        if old_text != expected_text:
            raise MutateAbort(
                f"ship: {relpath} changed on disk between the stamp write "
                "and this ship attempt (Row 6 CAS) -- a peer wrote this "
                "handoff in the gap; ship aborted rather than risk "
                "overwriting or losing that write"
            )
        return ship_mutate(old_text)

    try:
        await asyncio.to_thread(locked_rmw, path, _combined_mutate, repo_root=repo_root)
    except FileNotFoundError:
        return {
            "exit_code": 1,
            "applied": False,
            "error": f"ship: handoff not found: {relpath}",
        }
    except LockTimeout as exc:
        return {
            "exit_code": 1,
            "applied": False,
            "error": f"ship: timed out waiting for file lock on {relpath}: {exc}",
        }
    except MutateAbort as exc:
        msg = str(exc.args[0]) if exc.args else "ship: mutation aborted"
        return {"exit_code": 1, "applied": False, "error": msg}

    return {
        "exit_code": 0,
        "applied": bool(ship_state["applied"]),
        "message": ship_state["message"],
    }


# ---------------------------------------------------------------------------
# Post-commit stamp + ship + follow-up commit — R1/R2/R3/R4 full pass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StampOutcome:
    """Outcome of `post_commit_stamp_and_ship()` — the post-commit half of
    C5's R1-R4 pass. See module docstring for field semantics."""

    stamped: list[str] = field(default_factory=list)
    skipped_future_dated: list[str] = field(default_factory=list)
    skipped_live_children: list[str] = field(default_factory=list)
    skipped_indeterminate: list[str] = field(default_factory=list)
    #: Already shipped AND archived before this pass ran (see
    #: `_already_shipped_and_archived`) — a genuine no-op, never promoted to
    #: `failed` the way `skipped_future_dated`/`skipped_live_children`/
    #: `skipped_indeterminate` are on a stamp-nothing chain-terminal close
    #: (those name an actionable remediation; this one names nothing left to do).
    skipped_already_shipped: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    empty_consumed_set: bool = False  # R2 loud-report flag (AC7)
    follow_up_committed_sha: Optional[str] = None
    #: True/False under `push_mode="sync"` (today's contract); None under
    #: "deferred"/"none" (DEC-1) -- no push was attempted here, not a
    #: not-yet-determined failure.
    follow_up_pushed: Optional[bool] = None
    follow_up_error: Optional[str] = None


def _compose_follow_up_message(stamped: list[str], committed_sha: str) -> str:
    """Compose the follow-up commit's message body.

    Not subject to AC4's golden-format parity requirement (that requirement
    is scoped to C2's MAIN ceremony commit-message composer) — this is a
    small single-purpose stamp-record commit.
    """
    lines = [
        f"ceremony: stamp shipped_in={committed_sha} on consumed handoff(s)",
        "",
    ]
    for p in stamped:
        lines.append(f"- {p}")
    return "\n".join(lines) + "\n"


async def post_commit_stamp_and_ship(
    worktree_root: Path,
    repo_root: Path,
    session_id: str,
    committed_sha: str,
    *,
    chain_terminal: bool,
    push_mode: str = PUSH_MODE_SYNC,
) -> StampOutcome:
    """R1-R4 post-commit pass. Runs UNLOCKED (see module docstring Negative-
    spec — this function has never acquired `ceremony_lock` itself, and as of
    DEC-3 its caller no longer holds one either).

    No-op (empty result, `empty_consumed_set=False`) when ``chain_terminal``
    is False.

    On a non-empty stamped set, commits ONLY the stamped frontmatter files in
    one explicit-pathspec follow-up commit (AC17) — never left as an unswept
    dirty working-tree edit. The commit ALWAYS happens (AC17's commit leg is
    unconditionally synchronous, per plan anti-scope). The PUSH half is
    gated by ``push_mode`` (wsc-tail-sub-2s-invoke-budget DEC-1): `"sync"`
    (default) pushes the follow-up commit directly, as before; `"deferred"`/
    `"none"` skip the push here entirely — `follow_up_pushed` comes back
    `None` (no attempt, not a failure) — leaving the branch tip to the
    caller's own single detached push spawned after the whole tail completes.
    """
    # asyncio deferred to first use here (not module scope, and re-deferred
    # locally in `_stamp_with_live_children_recheck` / `_ship_with_cas`
    # below for the same reason) — a module-scope `import asyncio` dragged
    # asyncio.base_events (~8ms) into every eager-loaded op import path that
    # never calls post_commit_stamp_and_ship. Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    if not chain_terminal:
        return StampOutcome()

    # R1/AC6 — liveness re-check: re-derive fresh, do NOT reuse the initial
    # (pre-lock) resolve's consumed set.
    redrived = redrive_consumed_set(worktree_root, session_id)
    redrived_paths = [p for p, _fm in redrived]
    fm_by_path: dict[str, dict[str, Any]] = dict(redrived)

    if not redrived_paths:
        # R2 (AC7): chain-terminal close, empty re-derived set at stamp
        # time — loud report, never a failure.
        return StampOutcome(empty_consumed_set=True)

    # R3 (AC8) — future-dated plausibility guard.
    accepted_paths, rejected_paths = reject_future_dated(redrived_paths)

    outcome_stamped: list[str] = []
    outcome_live: list[str] = []
    outcome_indeterminate: list[str] = []
    outcome_already_shipped: list[str] = []
    outcome_errors: list[dict[str, str]] = []

    for relpath in accepted_paths:
        handoff_abs = str(worktree_root / relpath)

        # R4(a) — live-children guard: RETAIN (skip) on exit_code 0 (real
        # live children) or 2 (indeterminate, fail-closed).
        retain, reply = await _live_children_guard(handoff_abs, repo_root=repo_root)
        if retain:
            if reply.get("exit_code") == 0:
                outcome_live.append(relpath)
            else:
                outcome_indeterminate.append(relpath)
            continue

        # Already-terminal no-op guard: a predecessor stamped `shipped_in`/
        # `deployment_state: shipped` directly (e.g. via `archive-stamp-cli
        # ship-handoff`) and was THEN swept to archive/handoffs/ before this
        # pass ran. `handoff.stamp` refuses any archive/handoffs/ path by
        # design (see `_already_shipped_and_archived`'s docstring) — recognize
        # the no-op here, before calling the verb, rather than routing around
        # its containment guard or reporting its refusal as a failure.
        if _already_shipped_and_archived(relpath, fm_by_path.get(relpath, {})):
            outcome_already_shipped.append(relpath)
            continue

        # R4(b)+(c), stamp-BEFORE-ship (see module docstring "DEVIATION" /
        # Negative-spec "Stamp-before-ship ordering is LOAD-BEARING"):
        # shipped_in must already be on disk before deployment_state flips to
        # shipped, or schema_validate.py's _cf_shipped_in_required (Rule
        # A3a-2) hard-fails the ship transition for any handoff created on or
        # after 2026-05-29.
        #
        # Row 7: the write itself re-verifies the live-children premise a
        # SECOND time, inside the stamp's own `locked_rmw` lock hold, right
        # before the write lands -- closing the gap between the pre-lock
        # guard call above and this write (see
        # `_stamp_with_live_children_recheck`'s docstring).
        stamp_attempt = await _stamp_with_live_children_recheck(
            relpath, handoff_abs, committed_sha, repo_root=repo_root
        )
        if stamp_attempt.retain_live:
            outcome_live.append(relpath)
            continue
        if stamp_attempt.retain_indeterminate:
            outcome_indeterminate.append(relpath)
            continue
        if not stamp_attempt.ok:
            outcome_errors.append(
                {"path": relpath, "error": stamp_attempt.error or "stamp failed"}
            )
            continue

        stamp_applied = stamp_attempt.applied

        # Row 6: ship is a CAS against the exact text the stamp write just
        # produced -- a peer write landing in the gap between the stamp's
        # lock release and this lock's acquisition aborts the ship rather
        # than silently losing that write (see `_ship_with_cas`'s docstring).
        ship_reply = await _ship_with_cas(
            relpath, stamp_attempt.result_text, worktree_root, repo_root
        )
        ship_applied_or_already = ship_reply.get("exit_code") == 0
        if not ship_applied_or_already:
            outcome_errors.append(
                {"path": relpath, "error": ship_reply.get("error", "ship failed")}
            )
            # Fall through: if the stamp mutated the file, it MUST still be
            # staged into the follow-up commit (AC17 — never left dirty),
            # even though the ship half of this candidate failed.

        if stamp_applied or ship_reply.get("applied"):
            outcome_stamped.append(relpath)
        # Neither mutation applied (both were pre-existing idempotent
        # no-ops) — nothing changed on disk for this path, nothing to stage.

    if not outcome_stamped:
        return StampOutcome(
            skipped_future_dated=rejected_paths,
            skipped_live_children=outcome_live,
            skipped_indeterminate=outcome_indeterminate,
            skipped_already_shipped=outcome_already_shipped,
            errors=outcome_errors,
        )

    follow_up_sha, pushed, follow_up_error = await asyncio.to_thread(
        _commit_and_push_follow_up, worktree_root, outcome_stamped, committed_sha, push_mode
    )

    return StampOutcome(
        stamped=outcome_stamped,
        skipped_future_dated=rejected_paths,
        skipped_live_children=outcome_live,
        skipped_indeterminate=outcome_indeterminate,
        skipped_already_shipped=outcome_already_shipped,
        errors=outcome_errors,
        follow_up_committed_sha=follow_up_sha,
        follow_up_pushed=pushed,
        follow_up_error=follow_up_error,
    )


def _commit_and_push_follow_up(
    worktree_root: Path, stamped_paths: list[str], committed_sha: str, push_mode: str = PUSH_MODE_SYNC
) -> tuple[Optional[str], Optional[bool], Optional[str]]:
    """Computed-mechanism commit (`git_native.commit_scoped`) of ONLY the
    stamped paths (AC17) — the commit leg is unconditional (anti-scope: never
    weakened). The push leg is gated
    by ``push_mode`` (DEC-1): `"sync"` pushes here directly (today's
    contract); `"deferred"`/`"none"` skip the push and return `pushed=None`
    (no attempt, not a failure) — the caller spawns ONE detached push for the
    whole tail after it completes.

    A returned ``error`` is the ONLY success signal a caller may trust — do not
    corroborate it against the branch tip. On a shared worktree an `index.lock`
    contention failure here is loud at the call but has been misread as success
    downstream, because the log tip a caller printed to "confirm" the commit was
    a PEER's commit, not this one (lesson 2026-07-21-git-index-lock-contention;
    row 10 of docs/research/2026-07-28-is-the-jettisoned-ceremony-lock-outer-ho.md).

    Returns (follow_up_sha, pushed, error). ``follow_up_sha`` is the real
    post-commit SHA of THIS follow-up commit (captured via `rev_parse_head`,
    never a branch-tip guess). A push failure is reported, not raised — the
    follow-up commit itself already landed locally; a caller may re-attempt
    the push on resumption (see AC18 crash-resumption design, C9's concern).
    """
    message = _compose_follow_up_message(stamped_paths, committed_sha)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(message)
        msg_path = fh.name

    try:
        # Routed through the computed selector (not a raw `add_paths` +
        # `commit_with_message_file` pair) -- see `git_native.commit_scoped`'s
        # docstring. `stamped_paths` is guaranteed non-empty here (the
        # caller returns early on an empty `outcome_stamped`); `commit_scoped`
        # stages internally on the AGREE branch and never re-derives staged
        # content from the worktree on the DIVERGED branch, closing the same
        # 506748a0 hazard the main ceremony commit is already routed around.
        commit_result = git_native.commit_scoped(
            stamped_paths, msg_path, worktree_root
        )
    finally:
        try:
            Path(msg_path).unlink()
        except OSError:
            print(f"skip: _commit_and_push_follow_up: Path(msg_path).unlink() failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass

    if not commit_result.ok:
        return None, False, f"git commit failed: {commit_result.stderr}"

    rev_result = git_native.rev_parse_head(worktree_root)
    follow_up_sha = rev_result.stdout.strip() if rev_result.ok else None

    # Review: code-reviewer — Finding 3: use the canonical PUSH_MODE_SYNC
    # constant (commit_pipeline.py's own enum) instead of a bare string
    # literal so this stays in sync if the canonical value ever changes.
    if push_mode != PUSH_MODE_SYNC:
        return follow_up_sha, None, None

    push_result = git_native.push(worktree_root)
    pushed = push_result.ok
    push_error = None if pushed else f"git push failed: {push_result.stderr}"

    return follow_up_sha, pushed, push_error
