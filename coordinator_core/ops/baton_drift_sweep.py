"""
coordinator_core.ops.baton_drift_sweep — read-only baton (handoff) drift diagnostic.

Purpose: for every OPEN baton (state/handoffs/*.md) that is itself non-terminal
(deployment_state not one of shipped|continued|closed — see handoff_archive_transition's
_TERMINAL_DEPLOYMENT_STATES, mirrored here), classify it by whether some OTHER handoff
names it as a predecessor:

  - Not referenced by anything          -> TIP    (live work in progress; normal)
  - Referenced, and the referencing successor is itself still live (non-terminal or not
    yet archived)                        -> HELD   (correctly held per the C1/C5 cascade —
                                                     steady state is roughly one held baton
                                                     per live chain)
  - Referenced, but every referencing successor is terminal/archived while THIS baton
    never got archived alongside it, split by a THIRD axis — was the baton itself EVER
    claimed or shipped (DR-242's own `claimed_or_shipped` predicate, not this module's
    successor-based test):
      - Baton was claimed or shipped -> STRANDED (a bug — the mint path's transactional
                                                   predecessor-archive should have swept
                                                   this; must be zero; C3's boot drain
                                                   supersedes it automatically)
      - Baton was NEVER claimed or shipped -> NEVER_STARTED (nobody ever picked this
                                                   up; a later handoff merely NAMED it as
                                                   predecessor. `handoff.archive_transition`
                                                   mode=supersede correctly REFUSES these
                                                   under DR-242 — stamping `continued` would
                                                   assert a succession that never happened.
                                                   Retiring one is an `abandoned` call,
                                                   reserved to an explicit human/session
                                                   decision, never to a sweep. This bucket
                                                   has no automated drain and is NOT
                                                   "must be zero" — see § C5,
                                                   docs/plans/2026-08-05-stranded-baton-
                                                   drainage-make-the-detecto.md.)

SECOND LEG (2026-08-04, cross-repo/inbox/2026-08-04-example-market-data-repo-em-
baton-terminal-state-not-cleared-programmatically.md, defect 1, item 3):
`STRANDED` above is successor-based by construction — it can only ever fire on
a baton some OTHER handoff names as its predecessor. A baton reconciled to
terminal because every one of its own next-steps was closed by work that
landed elsewhere has NO successor at all (nothing survived the reconcile to
spawn one) and is therefore structurally invisible to a successor-walk: there
is no successor to walk to. It falls into `tips` (referenced by nothing) and
reads as ordinary live work, indistinguishable from genuine in-progress
scope, even though a human/session already concluded terminal.

`RECONCILED_NO_SUCCESSOR` closes that gap for the ONE population where a
record of that conclusion survives on disk: `state/audits/*-baton-reconciled-
closed.md` files carrying `kind: audit-record` frontmatter (the naming/kind
convention the reporting memo's own example uses) whose body names a live,
still-non-terminal `state/handoffs/*.md` path. Per this module's own
Negative-spec (does not re-derive next-step closure heuristically), THIS leg
does not attempt to re-judge whether a baton's next-steps are actually
closed — it keys off the durable, already-on-disk EVIDENCE that some prior
session already made that judgment and recorded it, exactly the "prefer
durable evidence over re-deriving a heuristic" shape the reporting memo
itself suggests. A baton is promoted out of `tips` into this bucket ONLY
when it has no successor reference at all (mirrors `STRANDED`'s own
successor-based test on the opposite branch of the same held/stranded/tips
partition above) — the two legs are therefore mutually exclusive by
construction: `STRANDED` requires the raw referencer list (any successor at
all, terminal or not — the same test `referenced_by(...).get("referenced")`
performs, see `_referencers_of`) to be non-empty; this leg only ever promotes
a baton for which that list is empty.

THIRD LEG (C4, docs/plans/2026-08-05-reaper-preserves-closure-evidence.md § AC10):
`REAPED_ORPHAN` closes a different gap in the same `tips` population — a chain-tip
baton the crash-orphan reaper released from a dead session (frontmatter
`reaped_from_session` present) that nobody has since adjudicated OR re-picked-up.
Placed on the no-successor side, mirroring `RECONCILED_NO_SUCCESSOR`'s own
mutual-exclusion construction against `STRANDED`/`HELD` (only a baton with zero
successor references reaches this leg at all). Reconciled evidence — a human/
session conclusion that the work is done — is checked FIRST and takes precedence:
a baton can carry both a qualifying reconciled-closed audit record and
`reaped_from_session` (it died holding work that was later concluded closed by a
different route), and the stronger signal (an actual conclusion) wins over the
weaker one (only "a session died holding this"). The key is `reaped_from_session`
present AND no active claim (`claimed_by`/`consumed_by` both empty) — that second
conjunct is load-bearing: `_claim` does not strip `reaped_from_session` on
re-pickup, so keying on mere presence would keep a legitimately re-claimed baton
in this bucket forever. This makes `reaped_orphan` genuinely zero-is-healthy — a
work QUEUE, not a monotonically-growing count like `never_started`: an entry
drains the moment it is adjudicated (shipped/archived -> terminal, handled by the
existing `terminal_not_archived` branch above this loop) OR re-picked-up (active
claim reappears). Unlike `RECONCILED_NO_SUCCESSOR`'s narrower "should be 0"
framing (a genuine bug signal), a nonzero `reaped_orphan` count is not itself
alarming — it is the expected shape of "batons currently sitting on the shelf
awaiting adjudication," and should be read as a queue depth, not a defect count.

Deliberately does NOT collapse this to a single "open batons with a successor" count —
see docs/plans/2026-07-26-push-side-write-discipline.md § D2d: a single count that is
never zero (held is expected, roughly one per live chain) is a line people stop reading.
Held/stranded is the only split that survives daily exposure, because zero-is-healthy is
the only shape of metric that does.

Reuses, does not reimplement:
  - coordinator_core.ops.handoff_children._collect_handoff_paths — the same live+archived
    dag_index scan handoff.has_live_children itself scans with.
  - coordinator_core.dag.handoff_edges / resolve_target / build_handoff_id_index /
    _read_meta — the exact primitives coordinator_core.archival.reverse_membership and
    coordinator_core.dag.referenced_by themselves compose internally, called here
    directly instead of through those two wrappers. Each wrapper re-walks the ENTIRE
    dag_index per call (O(live x index): every live baton triggers a full re-scan of
    every archived+live handoff's frontmatter, twice — once per wrapper), which is fine
    at their own single-target call sites but is the dominant cost when called once per
    live baton here. `_build_predecessor_reverse_index` performs the SAME resolution
    logic in one forward pass over dag_index, producing a reverse map any number of
    single-target lookups then consult in O(1) — same inputs, same predicates
    (`_is_terminal_or_archived_child` included, applied identically), only the call
    count differs. See that function's own docstring for the equivalence argument.
  - coordinator_core.ops.fleet._common.main_worktree_root / handoff_archive_transition's
    own _TERMINAL_DEPLOYMENT_STATES constant (duplicated here, not imported — see that
    module's own "vendored, not imported" precedent for _SCHEMA_PATH; importing it would
    pull in the full archive_transition op-registration side effect for one constant).
  - coordinator_core.archival.claimed_or_shipped_at_path — the SAME predicate
    coordinator_core.archive_stamp.cs_supersede_archive_handoff's own DR-242 refusal
    site imports, reused here rather than re-derived so the STRANDED/NEVER_STARTED
    split can never drift from the op that actually refuses NEVER_STARTED batons.
    Relocated (2026-08-06) out of `coordinator_core.tests._baton_dag_oracle` into this
    production home — production must not import a `tests`-named package; see
    `coordinator_core.archival`'s own DR-242 section header for why this is a
    relocation, not a redesign, and why the differential-oracle's own copy of the
    frontmatter-reading helpers stays behind unchanged.

NOT wired through @register_op / the JSON-RPC op-classification quad — same precedent as
completion_ops.day_coverage_sweep (see that module's own header): a pure read-only
diagnostic, direct-import CLI trampoline, no IPC dispatch.

Negative-spec:
  - READ-ONLY: performs zero writes, mutates nothing on disk, runs no mutating git verb.
  - Does NOT walk transitively — single-hop reverse-edge test only (mirrors
    reverse_membership / referenced_by's own single-hop contract).
  - Does NOT follow additional_predecessors/forked_from — only the primary `predecessor`
    spine (aliased to `predecessor_id`), matching CLAUDE.md's "Predecessor is whatever
    handoff this session was opened with — the primary spine; adjacency is not ancestry."
  - A baton with continued_into set already carries deployment_state:continued (terminal
    by construction, see _TERMINAL_DEPLOYMENT_STATES), so it never reaches the
    non-terminal population (held/stranded/never_started/tips/reconciled_no_successor/
    reaped_orphan/desynced) this sweep classifies above; that shape falls under
    terminal_not_archived — FIFTH LEG (below) is the one exception that looks inside
    that population for the specific `continued`-with-dangling-move shape.
  - (SECOND LEG) Does NOT re-derive whether a baton's next-steps are actually closed —
    reads only the audit record's EXISTENCE, its `kind: audit-record` frontmatter, its
    filename suffix, and a plain textual scan for a live handoff path in its body; never
    re-evaluates commit evidence or next-step closure itself (that judgment stays with
    whichever ceremony wrote the audit record).
  - (SECOND LEG) Does NOT walk `archive/audits/` or any archived-audit location — only
    `state/audits/` (live, un-archived audit records) is scanned; an audit record that
    has ITSELF since been archived is out of scope for this sweep.
  - (SECOND LEG) Does NOT promote a baton that already has ANY successor reference
    (live or terminal) — that shape is STRANDED's or HELD's, not this leg's; see the
    module docstring's mutual-exclusion argument.
  - (C5) Does NOT re-derive "was this baton ever claimed or shipped" — imports
    `claimed_or_shipped_at_path` rather than writing a second implementation that could
    drift from `archive_stamp.cs_supersede_archive_handoff`'s own DR-242 refusal check
    (see "Reuses, does not reimplement" above). NEVER_STARTED and STRANDED are computed
    from that ONE predicate applied to the successor-referenced, all-terminal population
    already isolated above — never double-counted, since every such baton lands in
    exactly one of the two buckets by construction (the predicate is a plain boolean).
  - (C5) Does NOT attempt to drain, stamp, or otherwise resolve NEVER_STARTED batons —
    read-only diagnostic, same as the rest of this module; disposing of one is an
    `abandoned` call reserved to an explicit human/session decision (see module docstring).
  - (THIRD LEG / C4) Does NOT promote a baton into `reaped_orphan` that also qualifies
    for `reconciled_no_successor` — reconciled evidence is checked FIRST and wins (see
    module docstring). A baton cannot land in both buckets.
  - (THIRD LEG / C4) Does NOT strip, backfill, or otherwise write `reaped_from_session` —
    read-only diagnostic; presence/absence is read exactly as found on disk.
  - (THIRD LEG / C4) Does NOT treat mere `reaped_from_session` presence as sufficient —
    also requires no active claim (`claimed_by`/`consumed_by` both empty), since `_claim`
    does not strip `reaped_from_session` on re-pickup (see module docstring).

FOURTH LEG (C8, docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md § C8):
`DESYNCED` closes a gap in the same no-successor population: a baton whose branch-
independent claim ledger still holds a LIVE claim while the tracked-frontmatter mirror
reverted to no-claim (the exact branch-switch-revert incident `coordinator_core.claim_state`
generalizes a fix for). Such a baton satisfies neither `reaped_orphan` (no
`reaped_from_session`) nor the successor-based buckets (no successor at all) cleanly, so
absent this leg it fell through to `tips` and reported as ordinary live work — the sweep
was accordingly reporting the corpus as healthier than it actually is. Keyed on
`coordinator_core.claim_state.resolve_claim_state`'s own `disagreement` flag (a live ledger
claim, no mirror claim) — checked AFTER `reconciled_no_successor` and `reaped_orphan` (both
stronger/more specific signals) and immediately ahead of the final `tips` fall-through, so a
baton reaching this leg has already failed every other classification.

Reuses, does not reimplement: `coordinator_core.claim_state.resolve_claim_state` — the SAME
ledger-first accessor C1 introduced, re-homing the `handoff_claim_dir` +
`cs_claim_holder_live` pair `handoff_reconcile._ancestor_liveness_blocked` already imports.
This module does not re-derive ledger-vs-mirror resolution.

Negative-spec (FOURTH LEG / C8):
  - DETECTION ONLY — does not repair, re-stamp, or otherwise write anything (repair is C9's
    leg, a distinct writer; adding a second one here would violate DR-212's sanctioned
    three-writer closure). This module remains READ-ONLY, unconditionally.
  - Does NOT promote a baton into `desynced` that already qualifies for
    `reconciled_no_successor` or `reaped_orphan` — both are checked first and win; a baton
    lands in at most one of the no-successor buckets.
  - Does NOT walk successor-referenced batons (`held`/`stranded`/`never_started`) — this leg
    only ever evaluates the same no-successor population `reconciled_no_successor` and
    `reaped_orphan` already isolate.

FIFTH LEG (C1, docs/plans/2026-08-18-retained-supersede-finishes-its-archive.md): `RETAINED`
looks INSIDE the `terminal_not_archived` population (not the non-terminal one every other
leg partitions) for one specific shape: `handoff.archive_transition` mode=supersede's own
retain grounds (`retain_kind: "live-holder"` / `"live-parent"` / `"indeterminate"`) commit
the status flip (deployment_state -> continued, continued_into -> successor) but
deliberately skip the archival git-mv — and nothing ever drains that promise once the
retain ground clears (see that op's own module docstring). A `state/handoffs/` record
with deployment_state:continued AND a non-empty continued_into IS that shape by
definition — the location predicate is load-bearing (a `continued` record already moved
to `archive/handoffs/` is the SUCCESS case, not a hit). REPORT-ONLY: computes and returns
per-hit CURRENT eligibility (holder now dead AND live-children guard now clears, using the
SAME two accessors the retain grounds themselves call) — never the unrecoverable historical
retain_kind, never a mutation. Draining a hit is a separate, operator-initiated act.

Reuses, does not reimplement: `handoff_archive_transition._handoff_live_holder_session` and
`handoff_children._handoff_has_live_children` — the exact two accessors the retain grounds
this leg detects call internally, so eligibility can never drift from what a re-run of
`archive_transition` mode=supersede would itself decide.

Negative-spec (FIFTH LEG / C1):
  - REPORT-ONLY — computes and returns; calls no `archive_transition` verb, no git mv, no
    stamp. Draining is a separate, operator-initiated act, not this sweep's job.
  - Does NOT widen `baton_assemble.apply._dispatch_handoff_supersede_predecessor`'s
    `superseded`-only assertion, and does NOT make a retain raise anywhere — out of scope
    entirely (see that plan's own Anti-scope: a 2026-08-03 live repro shows a deterministic
    raise there deleting a freshly-minted successor on every retry, unable to converge).
  - Does NOT treat a `continued` record under `archive/handoffs/` as a hit — that is the
    SUCCESS case (the move already landed); this leg's location predicate keys on
    `state/handoffs/` specifically.
  - Does NOT persist or attempt to recover the original `retain_kind` — it was never
    written to disk and this leg does not re-derive it; only CURRENT eligibility is
    reported.

Spec backlink (FIFTH LEG / C1): pln-retained-supersede-finishes-it-d1deb5 § Tasks C1, AC1-AC3.

Spec backlink: DoE-claude:pln-push-side-write-discipline-for-05c30d § D2d
Spec backlink (SECOND LEG): cross-repo/inbox/2026-08-04-example-market-data-repo-em-baton-
terminal-state-not-cleared-programmatically.md, defect 1, item 3.
Spec backlink (C5): pln-stranded-baton-drainage-make-t-f4a679 § C5.
Spec backlink (THIRD LEG / C4): pln-reaper-preserves-closure-evide-34a6fc § AC10.
Spec backlink (FOURTH LEG / C8): pln-claim-state-make-the-ledger-th-6641e3
§ Tasks C8, AC17. NOTE: this leg knowingly breaks AC5 of
docs/plans/2026-08-05-stranded-baton-drainage-make-the-detecto.md (byte-identical result-dict
output) — the sibling plan's AC5 gives, by PM ruling 2026-08-07; see that plan's own amendment
recording the collision.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Dict, FrozenSet, List, Tuple

# C5 (docs/plans/2026-08-05-stranded-baton-drainage-make-the-detecto.md): the same
# DR-242 predicate coordinator_core.archive_stamp.cs_supersede_archive_handoff's own
# refusal site imports — see this module's "Reuses, does not reimplement" section.
from coordinator_core.archival import _is_terminal_or_archived_child, claimed_or_shipped_at_path
# C8 (docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md § C8): the SAME
# ledger-first accessor C1 introduced — see this module's "FOURTH LEG" docstring section.
from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.dag import (
    _read_meta,
    _ref_names_foreign_family,
    build_handoff_id_index,
    handoff_edges,
    resolve_target,
)
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.handoff_archive_transition import _handoff_live_holder_session
from coordinator_core.ops.handoff_children import _collect_handoff_paths, _handoff_has_live_children

# Mirrors handoff_archive_transition._TERMINAL_DEPLOYMENT_STATES (vendored, not
# imported — see module docstring).
_TERMINAL_DEPLOYMENT_STATES = frozenset({"shipped", "continued", "closed"})

#: Primary-spine only (see module docstring Negative-spec).
_EDGE_KINDS = frozenset({"predecessor"})

#: SECOND LEG — the naming convention the reporting memo's own example audit
#: record uses (`<date>-<slug>-baton-reconciled-closed.md`). Only files ending
#: in this exact suffix are scanned — a filename this specific is itself part
#: of the durable, on-disk evidence this leg keys off (see module docstring).
_RECONCILED_CLOSED_AUDIT_SUFFIX = "-baton-reconciled-closed.md"

#: SECOND LEG — the `kind:` frontmatter value the reporting memo's own example
#: audit record carries. An audit file missing this frontmatter value, or
#: carrying a different one, is not treated as qualifying evidence — the
#: filename suffix alone is not sufficient (a same-named but differently-kinded
#: doc must not silently qualify).
_AUDIT_RECORD_KIND = "audit-record"

#: SECOND LEG — matches a live `state/handoffs/*.md` path mention inside an
#: audit record's body text. Deliberately loose (any non-whitespace run ending
#: in `.md`) rather than a strict basename grammar — audit-record prose is
#: free-form, and the goal is "does this audit's own text name a specific live
#: handoff", not full path validation (the candidate is re-validated against
#: the actual `open_paths` set below regardless of what this regex captures).
_HANDOFF_PATH_MENTION_RE = re.compile(r"state/handoffs/([^\s)\]\"'`|]+\.md)")


def _reconciled_no_successor_basenames(worktree_root: Path) -> "FrozenSet[str]":
    """SECOND LEG — basenames of live handoffs named by a qualifying
    `*-baton-reconciled-closed.md` audit record under `state/audits/`.

    A file qualifies when its name ends with `_RECONCILED_CLOSED_AUDIT_SUFFIX`
    AND its frontmatter `kind:` equals `_AUDIT_RECORD_KIND` — both durable,
    already-on-disk facts, never re-derived from the audit's own prose
    judgment (see module docstring's Negative-spec). Every
    `state/handoffs/*.md` path mention in a qualifying audit's full text
    (frontmatter + body) contributes its basename to the returned set; the
    caller cross-checks each basename against the ACTUAL live open-handoff
    set, so a stale/typo'd/no-longer-live mention here is harmless — it
    simply never matches anything in the caller's loop.

    Returns an empty set when `state/audits/` does not exist, or names no
    qualifying file — the common case for a repo with no orphaned reconcile
    conclusions.
    """
    audits_dir = worktree_root / "state" / "audits"
    if not audits_dir.is_dir():
        return frozenset()

    basenames: set[str] = set()
    for path in sorted(audits_dir.iterdir()):
        if not path.is_file() or not path.name.endswith(_RECONCILED_CLOSED_AUDIT_SUFFIX):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        split = split_frontmatter(text)
        if split is None:
            continue
        if read_fm_field(split.fm_text, "kind") != _AUDIT_RECORD_KIND:
            continue
        for match in _HANDOFF_PATH_MENTION_RE.finditer(text):
            basenames.add(Path(match.group(1)).name)
    return frozenset(basenames)


def _build_predecessor_reverse_index(
    dag_index: List[str], repo_root: str
) -> "Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, dict]]":
    """One pass over dag_index building the `predecessor` reverse-edge index.

    `reverse_membership` and `referenced_by` each independently re-derive the
    SAME question — "which node in dag_index names this target as
    predecessor?" — once per candidate baton, at O(live x index) cost: every
    call re-walks the full index, calling `_read_meta` on every one of its
    246 files whether or not that file's edges were already inspected for a
    different target this same sweep. A single forward pass over dag_index,
    reading each node's own frontmatter once and resolving its own
    predecessor edge(s) once (via `handoff_edges` + `resolve_target`, the
    exact primitives those two callees use internally), then recording the
    referencing node under every target its edge(s) resolve to, answers the
    identical membership question for every target at once — the RESOLUTION
    LOGIC is untouched, only how many times the same resolution is computed.
    This is why the two shapes are byte-identical: same predicate, same
    inputs, same call graph one hop lower, just amortized across candidates
    instead of repeated per candidate.

    Dedup is per (node, target) and per (node, basename) — a node whose
    `predecessor` and `predecessor_id` both resolve to the same target must
    still appear once in that target's referencer list, mirroring the
    break-after-first-match semantics of the single-target scan this
    replaces (a match on any one edge is sufficient; further edges add
    nothing once a node is already recorded against a given target).

    The (unresolvable-or-'git-history') fallback branch — `referenced_by`'s
    own basename(raw_ref) == basename(target) comparison — is preserved
    exactly, bucketed by raw_ref's OWN basename (not the eventual target's)
    so a later lookup keyed by any target sharing that basename reproduces
    the same fallback hit the per-call scan would have produced, including
    its known imprecision (a basename match does not confirm the ref and
    the target are the same file — see `referenced_by`'s own docstring).

    Returns:
        (exact_by_target, fallback_by_basename, meta_by_path) — absolute
        target path -> referencing node list, raw-ref basename -> referencing
        node list, and every node's own `_read_meta` result captured for free
        during this same pass so the caller's per-baton deployment_state read
        does not re-read a file this pass already read.
    """
    id_index = build_handoff_id_index(dag_index)
    exact_by_target: Dict[str, List[str]] = {}
    fallback_by_basename: Dict[str, List[str]] = {}
    meta_by_path: Dict[str, dict] = {}

    for node_abs_path in dag_index:
        meta = _read_meta(node_abs_path)
        meta_by_path[node_abs_path] = meta
        node_handoff_dir = os.path.dirname(node_abs_path)
        raw_edges = handoff_edges(meta, _EDGE_KINDS)

        seen_targets: set = set()
        seen_basenames: set = set()
        for raw_ref in raw_edges:
            # This branch is a deliberate duplicate of `dag.referenced_by`'s fallback
            # (see this module's docstring), so it inherits that function's two
            # constraints rather than only the one it was written with. Tier 3 is off
            # for the same reason: the branch below collapses `None` and `'git-history'`
            # onto one path, so the probe can change no outcome while costing a
            # `git log --all` spawn per (node, edge) pair. And basename recovery is
            # withheld from a ref naming a non-baton family — without that gate a
            # handoff carrying `predecessor: cross-repo/inbox/<slug>.md`, named
            # `<slug>.md` itself, indexes as its own referencer once the memo archives,
            # and STRANDED/HELD classification then sees a live child that is the baton
            # itself. Keep both in step with `dag.referenced_by`.
            resolved_ref = resolve_target(
                raw_ref,
                node_handoff_dir,
                repo_root,
                id_index=id_index,
                include_history_tier=False,
            )
            if resolved_ref is None or resolved_ref == "git-history":
                if _ref_names_foreign_family(raw_ref):
                    continue
                basename = os.path.basename(raw_ref)
                if basename not in seen_basenames:
                    seen_basenames.add(basename)
                    fallback_by_basename.setdefault(basename, []).append(node_abs_path)
                continue
            abs_target = os.path.abspath(resolved_ref)
            if abs_target not in seen_targets:
                seen_targets.add(abs_target)
                exact_by_target.setdefault(abs_target, []).append(node_abs_path)

    return exact_by_target, fallback_by_basename, meta_by_path


def _referencers_of(
    path: str,
    exact_by_target: Dict[str, List[str]],
    fallback_by_basename: Dict[str, List[str]],
) -> List[str]:
    """Dict-lookup equivalent of `referenced_by(path, ...)["referencedBy"]`.

    Union of the exact-path bucket and the basename-fallback bucket for
    `path`, deduped so a node that hits both (an exact edge to `path` AND an
    unrelated unresolvable edge sharing `path`'s basename) is not
    double-counted — matches the single-entry-per-node guarantee of the
    primitive this replaces.
    """
    abs_path = os.path.abspath(path)
    referencers = list(exact_by_target.get(abs_path, ()))
    seen = set(referencers)
    for node in fallback_by_basename.get(os.path.basename(path), ()):
        if node not in seen:
            seen.add(node)
            referencers.append(node)
    return referencers


def _retained_supersede_eligibility(
    path: str,
    continued_into: str,
    node_repo_root: str,
    id_index: Dict[str, str],
    common_dir: "Path | None",
) -> bool:
    """CURRENT eligibility (AC2) for a `retained`-bucket hit — whether the
    supersede move this record's `continued_into` promised can be completed
    TODAY, not why it was retained originally (that cause was never
    persisted — see the module's `retained` docstring entry).

    Eligible only when BOTH retain grounds
    `handoff_archive_transition::_supersede_continued`'s own gates check have
    now cleared:
      - holder liveness: `_handoff_live_holder_session` (the SAME accessor
        the live-holder retain ground calls) returns None for this record.
      - live children: `handoff.has_live_children` (the SAME guard the
        live-parent/indeterminate retain ground calls), with `exclude` set
        to the successor's OWN absolute path — omitting the exclude makes
        the successor read as its own live child (it names this record via
        `predecessor:`/`continued_into` by construction) and every hit would
        read ineligible regardless of the true holder/child state.

    A `continued_into` value this module cannot resolve to an on-disk path
    (id/path shape it has no evidence for) cannot be safely excluded, so the
    live-children guard is called WITHOUT an exclude in that case — fail-
    closed (the successor then counts as its own live child, reading
    ineligible) rather than guessing a path. `include_history_tier=False`
    on the resolve: this call must never spawn a per-record git subprocess
    (coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py governs
    this file's family) — the successor is always disk-resident by the time
    a `continued` stamp names it, so tiers 1-2 (pure filesystem) are
    sufficient.
    """
    handoff_dir = os.path.dirname(path)
    target_abs = resolve_target(
        continued_into,
        handoff_dir,
        node_repo_root,
        id_index=id_index,
        include_history_tier=False,
    )
    exclude = [target_abs] if target_abs and target_abs != "git-history" else []

    if common_dir is not None:
        try:
            holder_session = _handoff_live_holder_session(Path(path), common_dir)
        except Exception:
            holder_session = "<indeterminate>"
    else:
        holder_session = "<indeterminate>"
    holder_cleared = holder_session is None

    guard_repo_root = common_dir if common_dir is not None else Path(node_repo_root)
    try:
        guard_result = asyncio.run(
            _handoff_has_live_children(
                {"candidate": path, "exclude": exclude}, guard_repo_root
            )
        )
        children_cleared = guard_result.get("exit_code") == 1
    except Exception:
        children_cleared = False

    return holder_cleared and children_cleared


def baton_drift_sweep(worktree_root: Path) -> dict:
    """Classify every open baton (state/handoffs/*.md) by archival/succession drift.

    Args:
        worktree_root: repo root (git worktree) — same noun as
            completion_ops.day_coverage_sweep's own `worktree_root` param.

    Returns:
        {
          "total_live": int,             # files currently under state/handoffs/
          "terminal_not_archived": int,  # deployment_state already terminal, not yet moved
          "non_terminal": int,           # total_live - terminal_not_archived
          "held": int,                   # non-terminal, referenced by a still-live successor
          "stranded": int,               # non-terminal, referenced ONLY by terminal/archived
                                          # successor(s), AND the baton was itself ever
                                          # claimed or shipped (DR-242's claimed_or_shipped) —
                                          # the chain broke after work started; drainable by
                                          # C3's boot supersede; should be 0
          "stranded_paths": [str, ...],  # absolute paths of the stranded batons
          "never_started": int,          # same successor shape as `stranded`, but the baton
                                          # was NEVER claimed or shipped — nobody picked it up;
                                          # a later handoff merely named it as predecessor.
                                          # handoff.archive_transition mode=supersede correctly
                                          # refuses these under DR-242 (stamping `continued`
                                          # would assert a succession that never happened);
                                          # retiring one is an `abandoned` call reserved to an
                                          # explicit human/session decision. NOT "should be 0"
                                          # — see module docstring's third-axis split (C5).
          "never_started_paths": [str, ...],  # absolute paths of that population
          "tips": int,                   # non-terminal, referenced by nothing, and NOT named
                                          # by a qualifying reconciled-closed audit record —
                                          # live work
          "reconciled_no_successor": int,        # SECOND LEG — non-terminal, referenced by
                                                  # nothing, but named by a qualifying
                                                  # *-baton-reconciled-closed.md audit record
                                                  # — should be 0 (see module docstring)
          "reconciled_no_successor_paths": [str, ...],  # absolute paths of that population
          "reaped_orphan": int,           # THIRD LEG (C4) — non-terminal, referenced by
                                           # nothing, NOT reconciled, but carries
                                           # reaped_from_session with no active claim
                                           # (claimed_by/consumed_by both empty) — a
                                           # crash-orphaned baton still on the shelf.
                                           # Genuinely zero-is-healthy as a WORK QUEUE
                                           # (not a bug signal like reconciled_no_successor):
                                           # drains on adjudication (shipped/archived) or
                                           # re-pickup (active claim reappears).
          "reaped_orphan_paths": [str, ...],  # absolute paths of that population
          "desynced": int,                # FOURTH LEG (C8) — non-terminal, referenced by
                                           # nothing, NOT reconciled, NOT a reaped orphan,
                                           # but the branch-independent claim ledger holds a
                                           # LIVE claim the tracked-frontmatter mirror does
                                           # not reflect (resolve_claim_state's own
                                           # `disagreement` flag) — the branch-switch-revert
                                           # desync coordinator_core.claim_state exists to
                                           # detect. Should be 0; a nonzero count means a
                                           # fully-worked baton is misfiled as ordinary live
                                           # work (`tips`).
          "desynced_paths": [str, ...],   # absolute paths of that population
          "retained": int,                # REPORT-ONLY (pln-retained-supersede-finishes-it-d1deb5,
                                           # C1) — a `state/handoffs/` record with
                                           # deployment_state:continued AND a non-empty
                                           # continued_into: the supersede's status flip
                                           # landed but its archival git-mv never did (see
                                           # module docstring's Anti-scope). NOT drained here
                                           # — computes and returns only; no
                                           # archive_transition call, no git mv, no stamp.
          "retained_paths": [str, ...],   # absolute paths of that population
          "retained_eligible": {str: bool},  # per-path CURRENT eligibility (AC2) keyed by
                                           # the same absolute path — True only when both the
                                           # holder-liveness and live-children retain grounds
                                           # have now cleared, i.e. the move could complete
                                           # today. NOT the historical retain_kind (never
                                           # persisted, unrecoverable).
        }
    """
    open_dir = worktree_root / "state" / "handoffs"
    open_paths: List[str] = (
        sorted(str(p.resolve()) for p in open_dir.iterdir() if p.suffix == ".md" and p.is_file())
        if open_dir.is_dir()
        else []
    )

    dag_index, _scan_errors = _collect_handoff_paths(worktree_root)
    reconciled_basenames = _reconciled_no_successor_basenames(worktree_root)

    # Mirrors dag._repo_root_from_handoff_dir(str(open_dir)) — the same
    # derivation referenced_by/reverse_membership would have used had
    # open_dir been threaded through as their handoff_dir argument.
    repo_root = os.path.normpath(os.path.join(str(open_dir), "..", ".."))
    exact_by_target, fallback_by_basename, meta_by_path = _build_predecessor_reverse_index(
        dag_index, repo_root
    )

    terminal_not_archived = 0
    held = 0
    stranded = 0
    stranded_paths: List[str] = []
    never_started = 0
    never_started_paths: List[str] = []
    tips = 0
    reconciled_no_successor = 0
    reconciled_no_successor_paths: List[str] = []
    reaped_orphan = 0
    reaped_orphan_paths: List[str] = []
    desynced = 0
    desynced_paths: List[str] = []
    retained = 0
    retained_paths: List[str] = []
    retained_eligible: Dict[str, bool] = {}

    # Built once, off the SAME dag_index the reverse-predecessor index above
    # already scanned (its per-file reads are cache-backed by _read_meta — see
    # that function's own docstring — so this second pass costs no new I/O on
    # the common case). Reused only to resolve `continued_into` values that
    # are a handoff_id rather than a path (C1's retained-bucket eligibility
    # check, below).
    id_index = build_handoff_id_index(dag_index)

    # C8: resolved once for the whole sweep, mirroring resolve_claim_state's own
    # hot-path contract (pass a pre-resolved common_dir to skip a second
    # git_common_dir resolution per baton; git_common_dir is itself lru_cache'd,
    # so this degrades gracefully even on failure).
    try:
        common_dir = git_common_dir(worktree_root)
    except Exception:
        common_dir = None

    for path in open_paths:
        meta = meta_by_path.get(path)
        if meta is None:
            meta = _read_meta(path)
        deployment_state = meta.get("deployment_state")

        if deployment_state in _TERMINAL_DEPLOYMENT_STATES:
            terminal_not_archived += 1

            # C1 (pln-retained-supersede-finishes-it-d1deb5): a `continued`
            # record still under state/handoffs/ (this loop's own scan root —
            # see module docstring's Anti-scope fourth bullet: a `continued`
            # record under archive/handoffs/ is the SUCCESS case, never a
            # hit, and this loop never walks that tree) with a non-empty
            # continued_into is a supersede whose status flip landed but
            # whose archival move never did.
            if deployment_state == "continued":
                continued_into = meta.get("continued_into")
                if isinstance(continued_into, str) and continued_into.strip():
                    retained += 1
                    retained_paths.append(path)
                    retained_eligible[path] = _retained_supersede_eligibility(
                        path, continued_into.strip(), repo_root, id_index, common_dir
                    )
            continue

        # One referencer computation feeds both HELD (filtered to still-live
        # successors) and STRANDED (any successor at all, terminal or not) —
        # the two existing calls this replaces (reverse_membership,
        # referenced_by) computed this identical raw referencer list twice
        # over the same index, differing only in whether
        # _is_terminal_or_archived_child was applied afterward.
        referencers = _referencers_of(path, exact_by_target, fallback_by_basename)
        live_children = [c for c in referencers if not _is_terminal_or_archived_child(c)]
        if live_children:
            held += 1
            continue

        if referencers:
            # C5 third axis: the successor-terminal shape above is fused no
            # further — split by whether THIS baton was itself ever claimed
            # or shipped (DR-242's own predicate, not a re-derivation of it;
            # see module docstring's "Reuses, does not reimplement"). A baton
            # lands in exactly one of the two buckets, never both.
            if claimed_or_shipped_at_path(path):
                stranded += 1
                stranded_paths.append(path)
            else:
                never_started += 1
                never_started_paths.append(path)
        elif Path(path).name in reconciled_basenames:
            # THIRD LEG (C4): reconciled evidence is checked FIRST — a stronger
            # signal (human/session conclusion the work is done) than mere
            # `reaped_from_session` presence (only "a session died holding
            # this"). See module docstring's precedence argument.
            reconciled_no_successor += 1
            reconciled_no_successor_paths.append(path)
        elif meta.get("reaped_from_session") and not (
            meta.get("claimed_by") or meta.get("consumed_by")
        ):
            reaped_orphan += 1
            reaped_orphan_paths.append(path)
        else:
            # FOURTH LEG (C8): checked last, ahead of the `tips` fall-through —
            # a baton reaching here already failed reconciled/reaped-orphan.
            claim_state = resolve_claim_state(
                Path(path), common_dir=common_dir, repo_root=worktree_root
            )
            if claim_state.disagreement:
                desynced += 1
                desynced_paths.append(path)
            else:
                tips += 1

    return {
        "total_live": len(open_paths),
        "terminal_not_archived": terminal_not_archived,
        "non_terminal": len(open_paths) - terminal_not_archived,
        "held": held,
        "stranded": stranded,
        "stranded_paths": stranded_paths,
        "never_started": never_started,
        "never_started_paths": never_started_paths,
        "tips": tips,
        "reconciled_no_successor": reconciled_no_successor,
        "reconciled_no_successor_paths": reconciled_no_successor_paths,
        "reaped_orphan": reaped_orphan,
        "reaped_orphan_paths": reaped_orphan_paths,
        "desynced": desynced,
        "desynced_paths": desynced_paths,
        "retained": retained,
        "retained_paths": retained_paths,
        "retained_eligible": retained_eligible,
    }
