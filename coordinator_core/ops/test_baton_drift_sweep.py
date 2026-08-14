"""
coordinator_core.ops.test_baton_drift_sweep — pytest coverage for
coordinator_core.ops.baton_drift_sweep (day_coverage_sweep's sibling
read-only diagnostic — see that module's own docstring).

Spec backlink: DoE-claude:pln-push-side-write-discipline-for-05c30d § D2d
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.archival import reverse_membership
from coordinator_core.dag import referenced_by
from coordinator_core.ops.baton_drift_sweep import baton_drift_sweep


def _write_handoff(
    path: Path,
    *,
    predecessor: str | None = "none",
    predecessor_id: str | None = None,
    handoff_id: str | None = None,
    deployment_state: str | None = None,
    status: str | None = None,
) -> None:
    lines = ["---"]
    if predecessor is not None:
        lines.append(f"predecessor: {predecessor}")
    if predecessor_id is not None:
        lines.append(f"predecessor_id: {predecessor_id}")
    if handoff_id is not None:
        lines.append(f"handoff_id: {handoff_id}")
    if deployment_state is not None:
        lines.append(f"deployment_state: {deployment_state}")
    if status is not None:
        lines.append(f"status: {status}")
    lines.append("---")
    lines.append("# handoff")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_tip_when_no_successor(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    lone = root / "state" / "handoffs" / "lone.md"
    _write_handoff(lone, predecessor="none")

    result = baton_drift_sweep(root)

    assert result["total_live"] == 1
    assert result["non_terminal"] == 1
    assert result["held"] == 0
    assert result["stranded"] == 0
    assert result["tips"] == 1


def test_held_when_live_successor_references_it(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    child = root / "state" / "handoffs" / "child.md"
    _write_handoff(parent, predecessor="none")
    _write_handoff(child, predecessor="parent.md")

    result = baton_drift_sweep(root)

    assert result["held"] == 1
    assert result["tips"] == 1  # child itself has no successor
    assert result["stranded"] == 0


# ---------------------------------------------------------------------------
# Review: code-reviewer — F2. baton_drift_sweep's `reverse_membership` call
# (the HELD test) relies on unpinned/implicit `handoff_dir` inference —
# unlike the sibling `referenced_by` call in the same loop body, which passes
# `handoff_dir` explicitly. This currently works only because
# `_collect_handoff_paths` happens to enumerate `state/handoffs/` entries
# before `archive/handoffs/**` ones and never sorts the combined list, so
# `dag_index[0]` is guaranteed to sit in `state/handoffs/`. Widening
# `reverse_membership`'s own signature to accept an explicit `handoff_dir`
# (mirroring `referenced_by`) touches `coordinator_core/archival.py`, which
# is outside this dispatch's write-scope (completion_ops.py /
# reconcile-completion-commits.py / baton_drift_sweep.py / their CLIs and
# tests only) — see the review-integration report for the disposition. This
# regression test is the in-scope half of the reviewer's suggested fix: pin
# the invariant that HELD classification does not depend on dag_index
# ordering, so a future reorder (or refactor of _collect_handoff_paths) that
# actually breaks it fails loud here.
# ---------------------------------------------------------------------------


def test_reverse_membership_held_classification_independent_of_dag_index_order(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    child = root / "state" / "handoffs" / "child.md"
    archived = root / "archive" / "handoffs" / "2026-06" / "old.md"

    _write_handoff(parent, predecessor="none")
    _write_handoff(child, predecessor="parent.md")
    _write_handoff(archived, predecessor="none", deployment_state="shipped")

    dag_index_state_first = [str(child), str(parent), str(archived)]
    dag_index_archive_first = [str(archived), str(child), str(parent)]

    live_children_state_first = reverse_membership(
        str(parent), dag_index_state_first, edge_kinds={"predecessor"}
    )
    live_children_archive_first = reverse_membership(
        str(parent), dag_index_archive_first, edge_kinds={"predecessor"}
    )

    assert live_children_state_first == frozenset({str(child)})
    assert live_children_archive_first == live_children_state_first, (
        "reverse_membership's HELD classification depends on dag_index ordering"
    )


def test_stranded_when_only_terminal_successor_references_it(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    # archival._is_terminal_or_archived_child keys off `status:` (consumed /
    # superseded / claimed / abandoned) — a distinct axis from
    # baton_drift_sweep's own `deployment_state:` terminal check on the
    # PARENT. `status: superseded` is terminal unconditionally (no
    # deployment_state:in_flight carve-out — see archival.py's own header).
    #
    # C5: `status="claimed"` on the PARENT is load-bearing post-split — this
    # test pins the successor-terminal shape landing in `stranded` (the
    # claimed-or-shipped bucket), not `never_started`; see
    # test_never_started_when_parent_was_never_claimed below for the sibling
    # case with an unclaimed parent.
    child = root / "state" / "handoffs" / "child.md"
    _write_handoff(parent, predecessor="none", status="claimed")
    _write_handoff(child, predecessor="parent.md", status="superseded")

    result = baton_drift_sweep(root)

    assert result["stranded"] == 1
    assert result["stranded_paths"] == [str(parent.resolve())]
    assert result["held"] == 0
    assert result["never_started"] == 0


# ---------------------------------------------------------------------------
# C1 (AC1) — STRANDED via an ARCHIVED successor, not a terminal-status one.
# docs/plans/2026-08-05-stranded-baton-drainage-make-the-detecto.md § Anti-scope:
# the existing test_stranded_when_only_terminal_successor_references_it uses a
# successor terminal by `status:` alone, still resident under state/handoffs/.
# This fixture's successor is terminal by ARCHIVE RESIDENCY alone (no status,
# no deployment_state on the child) — pinning the shape a "restrict the DAG
# index to live handoffs" rewrite would silently stop detecting.
# ---------------------------------------------------------------------------


def test_stranded_when_only_successor_is_archived(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    child = root / "archive" / "handoffs" / "2026-07" / "child.md"
    # C5: claimed, so this successor-terminal shape lands in `stranded`
    # (drainable), not `never_started` — see module docstring's third axis.
    _write_handoff(parent, predecessor="none", status="claimed")
    _write_handoff(child, predecessor="parent.md")

    result = baton_drift_sweep(root)

    assert result["stranded"] == 1
    assert result["stranded_paths"] == [str(parent.resolve())]
    assert result["held"] == 0
    assert result["never_started"] == 0


# ---------------------------------------------------------------------------
# C1 (AC2) — handoff_id / predecessor_id resolution. Every fixture above uses
# filename-shaped predecessor: values; build_handoff_id_index and
# resolve_target's id_index tier (dag.py) are otherwise entirely untested by
# this suite.
# ---------------------------------------------------------------------------


def test_held_when_successor_references_it_via_predecessor_id(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    child = root / "state" / "handoffs" / "child.md"
    _write_handoff(parent, predecessor="none", handoff_id="hnd-parent-a1b2c3")
    _write_handoff(child, predecessor=None, predecessor_id="hnd-parent-a1b2c3")

    result = baton_drift_sweep(root)

    assert result["held"] == 1
    assert result["stranded"] == 0
    assert result["tips"] == 1  # child itself has no successor


def test_stranded_when_only_successor_via_predecessor_id_is_terminal(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    child = root / "state" / "handoffs" / "child.md"
    # C5: claimed, so this successor-terminal shape lands in `stranded`
    # (drainable), not `never_started`.
    _write_handoff(
        parent, predecessor="none", handoff_id="hnd-parent-a1b2c3", status="claimed"
    )
    _write_handoff(
        child,
        predecessor=None,
        predecessor_id="hnd-parent-a1b2c3",
        status="superseded",
    )

    result = baton_drift_sweep(root)

    assert result["stranded"] == 1
    assert result["stranded_paths"] == [str(parent.resolve())]
    assert result["held"] == 0
    assert result["never_started"] == 0


# ---------------------------------------------------------------------------
# C1 (AC3) — unresolvable-ref basename fallback. dag.referenced_by falls back
# to `os.path.basename(raw_ref) == target_basename` only when resolve_target
# exhausts all three tiers (live path, on-disk archive, git-history) and
# returns None. This is unreachable via baton_drift_sweep's own STRANDED path
# (its target is always a state/handoffs file, and resolve_target's
# root-anchored `repo_root/state/handoffs/<basename>` tier resolves any
# same-basename ref to that file before the None branch is ever reached) —
# pinned directly against dag.referenced_by, mirroring this file's existing
# direct-primitive-test idiom (see the dag_index-ordering test above).
# ---------------------------------------------------------------------------


def test_referenced_by_falls_back_to_basename_when_target_unresolvable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    handoff_dir = root / "state" / "handoffs"
    # Deliberately OUTSIDE any convention resolve_target's tiers know how to
    # find (not state/handoffs, not archive/handoffs, not git-history) —
    # resolve_target must exhaust every tier and return None for any ref
    # naming it, no matter the ref's basename.
    target = root / "custom" / "weird_location" / "special-target.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\npredecessor: none\n---\n# target\n", encoding="utf-8")

    child = handoff_dir / "child.md"
    _write_handoff(child, predecessor="special-target.md")

    result = referenced_by(
        str(target),
        [str(child)],
        edge_kinds={"predecessor"},
        handoff_dir=str(handoff_dir),
    )

    assert result["referenced"] is True
    assert result["referencedBy"] == [str(child)]


def test_referenced_by_basename_fallback_does_not_false_match(tmp_path: Path) -> None:
    """The fallback's basename comparison must not fire for an unrelated ref —
    a genuinely dangling/unresolvable pointer to a DIFFERENT name must not be
    misread as a reference to the target under test."""
    root = tmp_path / "repo"
    handoff_dir = root / "state" / "handoffs"
    target = root / "custom" / "weird_location" / "special-target.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\npredecessor: none\n---\n# target\n", encoding="utf-8")

    child = handoff_dir / "child.md"
    _write_handoff(child, predecessor="some-other-name.md")

    result = referenced_by(
        str(target),
        [str(child)],
        edge_kinds={"predecessor"},
        handoff_dir=str(handoff_dir),
    )

    assert result["referenced"] is False
    assert result["referencedBy"] == []


# ---------------------------------------------------------------------------
# C5 — split STRANDED into drainable (claimed-or-shipped) vs never_started
# (never claimed, never shipped). docs/plans/2026-08-05-stranded-baton-
# drainage-make-the-detecto.md § C4 result / § C5. Uses the SAME predicate
# `handoff.archive_transition` mode=supersede's own DR-242 refusal site
# imports (`claimed_or_shipped_at_path`) — these fixtures exercise the two
# outcomes of that boolean, not a re-derivation of it.
# ---------------------------------------------------------------------------


def test_never_started_when_parent_was_never_claimed(tmp_path: Path) -> None:
    """Successor-terminal shape identical to test_stranded_when_only_
    successor_is_archived, but the parent carries no status/claimed_at/
    claimed_by/consumed_at/consumed_by/shipped_in and no terminal
    deployment_state — never claimed, never shipped. Must classify as
    never_started, not stranded, and must not double-count into stranded."""
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    child = root / "archive" / "handoffs" / "2026-07" / "child.md"
    _write_handoff(parent, predecessor="none")
    _write_handoff(child, predecessor="parent.md")

    result = baton_drift_sweep(root)

    assert result["never_started"] == 1
    assert result["never_started_paths"] == [str(parent.resolve())]
    assert result["stranded"] == 0
    assert result["stranded_paths"] == []
    assert result["held"] == 0


def test_stranded_and_never_started_never_double_count(tmp_path: Path) -> None:
    """One claimed (drainable) parent and one never-claimed (never_started)
    parent, each with its own terminal successor — each must land in
    exactly one bucket, and the two buckets must not overlap."""
    root = tmp_path / "repo"
    claimed_parent = root / "state" / "handoffs" / "claimed-parent.md"
    claimed_child = root / "state" / "handoffs" / "claimed-child.md"
    unclaimed_parent = root / "state" / "handoffs" / "unclaimed-parent.md"
    unclaimed_child = root / "state" / "handoffs" / "unclaimed-child.md"

    _write_handoff(claimed_parent, predecessor="none", status="claimed")
    _write_handoff(claimed_child, predecessor="claimed-parent.md", status="superseded")
    _write_handoff(unclaimed_parent, predecessor="none")
    _write_handoff(unclaimed_child, predecessor="unclaimed-parent.md", status="superseded")

    result = baton_drift_sweep(root)

    assert result["stranded"] == 1
    assert result["stranded_paths"] == [str(claimed_parent.resolve())]
    assert result["never_started"] == 1
    assert result["never_started_paths"] == [str(unclaimed_parent.resolve())]
    assert set(result["stranded_paths"]) & set(result["never_started_paths"]) == set()


def test_no_open_handoffs_dir_returns_zeroed_result(tmp_path: Path) -> None:
    # C5: this asserts the EXACT full result dict — adding `never_started` /
    # `never_started_paths` keys MUST fail it until updated here. That is
    # the shape guard doing its job (docs/plans/2026-08-05-stranded-baton-
    # drainage-make-the-detecto.md § C5); updated deliberately, not worked
    # around.
    root = tmp_path / "repo"
    result = baton_drift_sweep(root)

    assert result == {
        "total_live": 0,
        "terminal_not_archived": 0,
        "non_terminal": 0,
        "held": 0,
        "stranded": 0,
        "stranded_paths": [],
        "never_started": 0,
        "never_started_paths": [],
        "tips": 0,
        "reconciled_no_successor": 0,
        "reconciled_no_successor_paths": [],
        "reaped_orphan": 0,
        "reaped_orphan_paths": [],
        "desynced": 0,
        "desynced_paths": [],
    }


# ---------------------------------------------------------------------------
# SECOND LEG — reconciled-to-terminal, no successor (durable audit-record
# evidence). cross-repo/inbox/2026-08-04-example-market-data-repo-em-baton-
# terminal-state-not-cleared-programmatically.md, defect 1, item 3.
# ---------------------------------------------------------------------------


def _write_audit(
    path: Path,
    *,
    kind: str = "audit-record",
    body_mentions: str = "",
) -> None:
    lines = [
        "---",
        'title: "Test audit"',
        "created: 2026-08-04",
        f"kind: {kind}",
        "---",
        "# Test audit",
        "",
        body_mentions,
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_reconciled_no_successor_promoted_out_of_tips(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    lone = root / "state" / "handoffs" / "2026-08-03_223833_qsent-05.md"
    _write_handoff(lone, predecessor="none", status="open", deployment_state="ready_to_fire")
    _write_audit(
        root / "state" / "audits" / "2026-08-04-qsent-05-baton-reconciled-closed.md",
        body_mentions="Reconciled state/handoffs/2026-08-03_223833_qsent-05.md to terminal.",
    )

    result = baton_drift_sweep(root)

    assert result["tips"] == 0
    assert result["stranded"] == 0
    assert result["held"] == 0
    assert result["reconciled_no_successor"] == 1
    assert result["reconciled_no_successor_paths"] == [str(lone.resolve())]


def test_reconciled_no_successor_ignores_wrong_kind(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    lone = root / "state" / "handoffs" / "2026-08-03_223833_qsent-05.md"
    _write_handoff(lone, predecessor="none")
    _write_audit(
        root / "state" / "audits" / "2026-08-04-qsent-05-baton-reconciled-closed.md",
        kind="audit",
        body_mentions="Reconciled state/handoffs/2026-08-03_223833_qsent-05.md to terminal.",
    )

    result = baton_drift_sweep(root)

    assert result["reconciled_no_successor"] == 0
    assert result["tips"] == 1


def test_reconciled_no_successor_ignores_wrong_filename_suffix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    lone = root / "state" / "handoffs" / "2026-08-03_223833_qsent-05.md"
    _write_handoff(lone, predecessor="none")
    _write_audit(
        root / "state" / "audits" / "2026-08-04-qsent-05-unrelated-audit.md",
        body_mentions="Reconciled state/handoffs/2026-08-03_223833_qsent-05.md to terminal.",
    )

    result = baton_drift_sweep(root)

    assert result["reconciled_no_successor"] == 0
    assert result["tips"] == 1


def test_reconciled_no_successor_does_not_double_count_stranded(tmp_path: Path) -> None:
    """A baton with a genuine (terminal) successor reference stays STRANDED —
    the presence of a qualifying audit record for it must not also promote
    it into reconciled_no_successor (mutual exclusion, module docstring)."""
    root = tmp_path / "repo"
    parent = root / "state" / "handoffs" / "parent.md"
    child = root / "state" / "handoffs" / "child.md"
    # C5: claimed, so this lands in `stranded`, not `never_started` — the
    # mutual-exclusion claim under test is against `reconciled_no_successor`,
    # orthogonal to the claimed-or-shipped axis.
    _write_handoff(parent, predecessor="none", status="claimed")
    _write_handoff(child, predecessor="parent.md", status="superseded")
    _write_audit(
        root / "state" / "audits" / "2026-08-04-parent-baton-reconciled-closed.md",
        body_mentions="Reconciled state/handoffs/parent.md to terminal.",
    )

    result = baton_drift_sweep(root)

    assert result["stranded"] == 1
    assert result["never_started"] == 0
    assert result["reconciled_no_successor"] == 0


def test_reconciled_no_successor_does_not_flag_genuinely_live_baton(tmp_path: Path) -> None:
    """A live baton with no matching audit record must never be swept up —
    over-eager archival loses work (the dangerous direction)."""
    root = tmp_path / "repo"
    lone = root / "state" / "handoffs" / "2026-08-04-genuinely-live.md"
    _write_handoff(lone, predecessor="none")
    _write_audit(
        root / "state" / "audits" / "2026-08-04-some-other-baton-reconciled-closed.md",
        body_mentions="Reconciled state/handoffs/2026-08-04-some-other-baton.md to terminal.",
    )

    result = baton_drift_sweep(root)

    assert result["reconciled_no_successor"] == 0
    assert result["tips"] == 1


def test_no_audits_dir_is_a_clean_no_op(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    lone = root / "state" / "handoffs" / "lone.md"
    _write_handoff(lone, predecessor="none")

    result = baton_drift_sweep(root)

    assert result["reconciled_no_successor"] == 0
    assert result["tips"] == 1


# ---------------------------------------------------------------------------
# C1 — a reaped chain tip is invisible to baton_drift_sweep TODAY.
# docs/plans/2026-08-05-reaper-preserves-closure-evidence.md § AC1/AC2. Written
# and passing against the CURRENT implementation (no `reaped_orphan` bucket
# exists yet) — these tests document the gap, they do not rationalise C4.
# Both fixtures are non-terminal, live, chain-tip handoffs (no successor
# references them, nothing distinguishes them from a genuinely live baton in
# today's classification loop) that were, in fact, reaped from a dead
# session's crash-orphaned claim.
# ---------------------------------------------------------------------------


def _write_reaped_tip(
    path: Path,
    *,
    park_note: str,
    reaped_from_session: str | None = None,
) -> None:
    lines = ["---", "predecessor: none", f'park_note: "{park_note}"']
    if reaped_from_session is not None:
        lines.append(f"reaped_from_session: {reaped_from_session}")
    lines.append("---")
    lines.append("# handoff")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_legacy_reaped_tip_with_no_reaped_from_session_stays_tips(
    tmp_path: Path,
) -> None:
    """Legacy shape: a live, non-terminal, chain-tip handoff carrying a
    crash-orphan park_note but no reaped_from_session and no consumed_by —
    nothing NAMES it as a predecessor's reaped successor. This test proves
    that this fixture classifies as `tips` today AND stays `tips`
    permanently, even after C4 lands and adds the reaped_orphan bucket —
    C4's classification key is the presence of reaped_from_session, which
    this fixture never carries and never will (the field is not backfilled
    onto records that never recorded a parseable sid). This permanence is
    the read-side motivation cited for C5's backfill (see that slice for
    the actual backfill write path and its own coverage) — it is not itself
    proof that C5's backfill closes the gap; this test only exercises the
    pre/post read-side classification shown here.
    # Review: coordinator:code-reviewer — narrowed to what this test proves; the prior
    # wording asserted C5's backfill was justified, which this slice cannot discharge.
    """
    root = tmp_path / "repo"
    lone = root / "state" / "handoffs" / "legacy-reaped.md"
    _write_reaped_tip(
        lone,
        park_note="claim released by crash-orphan reaper — session sid-dead-abc123 no longer alive",
    )

    result = baton_drift_sweep(root)

    assert result["total_live"] == 1
    assert result["non_terminal"] == 1
    assert result["held"] == 0
    assert result["stranded"] == 0
    assert result["tips"] == 1


def test_post_fix_reaped_tip_with_reaped_from_session_classifies_as_reaped_orphan(
    tmp_path: Path,
) -> None:
    """Post-fix shape: the same live, non-terminal, chain-tip handoff, but
    carrying reaped_from_session (as C2/C3 will write going forward at the
    point a producer unclaims a dead session's crash-orphaned tip).
    C4 (docs/plans/2026-08-05-reaper-preserves-closure-evidence.md § AC10)
    adds the `reaped_orphan` bucket keyed on this field (with no active
    claim) — this is the ONE fixture that flips out of `tips`; the legacy
    fixture above never flips (see its own docstring)."""
    root = tmp_path / "repo"
    lone = root / "state" / "handoffs" / "post-fix-reaped.md"
    _write_reaped_tip(
        lone,
        park_note="claim released by crash-orphan reaper — session sid-dead-xyz789 no longer alive",
        reaped_from_session="sid-dead-xyz789",
    )

    result = baton_drift_sweep(root)

    assert result["total_live"] == 1
    assert result["non_terminal"] == 1
    assert result["held"] == 0
    assert result["stranded"] == 0
    assert result["tips"] == 0
    assert result["reaped_orphan"] == 1
    assert result["reaped_orphan_paths"] == [str(lone.resolve())]


# ---------------------------------------------------------------------------
# C4 — precedence between RECONCILED_NO_SUCCESSOR and REAPED_ORPHAN when a
# baton qualifies for both. docs/plans/2026-08-05-reaper-preserves-closure-
# evidence.md § AC10: reconciled evidence (a human/session conclusion the
# work is done) is the stronger signal and wins.
# ---------------------------------------------------------------------------


def test_reconciled_no_successor_takes_precedence_over_reaped_orphan(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    lone = root / "state" / "handoffs" / "2026-08-03_223833_qsent-05.md"
    _write_reaped_tip(
        lone,
        park_note="claim released by crash-orphan reaper — session sid-dead-abc123 no longer alive",
        reaped_from_session="sid-dead-abc123",
    )
    _write_audit(
        root / "state" / "audits" / "2026-08-04-qsent-05-baton-reconciled-closed.md",
        body_mentions="Reconciled state/handoffs/2026-08-03_223833_qsent-05.md to terminal.",
    )

    result = baton_drift_sweep(root)

    assert result["reconciled_no_successor"] == 1
    assert result["reconciled_no_successor_paths"] == [str(lone.resolve())]
    assert result["reaped_orphan"] == 0
    assert result["reaped_orphan_paths"] == []
    assert result["tips"] == 0


def test_reaped_orphan_drains_when_active_claim_present(tmp_path: Path) -> None:
    """A re-picked-up baton (active claim present) must NOT land in
    reaped_orphan even though reaped_from_session is still on it — `_claim`
    does not strip that field on re-pickup (module docstring)."""
    root = tmp_path / "repo"
    lone = root / "state" / "handoffs" / "reclaimed.md"
    lines = [
        "---",
        "predecessor: none",
        "reaped_from_session: sid-dead-abc123",
        "claimed_by: sid-live-def456",
        "---",
        "# handoff",
    ]
    lone.parent.mkdir(parents=True, exist_ok=True)
    lone.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = baton_drift_sweep(root)

    assert result["reaped_orphan"] == 0
    assert result["reaped_orphan_paths"] == []
    assert result["tips"] == 1


# ---------------------------------------------------------------------------
# C6/AC11 — baton_drift_sweep writes nothing. A REAL read-only assertion (file
# content AND mtime unchanged), not a diff-read.
# docs/plans/2026-08-05-reaper-preserves-closure-evidence.md § AC11.
# ---------------------------------------------------------------------------


def test_sweep_is_read_only_content_and_mtime_unchanged(tmp_path: Path) -> None:
    """baton_drift_sweep is a diagnostic (module docstring): it must not
    mutate any file it walks. Cover every population the sweep classifies —
    tips, held, stranded, reconciled_no_successor, reaped_orphan — so a write
    hiding behind any single branch would be caught."""
    root = tmp_path / "repo"

    tip = root / "state" / "handoffs" / "tip.md"
    _write_handoff(tip, predecessor="none")

    parent = root / "state" / "handoffs" / "parent.md"
    child = root / "state" / "handoffs" / "child.md"
    _write_handoff(parent, predecessor="none", handoff_id="parent-1")
    _write_handoff(child, predecessor="parent-1")

    reaped = root / "state" / "handoffs" / "reaped.md"
    _write_reaped_tip(
        reaped,
        park_note="claim released by crash-orphan reaper — session sid-dead-mtime123 no longer alive",
        reaped_from_session="sid-dead-mtime123",
    )

    reconciled = root / "state" / "handoffs" / "2026-08-03_223833_qsent-mtime.md"
    _write_reaped_tip(
        reconciled,
        park_note="claim released by crash-orphan reaper — session sid-dead-recon456 no longer alive",
        reaped_from_session="sid-dead-recon456",
    )
    _write_audit(
        root / "state" / "audits" / "2026-08-04-qsent-mtime-baton-reconciled-closed.md",
        body_mentions="Reconciled state/handoffs/2026-08-03_223833_qsent-mtime.md to terminal.",
    )

    all_paths = [
        tip,
        parent,
        child,
        reaped,
        reconciled,
        root / "state" / "audits" / "2026-08-04-qsent-mtime-baton-reconciled-closed.md",
    ]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in all_paths}

    result = baton_drift_sweep(root)

    assert result["total_live"] >= 1  # sweep actually walked the fixtures
    for p in all_paths:
        content_after, mtime_after = p.read_bytes(), p.stat().st_mtime_ns
        assert content_after == before[p][0], f"{p} content changed"
        assert mtime_after == before[p][1], f"{p} mtime changed"
