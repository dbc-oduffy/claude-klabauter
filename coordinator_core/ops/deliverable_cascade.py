"""
coordinator_core.ops.deliverable_cascade — JSON-RPC "deliverable.cascade_terminal" operation.

Purpose: the SHARED, addressable cascade mechanism PM rulings R1/R1a call for — a plan
reaching `status: implemented` (or, later, C6b's second trigger: a handoff concluding
terminally-positive) advances every LIVE handoff joined to it by `deliverable_id`, in the
same operation, with zero additional EM-facing steps. This is the ONE implementation both
triggers call; per Addendum Q4, its home is `coordinator_core/ops/` (not a helper inside
`archive_stamp.py` or `plan_status_transition.py`) precisely so C6b (`post_commit_tail`) and
this chunk's trigger (`plan_status_transition.main()`, after a successful non-no-op flip) both
route through ONE addressable op — AC6i's idempotency/no-re-entry guarantee only holds when
there is exactly one entrypoint, not two implementations that happen to agree.

Delivers: AC6, AC6c, AC6e, AC6h, AC6g (row depth composed from the sibling
module `coordinator_core.ops.cascade_baton_rows` — see that module's own
docstring for the row-level join/write mechanism; this module's role in
AC6g is only to call it once per advanced candidate and fold its result
into this op's own response and provenance).
Spec backlink: pln-terminal-state-propagation-giv-c85539 § C6 (R1, R1a)

Join key: `deliverable_id`, exact-string match against each live handoff's own frontmatter
field — NEVER the `plan:` pointer (1 of 80 live handoffs carries it; C12 retires it). No
fork-equivalence canonicalization (unlike `deliverable.rollup`) — C6's body does not call for
it and folding it in here would be undeclared scope creep on a join this plan's own table
names as the one everybody already carries.

Second target kind (C2, this chunk): a CLOSED kind-descriptor (`_KindDescriptor`,
`_KIND_DESCRIPTORS`) parameterises `_collect_live_candidates` over corpus dir,
record reader, lifecycle field/terminal-value set, validator schema, and the
DR-263 predicate-leg policy table. Two kinds are registered: `handoff`
(default, this section's description, byte-for-byte unchanged) and `sizing`
(`state/sizings/*.yaml`, whole-document YAML, `status`/`{"shipped"}` in place
of `deployment_state`/HANDOFF_TERMINAL_DEPLOYMENT). An unregistered kind name
raises (AC5), never degrades to an empty candidate set. This chunk wires the
descriptor and the read side only — C3 owns the sizing kind's per-target
write side, dispatched internally behind this SAME entrypoint (AC4, never a
second `register_op`). See `docs/plans/2026-08-10-sizing-objects-join-the-deliverable-spine.md`
§ C2 for the full spec, including AC6a's unreadable-record handling below.

Candidate surface (handoff kind, unchanged): `state/handoffs/*.md` ONLY (flat, live corpus) — mirrors
`handoff_transition._resolve_path`'s own containment discipline (mutation verbs are live-only;
archived handoffs are out of scope for a mutation verb by long-standing convention, see
docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4). A candidate is
"still advertising its work as live" when its `deployment_state` is NOT a member of
`lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT`.

Per-target predicate (AC6h) — evaluated on EVERY candidate BEFORE any write is attempted,
closing the gap Anti-scope/R1 name: DR-217's discriminator is evaluated on the node that
itself transitions; this cascade fans OUT from a transitioning plan to a query-selected set,
so none of DR-217's three conjuncts is knowable from the plan's own transition — each is read
off the candidate itself, here:
  (a) claimed by a live session? — `claimed_by`/`consumed_by` (dual-vocab) resolved against
      `coordinator_core.liveness.resolve_live_session_ids()`, mirroring the established
      `archive_handoffs._is_terminal` consumed_by-liveness fallback idiom.
  (b) has a live successor/continuation? — composes the existing `handoff.has_live_children`
      resolver in-process (never re-derives reverse-membership) with THIS candidate as the
      query. `referenced=True` (exit_code 0) or an indeterminate/fail-closed read (exit_code 2)
      both refuse — an ambiguous "cannot tell" answer is never treated as a green light.
  (c) is its own work-state consistent with terminal? — `deployment_state` must be one of
      `{"ready_to_fire", "in_flight"}`. `awaiting_gate` is live-but-blocked-on-something-else,
      not "simply idle and safe to close" — it fails this leg and is named, not flipped.

Any leg failing REFUSES the candidate — named in the result's `refused` list, never silently
dropped, and no write of any kind is attempted for it (AC6h — "rather than flipping it").

Fixpoint iteration (chain convergence) — `_handler` re-runs the candidate loop while the
PREVIOUS pass advanced (or found already-advanced) at least one candidate, bounded at
`len(candidates)` passes (a chain of N candidates can never need more than N passes to fully
drain; the bound is a write-safety property, not a nicety). This exists because leg (b) is
evaluated once per candidate per SINGLE pass over the whole batch — on a chain A -> B sharing
one `deliverable_id` where both are live and B names A as predecessor, a single pass judges A
against B while B is still (from disk's point of view) live, refusing A in the same pass B
itself clears and advances. Iterating lets a later pass re-judge A once B is no longer a
candidate.

Re-judging A is NOT enough on its own, however: `handoff.has_live_children`'s own liveness
definition for a referencing child is keyed off that child's `status` frontmatter field
(`archival._is_terminal_or_archived_child`) — `_advance_one` below deliberately leaves `status`
untouched when it flips a candidate's `deployment_state` to `shipped` (see `build_ship_mutate`'s
own "status untouched" contract). Re-reading B fresh off disk on pass 2 therefore does NOT, by
itself, make leg (b) see B as gone — verified empirically: advancing B and then re-querying
`handoff.has_live_children` for A, with no other change, still returns `referenced=True`. Leg
(b) is NOT weakened to fix this (its `exit_code != 0` fail-closed arm, and its behaviour for any
successor NOT advanced by this same run, are both untouched) — instead, `_handler` accumulates
the resolved paths of every candidate THIS RUN has itself advanced (or found already-advanced)
and threads them through `_predicate_refusal`'s `exclude_children_check` param into
`handoff.has_live_children`'s own `exclude` param (an existing, documented scan-set-adjustment
knob, not a new backdoor) on every subsequent pass. This is scoped strictly to paths this same
invocation itself just proved terminal via its own authoritative `deployment_state` write — a
genuinely live successor sitting elsewhere on disk, or a successor some OTHER process advanced
outside this run, is never excluded and leg (b) still refuses on it exactly as before.

Write (only for a candidate that clears all three legs): reuses the SAME established
primitives every other terminal-ship path in this repo already composes — never re-derives
shipped_in resolution or the deployment_state flip:
  1. `archive_stamp.resolve_source_ship_sha(source_path, not_after=advanced_at)`, tried
     FIRST (2026-08-04) — the commit that landed the artifact whose terminal transition
     FIRED this cascade (the plan that reached `status: implemented`, or the terminal
     handoff C6b's trigger names) is honest, already-known evidence, stamped via
     `stamp_shipped_in(kind="ship-commit", sha=<that commit>)`. Only when this yields
     nothing does resolution fall back to:
  2. `archive_stamp.stamp_shipped_in(kind="scope-derived", allow_branch_tip_fallback=False,
     not_after=advanced_at)` — Position A self-derivation from the candidate's own
     `scope:` paths, guarded against an implausible (postdating-the-trigger) match by the
     same `not_after` param. A genuine no-commit-found result from EITHER leg is NOT an
     error (existing established contract) — it REFUSES this candidate (named: "no commit
     evidence resolvable for shipped_in") rather than flipping a handoff to `shipped` with
     no `shipped_in`, which the schema's post-cutoff cross-field rule
     (`_cf_shipped_in_required`) would reject anyway. See `_advance_one`'s own docstring
     for why source-first, never the reverse.
  3. `handoff_transition.build_ship_mutate` — the SAME mutate closure `_ship` itself calls,
     composed here (not re-invented) inside ONE extra `locked_rmw` pass that ALSO stamps this
     cascade's own provenance fields (`advanced_by`, `advanced_at`) atomically with the
     deployment_state flip — AC6e's provenance is written in the SAME write as the flip it
     describes, not a second racing write.
  4. Commit (C2, 2026-08-14): every candidate this run itself advanced (both the handoff and
     sizing per-target writes) is a `locked_rmw` write to the worktree ONLY — steps 1-3 above
     never touch git. `_handler` accumulates the resolved paths of every `advanced` entry and
     commits exactly that set, once, via `git_native.commit_scoped` (`_commit_mutated_paths`),
     before returning. This is the substitute committer the negative-spec below never named:
     an uncommitted terminal write here is simultaneously too-terminal for the closers
     (`promote_shipped_in_flight_stubs`/`handoff.close_origin_stub`, both of which exclude a
     terminal `deployment_state`) and not-terminal-enough for the archiver (the disk/HEAD
     drift guard in `ops/fleet/_common.py` refuses on an uncommitted mutation).

Idempotency/no-re-entry (AC6i): falls out of construction, not extra bookkeeping. A second
invocation over the same join-closure re-scans state/handoffs/ and finds every
previously-advanced candidate now `deployment_state:shipped` — i.e. no longer live — so it is
excluded from the candidate set before the predicate even runs; nothing is re-evaluated, nothing
is re-written. This op never calls itself and never re-invokes the cascade recursively on an
artifact it just advanced — re-entrancy across the TWO triggers (this chunk's plan trigger and
C6b's handoff trigger) is a property of trigger wiring, not of this op, and is C6b's to satisfy
by calling through this same entrypoint rather than duplicating it.

Failure posture (per C6's body, mirroring C2's "pairs_resolved=0 is never silent" discipline):
a cascade that resolves NO downstream artifact — zero candidates matched, OR candidates matched
but every one refused — is the C2 case, not success. `exit_code` is 1 whenever `advanced` is
empty, with `message` naming the deliverable_id and (when candidates existed) every refusal
already present in `refused`. A successful cascade with at least one advanced artifact is
`exit_code: 0` even when SOME candidates were refused — refusal-of-some is a correct, named
outcome per AC6h, not a failure of the cascade as a whole.

Self-registration: importing this module calls register_op("deliverable.cascade_terminal") as
a side-effect. Add this module to coordinator_core/ops/__init__.py's eager-import table to
trigger registration at start_server() time (done in this same chunk).

Negative-spec:
  - Does NOT widen `close_out_and_stamp`'s `stage_paths` — this op's writes are its own,
    reached from `plan_status_transition.main()` directly, never from `close_out_and_stamp`.
  - Does NOT touch `docs/plans/*.md` — the plan whose stamp fired this cascade is read
    (`deliverable_id` is supplied by the caller as a param, never re-derived from the plan file
    here) but never written by this op.
  - Does NOT scan `archive/handoffs/` — mutation targets are live-only (see Candidate surface
    above); a candidate that has already been archived is, by construction, no longer
    advertising its work as live and is outside this op's join surface.
  - Does NOT build a decision-record artifact or any confirmation step (R1a's anchor) — the
    caller's stamp IS the decision; this op is pure bookkeeping fan-out.
  - AC6g's baton-row depth (rows inside a roadmap-baton handoff's own body, joined via
    `_determine_shipped`/`_committed_chunk_ids`) IS implemented, but not IN this module — see
    `coordinator_core.ops.cascade_baton_rows.resolve_baton_rows`, called once per advanced
    candidate below (§ JSON-RPC handler). This module never re-derives that join itself.
  - Does NOT implement AC6f's retraction (C6d) or the second trigger's wiring into
    `post_commit_tail` (C6b) — both are explicitly out of this chunk's scope and are named as
    depending on THIS op's provenance shape (AC6e) rather than re-deriving it.
"""

from __future__ import annotations

import dataclasses
import datetime
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional

import yaml

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.dag import _read_meta
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.liveness import resolve_live_session_ids
from coordinator_core.ops.cascade_baton_rows import resolve_baton_rows
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.ceremony.git_native import commit_scoped
from coordinator_core.ops.fleet._common import main_worktree_root

# Vendored handoff schema path — same file every other handoff-mutating op in this
# package validates against; each mutating module keeps its own local copy of this
# constant by established convention (see e.g. ops/handoff_transition.py) rather than
# importing another module's private symbol.
_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "handoff.schema.json"
)

_LIVE_DEPLOYMENT_STATES = frozenset({"ready_to_fire", "in_flight"})

# Vendored sizing-object schema path — the sizing kind's own validator, mirroring
# `_SCHEMA_PATH` above's per-module-local-copy convention. x-schema-version 1.8.0
# carries `deliverable_id`, the reverse `plan` FK, and `shipped` in `status.enum`
# (vendored C0, docs/plans/2026-08-10-sizing-objects-join-the-deliverable-spine.md).
_SIZING_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)

# Sizing kind's terminal-value set (leg (c) / lifecycle-field parameterisation) —
# `shipped` and `declined` are BOTH terminal (nothing further can advance either
# one); `superseded` means a different fact ("replaced by a later sizing for the
# same intent"), never "this shipped" or "this was declined". `declined` was added
# 2026-08-10 (docs/plans/2026-08-10-a-terminal-status-for-a-declined-sizing.md
# § C2): a declined sizing must be excluded from this cascade's live-candidate
# collection the same way `shipped` already is — it is done, by refusal rather
# than delivery, and this cascade's job (advancing a candidate to `shipped` when
# its deliverable ships) has nothing to do to it. Leaving it out of this set
# would still be caught by leg (c)'s `_SIZING_LIVE_STATUS` check below (refused,
# not silently flipped), but folding it in HERE is the more honest shape: a
# declined sizing was never a live candidate for this cascade in the first
# place, not a live one that happens to fail a downstream leg.
# Review: coordinator:code-reviewer — Finding 5: this file's own hand-copy
# mirror is `coordinator/bin/coordinator-doc-new.py::_SIZING_TERMINAL_STATUSES`
# (plural). Not consolidated (EM-adjudicated) — but a future editor of
# either set should check the other before assuming parity; as of this
# comment they have already drifted (this set includes `declined`, the
# mirror does not).
_SIZING_TERMINAL_STATUS: FrozenSet[str] = frozenset({"shipped", "declined"})

# Review: staff-eng — Finding 1: leg (c)'s POSITIVE live-set for the sizing
# kind. `draft`/`superseded` are excluded deliberately: `draft` has no route
# chosen yet (nothing downstream to be consistent WITH), and `superseded`
# names a different fact than "still live" per `_SIZING_TERMINAL_STATUS`'s
# own comment — flipping either straight to `shipped` on a plan-trigger
# cascade would be exactly the "own work-state not consistent with terminal"
# case leg (c) exists to catch.
_SIZING_LIVE_STATUS: FrozenSet[str] = frozenset({"sized", "routed"})


def _validate_fm(fm_text: str) -> list:
    """Parse fm_text as YAML and validate against the vendored handoff schema.

    Mirrors handoff_transition._validate_fm's contract exactly (own local copy per
    this package's established per-module convention).
    """
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error in frontmatter: {exc}", "hint": ""}]
    return validate_frontmatter(fm_dict, _SCHEMA_PATH)


def _validate_sizing_fm(fm_text: str) -> list:
    """Parse fm_text as YAML and validate against the vendored sizing-object schema.

    Mirrors `_validate_fm`'s contract exactly, against `_SIZING_SCHEMA_PATH`
    instead of `_SCHEMA_PATH` — the sizing kind's own local copy of the same
    per-module validate-before-write discipline every mutating op in this
    package follows (C3).
    """
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error in frontmatter: {exc}", "hint": ""}]
    return validate_frontmatter(fm_dict, _SIZING_SCHEMA_PATH)


def _read_sizing_meta(file_path: str) -> dict:
    """Whole-document YAML reader for `state/sizings/*.yaml` records.

    Unlike `_read_meta` (`coordinator_core.dag`), which parses `---`-delimited
    frontmatter out of a `.md` file and returns `{}` — NOT an error — on a
    whole-document YAML file (see module docstring "Measured in the spike"), a
    sizing-object IS the entire document with no fences. This reader RAISES on
    a parse failure or a non-mapping result rather than swallowing it, so
    `_collect_live_candidates`'s sizing-kind path (AC6a) can route the failure
    into `scan_incomplete`/`unreadable` instead of a silent empty-candidate
    zero — the same silent-zero-match hazard the spike measured, reached by a
    different door (a malformed record, not a wrong `base_dir`).
    """
    text = Path(file_path).read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"sizing-object at {file_path} did not parse to a YAML mapping")
    return parsed


# ---------------------------------------------------------------------------
# Kind descriptor (AC5, AC6, AC6a, AC11) — the CLOSED, per-target-kind shape
# `_collect_live_candidates` (this chunk) and C3's per-kind write sides key
# off. Per the plan's own restated shape (§ "Restating the shape honestly"),
# this is THREE things, not two: the corpus/reader/lifecycle-field shape the
# read side parameterises over, PLUS a predicate-leg POLICY TABLE (AC11) —
# each leg entry drawn from a CLOSED vocabulary, `applies` or
# `exempt(reason: str)` only, never a callable or kind-specific branch logic.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _PredicateLeg:
    """One DR-263 predicate leg's disposition for a kind — closed vocabulary:
    `applies` (evaluated normally) or `exempt(reason)` (short-circuited to
    pass, with the exemption recorded here rather than by silent omission).
    Never a callable, never kind-specific branch logic (per the plan body).
    """

    applies: bool
    reason: str = ""


@dataclasses.dataclass(frozen=True)
class _KindDescriptor:
    """Closed per-kind shape: corpus dir, record reader, lifecycle field,
    terminal-value set, validator schema path, and the DR-263 predicate-leg
    policy table (AC11). `strict_unreadable=False` preserves the handoff
    kind's existing swallow-and-continue read behaviour BYTE-FOR-BYTE (the
    plan body's own bar); `strict_unreadable=True` is the sizing kind's AC6a
    behaviour — an unreadable/unparseable record routes into
    `scan_incomplete`/`unreadable` instead of a clean, silent zero.
    """

    name: str
    corpus_subdir: str  # relative to the worktree root, e.g. "state/handoffs"
    suffix: str  # e.g. ".md" or ".yaml"
    reader: Callable[[str], dict]
    lifecycle_field: str
    terminal_values: FrozenSet[str]
    # Review: staff-eng — Finding 1: leg (c) needs a POSITIVE live-set check,
    # distinct from `terminal_values`. `terminal_values` alone degenerates
    # leg (c) into a re-test of `_collect_live_candidates_for_kind`'s own
    # exclusion filter (which already dropped every terminal record before
    # leg (c) ever runs) — the plan's claim that terminal_values "doubles as
    # leg (c)'s live-set" is false for any kind with intermediate non-live
    # states (e.g. sizing's `superseded`, which is not terminal but is also
    # not live). `live_values` names the states leg (c) treats as still
    # live-and-consistent; anything else refuses.
    live_values: FrozenSet[str]
    schema_path: Path
    strict_unreadable: bool
    predicate_legs: Dict[str, _PredicateLeg]  # keys: "a", "b", "c"


_HANDOFF_KIND = _KindDescriptor(
    name="handoff",
    corpus_subdir="state/handoffs",
    suffix=".md",
    reader=lambda p: _read_meta(p),
    lifecycle_field="deployment_state",
    terminal_values=HANDOFF_TERMINAL_DEPLOYMENT,
    live_values=_LIVE_DEPLOYMENT_STATES,
    schema_path=_SCHEMA_PATH,
    strict_unreadable=False,
    predicate_legs={
        "a": _PredicateLeg(applies=True),
        "b": _PredicateLeg(applies=True),
        "c": _PredicateLeg(applies=True),
    },
)

_SIZING_KIND = _KindDescriptor(
    name="sizing",
    corpus_subdir="state/sizings",
    suffix=".yaml",
    reader=_read_sizing_meta,
    lifecycle_field="status",
    terminal_values=_SIZING_TERMINAL_STATUS,
    live_values=_SIZING_LIVE_STATUS,
    schema_path=_SIZING_SCHEMA_PATH,
    strict_unreadable=True,
    predicate_legs={
        "a": _PredicateLeg(applies=True),
        "b": _PredicateLeg(
            applies=False,
            reason=(
                "no successor-edge vocabulary reaches a sizing-object — grepped "
                "handoff_children.py and dag.py for any edge kind whose source or "
                "target is a sizing-object: none exists. A sizing-object's only "
                "recorded downstream relationship is the `plan` FK (C4), which "
                "DR-263's join-closure leg (b) hazard does not analogue onto."
            ),
        ),
        "c": _PredicateLeg(applies=True),
    },
)

#: Closed registry — the only two target kinds this cascade knows. AC5: an
#: unknown kind is a fail-loud error (`_kind_descriptor` raises), never a
#: silent skip or an empty candidate list.
_KIND_DESCRIPTORS: Dict[str, _KindDescriptor] = {
    "handoff": _HANDOFF_KIND,
    "sizing": _SIZING_KIND,
}


def _kind_descriptor(name: str) -> _KindDescriptor:
    """Resolve a target-kind name to its descriptor. Raises ValueError, never
    returns a default or an empty descriptor, on an unregistered name (AC5).
    """
    try:
        return _KIND_DESCRIPTORS[name]
    except KeyError:
        raise ValueError(
            f"deliverable.cascade_terminal: unknown target kind {name!r} — "
            f"registered kinds: {sorted(_KIND_DESCRIPTORS)}"
        ) from None


def _iso_now() -> str:
    """UTC ISO-8601 timestamp with a literal 'Z' suffix (no external tz dependency)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _claimant(candidate_path: Path, repo_root: Path) -> Optional[str]:
    """Ledger-first (DR-ledger-authoritative) read of the handoff's current claim
    holder, if any.

    Routes through `coordinator_core.claim_state.resolve_claim_state` rather than
    reading the tracked-frontmatter mirror directly — a desynced mirror (branch
    switch reverted `claimed_by`/`consumed_by` while the branch-independent claim
    ledger still holds a live claim) must never read as "unclaimed" here: this
    leg's write TERMINALIZES the candidate, so a false "no claimant" is the
    highest-severity failure mode of the seven C6a sites.

    `repo_root` here is the git COMMON dir (see `_handler`'s own precondition
    docstring) — passed straight through as `resolve_claim_state`'s `common_dir`
    to skip a redundant `git_common_dir` resolution on this hot predicate path.
    """
    return resolve_claim_state(candidate_path, common_dir=repo_root).holder


# ---------------------------------------------------------------------------
# Candidate collection — state/handoffs/*.md only (live, flat; see module docstring)
# ---------------------------------------------------------------------------


def _collect_live_candidates(worktree_root: Path, deliverable_id: str) -> tuple[List[dict], bool]:
    """Byte-for-byte-compatible 2-tuple wrapper over `_collect_live_candidates_for_kind`,
    fixed to the handoff kind (this function's own pre-existing, only behaviour before
    this chunk). Preserved under its original name/signature because
    `coordinator_core.ops.cascade_backstop_sweep` — outside this chunk's scope — calls
    it directly and unpacks exactly two values; the parameterised (AC5/AC6/AC6a) form
    lives under a new name below so that caller is unaffected by this chunk.
    """
    matches, scan_incomplete, _unreadable = _collect_live_candidates_for_kind(
        worktree_root, deliverable_id, kind=_HANDOFF_KIND
    )
    return matches, scan_incomplete


def _collect_live_candidates_for_kind(
    worktree_root: Path, deliverable_id: str, kind: _KindDescriptor = _HANDOFF_KIND
) -> tuple[List[dict], bool, List[dict]]:
    """Return ([{path, fm}, ...], scan_incomplete, unreadable) for every LIVE
    candidate of `kind` whose own `deliverable_id` field exact-matches the
    query, and whose lifecycle field (`kind.lifecycle_field`) is NOT a member
    of `kind.terminal_values` (i.e. still advertises its work as live).

    Parameterised over the kind descriptor (AC5/AC6/AC6a) — corpus dir,
    filename suffix, record reader, and lifecycle field/terminal-value set
    all come from `kind`. The default (`_HANDOFF_KIND`) reproduces the
    pre-existing handoff-only behaviour BYTE-FOR-BYTE: every existing caller
    that omits `kind` is unaffected by this parameterisation.

    scan_incomplete=True means the corpus directory could not be fully
    enumerated — because it does not exist (2026-08-10 fix: a nonexistent
    `base_dir` is no longer indistinguishable from a corpus that exists and
    legitimately has zero matches; a bad/misresolved `worktree_root` — e.g.
    the `main_worktree_root` drive-root-misresolution defect — must never
    read back as a clean, complete empty scan) or because enumeration raised
    (permission denied), OR — for a kind with `strict_unreadable=True` (the
    sizing kind) — at least one record in the corpus failed to read/parse.
    Either way the caller MUST treat this as "candidates may be missing,"
    never as "this is the complete set."

    unreadable is `[{"path": ..., "reason": ...}, ...]` for every record a
    `strict_unreadable=True` kind failed to read — AC6a's named list, kept
    distinct from a legitimate empty match set. For `strict_unreadable=False`
    (handoff), this is always `[]` and an unreadable/malformed record is
    silently dropped exactly as before (unchanged handoff behaviour).
    """
    base_dir = worktree_root / Path(kind.corpus_subdir)
    matches: List[dict] = []
    unreadable: List[dict] = []
    if not base_dir.is_dir():
        return matches, True, unreadable

    try:
        entries = list(base_dir.iterdir())
    except OSError:
        return matches, True, unreadable

    for path in entries:
        if path.suffix != kind.suffix or not path.is_file():
            continue
        try:
            fm = kind.reader(str(path))
        except Exception as exc:  # noqa: BLE001 — quarantine an unreadable/malformed record
            if kind.strict_unreadable:
                unreadable.append({"path": str(path), "reason": str(exc)})
            continue
        if not fm:
            if kind.strict_unreadable:
                unreadable.append({"path": str(path), "reason": "empty or unparseable record"})
            continue
        artifact_did = fm.get("deliverable_id")
        if not isinstance(artifact_did, str) or artifact_did.strip() != deliverable_id:
            continue
        lifecycle_value = fm.get(kind.lifecycle_field)
        if lifecycle_value in kind.terminal_values:
            continue
        matches.append({"path": path, "fm": fm})

    scan_incomplete = bool(unreadable)
    return matches, scan_incomplete, unreadable


# ---------------------------------------------------------------------------
# AC6h per-target predicate — refuse-and-name, never silently flip
# ---------------------------------------------------------------------------


async def _predicate_refusal(
    candidate_path: Path,
    fm: dict,
    repo_root: Path,
    exclude_children_check: Optional[List[str]] = None,
    kind: _KindDescriptor = _HANDOFF_KIND,
) -> Optional[str]:
    """Evaluate the DR-263 three-legged per-target predicate, per `kind`'s own
    predicate-leg policy table (AC11). Returns a human-readable refusal reason,
    or None when the candidate clears every APPLIES leg (an EXEMPT leg always
    short-circuits to pass — recorded via its `_PredicateLeg.reason`, never a
    silent skip) and is safe to advance.

    `kind` defaults to `_HANDOFF_KIND` so `cascade_backstop_sweep.py`'s
    existing 3-positional-arg call site (which never supplies `kind`) is
    byte-for-byte unaffected — leg (c) below takes the pre-existing
    `deployment_state`/`_LIVE_DEPLOYMENT_STATES` branch for that kind
    specifically, reproducing the handoff kind's prior behaviour exactly
    rather than re-deriving it from the generic `terminal_values` set (which
    is coarser: it would not distinguish `awaiting_gate` from a genuinely
    terminal state the way the handoff-specific check does).

    exclude_children_check (keyword-only by convention, positional-compatible for
    the two existing 3-positional-arg callers -- cascade_backstop_sweep.py and this
    module's own single-pass-era call sites): paths (any absolute-path spelling;
    resolved internally before comparison) to drop from leg (b)'s live-successor
    scan. THIS module's `_handler` is the only caller that ever supplies a non-empty
    value -- see its own docstring/module docstring "Fixpoint iteration" note for
    why. `cascade_backstop_sweep.py` never passes this (report-only, single pass,
    no candidate of its own scan has been written yet -- nothing to exclude), and
    its omission is exactly the pre-existing default (None -> no exclusion, byte-
    identical to this function's prior behaviour).
    """
    # Leg (c) — own work-state consistent with live-and-advanceable. Review:
    # staff-eng — Finding 1: a POSITIVE check against `kind.live_values` for
    # every kind, uniformly — the prior `kind is _HANDOFF_KIND` branch made
    # this leg a no-op for sizing (re-testing `terminal_values`, which the
    # collector already excluded upstream), so a `superseded` sizing cleared
    # leg (c) and was flipped straight to `shipped`.
    leg_c = kind.predicate_legs["c"]
    if leg_c.applies:
        lifecycle_value = fm.get(kind.lifecycle_field)
        if lifecycle_value not in kind.live_values:
            # Review: coordinator:code-reviewer — unifying the two kinds'
            # branches into one POSITIVE `live_values` check (Finding 1)
            # collapsed the handoff-specific wording into a single generic
            # string, losing operator-facing signal the handoff kind used to
            # carry (live-but-blocked reads differently from already-shipped).
            # That collapse was never required by Finding 1 — restored here,
            # kind-specific wording only, predicate logic unchanged.
            if kind is _HANDOFF_KIND:
                return (
                    f"own work-state ({kind.lifecycle_field}={lifecycle_value!r}) is not "
                    "consistent with terminal — live-but-blocked-on-something-else, not simply idle"
                )
            # `already terminal` was inaccurate for the not-live-but-not-terminal
            # values (`superseded` is neither in `_SIZING_TERMINAL_STATUS` nor in
            # `live_values`), so the message asserted a lifecycle fact that was
            # false for exactly the case leg (c) exists to catch.
            return (
                f"own work-state ({kind.lifecycle_field}={lifecycle_value!r}) is not "
                f"live-and-advanceable — advanceable from "
                f"{sorted(kind.live_values)}"
            )

    # Leg (a) — claimed by a live session. Ledger-first (see _claimant) — a
    # desynced mirror never reads as "unclaimed" here.
    leg_a = kind.predicate_legs["a"]
    if leg_a.applies:
        claimant = _claimant(candidate_path, repo_root)
        if claimant is not None:
            live_sids = resolve_live_session_ids()
            if claimant in live_sids:
                return f"claimed by live session {claimant!r} — refusing to advance out from under it"

    # Leg (b) — has a live successor/continuation. EXEMPT for a kind whose
    # descriptor names no successor-edge vocabulary (sizing) — short-circuits
    # to pass, per the recorded `reason`, never a silent omission.
    leg_b = kind.predicate_legs["b"]
    if leg_b.applies:
        # Function-local import: composes the existing resolver rather than re-deriving
        # reverse-membership; mirrors ops/handoff_children.py's own function-local-import
        # discipline note for a sibling case (avoid pulling its transitive import set into
        # every eager-registration pass just for a defensive edge that today is acyclic).
        from coordinator_core.ops.handoff_children import CONCLUSION_EDGE_KINDS, _handoff_has_live_children

        # This leg is conclusion-shaped, not archival-shaped: the write it gates is
        # `deployment_state -> shipped` with `status` untouched and NO file move
        # (`build_ship_mutate`'s "status untouched" contract, above). Nothing is
        # archived here, so no origin pointer can be stranded — see
        # dag.CONTINUATION_EDGE_KINDS for the general archival-vs-conclusion
        # rationale (example-cockpit-repo-em, 2026-08-05, cross-repo/inbox/2026-08-05-
        # example-cockpit-repo-em-wsc-leg-b-counts-spinoffs-as-live-children.md).
        # Narrowed here explicitly rather than taking the op's archival-shaped
        # default (`_DEFAULT_EDGE_KINDS`).
        children_params: Dict[str, Any] = {
            "candidate": str(candidate_path),
            "edge_kinds": CONCLUSION_EDGE_KINDS,
        }
        if exclude_children_check:
            children_params["exclude"] = list(exclude_children_check)
        children_result = await _handoff_has_live_children(children_params, repo_root)
        children_exit = children_result.get("exit_code")
        if children_exit == 0:
            children = children_result.get("children") or []
            return f"has a live successor/continuation (referenced by: {children})"
        if children_exit != 1:
            # 2 (indeterminate) or any other unexpected value — fail-closed, never
            # treat "cannot tell" as a green light.
            return (
                "live-successor check indeterminate: "
                f"{children_result.get('error', 'unknown error')} — refusing (fail-closed)"
            )

    return None


# ---------------------------------------------------------------------------
# Write — shipped_in stamp + deployment_state flip + AC6e provenance, one locked_rmw
# ---------------------------------------------------------------------------


def _current_shipped_in(candidate_path: Path) -> Optional[str]:
    """Read the on-disk `shipped_in` value straight off disk, or None (absent/unreadable)."""
    try:
        text = candidate_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    value = read_fm_field(split.fm_text, "shipped_in")
    return value if value not in (None, "null", "") else None


#: `_advance_one`'s own marker string for the benign-race/idempotency-floor
#: outcome — distinct from a genuine per-target refusal reason, so
#: `_handler` can route it to `already_advanced` rather than `refused`.
#: Review: coordinator:code-reviewer Finding 4.
_ALREADY_ADVANCED_MARKER = "already advanced (idempotent no-op)"


def _advance_one(
    candidate_path: Path,
    deliverable_id: str,
    advanced_at: str,
    repo_root: Path,
    source_path: str = "",
    source_kind: str = "",
) -> tuple[bool, Optional[str]]:
    """Attempt to advance a single candidate that has already cleared the AC6h predicate.

    Returns (advanced, refusal_reason). Never raises — every failure mode is folded into
    a named refusal reason instead.

    shipped_in evidence priority (2026-08-04, revised 2026-08-14 — see
    docs/plans/2026-08-14-cascade-ship-evidence-and-write-durability.md § C1): Position 1
    (`archive_stamp.resolve_source_ship_sha(source_path, ...)`, kind `"ship-commit"`) is
    trustworthy ONLY on the handoff trigger (`source_kind == "handoff"`), where
    `source_path` is the handoff that itself concluded terminally-positive and "what last
    touched it" is the proxy `_advance_one`'s original contract was built around. On the
    plan trigger (`source_kind == "plan"`), `source_path` is the plan document that
    `plan_status_transition._commit_plan_flip` commits immediately before calling this
    cascade — Position 1 would then resolve the caller's own bookkeeping flip commit as
    ship evidence, which neither of `resolve_source_ship_sha`'s own guards can catch
    (the flip commit both touches `source_path` most-recently AND predates
    `advanced_at`). So the plan trigger skips Position 1 entirely and goes straight to
    Position A — scope-derived self-derivation from the CANDIDATE's own `scope:` paths
    (`kind="scope-derived"`, `allow_branch_tip_fallback=False`, `not_after=advanced_at`).
    A genuine no-commit-found result from either position is NOT an error (established
    contract) — it REFUSES this candidate (named: "no commit evidence resolvable for
    shipped_in") rather than flipping a handoff to `shipped` with no `shipped_in` or with
    a proxy commit that never actually shipped it.
    """
    from coordinator_core.archive_stamp import resolve_source_ship_sha, stamp_shipped_in

    source_sha = (
        resolve_source_ship_sha(
            source_path, not_after=advanced_at, worktree=main_worktree_root(repo_root)
        )
        if source_kind == "handoff"
        else None
    )
    if source_sha:
        outcome = stamp_shipped_in(
            str(candidate_path),
            kind="ship-commit",
            sha=source_sha,
        )
    else:
        outcome = stamp_shipped_in(
            str(candidate_path),
            kind="scope-derived",
            allow_branch_tip_fallback=False,
            not_after=advanced_at,
        )
    if outcome.exit_code != 0:
        return False, f"stamp_shipped_in failed: {outcome.error}"

    # Position A: a genuine no-commit-found result is an honest non-error that still
    # leaves shipped_in unset — the schema's post-cutoff cross-field rule
    # (_cf_shipped_in_required) requires shipped_in whenever deployment_state:shipped
    # is written, so an unresolved shipped_in here means the flip below cannot proceed.
    if not _current_shipped_in(candidate_path):
        return False, (
            "no commit evidence resolvable for shipped_in (source-derived: no evidence "
            "from source_path; scope-derived Position A: no branch-tip fallback) — "
            "deferred, not flipped"
        )

    _state: Dict[str, Any] = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        from coordinator_core.ops.handoff_transition import build_ship_mutate

        ship_mutate, ship_state = build_ship_mutate(str(candidate_path))
        after_ship = ship_mutate(old_text)
        if after_ship == old_text:
            # Already shipped (idempotency floor) — nothing to advance or provenance-stamp.
            _state["applied"] = False
            _state["message"] = ship_state["message"]
            return old_text

        split = split_frontmatter(after_ship)
        if split is None:
            raise MutateAbort(f"advance: no parseable YAML frontmatter after ship mutation for {candidate_path}")
        fm_text = split.fm_text

        # AC6e provenance — inserted (never left absent) in the SAME write as the flip
        # it describes, per this module's docstring.
        if read_fm_field(fm_text, "advanced_by") is not None:
            fm_text = replace_fm_field(fm_text, "advanced_by", deliverable_id)
        else:
            fm_text = insert_fm_field(fm_text, "advanced_by", deliverable_id, "deployment_state")
        if read_fm_field(fm_text, "advanced_at") is not None:
            fm_text = replace_fm_field(fm_text, "advanced_at", advanced_at)
        else:
            fm_text = insert_fm_field(fm_text, "advanced_at", advanced_at, "advanced_by")

        errors = _validate_fm(fm_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"advance: post-mutation schema validation failed: {details}")

        _state["applied"] = True
        _state["message"] = f"advanced {candidate_path} (deployment_state: shipped, advanced_by: {deliverable_id})"
        return rebuild(split, fm_text)

    try:
        locked_rmw(candidate_path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return False, f"advance: handoff not found: {candidate_path}"
    except LockTimeout as exc:
        return False, f"advance: timed out waiting for file lock on {candidate_path}: {exc}"
    except MutateAbort as exc:
        return False, (exc.args[0] if exc.args else "advance: mutation aborted")

    return bool(_state["applied"]), None if _state["applied"] else _ALREADY_ADVANCED_MARKER


def _advance_one_sizing(
    candidate_path: Path,
    plan_path: str,
    repo_root: Path,
) -> tuple[bool, Optional[str]]:
    """Per-kind mutate for the sizing kind (C3) — sibling to `_advance_one`,
    dispatched internally behind the SAME `deliverable.cascade_terminal`
    entrypoint (AC4, never a second `register_op`). Writes `status: shipped`
    plus the `plan` FK, and ONLY those two fields.

    Deliberately does NOT write `shipped_in`, `advanced_by`, or `advanced_at`
    — those are handoff-shaped ship-commit provenance
    (`_cf_shipped_in_required` has no sizing-schema analogue) — and
    deliberately does NOT route through `_advance_one`/`build_ship_mutate`,
    which compose `resolve_source_ship_sha` and REFUSE the flip when no
    ship-commit evidence resolves (correct for a handoff, meaningless for a
    sizing-object that has no `shipped_in` field at all). See module
    docstring / plan § C3.

    Returns (advanced, refusal_reason), mirroring `_advance_one`'s contract.
    Idempotent by construction (AC3 layer-2, inherited not automatic — plan
    § "Idempotency must be inherited, not assumed"): `mutate` returns
    `old_text` UNCHANGED, byte-for-byte, whenever the record's own `status`
    is already a member of the sizing kind's terminal-value set, which is
    exactly what makes `locked_rmw` short-circuit to the same idempotency
    floor `_advance_one` gives handoffs.
    """
    _state: Dict[str, Any] = {"applied": False}

    def mutate(old_text: str) -> str:
        # A sizing-object is whole-document YAML with no `---` frontmatter
        # fences (see `_read_sizing_meta`'s own docstring) — `split_frontmatter`
        # returns None for it. The regex-based fm-field primitives operate on
        # any block of `key: value` text, not specifically a frontmatter
        # fence's interior, so the entire document IS the "fm text" here.
        split = split_frontmatter(old_text)
        whole_doc = split is None
        fm_text = old_text if whole_doc else split.fm_text

        # Review: staff-eng — Finding 0: the idempotency-floor comparison is
        # against an unquoted in-memory value (_SIZING_TERMINAL_STATUS), so it
        # must read through read_fm_field_unquoted (per that function's own
        # documented use rule) rather than read_fm_field's raw on-disk bytes —
        # otherwise `status: 'shipped'` or a trailing-comment-bearing
        # `status: shipped  # ...` misses the set and is spuriously rewritten.
        current_status = read_fm_field_unquoted(fm_text, "status")
        if current_status in _SIZING_TERMINAL_STATUS:
            # Already terminal — idempotency floor (AC3 layer-2). Byte-identical
            # no-op, same contract as _advance_one's "already shipped" arm.
            _state["applied"] = False
            return old_text

        if current_status is None:
            raise MutateAbort(f"advance: sizing at {candidate_path} has no 'status' field")
        fm_text = replace_fm_field(fm_text, "status", "shipped")

        # Review: staff-eng — Finding 8 (EM-adjudicated policy): this cascade
        # write holds the terminal fact for the `plan` FK and OVERWRITES an
        # existing, differing value without a collision guard — the opposite
        # policy from coordinator-doc-new::_mutate_sizing_reverse_edge, which
        # raises MutateAbort on a differing existing value. The asymmetry is
        # deliberate, not accidental: this cascade wins because it fires from
        # the terminal (plan reached status: implemented) event, so a stale
        # or provisional FK written earlier by the reverse-edge path must
        # yield to it. See _mutate_sizing_reverse_edge's own docstring for
        # the mirror statement of this same policy.
        if plan_path:
            if read_fm_field(fm_text, "plan") is not None:
                fm_text = replace_fm_field(fm_text, "plan", plan_path)
            else:
                fm_text = insert_fm_field(fm_text, "plan", plan_path, "status")

        errors = _validate_sizing_fm(fm_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"advance: post-mutation schema validation failed: {details}")

        _state["applied"] = True
        return fm_text if whole_doc else rebuild(split, fm_text)

    try:
        locked_rmw(candidate_path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return False, f"advance: sizing not found: {candidate_path}"
    except LockTimeout as exc:
        return False, f"advance: timed out waiting for file lock on {candidate_path}: {exc}"
    except MutateAbort as exc:
        return False, (exc.args[0] if exc.args else "advance: mutation aborted")

    return bool(_state["applied"]), None if _state["applied"] else _ALREADY_ADVANCED_MARKER


# ---------------------------------------------------------------------------
# Commit (C2, 2026-08-14) — this op's own writes get a named committer: itself.
# ---------------------------------------------------------------------------


def _compose_cascade_commit_message(deliverable_id: str, mutated_paths: List[str]) -> str:
    """Commit message for the cascade's own follow-up commit -- names the
    deliverable and every path it advanced, mirroring
    `post_commit_tail._compose_origin_stub_close_message`'s shape (subject
    line + a bulleted path list) rather than inventing a new one.
    """
    lines = [
        f"deliverable.cascade_terminal: advance {len(mutated_paths)} candidate(s) "
        f"for deliverable_id={deliverable_id}",
        "",
    ]
    for p in mutated_paths:
        lines.append(f"- {p}")
    return "\n".join(lines) + "\n"


def _commit_mutated_paths(
    mutated_paths: List[str], worktree_root: Path, deliverable_id: str
) -> Optional[str]:
    """Commit exactly `mutated_paths` via `git_native.commit_scoped` -- the
    substitute committer this op's own negative-spec never named (see module
    docstring "Negative-spec" and
    docs/plans/2026-08-14-cascade-ship-evidence-and-write-durability.md § C2).

    Never `git add -A`/`.`/`-a` -- `commit_scoped` is the computed-mechanism
    selector every other scoped follow-up commit in this package already
    routes through (`post_commit_tail._commit_and_push_origin_stub_close`,
    `consumed_handoff_stamp`), and it fails loud on an empty or
    directory-shaped pathspec rather than silently widening it.

    Returns None on a landed commit, or a human-readable error string on a
    commit failure -- the caller folds a non-None return into the result's
    `commit_error` field (AC8: a commit failure must surface, never be
    swallowed) without touching `exit_code`, which stays keyed off `advanced`
    alone per this chunk's own hard constraint.
    """
    message = _compose_cascade_commit_message(deliverable_id, mutated_paths)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(message)
        msg_path = fh.name
    try:
        commit_result = commit_scoped(mutated_paths, msg_path, worktree_root)
    finally:
        try:
            Path(msg_path).unlink()
        except OSError:
            pass
    if not commit_result.ok:
        return f"deliverable.cascade_terminal: commit failed: {commit_result.stderr}"
    return None


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("deliverable.cascade_terminal")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "deliverable.cascade_terminal" handler — the shared cascade entrypoint.

    Precondition on `repo_root`: this MUST be the git COMMON dir (the directory
    ending in `.git`), never the worktree root — `main_worktree_root(repo_root)`
    below takes `.parent`, which only derives the correct worktree root when
    `repo_root` IS the common dir. The router's own JSON-RPC dispatch supplies
    this correctly (C1b-ii convention, see `handoff_children._handoff_has_live_children`'s
    identical precondition note); a caller invoking this handler in-process directly
    (as `plan_status_transition._run_cascade` does) must resolve and pass the common
    dir itself, NOT a raw worktree/repo root — passing the wrong one does not raise,
    it silently scans `<repo_root>.parent/state/handoffs` and finds nothing (a real,
    previously-observed false negative; see `plan_status_transition`'s own
    `git_common_dir`-named binding and its rename note).

    Required params:
        deliverable_id (str) — the join key. Every live handoff whose own frontmatter
                                `deliverable_id` exact-matches this value is a candidate.
        source_kind    (str) — "plan" | "handoff" — the kind of terminal node that fired
                                this cascade. Purely a provenance/labeling input in THIS
                                chunk (only the plan trigger is wired); C6b wires the
                                "handoff" trigger through this SAME entrypoint.
        source_path    (str) — path of the terminal node that fired the cascade. Excluded
                                from its own candidate set when it happens to itself be a
                                live handoff (source_kind == "handoff") — self-advance
                                guard, needed by C6b, harmless no-op for C6's plan trigger.

    Optional params:
        at (str) — ISO-8601 timestamp for `advanced_at`. Defaults to now (UTC) when absent.
        target_kind (str) — which kind descriptor's corpus to scan (AC5). Defaults to
                             "handoff" (the pre-existing, only behaviour before this chunk),
                             so every existing caller that omits this param is unaffected.
                             An unregistered value RAISES (fail-loud, AC5) rather than
                             returning an empty candidate set — see `_kind_descriptor`.
                             This chunk (C2) wires the kind descriptor and read side only;
                             C3 owns the per-kind write side this selects into.

    Returns:
        {
          "exit_code": 0 | 1,
          "deliverable_id": ..., "source_kind": ..., "source_path": ...,
          "candidates_matched": <int>,
          "advanced": [{"handoff_path": ..., "message": ...,
                        "baton_rows_advanced": [{"row_id": ..., "message": ...}, ...],
                        "baton_rows_unresolved": [{"row_id": ..., "reason": ...}, ...],
                        "baton_rows_error": <str, present only on a genuine row-resolution
                                             failure -- see cascade_baton_rows.py>}, ...],
          "refused":  [{"handoff_path": ..., "reason": ...}, ...],
          "already_advanced": [{"handoff_path": ..., "reason": ...}, ...],
          "scan_incomplete": <bool>,
          "unreadable": [{"path": ..., "reason": ...}, ...],  # AC6a — only populated
                                                                 # for a strict_unreadable
                                                                 # kind (sizing); always
                                                                 # [] for handoff.
          "error": <str, present iff exit_code==1>,
          "commit_error": <str, present iff the follow-up commit of this run's own
                           mutated paths failed -- see "Commit" below. Independent of
                           exit_code (AC8: a commit failure surfaces without being
                           swallowed, but does not override the advanced-artifact
                           success signal).>,
        }

    Commit (C2, 2026-08-14): every path this run itself mutated -- every `advanced`
    entry, on both the handoff and sizing kinds -- is committed by this op, in ONE
    follow-up commit scoped to exactly that path list, before this handler returns
    (see `_commit_mutated_paths`). Zero advanced candidates -> zero mutated paths ->
    no commit attempted, no error. A refused or already-advanced candidate
    contributes no path -- `already_advanced` names a candidate this run did NOT
    itself write (see its own docstring below), so it is excluded from the commit
    scope the same way a `refused` candidate is.

    Failure posture (see module docstring): exit_code is 1 whenever `advanced` is empty —
    zero candidates matched, or every candidate matched was refused — mirroring C2's
    "pairs_resolved=0 is never silent" discipline. Every per-target refusal is named in
    `refused` regardless of the aggregate outcome (AC6h).

    `already_advanced` is a DISTINCT list from `refused` (Review: coordinator:code-reviewer
    -- a candidate that cleared the AC6h predicate but turned out already-shipped by the
    time the write lock was acquired, e.g. a genuinely concurrent second trigger racing
    the identical join-closure, is a benign idempotency outcome, not a genuine AC6h
    predicate refusal — folding it into `refused` made the two indistinguishable to a
    caller reading AC6e's provenance/explainability contract).
    """
    import asyncio  # deferred: see _advance_one call site below for why it's needed

    deliverable_id: str = (params.get("deliverable_id") or "").strip()
    source_kind: str = (params.get("source_kind") or "").strip()
    source_path: str = (params.get("source_path") or "").strip()
    at: str = (params.get("at") or "").strip() or _iso_now()
    target_kind_name: str = (params.get("target_kind") or "").strip() or "handoff"
    # Fail-loud on an unregistered kind (AC5) — never caught here, propagates
    # as a genuine exception rather than degrading to an empty candidate set.
    target_kind: _KindDescriptor = _kind_descriptor(target_kind_name)

    if not deliverable_id:
        return {
            "exit_code": 1,
            "deliverable_id": deliverable_id,
            "source_kind": source_kind,
            "source_path": source_path,
            "candidates_matched": 0,
            "advanced": [],
            "refused": [],
            "already_advanced": [],
            "scan_incomplete": False,
            "unreadable": [],
            "error": "deliverable.cascade_terminal: 'deliverable_id' is required",
        }
    # Fail-loud on an unenumerated source_kind (Review: coordinator:code-reviewer
    # -- an unvalidated value silently fell into `_advance_one`'s `== "handoff"`
    # gate's else-branch, taking the plan-trigger path and losing Position 1 for
    # any caller that ever passed something other than the exact literal
    # "handoff"). Both current callers pass hardcoded literals, so this is
    # dormant risk today, not a live bug -- validated here so a future third
    # caller errors instead of quietly losing ship evidence.
    if source_kind not in ("plan", "handoff"):
        return {
            "exit_code": 1,
            "deliverable_id": deliverable_id,
            "source_kind": source_kind,
            "source_path": source_path,
            "candidates_matched": 0,
            "advanced": [],
            "refused": [],
            "already_advanced": [],
            "scan_incomplete": False,
            "unreadable": [],
            "error": (
                "deliverable.cascade_terminal: 'source_kind' must be 'plan' or "
                f"'handoff', got {source_kind!r}"
            ),
        }
    if repo_root is None:
        return {
            "exit_code": 1,
            "deliverable_id": deliverable_id,
            "source_kind": source_kind,
            "source_path": source_path,
            "candidates_matched": 0,
            "advanced": [],
            "refused": [],
            "already_advanced": [],
            "scan_incomplete": False,
            "unreadable": [],
            "error": "deliverable.cascade_terminal: repo_root is required (no founding root available)",
        }

    worktree_root = main_worktree_root(repo_root)

    candidates, scan_incomplete, unreadable = _collect_live_candidates_for_kind(
        worktree_root, deliverable_id, kind=target_kind
    )

    # Sizing kind's write side (C3) writes the `plan` FK alongside `status:
    # shipped` — normalized worktree-relative posix, matching the vendored
    # schema's `^docs/plans/.+\.md$` pattern, regardless of how `source_path`
    # arrived (absolute, OS-native-separator, or already relative).
    sizing_plan_fk = ""
    # Review: staff-eng — Finding 4: an unresolvable `source_path` (out-of-
    # worktree mount, symlinked temp root, etc.) used to degrade silently to
    # `""` — the record still flips to `shipped` with the `plan` FK dropped
    # and no signal in the response. Track the failure explicitly so the
    # advanced entry can name it (`plan_fk_unresolved`) rather than reading
    # as a clean, unqualified success.
    sizing_plan_fk_unresolved = False
    if target_kind is _SIZING_KIND and source_kind == "plan" and source_path:
        try:
            candidate_source = Path(source_path)
            resolved = candidate_source if candidate_source.is_absolute() else (worktree_root / candidate_source)
            sizing_plan_fk = resolved.resolve().relative_to(worktree_root.resolve()).as_posix()
        except (OSError, ValueError):
            sizing_plan_fk = ""
            sizing_plan_fk_unresolved = True

    # Self-advance guard (needed by C6b's handoff trigger; a no-op for C6's plan
    # trigger, whose source_path is never itself a handoff in this scan surface).
    # `source_path` arrives from `post_commit_tail._run_deliverable_cascade` as
    # `str(handoff_abs)` -- an OS-native absolute path, deliberately never
    # `.as_posix()`-normalized. Safe by construction: the comparison two lines
    # below is `Path(...).resolve()` identity via `contained_path`, not string
    # equality, so an OS-native path string is fine here on any platform,
    # including Windows -- do not "fix" this into a posix-normalized string
    # (Review: coordinator:code-reviewer Finding 5).
    if source_kind == "handoff" and source_path:
        try:
            resolved_source = contained_path(
                Path(source_path), [worktree_root / "state" / "handoffs"]
            )
        except Exception:  # noqa: BLE001
            resolved_source = None
        if resolved_source is not None:
            candidates = [c for c in candidates if c["path"].resolve() != resolved_source]

    advanced: List[dict] = []
    refused: List[dict] = []
    already_advanced: List[dict] = []

    # Fixpoint iteration (see module docstring "Fixpoint iteration (chain
    # convergence)") -- `pending` holds candidates not yet finally resolved;
    # a candidate leaves `pending` the moment it advances, is found
    # already-advanced, or a pass's write attempt fails for a reason other
    # than the predicate (all three are terminal outcomes for this run).
    # `refused` is provisional per-pass — REPLACED (never appended twice for
    # the same path) so a candidate refused in an early pass and advanced in
    # a later one appears ONLY in `advanced`, per AC6i provenance coherence.
    pending: List[dict] = list(candidates)
    refused_reason: Dict[str, str] = {}
    # Resolved (realpath) paths of every candidate THIS RUN has itself proven
    # terminal (advanced or found already-advanced) — threaded into leg (b)'s
    # live-successor scan on every subsequent pass so a chain converges
    # without weakening leg (b) for anything this run did NOT itself resolve.
    resolved_this_run: List[str] = []

    max_passes = len(candidates)
    passes_done = 0
    progressed = True
    while pending and progressed and passes_done < max_passes:
        progressed = False
        passes_done += 1
        still_pending: List[dict] = []

        for candidate in pending:
            candidate_path: Path = candidate["path"]

            # Re-read the record fresh off disk each pass -- legs (a)/(c)
            # must judge CURRENT state, not a snapshot from before this run's
            # own earlier passes may have changed the corpus around it.
            # Uses `target_kind.reader` (not the handoff-fixed `_read_meta`)
            # so a re-read for the sizing kind actually parses whole-document
            # YAML instead of silently returning `{}` on a file with no
            # `---` frontmatter fences.
            try:
                fm = target_kind.reader(str(candidate_path)) or candidate["fm"]
            except Exception:  # noqa: BLE001 — fall back to the snapshot read at collection time
                fm = candidate["fm"]

            refusal = await _predicate_refusal(
                candidate_path,
                fm,
                repo_root,
                exclude_children_check=resolved_this_run,
                kind=target_kind,
            )
            if refusal is not None:
                refused_reason[str(candidate_path)] = refusal
                still_pending.append(candidate)
                continue

            # asyncio.to_thread: _advance_one composes archive_stamp.stamp_shipped_in,
            # which itself does its own asyncio.run(...) internally (see that module's
            # existing convention) — calling it directly from inside this coroutine's
            # already-running event loop would raise RuntimeError. Off-loading to a
            # thread also keeps the event loop live during the blocking git-log/file-lock
            # work, per DR-212 D3.
            if target_kind is _SIZING_KIND:
                # C3's per-kind write side -- never routed through _advance_one
                # (see module docstring / _advance_one_sizing's own docstring
                # for why: it composes ship-commit resolution that has no
                # sizing-schema analogue and would refuse every sizing).
                did_advance, write_refusal = await asyncio.to_thread(
                    _advance_one_sizing, candidate_path, sizing_plan_fk, repo_root
                )
            else:
                did_advance, write_refusal = await asyncio.to_thread(
                    _advance_one, candidate_path, deliverable_id, at, repo_root, source_path, source_kind
                )
            if did_advance:
                entry = {
                    # Review: staff-eng — Finding 7: "path" alongside the
                    # legacy "handoff_path" key, which the sizing kind's own
                    # tests already assert on and stays for compatibility —
                    # a kind-agnostic name for the growing set of non-handoff
                    # callers.
                    "handoff_path": str(candidate_path),
                    "path": str(candidate_path),
                    "message": (
                        f"advanced (status: shipped{', plan: ' + sizing_plan_fk if sizing_plan_fk else ''})"
                        if target_kind is _SIZING_KIND
                        else f"advanced (deployment_state: shipped, advanced_by: {deliverable_id})"
                    ),
                }
                if target_kind is _SIZING_KIND and sizing_plan_fk_unresolved:
                    # Review: staff-eng — Finding 4: name the dropped join
                    # rather than let a silent "" pass as a clean success.
                    entry["plan_fk_unresolved"] = source_path
                if target_kind is not _SIZING_KIND:
                    # AC6g depth: the candidate itself just advanced -- also resolve
                    # any rows its OWN body carries (evidence-joined, never blanket;
                    # see cascade_baton_rows.py's own docstring). Composed here, not
                    # re-derived -- deciding WHETHER this candidate advances stays
                    # entirely this module's per-target predicate (AC6h); row
                    # resolution only ever runs for a candidate already decided.
                    # Handoff-kind only: a sizing-object has no baton-row body to
                    # scan, and AC6g's join is defined over roadmap-baton handoffs.
                    baton_rows = await asyncio.to_thread(
                        resolve_baton_rows, candidate_path, deliverable_id, at, repo_root
                    )
                    entry["baton_rows_advanced"] = baton_rows["advanced"]
                    entry["baton_rows_unresolved"] = baton_rows["unresolved"]
                    if baton_rows.get("error"):
                        entry["baton_rows_error"] = baton_rows["error"]
                advanced.append(entry)
                refused_reason.pop(str(candidate_path), None)
                resolved_this_run.append(str(candidate_path.resolve()))
                progressed = True
            elif write_refusal == _ALREADY_ADVANCED_MARKER:
                already_advanced.append(
                    {"handoff_path": str(candidate_path), "path": str(candidate_path), "reason": write_refusal}
                )
                refused_reason.pop(str(candidate_path), None)
                resolved_this_run.append(str(candidate_path.resolve()))
                progressed = True
            else:
                refused_reason[str(candidate_path)] = write_refusal or "not advanced"
                still_pending.append(candidate)

        pending = still_pending

    # Every candidate still pending when the loop exits (no more progress, or
    # the pass bound was reached) is a genuine, final refusal.
    for candidate in pending:
        path_str = str(candidate["path"])
        refused.append(
            {
                "handoff_path": path_str,
                "path": path_str,
                "reason": refused_reason.get(path_str, "not advanced"),
            }
        )

    # Commit (C2) — every path this run itself mutated (advanced entries only;
    # a refused or already-advanced candidate contributes none) is committed
    # in ONE follow-up commit scoped to exactly that list, before this
    # handler returns. Worktree-relative posix, mirroring the shape
    # `post_commit_tail`'s own follow-up commit paths already carry.
    mutated_paths: List[str] = []
    for entry in advanced:
        candidate_path = Path(entry["path"])
        try:
            rel = candidate_path.resolve().relative_to(worktree_root.resolve()).as_posix()
        except ValueError:
            rel = str(candidate_path)
        mutated_paths.append(rel)

    commit_error: Optional[str] = None
    if mutated_paths:
        commit_error = await asyncio.to_thread(
            _commit_mutated_paths, mutated_paths, worktree_root, deliverable_id
        )

    result = {
        "exit_code": 0 if advanced else 1,
        "deliverable_id": deliverable_id,
        "source_kind": source_kind,
        "source_path": source_path,
        "candidates_matched": len(candidates),
        "advanced": advanced,
        "refused": refused,
        "already_advanced": already_advanced,
        "scan_incomplete": scan_incomplete,
        "unreadable": unreadable,
    }
    if commit_error:
        result["commit_error"] = commit_error
    if not advanced:
        if not candidates:
            # Review: staff-eng — Finding 7: this message was hardcoded to
            # the handoff kind and named the wrong corpus/artifact class when
            # run with target_kind="sizing" — the zero-candidate path is the
            # only signal an EM gets, so it must name the corpus it actually
            # scanned.
            result["error"] = (
                f"deliverable.cascade_terminal: no live {target_kind.name} in "
                f"{target_kind.corpus_subdir}/ carries deliverable_id={deliverable_id!r} "
                "— nothing to advance"
            )
        else:
            result["error"] = (
                f"deliverable.cascade_terminal: {len(candidates)} candidate(s) matched "
                f"deliverable_id={deliverable_id!r} but every one was refused — see 'refused'"
            )
    return result
