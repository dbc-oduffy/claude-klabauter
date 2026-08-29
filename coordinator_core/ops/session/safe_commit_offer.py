"""
coordinator_core.ops.session.safe_commit_offer — EM-initiated commit+push of
a session's own claimed dirty paths, offered by a ceremony the operator ran.

NOT automatic, NOT unattended, NOT stop-event driven. This line said exactly
that until 2026-08-27 and was wrong: the SessionEnd registration it described
had been retired, and a summary line is the most-read text in a module, so it
outranked the corrected paragraph below it for every reader who stopped at
the first sentence. If a future change reinstates an unattended trigger, this
sentence is the first thing that must change with it.

PIVOT (PM ruling, 2026-07-31, superseding this module's original "offer,
then one confirmation" shape): "I get annoyed when I'm asked if there should
be a commit or not. y'all are the engineers... I don't want work lost." The
confirmation step this module originally computed FOR is gone — being asked
whether to commit was itself the defect. RECORDED AS HISTORY, and reversed
since: at the time of that PIVOT, the module's own SessionEnd-hook trigger
was still registered, so an unattended, unconfirmed commit could fire with
nobody watching, and "offer" briefly read as a misnomer for what the module
had become. That trigger registration has since been retired — verified this
session against the DoE-claude repo's `coordinator/hooks/hooks.json`, whose
`SessionEnd` array registers only `sessionend-archive-session.py`, not this
module. Every surviving caller today is an EM-initiated ceremony
(`/handoff`'s dirty-tree residue and `/quick-wrap` step 1) — a session, not a
stop event, choosing to commit while still running and still able to answer
for it. `/workstream-complete` was named here as a third caller until
2026-08-27 and never was one: doe-claude-em read the skill on their side and
found one `SKILL.md`, no residue directory, and no hit for this mechanism
under any name — its dirty-tree handling is its own case-a/b/c classification
and its commit tail routes through `snippets/scoped-commit-route.md`. A
caller list is the first thing a reader trusts and the last thing anyone
re-derives, so a name that was never in it costs a search every time. The module genuinely offers again, `safe_commit_offer` describes it
correctly, and the name stays; a future reader must read this paragraph as
the reversal's record, not as license to reinstate a SessionEnd registration
this module no longer expects.

The REAL failure mode this exists to prevent, per the PM, restated: not
primarily cross-session sweeps (those are accepted collateral of a many-EM
workflow — see below) but sessions finishing real work and never committing
it at all. A dirty tree with 40+ uncommitted paths, some from sessions that
are simply gone, is data loss waiting on a machine failure. That failure mode
is unchanged; what changed is who is responsible for catching it. This module
no longer has a stop-event trigger, so it cannot catch the session that dies
without committing — `/workday-complete`'s dirty-tree sweep
(`coordinator_core/ops/workday_complete_step2_5_dirty_tree.py`) is the named
backstop for that case, and retiring the unattended trigger was accepted on
exactly that basis. See `docs/wiki/scoped-safety-commits.md` § 3b.
`coordinator/hooks/scripts/sessionend-auto-commit.py` (DoE-claude side) still
exists on disk but is NO LONGER REGISTERED and no longer calls this module —
do not read it as a live caller.

Composition, not new computation — ONE primitive since the 2026-08-21
rebuild:
  - `coordinator_core.session.claim_index.commit_set` — "what belongs to this
    session to commit right now", a zero-spawn projection of the claim
    index's own reverse map.

(`session.claims.my_agent_touched` is still imported here, but only by the
helpers `session.scope` borrows — see their section comment. Nothing on
`compute_offer`'s own path reads it.)

WHAT THE REBUILD REMOVED, recorded here so a reader who finds a gap where a
mechanism used to be does not helpfully rebuild it. `compute_offer` composed
`session.scope.compute_scope` with `session.claims.my_agent_touched`, paying a
git subprocess per candidate: 73 processes and 5,609ms of kernel+user CPU per
call, twice per close ceremony, in the op every workstream in the fleet passes
through. Killed at `e927d9463`, rebuilt here. Three things went with it, each
deliberately:

  - THE EXACT/BROADENED MODE CHOICE, and the live hazard it carried.
    `my_agent_touched("broadened")` returns an IDENTICAL union for every
    session id (candidate-set-only, not per-session attribution, by its own
    docstring — confirmed 2026-07-31 on a live repo with 5 concurrent
    sessions), so an unattended committer driven off it would hand the same
    dirty paths to whichever stop event fired first and commit them under
    that session's message: an automated reproduction of the bare-commit
    sweep this whole mechanism exists to prevent. It was correct only
    because one call site passed one literal. `claim_index.rebuild` resolves
    an agent's claim back to its owning EM session through the
    `.agents/<aid>/em-session-id.txt` back-pointer, so per-session
    attribution is no longer a MODE and cannot be selected wrongly.

  - THE STEP-2 MTIME FALLBACK, and with it the adoption question.
    `compute_scope` treated a dirty file carrying no claim anywhere as
    possibly-mine when its mtime post-dated this session's `started_at`.
    The rebuilt answer reads claims and nothing else: an unclaimed file is
    not this session's, whatever its mtime says. See `compute_offer`'s
    `orphans` contract for why that key is now always empty, and why
    repopulating it means re-adding a worktree read this answer must not
    have.

  - THE `.agents/<aid>/touched.txt` PATH-DIALECT JOIN, from THIS answer.
    `claim_index` reads those files through the shared
    `session.path_dialect` canonicalizer, so `compute_offer` no longer has a
    dialect question of its own. The four helpers that answered it
    (`_normalize_agent_touched_entry` and the dirty-directory expansion
    around it) are still in this file and still correct -- `session.scope`
    imports them by name -- but they are no longer this module's own path.
    See their section comment below.

What did NOT change: the advisory-only disposition of a path left uncommitted
(DR-227) — it is named in the diagnostics sink (`_log_excluded_diagnostic`)
and never gated or blocked. (This sentence used to open with "the SessionEnd
trigger reliability this module's CLI exists for". That trigger is retired,
and so is the CLI; the OP exists for the ceremonies now.)

THE ``invoker`` PARAMETER IS DELETED (2026-08-27) and must not come back
without a producer. It framed a commit three ways — ``"unattended"`` (a
stop-event rescue), ``"attended"`` (a deliberate ceremony), and undeclared.
The SessionEnd registration that was ``"unattended"``'s only real caller was
retired, no engine caller ever passed any value, and doe-claude-em confirmed
neither surviving ceremony call site passes one or intends to. It was kept for
a while on the theory that it was a wire surface a sibling's skills named — that
theory was never checked with the sibling, and was wrong.

What survives is the UNDECLARED framing, because it is the only one that is
true of every caller. ``"attended"`` asserted a deliberate ceremony, which this
op cannot know: it answers on the warm door and anything that can dial it can
reach the mechanical fallback. A commit body may say how the paths were
bucketed; it may not claim why the commit happened. Read the retired
"Stop-event safety net" framing as describing a shape nothing
currently produces, and if you are here because you found that framing in a
real commit message, that is a finding worth chasing: it means something
started declaring itself unattended again.

Read-only halves stay read-only; the mutating half
(`commit_session_offer`/`commit_session_offer_async`) is the ONLY part of this
module that stages, commits, or pushes — and it does so by composing an
ALREADY-EXISTING commit primitive in-process, never a hand-rolled
`git commit`. That primitive is
`coordinator_core.git.commit.commit_paths`, called directly (see
`_commit_group`) -- repointed (C4, docs/plans/2026-08-29-the-push-subsystem-
leaves-and-then-the-pipeline-can-go.md) off the killed `run_commit_pipeline`,
mirroring `execute_plan_assemble.close_out_and_stamp`'s own repoint (C3 of
the same plan). It is NOT `ceremony.scoped_git_commit`: that op was
DELETED 2026-08-23 over the DR-344 brightline and is registered nowhere
outside a test fixture, so resolving it by name gets "Method not found".
This module never owns a push: `_commit_group` never pushes, and publication
is left to `coordinator_core.hooks.auto_push`'s post-commit git hook and
whichever cadence checkpoint runs `push_outstanding()` next.

Multi-session overlap on the SAME file remains accepted collateral, by
explicit PM ruling — this module does not attempt conflict resolution for
two sessions that both legitimately touched one file; it only prevents ONE
session's unattended auto-commit from sweeping a peer's UNRELATED work.

Spec backlink: DoE-claude state/sizings/2026-07-31-safe-commit-offer-at-
session-stop-events.yaml
"""

from __future__ import annotations

GENERATES = []  # only direct file write is an append to coordinator-sessions/logs/sessionend-auto-commit-diagnostics.log under the git common dir; actual commits delegate to coordinator_core.git.commit.commit_paths, not written here

import asyncio
import posixpath
import subprocess
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional, Sequence, Tuple, TypedDict

from functools import partial

from coordinator_core.git.commit import (
    CommitRefused,
    FilterUnsupported,
    commit_paths,
    hash_worktree_blobs_via_spawn,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.push import PUSH_STATUS_NOT_ATTEMPTED
from coordinator_core.ops.dirty_tree_gate import parse_porcelain_paths
from coordinator_core.session import core
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.session import claim_index
from coordinator_core.session.claims import my_agent_touched
from coordinator_core.session import scope as scope_module
from coordinator_core.session.liveness import _ABANDONMENT_WINDOW_SEC, live_session_ids

class ExcludedPath(TypedDict):
    path: str
    reason: str


class PeerOwnedPath(TypedDict):
    """One path this call attributes to a NAMED peer/agent claim -- the
    serialized-for-the-wire projection of one `coordinator_core.session.
    scope.OwnerFact` entry (a `NamedTuple`, which cannot itself cross an op
    boundary as JSON). WIRE SHAPE, decided here (C5,
    docs/plans/2026-08-05-in-process-writers-declare-their-writes.md): one
    dict per attributed path, fields named identically to `OwnerFact`'s own
    (`owner`/`liveness`/`claim_source`) plus the `path` key itself, so a
    consumer never has to re-derive the mapping key -> value pairing by hand.
    `liveness`/`claim_source` mirror `OwnerFact`'s own `Literal` value sets
    verbatim -- see that class's docstring in `coordinator_core.session.scope`
    for what each value means. Since the 2026-08-21 rebuild `compute_offer`
    only ever EMITS `claim_source="session"`: the claim index resolves an
    agent's claim back to its owning EM session before any reader sees it, so
    the `"agent"`/`"agent-race"` distinction has no producer left here. The
    value set is kept whole rather than narrowed -- it is a wire shape two
    sibling plans consume, and narrowing a Literal a consumer already
    switches on is a breaking change for no gain.

    C3 (docs/plans/2026-08-27-safe-commit-offer-excludes-a-live-agent.md)
    restores a real producer for `claim_source="agent"`: `compute_offer`
    now emits it for a path this session holds SOLELY through a live
    dispatched agent's own touch record (`claim_index.CommitSet.
    in_flight_agent_claims`) -- the owning session has not directly claimed
    it, so the claim is not this session's own to fold into `mine` while
    that agent is still in flight. `"agent-race"` still has NO producer
    anywhere in this package -- it would name two LIVE claimants racing on
    the SAME path (this session's own claim vs. a peer's), a case this
    chunk does not create a code path for and does not attempt to detect.
    Kept in the `Literal` for the same reason it was kept before: it is a
    wire shape two sibling plans consume, and narrowing a value set a
    consumer already switches on is a breaking change for no gain.

    Never constructed for a claim this call cannot stand behind -- see
    `compute_offer`'s ownership paragraph (AC7: never print an owner this
    call cannot stand behind)."""

    path: str
    owner: str
    liveness: Literal["live", "dead", "undetermined"]
    claim_source: Literal["session", "agent", "agent-race"]


class OwnershipReadout(TypedDict):
    """The four-bucket, per-session ownership readout (C5) -- EXTENDS the
    post-commit residue report C3 shipped (`CommitOfferReport.residue`,
    `SafeCommitOffer.excluded`), it does not replace either. Those stay
    candidate-set-only, by their own docstrings; this is the surface that
    answers "who does the claim index say holds this path", not merely "is
    this path safe to adopt".

    Buckets, mutually exclusive by construction:
      - ``mine``         — this session's own claimed paths. Identical
        to `SafeCommitOffer.safe_paths` at the same call -- duplicated here
        (not a reference to the sibling key) so a caller consuming ONLY this
        one key gets a complete, self-contained four-bucket answer without
        also having to thread `safe_paths` through separately.
      - ``peer``          — every CONTESTED path: one this session holds
        that a peer holds too. Named with an EARNED liveness verdict (see
        `_peer_liveness`) rather than an asserted one.
      - ``unattributed``  — every contested path this call cannot stand
        behind an owner name for, which post-rebuild means: every one of
        them, whenever ``degraded`` is set, and none otherwise. Per DR-258
        this bucket can NEVER be emptied by any heuristic in the general
        case: a peer's Bash-authored write carries no claim anywhere any
        reader can see, and is genuinely indistinguishable from nobody's
        file. Do not chase it toward zero -- it is the honest default, not
        residue to optimise away.
      - ``degraded``      — mirrors `SafeCommitOffer.indeterminate` for
        THIS call. `True` means the index walk behind this answer was
        incomplete -- a claim set it could not read, an I/O error, or its
        wall-clock cap -- so both other buckets may be SHORT. When set, ``peer`` is returned empty
        OUTRIGHT and every non-mine path this call saw (including one that
        DOES have a resolvable `OwnerFact` from some earlier, still-valid
        read) is folded into ``unattributed``: this call cannot stand
        behind ANY owner name it would otherwise print (AC7's binding
        requirement), so it renders the honest degraded shape rather than a
        partially-trustworthy one. A caller SHOULD render a call-wide
        banner when this is `True` rather than silently trusting `peer`
        (empty) as "nobody else has claims" -- it means "this call could not
        tell you", not "there are no peer claims"."""

    mine: List[str]
    peer: List[PeerOwnedPath]
    unattributed: List[str]
    degraded: bool


class SafeCommitOffer(TypedDict):
    session_id: str
    safe_paths: List[str]
    excluded: List[ExcludedPath]
    orphans: List[str]
    indeterminate: bool  # staff-eng P3 (2026-08-03, pass 3) — mirrors the
    # claim index walk's own `complete` bit, surfaced here so a caller
    # composing ONLY compute_offer can still distinguish "this path is
    # genuinely unclaimed" from "the walk behind this answer was incomplete,
    # so it may be short". See `compute_offer`'s own contract.
    ownership: OwnershipReadout  # C5 (2026-08-05 in-process-writers-declare-
    # their-writes plan) — the four-bucket ownership readout (mine / named
    # peer / unattributed / degraded), extending C3's post-commit `residue`
    # report rather than replacing it. ADDITIVE ONLY: every pre-existing key
    # on this TypedDict is unchanged in shape and meaning (two live sibling
    # plans consume this shape verbatim -- see the plan's § Cross-plan
    # coordination). See `OwnershipReadout`'s own docstring for the bucket
    # contract and `compute_offer` for how it is derived from the claim
    # index.


class CommitGroup(TypedDict, total=False):
    paths: List[str]  # required in practice; total=False only to make `prose` optional
    message: str  # required in practice; the commit SUBJECT line
    prose: str  # optional — the commit message BODY (see `_commit_group`)


class GroupResult(TypedDict):
    paths: List[str]
    message: str
    committed: bool
    sha: Optional[str]
    push_state: Optional[str]
    error: Optional[str]
    commit_failed: bool  # True iff this group genuinely failed to commit (a
    # gate or the commit subprocess itself) -- False for both a landed
    # commit AND a benign no-op (paths already committed / handler not
    # reached). Distinct from `error`, which is None on the benign no-op
    # even though `committed` is also False there -- see `_commit_group`.
    reason: Optional[str]  # Review: code-reviewer (Finding 3) — the op's own
    # benign-no-op reason (e.g. "empty-commit-set"), threaded through so
    # `_render_report`'s benign branch can say WHY, not merely that it was a
    # no-op. `None` on a landed commit or a genuine `commit_failed`.


class DroppedGroup(TypedDict):
    """One caller-supplied `CommitGroup` (handoff item 1,
    `state/handoffs/2026-08-03-touched-path-bookkeeping.md`) that lost some
    or all of its named paths to `safe_set` filtering in
    `commit_session_offer_async` -- i.e. `len(kept) < len(g["paths"])`,
    total-drop and partial-drop alike. A group that loses every path
    previously vanished from `groups`, `failed_groups`, and `excluded`
    (which is `compute_offer`-derived, not group-derived) all at once --
    silent to both the operator and the diagnostics-log sink. This is that
    group's own record, keyed on the caller-supplied `message` rather than
    the (possibly now-empty) `paths` list, since an all-dropped group has no
    surviving path to key on.

    ``named`` -- how many paths the caller put in this group. ``matched`` --
    how many of them were actually in this session's own computed
    `safe_paths`; `matched < named` is the entry's own reason for existing.
    Never populated for `_default_groups` output (the unattended-trigger
    fallback): that grouping is computed FROM `safe_paths` itself, so it can
    never lose a path to this filter -- see `commit_session_offer_async`."""

    message: str
    named: int
    matched: int


class CommitOutcome(TypedDict):
    """C4 (2026-08-20 the-close-ceremony-commits-what-the-session-wrote plan)
    -- the structured, caller-renderable verdict for ONE
    `commit_session_offer_async` call, additive alongside `groups`/
    `failed_groups`/`residue` (never a replacement for any of them; those
    still carry their own per-group detail). AC9's own requirement: "return
    the outcome ... as a structured result the caller can render, not only a
    log line" -- this is that result. C5 folds this into each ceremony's own
    close output; this module only computes and returns it.

    ``status`` -- exactly one of:
      - ``"committed"`` -- at least one path landed in at least one group
        this call. ``committed_paths`` names them.
      - ``"empty"`` -- nothing to commit this call (no claimed paths, or
        every claimed path was already committed / dropped) and no
        degraded/conflict reason applied.
      - ``"skipped_indeterminate"`` -- (a) `offer["indeterminate"]` was
        `True` this call: the claim index walk behind the answer was
        incomplete, so this call commits NOTHING -- a degraded claim read
        means attribution is untrustworthy call-wide, not that the
        unattributed paths are free. Mirrors `compute_offer`'s own
        `indeterminate` contract; see that field's docstring.
      - ``"skipped_degraded"`` -- (a) `offer["ownership"]["degraded"]` was
        `True` this call (the same signal, read via the ownership readout
        instead) -- same fail-closed response, nothing committed.
      - ``"dirty_conflict_skipped"`` -- (c) at least one claimed path was
        ALSO present in this same call's own `ownership["peer"]` bucket (a
        co-resident peer claim on a path this session also claims -- see
        `OwnershipReadout`'s own docstring: the two buckets are mutually
        exclusive by construction today, so this is a defensive belt-and-
        braces check against future drift in that invariant, not a path
        expected to fire under the current `compute_offer` contract).
        `conflicted_paths` names what was withheld; anything else this call
        claimed still committed normally, so a call CAN be both
        `"dirty_conflict_skipped"` (partial withhold) and still land some
        paths -- see `committed_paths`.

    ``detail`` -- one human-readable sentence naming why, never a bare
    status word alone.

    ``committed_paths`` -- every path that actually landed in a `committed`
    group this call, across every group, flattened. Empty for every
    non-``"committed"``-adjacent status (a `"dirty_conflict_skipped"` call
    CAN still populate this if some paths committed alongside the
    withhold -- see that status's own note above).

    ``conflicted_paths`` -- populated ONLY by the (c) dirty-conflict check;
    empty for every other status. A path here was in this call's OWN
    computed `safe_paths` but ALSO named in `ownership["peer"]` -- withheld
    from every group before any commit call, never
    partially staged then rolled back.

    (b) post-stage verify (brief item (b): `git diff --cached --name-only`
    after staging, compared against the expected claim set) is NOT
    represented in this TypedDict -- staging itself is owned by
    `coordinator_core.git.commit.commit_paths`, which this module composes
    in-process rather than reaching into (see this module's own docstring,
    "Composition, not new computation"). Implementing a true post-STAGE
    (pre-commit) verify would require observing that call's index state
    mid-call, which is outside this chunk's `writes:` scope -- named here as
    a follow-up chunk against `coordinator_core.git.commit` itself, not
    implemented in this module."""

    status: Literal[
        "committed",
        "empty",
        "skipped_indeterminate",
        "skipped_degraded",
        "dirty_conflict_skipped",
    ]
    detail: str
    committed_paths: List[str]
    conflicted_paths: List[str]


class Reconciliation(TypedDict):
    """What this call actually CHECKED the write ledger against, and what the
    check found — the answer to "is this offer's confidence earned".

    Exists because the ledger is append-only and write-only: `hooks.
    track_touched_files` records a path on the Write/Edit route and observes
    nothing else, so it drifts in BOTH directions. It under-records (a path
    written through a shell carries no claim — DR-258, a named permanent
    limit) and it over-records (a path claimed and later deleted keeps its
    claim, because the recording path requires an existing regular file and
    so can never self-report a deletion).

    ``reconciled`` IS NOT A HEALTH FLAG, and this is the whole point of
    having it. It answers ONLY "did the ledger-versus-tree check run on this
    call" — never "did the ledger and the tree agree". A permanently
    non-empty ``claimed_absent`` is expected on a repo that closes queue
    entries by ``git mv`` (doe-claude-em, 2026-08-29, citing their SC-DR-021
    d1: the deletion side of an archival move can never be self-reported, so
    the population is by construction and forever). Had this been a health
    flag it would read red in steady state, and a signal that is always red
    is a signal nobody reads.

    DELIBERATELY DISTINCT FROM ``OwnershipReadout.degraded``, which is
    ``not CommitSet.complete`` — "the index WALK finished", a statement about
    the claim ledger's own readability and nothing at all about the tree.
    The two are independent: a complete walk over a stale ledger is
    ``degraded: False`` with ``reconciled: True`` and a non-empty
    ``claimed_absent``. Do NOT fold this into ``degraded`` however tempting
    the shorter shape looks — ``degraded`` carries a second, load-bearing
    consequence (when set, ``ownership.peer`` is emptied outright and every
    non-mine path folds into ``unattributed``, AC7), so overloading it would
    silently trigger that fold on a healthy walk.
    """

    reconciled: bool  # did the check run at all — False when there was no
    # worktree root to check against, or the call short-circuited before
    # reaching the check (a degraded/indeterminate skip). NEVER an assertion
    # that the ledger and the tree agree.
    claimed_absent: List[str]  # paths this session claims that have no file
    # on disk. NOT automatically wrong: a deletion this session made is a
    # legitimate thing to commit, and `run_commit_pipeline` handles deletions
    # and reports `empty-commit-set` on a no-op. Named, never adjudicated.
    unclaimed: List[str]  # every dirty path this call saw that NO session
    # claims — the adoption CANDIDATE set, enumerated in full here while
    # `_render_report` samples it. Enumeration is the load-bearing property,
    # not the rendering: doe-claude-em's SC-DR-022 half 1 permits an operator
    # to adopt an unclaimed path with `--include-orphans`, and the verified
    # property that makes that remedy safe is that "the named paths half 1
    # permits are named by the ENGINE, not assembled by the adopter". An
    # aggregate count supplies no candidate list, so the remedy has no input.
    # NAMED, NEVER ADOPTED: nothing here is committed, and nothing here is
    # attributed to this session. Most entries on a shared worktree belong to
    # nobody in particular and some belong to peers who have not claimed
    # them yet — see this module's own DR-258 note on why chasing this bucket
    # toward zero is not a goal.


class CommitOfferReport(TypedDict):
    session_id: str
    groups: List[GroupResult]
    excluded: List[ExcludedPath]
    failed_groups: List[GroupResult]  # subset of `groups` with
    # `commit_failed` True -- surfaced separately (2026-07-31 fix) so a
    # caller (the SessionEnd hook) can detect and report a genuine
    # commit/gate failure without re-deriving it from `groups` + `error`,
    # and without conflating it with the benign already-committed no-op
    # shape, which must stay quiet (see module docstring's wolf-crying
    # constraint).
    dropped_groups: List[DroppedGroup]  # handoff item 1 (2026-08-03,
    # touched-path-bookkeeping) -- one entry per caller-supplied `groups`
    # entry that lost some or all of its named paths to the `safe_set`
    # filter, empty when `groups` is `None` (the computed `_default_groups`
    # path can never drop a path it did not itself put there). ADVISORY
    # ONLY, same as `excluded`/DR-227 -- never a gate, never changes
    # `main`'s exit code, and never widens `resolved_groups`/the commit
    # boundary; see `DroppedGroup`'s own docstring for the shape.
    residue: "OrderedDict[str, List[str]]"  # C3 (2026-08-05 engine-ops-
    # declare-what-they-write plan) -- REPORT-ONLY, never a gate: every dirty
    # path still present in `git status --porcelain` AFTER the commit groups
    # above landed, MINUS whatever this call actually committed and MINUS
    # any path a live peer session already owns (per a FRESH `compute_offer`
    # re-read taken immediately before residue is computed, post-commit --
    # see `_compute_residue`, Review: code-reviewer Finding 2), grouped by
    # top-level
    # `state/` class. Purely additive: nothing here feeds back into
    # `safe_set`/`resolved_groups` in `commit_session_offer_async`, so it
    # cannot widen the commit boundary (AC4, negative-spec: "do not widen
    # what any ceremony commits"). Empty is the common case and is not an
    # error -- an empty `residue` after a healthy commit is exactly what a
    # correctly-scoped ceremony should leave behind.
    reconciliation: Reconciliation  # 2026-08-29 — what this call checked the
    # write ledger against, and what it found. REPORT-ONLY, never a gate:
    # nothing here feeds back into `safe_set`/`resolved_groups`, so it cannot
    # widen the commit boundary, exactly like `residue`/`excluded` above.
    # See `Reconciliation`'s own docstring for why `reconciled` is a
    # did-the-check-run flag and NOT a health flag.
    outcome: CommitOutcome  # C4 (2026-08-20 the-close-ceremony-commits-what-
    # the-session-wrote plan, AC9) -- the structured, caller-renderable
    # verdict for this call (committed / degraded-or-indeterminate skip /
    # dirty-conflict fail-closed / empty). See `CommitOutcome`'s own
    # docstring for the bucket contract. Additive: every other key on this
    # TypedDict keeps its pre-existing shape and meaning.


# ---------------------------------------------------------------------------
# Path normalization for the `.agents/*/touched.txt` fan-out
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sub-agent fan-out candidate resolution -- NO LONGER THIS MODULE'S OWN.
#
# The 2026-08-21 rebuild took `compute_offer` off these four helpers entirely
# (see its docstring for what went and why). They stay here because
# `coordinator_core.session.scope` imports them BY NAME at three call sites
# (`compute_scope`'s own Step 3b candidate resolution), and that module -- still
# reached by `coordinator/bin/coordinator-safe-commit.py` -- is now their ONLY
# consumer. They were left in place rather than moved: relocating them into
# `scope.py` means untangling a two-way import, which is its own change and not
# this one. What must NOT happen is `compute_offer` growing a call back to them
# -- that is the 73-process shape, reassembled.
# ---------------------------------------------------------------------------


def _normalize_agent_touched_entry(entry: str) -> Optional[str]:
    """Repo-relative-ize one raw `.agents/<aid>/touched.txt` line.

    POST-C2: entries are already repo-relative (the writer,
    `coordinator_core.hooks.track_touched_files`, emits clean repo-relative
    paths directly) — this only normalizes separators/`.`/`..` segments
    in-place and rejects anything that is not, in fact, repo-relative. NO
    join onto a plugin-directory prefix happens here (see module docstring's
    "Path-format fix, SUPERSEDED" note for why that join existed and why it
    is gone).

    Returns ``None`` for an entry that resolves outside this repo (a
    `../`-escaping entry, cross-repo fan-out into a sibling repo, or an
    out-of-repo scratch path) — dropped, never passed through as a
    candidate. A directory entry (trailing ``/``) keeps its trailing slash
    in the return value; the caller expands it.
    """
    if not entry:
        return None
    # Reject absolute-path entries up front — covers all three shapes
    # multi-OS demands: POSIX-absolute, backslash-absolute (pre-
    # normalization), and a Windows drive-letter prefix.
    raw_check = entry.strip()
    if (
        raw_check.startswith("/")
        or raw_check.startswith("\\")
        or (len(raw_check) >= 2 and raw_check[1] == ":" and raw_check[0].isalpha())
    ):
        return None
    entry = entry.replace("\\", "/")
    is_dir = entry.endswith("/")
    stripped = entry.rstrip("/")
    if not stripped:
        return None
    combined = posixpath.normpath(stripped)
    if combined in (".", "..") or combined.startswith("../") or posixpath.isabs(combined):
        return None
    return combined + "/" if is_dir else combined


def _dirty_files_under(dir_path: str, cwd: Optional[str]) -> List[str]:
    """Repo-relative dirty files (tracked-modified ∪ untracked) currently
    under ``dir_path`` — a small, targeted git query (NOT a repo-wide scan)
    used only to expand a directory entry from the agent fan-out into
    individual file candidates. Fails closed (empty list) on any git error.

    ``core.quotepath=false`` is REQUIRED, not cosmetic: these paths are
    matched against ``compute_scope``'s repo-wide dirty set, which sets the
    same flag. With git's default quoting a non-ASCII path would come back
    C-escaped from one scan and raw from the other, so an expanded claim
    would silently fail the membership test and be pruned — widening the
    committer's allow-list, the direction ``compute_scope``'s fail-closed
    invariant forbids. The two scans must share one dialect.
    """
    out: List[str] = []
    for args in (
        ["-c", "core.quotepath=false", "diff", "--name-only", "HEAD", "--", dir_path],
        [
            "-c",
            "core.quotepath=false",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            dir_path,
        ],
    ):
        try:
            result = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                cwd=cwd,
                **no_console_creationflags(),
            )
        except OSError:
            continue
        if result.returncode != 0:
            continue
        out.extend(line for line in result.stdout.splitlines() if line)
    return out


def _dirty_files_under_batch(
    dir_paths: Sequence[str], cwd: Optional[str]
) -> "OrderedDict[str, List[str]]":
    """Batched sibling of `_dirty_files_under` — ONE `git diff` + ONE
    `git ls-files` pathspec-batched over every ``dir_paths`` entry, instead
    of that same pair of spawns once per directory entry (the N+1 shape this
    chunk exists to retire; see
    `_resolve_agent_touched_candidates`, its sole caller in this module).

    § Anti-scope 1/2/4: this is OBJECT/PATHSPEC batching, not range
    batching — `git status`/`git diff` over multiple pathspecs resolves each
    pathspec independently server-side, so handing git N directory
    pathspecs in one call is safe in a way collapsing N independent
    reachability RANGES into one expression is not (see this module's other
    git calls, which never do the latter).

    Same worktree-dirtiness read as `_dirty_files_under`
    (`git diff --name-only HEAD` ∪ `git ls-files --others
    --exclude-standard`) and the same deliberate omission of
    `--no-optional-locks` — C1's `dirty_tree_gate` batching (commit
    `9e3084df78e5`, `commit_gates.py`) made the identical choice for the
    identical reason (§ Anti-scope 14): suppressing the lock can leave
    phantom-dirty state permanently unresolved, which is worse than paying
    the lock cost here.

    Returns an `OrderedDict` keyed by EVERY entry of ``dir_paths``, in the
    order first seen, duplicates collapsed onto their first occurrence —
    never a subset. § Anti-scope 25 (absence must never silently read as
    "clean"): a directory entry present in the input is ALWAYS present as a
    key in the output, with an EMPTY list value meaning "queried, found
    nothing dirty under it" — this function never uses key-absence to mean
    either "clean" or "not asked"; a key is absent from the return value
    only when it was never in ``dir_paths`` to begin with. Mirrors
    `emit/sections/handoffs.py`'s `_resolve_shipped_in_dates` shape (a
    pre-seeded map plus prefix-match against the batched query output),
    cited per this chunk's brief rather than re-derived.

    A file may be reported under more than one ``dir_paths`` entry if the
    entries themselves nest (e.g. ``"a/"`` and ``"a/b/"`` both present) —
    each pathspec resolves independently per the anti-scope note above, so
    each entry's own membership test is answered on its own terms.

    Fails closed (every value empty) on any git error, same as
    `_dirty_files_under` — a git hiccup must never widen a caller's
    candidate set.
    """
    result: "OrderedDict[str, List[str]]" = OrderedDict()
    for d in dir_paths:
        result.setdefault(d, [])
    if not result:
        return result

    unique_dirs = list(result.keys())
    raw_files: List[str] = []
    for args in (
        ["-c", "core.quotepath=false", "diff", "--name-only", "HEAD", "--", *unique_dirs],
        [
            "-c",
            "core.quotepath=false",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            *unique_dirs,
        ],
    ):
        try:
            proc = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                cwd=cwd,
                **no_console_creationflags(),
            )
        except OSError:
            continue
        if proc.returncode != 0:
            continue
        raw_files.extend(line for line in proc.stdout.splitlines() if line)

    seen_files: set = set()
    for f in raw_files:
        if f in seen_files:
            continue
        seen_files.add(f)
        for d in unique_dirs:
            if f.startswith(d):
                result[d].append(f)
    return result


def _resolve_agent_touched_candidates(session_id: str, cwd: Optional[str]) -> List[str]:
    """Repo-relative, de-duplicated, order-preserving candidate list from
    this session's own dispatched sub-agent fan-out (`"exact"` mode only —
    see module docstring).

    Directory entries (trailing ``/``) are expanded via ONE batched
    `_dirty_files_under_batch` call over every directory entry this
    session's fan-out named, rather than one `_dirty_files_under` spawn
    pair per entry — the N+1 shape this chunk (C16) retires. Per-entry
    order is preserved: a directory entry's expanded files are spliced in
    at that entry's own position, identically to the pre-batch per-call
    behaviour, only the git spawn count changes.
    """
    raw = my_agent_touched(session_id, "exact", cwd)
    normalized: List[Optional[str]] = [_normalize_agent_touched_entry(e) for e in raw]
    dir_entries = [n for n in normalized if n is not None and n.endswith("/")]
    dirty_by_dir = _dirty_files_under_batch(dir_entries, cwd)

    resolved: List[str] = []
    for norm in normalized:
        if norm is None:
            continue
        if norm.endswith("/"):
            resolved.extend(dirty_by_dir.get(norm, []))
        else:
            resolved.append(norm)

    seen = set()
    out: List[str] = []
    for p in resolved:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Pathspec computation (pure, read-only)
# ---------------------------------------------------------------------------


def _peer_liveness(peers, cwd: Optional[str]) -> str:
    """The ONE liveness verdict this module prints for a peer claim, earned
    rather than asserted (the rule `scope_report._classify_denied_path`
    already carries, applied here at the other surface that names owners).

    ``"undetermined"`` on an EMPTY live set as well as on a raise:
    ``live_session_ids``' documented contract returns an empty frozenset on
    error, which is indistinguishable from "nothing is live", so an empty
    result is UNKNOWN and never DEAD. In-process (psutil, not git) -- this
    adds no subprocess to the answer.
    """
    try:
        live = live_session_ids(cwd)
    except Exception:  # noqa: BLE001 - a readout must never raise
        return "undetermined"
    return _liveness_from_set(peers, live)


def _liveness_from_set(owner: str, live) -> str:
    """The verdict itself, against an ALREADY-RESOLVED live set -- so a caller
    with many owners to label resolves that set once instead of once per owner
    (see `full_ownership_map` for the 23,910ms this split removed).

    ``None`` or an EMPTY set both read ``"undetermined"``, never ``"dead"``:
    `live_session_ids` returns an empty frozenset on error, which is
    indistinguishable from "nothing is live", and a wrong DEAD is the verdict
    that gets a live peer's work committed over.
    """
    if not live:
        return "undetermined"
    return "live" if owner in live else "dead"


def full_ownership_map(session_id: str, cwd: Optional[str] = None):
    """``(paths this session solely holds, {path: PeerOwnedPath})`` over the
    WHOLE claim ledger -- the in-process answer for a caller classifying paths
    it found some other way.

    Why this is not just `compute_offer(...)["ownership"]`: that readout is
    scoped to paths THIS session holds, because it answers "what is mine and
    why is something of mine missing". A dirty-tree sweep asks a different
    question about a path it did not get from here at all, and needs "a peer
    owns this" told apart from "nobody has claimed this" -- a distinction the
    offer's own buckets cannot make for a path it never considered. Collapsing
    the two reads a peer's in-flight file as unattributed, which is how an
    unattended sweep gets nudged into committing it.

    IN-PROCESS ONLY, and the split exists for that reason: the peer map is
    sized by the ledger rather than by this session (~405 entries, ~72KB as
    JSON on this repo, 2026-08-21), and putting it on `compute_offer`'s return
    would put it on the `session.scope_report` op's wire for every caller,
    most of which never look at it. One index rebuild, zero git spawns, same
    as every other answer in this module.

    Liveness on each entry is EARNED (see :func:`_peer_liveness`), never
    asserted. ``claim_source`` is always ``"session"`` -- see
    :class:`PeerOwnedPath` for why the index has no other value left to
    report. Degraded walk: the peer map comes back EMPTY, matching
    `compute_offer`'s own AC7 fold -- a call that cannot finish its walk must
    not print an owner it cannot stand behind, and an empty map degrades this
    caller to "no claim awareness", never to a wider verdict.
    """
    answer = claim_index.commit_set(session_id, cwd=cwd)
    mine = frozenset(answer.paths)
    if not answer.complete:
        return mine, {}

    # The live set is resolved ONCE, here, and every entry below is answered
    # from it. Measured 2026-08-21, job object, k=20: calling `_peer_liveness`
    # per entry instead cost 23,910ms of process time on this repo's ~405 peer
    # claims -- 48x the 500ms brightline, in ZERO subprocesses, because
    # `live_session_ids` is documented as deliberately un-memoised (a cached
    # live-set reopens the wrong-attribution race) and re-walks every session
    # dir on each call. That is per-item amplification of exactly the shape
    # this whole rebuild exists to remove, reintroduced inside the fix for it.
    # NEGATIVE SPEC: do not move a liveness call back inside this loop, and do
    # not "fix" the cost by memoising `live_session_ids` -- the hoist is free
    # and correct; the cache is neither.
    try:
        live = live_session_ids(cwd)
    except Exception:  # noqa: BLE001 - a readout must never raise
        live = None

    peer_map = {}
    for path, holders in list(answer.peers.items()) + list(answer.contested.items()):
        peer_map[path] = {
            "path": path,
            "owner": holders[0],
            "liveness": _liveness_from_set(holders[0], live),
            "claim_source": "session",
        }
    return mine, peer_map


def compute_offer(session_id: str, cwd: Optional[str] = None) -> SafeCommitOffer:
    """Compute this session's commit pathspec and the withheld-path narration.

    REBUILT 2026-08-21 on ``coordinator_core.session.claim_index.commit_set``
    -- one zero-spawn index rebuild. The mechanism it replaces cost **73
    processes and 5,609ms of kernel+user CPU per call**, in the op every
    workstream in the fleet passes through to close, which the close ceremony
    calls TWICE per pass: multiple simultaneous breaches of CLAUDE.md's
    brightline (500ms end-to-end; one process over 200ms needs a fix; >1s is
    deleted and rebuilt from first principles). 33 of its 35 ``subprocess.run``
    calls were ``git ls-files --full-name`` over a single absolute
    ``touched.txt`` entry each, at a 1,411ms mean, and 25 of 25 sampled
    entries were not under the worktree root at all -- it was spawning a git
    process per entry to be told, slowly, that a file on another drive is not
    in this repo. Verdict record:
    docs/research/spike-verdicts/2026-08-21-ceremony-tail-session-artifact-
    commit-cost.md; disabled at ``e927d9463``, rebuilt here.

    ``safe_paths`` -- what belongs to THIS session to commit right now:
    ``commit_set().paths``, i.e. every path whose current claim verb is ``T``
    for this session and for no other. Agent fan-out is already folded in
    (``rebuild`` resolves ``.agents/<aid>/em-session-id.txt`` back-pointers)
    and so is last-event-wins, so neither is re-derived here.

    NEGATIVE SPEC -- ``safe_paths`` is a LOWER BOUND on what this session
    wrote, never a complete inventory of it, and ``indeterminate`` does NOT
    detect the difference. A path's absence here is not evidence the session
    did not author it: the claim index reads one dialect, records nothing a
    shell redirect or spawned CLI wrote, and goes fully blind on the
    Edit/Write route whenever the mirror is percolated with a reader ahead of
    its writer -- which happened, fleet-wide and undetected, on 2026-08-26
    (``state/audits/2026-08-26-touch-ledger-coverage-and-the-published-
    dialect-split.md``; ``claim_index._TOUCHED_FILENAME``'s own negative spec
    enumerates all three classes). This surfaces to the operator as an
    ordinary scope refusal on their own file, with ``indeterminate`` False.
    Do NOT "fix" that by reading the worktree here -- see ``orphans`` below;
    the fix belongs at the index's coverage, not at this answer's shape.

    ``excluded`` -- every CONTESTED path: one this session holds that a peer
    holds too. Withheld from ``safe_paths`` (a peer's path is not yours) but
    NAMED, because a silent omission reintroduces exactly the doubt this
    answer exists to remove.

    C3 (docs/plans/2026-08-27-safe-commit-offer-excludes-a-live-agent.md)
    tried to resolve ``answer.in_flight_agent_claims`` against
    ``live_session_ids`` and shipped an INERT fix: `liveness.py ::
    _NON_SESSION_DIR_NAMES` excludes the agent plane from that set by
    design, so an agent id is NEVER a member of it and the check folded
    every in-flight agent claim back into ``safe_paths`` unconditionally,
    in production, forever. There is no agent-liveness primitive anywhere
    in this repo (no pid, no start/stop marker under `.agents/<aid>/`) and
    building one is out of scope here (it needs a real dispatch-completion
    hook, which lives in doe-claude's tree). C5 (this chunk) replaces the
    liveness check with a RECENCY check instead: a claim is treated as
    in-flight only while its most recent touch-record timestamp for this
    path/session falls inside ``liveness._ABANDONMENT_WINDOW_SEC`` (the
    same constant ``session_abandoned`` already uses, imported rather than
    re-valued -- docs/research/2026-08-19-abandonment-signal-census.md).
    Outside that window the claim is treated as abandoned and the path
    stays in ``safe_paths``, same as before this fix existed.

    RESIDUAL, named rather than papered over: ``edit_ts`` records the
    FIRST edit of a claim run, not the latest (``_IndexState``'s own
    contract; DR-296 reads it as-is), so an agent that holds one path open
    and keeps re-editing it for longer than the window ages that path out
    and it is offered again -- this narrows the collision window this plan
    exists to close, it does not close it. A genuine fix needs a real
    dispatch-completion signal.

    BIAS, deliberate: every unknown resolves toward INCLUDING the path in
    ``safe_paths`` (today's behaviour, and the direction the plan's
    Anti-scope demands) -- a claim with no parseable timestamp anywhere in
    ``edit_ts`` (a legacy line -- see ``_IndexState``'s own contract) is
    NOT excluded on that basis alone.

    A recency-in-window claim is withheld from ``safe_paths`` exactly like
    a contested peer path -- named in ``excluded`` with an
    operator-actionable reason, and surfaced in ``ownership.peer`` as a
    ``PeerOwnedPath`` with ``claim_source="agent"``. An OUT-OF-WINDOW (or
    unresolvable-timestamp) claim is folded back into ``safe_paths`` -- the
    claim is old enough to treat as this session's own to commit, same as
    ``test_commit_set_leaves_a_dead_agents_orphaned_claim_in_mine`` (moved
    to this module's own test file, ``TestComputeOffer``, since this is the
    surface that actually knows the verdict) exists to guard. The
    per-path edit-timestamp lookup is resolved ONCE per call (one
    ``claim_index.lookup`` over every in-flight-agent-claimed path this
    call saw), never once per path.

    ``orphans`` -- ALWAYS EMPTY, and that is the contract, not a degradation.
    An orphan is a DIRTY path claimed by nobody; dirtiness left this answer by
    PM ruling (2026-08-21) and this function no longer reads the worktree, so
    it has no basis on which to enumerate one. The key is kept rather than
    dropped because consumers destructure this shape, and an absent key reads
    as an error where an empty list reads as "none to report". NEGATIVE SPEC:
    do not repopulate it by adding a dirty read here -- "what belongs to me"
    and "what is dirty" are two different questions, and fusing them is
    precisely the trade that justified the 73-process shape in the first
    place. A caller that wants the dirty view asks for it separately;
    ``_compute_residue`` below is the in-module example, and it takes its own
    read AFTER the commits land rather than borrowing this one.

    ``indeterminate`` -- ``True`` iff the index walk behind this answer was
    incomplete (aborted on its wall-clock cap, an I/O error, or an
    unresolvable base). Both ``safe_paths`` and ``excluded`` may then be SHORT,
    so a caller must say so rather than presenting a partial answer as the
    answer; ``commit_session_offer_async``'s hardening (a) commits nothing at
    all on it.

    ``ownership`` -- the same four buckets as before (mine / named peer /
    unattributed / degraded), now derived from the claim index. ``peer``
    carries an EARNED liveness verdict (see :func:`_peer_liveness`); on a
    degraded call it is emptied outright and every contested path folds into
    ``unattributed``, because a call that cannot stand behind an owner name
    must not print one (AC7).

    REMOVED IN THE REBUILD -- ``extra_candidates``. It existed because
    ``compute_scope`` could only answer about paths it had already adopted as
    candidates, so naming a holder for a caller-supplied path required
    feeding that pathspec back in, producing an offer that adopted whatever it
    was handed and was catastrophic to read as a verdict. The gate that needed
    it now asks ``claim_index.classify_paths`` directly (see
    ``coordinator_core.ops.session.scope_report``). NEGATIVE SPEC: never
    reintroduce a caller-supplied candidate set on this function -- it is the
    shape that let any caller own any path by naming it.

    Read-only: makes no git or ``touched.txt`` mutation, and spawns no
    subprocess. Raises ``ValueError`` if ``session_id`` is empty.
    """
    if not session_id:
        raise ValueError("session_id is required")

    answer = claim_index.commit_set(session_id, cwd=cwd)
    degraded = not answer.complete

    safe_paths: List[str] = list(answer.paths)
    excluded: List[ExcludedPath] = []
    peer: List[PeerOwnedPath] = []
    unattributed: List[str] = []
    for path in sorted(answer.contested):
        holders = answer.contested[path]
        excluded.append(
            {"path": path, "reason": "owned by session %s" % (holders[0],)}
        )
        if degraded:
            unattributed.append(path)
            continue
        peer.append(
            {
                "path": path,
                "owner": holders[0],
                "liveness": _peer_liveness(holders[0], cwd),
                # Every claim the index carries is a session claim by the time
                # it is read: `rebuild` has already resolved an agent's claim
                # back to the EM session that owns it, so there is no
                # `"agent"`/`"agent-race"` distinction left to report here and
                # inventing one would be a wire value nothing measured.
                "claim_source": "session",
            }
        )

    # C5 (docs/plans/2026-08-27-safe-commit-offer-excludes-a-live-agent.md,
    # this chunk): C3's `live_session_ids`-based verdict is INERT in
    # production -- see this function's own docstring for the measured
    # reason -- so the verdict is resolved on RECENCY instead. One
    # `claim_index.lookup` over every in-flight-agent-claimed path this call
    # saw resolves every needed timestamp in one pass (never once per path).
    if answer.in_flight_agent_claims and not degraded:
        try:
            agent_edit_ts = claim_index.lookup(
                list(answer.in_flight_agent_claims), cwd=cwd
            ).edit_ts
        except Exception:  # noqa: BLE001 - a readout must never raise
            agent_edit_ts = {}
    else:
        agent_edit_ts = {}

    now = datetime.now(timezone.utc)
    for path in sorted(answer.in_flight_agent_claims):
        if degraded:
            unattributed.append(path)
            continue
        owner_agent = answer.in_flight_agent_claims[path][0]
        # `edit_ts` is keyed by claimant_sid, already resolved to this
        # OWNING SESSION's id (not the raw agent id) -- see `rebuild`'s own
        # `claimant_sid`/`agent_id` split.
        claim_ts = agent_edit_ts.get(path, {}).get(session_id)
        in_window = (
            claim_ts is not None
            and (now - claim_ts).total_seconds() < _ABANDONMENT_WINDOW_SEC
        )
        if in_window:
            excluded.append(
                {
                    "path": path,
                    "reason": "in-flight claim touched by dispatched agent %s "
                    "%ds ago" % (owner_agent, int((now - claim_ts).total_seconds())),
                }
            )
            peer.append(
                {
                    "path": path,
                    "owner": owner_agent,
                    "liveness": "live",
                    "claim_source": "agent",
                }
            )
        else:
            # No parseable timestamp, or the touch aged out of the
            # abandonment window: fold back into `safe_paths` -- see the
            # docstring's BIAS paragraph for why an unresolvable timestamp
            # is deliberately NOT treated as still in-flight.
            safe_paths.append(path)

    safe_paths.sort()

    return {
        "session_id": session_id,
        "safe_paths": safe_paths,
        "excluded": excluded,
        "orphans": [],
        "indeterminate": degraded,
        "ownership": {
            "mine": list(safe_paths),
            "peer": peer,
            "unattributed": unattributed,
            "degraded": degraded,
        },
    }


# ---------------------------------------------------------------------------
# Grouping (mechanical fallback — an EM/ceremony with real judgment should
# prefer passing explicit `groups` instead, see `commit_session_offer`)
# ---------------------------------------------------------------------------


def _default_groups(
    safe_paths: List[str], session_id: str
) -> List[CommitGroup]:
    """Mechanical grouping used ONLY when the caller supplies no explicit
    `groups` — i.e. no EM/human judgment authored a per-group description for
    THIS call. Groups by the
    path's top-two segments (a coarse subsystem boundary —
    `coordinator/skills`, `state/handoffs`, a bare top-level dir when there's
    only one segment) rather than either extreme the PM named as worse than
    no automation: one mega-commit spanning unrelated subsystems, or one
    commit per file. An EM-run ceremony with real judgment to spend should
    still prefer drafting real per-group descriptions and passing explicit
    `groups` instead of relying on this fallback — grouping "like an
    engineer would" needs judgment this mechanical layer doesn't have; this
    default only bounds the mechanical case, it doesn't aspire to replace
    authored judgment.

    THE BODY ASSERTS HOW, NEVER WHY. It says the paths were bucketed
    mechanically and nothing about the reason the commit happened. That is the
    surviving half of the ``invoker`` contract deleted 2026-08-27 (see the
    module docstring): the parameter framed a commit as a stop-event rescue or
    as a deliberate ceremony, and neither claim is one this function can
    substantiate now that any caller can reach it through the warm door.

    The incident that produced ``invoker`` still governs the wording — see the
    module's dispatching memo (example-cockpit-repo-em, 2026-08-17): a deliberate,
    curated ceremony commit landed in history confidently mislabelled an
    unattended accident, because this function asserted the stop-event story
    unconditionally. Deleting the parameter does not reinstate that bug, it
    removes the vocabulary that made it expressible. Do not reintroduce a
    "rescued at session stop" subject without a producer that can prove it — the
    session is still running and chose to commit, and this function cannot
    tell that from a stop-event rescue.

    Subject/body split (PM framing, 2026-07-31: "if I have to commit, it's a
    safety [net] because someone forgot to commit" — the origin framing, true
    of the stop-event trigger that no longer exists; the narrower shape here is
    this function's later correction, not a reversal of it). The subject stays
    SHORT and BOUNDED — file count + subsystem key + short session id, never an
    enumerated file list (unbounded at N paths, unreadable in a `git log
    --oneline`). The full path list, and which subsystem key grouped them,
    live in the BODY (`prose`) in every shape, where length costs nothing and
    a future archaeologist reading `git log` (not `--oneline`) gets the
    complete picture plus the honest framing rather than a commit dressed up
    as something it isn't.
    """
    if not safe_paths:
        return []
    buckets: "OrderedDict[str, List[str]]" = OrderedDict()
    for p in safe_paths:
        segments = p.split("/")
        # The DIRECTORY prefix only (drop the filename) -- up to two
        # directory levels deep, e.g. "coordinator/skills" for
        # "coordinator/skills/handoff/SKILL.md". A bare top-level file (no
        # directory) never becomes the bucket key itself -- that would put
        # the exact filename back into the subject line via `key`, the same
        # unbounded-subject shape this split fixes. Joining a 2-segment path
        # like "sub/file.py" on segments[:2] would ALSO reproduce the
        # filename (there IS no directory-only prefix shorter than the
        # whole path) -- segments[:-1] (directories only) is the fix.
        dir_segments = segments[:-1][:2]
        key = "/".join(dir_segments) if dir_segments else "(repo root)"
        buckets.setdefault(key, []).append(p)

    groups: List[CommitGroup] = []
    for key, paths in buckets.items():
        subject = "auto-commit: %d file(s) (session %s, %s)" % (
            len(paths), session_id[:6], key
        )
        prose = (
            "Mechanically grouped — this commit asserts nothing about why it "
            "happened, only how the paths below were bucketed (subsystem-"
            "grouped, no per-group description authored this call). Session %s "
            "reached this fallback rather than supplying explicit groups.\n\n"
            "Grouped under %r (this session's own touch-list claim, subsystem-"
            "bucketed):\n%s"
        ) % (session_id, key, "\n".join("  - %s" % p for p in paths))
        groups.append({"paths": paths, "message": subject, "prose": prose})
    return groups


# ---------------------------------------------------------------------------
# Post-commit residue report (C3, 2026-08-05 engine-ops-declare-what-they-
# write plan) — REPORT-ONLY, read-only. Never stages, never blocks, never
# widens the commit pathspec (AC4). Runs AFTER the commit groups already
# landed, purely to name what git status still shows dirty.
# ---------------------------------------------------------------------------


def _current_dirty_paths(cwd: Optional[str]) -> List[str]:
    """Repo-relative paths `git status --porcelain` reports dirty RIGHT NOW
    -- a fresh, POST-commit re-read. It is now the ONLY worktree read left
    in this module: the pre-commit dirty enumeration the 2026-08-21 rebuild
    removed is not to be reintroduced by reusing this one earlier (see
    `compute_offer`'s `orphans` negative spec). ``core.quotepath=false``
    matches every other git invocation in this module, so the two path
    dialects agree. A rename line (`R  old -> new`) reports only the NEW
    path — the old path is gone from the tree and cannot be residue. Fails
    closed (empty list) on any git error; a residue
    report that under-counts on a git hiccup is safe (REPORT-ONLY, never
    gates), one that raised would not be.

    ``--untracked-files=all`` (not the porcelain default of ``normal``) is
    REQUIRED: an entirely-untracked directory otherwise collapses to one
    ``?? state/`` line naming the directory, not its files — every file
    under it would then vanish from residue entirely rather than being
    named/grouped, exactly the invisibility AC3 exists to fix.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            **no_console_creationflags(),
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    # Review: code-reviewer (Finding 3) — reuse the ONE sanctioned porcelain
    # parser (`dirty_tree_gate.parse_porcelain_paths`) rather than a second
    # hand-rolled copy; that function's own docstring forbids a second copy.
    # `--untracked-files=all` is set on the `git status` call above, not by
    # the parser — the parser only splits lines already produced by that
    # call, so the all-files behavior this function's docstring promises is
    # unaffected by delegating the line-splitting itself.
    return [path for _xy, path in parse_porcelain_paths(result.stdout) if path.strip()]


def _residue_class(path: str) -> str:
    """Grouping key for the residue report: `state/<subdir>` for anything
    under `state/` (the class the plan's Problem section measures — e.g.
    `state/memo-outbox`, `state/ceremony`), or the bare top-level segment for
    anything else (e.g. `coordinator_core`). Never the full path -- grouping
    is the whole point of bounding this report (see `_compute_residue`)."""
    segments = path.split("/")
    if segments and segments[0] == "state" and len(segments) > 1:
        return "state/%s" % segments[1]
    return segments[0] if segments else path


def _excluded_with_peer_owned_dirty(
    excluded: List[ExcludedPath],
    session_id: str,
    worktree_root: Optional[str],
) -> List[ExcludedPath]:
    """``compute_offer``'s ``excluded`` plus the peer-owned paths that are
    actually DIRTY right now — the operator-facing half of the peer-only
    attribution fix (2026-08-27).

    ``compute_offer`` builds ``excluded`` from ``CommitSet.contested`` alone:
    paths THIS session claims that a peer claims too. A path only the PEER
    claims is not "excluded" from an offer it was never in, so by that
    function's own contract it does not belong there — and ``compute_offer``
    must stay a pure, worktree-free projection of the claim index, so it
    cannot ask what is dirty.

    But the REPORT has a different reader with a different question. An
    operator looking at "what did this ceremony not commit, and why" needs
    ``peer.py`` named and attributed, or its absence reads as an unexplained
    gap — and the next move after an unexplained gap is a bulk sweep over a
    peer's uncommitted work.

    Scoped to DIRTY paths deliberately: ``CommitSet.peers`` is sized by the
    claim ledger (~405 entries on this repo, see its docstring), not by the
    tree. Emitting all of it would bury the few paths an operator can act on.
    Returns ``excluded`` unchanged when the worktree is unavailable.
    """
    if not worktree_root:
        return excluded
    peers = claim_index.commit_set(session_id, cwd=worktree_root).peers
    if not peers:
        return excluded
    already = {e["path"] for e in excluded}
    extra: List[ExcludedPath] = [
        {"path": path, "reason": "owned by session %s" % (peers[path][0],)}
        for path in sorted(set(_current_dirty_paths(worktree_root)) & set(peers))
        if path not in already
    ]
    return list(excluded) + extra


def _compute_residue(
    session_id: str,
    group_results: List[GroupResult],
    worktree_root: Optional[str],
) -> "Tuple[OrderedDict[str, List[str]], Reconciliation]":
    """What the ceremony left dirty, grouped by `_residue_class` -- the
    diagnostic AC3 exists to add -- PLUS this call's `Reconciliation`
    (2026-08-29), returned as a pair rather than computed by a second
    function so that BOTH products come off the single sanctioned
    `_current_dirty_paths` read. See the comment at that read for why the
    pair is deliberate; `Reconciliation`'s own docstring carries the
    contract for the second element. REPORT-ONLY (AC4): read-only throughout,
    computed strictly AFTER `group_results` already landed, and never fed
    back into any pathspec -- the caller (`commit_session_offer_async`) only
    attaches this dict to the returned report, it never reads it back into
    `safe_set`/`resolved_groups`.

    Attribution before residue (AC5): a dirty path this call did NOT commit
    is first checked against a FRESH `compute_offer(session_id, ...)`
    re-read -- taken here, immediately before residue is computed, NOT the
    `offer["excluded"]` snapshot `commit_session_offer_async` took BEFORE the
    commit groups ran. Review: code-reviewer (Finding 2) -- that earlier
    snapshot predates every `git commit` subprocess call this session just
    ran; a peer session that dirtied a new file, or wrote its own claim for
    an already-dirty file, during that window was invisible to it and would
    render here as this session's own residue -- exactly the harm AC5 exists
    to prevent. Re-deriving ownership fresh, right before the dirty-path
    read below, closes that window: both reads are now taken back-to-back,
    post-commit, so a peer's claim recorded any time up to "now" is
    attributed correctly. A path with a `"owned by session <id>"` reason in
    this fresh read's `excluded` is a live peer's in-flight work, not this
    session's leftover, and is excluded from `residue` entirely rather than
    rendered as something this ceremony "left behind". Reporting a peer's
    file as residue is exactly the harm this module's own scope-narrowing
    exists to prevent (see the plan's C3 body) -- it is how an operator gets
    nudged into a bulk sweep that clobbers a peer's uncommitted work.

    Everything else still dirty after the commit groups landed -- a path
    that never entered `safe_paths` at all (an orphan, "untouched by this
    session"), or one that WAS in `safe_paths` but whose group never
    actually committed (`commit_failed`, or a group the caller's own
    `groups` override dropped) -- is genuine residue.

    Returns an empty (not merely falsy) `OrderedDict` when `worktree_root`
    is unavailable or nothing is left dirty; never raises.
    """
    empty: "OrderedDict[str, List[str]]" = OrderedDict()
    unchecked: Reconciliation = {
        "reconciled": False,
        "claimed_absent": [],
        "unclaimed": [],
    }
    if not worktree_root:
        return empty, unchecked

    committed_paths: set = set()
    for g in group_results:
        if g.get("committed"):
            committed_paths.update(g["paths"])

    fresh_offer = compute_offer(session_id, worktree_root)
    owned_paths = {
        e["path"]
        for e in fresh_offer.get("excluded") or []
        if str(e.get("reason", "")).startswith("owned by session")
    }

    # PEER-ONLY CLAIMS ARE ATTRIBUTED HERE, NOT VIA `excluded` (fixed
    # 2026-08-27). `compute_offer`'s `excluded` is built from
    # `CommitSet.contested` alone -- paths THIS session claims that a peer
    # claims too. A path only the PEER claims never reaches it, so the
    # `"owned by session"` filter above missed exactly the case AC5 names and
    # a live peer's in-flight file rendered as this ceremony's residue: the
    # harm this function's own docstring calls out, and the shape that nudges
    # an operator into a bulk sweep over a peer's uncommitted work.
    #
    # Attributed at THIS seam rather than by widening `excluded`, deliberately:
    # `CommitSet.peers` is sized by the claim ledger (~405 entries on this
    # repo, see its own docstring), not by the dirty tree, so folding it into
    # `excluded` would bury the handful of genuinely-withheld paths an
    # operator needs to see under hundreds of irrelevant ones. Here the
    # question is only ever asked about paths that are actually dirty.
    peer_claimed = claim_index.commit_set(session_id, cwd=worktree_root).peers

    # The ONE dirty read this module is allowed (see `_current_dirty_paths`)
    # is taken here and reused for BOTH products below -- residue buckets and
    # the reconciliation answer. Do not add a second read for the second
    # product: `_compute_residue` returning a pair is deliberately uglier
    # than two tidy functions, because two tidy functions would each want
    # their own `git status` and the second spawn is the thing that is
    # actually forbidden.
    dirty = _current_dirty_paths(worktree_root)

    buckets: "OrderedDict[str, List[str]]" = OrderedDict()
    for path in dirty:
        if path in committed_paths or path in owned_paths:
            continue
        if path in peer_claimed:
            continue
        buckets.setdefault(_residue_class(path), []).append(path)

    # UNCLAIMED = dirty, and claimed by NO session -- this session included.
    # `fresh_offer["safe_paths"]` is subtracted because a residue path this
    # session DOES claim (its commit group failed, or the caller's own
    # `groups` override dropped it) is this session's own uncommitted work,
    # not an adoption candidate; folding the two together is what would make
    # the enumeration unsafe to hand `--include-orphans`.
    mine_now = set(fresh_offer.get("safe_paths") or [])
    unclaimed = sorted(
        path
        for path in dirty
        if path not in committed_paths
        and path not in owned_paths
        and path not in peer_claimed
        and path not in mine_now
    )

    # CLAIMED-BUT-ABSENT -- stat-only, one `os.path.exists` per claimed path,
    # never a walk and never a git call. Bounded by the claim count (small by
    # construction), not by the tree.
    claimed_absent = sorted(
        path for path in mine_now if not (Path(worktree_root) / path).exists()
    )

    reconciliation: Reconciliation = {
        "reconciled": True,
        "claimed_absent": claimed_absent,
        "unclaimed": unclaimed,
    }
    return buckets, reconciliation


# ---------------------------------------------------------------------------
# Auto-commit + auto-push (mutating — the only part of this module that is)
# ---------------------------------------------------------------------------


async def _commit_group(
    worktree_root: str, group: CommitGroup, session_id: Optional[str] = None
) -> GroupResult:
    """Repointed (C4, docs/plans/2026-08-29-the-push-subsystem-leaves-and-
    then-the-pipeline-can-go.md) off the killed `run_commit_pipeline` onto the
    sanctioned zero-spawn commit shape, `coordinator_core.git.commit.
    commit_paths` -- `ceremony.commit_v2`'s own in-process call, mirrored here
    exactly as `execute_plan_assemble.close_out_and_stamp` already does (C3 of
    the same plan), rather than round-tripped through the op registry: this
    module already runs in-process.

    Pathspec scope mirrors the prior pipeline call site exactly: only
    `group["paths"]` is ever staged, never a caller-widened or auto-detected
    scope (constraint 3 of the original DR-344 rewiring's brief, unchanged by
    this repoint).

    `prefer_deliberate_stage=True` (DR-379) -- the opt-in `close_out_and_stamp`
    could not take (AC4's missing half, this chunk): this module commits a
    SESSION's own claimed dirty paths, so a path this session has already
    deliberately staged (a partial hunk, a curated subset) should land its
    staged bytes rather than being passed over for its worktree content. That
    is exactly the substitution `prefer_deliberate_stage` performs.

    No push leg at all any more: this module's auto-commit path never owned a
    synchronous push (`push_state` was already always reported `None` on
    every prior real-commit path here, same as `close_out_and_stamp`'s own
    reasoning) -- publication is left to whichever cadence checkpoint runs
    next.

    Return-contract mapping (constraint 1, unchanged): every `GroupResult`
    key is still populated, none renamed/dropped/added. `committed` is
    `outcome.sha is not None` on the success path -- a refused commit never
    reports `committed: True` (constraint 2); `push_state` is always
    `PUSH_STATUS_NOT_ATTEMPTED` (this call never pushes, mirroring the prior
    `push_mode=PUSH_MODE_NEVER` behaviour).
    """
    message = group["message"]
    prose = group.get("prose", "")
    if prose:
        message = f"{message}\n\n{prose}"
    # `group["paths"]` may legitimately name a claimed-but-deleted path (a
    # deletion this session made is a real thing to commit -- see this
    # module's own `Reconciliation.claimed_absent` docstring) -- `commit_
    # paths` needs those split into its own `deleted_paths` kwarg rather than
    # `paths`, since a present-path read (`root / p).read_bytes()`) is what
    # the old `run_commit_pipeline`'s `stage_paths` auto-classification used
    # to do internally.
    present_paths = [p for p in group["paths"] if (Path(worktree_root) / p).exists()]
    deleted_paths = [p for p in group["paths"] if p not in present_paths]
    try:
        outcome = commit_paths(
            worktree_root,
            present_paths,
            message,
            deleted_paths=deleted_paths,
            prefer_deliberate_stage=True,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree_root),
        )
    except (CommitRefused, FilterUnsupported) as exc:
        return {
            "paths": group["paths"],
            "message": group["message"],
            "committed": False,
            "sha": None,
            "push_state": PUSH_STATUS_NOT_ATTEMPTED,
            "error": str(exc),
            "commit_failed": True,
            "reason": None,
        }
    return {
        "paths": group["paths"],
        "message": group["message"],
        "committed": outcome.sha is not None,
        "sha": outcome.sha,
        "push_state": PUSH_STATUS_NOT_ATTEMPTED,
        "error": None,
        "commit_failed": False,
        "reason": None,
    }


async def commit_session_offer_async(
    session_id: str,
    cwd: Optional[str] = None,
    groups: Optional[List[CommitGroup]] = None,
) -> CommitOfferReport:
    """Compute this session's safe pathspec and commit+push it — NO
    confirmation step, by explicit PM ruling.

    RENAMED 2026-08-27, from ``auto_commit_session``. The old name's "auto"
    was defensible only under one reading — unconfirmed, which is still true
    (the 2026-07-31 PIVOT removed the group-by-group confirmation step) —
    and false under the reading every passing user actually takes:
    unattended. A name that needs a paragraph explaining which of two senses
    it means is not saying what it does, which is precisely what this
    module's Axis-2 reconciliation was for. The argument for keeping it was
    migration cost (73 references across 9 files); that was cost talking,
    not correctness, and the migration was mechanical.

    "Offer" is the module's own noun and the surviving callers are all
    EM-initiated ceremonies, so this verb reads as what it is: commit the
    offer this session computed for itself. There is deliberately NO
    backward-compatible alias — the old name has no caller outside this repo
    (DoE-claude's retired `sessionend-auto-commit.py` mentions it in prose
    only, never imports it), and an alias would preserve the ambiguity this
    rename exists to remove.

    ``groups`` lets a caller with
    real judgment (an EM-run ceremony) supply its own meaningful
    grouping/messages; any path in a supplied group that is NOT in this
    session's own computed ``safe_paths`` is silently dropped from its group
    — the auto-commit boundary is COMPUTED, never caller-widened. ``None``
    (the default) uses ``_default_groups``.

    C4 hardening (a) — read ``offer["indeterminate"]``/
    ``offer["ownership"]["degraded"]`` BEFORE building any group or calling
    the commit pipeline at all: either being ``True`` means this
    call's own claim reads were degraded call-wide, so attribution is
    untrustworthy for EVERY path this call would otherwise claim as
    "mine" — not merely that the unattributed paths are free to leave out.
    Returns a ``"skipped_indeterminate"``/``"skipped_degraded"`` ``outcome``
    with an empty ``groups``/``failed_groups``/``residue`` and NO
    ``_commit_group`` call made at all. Non-blocking, same as every other
    path here (DR-227): this reports why, it never raises.

    C4 hardening (c) — fail closed on a claimed path that is ALSO present in
    this same call's own ``ownership["peer"]`` bucket. The two buckets are
    mutually exclusive by construction today (see ``OwnershipReadout``'s own
    docstring) — this is belt-and-braces against future drift in that
    invariant, per the brief's own note that this is "structurally
    near-impossible for this pathspec specifically" — withheld from every
    group BEFORE any commit call, never partially
    staged then rolled back. Named in the returned ``outcome`` as
    ``conflicted_paths``.
    """
    offer = compute_offer(session_id, cwd)
    safe_set = set(offer["safe_paths"])

    # (a) — degraded/indeterminate claim read: commit NOTHING this call.
    if offer["indeterminate"] or offer["ownership"]["degraded"]:
        status: Literal["skipped_indeterminate", "skipped_degraded"] = (
            "skipped_indeterminate" if offer["indeterminate"] else "skipped_degraded"
        )
        detail = (
            "Commit withheld call-wide for session %s: this call's own claim "
            "reads were %s, so attribution for %d claimed path(s) cannot be "
            "trusted this call and nothing was staged (see OwnershipReadout's "
            "own docstring)."
            % (
                session_id,
                "indeterminate" if status == "skipped_indeterminate" else "degraded",
                len(safe_set),
            )
        )
        return {
            "session_id": session_id,
            "groups": [],
            "excluded": offer["excluded"],
            "failed_groups": [],
            "dropped_groups": [],
            "residue": OrderedDict(),
            # This call short-circuited before any commit and before the
            # residue pass, so no ledger-versus-tree check ran. `reconciled:
            # False` says exactly that, and is the shape a caller must not
            # read as "the ledger agrees" -- the whole reason it is a
            # did-the-check-run flag.
            "reconciliation": {
                "reconciled": False,
                "claimed_absent": [],
                "unclaimed": [],
            },
            "outcome": {
                "status": status,
                "detail": detail,
                "committed_paths": [],
                "conflicted_paths": [],
            },
        }

    # (c) — defensive fail-closed check: a path this call claims as "mine"
    # must never also appear in this same call's own `ownership["peer"]`
    # bucket. Withheld from every group before any commit, not filtered
    # post-hoc from a landed commit.
    peer_paths = {p["path"] for p in offer["ownership"]["peer"]}
    conflicted_paths = sorted(safe_set & peer_paths)
    conflict_set = set(conflicted_paths)

    dropped_groups: List[DroppedGroup] = []
    if groups is None:
        resolved_groups = _default_groups(offer["safe_paths"], session_id)
    else:
        resolved_groups = []
        for g in groups:
            named_paths = g["paths"]
            kept = [p for p in named_paths if p in safe_set]
            if len(kept) < len(named_paths):
                # Handoff item 1 (2026-08-03, touched-path-bookkeeping) --
                # record the drop, total or partial, BEFORE the `if kept`
                # gate below decides whether the group survives at all. A
                # group that loses every path takes the `if kept` branch's
                # else (never reached, `resolved_groups.append` skipped) and
                # would otherwise leave no trace anywhere in this report.
                dropped_groups.append(
                    {
                        "message": g["message"],
                        "named": len(named_paths),
                        "matched": len(kept),
                    }
                )
            if kept:
                # Review: code-reviewer (Finding 4) — carry the caller-
                # supplied `prose` body through; it was previously dropped
                # here, so only the mechanical `_default_groups` fallback
                # ever produced a commit body.
                resolved_groups.append(
                    {"paths": kept, "message": g["message"], "prose": g.get("prose", "")}
                )

    if conflict_set:
        # (c) — strip conflicted paths out of every group BEFORE any of
        # them reach `_commit_group`/`run_commit_pipeline`. A group
        # that loses every one of its paths this way is dropped entirely,
        # same as an empty-`kept` caller-supplied group above.
        filtered_groups: List[CommitGroup] = []
        for g in resolved_groups:
            kept_paths = [p for p in g["paths"] if p not in conflict_set]
            if kept_paths:
                filtered_group: CommitGroup = dict(g)  # type: ignore[assignment]
                filtered_group["paths"] = kept_paths
                filtered_groups.append(filtered_group)
        resolved_groups = filtered_groups

    worktree_root = core.git_root(cwd) or cwd or "."
    group_results = [
        await _commit_group(worktree_root, g, session_id) for g in resolved_groups
    ]
    failed_groups = [g for g in group_results if g.get("commit_failed")]
    residue, reconciliation = _compute_residue(
        session_id, group_results, worktree_root
    )

    committed_paths: List[str] = []
    for g in group_results:
        if g.get("committed"):
            committed_paths.extend(g["paths"])

    if conflicted_paths:
        outcome: CommitOutcome = {
            "status": "dirty_conflict_skipped",
            "detail": (
                "%d claimed path(s) were also seen as a named peer claim this "
                "call and were withheld from every commit group (fail-closed); "
                "%d other path(s) still committed normally."
                % (len(conflicted_paths), len(committed_paths))
            ),
            "committed_paths": committed_paths,
            "conflicted_paths": conflicted_paths,
        }
    elif committed_paths:
        outcome = {
            "status": "committed",
            "detail": "%d path(s) committed across %d group(s)."
            % (len(committed_paths), sum(1 for g in group_results if g.get("committed"))),
            "committed_paths": committed_paths,
            "conflicted_paths": [],
        }
    else:
        outcome = {
            "status": "empty",
            "detail": "no claimed path(s) to commit this call.",
            "committed_paths": [],
            "conflicted_paths": [],
        }

    return {
        "session_id": session_id,
        "groups": group_results,
        "excluded": _excluded_with_peer_owned_dirty(
            offer["excluded"], session_id, worktree_root
        ),
        "failed_groups": failed_groups,
        "dropped_groups": dropped_groups,
        "residue": residue,
        "reconciliation": reconciliation,
        "outcome": outcome,
    }


def commit_session_offer(
    session_id: str,
    cwd: Optional[str] = None,
    groups: Optional[List[CommitGroup]] = None,
) -> CommitOfferReport:
    """Sync wrapper — a single ``asyncio.run()`` drives the whole call
    (matches the single-event-loop convention other in-process op composers
    use, e.g. ``coordinator_core.ops.promote_shipped_in_flight_stubs``)."""
    return asyncio.run(commit_session_offer_async(session_id, cwd, groups))


# ---------------------------------------------------------------------------
# Diagnostics sinks
# ---------------------------------------------------------------------------


def _log_failed_groups_diagnostic(
    worktree_root: str, session_id: str, failed_groups: List[GroupResult]
) -> None:
    """Best-effort write to the SAME
    ``coordinator-sessions/logs/sessionend-auto-commit-diagnostics.log`` file
    the SessionEnd hook (`DoE-claude/coordinator/hooks/scripts/
    sessionend-auto-commit.py._log_diagnostic`) appends to, naming WHICH
    groups failed and why -- not merely that something did. Never raises; a
    diagnostics-write failure must not break the op's own return path.

    Review: code-reviewer (Finding 1) — `failed_groups` was computed and
    tested but never surfaced anywhere a human or the hook actually reads.
    The hook only inspects the subprocess exit code (never stdout), so this
    module's own `main()` must both (a) return a distinct exit code and (b)
    write the failure detail in-process, since nothing downstream parses the
    report body.
    """
    try:
        from coordinator_core.lifecycle import git_common_dir

        common_dir = git_common_dir(Path(worktree_root))
        log_dir = common_dir / "coordinator-sessions" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            "[%s] safe-commit-offer: %d group(s) genuinely failed to commit "
            "for session %s:" % (stamp, len(failed_groups), session_id)
        ]
        for g in failed_groups:
            lines.append(
                "  - %s (%d path(s)): %s"
                % (g["message"], len(g["paths"]), g.get("error") or "commit failed")
            )
        with (log_dir / "sessionend-auto-commit-diagnostics.log").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        return


#: Bound on how many withheld paths get named inline in the diagnostics-log
#: entry. The log line is a breadcrumb pointing at full detail, not the full
#: detail (DR-227 -- advisory only, see `_log_excluded_diagnostic`); the op's
#: own bounded `dry_run=true` return is where the whole list lives.
#:
#: HISTORY, because the bound's justification changed even though its value
#: did not. Pre-2026-08-21, `excluded` held roughly every unclaimed dirty path
#: in the tree on every close (dozens, observed live), and a code-reviewer
#: finding required the preview to BIAS toward the orphan-derived entries so
#: a newly-declined orphan could not be pushed into the "and N more" tail by
#: peer-owned paths that were already visible elsewhere. `compute_offer` no
#: longer reports orphans at all (see its own contract), so `excluded` is
#: contested paths only, that bias had exactly one class left to sort, and it
#: was removed rather than left as a no-op sort a reader would take for a
#: live invariant. The BOUND stays: a session sharing many paths with a peer
#: is an ordinary shape, and an unbounded log line is still unbounded.
_EXCLUDED_LOG_PREVIEW_COUNT = 10


def _log_excluded_diagnostic(
    worktree_root: str, session_id: str, excluded: List[ExcludedPath]
) -> None:
    """Best-effort write to the SAME
    ``coordinator-sessions/logs/sessionend-auto-commit-diagnostics.log`` file
    `_log_failed_groups_diagnostic` appends to (mirrors its directory
    resolution, exception-swallowing, and append shape exactly) -- naming the
    withheld paths in the ONE sink an unattended caller could actually
    surface. That framing is now historical on both counts: the SessionEnd
    hook it was written for is retired, and since 2026-08-27 there is no
    subprocess or exit code to inspect either — every caller reaches this
    module as the `session.safe_commit_offer` op and gets the whole report
    back on the wire. The sink stays because the diagnostics log outlives any
    one call, which the return value does not. Never raises; a
    diagnostics-write failure must not break the op's own return path.

    WHAT IS BEING NAMED, post-2026-08-21: a path this session holds that a
    PEER holds too. It is withheld because a shared path is not solely this
    session's to commit, and it is named because a silent omission is exactly
    the doubt this whole answer exists to remove. That is a different fact
    from the one this sink used to carry ("declined adoption" -- a dirty file
    nobody had claimed), and the wording follows the fact rather than the
    other way round.

    DR-227 -- advisory ONLY. This never changes `main`'s exit code and must
    never be wired to do so: withholding a contested path is the correct,
    expected outcome (per this module's wolf-crying constraint, see the module
    docstring), not a failure. Do not promote this to a gate.
    """
    if not excluded:
        return
    try:
        from coordinator_core.lifecycle import git_common_dir

        common_dir = git_common_dir(Path(worktree_root))
        log_dir = common_dir / "coordinator-sessions" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        preview = excluded[:_EXCLUDED_LOG_PREVIEW_COUNT]
        lines = [
            "[%s] safe-commit-offer: %d file(s) withheld for session %s -- "
            "also claimed by another session (not committed -- advisory only, "
            "DR-227, exit code unaffected):" % (stamp, len(excluded), session_id)
        ]
        lines.extend("  - %s — %s" % (e["path"], e["reason"]) for e in preview)
        remaining = len(excluded) - len(preview)
        if remaining > 0:
            lines.append(
                "  ... and %d more (see the session.safe_commit_offer op with dry_run "
                "for the full list)" % remaining
            )
        with (log_dir / "sessionend-auto-commit-diagnostics.log").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        return


def _log_dropped_groups_diagnostic(
    worktree_root: str, session_id: str, dropped_groups: List[DroppedGroup]
) -> None:
    """Best-effort write to the SAME
    ``coordinator-sessions/logs/sessionend-auto-commit-diagnostics.log`` file
    `_log_failed_groups_diagnostic`/`_log_excluded_diagnostic` append to --
    mirrors both exactly in directory resolution, timestamp format,
    exception-swallowing, and append shape. Handoff item 1 (2026-08-03,
    touched-path-bookkeeping): a caller-supplied group that loses some or all
    of its named paths to the `safe_set` filter previously appeared in
    neither `groups`, `failed_groups`, nor `excluded` -- this is the ONE sink
    that outlives the call, the return value having no reader once the caller
    has moved on (the unattended SessionEnd hook this was written for is
    retired, and since 2026-08-27 there is no subprocess exit code to inspect
    either -- see `_log_failed_groups_diagnostic`'s own docstring). Never
    raises; a diagnostics-write failure must not break the op's own return
    path.

    DR-227 -- advisory ONLY, same as `_log_excluded_diagnostic`. This never
    promotes itself into the op's `error` field and must never be wired to do
    so: a dropped
    group means the caller named a path outside its own session's computed
    scope, not a failure of this module's own computation -- see
    `CommitOfferReport.dropped_groups`'s own docstring. Do not promote this to
    a gate.
    """
    if not dropped_groups:
        return
    try:
        from coordinator_core.lifecycle import git_common_dir

        common_dir = git_common_dir(Path(worktree_root))
        log_dir = common_dir / "coordinator-sessions" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            "[%s] safe-commit-offer: %d caller-supplied group(s) partially or "
            "fully dropped for session %s (named path(s) outside the "
            "computed scope -- advisory only, exit code unaffected):"
            % (stamp, len(dropped_groups), session_id)
        ]
        preview = dropped_groups[:_DROPPED_GROUPS_PREVIEW_COUNT]
        for dg in preview:
            lines.append(
                "  - %s — named %d paths, %d matched"
                % (dg["message"], dg["named"], dg["matched"])
            )
        remaining = len(dropped_groups) - len(preview)
        if remaining > 0:
            lines.append("  ... and %d more group(s)" % remaining)
        with (log_dir / "sessionend-auto-commit-diagnostics.log").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        return


#: How many of a group's own paths get named inline before the rest collapse
#: into a "... and N more" tail. Bounds the report the same way
#: `_EXCLUDED_LOG_PREVIEW_COUNT` bounds the diagnostics-log entry; the full
#: list is always in the op's own structured return, and for a LANDED group
#: also in the commit
#: body `_default_groups` composes.
_REPORT_PATH_PREVIEW_COUNT = 8

#: How many sample paths get named inline per residue class before the rest
#: collapse into a "... and N more" tail (C3). Mirrors the reasoning behind
#: `_REPORT_PATH_PREVIEW_COUNT` and `_EXCLUDED_LOG_PREVIEW_COUNT`: a per-class
#: COUNT is always shown regardless, this only bounds the sample listed next
#: to it -- a residue class with hundreds of paths (the exact shape the three
#: hand-typed bulk sweeps left behind) must never turn into a per-path dump.
_RESIDUE_CLASS_SAMPLE_COUNT = 3

#: How many residue CLASSES (not paths within a class) get their own line
#: before the rest collapse into a "... and N more class(es)" tail. Review:
#: code-reviewer (Finding 1) -- `_RESIDUE_CLASS_SAMPLE_COUNT` above only
#: bounds the sample listed WITHIN one class; it does not bound the number
#: of classes themselves. `_residue_class` returns the bare top-level
#: segment for a repo-root file with no directory, so a pile of repo-root
#: orphan files (e.g. many `orphan_NNN.py` siblings) each become their own
#: class and the render loop below emitted one line per file -- reproducing
#: the exact "1938-line stdout for a one-file commit" incident this
#: module's docstring says the report exists to retire, for the residue
#: section specifically. Mirrors `_RESIDUE_CLASS_SAMPLE_COUNT`'s reasoning:
#: the per-class COUNT is still shown in the summary line above regardless,
#: this only bounds how many per-class detail lines get printed inline.
_RESIDUE_CLASS_PREVIEW_COUNT = 8

#: How many `dropped_groups` entries (handoff item 1, 2026-08-03
#: touched-path-bookkeeping) get their own line -- in `_render_report`'s
#: stdout and in `_log_dropped_groups_diagnostic`'s log sink alike -- before
#: the rest collapse into a "... and N more group(s)" tail. Same idiom as
#: `_RESIDUE_CLASS_PREVIEW_COUNT`: one line per GROUP, never one line per
#: path -- a caller that named hundreds of groups in one call must not
#: reproduce the 1938-line `excluded` incident this module's docstring
#: already retired once.
_DROPPED_GROUPS_PREVIEW_COUNT = 8


def _commit_changed_count(sha: Optional[str], worktree_root: Optional[str]) -> Optional[int]:
    """SUPERSEDED (C5, 2026-08-15 composition-invocation-budgets plan) —
    always returns ``None`` now. This used to run one untimed ``git show
    --name-only`` spawn per commit group on the COMMIT hot path purely to
    populate a parenthetical in a human-readable report line (see
    `_render_report`'s "committed ... file(s) changed (... in scope)" tail);
    that line already had a no-spawn fallback wording, already tested
    (`"%d file(s) in scope (changed-file count unavailable)"`), and nothing
    parses this output — it is terminal/report text only (confirmed by grep
    across `coordinator_core/`, `coordinator/bin/`, tests, `docs/`, `state/`
    at the time of this cut; see the plan's C5 row). `_render_report` now
    takes that fallback path unconditionally. Kept as a stub (rather than
    deleted outright) so `_render_report`'s call site and this function's own
    call-signature stay unchanged for any caller that still imports it
    directly; it is intentionally side-effect-free and spawn-free.
    """
    return None


#: Emitted FIRST when `scope.normalize_diagnostic_fired()` is set. First, not
#: last, because `_render_report`'s outcome-last property is pinned by
#: `coordinator/tests/test_safe_commit_offer_outcome_signal.py` — and bounded
#: output means the head of the report is a few lines above the verdict, not
#: buried.
#:
#: The wording tracks EXACTLY what a set latch now implies, no more. Since
#: `scope._ls_files_failure_is_benign` landed (2026-08-05), the latch no longer
#: fires for a path outside this repo — the routine case, which the relpath
#: fallback handles correctly and which used to put this banner at the head of
#: most reports on a non-condition, a false alarm in the top line of the exact
#: report the same commit was fixing for being falsely alarming. What remains
#: is an UNCLASSIFIED normalization failure, whose consequence for this report
#: is precisely: an entry may have been dropped (a path this session wrote is
#: absent from the pathspec) or mis-normalized (present but naming the wrong
#: file) — NOT that anything was mis-committed, and NOT that the entries are
#: known-bad.
_DEGRADED_SCOPE_NOTICE = (
    "DEGRADED INPUT — the touch record was written this process after an "
    "unexpected path-normalization failure (see stderr: normalize_touch_path); "
    "entries may have been dropped or mis-normalized, so the scope below may "
    "be incomplete or name the wrong path. Routine out-of-repo paths do not "
    "raise this."
)


def _render_report(report: CommitOfferReport, worktree_root: Optional[str] = None) -> str:
    """Render the operator-facing report: detail first, VERDICT LAST.

    Two properties are load-bearing, both from the 2026-08-03 live incident
    (`coordinator/tests/test_safe_commit_offer_outcome_signal.py`):

    Outcome last. Every per-group verdict line is emitted AFTER that group's
    own path detail, and the excluded summary is emitted before any of it.
    Previously the excluded dump came last, so the verdict an operator
    actually needs was buried under it and only findable by grep.

    Bounded output. Nothing here grows one line per excluded path — `excluded`
    held 1938 entries in the live run (a 1938-line stdout for a one-file
    commit). The withholding POLICY is unchanged and correct: it is what stops
    one session's stop-event commit sweeping a peer's work. Only its rendering
    is aggregated — a count, split by reason class, plus a pointer at the
    full list. The `residue` section carries the same bound on TWO axes, not
    one: `_RESIDUE_CLASS_SAMPLE_COUNT` bounds the sample paths listed inside
    one class, and `_RESIDUE_CLASS_PREVIEW_COUNT` (Review: code-reviewer,
    Finding 1) separately bounds how many CLASSES get their own inline line
    before the rest collapse into a "... and N more class(es)" tail — a repo
    with many repo-root residue files (each its own class, per
    `_residue_class`) previously emitted one line per file here, the same
    unbounded shape this paragraph already forbids for `excluded`.
    `dropped_groups` (handoff item 1, 2026-08-03 touched-path-bookkeeping)
    carries the same bound via `_DROPPED_GROUPS_PREVIEW_COUNT` — one line
    per GROUP, never per path.

    A THIRD property, from the example-cockpit-repo-em memo of 2026-08-05: every
    file count is labelled as either the commit's own change count or the
    breadth of the pathspec it was handed, never a bare "N file(s)" readable
    as both. Live, a one-file commit was reported as "14 file(s)" because the
    other 13 in-scope paths had nothing to commit — an inflated number on a
    shared branch raises exactly the alarm ("did this sweep a peer's work?")
    this module exists to retire, settleable only by the `git show --stat` the
    report was meant to save.

    ``worktree_root`` is where the change count is read from; without it (a
    caller rendering a report detached from its repo) the landed-commit line
    degrades to the labelled scope-only form. It never degrades to the old
    ambiguous wording, and never raises.
    """
    lines: List[str] = []
    groups = report["groups"]
    excluded = report["excluded"]

    if scope_module.normalize_diagnostic_fired():
        lines.append(_DEGRADED_SCOPE_NOTICE)

    # C4 (AC9) — a short-circuit outcome (degraded/indeterminate skip, or a
    # dirty-conflict withhold) produced no per-group lines below to carry the
    # verdict; without this, those calls would otherwise render identically
    # to a genuinely clean tree. A plain `"committed"`/`"empty"` outcome adds
    # no line here — the per-group loop below already states that verdict.
    outcome = report.get("outcome")
    if outcome and outcome["status"] in (
        "skipped_indeterminate",
        "skipped_degraded",
        "dirty_conflict_skipped",
    ):
        lines.append("OUTCOME (%s): %s" % (outcome["status"], outcome["detail"]))

    if excluded:
        owned = sum(1 for e in excluded if e["reason"].startswith("owned by session"))
        untouched = len(excluded) - owned
        lines.append(
            "Excluded %d file(s) — %d claimed by another session, %d untouched by "
            "this session; left uncommitted on purpose (run `safe-commit-offer "
            "dry_run=true` for the full list)." % (len(excluded), owned, untouched)
        )

    dropped_groups = report.get("dropped_groups") or []
    if dropped_groups:
        # Handoff item 1 (2026-08-03, touched-path-bookkeeping) -- a
        # caller-supplied group whose named paths fell wholly or partly
        # outside this session's computed `safe_paths` is advisory-only
        # (DR-227, same as `excluded` above), never a gate: the group is
        # simply smaller (or absent) from `groups` below, not a failure.
        lines.append(
            "Dropped %d caller-supplied group(s) — named path(s) outside "
            "this session's computed scope, left uncommitted on purpose "
            "(run the session.safe_commit_offer op with dry_run for the full list):"
            % len(dropped_groups)
        )
        shown = dropped_groups[:_DROPPED_GROUPS_PREVIEW_COUNT]
        for dg in shown:
            lines.append(
                "  %s — named %d paths, %d matched" % (dg["message"], dg["named"], dg["matched"])
            )
        remaining = len(dropped_groups) - len(shown)
        if remaining > 0:
            lines.append("  ... and %d more group(s)" % remaining)

    residue = report.get("residue") or {}
    if residue:
        total_residue = sum(len(paths) for paths in residue.values())
        lines.append(
            "Residue: %d file(s) still dirty after this commit, not this "
            "session's peer-owned/committed work, across %d class(es) "
            "(report-only -- nothing staged or blocked; see "
            "the session.safe_commit_offer op with dry_run, or git status, for the "
            "full list):" % (total_residue, len(residue))
        )
        residue_items = list(residue.items())
        shown_classes = residue_items[:_RESIDUE_CLASS_PREVIEW_COUNT]
        for cls, paths in shown_classes:
            sample = paths[:_RESIDUE_CLASS_SAMPLE_COUNT]
            tail = ""
            remaining = len(paths) - len(sample)
            if remaining > 0:
                tail = " (+%d more)" % remaining
            lines.append(
                "  %s: %d file(s) — e.g. %s%s"
                % (cls, len(paths), ", ".join(sample), tail)
            )
        remaining_classes = len(residue_items) - len(shown_classes)
        if remaining_classes > 0:
            lines.append(
                "  ... and %d more class(es) (see `safe-commit-offer "
                "dry_run=true` or `git status` for the full list)"
                % remaining_classes
            )

    # RECONCILIATION (2026-08-29). Rendered SAMPLE-BOUNDED, exactly like
    # `residue` and `excluded` above -- the enumeration lives in the report
    # object's own `reconciliation` key, which is what an adopting caller
    # reads; this block is the operator's pointer at it, never the list
    # itself. A 501-path unclaimed set is a real observed size on this
    # worktree, and one line per path is the unbounded shape this function's
    # own docstring forbids.
    reconciliation = report.get("reconciliation") or {}
    claimed_absent = reconciliation.get("claimed_absent") or []
    unclaimed = reconciliation.get("unclaimed") or []
    if claimed_absent:
        sample = claimed_absent[:_RESIDUE_CLASS_SAMPLE_COUNT]
        tail = ""
        remaining = len(claimed_absent) - len(sample)
        if remaining > 0:
            tail = " (+%d more)" % remaining
        lines.append(
            "Claimed but absent from disk: %d path(s) this session claims "
            "have no file on disk — e.g. %s%s. Not an error on its own: a "
            "deletion this session made is a legitimate thing to commit, and "
            "an archival `git mv` leaves one at the source by construction."
            % (len(claimed_absent), ", ".join(sample), tail)
        )
    if unclaimed:
        sample = unclaimed[:_RESIDUE_CLASS_SAMPLE_COUNT]
        tail = ""
        remaining = len(unclaimed) - len(sample)
        if remaining > 0:
            tail = " (+%d more)" % remaining
        lines.append(
            "Unclaimed and dirty: %d path(s) no session claims — e.g. %s%s. "
            "NAMED, NOT ADOPTED: nothing here was committed and nothing here "
            "is attributed to this session. The full list is enumerated on "
            "the report's `reconciliation.unclaimed`; a shell-written file of "
            "your own lands here, and so do paths that are nobody's."
            % (len(unclaimed), ", ".join(sample), tail)
        )
    if not reconciliation.get("reconciled", False):
        lines.append(
            "Ledger not reconciled against the tree this call — the two "
            "buckets above are unchecked, not empty."
        )

    if not groups:
        # "I could not look" must never render as "there is nothing". An empty
        # `groups` has two causes that read identically to an operator: a
        # genuinely clean tree, and a tree where every dirty path was seen and
        # declined because nothing carried this session's claim — the shape a
        # CLI-written file always takes, since only the Edit/Write hot path
        # writes `touched.txt`. The `excluded` count is already stated above;
        # this line adds the disposition and the route out, not a second count.
        #
        # `excluded` alone did not carry that distinction. A session whose every
        # write went through the Bash tool -- the channel this harness's own
        # bypass-permissions instruction directs work through, and the one the
        # touch-list never sees -- produces `excluded: []` AND a non-empty
        # `residue`, and fell to the clean-tree line below with its own dirty
        # files listed in the residue table directly above it. Observed
        # 2026-08-26 (state/bug-backlog/2026-08-26-safe-commit-offer-attributes-
        # no-bash-wri-47b599a8460a.yaml): three modified tracked files, exit 0,
        # "working tree clean". Either signal means dirty paths exist, so the
        # clean-tree claim requires BOTH to be empty.
        if excluded or residue:
            lines.append(
                "Nothing to commit for session %s — every dirty path was seen "
                "and declined; none carried this session's claim. A file a CLI "
                "or a workflow-internal agent wrote for this session records no "
                "claim and reads as untouched here — so does anything written "
                "through the Bash tool rather than Edit/Write. Commit it by "
                'name: `coordinator-safe-commit "<subject>" -- <paths>`.'
                % report["session_id"]
            )
        else:
            lines.append(
                "Nothing to commit for session %s — working tree clean."
                % report["session_id"]
            )

    for g in groups:
        preview = g["paths"][:_REPORT_PATH_PREVIEW_COUNT]
        lines.extend("  %s" % p for p in preview)
        remaining = len(g["paths"]) - len(preview)
        if remaining > 0:
            lines.append("  ... and %d more file(s)" % remaining)

        if g["committed"]:
            sha = (g["sha"] or "?")[:12]
            push = g.get("push_state") or "?"
            scope_count = len(g["paths"])
            changed = _commit_changed_count(g.get("sha"), worktree_root)
            if changed is None:
                tail = "%d file(s) in scope" % scope_count
            else:
                tail = "%d file(s) changed (%d in scope)" % (changed, scope_count)
            lines.append(
                "committed %s (%s) — %s — %s" % (sha, push, g["message"], tail)
            )
        elif g["commit_failed"]:
            detail = g.get("error") or "commit failed"
            lines.append("NOT committed — %s — %s" % (g["message"], detail))
        else:
            # The benign no-op. "NOT committed" is reserved for a genuine
            # failure and must never appear here: an operator (or an automated
            # caller) reading it over paths a previous invocation already
            # landed retries or hand-commits a duplicate — the live incident.
            # The count here is pathspec breadth and is labelled as such: no
            # commit happened on this call, so no change count may be attached
            # to it (example-cockpit-repo-em memo, 2026-08-05).
            lines.append(
                "already committed — %s — %d file(s) in scope, nothing new to commit (%s)"
                % (g["message"], len(g["paths"]), g.get("reason") or "no-op")
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Op
# ---------------------------------------------------------------------------


@register_op("session.safe_commit_offer")
def _handler(params: dict, repo_root=None) -> dict:
    """session.safe_commit_offer — MUTATING: commits and pushes THIS session's
    own claimed dirty paths. No confirmation step of any kind.

    Replaced this module's `main()` CLI on 2026-08-27. There is no Python door
    left and none may be re-added: reaching the warm engine through an
    interpreter cost two process starts (43ms for a bare interpreter, 146ms to
    import this module) before the call resolved a session id or touched git,
    against 13ms for the whole round trip through `coordinator-invoke.exe`.
    The interpreter start WAS the defect, so a shim preserving it behind a
    nicer name is not a compatibility surface — see DR-344 § "Warm engine,
    <50ms to reach it", and this op's sizing-object
    (`dlv-the-safe-commit-offer-answers-on-the-exe-0899da`).

    HOW TO CALL IT — one positional JSON string, and nothing else:

        coordinator-invoke.exe session.safe_commit_offer "{\"cwd\": \"<repo>\",
            \"session_id\": \"<uuid>\", \"message\": \"<subject>\"}"

    NOT `k=v` tokens. `coordinator-invoke` takes a single positional it parses
    as JSON: the first `k=v` token is swallowed as that positional and fails
    with `Invalid params_json: Expecting value: line 1 column 1`, and a second
    token is rejected outright as `unrecognized arguments`. This module's own
    docstring prescribed the `k=v` form until 2026-08-27 and doe-claude-em's
    executor copied it verbatim into a repoint that was dead on arrival; it
    fails loud, so nothing shipped, but a caller-facing example is a contract
    and this one was wrong.

    NOT `--repo` either. That flag is refused for this op (`-32603`) because
    its scope is "none" (DR-279) — it resolves its own target from wire params.
    A house style that reflexively appends `--repo` (as `push.outstanding`'s
    does) breaks every call.

    Sync (not async): `commit_session_offer` drives the whole call under a
    single `asyncio.run()`; ipc.py offloads sync handlers via
    `asyncio.to_thread` (the `commit.exec_bit_change` pattern).

    Op scope "none" (mirrors `session.scope_report`, which composes this
    module's own `compute_offer` and resolves its target the same way): the
    `repo_root` handler arg is unused and always None. Identity comes from
    wire params, falling back to
    `coordinator_core.session.core.resolve_session_id(cwd)` — the substrate's
    canonical 4-tier resolver, anchored on the CALLER's `cwd`, never on this
    server process's own environment. Load-bearing on the warm path: this
    process is the server, whose `os.environ` is its spawner's, so an env-only
    read here would resolve the ENGINE's identity and commit under the wrong
    session's claim. `cwd` is what makes the resolution the caller's.

    params:
      session_id (str, optional)  — explicit session id; falls back to
                                    `resolve_session_id(cwd)` as above.
      cwd        (str, optional)  — the caller's working directory, threaded
                                    into BOTH identity resolution and
                                    `compute_offer`.
      message    (str, optional)  — single group, all of `safe_paths`, under
                                    this subject (a caller that already decided
                                    on ONE description).
      groups     (list, optional) — `[{"paths": [...], "message": "..."}]` for
                                    a caller with real per-group judgment.
                                    Mutually exclusive with `message`. This is
                                    the inline replacement for the CLI's
                                    `--groups-json <file>`: the wire carries the
                                    list itself, so no caller writes a temp file
                                    to hand a list to a local process.
      dry_run    (bool, optional) — compute and return the offer WITHOUT
                                    committing, for inspection only. Never gate
                                    a real invocation behind it.

    Returns the `CommitOfferReport` verbatim, plus `"rendered"`: the operator-
    facing text `_render_report` produces. The report stays the structured
    return — `rendered` is an additional field, never a replacement — because
    the ceremonies that call this op echo that text rather than re-derive it,
    and moving the rendering out would re-author it once per ceremony.

    On a dry run, returns `{"dry_run": True, **offer}` with `rendered` naming
    the safe-path count — never a commit.

    On an unresolvable session id or a usage error, returns an error envelope
    rather than raising: `{"error": str}` alongside empty result fields. The
    CLI's exit codes are gone with the CLI; a caller distinguishes outcomes by
    reading `error` and `failed_groups`, and `failed_groups` being non-empty is
    the only "one or more groups genuinely failed to commit" signal. An empty
    result is still a valid "nothing to commit" outcome, INCLUDING the benign
    already-committed no-op — that shape must never be conflated with a
    failure (see the module's wolf-crying constraint).
    """
    cwd = params.get("cwd")
    message = params.get("message")
    groups = params.get("groups")

    if message is not None and groups is not None:
        return _error_envelope("params.message and params.groups are mutually exclusive")
    if groups is not None and not isinstance(groups, list):
        return _error_envelope("params.groups must be a list of {paths, message} objects")

    session_id = params.get("session_id") or core.carried_session_id()
    if not session_id:
        return _error_envelope(
            "caller identity could not be established — pass params.session_id. "
            "This op refuses rather than falling back to the engine process's "
            "own environment: on the warm path that environment belongs to "
            "whoever spawned the server, so the fallback commits one session's "
            "paths under another's claim."
        )

    if params.get("dry_run"):
        offer = compute_offer(session_id, cwd)
        return {
            "dry_run": True,
            "rendered": "DRY RUN — safe_paths: %d, excluded: %d"
            % (len(offer["safe_paths"]), len(offer["excluded"])),
            **offer,
        }

    resolved_groups: Optional[List[CommitGroup]] = None
    if message is not None:
        offer = compute_offer(session_id, cwd)
        resolved_groups = (
            [{"paths": offer["safe_paths"], "message": message}]
            if offer["safe_paths"]
            else []
        )
    elif groups is not None:
        resolved_groups = groups

    report = commit_session_offer(session_id, cwd, resolved_groups)
    worktree_root = core.git_root(cwd) or cwd or "."

    _log_excluded_diagnostic(worktree_root, session_id, report["excluded"])
    _log_dropped_groups_diagnostic(worktree_root, session_id, report["dropped_groups"])
    if report["failed_groups"]:
        _log_failed_groups_diagnostic(worktree_root, session_id, report["failed_groups"])

    return {**report, "rendered": _render_report(report, worktree_root)}


def _error_envelope(message: str) -> dict:
    """Structured refusal, never an exception — a handler that raises reaches
    the caller as a generic JSON-RPC internal error, losing which of this op's
    named preconditions actually failed.
    """
    return {
        "session_id": "",
        "groups": [],
        "excluded": [],
        "failed_groups": [],
        "dropped_groups": [],
        "error": message,
        "rendered": "safe-commit-offer: %s" % message,
    }
