"""
coordinator_core.ops.session.scope_report — read-only session-scope reporter,
plus the in-process ownership helper used by the commit-confinement guard
(`coordinator_core.bash_guards.block_subagent_commit`, C4b) and the commit
sink (C4c) to decide whether a candidate commit pathspec is safe to stage.

CITATION NOTE, once, for every `ceremony.scoped_git_commit` reference below
(2026-08-31, `state/handoffs/2026-08-30-the-commit-path-scoped-commits-the-
share.md` owed item 3 — "re-home the three stale citations ... or the next
reader will conclude the leg is dead"). THE LEG IS LIVE; ONLY THE SINK MOVED.
`ceremony.scoped_git_commit` was DELETED over the brightline (DR-344), not
suspended, and `coordinator_core/ops/ceremony/scoped_git_commit.py` does not
exist. Its successors:

  * the commit OP is `ceremony.commit_v2`;
  * the in-process committer both it and `safe_commit_offer` reach is
    `coordinator_core.git.commit :: commit_paths` (the killed
    `commit_pipeline.run_commit_pipeline` is the other name a reader will
    find in older prose — same replacement);
  * `--stage-patch` / `stage_patch` is retired outright:
    `git_native.py :: patch_touched_paths` records that `commit_paths` "has
    no `stage_patch` concept", and the shared-index reconcile gap that memo
    reported is closed by bound 7 in
    `git_native.py :: _commit_scoped_private_index`.

The C4c references are kept rather than rewritten because they name WHICH
CONSUMER a given rule was written for, and rewriting them would erase the
distinction between the guard's constraints and the sink's. Read them as
"the commit sink", whose current identity is above.

Purpose: TWO surfaces, which since the 2026-08-21 rebuild no longer share a
source — read that as the load-bearing fact about this module, not a
transitional state:

  (a) `session.scope_report` — a REGISTERED, read-only op. Reports the
      calling session's own claimed dirty paths (`safe_paths`) and the
      narrated `excluded` set, straight from
      `coordinator_core.ops.session.safe_commit_offer.compute_offer`. Mutates
      nothing. This is the surface a DISPATCHED AGENT reads (plan AC10) —
      never the guard's own transport (see Negative-spec).

  (b) `assert_paths_in_session_scope` — a plain in-process function, NOT an
      op. Strict allow-list, used by BOTH `block_subagent_commit` (PreToolUse
      guard, C4b) and `scoped_git_commit`'s handler (commit sink, C4c). Never
      shell the op out of a guard: that would put the answer behind a harness
      timeout whose non-zero exit reads as an ALLOW, outside a fail-closed
      guard's contract, and adds a spawn-per-call op invocation to the Bash
      hot path against the per-call invocation budget.

REVERTED 2026-08-03 (P1 security fix, reversing the same-day sink-polarity
amendment described below): the sink briefly composed a SEPARATE,
deny-list-shaped predicate, `assert_paths_not_foreign_owned` — since DELETED
from this module. That amendment was motivated by a real live incident (the
EM's own commit of its own freshly-dispatched sub-agent's output was
rejected, because `compute_offer` cannot yet attribute a path to its
dispatching session during the async `.agents/<aid>/em-session-id.txt`
back-pointer race window — see `coordinator_core.session.scope.compute_scope`
Step 3b). But `compute_offer`'s own orphan/race-window pass-through
(`compute_scope` Step 5) is NOT scoped to the calling session at all — it
scans every currently-dirty path in the repo, independent of who is asking,
and `session_dir()` never verifies a `session_id` actually names a session
that ever existed. Composed into a caller-facing predicate, that meant ANY
caller — including a fabricated `session_id` reaching the sink via the
exact obfuscated-payload threat model AC18 exists to defend against — got
the identical "safe to pass" verdict for a genuinely live peer's in-flight
race-window path, or a dead session's leftover uncommitted work, that a true
owner would have gotten. A fail-closed gate with occasional operational
friction beats a gate with a hole: the sink now composes the SAME strict
`assert_paths_in_session_scope` the guard uses. The known cost is real and
accepted — a caller invoking the sink during the back-pointer race window
can be falsely rejected — but the workaround (an explicit `git add --
<paths>` / `git commit -- <paths>`) is EM-available, not subagent-available,
so AC17/AC18's caller-confinement property is preserved rather than
reopened. Recorded here so the permissive variant is not re-derived: it was
tried, found unsound, and reverted.

THE TWO-OFFER SPLIT IS GONE, AND SO IS THE CLASS OF BUG IT MANAGED
(2026-08-21 rebuild). What follows is kept because it names a shape that must
not be rebuilt, not because it describes what runs.

`assert_paths_in_session_scope` no longer composes `compute_offer` at all. It
reads `coordinator_core.session.claim_index.classify_paths` — ONE zero-spawn
index rebuild that answers about ANY path put to it, including one this
session never touched. The old leg could not do that: `compute_scope` only
answered about paths it had already adopted as candidates, so naming a live
holder for a caller-supplied path required a SECOND, wider offer computed
with the caller's own pathspec as `extra_candidates` — an offer that, by
construction, adopts whatever it is handed. Two offers, one safe to read as
a verdict and one catastrophic to, distinguished only by discipline. The
second offer no longer exists, and neither does the merge that discipline
was holding back. NEGATIVE SPEC: do not reintroduce a caller-supplied
candidate set anywhere in this module.

The historical split, recorded so the shape is recognizable if it recurs
(2026-08-07, scoped-commit ownership-gate misclassification; bug-backlog
`2026-08-06-scoped-commit-denial-names-unclassified-for-a-peer-held-path`):

  - The PRIMARY offer — `compute_offer(session_id, cwd)`, no
    `extra_candidates` — is the ONLY input to the verdict. Every set the
    membership test reads (`safe_set`, `orphan_set`, `all_orphans`,
    `call_indeterminate`, `verified_caller`) is derived from it, unchanged.
  - The CLASSIFICATION offer — `compute_offer(..., extra_candidates=<the
    caller's own pathspec>)` — is computed lazily, only once at least one
    path has already been denied, and is consumed by `_classify_denied_path`
    for WORDING ONLY.

The classification offer exists because a path the calling session never
touched is not a `compute_scope` candidate, so Step 3/3b's peer-claim read
is never consulted for it: it gets no `skipped` entry naming its holder, and
Step 5 keeps it out of `orphans` too (`other_owner` holds it). It is absent
from every key of the primary offer, so `_classify_denied_path` fell through
to `_CLASSIFICATION_UNCLASSIFIED` — "unclaimed/never classified" — for a path
a live peer demonstrably held, pointing an operator at `include_orphans:
true`, a remedy that provably cannot help a peer-claimed path. Naming those
paths as `extra_candidates` puts them through Step 1 so the peer read
happens and the holder can be named.

It is exactly that Step 1 adoption that makes the classification offer
UNUSABLE as an allow-list: a named path no session claims is adopted into
`safe_paths` BECAUSE the caller named it. Reading membership off it would
hand any caller any dirty path in the tree for the price of putting it in
its own pathspec — a hole strictly worse than the message defect it fixes,
and the same shape as the REVERTED predicate above reached by a different
route. A future "simplification" collapsing the two `compute_offer` calls
into one is therefore a privilege escalation, not a tidy-up; it is pinned
red by `test_classification_offer_never_widens_the_verdict` in this module's
own test file, and by `compute_offer`'s own `extra_candidates` negative spec.

Allow-list polarity, not denylist (LOAD-BEARING — see the governing lesson,
`state/lessons/2026-07-31-a-dirty-tree-is-not-evidence-you-are-alone-in-it-
3d5f8a91.yaml`): `assert_paths_in_session_scope` allows ONLY when every
element of `paths` is a member of THIS session's own `safe_paths`, as
`compute_offer` computes it. Everything else — a path owned by another live
session, an unclaimed/orphan dirty file, a path `compute_offer` never saw at
all — denies. A denylist keyed on "owned by another LIVE session" would let
every UNOWNED path pass silently; that is precisely the harm case
`compute_offer`'s own `excluded` narration already distinguishes as
"untouched by this session".

Fail-CLOSED throughout: an empty/unresolvable `session_id`, an empty
`paths`, an unreadable scope (`compute_offer` returning an empty
`safe_paths` because the git root did not resolve — `compute_scope` itself
never raises for that case, see its own docstring; the empty-set outcome is
handled here by ordinary allow-list membership, not a special branch), or
ANY exception raised anywhere beneath `compute_offer` — all return
`(False, <reason>)`. This function never raises and never returns `True` on
a degraded read.

No liveness check is added by this module — `compute_offer` (via
`coordinator_core.session.scope.compute_scope`) already carries its own
fail-closed liveness gate (`coordinator_core.session.liveness`), and
re-deriving that gate here is exactly the mistake `compute_offer`'s own
docstring warns against (see `safe_commit_offer.py`'s module docstring:
"do NOT call compute_scope directly"). This module composes `compute_offer`
only; it never touches `coordinator_core.bash_guards._alternative_liveness`
or `check_raw_pid_liveness` — those modules probe guard-CHECK command
alternatives, an unrelated concern, and are named in this dispatch's own
substrate notes only as the wrap-not-modify seam were a liveness check ever
needed here (it is not, for this chunk).

Spec backlink: docs/plans/2026-08-03-narrow-subagent-commit-confinement-
two-classes.md § C4a (AC10, substrate for AC11/AC12/AC17).

Self-registration: importing this module calls
register_op("session.scope_report", _handler) as a side-effect. Add this
module to coordinator_core/ops/_registry_map.py and
coordinator_core/op_scopes.py to trigger/scope registration.

Negative-spec:
  - Does NOT call `coordinator_core.session.scope.compute_scope` directly —
    always composes `safe_commit_offer.compute_offer`, which already carries
    the hardened fail-closed semantics (see module docstring above).
  - Does NOT mutate any state — no git write, no touched.txt write, no
    session-shape.json write. Both surfaces in this module are read-only.
  - `assert_paths_in_session_scope` is NEVER invoked by shelling out to the
    `session.scope_report` op — the guard and the sink each import it
    directly, in-process.
  - `assert_paths_in_session_scope` does NOT implement a denylist ("deny only
    if owned by another live session") — see "Allow-list polarity" above.
  - Does NOT let the CLASSIFICATION offer (see "TWO OFFERS" below) reach
    any membership test — `safe_set`, `orphan_set`, `all_orphans`,
    `call_indeterminate` and `verified_caller` all come from the PRIMARY
    offer, and merging the two is the failure mode
    `test_classification_offer_never_widens_the_verdict` exists to catch.
  - Does NOT define a caller-unscoped orphan/race-window pass-through
    predicate (`assert_paths_not_foreign_owned`, deleted 2026-08-03) — see
    "REVERTED 2026-08-03" above for why that shape is unsound as a
    commit-gating sink, and do not re-add it without first closing the
    caller-affinity gap named there.
"""

from __future__ import annotations

import os
from typing import Optional, Sequence, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops.session.safe_commit_offer import compute_offer
from coordinator_core.session import claim_index
from coordinator_core.session import core
from coordinator_core.session.liveness import live_session_ids

#: Orphan ADOPTION is enabled — orphan *diagnosis* has shipped since
#: 2026-08-03, and staff-eng R1 (2026-08-03, re-review pass 2) is now closed:
#: `ScopeResult.indeterminate` (`coordinator_core.session.scope`) covers the
#: non-candidate shape the earlier `orphans - skipped_paths` subtraction
#: alone could not — a dirty path never adopted as a candidate this call
#: (`started_at` in the future or unreadable, or an mtime predating session
#: start) used to bypass `compute_scope` Step 4 entirely, get no `skipped`
#: counterpart for that subtraction to remove, and reach `orphans` even
#: while a live peer's claim set was unreadable. `compute_offer` now returns
#: `orphans: []` outright whenever `ScopeResult.indeterminate` is set (any
#: unreadable claim set, or an unresolved agent-race overlap, this call) —
#: see `safe_commit_offer.compute_offer`'s own docstring for the full
#: accounting, including why the whole call's orphan set is withheld rather
#: than just the specific candidate.
#:
#: Residual, NOT closed by this fix (deliberately out of scope — see
#: `ScopeResult.indeterminate`'s own docstring and `compute_scope`'s own
#: docstring for where it is documented): the pre-existing liveness-
#: enumeration partial under-report. A peer claim released by that residual
#: never sets `unreadable_other_sessions` or `agent_race_paths`, so it never
#: sets `indeterminate` either — it shares R1's shape (an orphan reaching
#: adoption despite an unresolved peer claim) but is not covered by this
#: gate, because nothing marks that call as degraded. This is what makes it
#: a genuine residual rather than an oversight; do not chase it as part of
#: this constant.
_ORPHAN_ADOPTION_ENABLED = True


def assert_paths_in_session_scope(
    session_id: str,
    paths: Sequence[str],
    cwd: Optional[str] = None,
    *,
    allow_orphans: bool = False,
    already_clean: Optional[Sequence[str]] = None,
) -> Tuple[bool, str]:
    """Allow-list ownership check over a candidate commit pathspec.

    Returns (True, "") ONLY when session_id resolves, the scope is readable,
    `paths` is non-empty, and EVERY element of `paths` classifies
    ``OWNERSHIP_MINE`` under ``claim_index.classify_paths`` — OR, when
    `allow_orphans` is True AND the caller clears the positive-evidence check
    below, classifies ``OWNERSHIP_UNCLAIMED``.

    DOCSTRING CORRECTED 2026-08-29 (flagged by doe-claude-em while answering
    the SC-DR-001 read-time-backstop memo). This paragraph described the
    membership test as ``compute_offer(...)["safe_paths"]`` / ``["orphans"]``
    with a citation to DoE's ``scoped-safety-commits.md:131``. That was the
    PRE-2026-08-21 shape. The runtime leg below has been
    ``claim_index.classify_paths`` since the rebuild that removed the
    73-process / 5,609ms composition, and ``compute_offer``'s ``orphans`` key
    is now ALWAYS EMPTY by its own contract — so a reader auditing this gate
    against the old description would conclude the orphan arm is inert. It is
    not: ``_ORPHAN_ADOPTION_ENABLED`` is ``True`` and the arm is live. Only
    the ENUMERATION was lost with dirtiness, not the adoption; the
    enumeration is restored at the report seam as
    ``safe_commit_offer.Reconciliation.unclaimed``, which is the candidate
    list an operator's ``--include-orphans`` remedy is meant to be handed
    (DoE SC-DR-022 half 1: the named paths are named by the ENGINE, never
    assembled by the adopter).

    `allow_orphans` never relaxes the peer-claimed case: a path claimed by a
    LIVE peer session still denies regardless of this flag (incident
    62e9a1f73). What keeps orphan and peer-claimed disjoint is no longer a
    fixed-up `orphans` field but `classify_paths` itself, which is fail-closed
    in both directions — an aborted walk yields neither `mine` nor
    `unclaimed`, and a peer claim denies whether or not the holder is live.

    KNOWN DIVERGENCE, named rather than left drifting (doe-claude-em,
    2026-08-29). Doctrine defines an orphan as **dirty AND claimed by
    nobody**. ``OWNERSHIP_UNCLAIMED`` is a pure claim-ledger verdict with no
    dirtiness component, so this gate currently admits **claimed by nobody**
    full stop — a clean, never-touched file named in an operator's pathspec is
    adoptable where the definition says it should not be. Tracked at
    ``state/bug-backlog/2026-08-29-orphan-adoption-admits-a-clean-path.yaml``;
    the resolution is to narrow the arm to the dirty set, NOT to widen the
    definition (see that row for the reasoning and the cost).

    Review: staff-eng F2/F3 (2026-08-03) — `allow_orphans` additionally
    requires POSITIVE EVIDENCE that `session_id` names a session directory
    that exists on disk AND contains a `meta.json` (written by
    `coordinator_core.session.core.init`, which every real touch-tracked
    session goes through — see `coordinator_core.session.scope.touch`'s
    lazy-init fallback). Absent either, `allow_orphans` has NO effect and the
    call falls back to the strict allow-list exactly as if `allow_orphans`
    were `False`.

    Review: staff-eng R2/R3 re-review (2026-08-03, pass 2) — softened from an
    earlier draft of this paragraph that claimed this check "closes two
    same-shaped holes" against the AC18 obfuscated-payload threat model. It
    does not hold against that model: every caller of this function already
    has repo write access by construction (it is reached from an in-process
    guard/sink, never from an unauthenticated surface), so a caller able to
    invoke this at all can already create a session directory and a
    `meta.json` by hand — this check raises the cost of a fabricated
    identity from "invent a string" to "create a directory and a file", it
    does not close it. What it DOES close, honestly: (F2) the accidental/
    naive shape — a `session_id` typo or a caller that never meant to name a
    real session gets denied rather than silently adopting every dirty
    orphan in the tree. (F3) the live incident shape this gate was built
    for — a bare `touched.txt` with no `meta.json` is not evidence of a real,
    touch-tracked session. See `_session_has_positive_evidence`'s own
    docstring for the same correction stated at the predicate itself.

    A session directory that exists but genuinely has NO `meta.json` is not
    only the fabricated-identity shape above — it is also a DOCUMENTED
    legitimate-absence race: `coordinator_core.session.scope.touch`'s own
    comment records defect A (2026-07-24), where another bookkeeping writer
    (push cursor, session-shape) can create the session dir first, leaving
    `meta.json` unwritten, and `coordinator_core.session.claims.
    atomic_dedup_append` is a second `touched.txt` writer independent of the
    touch hook entirely. Such a session silently loses `allow_orphans` — see
    `_classify_denied_path`'s own docstring for the distinct deny reason this
    module now emits for that shape, so a caller that asked for adoption can
    tell it was ignored rather than reading a message indistinguishable from
    "you never asked".

    The deny path computes a SECOND, classification-only `compute_offer`
    (see this module's "TWO OFFERS" paragraph) whose sole consumer is
    `_classify_denied_path`. The verdict returned by this function is
    byte-for-byte independent of it: it is computed only after the loop
    below has already denied at least one path, and no set the membership
    test reads is derived from it. Do not pass it anywhere else.

    See this module's OWN "REVERTED 2026-08-03" paragraph (module docstring,
    above) for the prior, unsound attempt at a similarly-shaped permissive
    check — this is a NARROWER gate (an additional requirement on an
    ALREADY-computed allow-list), not a re-derivation of ownership from a
    different signal, so it does not reproduce that shape.

    Fail-CLOSED in every other case — returns (False, <human reason>) on an
    empty/unresolvable session_id, an empty `paths`, an unreadable scope, an
    unresolvable git root, or ANY exception raised anywhere beneath. It never
    raises and never returns True on a degraded read. If `compute_offer`
    returns no readable `orphans` list, EVERY path is treated as NOT an
    orphan and denied — indeterminacy never resolves toward inclusion, even
    with `allow_orphans=True`.

    The deny reason NAMES the classification (rather than a bare "path
    outside session scope") so a caller can tell an orphan denial from a
    peer-claimed one, AND enumerates the full pathspec rather than stopping
    at the first denied path (cross-repo ruling SC-DR-019, DoE-claude: "A
    scoped-commit refusal is per-path, not per-commit. Commit the
    uncontested remainder immediately, then wait holding only the contested
    path" — mechanical only if the refusal names every contested path AND
    the committable remainder, so both are now in the message rather than
    left for the caller to re-derive by hand). The message ALWAYS starts
    with the stable prefix ``"path outside session %s scope: %r (%s)"`` for
    the FIRST denied path, unchanged, so existing pinned callers keep
    matching that prefix; it then appends a per-path breakdown of EVERY
    denied path (``<path> (<classification>)``, including the first) and,
    when at least one path in the pathspec was allowed, names that
    uncontested remainder as committable now with a narrower pathspec — or,
    if every path was denied, says explicitly that there is no committable
    remainder rather than emitting an empty list. Both lists cap at 25
    entries with a ``(+N more)`` suffix rather than truncating silently.

    `already_clean` (Half 2 of the mixed-pathspec fix; its spec backlink was
    `ceremony.scoped_git_commit`'s module docstring, deleted with that op --
    see this module's own CITATION NOTE for the successor):
    an optional caller-supplied set of paths, drawn from `paths`, already
    known to have nothing left to commit at HEAD. Advisory naming ONLY --
    never a bypass. A path in `already_clean` still goes through the
    ordinary allow-list membership test above; it is denied exactly as any
    other path outside `safe_paths`/`orphans` would be, but its deny reason
    names the real condition (`_CLASSIFICATION_ALREADY_CLEAN`) instead of
    the ownership-shaped `_CLASSIFICATION_UNCLASSIFIED` an operator would
    otherwise misread as "unclaimed" and try to fix with
    `include_orphans: true` -- a remedy that provably cannot help a path
    that was never dirty in the first place.
    """
    if not session_id or not isinstance(session_id, str) or not session_id.strip():
        return False, "session_id is empty/unresolvable"

    if not paths:
        return False, "paths is empty"

    # THE OWNERSHIP LEG, REBUILT (2026-08-21) on `claim_index.classify_paths`
    # -- one zero-spawn index rebuild, answering this pathspec directly.
    #
    # It replaces a `compute_offer` composition that cost 73 processes and
    # 5,609ms of CPU per call, on the COMMIT HOT PATH, and was stood down
    # entirely between `e927d9463` and this change (the suspension is over;
    # a dispatched committer is again prevented from committing a peer's
    # path). The old shape needed TWO offers -- one for the verdict, one
    # with the caller's own pathspec as `extra_candidates` purely so a
    # holder could be NAMED for a path this session never touched -- because
    # `compute_scope` could only answer about paths it had already adopted
    # as candidates. `classify_paths` answers about any path put to it, so
    # the second offer, and with it the whole class of bug where the two get
    # merged into one membership test, no longer exists to be gotten wrong.
    #
    # The allow-list polarity is UNCHANGED (see "Allow-list polarity" in this
    # module's docstring): allowed means positively attributed to THIS
    # session, never "not proven foreign". `classify_paths` is fail-closed in
    # both its directions -- an aborted walk yields neither `mine` nor
    # `unclaimed`, and a peer claim denies whether or not the holder is still
    # live -- so an empty or degraded index denies rather than admits.
    try:
        answer = claim_index.classify_paths(
            session_id, [p for p in paths if isinstance(p, str)], cwd=cwd
        )
    except Exception as exc:  # noqa: BLE001 - fail-closed on ANY error beneath
        return False, "claim_index.classify_paths raised: %s" % (exc,)

    # Review: staff-eng F2/F3 - `allow_orphans` takes effect only given
    # positive evidence `session_id` names a real, previously-initialized
    # session (see this function's own docstring paragraph). A fabricated
    # id, or a bare directory some non-tracked writer created with no
    # `meta.json`, degrades this to the same strict allow-list as
    # `allow_orphans=False` - never to a wider one.
    verified_caller = (
        allow_orphans
        and _ORPHAN_ADOPTION_ENABLED
        and _session_has_positive_evidence(session_id, cwd)
    )

    # Review: staff-eng R3 - an `allow_orphans` request this call did not
    # honor (because `_session_has_positive_evidence` failed) must not read
    # the same as "you never asked". Threaded through so
    # `_classify_denied_path` can name it distinctly.
    orphan_adoption_requested_but_unverified = allow_orphans and not verified_caller

    # An aborted walk is this gate's `indeterminate`: same meaning as
    # `ScopeResult.indeterminate` carried before -- "this call's claim reads
    # were degraded, so a path's classification is unresolved rather than
    # unrecognized" -- and it is what stops a denial from being narrated as
    # "the system has never heard of this path".
    call_indeterminate = not answer.complete

    # `already_clean` (Half 2 of the mixed-pathspec fix) -- advisory naming
    # only, never a bypass: a path here still goes through the ordinary
    # allow-list membership test below exactly like any other path.
    already_clean_set = set(already_clean) if already_clean else set()

    denied_paths: list = []
    allowed: list = []
    for p in paths:
        owned = answer.by_path.get(p) if isinstance(p, str) else None
        verdict = owned.verdict if owned else claim_index.OWNERSHIP_UNANSWERABLE
        if verdict == claim_index.OWNERSHIP_MINE:
            allowed.append(p)
            continue
        if verdict == claim_index.OWNERSHIP_UNCLAIMED and verified_caller:
            allowed.append(p)
            continue
        denied_paths.append(p)

    if not denied_paths:
        return True, ""

    # `_classify_denied_path` reads ONE key (`excluded`) and the orphan list,
    # and its three claimed-by branches -- including the earned-liveness
    # wording every downstream consumer discriminates on via
    # `CLAIMED_BY_SENTINELS` -- are the operator-facing contract. It is fed
    # from the same answer that produced the verdict rather than from a
    # second, wider offer: nothing here can widen what was already decided,
    # because the classification runs only over paths already denied.
    classification_offer = {
        "excluded": [
            {
                "path": p,
                "reason": "owned by session %s" % (answer.by_path[p].peers[0],),
            }
            for p in denied_paths
            if isinstance(p, str)
            and answer.by_path.get(p) is not None
            and answer.by_path[p].verdict == claim_index.OWNERSHIP_PEER
            and answer.by_path[p].peers
        ]
    }
    all_orphans = [
        p
        for p in denied_paths
        if isinstance(p, str)
        and answer.by_path.get(p) is not None
        and answer.by_path[p].verdict == claim_index.OWNERSHIP_UNCLAIMED
    ]

    denied: list = []
    for p in denied_paths:
        classification = _classify_denied_path(
            p,
            classification_offer,
            all_orphans,
            orphan_adoption_requested_but_unverified,
            call_indeterminate,
            already_clean=(isinstance(p, str) and p in already_clean_set),
            cwd=cwd,
        )
        denied.append((p, classification))

    first_path, first_classification = denied[0]
    reason = "path outside session %s scope: %r (%s)" % (
        session_id,
        first_path,
        first_classification,
    )

    reason += "; denied paths (%d): %s" % (
        len(denied),
        _format_pathspec_list(
            ["%r (%s)" % (p, c) for p, c in denied],
        ),
    )

    # A path several peers hold gets ONE holder named by
    # `_classify_denied_path` (its claimed-by branches take a single sid, and
    # the earned-liveness wording only survives on a bare token). The rest
    # would otherwise vanish from the refusal -- a silent omission in exactly
    # the message an operator uses to decide who to go and talk to -- so they
    # are named here instead of being dropped.
    multi_held = [
        (p, answer.by_path[p].peers)
        for p in denied_paths
        if isinstance(p, str)
        and answer.by_path.get(p) is not None
        and len(answer.by_path[p].peers) > 1
    ]
    if multi_held:
        reason += "; additional holders: %s" % _format_pathspec_list(
            ["%r also claimed by %s" % (p, ", ".join(peers[1:])) for p, peers in multi_held]
        )

    if allowed:
        reason += "; committable now (SC-DR-019: scoped-commit refusal is " \
            "per-path, not per-commit) as a narrower pathspec, %d " \
            "uncontested path(s): %s" % (
                len(allowed),
                _format_pathspec_list([repr(p) for p in allowed]),
            )
    else:
        reason += (
            "; no committable remainder (SC-DR-019) — every path in this "
            "pathspec was denied"
        )

    return False, reason


_MAX_LISTED_PATHS = 25


def _format_pathspec_list(entries: list) -> str:
    """Join pre-formatted ``entries`` with ", ", capping pathological output
    at :data:`_MAX_LISTED_PATHS` items and appending ``(+N more)`` when
    truncated — never silently drops entries without naming the count (see
    :func:`assert_paths_in_session_scope`'s docstring, SC-DR-019 remainder
    reporting)."""
    if len(entries) <= _MAX_LISTED_PATHS:
        return ", ".join(entries)
    shown = entries[:_MAX_LISTED_PATHS]
    return "%s (+%d more)" % (", ".join(shown), len(entries) - _MAX_LISTED_PATHS)


def _session_has_positive_evidence(session_id: str, cwd: Optional[str]) -> bool:
    """True iff `session_id` names a session directory that actually exists
    on disk AND carries a `meta.json` — the artifact
    `coordinator_core.session.core.init` writes, which every session that
    ever went through the real touch-tracked hot path
    (`coordinator_core.session.scope.touch`'s lazy-init fallback) already
    has. Review: staff-eng F2/F3 — this is the positive-evidence check
    `allow_orphans` is gated on; see `assert_paths_in_session_scope`'s own
    docstring for why a bare directory (or none at all) is not enough.

    Review: staff-eng R3 (2026-08-03, pass 2) — this is a MISTAKE guard, not
    an authentication check: it requires evidence a session was initialized
    through the tracked hot path, which raises the cost of a fabricated
    identity from "invent a string" to "create a directory and a file" for a
    caller who already has repo write access by construction. It does not
    hold against an adversary willing to do that — see
    `assert_paths_in_session_scope`'s own docstring for the corrected claim
    about what this closes (the accidental/naive shape and the F3 live
    incident), not a stronger one.

    Never raises — an unresolvable/unreadable path is "no evidence", the
    fail-closed answer.
    """
    try:
        sdir = core.session_dir(session_id, cwd)
    except Exception:
        return False
    if not sdir or not os.path.isdir(sdir):
        return False
    return os.path.isfile(os.path.join(sdir, "meta.json"))


#: `_classify_denied_path`'s own return values, named as module-level
#: constants (2026-08-04, staff-eng F1 fix) so a caller downstream of this
#: module -- `block_subagent_commit.py`'s `_ownership_leg_summary`, which
#: caps the threaded reason at a fixed byte budget far shorter than these
#: full sentences -- can rely on the DISCRIMINATING word (`orphan`,
#: `include_orphans ignored`, `indeterminate`) surviving truncation by
#: construction, because it is the FIRST word/phrase in the string, not
#: buried after an already-over-budget preamble. Word order only: the
#: classification SEMANTICS these four strings express are unchanged from
#: before this fix (verified against every consumer of
#: `assert_paths_in_session_scope` -- see this module's own history --
#: before landing; none pattern-matches on word order, only on the presence
#: of `"include_orphans ignored"` as a substring).
_CLASSIFICATION_INCLUDE_ORPHANS_IGNORED = (
    "include_orphans ignored — orphan, but this session has no "
    "initialization record"
)
#: The REMEDY half of the two classifications below, appended rather than
#: interleaved (2026-08-31, `state/handoffs/2026-08-30-the-commit-path-scoped-
#: commits-the-share.md` owed item 4: "give the unanswerable refusal a runnable
#: remedy and a message distinguishable from a genuine peer conflict").
#:
#: `docs/wiki/guard-messaging.md` § Register: one fact, once, plus a terse
#: alternative. The fact was already here and is unchanged; what was missing is
#: the alternative. And per this repo's cold-path rule, a remediation names a
#: RUNNABLE SCRIPT, never a slash command -- what fires before a session exists
#: cannot be fixed by the surface that just failed.
#:
#: `session-claim-cli who-claims-path <path>` is that script (a `bin/`
#: forwarder, on PATH). It is the right one specifically because its answer
#: space matches the distinction this classification is about: rc=0 with rows
#: names live/dead holders, rc=0 with NO rows is a determinate "nobody holds
#: it", and rc=1 prints "could not be determined ... NOT a verdict that the
#: path is unclaimed" plus the `abort_cause`. That is exactly the
#: indeterminate-vs-unclaimed split an operator cannot make from the refusal
#: alone, answered by a command rather than by reasoning.
#:
#: TRUNCATION IS EXPECTED AND CORRECT HERE. `block_subagent_commit.py`'s
#: `_ownership_leg_summary` caps the threaded reason at ~70 bytes, so the
#: remedy is cut in that path -- which is why it goes at the END and the
#: discriminating token stays FIRST (see the word-order note above, and
#: `test_indeterminate_call_names_the_degradation` which pins that the
#: capped path still carries "indeterminate"/"adoption withheld"). The
#: remedy is for the reader of the FULL string; the capped reader needs the
#: discriminator, not the command.
_REMEDY_WHO_CLAIMS = " — run: session-claim-cli who-claims-path <path>"

#: Carries the remedy for the same reason its siblings do, and it was the
#: only one of the four without it. The discriminator stays FIRST so the
#: ~70-byte cap in `block_subagent_commit._ownership_leg_summary` keeps it.
#:
#: WHAT THIS DOES AND DOES NOT MEAN, because a dispatched committer reads it
#: as "your writes were rejected" and that is not what it says. It reports
#: that NO touch record in the index covers this path. A dispatched agent's
#: writes are NOT excluded by being an agent's: `claim_index.rebuild()`
#: resolves each agent's claims onto its owning EM session through the
#: `.agents/<agent_id>/em-session-id.txt` back-pointer, so an executor's
#: write and its EM's commit are the SAME claimant by construction. That is
#: the sanctioned attribution path, and it needs nothing at dispatch time.
#:
#: So an orphan verdict on a path an agent demonstrably wrote means the touch
#: record is missing or unattributable, not that attribution was refused --
#: the writer bypassed the Edit/Write hot path (an engine CLI or a shell
#: redirect writes no touch record), or its agent dir carries no resolvable
#: owner, in which case `_agent_owner_sid` contributes NO claims and the
#: path reads exactly as if nobody had touched it. Those two are
#: indistinguishable from this string, which is why the remedy is a command
#: and not a sentence: `who-claims-path` answers determinately-unclaimed
#: (rc=0, no rows) against could-not-determine (rc=1) where reasoning cannot.
#:
#: Asked and answered for an emitted workflow's commit phase, 2026-09-01,
#: cross-repo/archive/2026-08-28-doe-claude-em-workflow-commit-phase-cannot-
#: commit-executor-writes.md.
_CLASSIFICATION_ORPHAN = (
    "orphan — no session holds a claim on it" + _REMEDY_WHO_CLAIMS
)

_CLASSIFICATION_INDETERMINATE = (
    "indeterminate — adoption withheld; this call's claim reads were "
    "degraded (an unreadable peer/agent claim, or an unresolved agent-race "
    "window), so this path's own classification is unresolved, not that it "
    "is unrecognized" + _REMEDY_WHO_CLAIMS
)
#: `compute_offer` is NOT the mechanism any more and has not been since
#: 2026-08-21, when the ownership leg was rebuilt on
#: `claim_index.classify_paths` (see that rebuild's own comment in
#: `assert_paths_in_session_scope`). Naming a retired function in
#: OPERATOR-FACING text sends a reader to grep for something that will not
#: explain their refusal -- the same stale-citation shape the 2026-08-30
#: baton is re-homing elsewhere in this path. States the condition instead,
#: and carries the same runnable remedy: "never classified" and "genuinely
#: unclaimed" are the two readings, and `who-claims-path` is what separates
#: them.
_CLASSIFICATION_UNCLASSIFIED = (
    "unclaimed — no claim record names this path" + _REMEDY_WHO_CLAIMS
)
_CLASSIFICATION_ALREADY_CLEAN = (
    "already clean at HEAD -- nothing to commit; drop it from the pathspec"
)

#: PUBLIC, and a CROSS-MODULE CONTRACT (2026-08-07): the prefix every
#: peer-claimed classification `_classify_denied_path` returns is built from,
#: and the substring a consumer tests to tell "a holder was actually found"
#: apart from every other deny classification above. Named consumer:
#: `coordinator_core.ops.ceremony.scoped_git_commit.
#: _include_orphans_ineffective_note`, which appends prose ASSERTING the
#: denied paths are claimed by another session — an assertion it can only
#: earn by finding this prefix in the already-computed deny reason, since it
#: mints no ownership oracle of its own. That module imports this constant
#: (falling back to the literal, since it must stay importable), so the two
#: modules cannot drift apart by one editing its own copy of the string.
#:
#: Same discipline, different axis, as the `_CLASSIFICATION_*` block above:
#: those exist so the DISCRIMINATING word survives `block_subagent_commit`'s
#: byte-budget truncation by being placed FIRST; this exists so a
#: discriminating substring is guaranteed BY CONSTRUCTION rather than by two
#: modules independently agreeing on a literal. Changing this string is a
#: two-module change — grep for the fallback literal in
#: `scoped_git_commit.py` before touching it.
#:
#: SUBSTRING COLLISION — this prefix ALONE is not a safe membership test.
#: `_CLASSIFICATION_ORPHAN` used to read "orphan — dirty but claimed by no
#: session", which contained this prefix mid-sentence while meaning the exact
#: INVERSE of a holder being found. It no longer does (2026-08-21: the leg
#: stopped reading dirtiness, so asserting it became unearned, and the
#: reworded string happens to drop the collision too) — but the collision is
#: a property of the prefix, not of any one classification string, and the
#: next one written from the same vocabulary reintroduces it. Consumers test
#: :data:`CLAIMED_BY_SENTINELS` below instead; this prefix is for
#: CONSTRUCTION.
CLAIMED_BY_PREFIX = "claimed by "

#: The membership test a consumer actually uses: the full, unambiguous lead
#: of each claimed-by branch, DERIVED from `CLAIMED_BY_PREFIX` rather than
#: re-spelled, so the two cannot drift and a fourth branch built the same way
#: is matched by construction. No sentinel is a substring of any
#: `_CLASSIFICATION_*` string, which is what makes this safe where the bare
#: prefix is not — the collision is designed out rather than argued away by
#: another module's reachability.
CLAIMED_BY_SENTINELS = tuple(
    CLAIMED_BY_PREFIX + suffix for suffix in ("live session ", "session ")
)


def deny_reason_names_a_holder(deny_reason: str) -> bool:
    """True iff *deny_reason* carries a classification that actually FOUND a
    holder — the earned-assertion predicate
    `coordinator_core.ops.ceremony.scoped_git_commit.
    _include_orphans_ineffective_note` gates its prose on, so that module
    tests ownership through this function rather than re-deriving a substring
    rule of its own (and inheriting the `_CLASSIFICATION_ORPHAN` collision
    :data:`CLAIMED_BY_PREFIX` documents).

    Never raises — a non-string reads as "no holder named", the fail-closed
    answer for a caller deciding whether it may assert one.
    """
    if not isinstance(deny_reason, str):
        return False
    return any(sentinel in deny_reason for sentinel in CLAIMED_BY_SENTINELS)


def _classify_denied_path(
    path: str,
    offer: dict,
    all_orphans: list,
    orphan_adoption_requested_but_unverified: bool = False,
    call_indeterminate: bool = False,
    already_clean: bool = False,
    cwd: Optional[str] = None,
) -> str:
    """Name WHY a denied path was denied — distinguishes a live-peer claim
    from an orphan from a path `compute_offer` never classified at all, so a
    caller's deny reason names the classification rather than a bare "outside
    scope" (see :func:`assert_paths_in_session_scope`'s docstring).

    Review: staff-eng F6 — this does not take the ORIGINAL `allow_orphans`
    parameter back: that branch was unreachable (with `allow_orphans` True
    and `orphans` a list, a path in `orphan_set` already `continue`d in the
    caller's loop before reaching here), and stays unreachable. The new
    `orphan_adoption_requested_but_unverified` parameter (staff-eng R3,
    2026-08-03 pass 2) is a DIFFERENT signal — not a re-derivation of
    `allow_orphans` — computed by the caller as `allow_orphans and not
    verified_caller`: true only when the caller explicitly asked for orphan
    adoption AND `_session_has_positive_evidence` failed, i.e. this is a call
    where suppressing the distinction WOULD be silent to the caller (see
    `assert_paths_in_session_scope`'s own docstring, and `scope.touch`'s
    defect-A comment, for why a real session dir with no `meta.json` is a
    documented shape, not a hypothetical).

    Review: staff-eng P3 (2026-08-03, pass 3) — `call_indeterminate` (mirrors
    `offer["indeterminate"]`, i.e. `ScopeResult.indeterminate`) is a THIRD,
    independent signal, checked ahead of the `all_orphans` membership test
    rather than inside it: when R1's whole-call withhold zeroed
    `compute_offer`'s `orphans` (an unreadable peer/agent claim, or an
    unresolved agent-race overlap, THIS call), a path that was a genuine raw
    orphan pre-wipe is no longer a member of `all_orphans` at all — it would
    otherwise fall through to the generic "unclaimed/never classified"
    message below, which reads to an operator as "the system has never heard
    of this path" when the truth is "this call's claim reads were degraded
    and adoption was withheld call-wide". Checked regardless of
    `allow_orphans`/`orphan_adoption_requested_but_unverified` — the
    degradation is real and worth naming even in default (non-adoption)
    mode, unlike the R3 signal, which is specifically about a caller's
    unmet ASK.

    `already_clean` (fifth classification, Half 2 of the mixed-pathspec
    fix — `coordinator_core.ops.ceremony.scoped_git_commit`'s module
    docstring): a FOURTH, independent signal, checked FIRST, ahead of every
    ownership-shaped classification below. A path with nothing left to
    commit at HEAD is not an ownership question at all — `compute_offer`
    structurally cannot classify it (it is invisible to `compute_scope`,
    which only ever enumerates DIRTY paths), so falling through to
    `_CLASSIFICATION_UNCLASSIFIED` names the wrong condition and points an
    operator at the wrong remedy (`include_orphans: true`, which provably
    cannot help — an already-clean path is never a member of `orphans`).
    Threaded from the caller (`assert_paths_in_session_scope`'s own
    `already_clean` kwarg) rather than re-derived here with a second git
    probe — this module has no cleanliness signal of its own by design (see
    this module's own docstring: it composes `compute_offer` only, never
    touches git directly).

    "owned by session" classification — liveness is EARNED, not asserted
    (break-class fix, 2026-08-07): `compute_offer`'s `excluded` entries carry
    OWNERSHIP only ("owned by session <remainder>") — never a liveness claim.
    The word "live" in this function's output must never be a bare
    format-string interpolation of that remainder again; it is produced only
    after a real liveness call confirms it. Three branches, checked in order:

      1. `<remainder>` is a bare session-id token (no whitespace, not the
         literal `unknown`) AND is a member of `live_session_ids(cwd)`
         -> "claimed by live session <sid>" — wording unchanged from before
         this fix, now earned rather than asserted.
      2. `<remainder>` is a bare session-id token AND is confirmed NOT a
         member of that set -> "claimed by session <sid> (holder reads NOT
         live in this repo; NOT a licence to reap ...)".
      3. Anything else -> "claimed by session <remainder> (liveness not
         checked)". This covers TWO distinct shapes, both real per
         `safe_commit_offer.compute_offer`'s degraded-owner reasons: (i)
         `<remainder>` is not a bare token at all — e.g. "unknown (claims
         unreadable: other)" or "unknown (agent race window, no
         em-session-id.txt yet)" — where feeding the non-sid string to the
         liveness oracle would deterministically read as "not live" and
         mint a NEW false assertion (DEAD) of exactly the kind this fix
         removes; and (ii) `resolve_live_session_ids()` itself raised or
         degraded — its documented contract returns an empty frozenset on
         error, indistinguishable from "nothing is live", so an empty
         result here is treated as UNKNOWN, never as DEAD. The oracle call
         is wrapped in `try/except Exception` for the same reason: this
         function sits on a refusal-message path and must never raise.

    THE LIVE SET IS PER-REPO, SO THE ORACLE MUST BE cwd-SCOPED (break-class
    fix, 2026-08-07 pass 2; cross-repo memo
    `2026-08-07-doe-claude-em-scoped-commit-calls-a-live-peer-dead-and-reapable`).
    Session registries live at `<repo>/.git/coordinator-sessions/`, so
    "is <sid> live?" is only answerable relative to a repo. This function
    previously called the ZERO-ARG `coordinator_core.liveness.
    resolve_live_session_ids()`, which resolves its sessions dir from the
    PROCESS cwd — not from the repo whose scope is being asserted. Invoked
    across repos (a sibling's `coordinator-safe-commit` shim reaching this
    engine), every peer session of the TARGET repo is structurally absent
    from the live set, so branch 2 fired on a demonstrably live peer:
    reproduced on 2026-08-07 as `session_live(sid, DoE) is True` while
    `session_live(sid, None) is False` from a claude-klabauter cwd, for a session whose
    `last_activity` was 5 seconds old. Use the cwd-scoped
    `coordinator_core.session.liveness.live_session_ids(cwd)` — the same
    entry point `ops.ceremony.commit_pipeline._absorbed_peer_claims_trailer`
    already established for the mirrored classification there. NEGATIVE
    SPEC: never reintroduce a zero-arg liveness call here; `cwd` is already
    threaded from `assert_paths_in_session_scope` for `compute_offer` and
    `_session_has_positive_evidence`, and the liveness oracle must agree with
    them about WHICH repo it is answering for.

    All three claimed-by branches are BUILT FROM :data:`CLAIMED_BY_PREFIX`
    rather than repeating the literal, so the substring a downstream consumer
    discriminates on is guaranteed by construction — see that constant's own
    comment for the named consumer and why the guarantee has to be structural
    rather than two modules agreeing on a string.

    `offer` is the CLASSIFICATION offer, not the verdict's offer (see this
    module's "TWO OFFERS" paragraph): its `excluded` list is the only thing
    read here, and it can name a holder for a path the caller named but never
    touched. Nothing this function returns feeds back into allow/deny.

    A REFUSAL MUST NOT PRESCRIBE A REMEDY IT CANNOT VOUCH FOR (same memo).
    Branch 2 previously read "claimed by DEAD session <sid> (claim is
    reapable)". To an agent reader the remedy is the actionable half and
    outweighs the refusal itself — acting on it means reaping a peer's claim
    on a shared worktree and committing over their in-flight surface, the
    exact work-loss shape this whole mechanism exists to prevent. Branch 2
    now names only what was observed (the holder does not read live in this
    repo) and explicitly withholds the licence. NEGATIVE SPEC: no branch of
    this function may emit "reapable", or any other takeover instruction,
    from a refusal path.
    """
    if not isinstance(path, str):
        return "not a string"

    if already_clean:
        return _CLASSIFICATION_ALREADY_CLEAN

    for entry in offer.get("excluded") or []:
        if entry.get("path") == path and str(entry.get("reason", "")).startswith(
            "owned by session"
        ):
            remainder = str(entry.get("reason", "")).removeprefix("owned by session ")
            is_bare_sid = bool(remainder) and " " not in remainder and remainder != "unknown"
            if is_bare_sid:
                try:
                    live_sids = live_session_ids(cwd)
                except Exception:
                    live_sids = None
                if live_sids:
                    if remainder in live_sids:
                        return "%slive session %s" % (CLAIMED_BY_PREFIX, remainder)
                    return (
                        "%ssession %s (holder reads NOT live in this "
                        "repo; NOT a licence to reap — re-verify the holder "
                        "before any takeover)" % (CLAIMED_BY_PREFIX, remainder)
                    )
            return "%ssession %s (liveness not checked)" % (CLAIMED_BY_PREFIX, remainder)

    if path in all_orphans:
        if orphan_adoption_requested_but_unverified:
            return _CLASSIFICATION_INCLUDE_ORPHANS_IGNORED
        return _CLASSIFICATION_ORPHAN

    if call_indeterminate:
        return _CLASSIFICATION_INDETERMINATE

    return _CLASSIFICATION_UNCLASSIFIED


@register_op("session.scope_report")
def _handler(params: dict, repo_root=None) -> dict:
    """session.scope_report — read-only report of THIS session's own scope.

    Op scope "none" (mirrors session.record_pickup / session.reap_claims_
    for_repos): the `repo_root` handler arg is unused (always None for
    scope-"none" ops). This op resolves its own target via wire params only.

    params:
      session_id (str, optional): explicit session id. Falls back to
                                   `coordinator_core.session.core.resolve_session_id(cwd)`
                                   (the calling session's own id) when absent.
      cwd        (str, optional): working directory passed through to both
                                   session-id resolution and `compute_offer`.

    Returns, straight from `compute_offer`, and mutates nothing:
      session_id     str
      safe_paths     List[str]
      excluded       List[{"path": str, "reason": str}]
      orphans        List[str]
      indeterminate  bool

    On an unresolvable session_id (no explicit param, and
    `resolve_session_id` returns empty), returns an error envelope instead:
      {"session_id": "", "safe_paths": [], "excluded": [],
       "error": "session_id could not be resolved"}
    — never raises; read-only either way.
    """
    cwd = params.get("cwd")
    session_id = params.get("session_id") or core.resolve_session_id(cwd)
    if not session_id:
        return {
            "session_id": "",
            "safe_paths": [],
            "excluded": [],
            "error": "session_id could not be resolved",
        }

    return compute_offer(session_id, cwd)
