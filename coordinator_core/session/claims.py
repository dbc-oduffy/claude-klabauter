"""
coordinator_core.session.claims — Python engine port of the CLAIMS module of
the coordinator session hub (Port of: coordinator-session.sh, DoE e34f2484,
2026-07-22).

The claim primitives were deliberately NOT extracted to a ``lib/session/``
sub-module by T0-decompose — they live in the MAIN bash file — so this is the
faithful port of that in-file cluster: the atomic mkdir-based claim/takeover/
release/clear machinery for concurrent ``/pickup`` and plan-execution race
detection, plus the touched.txt self-attribution helpers.

This is a PURE IN-PROCESS LIBRARY, not an IPC op — it self-registers nothing,
touches none of the shared op-registry files, and is imported directly by
sibling modules and (in a LATER chunk) by the native op wrappers. The bash
callers (pickup / execute-plan / workstream-complete SKILL.md, handoff) are a
SEPARATE repoint chunk, NOT rewired here.

TWO DISTINCT DISCIPLINES the claim layer routes through (both in liveness.py):
    - claim TAKEOVER / clear / reap ask the LIVENESS question — is the HOLDER
      still alive? — via ``liveness.claim_holder_live`` (the claim dir's OWN
      metadata, NEVER the caller's pid/sid). One rule, shared with the reaper
      and the memo sweep (single-liveness-key invariant, D5/pcore-03).
    - release / plan re-entrancy ask the IDENTITY question — am I the recorded
      holder? — via ``liveness.claim_held_by_me`` with a PRE-RESOLVED my_sid
      (the two-call TOCTOU re-read keys both reads off ONE identity).

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4a-g1
Spec backlinks (original claim contracts):
    archive/specs/2026-06/2026-06-17-foreign-cwd-pickup-hardening.md § C1;
    archive/specs/2026-06/2026-06-17-concurrent-pickup-guard-sid-regression.md § C1;
    archive/specs/2026-06/2026-06-21-memo-pickup-claim-lock-and-routed-plan-reconcile.md § C1;
    docs/plans/2026-06-26-cs-claim-plan-execution-lock.md § C1;
    docs/plans/2026-06-30-claim-clear-liveness-gate.md § C2;
    docs/plans/2026-07-02-ceremony-as-pipeline-v1-session-state-co.md § C3
Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § claims.py

Negative-spec:
    - The claim dir is BASENAME-ONLY, NEVER sid-namespaced. All sessions
      sharing a ``<root>`` contend for ONE lock per artifact; that shared-path
      mkdir IS the same-machine concurrent-pickup guard (DR-110). It was
      sid-namespaced until 2026-06-17, which silently defeated the guard once
      Claude Code moved to per-session CLAUDE_CODE_SESSION_ID. Do NOT
      re-namespace.
    - On EEXIST, evaluate liveness against the HOLDER's OWN metadata via
      ``liveness.claim_holder_live`` — NEVER against my pid/sid. Do NOT
      hand-roll a ``held_sid == sid`` string compare — that reintroduces the
      per-session bug and skips the session_id-vs-legacy-pid discrimination +
      internal TOCTOU re-read of ``liveness.claim_held_by_me``.
    - The re-entrant self-claim branch is PLAN-CLASS-ONLY. handoff/memo are
      claimed ONCE per session and their contract is "a same-session re-claim
      is REJECTED" (regression tests T16a/T18c). A global re-entrant branch
      would silently flip that to return-True and break byte-for-byte
      compatibility. Do NOT broaden it.
    - Do NOT re-port ``cs_reap_stale_claims`` (already native ->
      ``ops/session/reap.py``) nor the RETIRED review-claim helpers.
    - Do NOT call ``cs_claim_artifact`` (this ``claim_artifact``) from a hook
      subprocess — the recorded pid is the CALLER's ``os.getpid()``, which
      MUST be a long-lived process (skill/interactive shell); a hook subshell
      exits within seconds and its claim reads immediately dead to the reaper.
    - The ``brief``-stage lease (``claim_stage``/``brief_lease_expired``) is
      the ONLY path on which a LIVE holder's claim is takeable. Do NOT extend
      that override to ``apply``-stage claims: an apply-stage claim is backed
      by a landed frontmatter stamp and real mutation, and its only takeover
      route stays the holder-is-dead one.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import List, Optional, Tuple, Union

from coordinator_core.session import claim_index
from coordinator_core.session import core
from coordinator_core.session import liveness
from coordinator_core.session import scope
from coordinator_core.session import shape


# ---------------------------------------------------------------------------
# Shared DR-084 lifecycle accessor loader (coordinator/bin/lib/handoff_lifecycle.py)
#
# Single exported loader for the frontmatter-level claimed_by/consumed_by
# dual-read accessor — distinct from this module's own mkdir-lock claim
# primitives above (artifact-first: given a path, who holds the mkdir lock).
# This is the session-first counterpart's shared dependency: given a session
# id, which handoff's claimed_by/consumed_by field names it — consumed by
# ``coordinator_core.ops.handoff_author_fork`` and
# ``coordinator_core.ops.session.resolve_chain_terminal_disposition``, both
# of which previously carried independent copies of this importlib-load
# boilerplate. The accessor file lives outside the ``coordinator_core``
# package (under the sibling ``coordinator/bin/lib/`` tree), so it cannot be
# a plain ``import`` — it must be loaded by file path via ``importlib``.
# ---------------------------------------------------------------------------

# Generator-provenance declaration (generator_provenance.py). Every write in this
# module (claim-dir pid/session_id/claimed_at/stage files, touched.txt appends,
# stamped markers) targets `.git/coordinator-sessions/<sid>/...` -- git-internal
# session-hub state, never a tracked repo artifact.
GENERATES = []

_HANDOFF_LIFECYCLE_ACCESSOR_PATH = (
    Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "lib" / "handoff_lifecycle.py"
)

_handoff_lifecycle_cache: Optional[ModuleType] = None


def handoff_lifecycle() -> ModuleType:
    """Load (once) the shared DR-084 lifecycle accessor module and cache it.

    Fail-loud when the accessor file is absent: a tree without it is a broken
    install, and falling back to a local dual-read would be exactly the
    second raw read site the single-accessor guard exists to forbid.
    """
    global _handoff_lifecycle_cache
    if _handoff_lifecycle_cache is not None:
        return _handoff_lifecycle_cache
    if not _HANDOFF_LIFECYCLE_ACCESSOR_PATH.is_file():
        raise RuntimeError(
            "coordinator_core.session.claims.handoff_lifecycle: shared DR-084 "
            f"accessor missing at {_HANDOFF_LIFECYCLE_ACCESSOR_PATH} — cannot "
            "resolve claim holders without the single shared lifecycle read "
            "site (never inline a dual-read here)"
        )
    spec = importlib.util.spec_from_file_location(
        "_claude_klabauter_handoff_lifecycle_accessor", _HANDOFF_LIFECYCLE_ACCESSOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            "coordinator_core.session.claims.handoff_lifecycle: importlib "
            f"could not build a spec for {_HANDOFF_LIFECYCLE_ACCESSOR_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    _handoff_lifecycle_cache = module
    return module


# ---------------------------------------------------------------------------
# touched.txt append primitive
# ---------------------------------------------------------------------------


def atomic_dedup_append(touched: str, entry: str) -> bool:
    """Port of ``cs_atomic_dedup_append <touched-file> <new-entry>`` (786-801),
    made EVENT-AWARE (EM ratification 2026-08-03, plan
    ``2026-08-03-scope-guard-peer-claim-release.md`` § EM ratification item 2,
    chunk C1g) — the SECOND ``touched.txt`` writer (reached from
    ``self_claim`` and, via ``js_bridge_cli``, from
    ``coordinator/lib/coordinator_session.py::claim_path``) now joins
    ``scope.touch()``'s event-log dialect instead of appending a bare,
    unstamped line.

    Append-only write (the fix for the T21 lost-update race: the prior
    mktemp+sort+mv pattern let N concurrent writers each read-then-overwrite,
    so the last mv won and earlier distinct-path merges were silently
    dropped). Still pure append, one short write under PIPE_BUF — NO mktemp,
    NO mv, NO flock; that discipline is the whole reason this function exists
    and is preserved exactly.

    ``entry`` is a bare PATH, never a whole event line — but this is a
    LOWER-LEVEL primitive, and only the ``self_claim`` caller is guaranteed
    to have repo-relativized it first (via ``scope.normalize_touch_path``).
    The other reachable caller (``js_bridge_cli._cmd_claim_path`` ->
    ``coordinator/lib/coordinator_session.py::claim_path``) forwards its
    ``entry`` argument verbatim, with NO normalization step of its own — an
    absolute-path caller there writes an absolute-path entry, which will
    never dedup-match a relative-form entry for the same file written via
    ``self_claim``. Normalizing that caller is a separate, out-of-scope
    change (Review: coordinatorcode-reviewer-7ca5d82a Finding 2) — the
    caller owns normalization at this layer, this function does not
    normalize on the caller's behalf.

    Two steps:
      1. Fast-exit if ``entry`` is already CLAIMED — i.e. the LAST event recorded for
         that path (via ``scope.parse_touch_event``, the single shared
         parser — no second dialect) is already ``T``. This subsumes the old
         whole-line ``grep -qxF`` dedup: a legacy bare-line record for
         ``entry`` parses to ``('T', None, entry)`` (§ ``parse_touch_event``
         fail-safe), so a path whose only record is a bare line is already
         CLAIMED and a ``self_claim`` for it correctly no-ops here too — no
         redundant append, no second dialect for the legacy corpus. Reading
         the file to decide whether to append is fine; rewriting it is not
         (this pass never writes back).
      2. Single-line append of ``scope.format_touch_event("T", entry)``. One
         short write under PIPE_BUF (4096) is atomic on POSIX O_APPEND files,
         and Windows NTFS (Git Bash) serializes concurrent appends without
         corruption.

    Silent-failure contract: ALWAYS returns True (bash ``return 0`` on every
    path), so an advisory hook never blocks tool calls. A missing/unreadable
    file simply falls through to the append; an append that itself fails
    (OSError) is swallowed and still reports success.

    ``touched`` and ``entry`` are REQUIRED (bash ``${1:?}`` / ``${2:?}``) —
    an empty value raises ValueError.
    """
    if not touched:
        raise ValueError("touched-file required")
    if not entry:
        raise ValueError("new-entry required")

    # Fast-exit: is the LAST event recorded for this path already 'T'?
    # Non-atomic read is fine — a false negative just falls through to the
    # append, where a duplicate T event may land (harmless: still CLAIMED,
    # and cleaned at next consumption-time dedup).
    #
    # Review: coordinatorcode-reviewer-7ca5d82a Finding 3 — mirrors
    # scope.touch()'s reversed-scan-and-break: only entry's own last event
    # is ever needed, so scan backward and stop at the first match rather
    # than building a Dict[str, str] over every distinct path in the file.
    already_claimed = False
    try:
        with open(touched, encoding="utf-8") as fh:
            lines = fh.readlines()
        for line in reversed(lines):
            stripped = line.rstrip("\n")
            if not stripped:
                continue
            verb, _ts, path = scope.parse_touch_event(stripped)
            if path == entry:
                already_claimed = verb == "T"
                break
    except OSError:
        pass  # file missing / unreadable -> fall through to append
    if already_claimed:
        return True

    try:
        with open(touched, "a", encoding="utf-8") as fh:
            fh.write(scope.format_touch_event("T", entry) + "\n")
    except OSError:
        return True  # silent-failure contract — never block the caller
    return True


# ---------------------------------------------------------------------------
# THE claim primitive + class wrappers
# ---------------------------------------------------------------------------


#: The two stages a claim dir can be in. ``apply`` is the historical (and
#: only) shape: a claim backed by a landed frontmatter stamp, takeable only
#: when its holder reads dead. ``brief`` is the pre-work reservation
#: ``pickup_assemble.brief`` takes so the read-verify-draft window between
#: `brief` and `apply` is no longer unguarded — it carries a wall-clock
#: lease (below) precisely because nothing durable backs it yet.
CLAIM_STAGE_BRIEF = "brief"
CLAIM_STAGE_APPLY = "apply"

#: How long a `brief`-stage claim survives, measured from the LAST brief of
#: that artifact by its holder (``touch_brief_claim`` refreshes it), before
#: any session may take it regardless of holder liveness. Env override:
#: COORDINATOR_BRIEF_CLAIM_LEASE_MINUTES.
#:
#: WHY A LEASE AT ALL. The dead-holder takeover path cannot bound this case:
#: ``liveness.session_live``'s Layer 1 is PPID-authoritative and does NOT
#: consult recency once ``stable_pid`` plus a birth witness are present, so a
#: session that briefs an artifact and walks away — without exiting — holds
#: the lock for its entire lifetime.
#:
#: WHY FOUR HOURS, AND NOT THE 30 THAT ``CLAIM_STALE_AFTER_MINUTES`` USES.
#: The two constants answer different questions and must not be aliased. The
#: cost of this one being too SHORT is severe and is the exact bug the
#: brief-stage claim exists to fix: a lease that elapses while its holder is
#: still verifying hands the artifact to a second session, and both then do
#: the work and both ship the external side effect. The cost of it being too
#: LONG is mild — a reservation nobody is advancing blocks a pickup, which
#: ``drop`` clears in one command, and which the DEAD-holder path reclaims
#: immediately anyway, lease or no lease. The lease is therefore load-bearing
#: only for a LIVE holder, where erring long is close to free.
#:
#: Sized against this box's load norm (docs/wiki/machine-load-norm.md: 50-70
#: concurrent LLMs, two dozen EMs as the floor): the brief-to-apply window
#: legitimately contains reading the artifact, verifying its claims against
#: HEAD, a dispatched subagent or two, and a test run — each of which is a
#: slow op here, not a hung one. Measured dispatches on this surface run past
#: 30 minutes routinely, so a 30-minute lease would expire mid-verification
#: as the NORMAL case. Four hours clears that with margin while still
#: bounding an abandoned reservation inside a working day.
BRIEF_CLAIM_LEASE_MINUTES = int(
    os.environ.get("COORDINATOR_BRIEF_CLAIM_LEASE_MINUTES", "240")
)


def _write_claim_meta(claim_dir: Path, sid: str, stage: str = CLAIM_STAGE_APPLY) -> None:
    """Write the ``pid`` / ``session_id`` / ``claimed_at`` / ``stage`` metadata
    files into a freshly-mkdir'd claim dir. ``pid`` is ``os.getpid()`` — the
    CALLER's pid, which MUST be long-lived (see module negative-spec)."""
    (claim_dir / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (claim_dir / "session_id").write_text(f"{sid}\n", encoding="utf-8")
    (claim_dir / "claimed_at").write_text(f"{core.now_iso()}\n", encoding="utf-8")
    (claim_dir / "stage").write_text(f"{stage}\n", encoding="utf-8")


def claim_stage(claim_dir: Union[str, Path]) -> str:
    """The stage of an existing claim dir — ``brief`` or ``apply``.

    A dir with NO ``stage`` file reads ``apply``: every claim written before
    the two-stage split, and every claim written by a caller that never asked
    for a brief-stage reservation, keeps exactly its historical semantics. An
    unrecognized value reads ``apply`` for the same reason — an unreadable
    stage must never be the thing that makes a live holder's claim takeable.
    """
    raw = _read_claim_field(Path(claim_dir), "stage")
    return CLAIM_STAGE_BRIEF if raw == CLAIM_STAGE_BRIEF else CLAIM_STAGE_APPLY


def claim_age_minutes(claim_dir: Union[str, Path]) -> Optional[int]:
    """Minutes since the claim dir's ``claimed_at``, or ``None`` when that file
    is missing or unparseable. An unreadable timestamp is an evidence gap —
    never fabricated as fresh or as expired."""
    raw = _read_claim_field(Path(claim_dir), "claimed_at")
    if not raw:
        return None
    claimed_epoch = core.iso_to_epoch(raw)
    if claimed_epoch <= 0:
        return None
    elapsed = core.now_epoch() - claimed_epoch
    return (elapsed if elapsed > 0 else 0) // 60


def brief_lease_expired(claim_dir: Union[str, Path]) -> bool:
    """True iff ``claim_dir`` holds a ``brief``-stage claim whose lease has
    elapsed — the one condition under which a LIVE holder's claim is takeable.

    False for every ``apply``-stage claim (those follow the dead-holder rule
    alone), and False when ``claimed_at`` is unreadable: an evidence gap is
    not evidence of expiry, so the conservative answer is "still held".
    """
    if claim_stage(claim_dir) != CLAIM_STAGE_BRIEF:
        return False
    age = claim_age_minutes(claim_dir)
    if age is None:
        return False
    return age > BRIEF_CLAIM_LEASE_MINUTES


def _claim_base(class_: str, baton_repo_root: str, cwd: Optional[str]) -> Optional[str]:
    """Resolve the ``<...>/coordinator-sessions`` base for a claim.

    Baton-repo mode (arg present) fails LOUD on an unresolvable root rather
    than silently writing the lock into the wrong (cwd) repo — returns None
    after emitting the diagnostic. Legacy (absent) mode resolves the cwd repo's
    sessions dir; None when not in a git repo.
    """
    if baton_repo_root:
        if not (Path(baton_repo_root) / ".git").is_dir():
            print(
                f"cs_claim_{class_}: baton repo root <{baton_repo_root}> is not a git repo",
                file=sys.stderr,
            )
            return None
        return str(Path(baton_repo_root) / ".git" / "coordinator-sessions")
    base = core.sessions_dir(cwd)
    if not base:
        return None
    return base


def claim_artifact(
    class_: str,
    basename: str,
    baton_repo_root: str = "",
    cwd: Optional[str] = None,
    stage: str = CLAIM_STAGE_APPLY,
) -> bool:
    """Port of ``cs_claim_artifact <class> <basename> [baton_repo_root]`` (864-962).

    THE atomic mkdir-based claim primitive for concurrent ``/pickup`` race
    detection, shared by the handoff/memo/plan classes (thin wrappers below).
    ``class_`` selects the claims subdir (``<class>-claims``) and the log
    prefix. Returns True on a successful (or re-entrant) claim, False on a
    live-holder collision, an unresolvable session id, a bad baton root, or a
    failed stale-takeover mkdir.

    Claim directory: ``<base>/<class>-claims/<basename>/`` — BASENAME-ONLY,
    NOT sid-namespaced (the shared-path mkdir IS the same-machine concurrent-
    pickup guard, DR-110 — do NOT re-namespace; see module negative-spec).

    ``baton_repo_root`` (optional): the git repo that OWNS the baton being
    picked up. When supplied, the claim lives under that repo's ``.git`` so two
    concurrent ``/pickup`` sessions of the SAME baton contest the same mkdir
    regardless of cwd (foreign-cwd pickup of a ``~/.claude`` baton). A
    SUPPLIED-but-non-git root FAILS LOUD (detect-then-fail-loud, never a silent
    cwd fallback). Only the lock LOCATION follows the baton — session-id
    resolution stays anchored to the running (cwd) session (the sentinel is
    written into the cwd session's ``.git`` by session-init, never the baton).

    On success: atomic ``os.mkdir`` then write pid/session_id/claimed_at inside
    the claim dir.

    On EEXIST — liveness is evaluated against the HOLDER (the claim dir's OWN
    metadata via ``liveness.claim_holder_live``), NEVER the caller:
      - PLAN-CLASS-ONLY re-entrant branch FIRST: if ``class_ == "plan"`` and
        ``liveness.claim_held_by_me(claim_dir, sid)``, return True immediately
        (re-entrant success). A plan is claimed at two seams — execute-plan +
        workstream-complete — which may both run in the same live session;
        without this branch the second seam would fail loud against itself.
        SCOPED to plan deliberately: handoff/memo are claimed once per session
        and their contract is "same-session re-claim REJECTED" (T16a/T18c). Do
        NOT broaden (module negative-spec).
      - Live holder -> failure (concurrent /pickup detected), UNLESS the
        existing claim is an EXPIRED ``brief``-stage lease (``brief_lease_
        expired``) — a reservation whose holder never came back to apply it.
        That one case falls through to the takeover branch below.
      - Dead / >30-min-idle holder -> ``rm -rf`` + re-mkdir takeover (the
        atomic rm+mkdir is itself the race guard: a peer that re-claims between
        them makes our mkdir fail).
      - Legacy pid-only claim dir (no session_id file) -> ``liveness.
        claim_holder_live`` falls back to the ephemeral-pid test.

    ``stage`` selects what is being taken. ``apply`` (the default, and what
    every pre-existing caller gets) is the durable claim backed by a landed
    frontmatter stamp. ``brief`` is ``pickup_assemble.brief``'s pre-work
    reservation, which self-expires after ``BRIEF_CLAIM_LEASE_MINUTES`` — see
    that constant's docstring for why liveness alone cannot bound it.
    Promotion ``brief`` -> ``apply`` is ``promote_claim_stage``, never a
    second ``claim_artifact`` call (handoff/memo reject a same-session
    re-claim by design).

    ``class_`` / ``basename`` are REQUIRED (bash ``${1:?}`` / ``${2:?}``) —
    empty raises ValueError.
    """
    if not class_:
        raise ValueError("artifact class required")
    if not basename:
        raise ValueError("basename required")

    # Canonical 4-tier resolution; sid is a property of the running (cwd)
    # session — only the lock LOCATION follows the baton. Empty -> FAIL LOUD.
    sid = core.resolve_session_id(cwd)
    if not sid:
        print(
            f"cs_claim_{class_}: session id unresolvable under concurrency — "
            f"run: export CLAUDE_SESSION_ID=<harness-id>",
            file=sys.stderr,
        )
        return False

    base = _claim_base(class_, baton_repo_root, cwd)
    if base is None:
        return False

    claims_dir = Path(base) / f"{class_}-claims"
    claim_dir = claims_dir / basename

    try:
        claims_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # mkdir -p ... 2>/dev/null || true

    # Attempt atomic mkdir. ANY failure (EEXIST or otherwise) falls through to
    # the EEXIST-inspection path, exactly as the bash `if mkdir ...; then` /
    # fall-through structure does.
    created = False
    try:
        os.mkdir(claim_dir)
        created = True
    except OSError:
        created = False
    if created:
        _write_claim_meta(claim_dir, sid, stage)
        return True

    # ---- EEXIST — inspect the existing claim (holder's OWN metadata) ----
    held_pid = _read_claim_field(claim_dir, "pid")
    held_sid = _read_claim_field(claim_dir, "session_id")

    # Re-entrant self-claim (PLAN CLASS ONLY) — BEFORE the liveness branch.
    if class_ == "plan" and liveness.claim_held_by_me(str(claim_dir), sid, cwd):
        return True

    lease_expired = brief_lease_expired(claim_dir)

    if liveness.claim_holder_live(str(claim_dir), cwd) and not lease_expired:
        live_pid = ""
        if held_sid:
            holder_sdir = core.session_dir(held_sid, cwd)
            if holder_sdir:
                live_pid = core.read_meta_field(holder_sdir, "stable_pid")
        if live_pid:
            pid_clause = f"live PID {live_pid} (session registry stable_pid)"
        elif not held_sid:
            # Legacy pid-only claim dir (no session_id file): the True verdict
            # we're inside this branch FOR came from claim_holder_live's own
            # fallback -- core.pid_alive(held_pid), the SAME pid printed here.
            # Liveness of held_pid IS confirmed (that's why we're here); what's
            # UNCONFIRMED is only the registry stable_pid cross-check, which
            # doesn't apply to a session_id-less dir. "not confirmed live"
            # would flatly contradict the branch we're in -- distinct wording
            # for the two distinct unknowns (19ac2abf fixed this message once
            # already; conflating them again is the same defect class).
            pid_clause = f"live PID {held_pid or '?'} (confirmed via legacy pid-liveness check, no session registry entry)"
        else:
            pid_clause = f"recorded-at-claim-time PID {held_pid or '?'} (not confirmed live)"
        print(
            f"cs_claim_{class_}: {basename} held by session {held_sid or '?'} "
            f"— {pid_clause} — concurrent /pickup detected; reconcile with that "
            f"session, or run clear-claim-if-dead once it exits",
            file=sys.stderr,
        )
        return False

    # Holder is dead / >30-min idle, or holds an expired brief-stage lease —
    # either way the claim is stale; take over.
    if lease_expired:
        print(
            f"cs_claim_{class_}: expired brief-stage claim on {basename} (session "
            f"{held_sid or '?'} reserved it {claim_age_minutes(claim_dir)}m ago and "
            f"never applied, lease is {BRIEF_CLAIM_LEASE_MINUTES}m) — taking over",
            file=sys.stderr,
        )
    else:
        print(
            f"cs_claim_{class_}: stale claim on {basename} (session {held_sid or '?'}, "
            f"PID {held_pid or '?'} not live) — taking over",
            file=sys.stderr,
        )
    shutil.rmtree(claim_dir, ignore_errors=True)
    try:
        os.mkdir(claim_dir)
    except OSError:
        print(
            f"cs_claim_{class_}: failed to create claim dir for {basename} "
            f"after stale takeover",
            file=sys.stderr,
        )
        return False
    _write_claim_meta(claim_dir, sid, stage)
    return True


def touch_brief_claim(
    class_: str,
    basename: str,
    baton_repo_root: str = "",
    cwd: Optional[str] = None,
) -> bool:
    """Refresh the lease on a ``brief``-stage claim THIS session holds, by
    rewriting ``claimed_at`` to now. Returns True when the lease was
    refreshed, False on every other path.

    This is what makes the lease measure "time since the holder last looked at
    this artifact" rather than "time since the holder first briefed it". A
    re-brief is evidence of active work, so a session still circling an
    artifact never loses it to the lease no matter how long the whole
    brief-to-apply window runs.

    Scoped to ``brief`` stage and to a self-held claim: an ``apply``-stage
    claim has no lease to refresh, and refreshing a peer's ``claimed_at``
    would extend a reservation this session has no business extending.
    """
    if not class_:
        raise ValueError("artifact class required")
    if not basename:
        raise ValueError("basename required")

    sid = core.resolve_session_id(cwd)
    if not sid:
        return False
    base = _claim_base(class_, baton_repo_root, cwd)
    if base is None:
        return False

    claim_dir = Path(base) / f"{class_}-claims" / basename
    if not claim_dir.is_dir():
        return False
    if not liveness.claim_held_by_me(str(claim_dir), sid, cwd):
        return False
    if claim_stage(claim_dir) != CLAIM_STAGE_BRIEF:
        return False
    try:
        (claim_dir / "claimed_at").write_text(f"{core.now_iso()}\n", encoding="utf-8")
    except OSError:
        return False
    return True


def promote_claim_stage(
    class_: str,
    basename: str,
    baton_repo_root: str = "",
    cwd: Optional[str] = None,
) -> bool:
    """Promote a claim THIS session holds from ``brief`` stage to ``apply``
    stage — the moment the pre-work reservation becomes a durable claim.

    Returns True when the stage file now reads ``apply`` for a claim this
    session holds, False on every other path (no claim dir, held by someone
    else, unresolvable session id or base, write failure). A no-op success for
    a claim already at ``apply`` stage, so ``apply`` may call it
    unconditionally.

    Deliberately NOT a second ``claim_artifact`` call: handoff and memo REJECT
    a same-session re-claim by design (module negative-spec), so the only
    correct way to change the stage of a claim already held is to rewrite the
    one file. ``claimed_at`` is left alone — it records when this session took
    the lock, which is still true, and the dead-holder settling window that
    reads it is safe to have measure from the earlier instant.

    Never touches a claim held by a DIFFERENT session, live or dead: promoting
    someone else's reservation would hand them a lease-free claim they never
    asked for.
    """
    if not class_:
        raise ValueError("artifact class required")
    if not basename:
        raise ValueError("basename required")

    sid = core.resolve_session_id(cwd)
    if not sid:
        return False
    base = _claim_base(class_, baton_repo_root, cwd)
    if base is None:
        return False

    claim_dir = Path(base) / f"{class_}-claims" / basename
    if not claim_dir.is_dir():
        return False
    if not liveness.claim_held_by_me(str(claim_dir), sid, cwd):
        return False
    if claim_stage(claim_dir) == CLAIM_STAGE_APPLY:
        return True
    try:
        (claim_dir / "stage").write_text(f"{CLAIM_STAGE_APPLY}\n", encoding="utf-8")
    except OSError:
        return False
    return True


def mark_claim_stamped(claim_dir: Union[str, Path]) -> bool:
    """Record that the frontmatter stamp backing this claim actually LANDED —
    a durable ``stamped`` marker file, written ONLY after the caller confirms
    a genuinely successful mutation (e.g. ``archive_stamp.cs_claim_handoff``
    returning ok on its OWN post-write ``_validate_fm`` pass), never before.

    WHY THIS EXISTS (cross-repo/inbox/2026-08-13-doe-claude-em-pickup-
    already-satisfied-masks-a-refused-write.md): ``claim_stage`` reads
    ``apply`` the moment ``promote_claim_stage`` runs, which ``pickup_
    assemble.apply.apply`` does UNCONDITIONALLY, BEFORE the directives that
    might actually perform the frontmatter stamp execute. A directive whose
    stamp attempt then fails (e.g. a schema-violating frontmatter write
    refused loud by ``handoff_transition._claim``) leaves the claim dir at
    ``apply`` stage with NO stamp ever landed — so ``stage == apply`` is
    reachable state on both a landed AND a refused write and cannot answer
    "did the stamp land" by itself. This marker is the fact that can: it is
    written from the ONE call site that has already confirmed success, not
    inferred from a stage transition that fires unconditionally pre-attempt.

    Best-effort, mirroring ``promote_claim_stage``'s own bool-return
    contract: a write failure here must never fail a caller that has already
    done the real, successful work — returns False rather than raising, and
    the caller (``apply.py``'s ``_dispatch_archive_stamp_cli``) treats this as
    advisory, not directive-failing.

    Returns True when the marker is present (freshly written, or already
    there — idempotent), False on any OSError or on a missing claim dir.
    """
    claim_dir = Path(claim_dir)
    if not claim_dir.is_dir():
        return False
    try:
        (claim_dir / "stamped").write_text(f"{core.now_iso()}\n", encoding="utf-8")
    except OSError:
        return False
    return True


def claim_stamped(claim_dir: Union[str, Path]) -> bool:
    """True iff ``claim_dir`` carries the ``stamped`` marker ``mark_claim_
    stamped`` writes — i.e. a frontmatter stamp for this claim has been
    CONFIRMED to land, as distinct from ``claim_stage(claim_dir) ==
    CLAIM_STAGE_APPLY``, which is true from the moment the claim is promoted
    and says nothing about whether the stamp attempt that followed actually
    succeeded (see ``mark_claim_stamped``'s docstring for the incident this
    distinction closes).

    False for a missing claim dir, a missing marker file (including every
    claim dir written before this marker existed — back-compat is safe by
    construction here: ``d2`` simply re-emits as unsatisfied on such a claim,
    and ``handoff_transition._claim`` is idempotent at the full target state,
    so a redundant re-stamp is a no-op success, not a duplicate mutation),
    or an unreadable marker file.
    """
    return (Path(claim_dir) / "stamped").is_file()


class ClaimRelocationError(OSError):
    """Raised by ``relocate_artifact_claim`` when the physical directory move
    itself fails (permission denial, cross-volume ``EXDEV``, transient FS
    error) — distinct from a ``False`` return, which is reserved for a
    genuine destination-collision refusal (someone else already holds
    ``new_basename``) or an unresolvable claim base. A caller catching only
    the bool could not previously tell "someone else holds it" (retry is
    wrong; resolve the collision first) apart from "the move broke" (retry
    might just work) — this exception restores that distinction.

    Review: code-reviewer — the two failure modes were both a bare `False`
    return; separated so callers can discriminate collision from OS error.
    """


def relocate_artifact_claim(
    class_: str,
    old_basename: str,
    new_basename: str,
    baton_repo_root: str = "",
    cwd: Optional[str] = None,
) -> bool:
    """THE sanctioned entrypoint for renaming a claimed artifact (handoff /
    memo / plan) on disk while it may be claimed — relocates the claim dir
    alongside the basename change, so the shared-path mkdir lock (module
    negative-spec, "BASENAME-ONLY, NEVER sid-namespaced") keeps tracking the
    SAME logical claim across a rename instead of orphaning under the
    vanished old name.

    THE DEFECT THIS CLOSES (state/bug-backlog/2026-08-10-two-sessions-held-
    one-baton-the-claim-di-1d9d62d1d8af.yaml): claim identity is basename-
    only and nothing relocated a claim dir when a handoff was renamed via a
    bare ``git mv`` — a peer then claimed the NEW basename cleanly (no claim
    dir sat there) while the original holder's claim sat live, invisible,
    under the OLD basename. Two sessions worked the same baton concurrently,
    and the fleet archival sweep's Check 4
    (``coordinator_core.ops.fleet.archive_handoffs._is_terminal``) also
    derives its liveness check from the artifact's CURRENT basename via
    ``coordinator_core.claim_state.handoff_claim_dir`` — so a claim orphaned
    by rename is invisible to it too, and archival proceeds unopposed against
    a live holder.

    Modeled on the two existing rename-plus-claim precedents named in the
    backlog's proposed_action — ``percolate/rewrite_basename.py``'s
    ``_do_rename`` (claim-aware rename, never silently drops a claim) and
    ``session/scope.py``'s ``relocate_touched_path`` (only act when
    something is actually claimed) — but structurally simpler than both:
    those two operate on a TOUCH-CLAIM append-only event log, where
    "relocating" means appending a new event. A claim dir here is a physical
    ``mkdir`` lock — one directory, holding pid/session_id/claimed_at/stage
    files — so relocating it is a physical directory move (``os.replace``,
    atomic on both POSIX and Windows NTFS when source and destination share
    a filesystem, which they always do here: both live under the SAME
    ``<base>/<class>-claims/`` parent), never an appended record.

    THIS FUNCTION DOES NOT RENAME THE ARTIFACT FILE ITSELF — callers (a
    ``git mv``, or any future rename tool built on top of this) perform the
    file move themselves and call this alongside it. Claim-dir identity and
    file identity are two independent filesystem objects with no cross-object
    atomicity available between them regardless of ordering, so callers may
    invoke this before or after the file rename; there is no unsafe ordering
    to warn about, only "call it, or the claim orphans."

    Returns:
      - True, no-op, when NO claim dir exists at ``old_basename`` — the
        common case (most renamed artifacts are unclaimed at rename time);
        nothing to relocate.
      - True on a successful relocation.
      - False when a DIFFERENT claim dir already exists at ``new_basename``
        — refuses to silently clobber or merge two claims; a destination
        collision is a genuine two-holder conflict the caller must resolve
        (e.g. via ``clear_claim_if_dead``) before renaming, not something
        this function may paper over.
      - False on an unresolvable claim base (bad baton root, not a git repo).
      - True (no-op) when ``old_basename == new_basename`` — nothing to do.

    Raises:
      - ``ClaimRelocationError`` (an ``OSError`` subclass) when the physical
        ``os.replace`` of the claim directory itself fails — permission
        denial, cross-volume ``EXDEV``, a transient FS error. Kept distinct
        from the ``False`` collision return: a collision needs the caller to
        resolve a two-holder conflict before renaming, while this needs
        either a retry or a surfaced failure — conflating the two under one
        falsy return left a caller unable to tell them apart.

    NEGATIVE-SPEC — the already-orphaned case (see backlog item's "already-
    orphaned case" discussion). This function does NOT scan for, detect, or
    adopt a PRE-EXISTING orphan left by a rename that happened before this
    entrypoint existed (e.g. the real incident's own orphaned
    ``handoff-claims/2026-08-10-untitled.md`` claim dir, still on disk at the
    time this was written). It only relocates a claim at the moment of ITS
    OWN invocation, for the rename ITS OWN caller is performing. Adopting a
    stale orphan would require matching an old basename to a current one by
    something other than an exact rename operation just performed (predecessor
    chain inspection, timing heuristics, or an operator's own judgment) — a
    materially different, riskier operation belonging to a separate
    reconciliation sweep, not this narrow rename-time relocator. Do NOT widen
    this function to do that matching.

    ``class_`` / ``old_basename`` / ``new_basename`` REQUIRED — empty raises
    ValueError, mirroring every other claim-dir primitive in this module.

    Spec backlink: state/bug-backlog/2026-08-10-two-sessions-held-one-baton-
    the-claim-di-1d9d62d1d8af.yaml proposed_action.
    """
    if not class_:
        raise ValueError("artifact class required")
    if not old_basename:
        raise ValueError("old_basename required")
    if not new_basename:
        raise ValueError("new_basename required")

    if old_basename == new_basename:
        return True  # nothing to relocate

    base = _claim_base(class_, baton_repo_root, cwd)
    if base is None:
        return False

    claims_dir = Path(base) / f"{class_}-claims"
    old_claim_dir = claims_dir / old_basename
    new_claim_dir = claims_dir / new_basename

    if not old_claim_dir.is_dir():
        return True  # nothing claimed at the old name — nothing to relocate

    # Review: code-reviewer — TOCTOU window, accepted and named rather than
    # closed. Between this `exists()` check and the `os.replace` below, a
    # peer's `claim_artifact` could create a claim dir at `new_basename`; on
    # POSIX, `os.rename`/`os.replace` onto an existing EMPTY directory
    # succeeds silently, so the peer's brand-new claim would be silently
    # clobbered rather than refused. On Windows NTFS (this repo's first-class
    # platform) `os.replace` raises `FileExistsError`/`PermissionError` when
    # the destination directory already exists — `MoveFileExW` will not
    # replace an existing directory — so the peer's claim wins there: the
    # `except OSError` branch below fires and this call now raises
    # `ClaimRelocationError` instead of silently winning the race. The
    # platforms diverge in outcome (Windows: safe, raises; POSIX: unsafe,
    # silent clobber) rather than diverging in whether the window exists —
    # closing it fully would need an atomic "create-if-absent, else fail"
    # primitive this claim dir's plain-`mkdir` shape does not have. Narrow
    # window in practice: claim-dir creation is itself rare and
    # basename-targeted.
    if new_claim_dir.exists():
        print(
            f"cs_relocate_claim: refusing to relocate {old_basename!r} -> "
            f"{new_basename!r} — a claim already exists at the destination; "
            "resolve the collision (e.g. clear_claim_if_dead) before renaming",
            file=sys.stderr,
        )
        return False

    try:
        claims_dir.mkdir(parents=True, exist_ok=True)
        os.replace(old_claim_dir, new_claim_dir)
    except OSError as exc:
        print(
            f"cs_relocate_claim: failed to relocate claim {old_basename!r} -> "
            f"{new_basename!r}: {exc}",
            file=sys.stderr,
        )
        raise ClaimRelocationError(
            f"failed to relocate claim {old_basename!r} -> {new_basename!r}: {exc}"
        ) from exc
    return True


def relocate_handoff_claim(
    old_basename: str,
    new_basename: str,
    baton_repo_root: str = "",
    cwd: Optional[str] = None,
) -> bool:
    """Thin class-bound wrapper over ``relocate_artifact_claim("handoff",
    ...)`` — the parity addition matching ``claim_handoff``/``claim_memo``'s
    existing class-bound-wrapper convention. The one intended caller shape
    for the incident this closes: whatever performs a handoff rename (a
    ``git mv``, or a future dedicated rename tool) alongside the file move."""
    return relocate_artifact_claim("handoff", old_basename, new_basename, baton_repo_root, cwd=cwd)


def _read_claim_field(claim_dir: Path, field: str) -> str:
    """Read a single-line claim metadata file (``pid`` / ``session_id``),
    stripped, or ``""`` if absent/unreadable (mirrors ``cat ... || echo ""``)."""
    try:
        return (claim_dir / field).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def claim_handoff(
    basename: str, baton_repo_root: str = "", cwd: Optional[str] = None
) -> bool:
    """Port of ``cs_claim_handoff <basename> [baton_repo_root]`` (970). Thin
    class-bound wrapper — a byte-for-byte 2-arg passthrough to
    ``claim_artifact("handoff", ...)`` preserving the existing call-site
    contract."""
    return claim_artifact("handoff", basename, baton_repo_root, cwd=cwd)


def claim_memo(
    basename: str, baton_repo_root: str = "", cwd: Optional[str] = None
) -> bool:
    """Port of ``cs_claim_memo <basename> [baton_repo_root]`` (971). Thin
    class-bound wrapper over ``claim_artifact("memo", ...)`` — the parity
    addition for memo-pickup (pickup SKILL.md Memo Branch M2.5)."""
    return claim_artifact("memo", basename, baton_repo_root, cwd=cwd)


def _read_scope_mode(plan_file: Path) -> str:
    """Read the ``scope_mode:`` frontmatter value from a plan file (first match),
    stripped of surrounding whitespace and ALL quote chars (mirrors the bash
    ``grep '^scope_mode:' | head -1 | sed ... | tr -d '"' | tr -d "'"``).
    Returns ``""`` on absence/read failure."""
    try:
        text = plan_file.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("scope_mode:"):
            val = line[len("scope_mode:"):].strip()
            return val.replace('"', "").replace("'", "")
    return ""


def claim_plan(slug: str, cwd: Optional[str] = None) -> bool:
    """Port of ``cs_claim_plan <basename>`` (980-1011).

    ONE-arg wrapper (the ``baton_repo_root`` arg is DELIBERATELY DROPPED —
    plan execution is always in-repo, and passing it through would silently
    absorb a stray extra arg and write the lock into the wrong repo). The
    cwd-default base guarantees an identical claim base across the execute-plan
    and workstream-complete seams.

    On a successful claim, best-effort records ``plan.path`` +
    ``plan.scope_mode`` (the write-time fact from plan frontmatter) into
    ``session-shape.json`` via ``shape.session_shape_set`` (C3 instrumentation).
    NON-FATAL: a shape-write failure must NOT break the plan claim — the True
    return is already committed by the claim success. ``light|full`` sizing is
    a deferred read-time ceremony derivation, NOT stored here (plan F4 fix).

    Returns the claim's success verdict (False if the underlying
    ``claim_artifact`` failed — the shape write is skipped entirely then).

    ``slug`` MUST be a bare basename (e.g. ``2026-07-26-some-plan``), NEVER a
    path. A path-shaped arg (containing ``/`` or ``\\``, or ending in
    ``.md``) is REJECTED here, loud and non-zero, before it ever reaches
    ``claim_artifact``'s ``os.mkdir`` — do NOT let it fall through to mkdir.
    That fall-through is a real hazard, not a theoretical one: ``os.mkdir``
    only requires its OWN leaf component be absent, so a path-shaped basename
    whose intermediate directories HAPPEN to already exist (left behind by an
    earlier failed path-shaped call, a concurrent session, or any other
    coincidence) succeeds SILENTLY — no stderr, ``True`` returned — while
    creating a claim nested under a bogus ``<...>/plan-claims/docs/plans/
    <file>.md`` subtree that protects nothing the real basename-keyed slug
    depends on. Two sessions can then both believe they hold the real plan's
    claim. Rejecting the shape here forecloses the whole class rather than
    hardening ``claim_artifact``'s mkdir fallback, which cannot distinguish
    "intermediate dirs coincidentally pre-exist" from "genuine EEXIST on the
    leaf" after the fact.

    Deliberately REJECT, not normalize-to-basename: this is the module's one
    plan-claiming entry point (also the ``session-claim-cli claim-plan``
    subcommand's target), and a caller passing a full path is itself a bug at
    the call site (see the ``/handoff`` d5-directive emitter fix, same
    workstream) — silently accepting and stripping it would launder that bug
    forward indefinitely instead of surfacing it where it can be fixed once.

    Spec backlink: pln-ceremony-as-pipeline-v1-session-state-co-596280 § C3
    """
    if "/" in slug or "\\" in slug or slug.endswith(".md"):
        print(
            f"cs_claim_plan: expected a bare plan slug (basename, no path "
            f"separator, no .md suffix) — got {slug!r}; pass the plan's "
            f"slug (e.g. 2026-07-26-some-plan), not its path",
            file=sys.stderr,
        )
        return False

    if not slug or slug.startswith("-"):
        print(
            f"cs_claim_plan: expected a bare plan slug (basename, no path "
            f"separator, no .md suffix) — got {slug!r}; pass the plan's "
            f"slug (e.g. 2026-07-26-some-plan), not its path",
            file=sys.stderr,
        )
        return False

    if not claim_artifact("plan", slug, cwd=cwd):
        return False

    # C3 — best-effort session-shape instrumentation (non-fatal).
    sid = core.resolve_session_id(cwd)
    if sid:
        rel = f"docs/plans/{slug}.md"
        scope = ""
        root = core.git_root(cwd)
        if root and (Path(root) / rel).is_file():
            scope = _read_scope_mode(Path(root) / rel)
        # A dict fragment lets session_shape_set / json.dump handle escaping —
        # no hand-rolled JSON string-escaping needed (bash escaped inline).
        fragment = {"plan": {"path": rel, "scope_mode": scope if scope else None}}
        try:
            shape.session_shape_set(sid, fragment, cwd)
        except Exception as exc:
            # non-fatal — plan claim already succeeded; surface the failure
            # for debugging without blocking the caller.
            print(
                f"cs_claim_plan: session-shape write failed for {slug} "
                f"(non-fatal): {exc}",
                file=sys.stderr,
            )
    return True


# ---------------------------------------------------------------------------
# Release + clear
# ---------------------------------------------------------------------------

#: The generic class selecting the PATH-TOUCH claim plane
#: (``coordinator_core.session.claim_index`` / each claimant's ``touched.txt``
#: ``T``/``R`` event log) instead of the mkdir-based ARTIFACT-CLAIM RECORD
#: STORE (``<class>-claims/<basename>/`` dirs) the ``handoff``/``memo``/
#: ``plan`` classes below manage. This is the plane ``who-claims-path``
#: answers over and ``ceremony.scoped_git_commit``'s commit gate
#: (``coordinator_core/ops/ceremony/scoped_git_commit.py::
#: _check_claim_conflicts``) fails closed on -- widened onto here per
#: cross-repo/inbox/2026-08-11-doe-claude-em-dead-claim-on-a-non-plan-
#: artifact-has-no-clear-path.md: a dead session's claim on an arbitrary
#: repo-relative path (e.g. a doctrine/code file the three classed forms
#: were never meant to cover) had a query surface (``who-claims-path``) and
#: a consuming gate (``scoped_git_commit``) but no release path. ``basename``
#: under this class is a repo-relative PATH, not a claim-store basename.
ARTIFACT_CLASS_PATH = "artifact"


def _release_path_claim_everywhere(
    path: str, sids: set, base: str, cwd: Optional[str] = None
) -> None:
    """Physically release *path*'s touch-claim (``T`` event) from every
    claimant in *sids*'s own ``touched.txt`` AND every agent ``touched.txt``
    back-pointed to one of them.

    The write-side counterpart to ``coordinator_core.session.claim_index``'s
    read-only reverse index -- that module's own docstring ("NO ``record()``
    -- rebuild-only by design") is why the write lives HERE, in claims.py
    (the module that already owns release/clear semantics), rather than
    there. Shared by ``_release_path_claim_artifact`` (self-release,
    identity-checked -- the ``release_artifact`` counterpart) and
    ``_clear_path_claim_if_dead`` (dead-holder release, liveness-checked --
    the ``clear_claim_if_dead`` counterpart): the two callers differ only in
    HOW *sids* was decided, never in how the write is performed.

    For each ``(touched_path, claimant_sid)`` pair ``claim_index.
    _enumerate_touched_files`` reaches whose claimant is in *sids*: re-scans
    that ONE file's own events for *path* (via ``scope.parse_touch_event``,
    normalized the same way ``claim_index._normalize_key`` normalizes, so a
    backslashed caller path matches a forward-slashed on-disk entry) and
    appends an ``R`` event ONLY if that file's LAST recorded event for
    *path* is currently ``T`` -- mirrors ``scope._release_from_touched_
    file``'s own T-check, just keyed on one caller-supplied path instead of
    a git-clean set. Append-only, one ``write()`` call per file (PIPE_BUF
    discipline, see ``atomic_dedup_append``'s docstring).

    Fail-safe RETAIN per file: an unreadable/unwritable ``touched.txt`` just
    skips that file's release rather than raising -- a partial release
    (some claimant files updated, others not) is always safe here because
    every remaining ``T`` still shows up on the next ``who-claims-path`` /
    commit-gate lookup rather than being silently lost.
    """
    if not sids:
        return
    normalized_target = claim_index._normalize_key(path)
    pairs, _complete = claim_index._enumerate_touched_files(base)
    for touched_path, claimant_sid in pairs:
        if claimant_sid not in sids:
            continue
        try:
            lines = Path(touched_path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        last_raw: Optional[str] = None
        last_verb: Optional[str] = None
        for line in lines:
            verb, _ts, raw_path = scope.parse_touch_event(line)
            if claim_index._normalize_key(raw_path) == normalized_target:
                last_verb = verb
                last_raw = raw_path
        if last_verb != "T" or last_raw is None:
            continue
        try:
            with open(touched_path, "a", encoding="utf-8") as fh:
                fh.write(scope.format_touch_event("R", last_raw) + "\n")
        except OSError:
            continue


def _resolve_path_claim_base(
    baton_repo_root: str, cwd: Optional[str]
) -> Tuple[Optional[str], bool]:
    """Resolve the ``<...>/coordinator-sessions`` base for a PATH-TOUCH claim
    op -- the ``artifact``-class sibling of ``_claim_base`` (that helper's
    ``<class>-claims`` subdir has no meaning on this plane, so this is a
    separate, narrower resolver rather than a widened ``_claim_base``).

    Returns ``(base, ok)``. ``ok`` is False only for a SUPPLIED-but-bad baton
    root (fail loud, mirroring ``_claim_base``); an absent (non-baton) base
    resolves to ``(None, True)`` -- "no sessions dir -> no claim can exist"
    is the caller's idempotent-success case, not a failure.
    """
    if baton_repo_root:
        if not (Path(baton_repo_root) / ".git").is_dir():
            return None, False
        return str(Path(baton_repo_root) / ".git" / "coordinator-sessions"), True
    base = core.sessions_dir(cwd)
    return base, True


def _release_path_claim_artifact(
    path: str, baton_repo_root: str = "", cwd: Optional[str] = None
) -> bool:
    """``class_ == "artifact"`` entrypoint for ``release_artifact`` --
    self-release of THIS session's own touch-claim on *path* (the PATH-TOUCH
    plane), mirroring ``release_artifact``'s existing identity-checked
    contract for the three classed forms: releases only what THIS session
    (or its own dispatched-agent fan-out) holds, never a peer's claim,
    liveness never enters into it. Unlike ``release_committed_claims`` this
    is NOT gated on the path being git-clean -- this is an explicit release
    of one named path, not a post-commit sweep.

    Always returns True (mirrors ``release_artifact``'s own "no-op paths are
    successes, not errors" contract): a bad baton root, unresolvable
    session id, or absent sessions dir is an idempotent no-op, same as the
    classed forms.
    """
    base, ok = _resolve_path_claim_base(baton_repo_root, cwd)
    if not ok or not base:
        return True

    my_sid = core.resolve_session_id(cwd)
    if not my_sid:
        return True

    _release_path_claim_everywhere(path, {my_sid}, base, cwd)
    return True


def _clear_path_claim_if_dead(
    path: str, baton_repo_root: str = "", cwd: Optional[str] = None
) -> bool:
    """``class_ == "artifact"`` entrypoint for ``clear_claim_if_dead`` -- the
    CLEAR-ONLY, liveness-gated counterpart to ``_release_path_claim_
    artifact`` for the PATH-TOUCH claim plane. This is the release path
    cross-repo/inbox/2026-08-11-doe-claude-em-dead-claim-on-a-non-plan-
    artifact-has-no-clear-path.md asks for: a claim ``who-claims-path``
    reports and ``ceremony.scoped_git_commit`` fails closed on, whose
    claimant is confirmed dead, had no sanctioned release path before this.

    Fail-closed exactly like the classed forms: ANY live claimant on *path*
    refuses the WHOLE clear (never a partial release) -- a path with two
    claimants, one dead and one live, stays claimed by the live one for the
    same reason ``scoped_git_commit`` already refuses it, so partially
    clearing it would accomplish nothing. An UNANSWERABLE claim-index
    verdict also refuses (never treated as "no claimant"). A DOUBLE
    ``claim_index.lookup`` + liveness re-read brackets the write (matches
    ``clear_claim_if_dead``'s own TOCTOU discipline for the mkdir plane): if
    a peer takes a fresh live claim on *path* between reads, the second read
    sees it and the whole clear aborts.

    Returns True on a successful clear OR an idempotent no-op (no claimant,
    absent sessions dir, bad baton root already ruled out by the caller).
    Returns False on an UNANSWERABLE index, a live claimant, or a claimant
    that became live on the TOCTOU re-read.
    """
    base, ok = _resolve_path_claim_base(baton_repo_root, cwd)
    if not ok:
        print(
            f"cs_clear_claim_if_dead: baton repo root <{baton_repo_root}> "
            f"is not a git repo",
            file=sys.stderr,
        )
        return False
    if not base:
        return True  # no sessions dir -> no claim can exist -> idempotent

    def _claimants() -> List[str]:
        return claim_index.lookup([path], sessions_dir=base, cwd=cwd).get(path, [])

    def _live_ones(sids: List[str]) -> List[str]:
        return [sid for sid in sids if liveness.session_live(sid, cwd)]

    claimants = _claimants()
    if claim_index.UNANSWERABLE in claimants:
        print(
            f"cs_clear_claim_if_dead: claim ownership for {path!r} could not "
            f"be verified (claim index unanswerable) -- refusing to clear",
            file=sys.stderr,
        )
        return False
    if not claimants:
        return True  # nothing claims this path -- idempotent no-op

    live = _live_ones(claimants)
    if live:
        print(
            f"cs_clear_claim_if_dead: refusing to clear path claim {path!r} "
            f"-- live claimant(s) {', '.join(sorted(live))}",
            file=sys.stderr,
        )
        return False

    # TOCTOU re-read — bracket the write, mirroring the mkdir-plane's own
    # double claim_holder_live read around its rm.
    claimants2 = _claimants()
    if claim_index.UNANSWERABLE in claimants2:
        print(
            f"cs_clear_claim_if_dead: aborting clear of path claim {path!r} "
            f"-- claim index became unanswerable on re-read",
            file=sys.stderr,
        )
        return False
    live2 = _live_ones(claimants2)
    if live2:
        print(
            f"cs_clear_claim_if_dead: aborting clear of path claim {path!r} "
            f"-- claimant became live after TOCTOU re-read "
            f"({', '.join(sorted(live2))})",
            file=sys.stderr,
        )
        return False

    dead_sids = set(claimants) | set(claimants2)
    _release_path_claim_everywhere(path, dead_sids, base, cwd)
    return True


def release_artifact(
    class_: str,
    basename: str,
    baton_repo_root: str = "",
    cwd: Optional[str] = None,
) -> bool:
    """Port of ``cs_release_artifact <class> <basename> [baton_repo_root]`` (1030-1062).

    Explicit, holder-identity-checked release of a claim. Unlike handoffs
    (dead-PID reaping only), memo-pickup reaches meaningful NON-TERMINAL
    dispositions (Decline, Surface-to-PM) while still alive — those must
    release the claim so a legitimate re-pickup is not blocked until the PID
    dies.

    SAFETY: releases only if THIS session is the recorded holder
    (``liveness.claim_held_by_me`` — keyed on session_id == my id, NEVER $$).
    Not the holder, or the claim already absent, or a bad/absent baton root ->
    NO-OP success. A bare rm without this check would race an inline dead-PID
    takeover and could delete a live peer's claim.

    ``my_sid`` is resolved ONCE and passed to BOTH ``claim_held_by_me`` reads
    (called TWICE — intentional TOCTOU re-read): the second read then varies
    only on the claim-dir CONTENT (the actual race), not on a re-resolution of
    my own id. If an inline takeover (rm + mkdir + new session_id) slipped in
    after the first check, the recheck no longer matches our id and we skip —
    never delete a live peer's claim.

    ORDERING CONTRACT (enforced by the CALLER, not here): the caller MUST
    revert the artifact's frontmatter (status in_progress -> open, clear stamps)
    BEFORE calling this. A crash between the two steps then leaves a recoverable
    "open but claim-held" state; the reverse (claim freed, status still
    in_progress) would re-admit two sessions.

    ``class_ == "artifact"`` (``ARTIFACT_CLASS_PATH``) is a FOURTH, additive
    form: ``basename`` is then a repo-relative PATH into the PATH-TOUCH claim
    plane (``coordinator_core.session.claim_index`` / ``touched.txt``), not a
    basename in the mkdir-based artifact-claim record store the three classed
    forms above manage — routes to ``_release_path_claim_artifact`` before any
    of the ``<class>-claims`` lookup below runs. See ``ARTIFACT_CLASS_PATH``'s
    own docstring for why this plane needed widening onto here.

    ALWAYS returns True (bash ``return 0`` on every path — the no-op paths are
    successes, not errors).

    ``class_`` / ``basename`` REQUIRED — empty raises ValueError.

    Spec backlink: docs/plans/2026-06-21-memo-pickup-claim-lock-and-routed-plan-reconcile.md § C1
    """
    if not class_:
        raise ValueError("artifact class required")
    if not basename:
        raise ValueError("basename required")

    if class_ == ARTIFACT_CLASS_PATH:
        return _release_path_claim_artifact(basename, baton_repo_root, cwd)

    if baton_repo_root:
        if not (Path(baton_repo_root) / ".git").is_dir():
            return True  # bad baton root -> no-op success
        base = str(Path(baton_repo_root) / ".git" / "coordinator-sessions")
    else:
        base = core.sessions_dir(cwd)
        if not base:
            return True  # not in a git repo -> no-op success

    claim_dir = Path(base) / f"{class_}-claims" / basename
    if not claim_dir.is_dir():
        return True  # already absent — no-op

    # Resolve my id ONCE; pass it to both TOCTOU reads (F1).
    my_sid = core.resolve_session_id(cwd)
    if not liveness.claim_held_by_me(str(claim_dir), my_sid, cwd):
        return True  # not the holder — no-op
    # TOCTOU re-read before rm (the second call IS the two-read discipline).
    if not liveness.claim_held_by_me(str(claim_dir), my_sid, cwd):
        return True  # takeover slipped in — skip; never delete a live peer's claim

    shutil.rmtree(claim_dir, ignore_errors=True)
    if class_ == "plan":
        _clear_shape_plan_pointer(basename, my_sid, cwd)
    return True


def _clear_shape_plan_pointer(
    slug: str,
    sid: Optional[str],
    cwd: Optional[str] = None,
) -> None:
    """Drop ``session-shape.json``'s ``plan`` block when it still names ``slug``.

    The mirror-side half of ``claim_plan``'s C3 instrumentation: that function
    writes ``plan.path`` into the shape file at claim time, and until this
    existed nothing unwrote it. A released plan therefore stayed resolvable
    through ``claimed_plan.resolve_claimed_plan_path``'s tier (a), which reads
    the shape pointer BEFORE the durable ``plan-claims/`` store and returns on
    a hit — so ``/handoff`` after a shipped plan resolved the shipped plan and
    surfaced a ``DivergentDeliverableIdError`` against the handoff chain's own
    ``deliverable_id`` (doe-claude-em memo, 2026-08-10; the two ids differing
    is the EXPECTED steady state for a chain spanning several plans, so the
    stale pointer made a routine seam fail loud).

    Writes ``{"plan": {}}``, never ``{"plan": None}``: ``ceremony.
    session_instructions``'s ``setdefault("plan", {})`` returns the existing
    None on an explicit null and then subscripts it, so a null clear trades
    this bug for a TypeError on the scope-override path.

    Best-effort and silent by design — the mirror of ``claim_plan``'s
    non-fatal shape write. The claim removal has already succeeded and is the
    durable fact; a shape-write failure must not turn a successful release
    into a reported failure. ``resolve_claimed_plan_path``'s tier-(a)
    validation against the claim store is the read-side backstop that also
    covers a shape file left by a session that died mid-plan.
    """
    if not sid:
        return
    try:
        raw = shape.session_shape_read(sid, cwd)
        parsed = json.loads(raw) if raw.strip() else None
    except (ValueError, OSError):
        return
    if not isinstance(parsed, dict):
        return
    plan_block = parsed.get("plan")
    if not isinstance(plan_block, dict):
        return
    if plan_block.get("path") != f"docs/plans/{slug}.md":
        return
    try:
        shape.session_shape_set(sid, {"plan": {}}, cwd)
    except Exception as exc:
        print(
            f"cs_release_artifact: session-shape plan-pointer clear failed for "
            f"{slug} (non-fatal): {exc}",
            file=sys.stderr,
        )


def clear_claim_if_dead(
    class_: str,
    basename: str,
    baton_repo_root: str = "",
    cwd: Optional[str] = None,
) -> bool:
    """Port of ``cs_clear_claim_if_dead <class> <basename> [baton_repo_root]`` (1097-1157).

    Single-named-claim sibling of ``cs_reap_stale_claims`` — CLEAR-ONLY (does
    NOT re-claim). The manual EM-driven claim-clear / takeover path: forces the
    caller through a liveness gate before any ``rm -rf``. The claim is removed
    only when the holder is DEAD; a live holder returns False with a stderr
    message naming the holder. Clear and re-claim are SEPARATE operations — do
    NOT bundle them.

    Returns:
      - True on a successful clear OR an idempotent no-op (absent claim /
        absent sessions dir).
      - False on an invalid class, a bad/absent baton root, or a refusal
        because the holder is (or became) live.

    Class validation: restrict to ``{handoff, memo, plan, artifact}`` (invalid
    -> False + stderr). Empty ``class_`` / ``basename`` raise ValueError first
    (bash ``${1:?}`` / ``${2:?}``).

    ``class_ == "artifact"`` (``ARTIFACT_CLASS_PATH``) routes to
    ``_clear_path_claim_if_dead`` BEFORE class validation runs — a FOURTH,
    additive form widening this function onto the PATH-TOUCH claim plane
    (``coordinator_core.session.claim_index`` / ``touched.txt``) that
    ``who-claims-path`` answers over and ``ceremony.scoped_git_commit``
    fails closed on, distinct from the three classed forms' mkdir-based
    artifact-claim record store. ``basename`` under this class is a
    repo-relative PATH, not a claim-store basename. See
    ``ARTIFACT_CLASS_PATH``'s own docstring for the incident this closes.

    Base resolution: a SUPPLIED-but-bad baton root FAILS LOUD (False + stderr,
    mirroring ``claim_artifact`` — this function is EM-callable and must
    surface errors, NOT the reaper's silent skip). An ABSENT sessions dir (no
    baton) is an idempotent success (no claim can exist).

    LIVENESS: delegates ENTIRELY to ``liveness.claim_holder_live`` — the
    canonical predicate. NEVER ``ps -p`` / ``kill -0`` on a stored pid
    directly (RAW-PID-LIVENESS floor). DOUBLE ``claim_holder_live`` (read 1 +
    TOCTOU read 2) brackets the rm: if a concurrent takeover lands between
    reads (rm + mkdir + new live session_id), read 2 sees the NEW live holder
    and aborts.

    LEGACY pid-only residual: a claim dir with no ``session_id`` file routes
    ``claim_holder_live`` to the ephemeral-pid test ("structurally always dead
    in-harness"), so a legacy pid-only claim whose session is LIVE is
    classified DEAD and cleared. A DISTINCT stderr note is emitted when
    clearing such a dir so the caller is not falsely reassured.

    Spec backlink: docs/plans/2026-06-30-claim-clear-liveness-gate.md § C2
    """
    if not class_:
        raise ValueError("class required")
    if not basename:
        raise ValueError("basename required")

    if class_ == ARTIFACT_CLASS_PATH:
        return _clear_path_claim_if_dead(basename, baton_repo_root, cwd)

    if class_ not in ("handoff", "memo", "plan"):
        print(
            f"cs_clear_claim_if_dead: invalid class '{class_}' "
            f"(must be handoff, memo, plan, or {ARTIFACT_CLASS_PATH})",
            file=sys.stderr,
        )
        return False

    if baton_repo_root:
        if not (Path(baton_repo_root) / ".git").is_dir():
            print(
                f"cs_clear_claim_if_dead: baton repo root <{baton_repo_root}> "
                f"is not a git repo",
                file=sys.stderr,
            )
            return False
        base = str(Path(baton_repo_root) / ".git" / "coordinator-sessions")
    else:
        base = core.sessions_dir(cwd)
        if not base:
            return True  # missing sessions dir -> no claim can exist -> idempotent

    claim_dir = Path(base) / f"{class_}-claims" / basename
    if not claim_dir.is_dir():
        return True  # idempotent on absent claim dir

    # Liveness gate — read 1: refuse if the holder is still live.
    if liveness.claim_holder_live(str(claim_dir), cwd):
        holder = _read_holder(claim_dir)
        print(
            f"cs_clear_claim_if_dead: refusing to clear claim '{basename}' — "
            f"holder is live (session: {holder})",
            file=sys.stderr,
        )
        return False

    # TOCTOU re-read — read 2.
    if liveness.claim_holder_live(str(claim_dir), cwd):
        holder = _read_holder(claim_dir)
        print(
            f"cs_clear_claim_if_dead: aborting clear of '{basename}' — "
            f"holder became live after TOCTOU re-read (session: {holder})",
            file=sys.stderr,
        )
        return False

    # Distinct note for a legacy pid-only claim dir (no session_id file).
    if not (claim_dir / "session_id").is_file():
        print(
            "note: cleared a legacy pid-only claim (no session_id) — "
            "liveness could not be session-verified",
            file=sys.stderr,
        )

    # Reap/ship bug fix 1 (docs/plans/2026-07-24-g4-execute-pipeline-two-repo-
    # rebuild.md § M3a): a handoff claim-lock clear must ALSO reconcile the
    # handoff's own frontmatter (status:claimed → open), or list_stale_claim_
    # handoffs keeps reporting it stale forever (it recomputes off claimed_by,
    # never off the lock-dir). unconsume-FIRST-then-rmtree — see
    # reconcile_dead_handoff_claim_frontmatter's docstring for the crash-
    # disposition rationale this ordering buys.
    if class_ == "handoff":
        reconcile_dead_handoff_claim_frontmatter(basename, Path(base))

    shutil.rmtree(claim_dir, ignore_errors=True)
    return True


def _handoff_has_named_successor(handoff_path: Path, worktree: Path) -> Optional[bool]:
    """Return True/False for "some other baton names this one as a predecessor",
    or ``None`` when the corpus could not be enumerated completely.

    Succession edges only: ``predecessor`` + ``additional_predecessors``, which is
    exactly the set ``baton_assemble``'s d6 fires a supersede for (one per
    predecessor, primary and fan-in alike). ``forked_from`` is deliberately
    excluded — d6 does not fire for a fork, so a fork's existence says nothing
    about whether its parent was continued.

    Scans live AND archived handoffs: the successor of a resolved baton is
    routinely already under ``archive/handoffs/``, which is the whole shape this
    predicate exists to detect. Reuses ``handoff_children._collect_handoff_paths``
    + ``dag.referenced_by`` — the same enumeration and the same single-hop
    reverse-edge primitive ``ops/baton_drift_sweep.py`` classifies STRANDED with,
    so the reaper and the diagnostic cannot drift apart on what "has a successor"
    means.

    ``None`` on ANY incompleteness (scan error or unexpected failure) so callers
    can fail closed: an unreadable subtree must never read as "no successor".
    """
    from coordinator_core.dag import referenced_by
    from coordinator_core.ops.handoff_children import _collect_handoff_paths

    try:
        paths, scan_errors = _collect_handoff_paths(worktree)
    except Exception:
        return None
    if scan_errors:
        return None
    try:
        result = referenced_by(
            str(handoff_path),
            paths,
            edge_kinds={"predecessor", "additional_predecessors"},
            handoff_dir=str(worktree / "state" / "handoffs"),
            exclude=[str(handoff_path)],
        )
    except Exception:
        return None
    return bool(result.get("referenced"))


def reconcile_dead_handoff_claim_frontmatter(basename: str, sessions_dir: Path) -> None:
    """Best-effort: flip a dead-claimed handoff's frontmatter back to open.

    Reuses the EXISTING ``unconsume`` verb (``coordinator_core.ops.handoff_
    transition._unclaim``) rather than hand-rolling a second frontmatter
    mutator. Shared by ``clear_claim_if_dead`` (this module, manual EM-driven
    clear) and ``coordinator_core.ops.session.reap._reap_orphaned_claims``
    (automated reaper) — both clear the same ``<sessions_dir>/handoff-claims/
    <basename>`` lock and both carried the identical frontmatter-drift bug
    (the Director of Engineering Finding 4/5).

    ORDERING (the Director of Engineering Finding 4 — "atomic" is an overclaim; rmtree and a
    frontmatter mutation are two distinct filesystem ops, never atomic across
    a crash): callers MUST invoke this BEFORE ``shutil.rmtree``-ing the claim
    dir, never after. Crash disposition: (a) crash after this call succeeds
    but before the caller's rmtree — frontmatter is open, lock dir still
    present; the (now-open) handoff re-enters the shelf and the stale lock
    dir is picked up by the next reap pass (self-healing). (b) crash before
    this call runs — no state change, re-run from scratch. Both degrade to
    "lock still present, re-reaped" — never today's silent drift (lock
    cleared, frontmatter still claimed). rmtree-first-then-unconsume is
    explicitly ruled out: a crash after rmtree reproduces exactly the bug
    this function exists to close.

    PRECONDITION (verified at handoff_transition.py:731,809-813): unconsume
    fails loud (``MutateAbort``) unless ``deployment_state`` is currently
    ``in_flight`` or ``ready_to_fire``. A dead-claimed handoff is normally
    ``in_flight``, but a claim could in principle be sitting on some other
    ``deployment_state``. Rather than let that raised ``MutateAbort``
    propagate uncaught into the caller's clear/reap path, this function
    checks ``deployment_state`` itself FIRST and SKIPS (logs + returns) on
    anything outside ``{in_flight, ready_to_fire}`` — the lock dir still
    gets cleared by the caller; only the frontmatter step is skipped. Also
    skips silently when the handoff file no longer exists (nothing to
    reconcile) — never raises.

    SUCCEEDED-BATON CARVE-OUT (2026-07-30, break-class fix). A handoff that
    some other baton already names as its predecessor is NOT returned to the
    pool, whatever the holder's liveness: the unclaim is SKIPPED and only the
    caller's lock clear proceeds. Two distinct harms made this break-class,
    both observed on disk (three claude-klabauter batons: the DR-084 vocabulary-migration,
    claude-klabauter-driven-ceremony-redesign, and registration-quad chains, reaped at
    ``796ebf5b`` / ``8a90946c`` / ``2892e394``):

      - RESURRECTION. ``_unclaim`` restores ``status: open`` +
        ``deployment_state: ready_to_fire`` on a baton that carries
        ``pickup_ready: true``, so resolved work re-advertises itself to
        ``/pickup``. The dead holder is dead precisely BECAUSE it finished and
        handed off; its successor is the live continuation.
      - EVIDENCE DESTRUCTION, and this one is not self-healing. DR-242's
        ``claimed_or_shipped_at_path`` gate requires independent
        PREDECESSOR-side proof that the parent was once claimed or reached a
        terminal state. Unclaiming erases exactly that proof, so every
        succession writer downstream — ``baton_assemble``'s d6,
        ``handoff.archive_transition`` mode="supersede" — refuses the baton
        FOREVER AFTER. The baton is left permanently open, permanently
        pickup-ready, and permanently unclosable by any automated path; it
        surfaces only as a ``baton-drift-sweep`` STRANDED row.

    The carve-out declines to REOPEN; it concludes nothing about succession and
    stamps nothing, so DR-242 is not in tension with it (DR-242 governs writing
    ``continued``, which no code path here does). The fail-safe directions differ
    in kind: not-unclaiming leaves a stale ``claimed`` a human can resolve with
    an explicit ``drop`` or a supersede, whereas unclaiming is effectively
    irreversible. Indeterminate corpus enumeration therefore also SKIPS —
    ``_handoff_has_named_successor`` returning ``None`` is treated as "may have
    a successor", never as "has none".

    ``sessions_dir``: the ``<common_dir>/coordinator-sessions`` dir (the same
    value ``clear_claim_if_dead``/``_reap_orphaned_claims`` already resolve);
    the handoff worktree is derived from its parent (``common_dir``) via
    ``main_worktree_root`` — the single reviewed home for that derivation
    (see that helper's own negative-spec; do not inline a bare ``.parent``).
    """
    # Local imports: avoids a claims.py <-> handoff_transition.py import-time
    # cycle (handoff_transition registers an IPC op at import time; claims.py
    # is a pure in-process library imported far earlier in the boot chain).
    from coordinator_core.ops.fleet._common import main_worktree_root
    from coordinator_core.ops.handoff_transition import _unclaim
    from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

    common_dir = sessions_dir.parent
    worktree = main_worktree_root(common_dir)
    handoff_path = worktree / "state" / "handoffs" / basename

    if not handoff_path.is_file():
        return  # claim referenced a handoff that's already gone — nothing to reconcile

    deployment = read_frontmatter_field(str(handoff_path), "deployment_state")
    if deployment not in ("in_flight", "ready_to_fire"):
        print(
            f"note: skipping frontmatter reconcile for dead handoff claim "
            f"'{basename}' — deployment_state '{deployment}' outside "
            f"unconsume's accepted set (in_flight, ready_to_fire); lock still cleared",
            file=sys.stderr,
        )
        return

    has_successor = _handoff_has_named_successor(handoff_path, worktree)
    if has_successor is not False:
        print(
            f"note: skipping frontmatter reconcile for dead handoff claim "
            f"'{basename}' — "
            + (
                "another baton names it as a predecessor"
                if has_successor
                else "the handoff corpus could not be enumerated completely"
            )
            + "; leaving it claimed rather than re-advertising it to /pickup "
            f"(see reconcile_dead_handoff_claim_frontmatter's succeeded-baton "
            f"carve-out); lock still cleared",
            file=sys.stderr,
        )
        return

    # reaped_from (C2): this dead-claim reconcile IS the reap path (the
    # legitimate drop/park path never calls this function), so it always
    # opts in. Resolved from the claim dir's own session_id/pid metadata via
    # _read_holder — the same value cs_clear_claim_if_dead already surfaces
    # in its own diagnostic message — as _unclaim's caller-supplied fallback;
    # _unclaim itself prefers the handoff frontmatter's claimed_by/consumed_by
    # over this when present.
    # Review: coordinator:code-reviewer — _read_holder's fallback rungs return
    # the non-empty sentinel "unknown", or a raw pid string, when the claim
    # dir has no session_id file; neither is a real session id, and passing
    # either through as reaped_from would let _unclaim's resolution chain
    # accept it as if it were one. Reject both shapes here rather than
    # widening _read_holder's own contract (cs_clear_claim_if_dead depends on
    # its current sentinel behaviour for its diagnostic) — pass "" (still
    # opts in to _unclaim's reaped_from resolution, since only `None` fully
    # disables it, but "" never wins the non-empty-string check itself).
    claim_dir = sessions_dir / "handoff-claims" / basename
    holder = _read_holder(claim_dir)
    reaped_from_sid = "" if holder == "unknown" or holder.isdigit() else holder
    result = _unclaim(str(handoff_path), "", worktree, common_dir, reaped_from=reaped_from_sid)
    if result.get("exit_code") != 0:
        print(
            f"note: frontmatter reconcile for dead handoff claim '{basename}' "
            f"failed — {result.get('error', 'unknown error')}; lock still cleared",
            file=sys.stderr,
        )


# Anchored on the FULL sentence ``reconcile_dead_handoff_claim_frontmatter``'s
# reaper caller emits (see that function's docstring) — em-dash included, and
# requiring a full 36-char UUID-shaped capture, never a bare ``\S+``. A
# park_note whose shape has drifted (including the differently-shaped,
# truncated-8-char-sid archived/quoted note class documented below) does NOT
# match and routes to the fail-closed skip in ``backfill_reaped_from_session``.
_CRASH_ORPHAN_PARK_NOTE_RE = re.compile(
    r"^claim released by crash-orphan reaper — holder "
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) "
    r"died without resolving"
)


def _collect_live_handoff_paths(worktree: Path) -> "Tuple[List[str], List[str]]":
    """Return (paths, scan_errors) for ``state/handoffs/*.md`` ONLY.

    Deliberately the LIVE HALF of ``handoff_children._collect_handoff_paths``,
    re-implemented here rather than reused, because that function's whole
    purpose is to also enumerate ``archive/handoffs/`` — this call site's one
    requirement is to never see that subtree at all (see
    ``backfill_reaped_from_session``'s negative-spec). Mirrors the live-half
    scan discipline exactly (``iterdir()``, not ``glob()``, so a permission
    error surfaces as a scan_errors entry rather than a silently-empty
    iterator — see that function's own NOTE for why ``glob()`` is unsafe
    here).
    """
    paths: List[str] = []
    scan_errors: List[str] = []

    state_dir = worktree / "state" / "handoffs"
    if state_dir.is_dir():
        try:
            entries = list(state_dir.iterdir())
        except OSError as exc:
            scan_errors.append(f"{state_dir}: {exc}")
        else:
            for p in entries:
                if p.suffix == ".md" and p.is_file():
                    paths.append(str(p.resolve()))

    return paths, scan_errors


def backfill_reaped_from_session(worktree: Path) -> dict:
    """One-shot backfill: parse the dead sid out of ``park_note`` for batons
    reaped BEFORE ``reaped_from_session`` (C2, this same plan) existed, and
    write it as a real frontmatter field.

    Spec backlink: pln-reaper-preserves-closure-evide-34a6fc § C5

    RE-DERIVES the target set at run time by scanning the LIVE corpus ONLY
    (``state/handoffs/``, via ``_collect_live_handoff_paths``) — deliberately
    does NOT hard-code the six paths named in the plan's Problem section, so a
    repo that has since reaped a seventh live baton the same way is served by
    the same entrypoint.

    Negative-spec: does NOT scan ``archive/handoffs/``. The plan's
    Out-of-scope section asserts no conclusion about the vendored
    ``handoff-archived.schema.json`` twin beyond noting the open question
    exists ("carries the same open question once these batons archive"); a
    write of ``reaped_from_session`` onto an already-archived, terminal
    record would assert exactly the conclusion that section declines to
    reach. Separately, ``reaped_orphan`` (the metric this field feeds) counts
    only NON-terminal batons, so an archived write buys this backfill nothing
    even setting the schema question aside. Do NOT widen this to
    ``handoff_children._collect_handoff_paths`` (its live+archived pair) —
    that reuse is intentional elsewhere in this module (see
    ``_handoff_has_named_successor``) but wrong here.

    A CLASS THIS CAN NEVER RECOVER: ``claims.py``'s own producer
    (``reconcile_dead_handoff_claim_frontmatter``) called ``_unclaim`` with an
    EMPTY note before C2 landed, so a tip reaped through that path before C2
    carries NO ``park_note`` at all — there is no sid to parse out of nothing.
    Such a baton is permanently unrecoverable by this function; only C2 going
    forward prevents new instances. It is counted among ``skipped`` here (no
    park_note to even attempt), never among ``errors``.

    Does NOT route through ``_unclaim``: every target baton is already at
    ``status: open`` + ``deployment_state: ready_to_fire`` — ``_unclaim``'s
    byte-identical idempotent no-op state — so calling it here would silently
    write nothing. This function writes the ONE new field directly, via
    ``locked_rmw`` (the same cross-process discipline every other frontmatter
    mutation in this engine uses — peer sessions are live in this tree).

    IDEMPOTENT: a baton already carrying ``reaped_from_session`` is skipped
    (counted in ``skipped``), and a second full run is byte-identical.

    UNVERIFIED BEYOND ONE RUN: re-run behaviour against a corpus that has
    since grown additional pre-C2 notes has not been exercised — only the
    single observed invocation against the live corpus at C5 time has been
    checked.

    FAIL-CLOSED PER BATON: an unparseable/absent/wrong-shape ``park_note``
    (including the archived, quoted, truncated-8-char-sid note class that an
    unanchored ``crash-orphan.*holder (\\S+)`` would mis-parse, e.g. "claim-
    release REVERTED 2026-07-30 — the crash-orphan reaper returned this baton
    to the pool at 796ebf5b, but holder 517027e6 had already minted its
    successor...") is skipped and named on stderr; the run keeps going for the
    rest of the corpus.

    Returns ``{"written": [...], "skipped": [...], "errors": [...]}`` — each
    a list of repo-relative path strings (``written``/``skipped``) or
    ``(path, message)`` tuples (``errors``, reserved for a locked_rmw failure
    — a parse/shape skip is NOT an error, it is an expected, named, fail-
    closed outcome and goes in ``skipped``). Paths are POSIX-normalized
    (``as_posix()``) regardless of platform, matching this engine's
    convention elsewhere (``_write_bump_message``'s ``clear_line``,
    ``scoped_git_commit``'s pathspec normalization) for any value that may
    later be logged, diffed, or compared across machines.
    """
    from coordinator_core.frontmatter.primitives import (
        insert_fm_field,
        read_fm_field,
        read_fm_field_unquoted,
        rebuild,
        split_frontmatter,
    )
    from coordinator_core.locked_write import MutateAbort, locked_rmw

    written: List[str] = []
    skipped: List[str] = []
    errors: List[Tuple[str, str]] = []

    paths, scan_errors = _collect_live_handoff_paths(worktree)
    for scan_error in scan_errors:
        errors.append(("<scan>", scan_error))

    for path_str in sorted(paths):
        path = Path(path_str)
        try:
            rel = path.relative_to(worktree).as_posix()
        except ValueError:
            rel = path_str

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append((rel, f"unreadable: {exc}"))
            continue

        split = split_frontmatter(text)
        if split is None:
            skipped.append(rel)
            continue
        fm = split.fm_text

        if read_fm_field(fm, "reaped_from_session") is not None:
            skipped.append(rel)  # already backfilled — idempotent no-op
            continue

        park_note = read_fm_field_unquoted(fm, "park_note")
        if not isinstance(park_note, str) or not park_note.strip():
            skipped.append(rel)  # no park_note at all — unrecoverable class
            continue

        match = _CRASH_ORPHAN_PARK_NOTE_RE.match(park_note.strip())
        if match is None:
            print(
                f"backfill_reaped_from_session: park_note shape not "
                f"recognized for {rel} — skipping: {park_note!r}",
                file=sys.stderr,
            )
            skipped.append(rel)
            continue

        sid = match.group(1)

        def mutate(old_text: str, _sid: str = sid) -> str:
            inner = split_frontmatter(old_text)
            if inner is None:
                raise MutateAbort(
                    f"backfill_reaped_from_session: frontmatter vanished "
                    f"under lock for {path}"
                )
            inner_fm = inner.fm_text
            if read_fm_field(inner_fm, "reaped_from_session") is not None:
                return old_text  # raced with a peer backfill — no-op
            inner_fm = insert_fm_field(
                inner_fm, "reaped_from_session", _sid, "deployment_state"
            )
            return rebuild(inner, inner_fm)

        try:
            locked_rmw(path, mutate, repo_root=worktree)
        except MutateAbort as exc:
            errors.append((rel, str(exc)))
            continue
        except OSError as exc:
            errors.append((rel, f"write failed: {exc}"))
            continue

        written.append(rel)

    return {"written": written, "skipped": skipped, "errors": errors}


def list_claims_by_session(sid: str, cwd: Optional[str] = None) -> List[Tuple[str, str]]:
    """Thin wrapper over ``list_claims_by_session_checked`` that discards its
    ``errors`` return arm. Kept byte-identical to its pre-existing contract
    (return type, signature, behaviour) for live callers and tests that
    assert ``== []`` on the no-git-repo path. Callers that need to
    distinguish "genuinely owns nothing" from "the claim store could not be
    resolved" (e.g. an unresolvable ``cwd``) must call the checked variant
    directly and inspect its ``errors`` list."""
    matches, _errors = list_claims_by_session_checked(sid, cwd)
    return matches


def list_claims_by_session_checked(
    sid: str, cwd: Optional[str] = None
) -> "Tuple[List[Tuple[str, str]], List[str]]":
    """Return ``([(class_, basename), ...], errors)`` for every claim dir under this
    repo's ``<sessions_dir>`` whose recorded ``session_id`` == ``sid``, across
    all three claim classes (``_CLAIM_SUBDIRS``). Reads the CLAIM-RECORD STORE
    directly (the ``session_id`` file inside each ``<class>-claims/<basename>/``
    dir) — NEVER the ``claimed_by``/``consumed_by`` frontmatter mirror. An
    unreadable or absent ``session_id`` file is "not a match", not an error —
    this is an ownership-index FILTER, not the tolerant diagnostic label
    ``_read_holder`` provides (that function's pid/``"unknown"`` fallback
    exists for a human-facing message, not for an identity predicate; a claim
    with no readable ``session_id`` cannot be attributed to any queried sid).

    Claim-record LIFECYCLE (which stages release a ``handoff-claims`` entry,
    named because C19's ownership index — and this function's own
    correctness for the AC20 double-count scenario — depends on it):

        - ship (``coordinator_core/ops/ceremony/tail_ops.py:900
          cs_release_artifact(common_dir, "plan", governing_plan_slug)``, called
          from ``wsc_tail.py``'s Step 6 — the native-port successor of the OLD
          ``wsc_commit.py``'s now-deleted ``_native_cs_release_artifact``,
          2026-07-29 kill-list op removal): SURVIVES. Hardcoded to
          ``artifact_class="plan"`` — releases the plan claim only; no
          handoff-class release call exists anywhere in this ship path.
          This wiring is verified by reading the call site, not by
          a test that executes it — invoking it requires the enclosing
          ``_commit_orchestration_sequence`` (PipelineContext + commit-outcome
          plumbing), assessed as disproportionate scaffolding for a unit test
          (Review: code-reviewer slice 2, 2026-07-27, Finding 1).
        - archive (``coordinator_core/ops/ceremony/consumed_handoff_stamp.py
          :381 post_commit_stamp_and_ship`` -> ``handoff_transition._ship``
          at ``coordinator_core/ops/handoff_transition.py:639``): SURVIVES.
          Neither function imports or calls ``release_artifact``/``claims.``
          anywhere. The ``_ship`` mutator itself IS exercised directly by
          ``coordinator_core/session/tests/test_claims.py::
          test_list_claims_by_session_survives_real_ship_call_site``, which
          calls it for real and asserts the handoff claim survives; the
          surrounding async ``post_commit_stamp_and_ship`` orchestration
          (liveness re-check, stamp-before-ship ordering) is not itself
          exercised by that test.
        - release (explicit ``drop``, ``coordinator_core/pickup_assemble/
          apply.py:~1029 release_artifact(class_, basename, cwd=str(root))``
          inside ``drop()`` at ``:967``, for ``class_ in ("handoff", "memo")``):
          RELEASED. This is the put-down/decline path, never a side effect
          of shipping.
        - reap (``coordinator_core/ops/session/reap.py:417
          _reap_orphaned_claims``, iterating ``_CLAIM_SUBDIRS``): released
          ONLY when the holding session is confirmed DEAD (fail-closed-to-keep
          on a live holder or a liveness-check exception). Not applicable at
          gate-read time for a still-running closing session — but is the
          mechanism that eventually reaps a handoff claim once its session
          truly exits.

    Consequence: a handoff claim survives both ship and archive, so an
    archived-mid-ceremony owned baton is still attributable to its session at
    gate-read time — neither closing path silently drops it from this index.
    """
    # Local import: avoids the same claims.py <-> ops.session.reap import-time
    # cycle documented on reconcile_dead_handoff_claim_frontmatter above —
    # ops.fleet._common's package (coordinator_core.ops) eager-imports every
    # op module at import time, including reap.py, which imports back from
    # this module.
    from coordinator_core.ops.fleet._common import _CLAIM_SUBDIRS

    matches: List[Tuple[str, str]] = []
    base = core.sessions_dir(cwd)
    if not base:
        return matches, [
            f"list_claims_by_session_checked: could not resolve sessions_dir "
            f"for cwd={cwd!r}"
        ]
    base_path = Path(base)
    for class_ in _CLAIM_SUBDIRS:
        class_dir = base_path / class_
        if not class_dir.is_dir():
            continue
        for claim_dir in sorted(class_dir.iterdir()):
            if not claim_dir.is_dir():
                continue
            sid_f = claim_dir / "session_id"
            try:
                held_sid = sid_f.read_text(encoding="utf-8").strip()
            except OSError:
                continue  # unreadable/absent session_id — not a match
            if held_sid == sid:
                matches.append((class_, claim_dir.name))
    return matches, []


def _read_holder(claim_dir: Path) -> str:
    """Holder label for a diagnostic message: the ``session_id`` file content
    (even if empty), else the ``pid`` file content, else ``"unknown"`` —
    mirrors the bash ``cat session_id || cat pid || echo unknown`` (where a
    present-but-empty session_id file short-circuits to ``""``, matching cat's
    exit-0 on an empty file)."""
    sid_f = claim_dir / "session_id"
    if sid_f.is_file():
        try:
            return sid_f.read_text(encoding="utf-8").strip()
        except OSError:
            pass  # unreadable session_id file — fall through to the pid rung
    pid_f = claim_dir / "pid"
    if pid_f.is_file():
        try:
            return pid_f.read_text(encoding="utf-8").strip()
        except OSError:
            pass  # unreadable pid file — fall through to "unknown"
    return "unknown"


# ---------------------------------------------------------------------------
# Sub-agent touch attribution + self-claim
# ---------------------------------------------------------------------------


def my_agent_touched(
    session_id: str, mode: str = "broadened", cwd: Optional[str] = None
) -> List[str]:
    """Port of ``_cs_my_agent_touched <session_id> [mode]`` (1181-1224).

    Return (as a list, one entry per path — the bash prints one per line) every
    repo-relative path touched by a sub-agent whose
    ``.agents/<aid>/em-session-id.txt`` back-pointer is in the candidate set
    for ``session_id``.

    ``mode`` (default ``"broadened"``):
      - ``broadened`` — candidate set = ``{session_id}`` UNION all live session
        ids (``liveness.live_session_ids``). Recovers the EM's own fan-out
        output on old Claude Code where an executor SessionStart can pollute
        the ``.current-session-id`` sentinel. Safe for the default (do_scoped)
        path because ``compute_scope`` re-subtracts other_sessions downstream,
        so any over-reach is self-correcting.
      - ``exact`` — candidate set = ``{session_id}`` only. Use on the blanket
        path where broadening would scoop a sibling EM's own sub-agent
        back-pointer into "own", causing the blanket to absorb the sibling's
        in-flight files.

    FAIL-OPEN: never raises (except the required-arg guard) — attribution is
    advisory and never blocks the caller. Empty back-pointer / missing
    ``touched.txt`` / unreadable file are all soft-skipped. Returns ``[]`` when
    not in a git repo or the ``.agents`` dir is absent, AND (degraded, still
    ``[]``, but with a logged stderr warning) when ``.agents`` exists but
    cannot be listed (e.g. permission-denied) — the sub-agent enumeration
    uses ``os.scandir()``, not ``Path.glob("*/")``, precisely so an
    unreadable dir raises ``OSError`` instead of silently reading as
    "genuinely zero sub-agent dirs" (the spec here is an AUTHORIZED BLANKET
    CAPTURE; a silently-missed dir is a completeness bug).

    ``session_id`` is REQUIRED — empty raises ValueError.

    RETURNS PATHS, NEVER RAW JOURNAL LINES. Each back-pointed agent dir's
    ``touched.txt`` is a ``T``/``R`` event journal
    (``scope.format_touch_event``), so its lines go through
    ``scope.project_self_scope`` — the same projection ``compute_scope``
    Step 1 and ``_release_from_touched_file`` use — before they leave this
    function. Projection is PER AGENT DIR because
    ``release_committed_claims`` appends each agent's ``R`` events into that
    agent's own file; a path whose last event there is ``R`` is released and
    is excluded. Legacy bare-path lines still parse as ``T`` and survive.

    Negative-spec: do NOT revert to appending raw lines. Every caller treats
    these as paths — ``safe_commit_offer._resolve_agent_touched_candidates``
    feeds them to ``_normalize_agent_touched_entry`` (whose contract is a
    path, and which returns a ``'T <ISO> …'`` line unchanged rather than
    rejecting it) and ``bash_guards.dispatch_checks`` unions them straight
    into ``my_scope``. Raw lines match no file, so the entire sub-agent
    fan-out leg contributes zero usable paths and files a session's own
    dispatched agents wrote get refused as ``unclaimed`` — the defect
    example-market-data-repo-em reported 2026-08-04
    (``cross-repo/inbox/2026-08-04-example-market-data-repo-em-agent-touched-journal-format-unmigrated.md``).

    Spec backlink: docs/plans/2026-06-22-authorized-blanket-orphan-capture-not-sibling-sweep.md § C1a Step 1.
    """
    if not session_id:
        raise ValueError("session_id required")

    base = core.sessions_dir(cwd)
    if not base:
        return []
    agents_dir = Path(base) / ".agents"
    if not agents_dir.is_dir():
        return []

    candidates = {session_id}
    if mode == "broadened":
        candidates |= set(liveness.live_session_ids(cwd))

    out: List[str] = []
    # bash pathname expansion (`base/.agents/*/`) is alphabetically sorted.
    #
    # NOTE: uses os.scandir(), NOT Path.glob("*/") -- Path.glob()'s selector
    # silently swallows PermissionError while walking (empirically re-
    # verified: a chmod-000 dir yields an empty iterator from glob(), no
    # exception), which would make an unreadable .agents/ indistinguishable
    # from "genuinely zero sub-agent dirs" -- the spec here is an
    # AUTHORIZED BLANKET CAPTURE, so a silently-missed dir is a completeness
    # bug, not a benign empty result. os.scandir() raises OSError as
    # expected; the failure is logged (module stays FAIL-OPEN -- attribution
    # is advisory and must never block the caller) rather than raised.
    try:
        agent_entries = sorted(os.scandir(agents_dir), key=lambda e: e.name)
    except OSError as exc:
        print(
            f"coordinator-session: cannot scan {agents_dir} — {exc}; "
            "agent-touch attribution may be INCOMPLETE (not the same as "
            "\"no sub-agent dirs\") — advisory only, not blocking the caller",
            file=sys.stderr,
        )
        return out

    for entry in agent_entries:
        agent_dir = Path(entry.path)
        if not agent_dir.is_dir():
            continue
        backptr = agent_dir / "em-session-id.txt"
        if not backptr.is_file():
            continue
        try:
            first = backptr.read_text(encoding="utf-8").splitlines()
        except OSError:
            first = []
        em_sid = first[0] if first else ""
        if not em_sid:
            continue  # malformed back-pointer; soft-skip
        if em_sid not in candidates:
            continue
        agent_touched = agent_dir / "touched.txt"
        if not agent_touched.is_file():
            continue
        try:
            lines = agent_touched.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue  # unreadable touched.txt — soft-skip per the FAIL-OPEN contract above
        for fpath in sorted(scope.project_self_scope(lines)):
            if fpath:
                out.append(fpath)
    return out


def self_claim(path: str, cwd: Optional[str] = None) -> bool:
    """Port of ``cs_self_claim <path>`` (726-758).

    Record ``path`` in the current session's ``touched.txt`` (best-effort
    attribution for tools that edit files outside the Edit/Write hook path).

    Normalization: ``path`` is run through
    ``coordinator_core.session.scope.normalize_touch_path`` before either
    append attempt below — the SAME normalize-or-skip-if-still-absolute
    contract ``scope.touch()`` (the PreToolUse hot path) already enforces.
    Do NOT re-inline a second normalization dialect here; a caller (e.g.
    ``coordinator_core.text.refresh_queries.process_file``) routinely
    invokes this with an ALWAYS-absolute path, so skipping this step is
    exactly how an absolute entry lands in ``touched.txt`` (DoE
    security-audit 2026-07-31: 240 such entries corroborated on disk). A
    path that is still absolute after normalization is SKIPPED (fail-open,
    advisory-only — this function's return-True-always contract already
    covers a skip; no touched.txt write happens for it).

    Resolution prefers the platform-injected session id directly —
    ``CLAUDE_SESSION_ID`` override, then ``CLAUDE_CODE_SESSION_ID`` (Claude
    Code >= ~2.1.150). That is O(1) and unambiguous. NOTE: this fast path reads
    ONLY those two env vars — it does NOT consult ``COORDINATOR_SESSION_ID`` or
    the cwd sentinel (that is deliberate; it is NOT the full 4-tier
    ``resolve_session_id`` chain). Only when NEITHER env var is set does it fall
    back to ``liveness.live_session_ids`` (O(n) over every session dir) and
    claim ONLY when EXACTLY ONE session is live (otherwise attribution is
    ambiguous — skip).

    ALWAYS returns True (bash ``return 0`` — fail-open: attribution is advisory
    and never blocks the caller). Appends via ``atomic_dedup_append``.

    ``path`` is REQUIRED — empty raises ValueError.
    """
    if not path:
        raise ValueError("path required")

    normalized = scope.normalize_touch_path(path, cwd)
    if normalized is None:
        # STILL absolute after normalization — skip (fail-open, advisory
        # only). Mirrors scope.touch()'s own guard; see this function's
        # docstring for why this cannot be inlined as a second dialect.
        print(
            f"coordinator-session: self-claim path {path!r} is still "
            f"absolute after normalization — skipping (would corrupt the "
            f"relative-path scope set)",
            file=sys.stderr,
        )
        return True
    entry = normalized

    # Fast path: platform tells us our own session id directly (these two env
    # vars ONLY — deliberately not the full resolve_session_id chain).
    sid = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get(
        "CLAUDE_CODE_SESSION_ID"
    ) or ""
    if sid:
        sdir = core.session_dir(sid, cwd)
        if not sdir:
            return True
        if not Path(sdir).is_dir():
            return True  # session dir gone — nothing to claim against
        try:
            atomic_dedup_append(str(Path(sdir) / "touched.txt"), entry)
        except (OSError, ValueError) as exc:
            # fail-open — self-claim attribution is advisory and must never
            # block the caller; surface the failure for debugging.
            print(
                f"coordinator-session: self-claim append failed for {entry} "
                f"(sid {sid}) — advisory only: {exc}",
                file=sys.stderr,
            )
        return True

    # Fallback (no session env var): enumerate live sessions; claim only when
    # EXACTLY ONE is live (otherwise attribution is ambiguous — skip).
    sids = liveness.live_session_ids(cwd)
    count = len(sids)
    if count == 0:
        print(
            f"coordinator-session: no active session found — "
            f"skipping self-claim for {entry}",
            file=sys.stderr,
        )
        return True
    if count > 1:
        print(
            f"coordinator-session: {count} live sessions (ambiguous) — "
            f"skipping self-claim for {entry}",
            file=sys.stderr,
        )
        return True
    sid = next(iter(sids))
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return True
    try:
        atomic_dedup_append(str(Path(sdir) / "touched.txt"), entry)
    except (OSError, ValueError) as exc:
        # fail-open — self-claim attribution is advisory and must never
        # block the caller; surface the failure for debugging.
        print(
            f"coordinator-session: self-claim append failed for {entry} "
            f"(sid {sid}) — advisory only: {exc}",
            file=sys.stderr,
        )
    return True
