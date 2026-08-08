"""
coordinator_core.session.scope — the session touch-tracking + scoped-
staging-set computation module of the coordinator session hub.

Port of: scope.sh (example-doctrine-repo e34f2484, 2026-07-22).

Purpose: track which repo-relative files a session has touched, and compute
the per-session scoped staging set — see :func:`compute_scope`'s own
docstring for the exact set-math formula (touched.txt + extra_candidates,
minus other sessions' AND peer sub-agents' claims, minus uncontested
mtime-only candidates); not duplicated here to avoid a second copy drifting
out of sync with the function docstring.

so that concurrent EM sessions sharing one working tree can each commit only
*their* files. ``touch()`` is the HOT path (fired from PreToolUse hooks on
every file write); ``compute_scope()`` is called at commit time by example-doctrine-repo's
``coordinator-safe-commit`` (Python port, in-process import of this module —
repointed 2026-07-22 per example-doctrine-repo plan
2026-07-22-coordinator-session-family-repoint-and-delete). ``archive()``
moves a finished session dir under ``.archive/`` after its final commit.

Reuses ``coordinator_core.session.core`` for every path resolver, clock,
mtime, meta.json, and git-root helper — see that module for the
NOT-cached-across-calls constraint (every path-resolving fn threads an
optional ``cwd``).

Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § scope.py
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4a-g1

Negative-spec:
    - ``compute_scope`` MUST use TWO git commands (``git diff --name-only
      HEAD`` UNION ``git ls-files --others --exclude-standard``), NOT ``git
      status --porcelain`` — porcelain collapses an untracked directory to
      ``dir/`` and loses the individual filenames the scope set needs.
    - Do NOT normalize an absolute ``touch`` path into ``touched.txt`` if it
      is STILL absolute after the git/realpath normalization attempt — an
      absolute path corrupts the relative-path scope set. The guard is on a
      STILL-absolute path, NOT on an empty one (``fpath`` is never empty —
      it is a required arg). Fail-open: skip.
    - First-writer-wins on the other-session owner scan (bash uses parallel
      arrays for 3.2-safety; this port uses a dict but preserves
      first-writer-wins by only recording a path's owner once).
    - Windows: the hub uses ``mkdir``-based locks (NOT ``flock``)
      deliberately; nothing in this module takes a lock, but preserve that
      convention if lock logic is ever added.
    - Do NOT port ``cs_reap_stale`` — it is ALREADY native at
      ``coordinator_core/ops/session/reap.py``.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import subprocess
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Literal, Mapping, NamedTuple, Optional, Set, Tuple

from coordinator_core.session import core, liveness
from coordinator_core.win_portability import no_console_creationflags

#: Matches a path bash treats as absolute: POSIX ``/…`` or a Windows/Git-Bash
#: drive-qualified ``C:…`` form. Mirrors the bash glob test
#: ``[[ "$fpath" == /* || "$fpath" == [A-Za-z]:* ]]``.
_ABSOLUTE_RE = re.compile(r"^(?:/|[A-Za-z]:)")


class OwnerFact(NamedTuple):
    """One ATTRIBUTED peer/agent claim on a path — the UNGATED counterpart to
    :func:`compute_scope`'s ``other_owner`` dict (C1,
    docs/plans/... scope-attribution split).

    This is an ATTRIBUTION-vs-SUBTRACTION split, not a projection of
    ``other_owner``: every peer/agent claim Step 3/3b's loop reads is
    recorded here — including a dead peer's claim (never entered into
    ``other_owner`` at all) and a clean-path-pruned claim (dropped from
    ``other_owner`` because the path has no uncommitted content) — so a
    caller wanting "who has ever claimed this path, and what did the
    liveness/dirty-path gates decide about it" has a place to look that
    ``other_owner`` cannot answer (it only ever holds a currently-contesting
    claim). ``ScopeResult.attribution`` MUST NEVER be read back into, or
    substituted for, ``other_owner``/``skipped``/``my_scope`` — it never
    gates and is never gated on; it is a reporting-only sidecar.

    Fields:
      owner         — the claiming session id, or the back-pointed
                       em-session-id for a dispatched sub-agent's claim
                       (Step 3b), or a sentinel identifier (matching the
                       ``unreadable_other_sessions``/agent-race dialect —
                       e.g. ``".agents/<agent-dir-name>"``) when the real
                       owner could not be resolved at all.
      liveness      — ``"live"`` | ``"dead"`` | ``"undetermined"`` — the
                       SAME liveness verdict Step 3/3b's gate itself used
                       for this claim (``"undetermined"`` when liveness
                       gating was disabled this call, or when no owner
                       identity was resolvable to check liveness against).
      claim_source  — ``"session"`` (Step 3 per-session ``touched.txt``
                       claim), ``"agent"`` (Step 3b claim resolved to an
                       owning EM session via ``em-session-id.txt``),
                       ``"agent-race"`` (Step 3b claim from a not-yet-back-
                       pointed, recently-active agent dir — see the
                       36ed64f58 race-window comment at that call site), or
                       ``"unreadable"`` (the claim set itself could not be
                       read, so only the fact of an indeterminate claim is
                       recorded, not its path content).
    """

    owner: str
    # Review: code-reviewer Finding 5 (2026-08-03) — the docstring above
    # enumerates exactly 3 `liveness` values and 4 `claim_source` values;
    # a bare `str` let a typo at any construction site type-check cleanly
    # and only surface as silent drift for a downstream exact-string
    # comparison. `Literal` closes that at every one of the ~6 call sites.
    liveness: Literal["live", "dead", "undetermined"]
    claim_source: Literal["session", "agent", "agent-race", "unreadable"]


class ScopeResult(NamedTuple):
    """Structured return of :func:`compute_scope`.

    Replaces the bash function's stdout/stderr split:
      - ``my_scope``  — the scoped staging set (bash: one path per line on
        stdout), in first-seen order.
      - ``skipped``   — ``(path, owner_sid)`` pairs subtracted because another
        session owns them (bash: ``"skipping <path> — owned by session
        <owner>"`` on stderr).
      - ``orphans``   — dirty paths this call could not attribute to ANY
        session. Read as UNATTRIBUTED, never as UNCLAIMED — every fail-closed
        withhold arm in Step 3/3b/4 (an unreadable peer touched.txt, an
        agent-race-window overlap, a liveness-enumeration indeterminacy) also
        drains into this field by construction, alongside genuine "nobody's
        claim" orphans. A caller that reads membership in ``orphans`` as
        permission to adopt a path (e.g. an opt-in orphan-inclusion allow-
        list) reproduces the exact bug staff-eng review Finding F1
        (2026-08-03, ``docs/plans/2026-08-03-scope-guard-peer-claim-release
        .md``) found live: a degraded read of a genuinely live peer's claim
        widened straight into "safe to commit". Never derive an allow-list
        from this field without first subtracting ``skipped`` (see
        :func:`coordinator_core.ops.session.safe_commit_offer.compute_offer`
        for the corrected pattern).

        THIRD population in this field, and the one no subtraction recovers
        (DR-258, a ratified permanent limit — not a gap awaiting a fix): a
        path a peer wrote ONLY via Bash. ``hooks.track_touched_files`` fires
        on Write/Edit/MultiEdit/NotebookEdit and nothing else, so such a path
        carries no claim and no release event anywhere — there is no record
        for this function to read, and nothing for the peer-facing projection
        to project back from. It is genuinely indistinguishable here from
        "nobody's file". Note the asymmetry with the released-path case: the
        claim-release projection restores parity for a path that WAS claimed
        and later released; it does not extend coverage to one that was never
        claimed. Read membership in ``orphans`` accordingly — "unattributed,
        and possibly a live peer's Bash-authored work in flight" — which is
        why adoption is opt-in and gated, never a default.
      - ``attribution`` — (C1, additive) ``Mapping[str, OwnerFact]`` of every
        peer/agent claim Step 3/3b's loop read, UNGATED by liveness or the
        clean-path prune — see :class:`OwnerFact` for the split from
        ``other_owner`` this exists to preserve. Defaults to an empty
        mapping for every pre-existing construction/call site (additive
        field; never widens or narrows ``my_scope``/``skipped``/``orphans``,
        and is never derived from them or vice versa). Do not mutate the
        returned mapping in place — treat it as read-only, like the other
        ``ScopeResult`` fields; this is enforced structurally, not just by
        convention, because ``attribution`` is ALWAYS a
        ``types.MappingProxyType`` — both the shared empty default below AND
        the real, freshly-built dict :func:`compute_scope` returns are
        wrapped in one before this NamedTuple is constructed, so an in-place
        mutation attempt raises ``TypeError`` on every code path, never just
        the rarely-exercised early-return one. Review: staff-eng P2
        (2026-08-03, pass 3) — an EARLIER version of this fix left the
        return-site value a plain ``dict`` while only the default was a
        ``mappingproxy``, which made the runtime type of this field vary by
        code path (``isinstance(x, dict)`` true on the common path, false on
        the rare one) — the worst shape for a future consumer, since the
        differing path is also the one least likely to be exercised in
        testing. The annotation is ``Mapping``, not ``dict``, precisely to
        keep the type honest about this: nothing outside this module may
        rely on ``attribution`` being a mutable ``dict``.
      - ``indeterminate`` — (R1, additive) ``True`` iff THIS call withheld at
        least one candidate for a reason it could not attribute to a
        specific claim — i.e. ``unreadable_other_sessions`` was non-empty
        (a peer's or agent's claim set could not be read) or
        ``agent_race_paths`` was non-empty (a recent, not-yet-back-pointed
        sub-agent claim overlapped a candidate) this call. Defaults to
        ``False``. Read as: "``orphans`` for this call is not trustworthy as
        an adoption allow-list" — see
        :func:`coordinator_core.ops.session.safe_commit_offer.compute_offer`,
        which returns ``orphans: []`` whenever this is ``True``, closing
        staff-eng R1 (2026-08-03): the ``orphans − skipped_paths``
        subtraction alone only recovers a withheld CANDIDATE (one that
        entered ``touched_set``); a dirty path never adopted as a candidate
        at all (``started_at`` in the future, unreadable, or an mtime
        predating session start) bypasses Step 4 entirely, gets no
        ``skipped`` counterpart for that subtraction to remove, and would
        otherwise reach ``orphans`` even while a live peer's claim set was
        unreadable. This flag covers that non-candidate shape and the
        agent-race non-candidate shape in ONE gate — the whole call's
        ``orphans`` is withheld from adoption, never just the specific
        candidate paths (a wider blast radius than per-path narrowing, but
        the correct one: nothing here can attribute WHICH orphans are
        actually safe once any claim set for this call was unreadable, so
        withholding all of them is the only sound default). Never narrows
        ``my_scope`` or ``skipped`` — those are unaffected by this flag; it
        exists purely as an ADOPTION-suitability signal for a caller reading
        ``orphans``. Does NOT cover the pre-existing liveness-enumeration
        under-report residual (documented above and at Step 3's docstring
        paragraph) — nothing marks that call as degraded, which is what
        keeps it a genuine residual rather than an oversight; see this
        function's own docstring for the full accounting.
    """

    my_scope: List[str]
    skipped: List[Tuple[str, str]]
    orphans: List[str]
    # A plain `{}` default here would be ONE dict instance shared across
    # every default-constructed ScopeResult (the classic mutable-default
    # hazard) — harmless only as long as every reader honors the read-only
    # contract above by convention. types.MappingProxyType({}) closes the
    # hazard structurally: it is still one shared instance, but it is
    # immutable, so an in-place mutation attempt raises TypeError instead of
    # silently corrupting every other caller's "empty" default.
    #
    # Review: staff-eng P2 (2026-08-03, pass 3) — the real attribution dict
    # compute_scope builds is ALSO wrapped in types.MappingProxyType at the
    # return statement (end of this module) before construction, not passed
    # through as a plain dict. Wrapping only the default (and not the real
    # value) would make this field's runtime type vary by code path — a
    # mappingproxy on the rare out-of-repo/no-sessions-dir early-return path
    # below, a plain dict on every real call — which is the worse shape: a
    # future consumer that works today (dict-shaped) breaks only on the path
    # least likely to be exercised in testing. Wrapping both sides makes the
    # runtime type uniform; see `test_attribution_runtime_type_is_uniform_
    # across_code_paths` in test_scope.py, which pins exactly this.
    attribution: "Mapping[str, OwnerFact]" = types.MappingProxyType({})
    indeterminate: bool = False


def _is_absolute(path: str) -> bool:
    """True iff ``path`` is absolute in the bash sense (POSIX ``/`` or a
    drive-qualified ``C:`` prefix)."""
    return _ABSOLUTE_RE.match(path or "") is not None


_normalize_diag_fired = False


def _emit_normalize_diagnostic(reason: str) -> None:
    """One-shot, deduped-per-process stderr diagnostic for
    :func:`normalize_touch_path`'s fail-open path.

    Fires at most once per process (module-level latch) so a systemic
    ``git ls-files``/relpath failure (e.g. the exit-128-on-every-edit class
    documented in the plan this satisfies) surfaces within one session
    instead of being silently swallowed on every ``touch()`` call. Never
    raises — this is an observability seam only, not a behavior change:
    ``normalize_touch_path``'s and ``touch()``'s fail-open contracts are
    unchanged either way.

    Negative spec on the WORDING: the message must not claim more than the
    code knows. A fired latch means one normalization attempt took an
    UNEXPECTED failure path, not that any specific entry is wrong — the
    relpath fallback frequently produces the correct repo-relative path
    anyway. It also must not be read as covering the routine
    path-outside-this-repo case, which :func:`_ls_files_failure_is_benign`
    now filters out before this is ever called (example-cockpit-repo-em memo,
    2026-08-05 § 2, and the follow-up mis-calibration finding: the latch used
    to fire on every session that touched a sibling repo, ``~/.claude``, or a
    scratch dir — the dominant case in this fleet — while asserting
    corruption that had not happened).
    """
    global _normalize_diag_fired
    if _normalize_diag_fired:
        return
    _normalize_diag_fired = True
    try:
        print(
            f"normalize_touch_path: {reason} failed unexpectedly at least "
            "once this process — further occurrences are silenced. This is "
            "NOT the routine path-outside-this-repo case (expected, handled "
            "by the relpath fallback); normalization fell back to a "
            "best-effort relative path, so touched.txt entries written this "
            "process may be mis-normalized or missing.",
            file=sys.stderr,
        )
    except Exception:
        pass


def normalize_diagnostic_fired() -> bool:
    """True iff :func:`_emit_normalize_diagnostic`'s one-shot latch has fired
    in this process — i.e. ``normalize_touch_path`` took its fail-open path
    for a reason it could NOT positively classify as benign, so
    ``touched.txt`` entries written this process may be mis-normalized or
    missing (they may equally be fine: the relpath fallback often still
    lands the right value).

    A READ of the latch, for a consumer whose own output should say so. Since
    the benign/operational discrimination below landed, a set latch means an
    UNEXPECTED normalization failure — never the routine
    path-outside-this-repo case, which no longer arms it at all.
    ``touched.txt`` is the input from which
    ``coordinator_core.ops.session.safe_commit_offer`` derives a session's
    commit pathspec, so the one caller computing that boundary can render the
    degradation NEXT to the boundary it degrades instead of leaving it as a
    stderr line from another module, scrolled past above the report
    (example-cockpit-repo-em memo, 2026-08-05).

    Negative spec: this does NOT reset the latch, does NOT arm it, and adds no
    side effect of any kind — the fail-open contract of
    ``normalize_touch_path`` and the once-per-process firing rule of
    ``_emit_normalize_diagnostic`` are unchanged and must stay that way. A
    consumer wanting per-call granularity needs a different mechanism, not a
    mutable accessor here.
    """
    return _normalize_diag_fired


class GitRun(NamedTuple):
    """One completed ``git`` invocation's full result — returncode, stdout and
    stderr — for the one call site that must DISCRIMINATE between failure
    shapes rather than collapse them (see :func:`_ls_files_failure_is_benign`).
    """

    returncode: int
    stdout: str
    stderr: str


def _git_run(args: List[str], cwd: Optional[str] = None) -> Optional[GitRun]:
    """Run ``git <args>`` and return the full :class:`GitRun`, or ``None`` iff
    git could not be EXECUTED at all (``OSError``: binary missing, not
    executable, fork failure).

    THE single subprocess seam for this module — :func:`_git_output` is a thin
    projection of this, deliberately, so there is exactly one git dialect here
    (same argv shape, same ``no_console_creationflags()`` flags, same text
    mode).

    Negative spec: ``None`` means "no git ran", NOT "git failed" — a non-zero
    exit is a fully-populated ``GitRun`` and must stay distinguishable from
    ``None``, because the two classify differently downstream (an unexecutable
    git is always operational; a non-zero exit may be benign).
    """
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            **no_console_creationflags(),
        )
    except OSError:
        return None
    return GitRun(result.returncode, result.stdout, result.stderr)


def _git_output(args: List[str], cwd: Optional[str] = None) -> Optional[str]:
    """Run ``git <args>`` and return stdout, or ``None`` on any failure
    (git missing, non-zero exit) — the irreducible-subprocess seam for the
    handful of git commands this module cannot replace natively. Mirrors the
    bash ``2>/dev/null`` swallow: a failed git command contributes nothing.

    Semantics UNCHANGED: still ``Optional[str]``, still collapsing every
    failure shape into ``None``. Every caller that only needs "stdout or
    nothing" (``compute_scope``'s dirty scan, the status read) keeps using
    this; a caller that must tell failure shapes apart calls :func:`_git_run`
    directly rather than widening this return type.
    """
    result = _git_run(args, cwd)
    if result is None or result.returncode != 0:
        return None
    return result.stdout


#: git's fatal for a pathspec that resolves outside the worktree, e.g.
#: ``fatal: /etc/hosts: '/etc/hosts' is outside repository at '<root>'``.
#: A CORROBORATING signal only — never the sole basis for a benign verdict,
#: since git's fatals are gettext-translatable and this substring is English.
#: The authoritative test is the native containment check below, which is
#: locale-proof.
_GIT_OUTSIDE_REPOSITORY_MARKER = "outside repository"


def _path_is_outside_worktree(fpath: str, root: Optional[str]) -> Optional[bool]:
    """``True``/``False`` iff ``fpath`` provably is/is not inside the worktree
    at ``root``; ``None`` when that cannot be decided (no resolved root).

    Native and locale-proof — realpath on BOTH sides, matching
    :func:`normalize_touch_path`'s own fallback (macOS ``/var`` →
    ``/private/var``). A ``ValueError`` from ``relpath`` is a Windows
    cross-drive comparison, which is itself proof the path is not under
    ``root``.
    """
    if not root:
        return None
    try:
        rel = os.path.relpath(os.path.realpath(fpath), os.path.realpath(root))
    except ValueError:
        return True  # different drive — cannot be inside this worktree
    except OSError:
        return None
    rel = rel.replace(os.sep, "/")
    return rel == ".." or rel.startswith("../")


def _ls_files_failure_is_benign(
    result: Optional[GitRun], fpath: str, root: Optional[str]
) -> bool:
    """Did ``git ls-files --full-name -- <fpath>`` fail merely because
    ``fpath`` is not in THIS repository?

    That case is routine and expected — a session touching a sibling repo,
    ``~/.claude``, or a scratch dir hits it on every such write, and
    :func:`normalize_touch_path`'s relpath fallback handles it correctly.
    Before this discrimination existed, it armed
    :func:`_emit_normalize_diagnostic`'s latch, which in this fleet made the
    latch fire for most sessions on a non-condition — and, once
    ``safe-commit-offer`` began LEADING its report with that latch, put a
    false "DEGRADED INPUT" banner at the head of most reports
    (example-cockpit-repo-em memo, 2026-08-05 § 2, and the follow-up finding
    against ``eb1e8b5d76c8``).

    Negative spec — FAIL TOWARD SURFACING. Only a POSITIVELY identified
    not-in-this-repo failure is benign. Everything else is operational and
    MUST arm the latch, including:
      - ``result is None`` (git missing/unexecutable): the containment check
        below may still say "outside", but nothing here was actually
        determined by git, and a missing git is systemic by definition.
      - an unresolvable ``root`` ("not a git repository", a racing/removed
        git dir): containment is undecidable, so the failure is unclassified.
      - any unrecognised non-zero exit for a path that IS inside the
        worktree (index lock contention, a ref race, a signal death, a git
        version rejecting the argv): exactly the systemic class this latch
        exists to surface.
    An unclassifiable failure must never be quietly reclassified as benign to
    keep the report clean — a silent latch is only correct when we can say
    WHY.
    """
    if result is None:
        return False
    if _GIT_OUTSIDE_REPOSITORY_MARKER in (result.stderr or ""):
        return True
    return _path_is_outside_worktree(fpath, root) is True


def _relpath_failure_is_benign(exc: Exception, fpath: str, root: str) -> bool:
    """Did ``os.path.relpath(realpath(fpath), realpath(root))`` fail merely
    because ``fpath`` and ``root`` are on different Windows drives?

    On Windows, ``os.path.relpath`` raises ``ValueError`` whenever the two
    paths do not share a drive letter. That is exactly the routine
    path-outside-this-repo case — a ``C:``-drive scratchpad, ``~/.claude``, a
    sibling repo — when this repo's worktree lives on a non-system drive
    (e.g. ``X:``). It is not evidence of anything unexpected and must not
    arm :func:`_emit_normalize_diagnostic`'s latch — mirrors
    :func:`_ls_files_failure_is_benign`'s discrimination for the ``ls-files``
    arm (2026-08-07 finding: independently reported by example-doctrine-repo and
    carried as an undiagnosed item on a live handoff in this tree, both
    traced to the same false premise — that relpath failure is non-routine
    on every platform, which holds on POSIX but not on Windows).

    Determined from the actual path values (drive/anchor comparison), never
    from the exception's message text — message text is locale- and
    version-fragile.

    Negative spec: only a ``ValueError`` attributable to a drive mismatch is
    benign. An ``OSError`` is never classified benign here — that stays an
    operational failure and still arms the latch, exactly as before this
    function existed. On POSIX there are no drive letters
    (``os.path.splitdrive`` always returns ``""`` for both sides), so this
    predicate is unconditionally ``False`` there and POSIX behavior is
    byte-identical to before this function existed: every relpath failure
    still arms the latch.
    """
    if not isinstance(exc, ValueError):
        return False
    fdrive = os.path.splitdrive(os.path.realpath(fpath))[0]
    rdrive = os.path.splitdrive(os.path.realpath(root))[0]
    return fdrive.lower() != rdrive.lower()


def normalize_touch_path(path: str, cwd: Optional[str] = None) -> Optional[str]:
    """Normalize a ``touched.txt`` candidate entry to repo-relative, or signal
    "skip" via ``None`` if it is STILL absolute after the attempt.

    Extracted from :func:`touch` so every writer of a session's
    ``touched.txt`` (the PreToolUse hot path here, and
    ``coordinator_core.session.claims.self_claim``'s
    ``atomic_dedup_append`` call) shares ONE normalization dialect —
    two writers with two dialects is how the absolute-path-in-touched.txt
    defect (240 corroborated on-disk entries, example-doctrine-repo security-audit
    2026-07-31) happened in the first place. Do NOT re-inline a second
    copy of this normalization at a new call site; import and call this.

    Normalization: an incoming absolute path is normalized to
    repo-relative — first via ``git ls-files --full-name`` (tracked/staged
    files), else via ``os.path.relpath(realpath(path), realpath(root))``.
    ``realpath`` is applied to BOTH sides so that macOS ``/var → /private/var``
    symlink resolution does not cause a prefix mismatch (git resolves the
    repo root through ``/private/var`` while the incoming path may still use
    ``/var``). ``realpath`` canonicalises the existing prefix of a
    non-existent path, so untracked files are safe.

    Returns the normalized (repo-relative, or already-relative) path, or
    ``None`` if the path is STILL absolute after the normalization attempt
    (Python relpath failed, path outside repo) — the caller's fail-open skip
    signal; an absolute path in ``touched.txt`` corrupts the relative-path
    scope set (see :func:`compute_scope`). Never raises for an operational
    failure — mirrors ``touch()``'s own fail-open contract.

    Diagnostic calibration (2026-08-05, revised 2026-08-07): an ``ls-files``
    failure arms :func:`_emit_normalize_diagnostic`'s latch ONLY when
    :func:`_ls_files_failure_is_benign` cannot positively attribute it to
    "this pathspec is not in this repo". A path outside the worktree — a
    sibling repo, a settings home, a scratch dir — is the routine case, is
    resolved correctly by the relpath fallback below, and must NOT arm the
    latch; an unclassifiable failure still must.

    The relpath arm gets the SAME treatment now, via
    :func:`_relpath_failure_is_benign`. The original premise here — "relpath
    failing is not a routine condition on any platform" — was FALSE: on
    Windows, ``os.path.relpath`` raises ``ValueError`` whenever ``fpath`` and
    ``root`` sit on different drive letters, which is exactly the routine
    path-outside-this-repo case when this repo's worktree lives on a
    non-system drive (e.g. ``X:``) and a session touches a path on another
    drive (a ``C:``-drive scratchpad, ``~/.claude``, a sibling repo). That is
    not evidence of anything unexpected and must not arm the latch. On
    POSIX, where there are no drive letters, the predicate never fires and
    every relpath failure still arms the latch, unchanged. Either way the
    entry is still dropped (``rel = ""``, entry lost) — only whether the
    diagnostic arms is affected, never the fail-open skip behavior.
    """
    fpath = path
    if _is_absolute(fpath):
        rel = ""
        ls_files = _git_run(["ls-files", "--full-name", "--", fpath], cwd)
        ls_files_failed = ls_files is None or ls_files.returncode != 0
        if not ls_files_failed and ls_files.stdout:
            lines = ls_files.stdout.splitlines()
            rel = lines[0].strip() if lines else ""
        if not rel:
            root = core.git_root(cwd)
            # Classification is deferred to HERE rather than done at the call
            # above so it can reuse the `root` the fallback already needs —
            # the discrimination costs zero extra subprocesses, on a hot path
            # (`touch()` fires from a PreToolUse hook on every file write).
            if ls_files_failed and not _ls_files_failure_is_benign(
                ls_files, fpath, root
            ):
                _emit_normalize_diagnostic("git ls-files")
            if root:
                try:
                    rel = os.path.relpath(
                        os.path.realpath(fpath), os.path.realpath(root)
                    ).replace(os.sep, "/")
                except (OSError, ValueError) as exc:
                    rel = ""
                    if not _relpath_failure_is_benign(exc, fpath, root):
                        _emit_normalize_diagnostic("relpath")
                else:
                    # C2 (2026-08-05): relpath succeeding is not the same as
                    # resolving INSIDE the worktree — a sibling-directory
                    # path (e.g. "../docs/peer.md") resolves cleanly to a
                    # non-absolute, '../'-laden string, so the STILL-absolute
                    # guard below never fires for it and the escape would
                    # otherwise reach touched.txt unfiltered. Drop it here,
                    # at the writer, matching this function's own documented
                    # intent ("path outside repo -> SKIP") and the treatment
                    # classify_touch_entry's absolute branch already applies
                    # to this same rescued-value shape.
                    # Review: code-reviewer Finding 1 (2026-08-05) — os.path.relpath
                    # never returns "" (its floor is "."), so the `if rel else "."`
                    # guard here was unreachable dead code copy-pasted from the
                    # absolute branch above where the input CAN legitimately be "".
                    canonical = posixpath.normpath(rel)
                    if (
                        canonical in (".", "..")
                        or canonical.startswith("../")
                        or posixpath.isabs(canonical)
                    ):
                        rel = ""
        if rel:
            fpath = rel

    if _is_absolute(fpath):
        return None
    return fpath


@dataclass
class TouchEntryClassification:
    """Classification + transform result for one historical ``touched.txt``
    entry — see :func:`classify_touch_entry`. Field-compatible with (and the
    canonical home for what was previously) ``migrate_touched_prefix.
    EntryOutcome`` — that module now imports this dataclass rather than
    defining its own copy (see :func:`classify_touch_entry`'s docstring for
    why the two must not fork)."""

    original: str
    new_value: Optional[str]  # None means: drop this entry
    entry_class: str
    drop_reason: Optional[str] = None


def classify_touch_entry(
    entry: str, worktree_root: Path
) -> TouchEntryClassification:
    """Classify and transform one ``touched.txt`` entry against the SAME
    single canonical-value CONTAINMENT rule
    ``coordinator_core.ops.session.migrate_touched_prefix`` applies to the
    historical on-disk corpus (C6) — the canonical home for that transform,
    moved here (not left there) so :func:`compute_scope`'s Step 1/3/3b
    defensive read-side normalization (AC8) and the one-time migration (AC7)
    share ONE dialect rather than forking a second copy. ``migrate_touched_
    prefix.classify_entry``/``EntryOutcome`` are now thin re-exports of this
    function/dataclass — see that module's docstring negative-spec.

    For every non-absolute entry the property being enforced is
    CONTAINMENT, not ``../``-prefix depth: a leading-``../``-token-count
    (the prior branch structure) leaves a real hole, since an entry can
    escape the worktree without starting with a literal ``../`` at all
    (e.g. ``docs/../../peer/x.md``, ``./../peer/x.md``, a backslash
    separator the token loop never matches). This function instead computes
    ONE canonical value — ``posixpath.normpath(entry.replace("\\", "/"))``
    — and uses it for both the containment test and the return value; see
    the ``clean``/``dropped`` classes below. Converges with
    ``coordinator_core.ops.session.safe_commit_offer.
    _normalize_agent_touched_entry``, which already applies exactly this
    predicate (reject absolute forms up front, separator-normalize,
    ``posixpath.normpath``, reject ``.``/``..``/``../``-prefixed/absolute)
    for the same entry class in this repo today and itself returns the
    normalized value, not the raw entry.

    Do NOT re-inline a third copy of this transform at a new call site;
    import and call this.

    ``normalize_touch_path`` alone does NOT touch a ``../``-prefixed entry —
    it guards absoluteness only — so this function is the actual fix for
    that class, not a restatement of ``normalize_touch_path``.

    Entry classes (mutually exclusive):
      blank              — empty entry, passed through unchanged.
      clean              — for a non-absolute entry, one canonical value
                           ``canonical = posixpath.normpath(entry.replace(
                           "\\", "/"))`` is contained inside the worktree
                           (not ``.``/``..``, does not start with ``../``,
                           not absolute). Returns ``entry`` UNCHANGED if
                           ``canonical == entry``, otherwise returns
                           ``canonical`` — the same single derivation used
                           for the containment test above, never a second,
                           separately-computed comparison.
      absolute_rescued   — an absolute entry that ``normalize_touch_path``
                           (run with ``worktree_root`` as ``cwd``) resolves
                           to a clean, non-absolute, in-tree path: rewritten
                           to the rescued value.
      dropped            — an absolute entry ``normalize_touch_path`` still
                           cannot resolve (still absolute, or the relpath
                           attempt failed), or a non-absolute entry whose
                           canonical value escapes the worktree (is ``.``/
                           ``..``, starts with ``../``, or is absolute):
                           dropped. This collapses the former
                           ``stripped_one_level``/``multi_level`` split —
                           both retired — into one outcome: the property
                           being enforced is containment, not ``../``-prefix
                           depth (see below).

    Existence-on-disk is deliberately NOT a disambiguator anywhere in this
    transform — a stripped entry naming a path that no longer exists
    (deleted/renamed since) is expected and must not, by itself, cause a
    drop. The only disambiguator applied is worktree containment
    (``posixpath.normpath``) — ``normalize_touch_path`` itself has no
    containment check to lean on.

    Never widens: every non-``clean``/``blank`` outcome either rewrites to
    an in-tree relative path or drops the entry — it never fabricates an
    out-of-worktree entry into an in-repo one (the safety invariant this
    function exists to uphold; see AC8's non-negotiable invariants).

    History: this invariant was TRUE of the absolute branch above but FALSE
    of the former non-absolute (``stripped_one_level``/``multi_level``)
    branch structure — a single ``../`` strip's containment check was
    vacuous by construction, since a remainder could only still begin with
    ``../`` if the original had two-or-more leading tokens, and
    ``leading >= 2`` had already returned ``multi_level`` one branch above.
    shell-doc-ok: the double-backticked comparison above is a Python
    boolean expression quoted from this module's own code, not a shell
    version constraint.
    So the containment check there could never fire for a genuine
    sibling-directory touch; it was a check that could not fail. Two
    changes now uphold the claim for BOTH branches: the writer
    (``normalize_touch_path``, C2) stops producing ``../``-laden entries in
    the first place, and this function's collapsed ``clean``/``dropped``
    containment predicate (C1, computed once against the full canonical
    value rather than a single-strip remainder) drops any that still reach
    it on disk. After C1 the claim is true for both branches.
    """
    if entry == "":
        return TouchEntryClassification(
            original=entry, new_value=entry, entry_class="blank"
        )

    if _is_absolute(entry):
        rescued = normalize_touch_path(entry, cwd=str(worktree_root))
        if rescued is None or _is_absolute(rescued):
            return TouchEntryClassification(
                original=entry,
                new_value=None,
                entry_class="dropped",
                drop_reason=(
                    "absolute entry unresolved by normalize_touch_path "
                    "(still absolute, or relpath failed) even with the "
                    "corrected worktree root"
                ),
            )
        # normalize_touch_path's relpath fallback can succeed (never raises)
        # for a path genuinely OUTSIDE the worktree, producing a '../'-laden
        # relative result rather than an absolute one — its own contract
        # guards absoluteness only. Containment is this transform's own
        # check to add.
        rescued_normalized = posixpath.normpath(rescued) if rescued else "."
        if (
            rescued == ""
            or rescued_normalized in (".", "..")
            or rescued_normalized.startswith("../")
            or posixpath.isabs(rescued_normalized)
        ):
            return TouchEntryClassification(
                original=entry,
                new_value=None,
                entry_class="dropped",
                drop_reason=(
                    "absolute entry resolves out-of-tree after "
                    f"normalize_touch_path (rescued={rescued!r})"
                ),
            )
        return TouchEntryClassification(
            original=entry, new_value=rescued, entry_class="absolute_rescued"
        )

    # Non-absolute entry: one canonical value drives BOTH the containment
    # test and the return value — never two separately-computed
    # normalizations that could disagree (that was the exact defect in an
    # earlier wording of this ruling: comparing a RAW, non-separator-
    # normalized entry left a backslash shape like 'state\\x.md' compared
    # equal to itself and returned raw, un-normalized, on precisely the
    # shape this normalization exists to catch).
    canonical = posixpath.normpath(entry.replace("\\", "/"))
    if (
        canonical in (".", "..")
        or canonical.startswith("../")
        or posixpath.isabs(canonical)
    ):
        # Once the writer (C2) stops producing new '../' poison, a
        # surviving entry here is legacy residue of two byte-indistinguish-
        # able kinds: a genuine sibling-directory touch (hazard) or a
        # common-dir-poisoned real in-repo touch (benign). Neither the
        # touched.txt T/R event-log timestamp nor the worktree root
        # separates them — dropping narrows, and narrowing is the
        # direction this function's own contract declares safe.
        return TouchEntryClassification(
            original=entry,
            new_value=None,
            entry_class="dropped",
            drop_reason=(
                "entry escapes the worktree after normalization "
                f"(canonical={canonical!r})"
            ),
        )
    return TouchEntryClassification(
        original=entry,
        new_value=entry if canonical == entry else canonical,
        entry_class="clean",
    )


def _maximal_strip_peer_fallback(entry: str, worktree_root: Path) -> Optional[str]:
    """Directional fallback for the peer/``other_owner`` side ONLY (never the
    candidate side — see :func:`normalize_peer_claim_key`'s docstring for
    why the two sides cannot share one symmetric transform).

    Strips ALL leading ``../`` tokens (not just one) and, if the remainder
    is verifiably in-tree (same ``posixpath.normpath`` containment check
    :func:`classify_touch_entry` uses), returns it as a defensive
    ``other_owner`` key. Returns ``None`` if there is nothing to strip, the
    entry is absolute (no ``../``-shape fallback applies to that class —
    :func:`classify_touch_entry`'s own ``normalize_touch_path``-based rescue
    already tried and failed), or the maximally-stripped remainder still
    escapes the worktree.

    Over-claiming here (treating a path as a live peer claim when it was not
    actually one) can only ever REMOVE a candidate from ``my_scope`` — the
    safe failure mode for this side. It must never be applied to Step 1
    candidates, where the equivalent bias would WIDEN ``my_scope`` instead.
    """
    if _is_absolute(entry):
        return None
    rest = entry
    while rest.startswith("../"):
        rest = rest[3:]
    if rest == entry:
        return None  # nothing to strip — not a '../'-prefixed entry
    normalized = posixpath.normpath(rest) if rest else "."
    if (
        rest == ""
        or normalized in (".", "..")
        or normalized.startswith("../")
        or posixpath.isabs(normalized)
    ):
        return None
    return rest


def normalize_peer_claim_key(
    entry: str, worktree_root: Optional[Path]
) -> Optional[str]:
    """Directional counterpart to :func:`normalize_historical_touch_entry`,
    for the peer/``other_owner`` key space ONLY (Step 3, Step 3b of
    :func:`compute_scope`) — never for Step 1 candidates.

    Review: code-reviewer Finding 1 (sidecar
    ``coordinatorcode-reviewer-359b224b.md``) — the original AC8 transform
    applied ``classify_touch_entry``'s ``dropped`` (formerly ``multi_level``/
    unrescuable-``dropped``, now one collapsed outcome)
    "drop this entry" disposition SYMMETRICALLY to both sides, on the
    argument that a dropped candidate and a dropped peer claim for the same
    real file "cancel out" and the outcome degrades to a no-op. That holds
    only when both sides feed the transform the SAME string for the same
    real file, which was true for the ONE bug this slice fixes (a constant,
    single-``../``-level poisoning depth) but is not a structural invariant:
    a future writer regression with a DIFFERENT depth shape could poison
    only the peer's entry (e.g. ``../../foo.py``) while this session's own
    candidate for the same real file (``foo.py``) stays clean. Symmetric
    dropping then silently vanishes the peer's claim from ``other_owner``
    while the clean candidate survives — the exact "this session sweeps a
    live peer's file into its own commit" failure C7 exists to prevent.

    The two sides must round in OPPOSITE directions to stay safe:
      - Candidate side (Step 1, ``normalize_historical_touch_entry``):
        dropping narrows ``my_scope`` — the safe direction. Unchanged.
      - Peer side (this function): a claim must never silently vanish just
        because the one-level strip didn't resolve it. Falls back to
        :func:`_maximal_strip_peer_fallback` (strip ALL leading ``../``) and,
        if THAT resolves to an in-tree path, uses it as a defensive
        ``other_owner`` key. A spurious extra key here can only ever REMOVE
        a path from ``my_scope`` (the consumer-safe direction for this
        allow-list), so over-claiming is acceptable; under-claiming
        (silently dropping a live peer's real claim) is not.

    Returns ``entry`` unchanged (pass-through, matching
    :func:`normalize_historical_touch_entry`'s degrade) if ``worktree_root``
    is falsy — the containment check cannot run without a resolved root.
    """
    if not worktree_root:
        return entry
    classification = classify_touch_entry(entry, worktree_root)
    if classification.new_value is not None:
        return classification.new_value
    if _is_absolute(entry):
        # Already tried normalize_touch_path's git-ls-files/realpath rescue
        # inside classify_touch_entry; no '../'-shape fallback applies to an
        # absolute entry that still failed to resolve.
        return None
    return _maximal_strip_peer_fallback(entry, worktree_root)


def normalize_historical_touch_entry(
    entry: str, worktree_root: Optional[Path]
) -> Optional[str]:
    """Thin read-side wrapper around :func:`classify_touch_entry` for
    callers (:func:`compute_scope`'s Step 1/3/3b, AC8) that only need the
    transformed value, not the full classification record
    ``migrate_touched_prefix`` reports.

    Returns ``entry`` UNCHANGED (a no-op pass-through, not a drop) if
    ``worktree_root`` is falsy/``None`` — the containment check the
    transform depends on cannot run without a resolved worktree root, and
    the safest degrade with no root to check against is to leave the entry
    exactly as :func:`compute_scope`'s pre-existing (pre-AC8) callers would
    have seen it, not to silently drop every candidate/claim this call.

    Otherwise returns ``classify_touch_entry(...).new_value`` — ``None``
    signals "drop this entry" to the caller (multi-level or unrescuable-
    absolute), the SAME narrowing-only disposition ``classify_touch_entry``
    documents.
    """
    if not worktree_root:
        return entry
    return classify_touch_entry(entry, worktree_root).new_value


def parse_touch_event(line: str) -> Tuple[str, Optional[datetime], str]:
    """Parse one touched.txt line into (verb, timestamp, path).

    Recognizes 'T <ISO-8601 UTC microsecond> <path>' and 'R <ISO-8601 UTC
    microsecond> <path>' via str.split(None, 2) — never str.split(' ') or a
    two-field split, which would corrupt a path containing a space.

    Fail-safe (AC3c): a line that is NOT exactly '<verb> <timestamp> <path>'
    with verb in {'T','R'} and a timestamp datetime.fromisoformat() can parse
    — including every pre-existing bare '<path>'-only legacy line, a
    truncated line, or an unknown verb token — is reported as
    ('T', None, <line>) i.e. CLAIMED, never RELEASED. The bare-path legacy
    case is detected by: split(None, 2) yields exactly ONE token (no verb,
    no timestamp) -> that one token IS the path.

    timestamp is None for the fail-safe/legacy case ('unknown time'). Sort
    ordering (used by the projection policies, not by this function) must
    treat None as EARLIER than every real timestamp — see
    `_TOUCH_EVENT_EPOCH_MIN`, the concrete sentinel: `datetime.min.replace(
    tzinfo=timezone.utc)`. `datetime.min` compares less than any real,
    timezone-aware UTC instant by construction (it is the earliest
    representable `datetime`), which is exactly the "ordered EARLIER than
    any stamped event" contract § The frozen record format requires — a
    legacy claim can never outrank a real timestamped R release by sorting
    later than it.
    """
    stripped = line.rstrip("\n")
    parts = stripped.split(None, 2)
    if len(parts) == 1:
        return ("T", None, parts[0])
    if len(parts) != 3:
        return ("T", None, stripped)
    verb, ts_str, path = parts
    if verb not in ("T", "R"):
        return ("T", None, stripped)
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return ("T", None, stripped)
    # A naive timestamp is fail-safe CLAIMED rather than a parsed event: every
    # consumer compares these against timezone-aware instants, and mixing the
    # two raises TypeError at comparison time — inside the projection, i.e.
    # past the point this function's own never-raise contract can absorb it.
    # Rejecting here keeps the failure in the one place that fails safe.
    if ts.tzinfo is None:
        return ("T", None, stripped)
    return (verb, ts, path)


# Internal sort-key helper the projection policies below both need —
# NOT part of the two-function public policy surface (AC12 forbids a THIRD
# named policy entry point, but a private timestamp-comparator helper is not
# a policy — it makes no CLAIMED/RELEASED decision itself).
_TOUCH_EVENT_EPOCH_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _touch_event_sort_key(ts: Optional[datetime]) -> datetime:
    return ts if ts is not None else _TOUCH_EVENT_EPOCH_MIN


def format_touch_event(verb: str, path: str, when: Optional[datetime] = None) -> str:
    """Format one event line: '<verb> <ISO-8601 UTC, microsecond> <path>'.

    verb MUST be 'T' or 'R' — raises ValueError otherwise (this is a NEW
    writer-side function with no legacy-compat obligation, unlike the
    parser). `when` defaults to `datetime.now(timezone.utc)` when None.
    A NAIVE `when` also raises ValueError (Review: code-reviewer Finding 2,
    2026-08-03) — silently formatting a naive instant with a trailing 'Z'
    would assert UTC about a value that may be local time or anything else,
    the exact asymmetry `parse_touch_event` was built to reject on the read
    side rather than let a tz-mixing TypeError escape into the projection.
    Space-delimited, never tab (example-doctrine-repo's schema-contract test asserts
    `assertNotIn("\\t", lines[0])`). Path last, unescaped — a path
    containing a literal space is legal here because the READER splits with
    split(None, 2), not on structure requiring the path be escaped.
    """
    if verb not in ("T", "R"):
        raise ValueError(f"format_touch_event: verb must be 'T' or 'R', got {verb!r}")
    if when is not None and when.tzinfo is None:
        raise ValueError(
            f"format_touch_event: when must be timezone-aware, got naive {when!r}"
        )
    ts = when if when is not None else datetime.now(timezone.utc)
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    # NOT `ts.isoformat()`: it renders the offset as '+00:00' rather than the
    # 'Z' § The frozen record format specifies, and it omits the microsecond
    # field entirely when it happens to be zero. Both matter — example-doctrine-repo re-pins a
    # schema-contract test against these exact bytes, and the projection's
    # mtime comparison is `>=` precisely because sub-second resolution is what
    # separates a peer's post-release write from a co-toucher's.
    return f"{verb} {ts.strftime('%Y-%m-%dT%H:%M:%S.%f')}Z {path}"


def _last_verb_map(lines: List[str]) -> Dict[str, str]:
    """Private plumbing (AC12 — not a third policy entry point; makes no
    CLAIMED/RELEASED decision itself): scan raw ``touched.txt`` lines and
    keep, per path, the verb of its LAST event in file order.

    Review: code-reviewer Finding 1 (sidecar
    ``coordinatorcode-reviewer-5c643f30.md``, plan
    ``docs/plans/2026-08-03-scope-guard-peer-claim-release.md``) — this scan
    was independently re-written in three places (``project_self_scope``'s
    own inline loop, ``coordinator-safe-commit::do_scoped``'s ``last_verb``
    local). Extracted here so both share one dialect; a caller filters the
    returned mapping for whichever verb it needs (``project_self_scope``
    keeps ``"T"``, ``do_scoped`` keeps ``"R"`` for its released-paths
    diagnostic).
    """
    last_verb: Dict[str, str] = {}
    for line in lines:
        if not line:
            continue
        verb, _ts, path = parse_touch_event(line)
        last_verb[path] = verb
    return last_verb


def _challenger_t_events(lines: List[str]) -> Dict[str, datetime]:
    """Private plumbing (AC12 — not a third policy entry point; makes no
    CLAIMED/RELEASED decision itself): scan raw ``touched.txt`` lines and
    keep, per path, the LATEST real-timestamped ``T`` event — the
    "challenger evidence" :func:`project_peer_claims`'s ``challenger_t_events``
    argument consumes (§ Decision 3 amendment — inference loses to
    evidence). A legacy/fail-safe ``T`` (unknown time, ``ts is None``)
    carries no evidence of post-dating anything and is excluded, matching
    every existing call site.

    Review: code-reviewer Finding 1 (sidecar
    ``coordinatorcode-reviewer-5c643f30.md``, plan
    ``docs/plans/2026-08-03-scope-guard-peer-claim-release.md``) — this scan
    was independently re-written in three places (``compute_scope``'s own
    inline loop and ``coordinator-safe-commit::do_blanket``'s
    ``challenger_t_events`` local, both over each caller's own real-
    timestamped ``T`` events). Extracted here so all three share one
    dialect and a future tie-breaking/parse-behavior fix lands in one
    place.
    """
    events: Dict[str, datetime] = {}
    for line in lines:
        if not line:
            continue
        verb, ts, path = parse_touch_event(line)
        if verb == "T" and ts is not None:
            existing_ts = events.get(path)
            if existing_ts is None or ts > existing_ts:
                events[path] = ts
    return events


def project_peer_claims(
    lines: List[str],
    path_mtimes: Dict[str, float],
    challenger_t_events: Optional[Dict[str, datetime]] = None,
) -> Dict[str, datetime]:
    """Peer-facing projection over ONE peer's (or one agent-dir's) raw
    touched.txt lines.

    Returns {path: last_T_sort_key} for every path currently projected
    CLAIMED — i.e. its last event is T, OR its last event is R but the path
    is currently dirty with mtime >= that R's timestamp (path_mtimes[path],
    the FILE's own on-disk mtime — NOT touched.txt's mtime) AND no entry in
    challenger_t_events for that path sorts later than that R (§ Decision 3
    amendment — inference loses to evidence). A path absent from
    path_mtimes is treated as not-currently-dirty (an R for it never
    re-projects to CLAIMED on mtime grounds alone).

    challenger_t_events (EM ruling 2026-08-03, item 1: option (a)) ranges
    over {peer_sid, sid} only — the peer whose record is being projected
    (already fully represented by `lines` itself: a peer's OWN later T for
    a path it also released simply becomes that path's last event, so no
    separate consultation of the peer's own claim is needed here) and the
    calling session `sid` (the actual source of every entry in this dict —
    `sid`'s own real-timestamped T events for a path, keyed by path). A
    third session's claim is NOT ranged over: it independently surfaces via
    compute_scope's own per-peer Step-3 scan, so the calling session stays
    correctly blocked either way (cosmetic mislabeling only, not a
    scope-widen) — see the plan's EM ratification for the full argument
    against a global cross-corpus pre-pass.

    Never RELEASED on any parse failure per path (AC3c, AC3b): silence
    (path never appears as a T-less, non-dirty R) withholds by simply not
    appearing in the returned dict — the caller's existing
    `other_owner.get(candidate)` lookup already treats "absent" as
    "no claim", so returning nothing IS the safe default, not an extra
    branch to add.
    """
    challenger = challenger_t_events or {}
    last_event: Dict[str, Tuple[str, Optional[datetime]]] = {}
    for line in lines:
        if not line:
            continue
        verb, ts, path = parse_touch_event(line)
        last_event[path] = (verb, ts)

    claimed: Dict[str, datetime] = {}
    for path, (verb, ts) in last_event.items():
        if verb == "T":
            claimed[path] = _touch_event_sort_key(ts)
            continue
        # verb == "R" — parse_touch_event only reports "R" when it actually
        # matched the timestamped pattern, so `ts` is always a real,
        # non-None datetime here (never the legacy/fail-safe branch).
        mtime = path_mtimes.get(path)
        if mtime is None:
            continue
        if mtime < ts.timestamp():
            continue
        challenger_ts = challenger.get(path)
        if challenger_ts is not None and challenger_ts > ts:
            continue
        claimed[path] = _touch_event_sort_key(ts)
    return claimed


def project_self_scope(lines: List[str]) -> Set[str]:
    """Self-facing projection over the CALLING session's own raw
    touched.txt lines (compute_scope Step 1, claims.my_agent_touched,
    safe_commit_offer, both coordinator-safe-commit do_* paths,
    baton_assemble._compute_dirty_tree_attribution).

    Returns the set of paths whose last event is T (including every legacy
    bare-line path, which parses to T at unknown time — still T). A path
    whose last event is R is RELEASED and excluded — absent a real T
    post-dating it. NEVER applies the mtime re-claim `project_peer_claims`
    applies: this is deliberately the arm that must not widen `my_scope`
    (§ Decision 3 table, "self-facing" row).
    """
    last_verb = _last_verb_map(lines)
    return {path for path, verb in last_verb.items() if verb == "T"}


def _collect_peer_path_mtimes(
    lines: List[str], root: Optional[str]
) -> Dict[str, float]:
    """Private plumbing for `project_peer_claims`'s `path_mtimes` argument —
    NOT a third policy entry point (AC12 forbids that; this makes no
    CLAIMED/RELEASED decision itself). Reads the FILE's own on-disk mtime
    (never touched.txt's mtime) for every path named in a peer's raw
    touched.txt lines, keyed on the parsed PATH FIELD (never the raw
    verb+timestamp+path line — see `parse_touch_event`). A path whose file
    does not currently exist on disk is left absent from the returned dict,
    matching `project_peer_claims`'s own "absent means not-currently-dirty"
    contract.
    """
    mtimes: Dict[str, float] = {}
    for line in lines:
        if not line:
            continue
        _, _, path = parse_touch_event(line)
        if not path or path in mtimes:
            continue
        abs_path = f"{root}/{path}" if root else path
        p = Path(abs_path)
        if p.is_file():
            try:
                mtimes[path] = p.stat().st_mtime
            except OSError:
                continue
    return mtimes


def touch(sid: str, path: str, cwd: Optional[str] = None) -> None:
    """Port of ``cs_touch <session_id> <path>``: append a repo-relative file
    path to this session's ``touched.txt`` as a ``T`` event (last-event
    dedup — a path whose last event is already ``T`` is skipped; a path
    whose last event is ``R`` or absent gets a fresh ``T``), then refresh
    ``last_activity`` in ``meta.json``.

    HOT path. Live callers are the engine's self-report scope-touch contract
    (``ipc.py``'s ``_SCOPE_TOUCH_PATHS_KEY`` block, recorded by
    ``dispatch_message``) and ``cli_entry.py`` — so this fires on every
    sanctioned-mutating engine op that declares a write, NOT on the PreToolUse
    Edit/Write path. That path runs ``hooks.track_touched_files``, which
    deliberately does not refresh ``last_activity`` (see its own note: the
    meta.json write costs ~36ms on Windows). Negative spec: do not re-describe
    this as a per-tool-call heartbeat — the distinction is what decides whether
    a session with no Bash call and no commit reads as live.
    No jq, no subshells beyond the git-root lookup. Fail-open: always returns
    ``None`` (bash ``return 0``), never raises for an operational failure.

    Normalization: an incoming absolute path is
    normalized to repo-relative — first via ``git ls-files --full-name``
    (tracked/staged files), else via ``os.path.relpath(realpath(path),
    realpath(root))``. ``realpath`` is applied to BOTH sides so that macOS
    ``/var → /private/var`` symlink resolution does not cause a prefix
    mismatch (git resolves the repo root through ``/private/var`` while the
    incoming path may still use ``/var``). ``realpath`` canonicalises the
    existing prefix of a non-existent path, so untracked files are safe.

    Guard: if the path is STILL absolute after the
    normalization attempt (Python relpath failed, path outside repo), SKIP
    it — an absolute path in ``touched.txt`` corrupts the relative-path scope
    set. The guard is on a STILL-absolute path, NOT on an empty one.

    Raises ``ValueError`` if ``sid`` or ``path`` is empty (bash
    ``${1:?}``/``${2:?}`` required-arg contract).
    """
    if not sid:
        raise ValueError("session_id required")
    if not path:
        raise ValueError("file_path required")

    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return  # not in a git repo — fail-open (bash `|| return 0`)

    normalized = normalize_touch_path(path, cwd)
    if normalized is None:
        # Guard: skip if STILL absolute after normalization (fail-open).
        return
    fpath = normalized

    touched = os.path.join(sdir, "touched.txt")

    # Create session dir / backfill meta.json on touch if init() was skipped
    # (fail-safe). Guard on meta.json presence, not just the dir: another
    # bookkeeping writer (push cursor, session-shape) can create the dir first,
    # leaving meta.json — and the Layer-1 liveness signal — unwritten (defect A,
    # 2026-07-24). core.init is idempotent and backfills into an existing dir.
    if not os.path.isdir(sdir) or not os.path.isfile(os.path.join(sdir, "meta.json")):
        try:
            core.init(sid, cwd=cwd)
        except Exception as exc:
            # fail-safe — touch() must not block on a failed lazy init();
            # surface it for debugging since this is not an expected path.
            print(
                f"cs_touch: lazy core.init() failed for session {sid} "
                f"(non-fatal): {exc}",
                file=sys.stderr,
            )

    # Event-aware dedup (AC8): a path whose LAST event is already T is
    # skipped (unchanged claim); a path whose last event is R (released) or
    # with no event at all gets a fresh T — an edit after a release must not
    # be silently unclaimed. Scan backwards and stop at the first matching
    # line rather than parsing the whole log (hot path — fires on every
    # tool-call file write).
    if os.path.isfile(touched):
        try:
            existing = Path(touched).read_text(encoding="utf-8").splitlines()
        except OSError:
            existing = []
        for line in reversed(existing):
            if not line:
                continue
            verb, _ts, line_path = parse_touch_event(line)
            if line_path == fpath:
                if verb == "T":
                    return
                break

    try:
        with open(touched, "a", encoding="utf-8") as fh:
            fh.write(format_touch_event("T", fpath) + "\n")
    except OSError as exc:
        # fail-open — touch() must never block a tool call on a write
        # failure; surface it for debugging since this is not expected.
        print(
            f"cs_touch: failed to append {fpath!r} to {touched} "
            f"(non-fatal): {exc}",
            file=sys.stderr,
        )
        return

    # Update last_activity (best-effort, no failure on error).
    core.update_meta_field(sdir, "last_activity", core.now_iso())
    return


def touch_written_path(session_id: str, rel_path: str, cwd: Optional[str] = None) -> None:
    """Record a touch-claim for a path an in-process writer just wrote,
    attributed to the RAW ``session_id`` as passed -- never ``agent_id``
    (a spawned subagent's own id) and never a path-sanitizer's lossy
    directory-leaf (see docs/plans/2026-08-05-in-process-writers-declare-
    their-writes.md § Key mechanism facts). A spawned subagent has no
    session dir of its own and its wrap ceremony never commits, so the
    DISPATCHING session's wrap is the only one that can ever claim the
    file it wrote on the subagent's behalf -- it owns the claim, and this
    function must be called with that raw id, never a re-resolved or
    re-sanitized substitute, and never the CURRENT session consulted
    afresh.

    Guard: skips silently when ``core.session_dir(session_id, cwd)`` does
    not already exist on disk -- the phantom-live-peer guard. ``touch()``
    lazily calls ``core.init()``, which would otherwise materialize a
    live peer for a session id that was never actually spawned (the
    scenario ``coordinator_core/ipc.py``'s F1 comment documents),
    reachable here because a caller may resolve ``session_id``/``cwd``
    from elsewhere (e.g. an explicit ``--cwd`` flag) rather than from the
    live session itself. This is the entire reason this helper exists
    rather than calling ``touch()`` directly at each write site -- do not
    drop it when adapting this function.

    Callers MUST invoke this only AFTER the write it is claiming has
    actually succeeded -- never speculatively, and never for a write that
    was skipped (e.g. an idempotent re-open of an existing file).
    """
    session_dir = core.session_dir(session_id, cwd)
    if not session_dir or not os.path.isdir(session_dir):
        return
    touch(session_id, rel_path, cwd)


def _tree_relocation_claim_pairs(
    sdir: str, src_norm: str, dst_norm: str
) -> List[Tuple[str, str]]:
    """Compute ``(old_path, new_path)`` restatement pairs for every path
    CURRENTLY self-T-claimed under the directory ``src_norm`` — the
    "directory" half of :func:`_restate_tree_claims`, split out so it can be
    unit-tested against a synthetic ``claimed`` set without a real session
    dir.

    NO FILESYSTEM TRAVERSAL happens here, despite the "tree"/"directory"
    naming — this is a string-prefix FILTER over the session's own already-
    claimed set read from ``touched.txt`` (no ``os.walk``/``scandir`` of the
    moved directory, ever). Review: code-reviewer Nit (2026-08-06, sidecar
    coordinatorcode-reviewer-5e45cd5a.md) — stated up front because it is
    precisely why symlink/deep-tree/unreadable-subdirectory hazards do not
    apply to this function at all, and the repeated "walk" language
    elsewhere in this module's docstrings could otherwise invite a reader to
    assume real ``os.walk`` semantics (symlink-following, permission errors)
    that never occur here.

    Reads this session's ``touched.txt`` exactly once and projects it
    through :func:`project_self_scope` — the SAME self-facing "last event is
    T" projection :func:`relocate_touched_path`'s single-path form already
    uses, never the peer-facing mtime-reclaim one.

    A claimed path qualifies only if it starts with ``src_norm + "/"``
    (never a bare ``==`` on ``src_norm`` itself — that is a directory entry,
    not a file claim, and touch-claims are keyed on files only) — this also
    correctly excludes a sibling whose name merely shares the prefix
    (``state/x-backup/f.md`` does not match a move of ``state/x``, since the
    comparison prefix is ``state/x/``, not ``state/x``). The matched
    remainder (everything after that prefix) is re-based onto ``dst_norm``
    unchanged, preserving the relative structure of the moved tree.

    Returns an empty list — writing NO events anywhere — when nothing under
    ``src_norm`` is currently claimed; the caller must not read an empty
    result as an error, only as "this tree has nothing to restate."
    """
    touched_path = os.path.join(sdir, "touched.txt")
    if not os.path.isfile(touched_path):
        return []
    try:
        lines = Path(touched_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    claimed = project_self_scope(lines)
    prefix = src_norm.rstrip("/") + "/"
    dst_base = dst_norm.rstrip("/")
    pairs: List[Tuple[str, str]] = []
    for path in claimed:
        if not path.startswith(prefix):
            continue
        suffix = path[len(prefix):]
        if not suffix:
            continue
        pairs.append((path, f"{dst_base}/{suffix}"))
    return pairs


def _restate_tree_claims(
    session_id: str,
    src_rel: str,
    dst_rel: str,
    cwd: Optional[str],
    sdir: str,
    root: Optional[str],
) -> None:
    """Directory-aware claim restatement — filter the session's own already-
    claimed set (via :func:`_tree_relocation_claim_pairs`; no filesystem
    traversal, see that function's docstring) and re-declare a claim for
    every currently-claimed descendant onto its post-move path. Performs NO
    filesystem move of any kind; the caller (either
    :func:`relocate_touched_path`'s directory branch, or the public
    :func:`restate_touched_tree`) is responsible for the physical relocation,
    before OR after this call returns — see :func:`restate_touched_tree`'s
    docstring for why call-ordering relative to the move does not matter
    here the way it does for the single-path form.

    Assumes the phantom-live-peer guard has ALREADY been checked by the
    caller (``sdir`` is confirmed to already exist) — this function does not
    re-check it, matching :func:`_tree_relocation_claim_pairs`'s own
    "private plumbing" framing; it is not a second, independently-reachable
    policy entry point.

    Normalizes ``src_rel``/``dst_rel`` via :func:`normalize_touch_path`
    ONLY (never :func:`classify_touch_entry` — same write-side/read-side
    split :func:`relocate_touched_path` documents). Returns early, writing
    nothing, if either fails to resolve to an in-tree path, or if the
    resolved destination fails :func:`_dst_is_claimable` (the ``.git/``/
    ``.archive/`` carve-out) — checked ONCE against the top-level
    destination, since every descendant shares its first path segment.

    Per descendant, the destination-pre-exists carve-out is re-applied
    individually (never inherited from a parent-level check): a descendant
    whose specific post-move path already exists on disk is left unclaimed,
    exactly as the single-path form declines to claim a pre-existing
    ``dst_rel`` — a partial prior merge into the destination tree must not
    be overwritten by a fabricated claim.

    Claims via :func:`touch_written_path` — inheriting its phantom-live-peer
    guard rather than reimplementing it — appending ``T(new)`` for each
    qualifying descendant and writing NO ``R`` event for any ``old`` path:
    every stale claim is left standing for
    :func:`release_committed_claims` to retire once the tree's deletions
    land in a commit, the same invariant the single-path form relies on.
    """
    src_norm = normalize_touch_path(src_rel, cwd)
    if src_norm is None:
        return
    dst_norm = normalize_touch_path(dst_rel, cwd)
    if dst_norm is None or not _dst_is_claimable(dst_norm):
        return
    for old_path, new_path in _tree_relocation_claim_pairs(sdir, src_norm, dst_norm):
        new_abs = (
            os.path.join(root, new_path)
            if root and not _is_absolute(new_path)
            else new_path
        )
        if os.path.exists(new_abs):
            continue
        touch_written_path(session_id, new_path, cwd)


def restate_touched_tree(
    session_id: str, src_rel: str, dst_rel: str, cwd: Optional[str] = None
) -> None:
    """Public, MOVE-FREE counterpart to :func:`relocate_touched_path` for a
    caller whose own directory relocation is NOT a plain move it can cede to
    ``shutil.move`` — e.g. a bespoke crash-safe rename protocol (a
    dest-exists check, a tree-signature comparison to detect and resume a
    crashed prior attempt, an EXDEV ``copytree``+``rename``+``rmtree``
    fallback the caller manages itself). Such a caller cannot hand its move
    to :func:`relocate_touched_path` (that function IS a move — it always
    performs one), so this exposes JUST the claim-restatement half.

    Call this ONCE, for the ``src_rel``/``dst_rel`` pair, BEFORE the
    physical relocation begins — on EITHER leg of a two-leg protocol (a
    plain rename, or an EXDEV fallback). Claim restatement is a pure
    ``touched.txt`` read+append operation over path STRINGS; it has no
    dependency on how — or whether yet — the directory was physically
    moved, so one call ahead of whichever leg actually runs is sufficient;
    there is no need to call this once per physical step, and no need to
    call it again after a successful move.

    Same invariants as :func:`relocate_touched_path`'s directory branch,
    since both route through the same private :func:`_restate_tree_claims`:
    claims only a descendant currently T-claimed by THIS session, appends
    ``T(new)`` via :func:`touch_written_path` (inheriting its phantom-live-
    peer guard), writes NO ``R`` event ever, respects the destination
    carve-outs (``_dst_is_claimable``, per-descendant pre-existence), and
    writes nothing at all when the tree holds no claim or the phantom-live-
    peer guard trips (``core.session_dir`` not already an existing
    directory).

    Raises ``ValueError`` if ``session_id``, ``src_rel``, or ``dst_rel`` is
    empty — mirrors :func:`relocate_touched_path`'s required-arg contract.
    Never raises for an operational bookkeeping failure (an unreadable
    ``touched.txt`` degrades to "nothing claimed"), and never touches the
    filesystem beyond ``touched.txt``/``meta.json`` — it performs no move,
    unlike its move-performing sibling.
    """
    if not session_id:
        raise ValueError("session_id required")
    if not src_rel:
        raise ValueError("src_rel required")
    if not dst_rel:
        raise ValueError("dst_rel required")

    sdir = core.session_dir(session_id, cwd)
    if not sdir or not os.path.isdir(sdir):
        return  # phantom-live-peer guard

    root = core.git_root(cwd)
    _restate_tree_claims(session_id, src_rel, dst_rel, cwd, sdir, root)


def _restate_single_path_claim(
    session_id: str,
    src_rel: str,
    dst_rel: str,
    cwd: Optional[str],
    sdir: str,
    root: Optional[str],
) -> None:
    """Shared single-path (move-free) claim-restatement body for
    :func:`restate_touched_path` and :func:`relocate_touched_path`'s own
    single-path branch — the ONE place this family's "is ``src_rel`` itself
    currently self-T-claimed, then claim ``dst_rel``" logic lives, so a
    future third caller extends this instead of forking a fourth copy (the
    exact drift :func:`restate_touched_path`'s own docstring calls out: a
    prior private ops-module copy of this same logic, since deleted).

    Assumes the phantom-live-peer guard has ALREADY been checked by the
    caller (``sdir`` is confirmed to already exist) — this function does
    not re-check it, matching :func:`_restate_tree_claims`'s own "private
    plumbing" framing; it is not a second, independently-reachable policy
    entry point.

    Normalizes ``src_rel``/``dst_rel`` via :func:`normalize_touch_path`
    ONLY (never :func:`classify_touch_entry` — write-side, not the
    read-side historical-entry migration). Writes nothing when: ``src_rel``
    fails to normalize; ``src_rel`` does not project as currently
    self-T-claimed (:func:`project_self_scope` over this session's own
    ``touched.txt``); ``dst_rel`` fails to normalize or fails
    :func:`_dst_is_claimable` (the ``.git/``/``.archive/`` carve-out); or
    the resolved destination already exists on disk (a partial prior merge
    must not be overwritten by a fabricated claim).

    Claims via :func:`touch_written_path` — inheriting its phantom-live-peer
    guard rather than reimplementing it — appending ``T(dst)`` only. Writes
    NO ``R`` event for ``src_rel``: the stale claim is left standing for
    :func:`release_committed_claims` to retire once the deletion half of
    the caller's own relocation lands in a commit. Performs NO filesystem
    move of any kind — that is entirely the caller's responsibility.
    """
    src_norm = normalize_touch_path(src_rel, cwd)
    if src_norm is None:
        return
    touched_path = os.path.join(sdir, "touched.txt")
    was_claimed = False
    if os.path.isfile(touched_path):
        try:
            lines = Path(touched_path).read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        was_claimed = src_norm in project_self_scope(lines)
    if not was_claimed:
        return

    dst_norm = normalize_touch_path(dst_rel, cwd)
    if dst_norm is None or not _dst_is_claimable(dst_norm):
        return
    dst_abs = (
        os.path.join(root, dst_norm)
        if root and not _is_absolute(dst_norm)
        else dst_norm
    )
    if os.path.exists(dst_abs):
        return
    touch_written_path(session_id, dst_norm, cwd)


def restate_touched_path(
    session_id: str, src_rel: str, dst_rel: str, cwd: Optional[str] = None
) -> None:
    """Public, MOVE-FREE counterpart to :func:`relocate_touched_path` for a
    single file (or any single path), the natural sibling of
    :func:`restate_touched_tree` (that function's directory-tree
    counterpart) — for a caller whose own relocation of ONE path is not a
    plain move it can cede to :func:`relocate_touched_path` (that function
    IS a move — it always performs one), e.g. a bespoke crash-safe rename
    protocol (a dest-exists check, a resumable per-step classification, an
    error path that must not have mutated anything) that owns the actual
    ``os.rename``/filesystem-move call itself.

    Call this ONCE, for the ``src_rel``/``dst_rel`` pair, BEFORE the
    physical relocation begins — matching :func:`restate_touched_tree`'s own
    call-ordering contract: claim restatement is a pure ``touched.txt``
    read+append operation over path STRINGS, with no dependency on how — or
    whether yet — the path was physically moved.

    Restates ONLY when ``src_rel`` is currently T-claimed by THIS session —
    never fabricates a claim on a path this session never held, which is
    how one session's wrap would sweep a peer's file. Appends ``T(dst_rel)``
    ONLY, via :func:`touch_written_path` (inheriting its phantom-live-peer
    guard rather than reimplementing it) — writes NO ``R`` event; the old
    claim on ``src_rel`` is left standing for
    :func:`release_committed_claims` to retire once the deletion half of
    the caller's own relocation lands in a commit. Honours the same
    destination carve-outs :func:`relocate_touched_path` applies: a
    pre-existing destination, or one that resolves outside claimable space
    (``.git/``, ``.archive/``, outside the worktree), is left unclaimed.
    Performs NO filesystem move of any kind — that is entirely the caller's
    own responsibility, before or after this call returns.

    Raises ``ValueError`` if ``session_id``, ``src_rel``, or ``dst_rel`` is
    empty — mirrors :func:`restate_touched_tree`'s required-arg contract.
    Never raises for an operational bookkeeping failure (an unreadable
    ``touched.txt`` degrades to "nothing claimed"), and never touches the
    filesystem beyond ``touched.txt``/``meta.json``. Writes nothing at all
    when the phantom-live-peer guard trips (``core.session_dir`` not
    already an existing directory).
    """
    if not session_id:
        raise ValueError("session_id required")
    if not src_rel:
        raise ValueError("src_rel required")
    if not dst_rel:
        raise ValueError("dst_rel required")

    sdir = core.session_dir(session_id, cwd)
    if not sdir or not os.path.isdir(sdir):
        return  # phantom-live-peer guard

    root = core.git_root(cwd)
    _restate_single_path_claim(session_id, src_rel, dst_rel, cwd, sdir, root)


def relocate_touched_path(
    session_id: str, src_rel: str, dst_rel: str, cwd: Optional[str] = None
) -> None:
    """Move a file OR DIRECTORY this session may have T-claimed (directly,
    or via one or more claimed descendants) from ``src_rel`` to ``dst_rel``,
    re-declaring the affected claim(s) on their NEW path(s) — the touch-claim
    counterpart of a rename/relocation, so that renaming something this
    session wrote does not silently strand its claim(s) on a path that no
    longer exists (plan
    ``docs/plans/2026-08-06-relocation-re-declares-the-touch-claim.md``, C1,
    widened 2026-08-06 to close the directory residual tracked at
    ``state/bug-backlog/2026-08-06-relocating-a-directory-strands-touch-cla-
    3878d0fc0ca0.yaml``).

    WIDENING, not a new function: this is the SAME entry point five
    call sites already use for single-file relocation, and the single-file
    behavior below is UNCHANGED byte-for-byte — same checks, same order,
    same ``touch_written_path`` call, same unconditional trailing
    ``shutil.move``. The only new behavior is an ADDITIONAL branch, taken
    only when ``src_abs`` is a real (non-symlink) directory at call time:
    every currently-claimed path NESTED under it is restated onto its
    analogous post-move path (:func:`_restate_tree_claims`) instead of the
    single-path "is ``src_rel`` itself claimed" check. A new, separate
    function would have forced every one of the five existing callers to
    branch on file-vs-directory themselves before choosing which helper to
    call — this way, a caller that does not know or care whether it is
    moving a file or a directory (e.g. ``rewrite_basename._rename_one_
    directory``, which already threads a resolved ``session_id``/
    ``relocate_fn`` pair through expecting the full move-plus-claim
    contract) gets the directory-aware behavior for free, with no call-site
    change at all.

    Kept to exactly ONE ``shutil.move`` call, unconditionally at the very
    end of the function, regardless of which claim-restatement branch ran —
    the file-vs-directory branching below decides ONLY whether/how a claim
    is restated first, never whether or how the move itself happens; both
    branches still fall through to the same single trailing
    ``shutil.move(src_abs, dst_abs)``. This also keeps the C6 static-scan
    allow-list entry for this function's ``shutil.move`` (ordinal 1)
    correct without any edit to it: a second, syntactically distinct
    ``shutil.move`` call in this function would need its own ordinal-2
    allow-list entry, which is not warranted here since there is genuinely
    only one call.

    "Currently T-claimed" is decided by normalizing ``src_rel`` with
    :func:`normalize_touch_path` (the SAME normalization ``touch()`` itself
    applies before ever writing an entry, so the comparison is against the
    same dialect ``touched.txt`` was written in) and checking membership in
    ``project_self_scope(lines)`` over THIS session's own ``touched.txt`` —
    the self-facing "last event is T" projection, never the peer-facing
    mtime-reclaim one (:func:`project_peer_claims`), since this is always a
    self-claim question about the session performing the relocation.

    "Currently T-claimed" is decided by normalizing ``src_rel`` with
    :func:`normalize_touch_path` (the SAME normalization ``touch()`` itself
    applies before ever writing an entry, so the comparison is against the
    same dialect ``touched.txt`` was written in) and checking membership in
    ``project_self_scope(lines)`` over THIS session's own ``touched.txt`` —
    the self-facing "last event is T" projection, never the peer-facing
    mtime-reclaim one (:func:`project_peer_claims`), since this is always a
    self-claim question about the session performing the relocation.

    Behavior:
      1. ``src_rel`` NOT currently claimed -> perform the move, write NO
         claim event anywhere. Fabricating a T for ``dst_rel`` here would be
         an over-claim on a file this session may never have written at all.
      2. ``src_rel`` IS currently claimed -> append ``T(dst_rel)`` FIRST, via
         :func:`touch_written_path` (so the phantom-live-peer guard below is
         INHERITED rather than reimplemented a second time), THEN perform the
         move. Appends NO ``R`` event anywhere, for ``src_rel`` or otherwise —
         the claim on ``src_rel`` is left standing for
         :func:`release_committed_claims` to retire on its own once the
         rename's deletion half actually lands in a commit.

    Ordering contract, and why claim-before-move is BOUNDED rather than
    CLOSED (Review: code-reviewer, sidecar coordinatorcode-reviewer-9d2370bf.md
    — an earlier version of this paragraph claimed the crash window was
    provably inert because "``compute_scope`` intersects every candidate
    against the actually-dirty tree, and a non-existent path is never dirty".
    That is false of ``compute_scope`` as written: its Step 4 subtraction
    appends every uncontested ``touched.txt`` candidate to ``my_scope``
    UNCONDITIONALLY — the dirty-tree intersection (``dirty_files_set``)
    gates only the Step 2 mtime-added candidates, a disjoint set from
    ``touched.txt`` entries by construction. A ``T(dst_rel)`` claim on a path
    that was never created reaches ``my_scope`` through plain ``touched.txt``
    membership, with no dirty-tree filter standing between it and a commit
    pathspec.):

    Claiming ``dst_rel`` before the move means a crash between the two
    steps leaves a ``T`` claim on a path that was never created — that path
    then surfaces in ``my_scope`` and is passed as part of the commit
    pathspec to :func:`coordinator_core.ops.ceremony.commit_pipeline.
    explicit_stage`. Traced there: a pathspec element that is genuinely
    absent on disk and IS in ``caller_paths`` (which it is — every caller of
    ``explicit_stage`` in this pipeline passes ``caller_paths=set(paths)``)
    is classified ``"missing-caller:<p>"``, added to
    ``StageOutcome.missing_caller_paths``, and drives ``exit_code == 2`` —
    a degraded-but-not-failed signal (see ``run_commit_pipeline``'s own
    docstring: "``exit_code == 2`` does NOT by itself set ``commit_failed``").
    The commit still lands; the phantom path is simply never staged. So the
    window is BOUNDED, not closed: it never fails a commit or corrupts one,
    but the phantom ``T(dst_rel)`` claim is not self-healing either — since
    ``dst_rel`` was never created, no future commit ever includes it, so
    :func:`release_committed_claims` never sees it land and never retires
    it. Left alone, that stale claim re-enters ``my_scope`` and re-triggers
    the same benign ``missing-caller`` diagnostic on every subsequent commit
    for this session, until something else (a manual release, a session
    reap) clears it. That residue — not silent corruption, not a failed
    commit — is the actual shape of the crash window this ordering leaves
    behind. Move-then-claim's alternative failure mode (a crash leaves
    ``dst_rel`` unclaimed despite existing on disk, i.e. an
    ``orphans``-shaped gap) is still the worse of the two: an unattributed
    dirty file indistinguishable from any other orphan, versus a self-
    reporting phantom claim that never silently vanishes. Claim-before-move
    remains the right call; its failure mode is just bounded, not inert.

    Destination normalization uses :func:`normalize_touch_path` ONLY, never
    :func:`classify_touch_entry` — the two are materially different
    transforms (the latter is the READ-side historical-entry migration
    ``compute_scope`` Step 1/3/3b applies to entries ALREADY on disk; this is
    a WRITE-side normalization of a path not yet in ``touched.txt`` at all),
    and applying the wrong one changes the bytes a claim would be written
    with.

    Two destination carve-outs, BOTH of which still perform the move but
    write NO claim event for ``dst_rel``:
      (a) ``dst_rel`` already exists on disk before the move —
          ``shutil.move`` silently overwrites an existing destination, and
          this function does not change that overwrite semantics (it is the
          caller's responsibility to have decided the overwrite is wanted);
          it only declines to claim a path that pre-existed, since this
          session did not create it.
      (b) ``dst_rel`` resolves outside claimable space: either
          ``normalize_touch_path`` returns ``None`` (still-absolute after
          normalization — outside the worktree entirely), or the normalized
          value falls under ``.git/`` or ``.archive/`` (see
          :func:`_dst_is_claimable`) — neither is trackable repo content a
          ``touched.txt`` claim can meaningfully cover.

    Inherits :func:`touch_written_path`'s phantom-live-peer guard by calling
    it rather than :func:`touch` directly: when ``core.session_dir(session_id,
    cwd)`` is not an already-existing directory, ALL claim bookkeeping is
    skipped (both the "is src claimed" read and the "claim dst" write) — but
    the move itself still happens either way. Do not drop this guard when
    adapting this function; it is the same guard documented at
    :func:`touch_written_path`, inherited rather than reimplemented.

    Raises ``ValueError`` if ``session_id``, ``src_rel``, or ``dst_rel`` is
    empty (mirrors :func:`touch`'s required-arg contract). Never raises for
    an operational failure in the claim bookkeeping itself (an unreadable
    ``touched.txt`` degrades to "not claimed", matching every other fail-open
    read in this module) — but does NOT catch a failure from the actual
    ``shutil.move``, which propagates to the caller like any other
    filesystem operation this module does not otherwise guard.
    """
    if not session_id:
        raise ValueError("session_id required")
    if not src_rel:
        raise ValueError("src_rel required")
    if not dst_rel:
        raise ValueError("dst_rel required")

    root = core.git_root(cwd)
    src_abs = (
        os.path.join(root, src_rel) if root and not _is_absolute(src_rel) else src_rel
    )
    dst_abs = (
        os.path.join(root, dst_rel) if root and not _is_absolute(dst_rel) else dst_rel
    )

    sdir = core.session_dir(session_id, cwd)
    bookkeeping_live = bool(sdir) and os.path.isdir(sdir)

    if bookkeeping_live and os.path.isdir(src_abs) and not os.path.islink(src_abs):
        # Directory branch (widening): restate every claimed descendant.
        # `src_abs` is checked, not `src_rel`, and BEFORE the move below —
        # after the move `src_abs` no longer exists to classify. A symlink
        # to a directory deliberately falls through to the single-path
        # branch below instead (unchanged pre-widening behavior for that
        # shape): it is not the "relocate a real directory tree" case this
        # branch exists for, and `shutil.move` on a symlink relocates the
        # link itself, not a tree of nested files, so there is nothing here
        # for the prefix filter (no filesystem walk — see
        # _tree_relocation_claim_pairs's docstring) to find distinct from
        # the link's own single path.
        _restate_tree_claims(session_id, src_rel, dst_rel, cwd, sdir, root)
    elif bookkeeping_live:
        # Delegates to the shared single-path body also used by the public
        # restate_touched_path — same checks, same order, same
        # touch_written_path call this branch always made; see
        # _restate_single_path_claim's docstring for the full contract.
        _restate_single_path_claim(session_id, src_rel, dst_rel, cwd, sdir, root)

    shutil.move(src_abs, dst_abs)


#: Repo-relative prefixes a relocation destination must never be claimed
#: under, even when ``normalize_touch_path`` resolves them to a clean,
#: in-tree, non-absolute value — ``.git/`` because it is version-control
#: plumbing, never trackable content, and ``.archive/`` because it mirrors
#: the archival-residency shape ``coordinator_core/archival.py`` already
#: treats as terminal/out-of-scope for the LIVE working set (that module's
#: ``archive/handoffs/`` convention; this is the sibling worktree-root
#: convention for the same idea — see :func:`_dst_is_claimable`). Neither
#: prefix is reachable through ``normalize_touch_path`` returning ``None``,
#: since both resolve to clean in-worktree relative paths.
_UNCLAIMABLE_DST_PREFIXES: Tuple[str, ...] = (".git", ".archive")


def _dst_is_claimable(dst_norm: str) -> bool:
    """True iff a normalized, in-tree, non-absolute relocation destination
    is eligible to be claimed by :func:`relocate_touched_path`.

    Excludes exactly the two path shapes named in that function's destination
    carve-out (b): a path under ``.git/`` (git's own plumbing) or under
    ``.archive/`` (the session hub's own archival-residency convention,
    see :func:`archive`) — neither is trackable repo content a
    ``touched.txt`` claim can meaningfully cover, and ``normalize_touch_path``
    does not filter either out on its own (both are clean, in-tree, relative
    paths from its point of view). Does NOT duplicate the
    outside-the-worktree check — that one is ``normalize_touch_path``
    returning ``None``, handled by the caller before this is ever consulted.

    Known, deliberate scope narrowing (Review: code-reviewer, sidecar
    coordinatorcode-reviewer-9d2370bf.md): the check inspects ONLY the first
    path segment (``dst_norm.split("/", 1)[0]``), so it excludes exactly a
    top-level ``.git``/``.archive`` and nothing deeper — a NESTED ``.git``
    (e.g. a vendored submodule's own ``.git``) or a non-root ``.archive``
    directory elsewhere in the tree is NOT caught by this function. This is
    intentional: the segment-based check is what correctly avoids a false
    positive on a sibling name like ``.github/...`` (a raw ``startswith``
    would wrongly match it), and the worktree-root convention this function
    is built for (``.git``, ``.archive``) is itself a root-level concept.
    A caller needing to exclude a nested vendored ``.git`` as well must add
    that check separately; it is out of scope here.
    """
    first = dst_norm.split("/", 1)[0]
    return first not in _UNCLAIMABLE_DST_PREFIXES


def _porcelain_dirty_paths(status_output: str) -> Optional[Set[str]]:
    """Parse one ``git status --porcelain`` invocation's stdout into the set
    of paths it reports as dirty — the per-call cleanliness probe
    :func:`release_committed_claims` needs (one subprocess, not N).

    A well-formed line is ``XY PATH`` (two status chars, one separating
    space, then the path) or, for a rename/copy, ``XY OLD -> NEW`` — both
    ``OLD`` and ``NEW`` are recorded dirty; a renamed path never releases
    cleanly through its from-name alone. Returns ``None`` (fail-safe
    signal) on ANY line that does not fit one of those two shapes — the
    caller's contract is to release NOTHING for the whole call rather than
    guess which path a malformed line concerns (AC7's "fail-safe is
    RETAIN").
    """
    dirty: Set[str] = set()
    for raw_line in status_output.splitlines():
        if not raw_line:
            continue
        if len(raw_line) < 4 or raw_line[2] != " ":
            return None
        path_field = raw_line[3:]
        if not path_field:
            return None
        if " -> " in path_field:
            old, _sep, new = path_field.partition(" -> ")
            if not old or not new:
                return None
            dirty.add(old)
            dirty.add(new)
        else:
            dirty.add(path_field)
    return dirty


def _release_from_touched_file(
    touched_path: str,
    clean: Set[str],
    when: datetime,
    normalize: Callable[[str], Optional[str]],
) -> None:
    """Append an ``R`` event for each currently-``T``-claimed path in
    *touched_path* whose normalized form is in *clean* — the shared
    per-file release step :func:`release_committed_claims` runs once for
    its own session dir and once per back-pointed agent dir. Private
    plumbing, not part of this module's policy surface (mirrors
    ``_collect_peer_path_mtimes``'s own "internal plumbing" framing) —
    AC12 forbids a third named CLAIMED/RELEASED policy entry point, and
    this makes no such decision itself; it only reconciles an
    already-computed clean-set against an already-computed claimed-set.

    APPEND ONLY — one ``write()`` call per emitted event line, safely
    under PIPE_BUF, mirroring ``touch()``'s and
    ``claims.py::atomic_dedup_append``'s discipline (see that function's
    docstring for the T21 lost-update race this is named for: the prior
    mktemp+sort+mv pattern on THIS FILE let concurrent writers each
    read-then-overwrite, silently dropping the loser's entries — NO
    mktemp, no mv, no flock, here either).

    Never releases a directory entry (``some/dir/``): its claim covers
    files outside the commit pathspec, so it is excluded before and after
    normalization. Fail-safe is RETAIN, not raise: any read/parse/write
    failure leaves this file's release skipped for this call.
    """
    if not os.path.isfile(touched_path):
        return
    try:
        existing_lines = Path(touched_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    claimed = project_self_scope(existing_lines)
    to_release: List[str] = []
    # Review: code-reviewer Finding 4 (2026-08-03) — `claimed` is a `Set[str]`;
    # iterating it unsorted made a multi-path release's on-disk append order
    # depend on hash randomization. Harmless for last-event-wins semantics
    # (each event is keyed by its own path) but non-reproducible for a file
    # this plan treats as an authoritative append-only ledger — sorted() is
    # free and gives every multi-path release a deterministic byte sequence.
    for raw_path in sorted(claimed):
        if not raw_path or raw_path.endswith("/"):
            continue  # never release a directory entry
        norm = normalize(raw_path)
        if norm is None or norm.endswith("/"):
            continue
        if norm in clean:
            to_release.append(raw_path)

    if not to_release:
        return  # AC10 — no empty write, don't churn touched.txt's mtime

    try:
        with open(touched_path, "a", encoding="utf-8") as fh:
            for raw_path in to_release:
                fh.write(format_touch_event("R", raw_path, when) + "\n")
    except OSError:
        return  # fail-safe RETAIN — a partial write leaves fewer paths
        # released than requested, never more; safe direction.


def release_committed_claims(
    sid: str, paths: List[str], cwd: Optional[str] = None
) -> None:
    """Append an ``R`` (release) event for each of *paths* that is clean in
    the worktree, to THIS session's OWN ``touched.txt`` and to every
    ``.agents/<aid>/touched.txt`` back-pointed at *this* ``sid`` — the
    claim-release counterpart to :func:`touch`, called post-commit once a
    pathspec has actually landed.

    Structurally incapable of releasing a PEER's claim: this function
    never accepts a peer session id and never iterates the sessions
    directory looking for anyone else's records — only THIS ``sid``'s own
    ``touched.txt`` and agent dirs whose ``em-session-id.txt``
    back-pointer resolves to THIS ``sid`` (i.e. genuinely this session's
    own dispatched-agent fan-out — the same self/other boundary
    :func:`compute_scope` Step 3b already draws, and self-exclusion is
    already how that step treats ``em_sid == sid``). Per the governing
    lesson (2026-07-31, a liveness-keyed rescue reattributing a live
    session's work), the release set is decided from AUTHORSHIP alone,
    never from a peer's liveness or claim state — there is no peer-facing
    branch in this function at all.

    The release set for EACH such file is exactly ``paths ∩
    clean-in-worktree ∩ currently-T-claimed-in-that-file``. A path
    outside the caller-supplied *paths* is never released regardless of
    cleanliness — otherwise every gitignored scratch/marker path this
    session ever touched (which ``git status --porcelain`` reports clean
    forever by construction) would get silently released. Cleanliness is
    computed with ONE ``git status --porcelain -- <paths>`` call, never N
    per-path calls (see :func:`_porcelain_dirty_paths`); a rename line or
    any other unparseable line fails the WHOLE call safe to DIRTY (nothing
    releases this call) rather than guessing which path a malformed line
    concerns. ``ops/ceremony/scoped_git_commit.py::_commit_paths_are_clean``
    is the aggregate cousin — ONE bool over a whole pathspec — and is NOT
    reused here: it cannot say WHICH of several paths is clean, only
    whether ALL are.

    Session-side entries are normalized via :func:`normalize_touch_path`
    (repo-root-relative dialect); agent-side entries via
    ``ops/session/safe_commit_offer._normalize_agent_touched_entry`` (the
    agent fan-out's own dialect) — never cross-applied, and never a
    directory entry released even if it would otherwise equal a
    caller-supplied path after normalization (see
    :func:`_release_from_touched_file`).

    Fail-safe is RETAIN, not raise: any ``OSError``, git failure, or parse
    ambiguity skips the affected file's release for this call — mirrors
    ``touch()``'s fail-open contract. Emits nothing when there is nothing
    to release for a given file (AC10): ``touched.txt``'s mtime is a
    recency/liveness signal several readers depend on
    (``dispatch_checks._rm_peer_claim_of``'s 30-min backstop,
    ``compute_scope`` Step 3b's agent-dir recency window,
    ``ops/session/reap.py``'s 24h agent staleness), so a no-op call must
    not churn it.
    """
    if not sid or not paths:
        return

    requested = {p for p in paths if p}
    if not requested:
        return

    status_out = _git_output(
        [
            "-c",
            "core.quotepath=false",
            "status",
            "--porcelain",
            "--",
            *sorted(requested),
        ],
        cwd,
    )
    if status_out is None:
        return  # git failed — fail-safe RETAIN

    dirty = _porcelain_dirty_paths(status_out)
    if dirty is None:
        return  # unparseable line — fail-safe RETAIN for the whole call

    clean = requested - dirty
    if not clean:
        return  # nothing clean to release this call

    when = datetime.now(timezone.utc)

    sdir = core.session_dir(sid, cwd)
    if sdir:
        own_touched = os.path.join(sdir, "touched.txt")
        _release_from_touched_file(
            own_touched,
            clean,
            when,
            lambda raw_path: normalize_touch_path(raw_path, cwd),
        )

    base = core.sessions_dir(cwd)
    if not base:
        return
    agents_base = os.path.join(base, ".agents")
    if not os.path.isdir(agents_base):
        return
    try:
        agent_entries = sorted(os.scandir(agents_base), key=lambda e: e.name)
    except OSError:
        return

    # Function-local import to avoid the module-scope import cycle
    # `compute_scope`'s own Step 3b already routes around (`safe_commit_
    # offer` imports `compute_scope` from THIS module at module scope).
    from coordinator_core.ops.session.safe_commit_offer import (
        _normalize_agent_touched_entry,
    )

    for entry in agent_entries:
        agent_dir = Path(entry.path)
        if not agent_dir.is_dir():
            continue
        backptr = agent_dir / "em-session-id.txt"
        if not backptr.is_file():
            continue
        try:
            first_lines = backptr.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        em_sid = (first_lines[0] if first_lines else "").strip()
        if em_sid != sid:
            continue  # not this session's own fan-out — never release it
        _release_from_touched_file(
            str(agent_dir / "touched.txt"),
            clean,
            when,
            _normalize_agent_touched_entry,
        )


#: Prefix that forces a git pathspec to be matched byte-literally, so a
#: claimed path containing pathspec magic (a leading ``:``/``!``, or a glob
#: metacharacter — ``*``, ``?``, ``[``, ``]``) is never parsed as a pattern.
#: Review: code-reviewer Finding 2 (2026-08-06, sidecar
#: coordinatorcode-reviewer-5e45cd5a.md) — without this, such a path is
#: parsed by git as a pathspec, which can either over-match (safe direction)
#: or fail to match the intended literal path (silently mis-classifies the
#: discriminator, which is not proven safe). Applied to every pathspec this
#: module passes to git at :func:`_tracked_at_head`'s and
#: :func:`_staged_in_index`'s call sites.
def _literal_pathspec(path: str) -> str:
    return f":(literal){path}"


def _tracked_at_head(paths: List[str], cwd: Optional[str]) -> Optional[Set[str]]:
    """Which of ``paths`` (repo-relative, already normalized) does git know
    about at ``HEAD`` — one half of the discriminator :func:`release_phantom_
    claims` needs to tell a never-created phantom apart from a tracked file
    this session legitimately deleted (the other half is
    :func:`_staged_in_index`, for content staged this session but not yet in
    HEAD).

    ONE ``git ls-tree -r --name-only HEAD -- <paths>`` call, never N. Returns
    ``None`` (fail-safe signal, mirroring :func:`_git_output`'s own contract)
    on ANY git failure — including the empty-repo "unknown revision HEAD"
    case, which a bare ``_git_output`` failure cannot distinguish from a
    systemic git problem; the caller's contract is RETAIN (release nothing)
    on either, not to guess.

    Returns the empty set (never ``None``) for an empty ``paths`` input — no
    subprocess needed, and "nothing tracked among nothing asked" is not a
    failure.

    Every pathspec is wrapped in :func:`_literal_pathspec` so a claimed path
    containing pathspec magic is matched byte-literally rather than parsed
    as a pattern.
    """
    if not paths:
        return set()
    out = _git_output(
        [
            "-c",
            "core.quotepath=false",
            "ls-tree",
            "-r",
            "--name-only",
            "HEAD",
            "--",
            *(_literal_pathspec(p) for p in paths),
        ],
        cwd,
    )
    if out is None:
        return None
    return {line for line in out.splitlines() if line}


def _staged_in_index(paths: List[str], cwd: Optional[str]) -> Optional[Set[str]]:
    """Which of ``paths`` (repo-relative, already normalized) currently carry
    STAGED content in the index — the other half of :func:`release_phantom_
    claims`'s discriminator, alongside :func:`_tracked_at_head`.

    Exists for exactly one shape ``_tracked_at_head`` cannot see: a path this
    session ``git add``-ed (new or modified) and then deleted from disk
    before committing is NOT tracked at HEAD, but the index still holds real
    staged content that a plain ``git commit`` will land regardless of
    whether this session's touch-claim survives. Review: code-reviewer
    Finding 1 (2026-08-06, sidecar coordinatorcode-reviewer-5e45cd5a.md).

    ONE ``git ls-files --stage -- <paths>`` call, never N — kept to exactly
    one additional invocation beyond ``_tracked_at_head``'s, since both run
    on :func:`release_phantom_claims`'s post-commit hot path and this module
    holds every op to an end-to-end invocation budget. Every pathspec is
    wrapped in :func:`_literal_pathspec`, matching ``_tracked_at_head``.

    Returns ``None`` (fail-safe signal) on ANY git failure — the caller's
    contract is RETAIN on either helper failing, never to guess. Returns the
    empty set (never ``None``) for an empty ``paths`` input.
    """
    if not paths:
        return set()
    out = _git_output(
        [
            "ls-files",
            "--stage",
            "--",
            *(_literal_pathspec(p) for p in paths),
        ],
        cwd,
    )
    if out is None:
        return None
    staged: Set[str] = set()
    for line in out.splitlines():
        if not line:
            continue
        # '<mode> <blob-sha> <stage>\t<path>' — split on the FIRST tab only,
        # matching parse_touch_event's own "never split on structure that
        # could appear in the path" discipline; the path is everything after
        # the tab, unescaped. This call only ever receives paths this module
        # itself already normalized, so no further unquoting is needed.
        _, _, path = line.partition("\t")
        if path:
            staged.add(path)
    return staged


def release_phantom_claims(sid: str, cwd: Optional[str] = None) -> None:
    """Self-heal a PHANTOM touch-claim — a ``T`` claim on a path that was
    never actually created on disk, the residue documented at
    ``state/bug-backlog/2026-08-06-a-phantom-touch-claim-from-an-interrupte-
    c21f5bbdd077.yaml``: :func:`relocate_touched_path` appends ``T(dst)``
    BEFORE its trailing ``shutil.move``, so a crash in that window leaves a
    claim on a path that was never created. That claim never lands in a
    commit, so :func:`release_committed_claims` — which only ever releases a
    path the CALLER names, post-commit — never sees it and never retires it;
    left alone it re-surfaces in ``compute_offer`` for the life of the
    session.

    THE DISCRIMINATOR, and the entire reason this is a release-side helper
    and not a widened ``compute_scope`` predicate (DR-254): a claimed path
    absent from disk is NOT by itself proof of a phantom — a claimed file
    this session legitimately DELETED as part of its real work is *also*
    absent from disk, and releasing THAT claim would silently drop a real
    deletion from the safe-commit offer, the exact class of bug this exists
    to fix, reintroduced from the other side. The two are separated by
    whether git knows the path at ``HEAD`` (:func:`_tracked_at_head`) OR
    currently holds staged content for it in the index
    (:func:`_staged_in_index`):

      - absent from disk, tracked at HEAD -> a REAL deletion. ``git status``
        reports it as a pending ``D``; there is a genuine git-representable
        change (``git rm`` / ``git add -u``) for a future commit to capture.
        MUST stay claimed. Never released here.
      - absent from disk, NOT tracked at HEAD, but STAGED in the index -> a
        path this session ``git add``-ed (new or modified) and then deleted
        from disk before committing. Not tracked at HEAD, but the index
        still holds real content a plain ``git commit`` will land regardless
        of whether this claim survives. MUST stay claimed for the same
        reason as the tracked-at-HEAD case: a real, git-representable change
        the safe-commit machinery must not stop tracking. Review:
        code-reviewer Finding 1 (2026-08-06, sidecar coordinatorcode-
        reviewer-5e45cd5a.md) — an earlier version of this docstring
        asserted this shape "collapsed" with the genuine-phantom case; it
        does not, and the paragraph below has been narrowed accordingly.
      - absent from disk, NOT tracked at HEAD, NOT staged in the index ->
        released. This covers two byte-indistinguishable shapes that
        collapse into one safe verdict: a genuine phantom (never existed at
        all) and a claimed-then-deleted UNTRACKED file (existed only outside
        git's index, now gone). Neither has anything for a commit pathspec
        to ever pick up — an untracked path was never added, so there is no
        ``git rm`` for its removal, no deletion for the offer to represent.
        Releasing both is therefore behaviorally correct, not merely a
        best-effort guess: there is nothing a future commit could do with
        either claim that releasing it does not already correctly forgo.
      - present on disk -> untouched by this function regardless of dirty
        state; :func:`release_committed_claims` (clean) and the ordinary
        dirty-tree path already cover a real, existing file.

    DR-254 check: this is a WRITE-side release, structurally identical in
    shape to :func:`release_committed_claims` (append-only ``R`` events to
    THIS session's own ``touched.txt``) — ``compute_scope`` gains no new
    predicate, and a release can only NARROW ``my_scope``, never widen it,
    matching that decision's fail-closed posture.

    Scoped to THIS session's own ``touched.txt`` only — the traced defect is
    :func:`relocate_touched_path` writing a session's OWN claim (via
    :func:`touch_written_path` -> :func:`touch`), never a dispatched agent's
    ``touched.txt``; unlike :func:`release_committed_claims` there is no
    agent-fan-out leg to mirror here.

    Fail-safe is RETAIN, not raise: an unreadable ``touched.txt``, an
    unresolvable ``normalize_touch_path``, or ANY git failure (including
    ``_tracked_at_head`` OR ``_staged_in_index`` returning ``None``) skips
    release for this call —
    the same fail-open contract every other reader/writer in this module
    keeps. Emits nothing (no write at all) when there is nothing to release
    (AC10 discipline: a no-op call must not churn ``touched.txt``'s mtime).
    """
    if not sid:
        return
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return
    touched_path = os.path.join(sdir, "touched.txt")
    if not os.path.isfile(touched_path):
        return
    try:
        lines = Path(touched_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    claimed = project_self_scope(lines)
    if not claimed:
        return

    root = core.git_root(cwd)
    candidates: List[str] = []
    norm_of: Dict[str, str] = {}
    for raw_path in sorted(claimed):
        if not raw_path or raw_path.endswith("/"):
            continue  # never release a directory entry
        # Defensive-only: every entry here is already output of touch()'s own
        # normalize_touch_path call at write time (the same dialect
        # touched.txt was written in) — this re-normalization does not expect
        # a second, different result, it guards against a future writer
        # regression rather than a known live case. Review: code-reviewer
        # Nit (2026-08-06, sidecar coordinatorcode-reviewer-5e45cd5a.md).
        norm = normalize_touch_path(raw_path, cwd)
        if norm is None or norm.endswith("/"):
            continue
        abs_path = (
            os.path.join(root, norm) if root and not _is_absolute(norm) else norm
        )
        if os.path.exists(abs_path):
            continue  # on disk -- not this function's concern
        candidates.append(raw_path)
        norm_of[raw_path] = norm

    if not candidates:
        return

    norm_paths = sorted(set(norm_of.values()))
    tracked = _tracked_at_head(norm_paths, cwd)
    if tracked is None:
        return  # git failed -- fail-safe RETAIN
    staged = _staged_in_index(norm_paths, cwd)
    if staged is None:
        return  # git failed -- fail-safe RETAIN

    git_known = tracked | staged
    to_release = [rp for rp in candidates if norm_of[rp] not in git_known]
    if not to_release:
        return

    when = datetime.now(timezone.utc)
    try:
        with open(touched_path, "a", encoding="utf-8") as fh:
            for raw_path in to_release:
                fh.write(format_touch_event("R", raw_path, when) + "\n")
    except OSError:
        return  # fail-safe RETAIN -- a partial write releases fewer paths
        # than requested, never more; safe direction.


def compute_scope(
    sid: str, cwd: Optional[str] = None, extra_candidates: Optional[List[str]] = None
) -> ScopeResult:
    """Port of ``cs_compute_scope <session_id>``, with the
    nested ``_cs_other_claim_owner`` helper folded in as a dict lookup.

    AC8 (belt-and-braces behind the C6 historical-corpus migration, not a
    substitute for it): Step 1 candidates AND the Step 3/3b ``other_owner``
    key space are BOTH defensively normalized on read, via
    ``classify_touch_entry``'s strip-one-leading-``../``-then-verify-
    containment transform — but NOT via the same symmetric application on
    both sides (Review: code-reviewer Finding 1, sidecar
    ``coordinatorcode-reviewer-359b224b.md`` — an earlier version of this
    docstring claimed the two sides "line up" identically; that claim was
    false for a `dropped` (formerly `multi_level`) peer entry naming an
    in-tree file whose colliding candidate happens to be clean). The two
    sides round in
    OPPOSITE, directional ways instead:
      - Step 1 candidates (``normalize_historical_touch_entry``): an
        unresolvable entry is DROPPED — narrows ``my_scope``, the safe
        direction for this side.
      - Step 3/3b ``other_owner`` keys (``normalize_peer_claim_key``): an
        unresolvable entry NEVER silently vanishes — it falls back to a
        maximal-strip (all leading ``../``) interpretation and, if that
        resolves in-tree, is entered defensively into ``other_owner``.
        Over-claiming here can only ever REMOVE a path from ``my_scope``,
        the safe direction for this side.
    A future writer regression that reintroduces ``../``-prefixed entries
    at a depth this transform cannot rescue therefore degrades to a
    candidate being dropped (narrowing) and/or a defensive peer key being
    added (also narrowing) — never to false self-ownership of a path a
    live peer still holds. ``normalize_touch_path`` alone does NOT cover
    this — it guards absoluteness only.

    ``extra_candidates`` (optional): additional repo-relative paths to seed
    into the Step-1 candidate set alongside ``touched.txt``, so they receive
    the SAME Step 3/4 other-session-ownership check as everything else —
    added for ``coordinator_core.ops.session.safe_commit_offer``, which
    unions in a dispatched sub-agent's fan-out (``my_agent_touched``,
    broadened mode) and must not hand those paths through uncontested: a
    broadened-mode over-reach that happens to name a sibling session's file
    would otherwise be offered as "safe to commit" verbatim. Order-preserving,
    de-duplicated against the existing candidate set. ``None`` (the default)
    reproduces the pre-existing behavior exactly for every other caller.

    Compute this session's scoped staging set:

        MY_SCOPE = touched.txt (+ extra_candidates)
                   − ⋃(LIVE other_sessions.touched.txt ∩ dirty_files)
                   − mtime_only_candidates

    (Review: staff-eng F2 — updated from the pre-liveness-gate formula:
    other-session claims now subtract only a LIVE peer's claim on a path
    that is ALSO currently dirty — see Step 3/3b below for the two-stage
    liveness-then-dirty-path gating this reflects, and the fail-closed
    invariant paragraph below for what a failure in either input does.)

    where ``mtime_only_candidates`` is the subset of
    ``mtime_dirty_since_started_at`` NOT already in ``touched.txt`` (or
    ``extra_candidates``) — i.e. dirty because SOMEBODY touched it, with no
    demonstrable claim that it was THIS session. An uncontested mtime-only
    candidate is routed to ``orphans`` (Step 5), never into ``my_scope``: a
    file mis-attributed into a stranger's commit is silent and corrupts
    review-coverage chain derivation downstream, whereas an orphan is visible
    in the commit-time report and recoverable (a human/EM can claim or defer
    it). ``extra_candidates`` are exempt from this exclusion — see Step 1/4.

    Returns a :class:`ScopeResult` (``my_scope``, ``skipped``, ``orphans``)
    — the clean structured replacement for the bash stdout/stderr split.
    Always succeeds (bash ``return 0``); an out-of-repo call returns three
    empty lists rather than raising.

    Set math, preserved exactly from the bash original except where noted:
      1. Candidate set seeds from ``touched.txt`` (non-empty lines, order
         preserved), plus ``extra_candidates`` appended if not already
         present.
      2. Dirty set = ``git diff --name-only HEAD`` ∪ ``git ls-files --others
         --exclude-standard``, ``sort -u``'d. A dirty file is ADDED to the
         candidate set only if it is not already present AND its mtime is
         ``>= started_at_epoch`` (files touched before the session started
         are not scope); such an addition is ALSO recorded into
         ``mtime_only`` (a candidate already present via touched.txt or
         extra_candidates never enters ``mtime_only``, by construction of
         this same "not already present" guard — Step 1 runs first). NOTE
         the TWO-git-command requirement — see the module negative-spec
         (porcelain collapses untracked dirs). If ``started_at`` cannot be
         read, this augmentation is skipped entirely (fail-closed — see
         Step 2 inline comment).
      3. Other-session claims: scan every OTHER session dir's ``touched.txt``;
         first-writer-wins on the owner of each claimed path (bash parallel-
         array first-match scan → dict ``setdefault``). Self, and any
         hidden dir (``.archive``/``.agents``/other dot-entries the bash
         ``*/`` glob would exclude), are skipped in THIS per-session scan.
         If an other-session ``touched.txt`` cannot be read, its claims are
         unknown (fail-closed — see Step 3 inline comment): no candidate is
         allowed to pass through as uncontested-mine while any sibling claim
         set is unreadable.

         Ownership is contested only by a LIVE peer holding a claim on a
         path that ALSO has uncommitted content right now — "touched-ever"
         is not "contended-now". A peer whose session is no longer live
         (``coordinator_core.session.liveness.live_session_ids``, computed
         ONCE before Step 3 and reused by Step 3b) does not contest a path
         at all — its entire claim set is skipped for that peer, regardless
         of path — and a peer's claim on a path absent from ``dirty_files``
         (Step 2) is pruned as stale-by-construction: the peer's work
         already landed, so there is nothing left to contest. This is the
         RELEASE path that was previously missing entirely: before this
         change, a session that touched a path, committed it, and exited
         owned that path for the rest of the branch's life, because nothing
         ever re-evaluated the claim. The liveness skip is evaluated FIRST
         (cheaper, and it is a per-PEER skip, not per-path), the dirty-path
         prune second (per-path, within a still-live peer). Prune scope
         note: this prunes only a DEFINITIVELY-LANDED claim (the path is
         clean).

         The formerly-open live-peer residual — a peer committed a path
         some time ago, STAYED LIVE, and THIS session has since dirtied it
         — is now closed, but at the WRITE side, not here. A still-live
         committer calls :func:`release_committed_claims` after its own
         commit lands (wired at the ``ops/ceremony/scoped_git_commit.py``
         post-commit call site), which appends an ``R`` event to the
         committer's OWN ``touched.txt``/agent records for every path that
         is clean at that moment. By the time THIS function's Step 3/3b
         scan reads that peer's record, the peer's last event for the path
         is already ``R``, not ``T`` — the claim is gone from the record
         itself, so there is nothing left for the read side here to
         resolve; :func:`project_peer_claims`'s own mtime-vs-challenger
         logic (see its docstring) decides whether a later dirty mtime or
         this session's own genuine challenger T re-projects it, which is a
         property of that function, not of this Step. This function
         acquires NO new read-side predicate to make that determination —
         the fail-closed invariant below is unchanged, because nothing here
         claims to attribute a dirty hunk within a file to a session; that
         genuinely remains something git cannot answer, and still is not
         attempted. If ``live_session_ids`` returns an
         EMPTY set while at least one peer session directory OR a non-empty
         ``.agents`` dir genuinely exists on disk (Review: staff-eng F5 —
         ``.agents`` is peer evidence too, an owning EM session can be
         reaped while its dispatched sub-agent's claim record survives),
         that emptiness is indeterminate (it is the same return shape as
         "everyone actually is dead") — liveness is a POSITIVE signal only,
         and a false-DEAD verdict WIDENS ``my_scope``, the unsafe direction
         (see the fail-closed invariant below), so this call falls back to
         the pre-existing UNCONDITIONAL exclusion (no liveness gating at
         all) rather than treat every peer as dead. The same fallback
         applies if ``live_session_ids`` itself raises, if the git dirty
         scan (Step 2) failed (Review: staff-eng F0 — ``dirty_scan_ok``;
         the clean-path prune below depends on the SAME dirty set, so an
         unreliable dirty set disables liveness gating too, not just the
         prune), or if the self-liveness canary trips (Review: staff-eng F1
         — ``sid not in live_ids`` while this session's own dir exists on
         disk: a caller that cannot see its own live session has a
         worthless verdict about peers; exempted for a post-mortem/tooling
         call whose own session dir is already archived/absent). Death
         evidence standard: liveness is read ONLY from
         ``live_session_ids()``'s two-layer verdict (Layer 1 PPID-
         authoritative, Layer 2 recency) — this function does not itself
         confirm death by any other signal. Residual NOT covered by any of
         the above guards: a PARTIAL under-report that omits some peer
         OTHER than ``sid`` itself, while ``live_ids`` is non-empty and
         does contain ``sid`` — that peer reads as dead and its claim is
         released without any guard here catching it. Closing that residual
         needs a positive "confirmed dead" predicate (deliberately out of
         scope for this pass; see F1's docstring residual note at the
         canary's call site).

         Downstream reader note: this release path changes what a peer's
         ``touched.txt`` looks like on disk (an ``R`` event line appended
         after a clean commit), and that shape change had a real, DIFFERENT-
         KIND consequence for at least one OTHER reader of the same file —
         ``coordinator_core.hooks.nudge_unrouted_sizing._session_touched_lines``,
         the one genuine historical reader of ``touched.txt`` outside this
         module. That reader does not merely go silent for an
         early-committing session, as originally assumed when this release
         path was designed: its pre-fix anchored-regex matching against the
         WHOLE raw line HARD-BREAKS the instant any line in the file carries
         a verb prefix at all (``'T <ts> <path>'`` / ``'R <ts> <path>'``),
         because the anchor never matches at position 0 once a verb prefix
         is present — a total, not partial, loss of that nudge for every
         session sharing this repo's ``touched.txt`` corpus, not only for
         the peer whose claim was released. Fixed by reading the bare path
         back out via ``parse_touch_event(line)[2]`` (see that reader's own
         docstring for the full account) rather than by anything in this
         function — ``compute_scope`` acquires no new obligation toward that
         reader from this note; it exists so the next person debugging a
         missing sizing/plan-routing nudge finds the cause without
         re-deriving it from scratch.

         Step 3 ALSO scans ``<sessions_dir>/.agents/<aid>/touched.txt`` — the
         record a dispatched sub-agent's own writes land in, back-pointed to
         its owning EM session via ``<aid>/em-session-id.txt``. Every OTHER
         agent's (i.e. not back-pointed at THIS ``sid`` — that fan-out is
         already unioned in via ``extra_candidates`` elsewhere, not a foreign
         claim) claimed paths are merged into ``other_owner``, attributed to
         the BACK-POINTED em-session-id, not the agent id, with the same
         first-writer-wins discipline, and the SAME liveness-then-dirty
         gating above — keyed on the back-pointed ``em_sid`` rather than the
         agent id (an agent has no liveness identity of its own). Entries
         use a DIFFERENT path dialect (plugin-directory-relative, i.e.
         relative to ``<repo>/coordinator``, NOT repo-root-relative like
         ``<sessions_dir>/<sid>/touched.txt``) — normalized via
         ``coordinator_core.ops.session.safe_commit_offer._normalize_agent_touched_entry``
         (function-local import — see Step 3 inline comment), with a
         directory entry expanded via that module's ``_dirty_files_under``.
         An unreadable ``.agents`` scan, or an unreadable individual agent's
         ``touched.txt``, is treated with the SAME fail-closed discipline as
         an unreadable per-session ``touched.txt`` above, UNCHANGED by the
         liveness gating (an unreadable claim set is never assumed dead;
         it is withheld, exactly as before). A NOT-YET-WRITTEN
         ``em-session-id.txt`` (36ed64f58 mis-attribution incident, 2026-08)
         gets a NARROWER, overlap-scoped fail-closed treatment, NOT the
         blanket ``unreadable_other_sessions`` mechanism above: a dispatched
         sub-agent's own ``touched.txt`` is written synchronously (by
         ``coordinator_core.hooks.track_touched_files``) on every one of its
         edits, while the ``em-session-id.txt`` back-pointer is written only
         by a PostToolUse hook on the DISPATCHING session's Agent/Task tool
         call (``coordinator_core.hooks.track_dispatched_agents``) — for a
         FOREGROUND dispatch (the common case) that fires only once the
         whole agent turn returns, so the agent's ENTIRE lifetime is a
         window where a real, dirty, genuinely-claimed path has no
         resolvable owner. BUT a real sweep of this repo's own on-disk
         ``.agents/`` corpus (2026-08) found 261 of 2011 agent dirs
         permanently in this exact shape — non-empty ``touched.txt``, no
         back-pointer, all long-dead residue (crashed/hard-killed agents,
         pre-back-pointer vintage), not live races. Applying the blanket
         mechanism there would withhold EVERY uncontested candidate on
         EVERY future call, forever — a permanent, silent commit outage,
         strictly worse than the incident. Two guards instead: (i)
         RECENCY — only a ``touched.txt`` modified within
         ``liveness._THIRTY_MIN`` (the SAME recency window
         ``session_live``'s own Layer-2 fallback already uses — reused, not
         re-invented) is treated as a plausibly-live race at all; older is
         dead residue and contests nothing. (ii) OVERLAP-SCOPING — even a
         plausibly-live race withholds ONLY the specific candidate paths
         that overlap this agent's OWN ``touched.txt`` content (tracked in
         ``agent_race_paths``, a separate set from ``other_owner`` and
         ``unreadable_other_sessions``), never every uncontested candidate
         this call — bounding the blast radius even if recency alone were
         ever wrong. A genuinely empty/new agent dir (no back-pointer AND no
         touched.txt content yet) has nothing to protect and is still
         skipped silently, at any age.
      4. Subtraction, in this exact order: (a) a candidate owned by another
         session is SKIPPED (recorded in ``skipped`` as ``(path, owner)``);
         (b) else if the candidate overlaps a recent, unresolved sub-agent
         race claim (``agent_race_paths`` — Step 3b), SKIPPED as owner
         "unknown (agent race window...)" — narrower than (c), only THIS
         candidate; (c) else if any other session's claims are unreadable,
         SKIPPED as owner "unknown" (fail-closed, applies to every
         uncontested candidate this call); (d) else if the candidate is
         mtime-only (uncontested, no session claims it), it is dropped from
         ``my_scope`` entirely — no append anywhere — so Step 5 reports it
         as an orphan; (e) else (touched.txt / extra_candidates, uncontested)
         it is in ``my_scope``. Any other ordering of (a)-(d) is a defect: it
         would either report a sibling-owned mtime candidate as an orphan
         instead of ``skipped`` with its real owner, or bypass the
         ``agent_race_paths``/``unreadable_other_sessions`` fail-closed arms
         for that candidate class.
      5. Orphans: a dirty file that is neither in ``my_scope`` nor owned by
         another session is an orphan (recorded in ``orphans``) — this now
         also catches every mtime-only candidate dropped in Step 4(c).
         Review: staff-eng F6 — the liveness/clean-path release path above
         (Step 3/3b) ALSO changes this Step's disposition for one shape:
         a dirty path claimed ONLY by a now-dead (or claim-pruned) peer,
         and never touched by THIS session, used to land in ``other_owner``
         unconditionally and was therefore excluded from ``orphans`` here
         (owned, not orphaned) — that is exactly the "owned that path for
         the rest of the branch's life" defect this change fixes. Post-fix,
         such a path is neither claimed (``other_owner`` has no entry — the
         peer was skipped/pruned) nor mine (never in ``touched_set``), so it
         now surfaces as an ORPHAN instead of silently disappearing. This is
         the intended release-path side effect, not a regression: an orphan
         is visible in the commit-time report and recoverable, whereas the
         old "owned forever" disposition was invisible. Pinned by
         ``test_dead_peer_untouched_dirty_file_becomes_orphan_not_silently_owned``
         in ``test_scope.py``.

    Fail-closed invariant: a read failure anywhere in this function may only
    NARROW ``my_scope`` (drop a candidate, move it from ``my_scope`` to
    ``skipped``, or — as of the mtime-only routing above — to ``orphans``),
    never widen it. This direction (moving paths OUT of ``my_scope``) always
    preserves and, for the mtime-only case, actively strengthens that
    invariant IN THE COMMITTER DIRECTION. This is NOT a universal "narrowing
    is always safe" claim, however: ``my_scope`` feeds directly into
    ``coordinator-safe-commit``'s scoped-staging check
    (``dispatch_checks.check_validate_commit``) as an ALLOW-LIST, where
    narrowing is the unsafe direction for THAT consumer (a legitimately-mine
    file dropped from the allow-list gets blocked, not silently accepted) —
    handling that consumer-side tradeoff is out of scope for this function.

    Review: staff-eng F2 — the liveness/clean-path release path (Step 3/3b)
    adds TWO inputs whose failure can WIDEN ``my_scope`` if left unguarded,
    on top of the pre-existing ``started_at``/other-session-touched.txt
    reads documented above:
      - ``liveness.live_session_ids()`` (liveness enumeration) — a false-
        DEAD verdict releases a still-live peer's claim. Guarded by: the
        empty-set indeterminacy check (session-dir-or-``.agents`` evidence),
        the self-liveness canary (F1), and an exception handler — all three
        degrade to "gating disabled this call" (pre-existing unconditional
        exclusion), never to "everyone is dead". Residual: a partial
        under-report of a peer OTHER than ``sid`` while ``live_ids`` still
        contains ``sid`` is not caught by any of the three (see the F1
        residual note at Step 3's docstring paragraph above).
      - the git dirty scan (Step 2's two ``_git_output`` calls) — both
        failing yields an empty ``dirty_files_set``, which the clean-path
        prune (Step 3/3b) would otherwise read as "every peer claim is
        stale". Guarded by ``dirty_scan_ok`` (F0): gates every clean-path
        prune site AND disables liveness gating entirely when tripped.
        ``dirty_scan_ok`` is ``True`` only if BOTH ``_git_output`` calls
        succeed — a partial (one-command) failure is treated the same as a
        total failure, deliberately conservative. Residual: none known.

    Raises ``ValueError`` if ``sid`` is empty (bash ``${1:?}``). Otherwise
    always succeeds (bash ``return 0``); an out-of-repo call returns three
    empty lists rather than raising.
    """
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return ScopeResult([], [], [])
    base = core.sessions_dir(cwd)
    if not base:
        return ScopeResult([], [], [])

    # AC8 / Tier 2: worktree root resolved up-front (rather than only later,
    # for the mtime-fallback abs_path join) so the SAME root backs the
    # defensive '../'-strip-then-verify-containment normalization applied to
    # Step 1 candidates below (`normalize_historical_touch_entry`) AND to
    # the Step 3/3b `other_owner` key space further down
    # (`normalize_peer_claim_key`) — belt-and-braces behind the C6
    # historical-corpus migration, not a substitute for it. The two sides
    # apply the transform DIRECTIONALLY, not symmetrically (see
    # `normalize_peer_claim_key`'s docstring): an unresolvable candidate is
    # dropped (narrows my_scope), an unresolvable peer claim falls back to a
    # maximal-strip defensive key (also narrows my_scope) rather than
    # silently vanishing — never false self-ownership of a path a live peer
    # still holds.
    root = core.git_root(cwd)
    root_path = Path(root) if root else None

    # --- Step 1: candidate set from touched.txt ---
    raw_touch_lines: List[str] = []
    touched_file = Path(sdir) / "touched.txt"
    if touched_file.is_file():
        try:
            for line in touched_file.read_text(encoding="utf-8").splitlines():
                if line:
                    raw_touch_lines.append(line)
        except OSError as exc:
            # advisory — a mid-read failure here silently drops this
            # session's touched.txt lines from scope; the mtime-dirty
            # fallback (Step 2) partially compensates, but surface it.
            print(
                f"cs_compute_scope: failed to read {touched_file} "
                f"(non-fatal, scope may be incomplete): {exc}",
                file=sys.stderr,
            )

    # SELF-facing projection (P3): a path whose last event is R is
    # RELEASED here — `project_self_scope` never applies the peer-facing
    # mtime re-claim (that arm must not widen `my_scope`). For a bare-line
    # legacy corpus (no writer emits an event line yet, so every line
    # parses to T at unknown time via the fail-safe) this reproduces the
    # pre-existing "every non-empty line is a candidate" behaviour exactly,
    # one candidate per distinct path, order-preserving.
    self_claimed_paths = project_self_scope(raw_touch_lines)
    touched_set: List[str] = []
    for line in raw_touch_lines:
        _, _, path = parse_touch_event(line)
        if path in self_claimed_paths and path not in touched_set:
            touched_set.append(path)

    # Peer-facing challenger evidence (EM ratification 2026-08-03, item 1:
    # option (a)) — only `sid`'s own REAL-timestamped T events feed
    # `project_peer_claims`'s `challenger_t_events` argument for Steps 3/3b
    # below. A legacy/fail-safe T (unknown time) carries no evidence of
    # post-dating anything, so it is excluded here — "inference loses to
    # evidence" cuts both ways.
    challenger_t_events = _challenger_t_events(raw_touch_lines)

    for extra in extra_candidates or []:
        if extra and extra not in touched_set:
            touched_set.append(extra)

    # AC8: defensive read-side normalization of Step 1 candidates — belt-and-
    # braces behind the C6 migration (see this function's docstring and
    # `classify_touch_entry`). Order-preserving, de-duplicated post-
    # normalization (two poisoned dialects of the same real path could
    # otherwise collide into two distinct pre-normalization strings that
    # normalize to the same clean value). A candidate that classifies as
    # `dropped` (a non-absolute entry whose canonical value escapes the
    # worktree, or an unrescuable absolute entry) is DROPPED from the
    # candidate set entirely here — narrowing only, per the fail-closed
    # invariant; it never becomes a fabricated in-repo path.
    normalized_touched_set: List[str] = []
    for candidate in touched_set:
        normalized_candidate = normalize_historical_touch_entry(candidate, root_path)
        if normalized_candidate and normalized_candidate not in normalized_touched_set:
            normalized_touched_set.append(normalized_candidate)
    touched_set = normalized_touched_set

    # --- Step 2: mtime fallback — dirty files modified after started_at ---
    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # Previously: an OSError reading started_at silently degraded to
    # started_at_iso = "" -> core.iso_to_epoch("") == 0 -> every dirty
    # file's mtime (core.mtime_epoch is always >= 0) satisfied
    # `file_mtime >= started_at_epoch`, so a transient read failure
    # WIDENED touched_set to every dirty file in the working tree instead
    # of narrowing it. Narrowing on failure is safe; widening is not (see
    # the fail-closed invariant in this function's docstring). Fail
    # CLOSED instead: an unreadable started_at skips the mtime-fallback
    # augmentation entirely, so touched_set is bounded by touched.txt
    # alone for this call.
    started_at_iso = ""
    started_at_file = Path(sdir) / "started_at"
    started_at_readable = True
    try:
        started_at_iso = started_at_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        started_at_readable = False
        print(
            f"cs_compute_scope: failed to read {started_at_file} "
            f"(non-fatal, mtime-fallback scope augmentation skipped "
            f"this call): {exc}",
            file=sys.stderr,
        )
    started_at_epoch = core.iso_to_epoch(started_at_iso)

    # Review: staff-eng F0/F8 — `-c core.quotepath=false` keeps the dirty
    # scan raw-byte-faithful with touched.txt's own dialect (latent today,
    # zero non-ASCII tracked paths, but the mismatch is safety-relevant the
    # moment one exists). `dirty_scan_ok` mirrors `started_at_readable`
    # immediately above: both `_git_output` calls return `None` on ANY
    # failure (swallowed by `or ""`), and a total failure of both must NOT
    # be read as "every peer claim is stale" — that WIDENS my_scope (the
    # unsafe direction), the opposite of what an unreadable git command
    # should do. Every clean-path-prune site below, and the liveness gate,
    # is gated on this flag.
    diff_out_raw = _git_output(
        ["-c", "core.quotepath=false", "diff", "--name-only", "HEAD"], cwd
    )
    others_out_raw = _git_output(
        ["-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard"],
        cwd,
    )
    dirty_scan_ok = diff_out_raw is not None and others_out_raw is not None
    if not dirty_scan_ok:
        print(
            "cs_compute_scope: git dirty-scan (diff/ls-files) failed "
            "(non-fatal, clean-path claim pruning AND liveness gating are "
            "both disabled this call — falling back to the pre-existing "
            "unconditional exclusion for every peer claim): "
            f"diff={'ok' if diff_out_raw is not None else 'FAILED'} "
            f"ls-files={'ok' if others_out_raw is not None else 'FAILED'}",
            file=sys.stderr,
        )
    diff_out = diff_out_raw or ""
    others_out = others_out_raw or ""
    dirty_files = sorted(
        {line for line in (diff_out.splitlines() + others_out.splitlines()) if line}
    )
    dirty_files_set = set(dirty_files)

    # `root` resolved up-front, before Step 1 (see the AC8 comment there) —
    # not re-derived here.
    mtime_only: set[str] = set()
    if started_at_readable:
        for dfile in dirty_files:
            # Mirror bash "${root}/${dfile}" string concatenation exactly
            # (an empty root yields "/<dfile>", which mtime_epoch resolves
            # to 0).
            abs_path = f"{root}/{dfile}"
            file_mtime = core.mtime_epoch(abs_path)
            if file_mtime >= started_at_epoch:
                if dfile not in touched_set:
                    touched_set.append(dfile)
                    mtime_only.add(dfile)
    # --- end Tier 2 ---

    # --- Liveness gate for Step 3 / Step 3b (the release path) ---
    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # Previously: other_owner was populated from ANY peer session's touch
    # record with no liveness check and no pruning -- ownership was
    # "touched-ever", not "contended-now". A session that touched a path,
    # committed it, and exited owned that path for the rest of the branch's
    # life, because Step 4 moves any candidate with an other_owner entry to
    # skipped and nothing ever re-evaluated the claim (a sibling repo hit
    # this and was forced to bypass the sanctioned commit boundary with a
    # raw `git commit`). Compute the live-session set ONCE here, before Step
    # 3, and reuse it for both Step 3 and Step 3b: a peer whose session is
    # no longer live does not contest at all (its claims are skipped
    # wholesale for that peer -- see Step 3/3b below), which is the release
    # path that was missing. Exclusion stays ABSOLUTE for a LIVE peer --
    # this is unchanged and unweakened (see the cadc5d87 29-file incident
    # this mechanism was built for, Step 3b's own comment below).
    #
    # Liveness is a POSITIVE signal only: a false-DEAD verdict WIDENS
    # my_scope, the unsafe direction under this function's fail-closed
    # invariant. So an unexpected exception from live_session_ids() degrades
    # to "gating disabled this call" (the pre-existing unconditional
    # exclusion), never to "everyone is dead".
    # Review: staff-eng F0 — the dirty scan is the second input this gate's
    # own safety depends on (the clean-path prune below reads
    # `dirty_files_set`, populated from the SAME two git commands). If the
    # dirty scan failed, `dirty_files_set` is unreliable, so disable
    # liveness gating too rather than let it release peer claims onto a
    # git-command failure. See `dirty_scan_ok`'s own comment above for why.
    if not dirty_scan_ok:
        live_ids: Optional[frozenset[str]] = None
        print(
            "cs_compute_scope: git dirty-scan failed -- liveness gating "
            "disabled this call regardless of live_session_ids() (both "
            "halves of the release path rest on an unverified dirty-set "
            "premise otherwise; pre-existing unconditional exclusion "
            "behaviour applies).",
            file=sys.stderr,
        )
    else:
        try:
            live_ids = liveness.live_session_ids(cwd)
        except Exception as exc:
            live_ids = None
            print(
                f"cs_compute_scope: liveness.live_session_ids() raised "
                f"(non-fatal, liveness gating disabled this call -- falling "
                f"back to the pre-existing unconditional exclusion): {exc}",
                file=sys.stderr,
            )

    if live_ids is not None and not live_ids:
        # Indeterminacy guard: an empty live set is the SAME return shape
        # whether every peer is genuinely dead, or enumeration silently saw
        # nobody. Disambiguate by checking whether a peer session directory
        # actually exists on disk; if one does, treat this as indeterminate
        # rather than concluding every peer is dead, and disable gating
        # entirely for this call.
        peer_dir_seen = False
        if os.path.isdir(base):
            # Review: staff-eng F10 — os.listdir(base) is unguarded on a
            # TOCTOU race (base removed/unreadable between the isdir()
            # check above and this call) or a permissions error. On
            # failure, treat as indeterminate (peer_dir_seen=True) rather
            # than silently proceeding as "no peers seen": the latter
            # would leave live_ids as an empty (not None) frozenset, which
            # Step 3 reads as "every peer is dead" -- the WIDENING
            # direction this whole disambiguator exists to prevent.
            try:
                base_entries = os.listdir(base)
            except OSError:
                base_entries = None
            if base_entries is None:
                peer_dir_seen = True
            else:
                for other_id in base_entries:
                    if other_id.startswith(".") or other_id == sid:
                        continue
                    if os.path.isdir(os.path.join(base, other_id)):
                        peer_dir_seen = True
                        break
                if not peer_dir_seen:
                    # Review: staff-eng F5 — a non-empty `.agents` dir is
                    # ALSO peer evidence: a dispatched sub-agent's claim
                    # (Step 3b) is back-pointed to an owning EM session
                    # that may itself have no visible session dir of its
                    # own (already reaped) while its sub-agent's claim
                    # record is still live on disk. Skipping `.agents`
                    # here (as the dot-entry check above does deliberately
                    # for the per-session scan) would miss exactly the
                    # peer-activity signal this disambiguator exists to
                    # catch. Also F10-guarded, same rationale as above.
                    agents_probe = os.path.join(base, ".agents")
                    try:
                        agents_entries = (
                            os.listdir(agents_probe)
                            if os.path.isdir(agents_probe)
                            else []
                        )
                    except OSError:
                        agents_entries = ["<unreadable>"]
                    if agents_entries:
                        peer_dir_seen = True
        if peer_dir_seen:
            live_ids = None
            print(
                "cs_compute_scope: liveness.live_session_ids() returned "
                "empty while at least one peer session directory exists "
                "-- treating liveness enumeration as indeterminate and "
                "disabling liveness gating for this call (pre-existing "
                "unconditional exclusion behaviour applies).",
                file=sys.stderr,
            )

    # Review: staff-eng F1 — self-liveness canary. Absence from `live_ids`
    # conflates confirmed-dead, no-evidence, and enumeration
    # under-reporting; the empty-set guard above only catches TOTAL
    # under-report. A caller that cannot see its OWN live session in the
    # set it is about to use to judge peers has a worthless verdict about
    # those peers — checking `sid not in live_ids` (rather than only
    # `not live_ids`) catches a PARTIAL under-report the empty-set guard
    # cannot. Exempted when this session's own dir does not exist on disk
    # (already archived, or a post-mortem/tooling invocation for a
    # deliberately non-live sid) — a caller with no live claim to begin
    # with is not evidence of a bad enumeration. Residual NOT covered:
    # a partial under-report that happens to omit some OTHER peer while
    # still correctly including `sid` sails through undetected — this
    # canary is a cheap self-check, not the full
    # confirmed-dead-vs-under-reported disambiguation (`peer_confirmed_dead`
    # is deliberately out of scope for this pass).
    if live_ids is not None and live_ids and os.path.isdir(sdir) and sid not in live_ids:
        live_ids = None
        print(
            f"cs_compute_scope: self-liveness canary tripped -- {sid!r} "
            "not present in its own live_session_ids() result while its "
            "own session dir exists -- treating liveness enumeration as "
            "unreliable and disabling liveness gating for this call "
            "(pre-existing unconditional exclusion behaviour applies).",
            file=sys.stderr,
        )
    # --- end Tier 2 ---

    # --- Step 3: other sessions' claim sets (first-writer-wins) ---
    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # Previously: an OSError reading another session's touched.txt silently
    # degraded to lines = [] -- "that session claims nothing" -- which is
    # the WIDENING direction: a candidate actually owned by that session
    # would incorrectly pass the Step 4 subtraction into my_scope, silently
    # suppressing coordinator-safe-commit's foreign-staged-file warning
    # (dispatch_checks.check_validate_commit). We cannot safely fabricate
    # an empty-but-plausible claim set for an unreadable file, so fail
    # CLOSED: track which sessions' claims were unreadable, and in Step 4,
    # withhold ANY currently-uncontested candidate from my_scope while a
    # sibling claim set is unreadable (moved to skipped, owner "unknown"),
    # rather than let it silently pass as uncontested-mine.
    other_owner: dict[str, str] = {}
    # C1 — ungated, per-path attribution sidecar. Populated for EVERY
    # peer/agent claim Step 3/3b's loop reads, before that claim's liveness
    # continue and clean-path prune are applied — never gates on, or feeds,
    # the other_owner/skipped/my_scope subtraction. See OwnerFact's
    # docstring for the split this preserves.
    attribution: dict[str, OwnerFact] = {}
    unreadable_other_sessions: List[str] = []
    # Overlap-scoped counterpart to `unreadable_other_sessions` (Step 3b,
    # missing-em-session-id.txt-but-recent branch) — paths withheld from
    # my_scope WITHOUT the global blast radius of the unreadable-claims
    # mechanism above; see that branch's comment for why the two must stay
    # separate (261-stale-agent-dir sweep finding, 2026-08).
    agent_race_paths: set[str] = set()

    # C1 — shared liveness-string helper for attribution entries (Step 3/3b).
    # TODO(C8): once `coordinator_core.session.liveness.live_session_verdicts()`
    # lands (peer chunk, `dict[str, tuple[bool, str, float|None]]` keyed by
    # session id), prefer its basis-bearing verdict for a resolved peer id
    # over this `live_ids`-membership-only inference — this reads only the
    # frozenset `live_session_ids()` already computed above, so it can only
    # ever report "live"/"dead"/"undetermined", never a verdict basis.
    def _peer_liveness_str(peer_id: Optional[str]) -> str:
        if live_ids is None or peer_id is None:
            return "undetermined"
        return "live" if peer_id in live_ids else "dead"

    if os.path.isdir(base):
        for other_id in sorted(os.listdir(base)):
            # bash `*/` glob excludes dot-entries (subsumes .archive/.agents).
            if other_id.startswith("."):
                continue
            # Review: staff-eng F4 — the dot-entry check alone misses the
            # NON-dot reserved children (`handoff-claims`, `memo-claims`,
            # `plan-claims`, `agent-sessions-locks`, `logs`, `no-session`)
            # that `liveness._NON_SESSION_DIR_NAMES` already excludes from
            # enumeration; without this, a claim-lock/log dir could be
            # scanned here as if it were a peer session.
            if other_id in liveness._NON_SESSION_DIR_NAMES:
                continue
            if other_id == sid:
                continue
            other_sdir = os.path.join(base, other_id)
            if not os.path.isdir(other_sdir):
                continue
            other_touched = os.path.join(other_sdir, "touched.txt")
            if os.path.isfile(other_touched):
                try:
                    lines = Path(other_touched).read_text(
                        encoding="utf-8"
                    ).splitlines()
                except OSError as exc:
                    unreadable_other_sessions.append(other_id)
                    print(
                        f"cs_compute_scope: failed to read {other_touched} "
                        f"(non-fatal, its claims are indeterminate this "
                        f"call — uncontested candidates will be withheld "
                        f"from my_scope): {exc}",
                        file=sys.stderr,
                    )
                    lines = []
                    # C1: the claim set itself is unreadable — no path
                    # content is knowable, so attribute against the same
                    # session-id sentinel `unreadable_other_sessions` uses.
                    if other_id not in attribution:
                        attribution[other_id] = OwnerFact(
                            other_id, "undetermined", "unreadable"
                        )

                # C1 — attribution (ungated): record every claim this peer's
                # touched.txt holds BEFORE the liveness continue below and
                # BEFORE the clean-path prune further down, so a dead peer's
                # claim and a since-landed (clean-path-pruned) claim both
                # still surface here even though neither enters
                # `other_owner`. First-writer-wins, same discipline as
                # `other_owner` (see this module's negative-spec).
                peer_liveness = _peer_liveness_str(other_id)
                for opath in lines:
                    if not opath:
                        continue
                    _, _, opath_attr_field = parse_touch_event(opath)
                    norm_attr_path = normalize_peer_claim_key(
                        opath_attr_field, root_path
                    )
                    if norm_attr_path and norm_attr_path not in attribution:
                        attribution[norm_attr_path] = OwnerFact(
                            other_id, peer_liveness, "session"
                        )

                # Liveness gate (evaluated first — see the comment block
                # above): a dead peer's touch record does not contest at
                # all. Its read-failure handling above is unchanged by this
                # — an unreadable claim set is never assumed dead.
                if live_ids is not None and other_id not in live_ids:
                    continue
                # PEER-facing projection (P3): gate `other_owner` population
                # through `project_peer_claims` before the existing AC8
                # normalization/clean-path-prune pipeline below — a released
                # path re-projects to CLAIMED only under the mtime-re-claim
                # rule (§ Decision 3); a released path with no re-claim
                # evidence stays absent here, same as `other_owner` never
                # gaining an entry for it today.
                nonblank_lines = [ln for ln in lines if ln]
                peer_path_mtimes = _collect_peer_path_mtimes(
                    nonblank_lines, root
                )
                peer_claimed_paths = project_peer_claims(
                    nonblank_lines, peer_path_mtimes, challenger_t_events
                )
                for opath in lines:
                    if not opath:
                        continue
                    _, _, opath_field = parse_touch_event(opath)
                    if opath_field not in peer_claimed_paths:
                        continue
                    # AC8: defensive read-side normalization of the
                    # other_owner key space — a DIRECTIONAL counterpart to
                    # Step 1's candidate-side transform, not the same
                    # symmetric one (Review: code-reviewer Finding 1 —
                    # sidecar coordinatorcode-reviewer-359b224b.md). This is
                    # the arm that actually fixes the memo's headline
                    # false-"owned by session X" symptom: a normalized Step
                    # 1 candidate colliding against an UN-normalized
                    # other_owner key would still mismatch. Unlike the
                    # candidate side, a peer claim that the one-level strip
                    # cannot resolve is NOT silently withheld here — see
                    # `normalize_peer_claim_key`'s docstring for why
                    # symmetric dropping on this side is unsafe.
                    norm_opath = normalize_peer_claim_key(opath_field, root_path)
                    if not norm_opath or norm_opath in other_owner:
                        continue
                    # Clean-path pruning: a claim on a path with no
                    # uncommitted content is stale by construction (the
                    # peer's work already landed) — see the docstring's
                    # scope note on what this does and does not cover.
                    # Gated on `dirty_scan_ok` (Review: staff-eng F0) — an
                    # unreliable dirty set must not be read as "clean".
                    if dirty_scan_ok and norm_opath not in dirty_files_set:
                        continue
                    other_owner[norm_opath] = other_id

    # --- Step 3b: peer EM sessions' dispatched sub-agent claims ---
    # Extends the per-session scan above: a dispatched sub-agent's own
    # writes land in <sessions_dir>/.agents/<aid>/touched.txt, back-pointed
    # to its owning EM session via <aid>/em-session-id.txt -- a durable,
    # on-disk ownership record the per-session scan above never reads (it
    # explicitly skips dot-entries, ".agents" included). This is the actual
    # mechanism behind the 29-file cadc5d87 incident: a peer's sub-agent's
    # Write-tool output had exactly this record on disk, and fell through
    # Step 4 uncontested because nothing looked at it.
    #
    # Self-exclusion: an agent back-pointed at THIS session (em_sid == sid)
    # is this session's OWN fan-out, already unioned into its candidate set
    # elsewhere via extra_candidates (safe_commit_offer's my_agent_touched,
    # "exact" mode) -- NOT a foreign claim. Counting it here would make a
    # session unable to commit its own dispatched agents' work.
    #
    # Fail-closed, matching the per-session scan above: an unreadable
    # .agents scan, an unreadable individual agent's em-session-id.txt, or
    # an unreadable individual agent's touched.txt, is recorded into the
    # SAME unreadable_other_sessions list. Review: staff-eng F7 — this was
    # previously misstated as only two sentinel shapes; there are THREE:
    # the literal ".agents" sentinel for a whole-dir scan failure (em_sid
    # unknown for every entry), a per-entry ".agents/<agent-dir-name>"
    # sentinel when em-session-id.txt itself is unreadable (em_sid is what
    # is unknowable here, so the agent's directory name is the only
    # identifying key available), or the resolved back-pointed
    # em-session-id when only that agent's touched.txt is unreadable (the
    # owner IS known, only its claim set is not). Any of the three makes
    # Step 4 withhold uncontested candidates rather than let a foreign
    # sub-agent claim silently read as "claims nothing".
    agents_base = os.path.join(base, ".agents")
    if os.path.isdir(agents_base):
        try:
            agent_entries = sorted(os.scandir(agents_base), key=lambda e: e.name)
        except OSError as exc:
            unreadable_other_sessions.append(".agents")
            print(
                f"cs_compute_scope: failed to scan {agents_base} "
                f"(non-fatal, peer sub-agent claims are indeterminate this "
                f"call — uncontested candidates will be withheld from "
                f"my_scope): {exc}",
                file=sys.stderr,
            )
            agent_entries = []

        # Path-dialect normalization is owned by safe_commit_offer (its own
        # docstring records the 240-absolute-path defect a second dialect
        # caused) -- reuse it rather than re-inlining a second copy here.
        # Function-local import: safe_commit_offer already imports
        # compute_scope from this module at module scope, so a module-scope
        # import here would be circular.
        from coordinator_core.ops.session.safe_commit_offer import (
            _dirty_files_under_batch,
            _normalize_agent_touched_entry,
        )

        for entry in agent_entries:
            agent_dir = Path(entry.path)
            if not agent_dir.is_dir():
                continue
            backptr = agent_dir / "em-session-id.txt"
            if not backptr.is_file():
                # Root cause (36ed64f58 mis-attribution incident, 2026-08):
                # coordinator_core.hooks.track_touched_files writes
                # .agents/<aid>/touched.txt SYNCHRONOUSLY, on every one of the
                # subagent's own Write/Edit/MultiEdit/NotebookEdit calls —
                # while coordinator_core.hooks.track_dispatched_agents (the
                # em-session-id.txt back-pointer writer) fires only in a
                # PostToolUse hook on the DISPATCHING session's Agent/Task
                # tool call. For a FOREGROUND dispatch (the common case) that
                # PostToolUse fires only once the agent returns, so the
                # WHOLE agent lifetime is the race window (corrected
                # 2026-08 — an earlier pass at this fix inverted this:
                # background dispatch returns the Agent/Task tool call
                # immediately, so its own back-pointer lands almost at once,
                # while it is a FOREGROUND dispatch's long lifetime that
                # actually exposes the window). A dirty, genuinely-claimed
                # path can therefore exist on disk — with a real touched.txt
                # entry — for the run of a still-working subagent, before
                # any back-pointer exists to resolve it to an owning EM
                # session.
                #
                # Real disk sweep of this repo's OWN .git/coordinator-
                # sessions/.agents/ (2026-08) found 261 (of 2011) agent dirs
                # in exactly this shape — non-empty touched.txt, no
                # em-session-id.txt — every one of them long-dead residue
                # (crashed/hard-killed agents, or pre-back-pointer vintage),
                # NOT live races. Treating every such dir identically to a
                # live race — as an initial pass at this fix did, via the
                # SAME global ``unreadable_other_sessions`` mechanism every
                # other indeterminate-claim branch in this function uses —
                # means a global withhold fires on EVERY scope computation,
                # forever: an occasional mis-attribution becomes a
                # permanent, silent commit outage. Two guards close that
                # without reopening the mis-attribution:
                #
                #   (1) Recency: only a dir whose touched.txt was modified
                #       within the last ``liveness._THIRTY_MIN`` (the
                #       existing session-liveness Layer-2 recency window —
                #       reused rather than inventing a second threshold) is
                #       treated as a plausibly-live race at all. Older than
                #       that is dead residue and contests nothing, exactly
                #       as if this dir did not exist.
                #   (2) Overlap-scoping: even a plausibly-live race
                #       withholds ONLY the specific candidate paths that
                #       overlap this agent's own touched.txt (via
                #       ``agent_race_paths`` below) — NOT every uncontested
                #       candidate in this call (the global
                #       ``unreadable_other_sessions`` mechanism). This
                #       bounds the blast radius to the actually-contested
                #       files even if recency alone were ever wrong in the
                #       unsafe direction.
                #
                # Residual (code-reviewer, slice A Finding 3, accepted
                # explicitly rather than left implicit): two narrow TOCTOU
                # windows here degrade toward NOT withholding rather than
                # the fail-closed posture used everywhere else in this
                # function for an indeterminate other-session claim — (a)
                # if touched_probe is deleted/replaced between the
                # `.stat()` size check above and `core.mtime_epoch()` below,
                # `mtime_epoch` returns 0 (not an error signal), making
                # `age_sec` enormous, so the recency guard treats a raced
                # file as ancient dead residue rather than indeterminate;
                # (b) if `read_text()` below raises `OSError` after
                # `has_activity` was already confirmed True, this dir
                # contests nothing for this call, silently (see the stderr
                # note in that except-arm). Both windows require a
                # concurrent delete/replace of touched.txt mid-scope-
                # computation — accepted as a residual rather than widened
                # into another global withhold, per the 261-of-2011-dirs
                # disk sweep that already forced a revert of exactly that
                # global-withhold shape (see the em-session-id.txt-missing
                # branch above).
                touched_probe = agent_dir / "touched.txt"
                try:
                    has_activity = touched_probe.stat().st_size > 0
                except OSError:
                    has_activity = False
                if has_activity:
                    age_sec = core.now_epoch() - core.mtime_epoch(str(touched_probe))
                    if age_sec < liveness._THIRTY_MIN:
                        try:
                            raw_lines = touched_probe.read_text(
                                encoding="utf-8"
                            ).splitlines()
                        except OSError as exc:
                            # See the residual note above `touched_probe`'s
                            # assignment: this arm silently no-ops the
                            # withhold for this agent_dir this call unless
                            # it says so.
                            raw_lines = []
                            print(
                                f"cs_compute_scope: {touched_probe} became "
                                f"unreadable between the size check and "
                                f"this read ({exc}) — treating this "
                                f"agent_dir as contesting nothing this "
                                f"call rather than withholding (accepted "
                                f"residual, not a fail-closed arm)",
                                file=sys.stderr,
                            )
                        normalized_race_lines = [
                            _normalize_agent_touched_entry(raw) for raw in raw_lines
                        ]
                        race_dir_entries = [
                            n
                            for n in normalized_race_lines
                            if n is not None and n.endswith("/")
                        ]
                        dirty_by_dir_race = _dirty_files_under_batch(
                            race_dir_entries, cwd
                        )
                        contested_here: List[str] = []
                        for norm in normalized_race_lines:
                            if norm is None:
                                continue
                            if norm.endswith("/"):
                                for opath in dirty_by_dir_race.get(norm, []):
                                    norm_opath = normalize_peer_claim_key(
                                        opath, root_path
                                    )
                                    if norm_opath:
                                        contested_here.append(norm_opath)
                            else:
                                norm_claim = normalize_peer_claim_key(
                                    norm, root_path
                                )
                                if norm_claim:
                                    contested_here.append(norm_claim)
                        if contested_here:
                            agent_race_paths.update(contested_here)
                            # C1: attribute every overlapping path to the
                            # not-yet-back-pointed agent dir itself (the
                            # owning em-session-id is genuinely unknowable
                            # here) — ungated, first-writer-wins.
                            agent_race_sentinel = f".agents/{agent_dir.name}"
                            for race_path in contested_here:
                                if race_path not in attribution:
                                    attribution[race_path] = OwnerFact(
                                        agent_race_sentinel,
                                        "undetermined",
                                        "agent-race",
                                    )
                            print(
                                f"cs_compute_scope: {backptr} not yet "
                                f"written (non-fatal, {agent_dir.name} has "
                                f"recent recorded touches — age "
                                f"{age_sec}s — but its owning em-session-id "
                                f"is not yet resolvable — the "
                                f"{len(contested_here)} overlapping "
                                f"candidate path(s) will be withheld from "
                                f"my_scope this call, uncontested "
                                f"candidates elsewhere are unaffected): "
                                f"{touched_probe}",
                                file=sys.stderr,
                            )
                    # else: older than the recency window — dead residue,
                    # contests nothing (see the sweep finding above).
                continue
            try:
                first_lines = backptr.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                # Review: code-reviewer Finding 1 — an unreadable
                # em-session-id.txt is NOT the same as a malformed
                # (successfully-read, empty) one: the owning em-session-id
                # is unknowable, so this must fail-closed like the
                # touched.txt-unreadable branch below, not soft-skip.
                agent_sentinel = f".agents/{agent_dir.name}"
                if agent_sentinel not in unreadable_other_sessions:
                    unreadable_other_sessions.append(agent_sentinel)
                # C1: the owning em-session-id is unknowable at all — no
                # path content is resolvable either, so attribute against
                # the same sentinel used above.
                if agent_sentinel not in attribution:
                    attribution[agent_sentinel] = OwnerFact(
                        agent_sentinel, "undetermined", "unreadable"
                    )
                print(
                    f"cs_compute_scope: failed to read {backptr} "
                    f"(non-fatal, its owning em-session-id is "
                    f"indeterminate this call — uncontested candidates "
                    f"will be withheld from my_scope): {exc}",
                    file=sys.stderr,
                )
                continue
            em_sid = (first_lines[0] if first_lines else "").strip()
            if not em_sid or em_sid == sid:
                continue  # malformed, or this session's own fan-out (see above)

            # C1 — attribution (ungated): record every claim this agent's
            # touched.txt holds BEFORE the liveness continue below and
            # BEFORE the clean-path prune further down, so a dead owning
            # EM session's sub-agent claim still surfaces here even though
            # it never enters `other_owner`. A SEPARATE read from the one
            # below (rather than hoisting that read above the gate) is
            # deliberate: the below read's failure feeds
            # `unreadable_other_sessions` (a Step-4 fail-closed input) only
            # for a LIVE em_sid today — hoisting it would widen that
            # fail-closed trigger to a dead peer's unreadable file too,
            # which this chunk must not change. This attribution-only read
            # never touches `unreadable_other_sessions`.
            attr_agent_touched = agent_dir / "touched.txt"
            if attr_agent_touched.is_file():
                try:
                    attr_raw_lines = attr_agent_touched.read_text(
                        encoding="utf-8"
                    ).splitlines()
                except OSError:
                    attr_raw_lines = []
                    if em_sid not in attribution:
                        attribution[em_sid] = OwnerFact(
                            em_sid, "undetermined", "unreadable"
                        )
                peer_liveness = _peer_liveness_str(em_sid)
                normalized_attr_lines = [
                    _normalize_agent_touched_entry(raw) for raw in attr_raw_lines
                ]
                attr_dir_entries = [
                    n
                    for n in normalized_attr_lines
                    if n is not None and n.endswith("/")
                ]
                dirty_by_dir_attr = _dirty_files_under_batch(attr_dir_entries, cwd)
                for norm in normalized_attr_lines:
                    if norm is None:
                        continue
                    if norm.endswith("/"):
                        for opath in dirty_by_dir_attr.get(norm, []):
                            norm_attr_path = normalize_peer_claim_key(
                                opath, root_path
                            )
                            if norm_attr_path and norm_attr_path not in attribution:
                                attribution[norm_attr_path] = OwnerFact(
                                    em_sid, peer_liveness, "agent"
                                )
                    else:
                        norm_attr_claim = normalize_peer_claim_key(norm, root_path)
                        if norm_attr_claim and norm_attr_claim not in attribution:
                            attribution[norm_attr_claim] = OwnerFact(
                                em_sid, peer_liveness, "agent"
                            )

            # Liveness gate (evaluated first — same rationale as Step 3
            # above), keyed on the back-pointed em_sid: a dead owning EM
            # session's sub-agent claim does not contest at all. Unreadable
            # handling below is unchanged by this.
            if live_ids is not None and em_sid not in live_ids:
                continue

            agent_touched = agent_dir / "touched.txt"
            if not agent_touched.is_file():
                continue
            try:
                raw_lines = agent_touched.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                if em_sid not in unreadable_other_sessions:
                    unreadable_other_sessions.append(em_sid)
                print(
                    f"cs_compute_scope: failed to read {agent_touched} "
                    f"(non-fatal, its claims are indeterminate this "
                    f"call — uncontested candidates will be withheld "
                    f"from my_scope): {exc}",
                    file=sys.stderr,
                )
                continue

            # PEER-facing projection (P3): gate this agent-dir's other_owner
            # population through `project_peer_claims`, same discipline as
            # Step 3 above. `.agents/<aid>/touched.txt` is written by a
            # different writer (`hooks/track_touched_files.py`, out of this
            # plan's scope) that emits bare, un-timestamped lines today —
            # every one parses to T via `parse_touch_event`'s fail-safe, so
            # this filter is a no-op for the current corpus (every entry
            # survives, identical to pre-existing behaviour) while staying
            # correct if that writer ever adopts the event format.
            nonblank_raw_lines = [ln for ln in raw_lines if ln]
            agent_path_mtimes = _collect_peer_path_mtimes(
                nonblank_raw_lines, root
            )
            agent_claimed_paths = project_peer_claims(
                nonblank_raw_lines, agent_path_mtimes, challenger_t_events
            )
            normalized_claim_lines: List[Optional[str]] = []
            for raw in raw_lines:
                if not raw:
                    normalized_claim_lines.append(None)
                    continue
                _, _, raw_claim_path = parse_touch_event(raw)
                if raw_claim_path not in agent_claimed_paths:
                    normalized_claim_lines.append(None)
                    continue
                normalized_claim_lines.append(_normalize_agent_touched_entry(raw))
            claim_dir_entries = [
                n for n in normalized_claim_lines if n is not None and n.endswith("/")
            ]
            dirty_by_dir_claims = _dirty_files_under_batch(claim_dir_entries, cwd)
            for norm in normalized_claim_lines:
                if norm is None:
                    continue
                if norm.endswith("/"):
                    for opath in dirty_by_dir_claims.get(norm, []):
                        if not opath:
                            continue
                        # AC8: same directional other_owner-key
                        # normalization as Step 3 above (`normalize_peer_
                        # claim_key`, not the candidate-side transform),
                        # applied for uniformity — _dirty_files_under
                        # already yields clean git-relative paths in
                        # practice, so this is a no-op here today, kept so
                        # every other_owner write site shares one dialect.
                        norm_opath = normalize_peer_claim_key(
                            opath, root_path
                        )
                        if not norm_opath or norm_opath in other_owner:
                            continue
                        # Clean-path pruning (see Step 3): _dirty_files_under
                        # is already dirty-only by construction, so this is
                        # a no-op here in practice — kept for uniformity
                        # with the single-entry arm below. Gated on
                        # `dirty_scan_ok` (Review: staff-eng F0) for the
                        # same uniformity, even though `_dirty_files_under`
                        # runs its own independent git calls.
                        if dirty_scan_ok and norm_opath not in dirty_files_set:
                            continue
                        other_owner[norm_opath] = em_sid
                else:
                    # AC8: normalize the single-entry claim before it enters
                    # other_owner — `norm` is already repo-root-relative via
                    # `_normalize_agent_touched_entry`'s dialect translation,
                    # but that function only translates dialect, it does not
                    # strip a poisoned '../' prefix, so a poisoned agent
                    # touched.txt entry can still reach here un-degraded
                    # without this pass. Uses the directional peer-side
                    # transform (`normalize_peer_claim_key`), not the
                    # candidate-side one — see Step 3's comment above.
                    norm_claim = normalize_peer_claim_key(norm, root_path)
                    if not norm_claim or norm_claim in other_owner:
                        continue
                    # Gated on `dirty_scan_ok` (Review: staff-eng F0) — see
                    # Step 3's clean-path-pruning comment.
                    if dirty_scan_ok and norm_claim not in dirty_files_set:
                        continue
                    other_owner[norm_claim] = em_sid

    # --- Step 4: subtraction → my_scope + skipped diagnostics ---
    my_scope: List[str] = []
    skipped: List[Tuple[str, str]] = []
    for candidate in touched_set:
        if not candidate:
            continue
        owner = other_owner.get(candidate)
        if owner:
            skipped.append((candidate, owner))
        elif candidate in agent_race_paths:
            # Overlap-scoped withhold (Step 3b) — a recent, unresolved
            # sub-agent claim overlaps THIS specific candidate. Narrower
            # than the `unreadable_other_sessions` branch below: only
            # candidates this agent actually touched are withheld, not
            # every uncontested candidate this call.
            skipped.append((candidate, "unknown (agent race window, no em-session-id.txt yet)"))
        elif unreadable_other_sessions:
            skipped.append((
                candidate,
                "unknown (claims unreadable: %s)"
                % ",".join(unreadable_other_sessions),
            ))
        elif candidate in mtime_only:
            # Uncontested mtime-only candidate: provenance is "somebody
            # dirtied this file", not "this session wrote it" (touched.txt
            # would already have it). Do NOT append anywhere here — let it
            # fall through to Step 5's orphan detection instead of the
            # committer's my_scope allow-list.
            pass
        else:
            my_scope.append(candidate)
    # --- end Tier 2 ---

    # --- Step 5: orphan detection ---
    my_scope_set = set(my_scope)
    orphans: List[str] = []
    for dfile in dirty_files:
        if not dfile:
            continue
        if dfile in my_scope_set:
            continue
        if other_owner.get(dfile):
            continue  # owned by another session — not an orphan
        orphans.append(dfile)

    # R1 (staff-eng, 2026-08-03) — supersedes the earlier per-arm Step 5
    # `agent_race_paths` surgery this replaced (it fixed only the
    # agent-race non-candidate shape, and in doing so widened `skipped`'s
    # documented meaning — see this function's own Step 4 docstring
    # paragraph (d)/(e) and `ScopeResult.skipped`'s docstring — to include
    # paths that were never candidates at all; example-doctrine-repo's coordinator-safe-commit
    # renders `skipped` as "skipping <path> — owned by session <owner>", so
    # an operator saw a skipping line for a file they never touched). A
    # single call-level flag instead: non-empty `unreadable_other_sessions`
    # or `agent_race_paths` means at least one claim set this call could not
    # attribute was withheld somewhere, so `orphans` (which drains every
    # fail-closed withhold arm by construction — see `ScopeResult.orphans`'s
    # own docstring) is not a trustworthy adoption allow-list for THIS call,
    # regardless of whether the specific withheld path happened to be a
    # candidate. See `ScopeResult.indeterminate`'s own docstring for the
    # full accounting, including the residual it does NOT cover.
    indeterminate = bool(unreadable_other_sessions) or bool(agent_race_paths)

    # Review: staff-eng P2 — wrap the real dict in the SAME immutable type
    # as ScopeResult.attribution's default, so the field's runtime type is
    # uniform across every code path (see that field's own docstring and
    # default-value comment for why a divergent type here is the worse
    # shape than the mutable-default hazard it would otherwise "fix").
    return ScopeResult(
        my_scope, skipped, orphans, types.MappingProxyType(attribution), indeterminate
    )


def archive(sid: str, cwd: Optional[str] = None) -> bool:
    """Port of ``cs_archive <session_id>``: move the
    session dir to ``<sessions_dir>/.archive/<sid>-<YYYY-MM-DD>/``.

    Called AFTER the final commit completes (archive-after-commit). Idempotent
    — a missing session dir returns ``True`` (already archived or never
    existed). The archive date is the LOCAL date (bash ``date +%Y-%m-%d``),
    falling back to ``"unknown"`` if the clock read fails.

    Returns ``True`` on success (or idempotent no-op), ``False`` on failure
    (not in a git repo → bash ``return 1``; ``mv`` failure → bash ``return
    1``). Raises ``ValueError`` if ``sid`` is empty (bash ``${1:?}``).

    Also clears the write-confinement bump's settings-home anchor record for
    ``sid`` (docs/plans/2026-08-03-write-bump-anchor-outside-the-guarded-repo.md
    AC12) — this is the live seam example-doctrine-repo's real ``SessionEnd`` hook actually calls
    (via ``coordinator/bin/wsc-close.py archive-session``), so the settings-home
    anchor's session-ephemera cleanup is anchored here rather than in
    ``sweep_stale_markers``, which has no production caller. Exact-``sid``
    match, no prefix matching — a different record from ``_write_bump_marker.py``'s
    marker (C6's surface), with no shared collision hazard and no coordination
    needed between the two. Best-effort and independent of this function's own
    return value: the settings-home cleanup is fail-open by construction (see
    ``delete_settings_home_session_record``'s own docstring), so it can never
    turn a successful in-repo archive into a failure, nor vice versa.
    """
    if not sid:
        raise ValueError("session_id required")

    from coordinator_core.bash_guards._write_bump_session_start import (
        delete_settings_home_session_record,
    )

    delete_settings_home_session_record(sid)

    base = core.sessions_dir(cwd)
    if not base:
        return False  # not in a git repo — bash `|| return 1`
    sdir = os.path.join(base, sid)

    if not os.path.isdir(sdir):
        return True  # already archived or never existed — idempotent

    try:
        today = datetime.now().strftime("%Y-%m-%d")
    except Exception:
        today = "unknown"

    archive_dir = os.path.join(base, ".archive", f"{sid}-{today}")
    try:
        os.makedirs(os.path.join(base, ".archive"), exist_ok=True)
        shutil.move(sdir, archive_dir)
    except OSError:
        return False
    return True
