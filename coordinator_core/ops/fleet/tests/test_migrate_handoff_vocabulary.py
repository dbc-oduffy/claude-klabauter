"""
Fixture tests for coordinator_core.ops.fleet.migrate_handoff_vocabulary — the
DR-084 § C7 consumer-corpus migration op.

Spec backlink: pln-handoff-lifecycle-vocabulary-o-22ada6 § C7
"""
from __future__ import annotations

from pathlib import Path

import pathlib
import pytest

from coordinator_core.ops.fleet import migrate_handoff_vocabulary as mig


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _handoffs(tmp_path: Path) -> Path:
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "archive" / "handoffs").mkdir(parents=True)
    return tmp_path


def test_status_and_field_rename_mapping(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "a.md"
    _write(f, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: shipped\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-1\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    assert len(plan["records"]) == 1
    text = plan["records"][0]["_rebuilt"]
    assert "status: claimed" in text
    assert "claimed_at: '2026-01-01T00:00:00Z'" in text
    assert "claimed_by: sess-1" in text
    assert "consumed_at" not in text
    assert "consumed_by" not in text
    assert "deployment_state: shipped" in text
    assert "Body.\n" in text


def test_both_present_collision_new_wins_and_is_reported(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "a.md"
    _write(f, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: shipped\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nclaimed_at: '2026-02-02T00:00:00Z'\n"
        "consumed_by: old-sess\nclaimed_by: new-sess\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    rec = plan["records"][0]
    assert len(rec["collisions"]) == 2
    text = rec["_rebuilt"]
    assert "claimed_at: '2026-02-02T00:00:00Z'" in text
    assert "claimed_by: new-sess" in text
    assert "consumed_at" not in text
    assert "consumed_by" not in text


def test_abandoned_with_successor_becomes_continued(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    parent = root / "state" / "handoffs" / "parent.md"
    _write(parent, (
        "---\ntitle: p\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    child = root / "state" / "handoffs" / "child.md"
    _write(child, (
        "---\ntitle: c\nstatus: open\npredecessor: \"state/handoffs/parent.md\"\n"
        "handoff_id: hnd-child-abc123\ndeployment_state: in_flight\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    assert "state/handoffs/parent.md" in recs
    text = recs["state/handoffs/parent.md"]["_rebuilt"]
    assert "deployment_state: continued" in text
    assert "continued_into: hnd-child-abc123" in text


def test_abandoned_without_successor_becomes_closed_stale(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "lonely.md"
    _write(f, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    text = plan["records"][0]["_rebuilt"]
    assert "deployment_state: closed" in text
    assert "closed_reason: stale" in text
    assert "# migration: DR-084 consumer-corpus re-expression" in text
    assert "continued_into" not in text


def test_deployment_state_superseded_repaired_and_split_with_successor(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    parent = root / "state" / "handoffs" / "parent.md"
    _write(parent, (
        "---\ntitle: p\nstatus: consumed\ndeployment_state: superseded\n"
        "consumed_at: '2026-07-10T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    child = root / "state" / "handoffs" / "child.md"
    _write(child, (
        "---\ntitle: c\nstatus: open\npredecessor: \"state/handoffs/parent.md\"\n"
        "handoff_id: hnd-child-xyz789\ndeployment_state: in_flight\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    parent_rec = recs["state/handoffs/parent.md"]
    assert parent_rec["repair"] is True
    assert plan["repairs"] == ["state/handoffs/parent.md"]
    text = parent_rec["_rebuilt"]
    assert "deployment_state: continued" in text
    assert "continued_into: hnd-child-xyz789" in text
    assert "REPAIR" in text
    assert any("REPAIR" in c for c in parent_rec["changes"])


def test_deployment_state_superseded_repaired_and_closed_without_successor(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "lonely.md"
    _write(f, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: superseded\n"
        "consumed_at: '2026-07-10T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    rec = plan["records"][0]
    assert rec["repair"] is True
    assert plan["repairs"] == ["state/handoffs/lonely.md"]
    text = rec["_rebuilt"]
    assert "deployment_state: closed" in text
    assert "closed_reason: stale" in text
    assert "REPAIR" in text
    assert "2026-06-26" in text


def test_status_superseded_untouched_alongside_deployment_state_superseded_repair(
    tmp_path: Path,
) -> None:
    """The two 'superseded' values live on different axes and must never be
    confused: status: superseded stays untouched even on a record whose
    deployment_state: superseded gets repaired."""
    root = _handoffs(tmp_path)
    f = root / "archive" / "handoffs" / "both.md"
    _write(f, "---\ntitle: t\nstatus: superseded\ndeployment_state: superseded\n---\nBody.\n")
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    rec = plan["records"][0]
    assert rec["repair"] is True
    text = rec["_rebuilt"]
    assert "status: superseded" in text
    assert "status: open" not in text
    assert "status: claimed" not in text
    assert "deployment_state: closed" in text


def test_missing_deployment_state_reported_not_guessed(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "no-deployment.md"
    original = "---\ntitle: t\nstatus: consumed\n---\nBody.\n"
    _write(f, original)
    plan = mig.plan_migration(str(root))
    assert plan["records"] == []
    assert len(plan["failures"]) == 1
    assert "required-field defect" in plan["failures"][0]["reason"]
    assert f.read_text(encoding="utf-8") == original


def test_dry_run_plan_categorizes_repairs_separately_from_renames(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    renamed = root / "state" / "handoffs" / "renamed.md"
    _write(renamed, "---\ntitle: t\nstatus: active\ndeployment_state: shipped\n---\nBody.\n")
    repaired = root / "state" / "handoffs" / "repaired.md"
    _write(repaired, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: superseded\n"
        "consumed_at: '2026-07-10T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    assert len(plan["records"]) == 2
    assert plan["repairs"] == ["state/handoffs/repaired.md"]
    non_repair_paths = [r["path"] for r in plan["records"] if not r["repair"]]
    assert non_repair_paths == ["state/handoffs/renamed.md"]


def test_trailing_comment_on_status_is_parsed_not_refused(tmp_path: Path) -> None:
    """status: consumed  # note ... is legal YAML for 'consumed' — must not be
    reported unclassifiable (a hand-rolled split('#') would wrongly refuse
    this; must reuse dag._strip_inline_comment instead)."""
    root = _handoffs(tmp_path)
    f = root / "archive" / "handoffs" / "commented.md"
    _write(f, (
        "---\ntitle: t\nstatus: consumed  # reconciled 2026-07-17: already landed; "
        "do NOT re-run wsc_commit\ndeployment_state: shipped\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    assert len(plan["records"]) == 1
    assert plan["records"][0]["changes"] == ["status: consumed → claimed"]


def test_trailing_comment_on_deployment_state_is_parsed(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "commented2.md"
    _write(f, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: abandoned  # dead end\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    assert len(plan["records"]) == 1
    text = plan["records"][0]["_rebuilt"]
    assert "deployment_state: closed" in text


def test_hash_inside_quoted_scalar_is_not_a_comment(tmp_path: Path) -> None:
    """A '#' inside a quoted scalar is data, not a comment opener — this must
    stay unclassifiable (an invalid status value), not be silently accepted
    as a truncated/mangled token."""
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "quoted-hash.md"
    _write(f, '---\ntitle: t\nstatus: "consumed#not-a-comment"\ndeployment_state: shipped\n---\nBody.\n')
    plan = mig.plan_migration(str(root))
    assert plan["records"] == []
    assert len(plan["failures"]) == 1


def test_superseded_status_left_untouched(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "archive" / "handoffs" / "old.md"
    _write(f, (
        "---\ntitle: t\nstatus: superseded\ndeployment_state: shipped\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    assert plan["records"] == []


def test_hidden_archive_dir_is_scanned(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / ".archive" / "straggler.md"
    _write(f, (
        "---\ntitle: t\nstatus: active\ndeployment_state: shipped\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    assert len(plan["records"]) == 1
    assert plan["records"][0]["path"] == "state/handoffs/.archive/straggler.md"


def test_idempotent_second_run_is_a_no_op(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "a.md"
    _write(f, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    first = mig.plan_migration(str(root))
    assert len(first["records"]) == 1
    mig.apply_migration(first)

    second = mig.plan_migration(str(root))
    assert second["records"] == []
    assert second["failures"] == []


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "a.md"
    original = (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: shipped\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    )
    _write(f, original)
    plan = mig.plan_migration(str(root))
    assert len(plan["records"]) == 1
    assert f.read_text(encoding="utf-8") == original


def test_unclassifiable_status_fails_loud(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "bad.md"
    _write(f, "---\ntitle: t\nstatus: mystery\ndeployment_state: shipped\n---\nBody.\n")
    plan = mig.plan_migration(str(root))
    assert plan["records"] == []
    assert len(plan["failures"]) == 1
    assert "mystery" in plan["failures"][0]["reason"]
    assert f.read_text(encoding="utf-8") == (
        "---\ntitle: t\nstatus: mystery\ndeployment_state: shipped\n---\nBody.\n"
    )


def test_abandoned_no_reverse_lineage_falls_back_to_superseded_by(tmp_path: Path) -> None:
    """No predecessor/additional_predecessors edge names this record, but its
    own superseded_by resolves to exactly one on-disk corpus path — rung 2 of
    the ladder must pick it up rather than mis-closing as stale."""
    root = _handoffs(tmp_path)
    parent = root / "state" / "handoffs" / "parent.md"
    _write(parent, (
        "---\ntitle: p\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n"
        "superseded_by: state/handoffs/child.md\n---\nBody.\n"
    ))
    child = root / "state" / "handoffs" / "child.md"
    _write(child, (
        "---\ntitle: c\nstatus: open\nhandoff_id: hnd-child-fallback\n"
        "deployment_state: in_flight\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    text = recs["state/handoffs/parent.md"]["_rebuilt"]
    assert "deployment_state: continued" in text
    assert "continued_into: hnd-child-fallback" in text


def test_reverse_lineage_wins_over_superseded_by(tmp_path: Path) -> None:
    """Reverse-lineage stays rung 1 — a present-and-different superseded_by
    must NOT override it (byte-parity guard against the shipped C5+C8 pass)."""
    root = _handoffs(tmp_path)
    parent = root / "state" / "handoffs" / "parent.md"
    _write(parent, (
        "---\ntitle: p\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n"
        "superseded_by: state/handoffs/decoy.md\n---\nBody.\n"
    ))
    lineage_child = root / "state" / "handoffs" / "child.md"
    _write(lineage_child, (
        "---\ntitle: c\nstatus: open\npredecessor: \"state/handoffs/parent.md\"\n"
        "handoff_id: hnd-lineage-child\ndeployment_state: in_flight\n---\nBody.\n"
    ))
    decoy = root / "state" / "handoffs" / "decoy.md"
    _write(decoy, (
        "---\ntitle: d\nstatus: open\nhandoff_id: hnd-decoy\n"
        "deployment_state: in_flight\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    text = recs["state/handoffs/parent.md"]["_rebuilt"]
    assert "continued_into: hnd-lineage-child" in text
    assert "hnd-decoy" not in text


def test_prose_valued_superseded_by_falls_to_closed_stale(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "lonely.md"
    _write(f, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n"
        "superseded_by: superseded by the cloud-first work\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    text = plan["records"][0]["_rebuilt"]
    assert "deployment_state: closed" in text
    assert "closed_reason: stale" in text
    assert "continued_into" not in text


def test_superseded_by_multiple_or_zero_matches_falls_to_closed_stale(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    other_a = root / "state" / "handoffs" / "other-a.md"
    _write(other_a, "---\ntitle: a\nstatus: open\ndeployment_state: in_flight\n---\nBody.\n")
    other_b = root / "state" / "handoffs" / "other-b.md"
    _write(other_b, "---\ntitle: b\nstatus: open\ndeployment_state: in_flight\n---\nBody.\n")

    multi = root / "state" / "handoffs" / "multi.md"
    _write(multi, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n"
        "superseded_by: state/handoffs/other-a.md, state/handoffs/other-b.md\n---\nBody.\n"
    ))
    zero = root / "state" / "handoffs" / "zero.md"
    _write(zero, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n"
        "superseded_by: state/handoffs/nonexistent.md\n---\nBody.\n"
    ))

    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    for path in ("state/handoffs/multi.md", "state/handoffs/zero.md"):
        text = recs[path]["_rebuilt"]
        assert "deployment_state: closed" in text
        assert "closed_reason: stale" in text
        assert "continued_into" not in text


def test_superseded_by_with_trailing_comment_is_cleaned_and_resolves(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    parent = root / "state" / "handoffs" / "parent.md"
    _write(parent, (
        "---\ntitle: p\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n"
        "superseded_by: state/handoffs/child.md  # noted 2026-07-20\n---\nBody.\n"
    ))
    child = root / "state" / "handoffs" / "child.md"
    _write(child, (
        "---\ntitle: c\nstatus: open\nhandoff_id: hnd-child-commented\n"
        "deployment_state: in_flight\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    text = recs["state/handoffs/parent.md"]["_rebuilt"]
    assert "deployment_state: continued" in text
    assert "continued_into: hnd-child-commented" in text


def test_origin_handoff_reverse_edge_resolves_successor(tmp_path: Path) -> None:
    """A kind: spinoff carries predecessor: none by design and names its
    abandoned parent via origin_handoff: instead — the reverse walk must union
    that edge in, or every spinoff succession looks orphaned (dr084 memo #1)."""
    root = _handoffs(tmp_path)
    parent = root / "state" / "handoffs" / "parent.md"
    _write(parent, (
        "---\ntitle: p\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    child = root / "state" / "handoffs" / "child.md"
    _write(child, (
        "---\ntitle: c\nkind: spinoff\nstatus: open\npredecessor: none\n"
        "origin_handoff: \"state/handoffs/parent.md\"\n"
        "handoff_id: hnd-spinoff-child\ndeployment_state: in_flight\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    text = recs["state/handoffs/parent.md"]["_rebuilt"]
    assert "deployment_state: continued" in text
    assert "continued_into: hnd-spinoff-child" in text
    assert any("rung: reverse-lineage" in c for c in recs["state/handoffs/parent.md"]["changes"])


def test_deliverable_id_join_resolves_roadmap_stub_to_execution_baton(tmp_path: Path) -> None:
    """A spinoff-roadmap stub and its session-handoff execution baton share no
    lineage field at all — only a common deliverable_id. The third rung must
    join them (dr084 correction memo)."""
    root = _handoffs(tmp_path)
    stub = root / "state" / "handoffs" / "2026-07-07_214503_roadmap-clki-04.md"
    _write(stub, (
        "---\ntitle: stub\nkind: spinoff-roadmap\nstatus: consumed\n"
        "deployment_state: abandoned\ndeliverable_id: dlv-clki-04\n"
        "consumed_at: '2026-07-07T21:45:03Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    baton = root / "state" / "handoffs" / "2026-07-11_002100_execute-clki-04.md"
    _write(baton, (
        "---\ntitle: baton\nkind: session-handoff\nstatus: open\n"
        "deployment_state: in_flight\ndeliverable_id: dlv-clki-04\n"
        "handoff_id: hnd-execute-clki-04\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    stub_rec = recs["state/handoffs/2026-07-07_214503_roadmap-clki-04.md"]
    text = stub_rec["_rebuilt"]
    assert "deployment_state: continued" in text
    assert "continued_into: hnd-execute-clki-04" in text
    assert any("rung: deliverable_id-join" in c for c in stub_rec["changes"])


# Review (formerly AC6b, second half): sat-01's declared fork pair used to
# join across `dlv-sat-01` (winner) and the `-locke-02c8bc` loser leg via
# `state/deliverable-equivalence.yaml` + `canonicalize()`. That mechanism is
# condemned and collapsed to identity (plan
# 2026-08-20-the-close-ceremony-stops-paying-for-the-join, F-1); the
# cross-fork join it proved no longer exists, so genuinely different raw
# ids never join here regardless of any declared equivalence artifact.


def test_deliverable_id_join_two_candidates_falls_to_closed_stale(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    stub = root / "state" / "handoffs" / "2026-07-07_214503_roadmap.md"
    _write(stub, (
        "---\ntitle: stub\nkind: spinoff-roadmap\nstatus: consumed\n"
        "deployment_state: abandoned\ndeliverable_id: dlv-x\n"
        "consumed_at: '2026-07-07T21:45:03Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    baton_a = root / "state" / "handoffs" / "2026-07-11_002100_execute-a.md"
    _write(baton_a, (
        "---\ntitle: a\nkind: session-handoff\nstatus: open\n"
        "deployment_state: in_flight\ndeliverable_id: dlv-x\n"
        "handoff_id: hnd-execute-a\n---\nBody.\n"
    ))
    baton_b = root / "state" / "handoffs" / "2026-07-12_002100_execute-b.md"
    _write(baton_b, (
        "---\ntitle: b\nkind: session-handoff\nstatus: open\n"
        "deployment_state: in_flight\ndeliverable_id: dlv-x\n"
        "handoff_id: hnd-execute-b\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    text = recs["state/handoffs/2026-07-07_214503_roadmap.md"]["_rebuilt"]
    assert "deployment_state: closed" in text
    assert "closed_reason: stale" in text
    assert "continued_into" not in text


def test_deliverable_id_join_earlier_candidate_falls_to_closed_stale(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    stub = root / "state" / "handoffs" / "2026-07-11_214503_roadmap.md"
    _write(stub, (
        "---\ntitle: stub\nkind: spinoff-roadmap\nstatus: consumed\n"
        "deployment_state: abandoned\ndeliverable_id: dlv-x\n"
        "consumed_at: '2026-07-11T21:45:03Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    earlier_baton = root / "state" / "handoffs" / "2026-07-05_002100_execute.md"
    _write(earlier_baton, (
        "---\ntitle: e\nkind: session-handoff\nstatus: open\n"
        "deployment_state: in_flight\ndeliverable_id: dlv-x\n"
        "handoff_id: hnd-earlier\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    text = recs["state/handoffs/2026-07-11_214503_roadmap.md"]["_rebuilt"]
    assert "deployment_state: closed" in text
    assert "closed_reason: stale" in text
    assert "continued_into" not in text


def test_deliverable_id_join_candidate_not_session_handoff_falls_to_closed_stale(
    tmp_path: Path,
) -> None:
    root = _handoffs(tmp_path)
    stub = root / "state" / "handoffs" / "2026-07-07_214503_roadmap.md"
    _write(stub, (
        "---\ntitle: stub\nkind: spinoff-roadmap\nstatus: consumed\n"
        "deployment_state: abandoned\ndeliverable_id: dlv-x\n"
        "consumed_at: '2026-07-07T21:45:03Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    other_stub = root / "state" / "handoffs" / "2026-07-11_002100_other-roadmap.md"
    _write(other_stub, (
        "---\ntitle: o\nkind: spinoff-roadmap\nstatus: open\n"
        "deployment_state: in_flight\ndeliverable_id: dlv-x\n"
        "handoff_id: hnd-other-stub\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    text = recs["state/handoffs/2026-07-07_214503_roadmap.md"]["_rebuilt"]
    assert "deployment_state: closed" in text
    assert "closed_reason: stale" in text
    assert "continued_into" not in text


def test_deliverable_id_join_subject_not_roadmap_falls_to_closed_stale(tmp_path: Path) -> None:
    """This rung models exactly one shape — a spinoff-roadmap stub graduating
    into its baton — and must not fire on an arbitrary same-deliverable
    sibling of a different kind."""
    root = _handoffs(tmp_path)
    subject = root / "state" / "handoffs" / "2026-07-07_214503_not-a-stub.md"
    _write(subject, (
        "---\ntitle: s\nkind: session-handoff\nstatus: consumed\n"
        "deployment_state: abandoned\ndeliverable_id: dlv-x\n"
        "consumed_at: '2026-07-07T21:45:03Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    baton = root / "state" / "handoffs" / "2026-07-11_002100_execute.md"
    _write(baton, (
        "---\ntitle: b\nkind: session-handoff\nstatus: open\n"
        "deployment_state: in_flight\ndeliverable_id: dlv-x\n"
        "handoff_id: hnd-execute\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    text = recs["state/handoffs/2026-07-07_214503_not-a-stub.md"]["_rebuilt"]
    assert "deployment_state: closed" in text
    assert "closed_reason: stale" in text
    assert "continued_into" not in text


def test_rung_ordering_reverse_lineage_wins_over_deliverable_id_join(tmp_path: Path) -> None:
    """A record resolvable by BOTH reverse-lineage and deliverable_id-join must
    resolve via reverse-lineage — ordering is load-bearing, not a coincidence
    of which rung happens to run first."""
    root = _handoffs(tmp_path)
    stub = root / "state" / "handoffs" / "2026-07-07_214503_roadmap.md"
    _write(stub, (
        "---\ntitle: stub\nkind: spinoff-roadmap\nstatus: consumed\n"
        "deployment_state: abandoned\ndeliverable_id: dlv-x\n"
        "consumed_at: '2026-07-07T21:45:03Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    lineage_child = root / "state" / "handoffs" / "2026-07-08_002100_lineage-child.md"
    _write(lineage_child, (
        "---\ntitle: c\nkind: session-handoff\nstatus: open\n"
        "predecessor: \"state/handoffs/2026-07-07_214503_roadmap.md\"\n"
        "handoff_id: hnd-lineage-child\ndeployment_state: in_flight\n---\nBody.\n"
    ))
    deliverable_baton = root / "state" / "handoffs" / "2026-07-11_002100_execute.md"
    _write(deliverable_baton, (
        "---\ntitle: b\nkind: session-handoff\nstatus: open\n"
        "deployment_state: in_flight\ndeliverable_id: dlv-x\n"
        "handoff_id: hnd-deliverable-baton\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    stub_rec = recs["state/handoffs/2026-07-07_214503_roadmap.md"]
    text = stub_rec["_rebuilt"]
    assert "continued_into: hnd-lineage-child" in text
    assert "hnd-deliverable-baton" not in text
    assert any("rung: reverse-lineage" in c for c in stub_rec["changes"])


def test_claimed_by_session_id_match_is_not_a_succession_edge(tmp_path: Path) -> None:
    """Two records sharing only a claimed_by/consumed_by session id must not
    resolve a successor via that shared id — session co-presence is not a
    lineage claim (dr084 correction memo, rejected candidate #1)."""
    root = _handoffs(tmp_path)
    subject = root / "state" / "handoffs" / "lonely.md"
    _write(subject, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-shared\n---\nBody.\n"
    ))
    same_session = root / "state" / "handoffs" / "later.md"
    _write(same_session, (
        "---\ntitle: l\nstatus: open\ndeployment_state: in_flight\n"
        "claimed_by: sess-shared\nhandoff_id: hnd-later\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    text = recs["state/handoffs/lonely.md"]["_rebuilt"]
    assert "deployment_state: closed" in text
    assert "closed_reason: stale" in text
    assert "continued_into" not in text


def test_dangling_origin_handoff_target_does_not_raise(tmp_path: Path) -> None:
    """An origin_handoff: naming a path absent from the tree must not raise —
    the resolver returns None and the record falls through to closed+stale
    (dr084 correction memo, dangling-target tolerance note)."""
    root = _handoffs(tmp_path)
    orphan = root / "state" / "handoffs" / "orphan.md"
    _write(orphan, (
        "---\ntitle: o\nkind: spinoff\nstatus: open\npredecessor: none\n"
        "origin_handoff: \"state/handoffs/does-not-exist.md\"\n"
        "handoff_id: hnd-orphan\ndeployment_state: in_flight\n---\nBody.\n"
    ))
    subject = root / "state" / "handoffs" / "lonely.md"
    _write(subject, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    recs = {r["path"]: r for r in plan["records"]}
    text = recs["state/handoffs/lonely.md"]["_rebuilt"]
    assert "deployment_state: closed" in text
    assert "closed_reason: stale" in text


def test_dropped_status_comment_reported_verbatim(tmp_path: Path) -> None:
    """A live operational warning riding on a rewritten status: line must be
    surfaced in the report, not silently eaten (dr084 memo item #4)."""
    root = _handoffs(tmp_path)
    f = root / "archive" / "handoffs" / "warned.md"
    _write(f, (
        "---\ntitle: t\nstatus: consumed  # reconciled 2026-07-17: already landed; "
        "do NOT re-run wsc_commit\ndeployment_state: shipped\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    rec = plan["records"][0]
    assert len(rec["dropped_comments"]) == 1
    assert "do NOT re-run wsc_commit" in rec["dropped_comments"][0]
    assert rec["dropped_comments"][0].startswith("status:")


def test_idempotent_second_run_over_migrated_corpus_with_all_new_edges_is_a_no_op(
    tmp_path: Path,
) -> None:
    """Re-running over an already-migrated corpus (including records that used
    the origin_handoff and deliverable_id rungs) still reports zero changes."""
    root = _handoffs(tmp_path)
    stub = root / "state" / "handoffs" / "2026-07-07_214503_roadmap.md"
    _write(stub, (
        "---\ntitle: stub\nkind: spinoff-roadmap\nstatus: consumed\n"
        "deployment_state: abandoned\ndeliverable_id: dlv-x\n"
        "consumed_at: '2026-07-07T21:45:03Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    baton = root / "state" / "handoffs" / "2026-07-11_002100_execute.md"
    _write(baton, (
        "---\ntitle: b\nkind: session-handoff\nstatus: open\n"
        "deployment_state: in_flight\ndeliverable_id: dlv-x\n"
        "handoff_id: hnd-execute\n---\nBody.\n"
    ))
    spinoff_parent = root / "state" / "handoffs" / "spinoff-parent.md"
    _write(spinoff_parent, (
        "---\ntitle: p\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    spinoff_child = root / "state" / "handoffs" / "spinoff-child.md"
    _write(spinoff_child, (
        "---\ntitle: c\nkind: spinoff\nstatus: open\npredecessor: none\n"
        "origin_handoff: \"state/handoffs/spinoff-parent.md\"\n"
        "handoff_id: hnd-spinoff-child\ndeployment_state: in_flight\n---\nBody.\n"
    ))

    first = mig.plan_migration(str(root))
    assert not first["failures"]

    # AC1 (plan 2026-08-18-supersede-stamps-and-archives-atomically): this
    # fixture's abandoned records live under state/handoffs/, so migrating them
    # to `continued` stamps a terminal deployment_state on a RESIDENT record.
    # apply_migration refuses that without a repo_root to discharge the archival
    # against, rather than silently leaving the record loose on disk — the
    # refusal half of "the stamp and the archival are one operation, or the
    # writer refuses". Asserted here because this fixture is a plain directory
    # tree, not a git repo; the discharge path itself is covered against real
    # git in test_migrate_vocabulary_discharges_archival.py.
    with pytest.raises(ValueError, match="refusing to write"):
        mig.apply_migration(first)

    # Idempotency of the PLAN is what this test exists to pin, and it holds
    # independently of the write: re-planning the untouched corpus reports the
    # same work, and re-planning after the frontmatter is on disk reports none.
    replanned = mig.plan_migration(str(root))
    assert [r["path"] for r in replanned["records"]] == [r["path"] for r in first["records"]]
    assert replanned["failures"] == []

    for rec in first["records"]:
        pathlib.Path(rec["_abs_path"]).write_text(rec["_rebuilt"], encoding="utf-8", newline="")

    second = mig.plan_migration(str(root))
    assert second["records"] == []
    assert second["failures"] == []


def test_closed_unverified_bucket_names_no_successor_records(tmp_path: Path) -> None:
    """A no-successor abandoned record must be surfaced as its own
    closed_unverified bucket entry (example-cockpit-repo dr084 ask), carrying an
    explicit working-tree-walk-negative-result provenance comment — not a
    death claim — on the record itself, and the record dict's own
    closed_unverified flag must be set."""
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "lonely.md"
    _write(f, (
        "---\ntitle: t\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    assert plan["closed_unverified"] == ["state/handoffs/lonely.md"]
    rec = plan["records"][0]
    assert rec["closed_unverified"] is True
    text = rec["_rebuilt"]
    assert "working-tree walk" in text
    assert "NOT a death claim" in text
    assert "git blob" in text
    assert "git history" in text


def test_closed_unverified_bucket_empty_when_all_records_resolve(tmp_path: Path) -> None:
    """A corpus where every abandoned record resolves to a successor must
    report an empty closed_unverified bucket — the bucket names only the
    residual, unverified fall-through, not every touched record."""
    root = _handoffs(tmp_path)
    parent = root / "state" / "handoffs" / "parent.md"
    _write(parent, (
        "---\ntitle: p\nstatus: consumed\ndeployment_state: abandoned\n"
        "consumed_at: '2026-01-01T00:00:00Z'\nconsumed_by: sess-a\n---\nBody.\n"
    ))
    child = root / "state" / "handoffs" / "child.md"
    _write(child, (
        "---\ntitle: c\nstatus: open\npredecessor: \"state/handoffs/parent.md\"\n"
        "handoff_id: hnd-child-abc123\ndeployment_state: in_flight\n---\nBody.\n"
    ))
    plan = mig.plan_migration(str(root))
    assert not plan["failures"]
    assert plan["closed_unverified"] == []
    recs = {r["path"]: r for r in plan["records"]}
    assert recs["state/handoffs/parent.md"]["closed_unverified"] is False


def test_cli_main_dry_run_default_and_exit_code(tmp_path: Path) -> None:
    root = _handoffs(tmp_path)
    f = root / "state" / "handoffs" / "a.md"
    _write(f, "---\ntitle: t\nstatus: active\ndeployment_state: shipped\n---\nBody.\n")
    rc = mig.main(["--root", str(root)])
    assert rc == 0
    assert f.read_text(encoding="utf-8") == (
        "---\ntitle: t\nstatus: active\ndeployment_state: shipped\n---\nBody.\n"
    )

    rc_apply = mig.main(["--root", str(root), "--apply"])
    assert rc_apply == 0
    assert "status: open" in f.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Key-locator CRLF parity (2026-07-28 code review, finding 2)
#
# `_key_line_re`'s boundary lookahead was `(?=[ \t]|$)`, which rejects the `\r`
# of a present-but-empty `key:\r\n`. On a Windows-authored handoff the rename
# then could not locate a key `read_fm_field` reports as present — the
# migration raised (or, at the raw-insert call site, silently mis-anchored) on
# a file it should have rewritten. Widened to `(?=[ \t]|\r?$)`.
#
# Both endings are asserted on every case: an LF-only assertion passes against
# the unfixed regex and proves nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_rename_fm_key_finds_a_present_but_empty_key(eol: str) -> None:
    fm = f"title: t{eol}consumed_by:{eol}status: active{eol}"
    out = mig._rename_fm_key(fm, "consumed_by", "claimed_by")
    assert out == f"title: t{eol}claimed_by:{eol}status: active{eol}"


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_rename_fm_key_finds_a_valued_key(eol: str) -> None:
    fm = f"title: t{eol}consumed_by: sess-1{eol}"
    out = mig._rename_fm_key(fm, "consumed_by", "claimed_by")
    assert out == f"title: t{eol}claimed_by: sess-1{eol}"


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_rename_fm_key_still_raises_on_a_genuinely_absent_key(eol: str) -> None:
    fm = f"title: t{eol}status: active{eol}"
    with pytest.raises(ValueError, match="consumed_by"):
        mig._rename_fm_key(fm, "consumed_by", "claimed_by")


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_rename_fm_key_respects_the_prefix_boundary(eol: str) -> None:
    """`consumed_by` must not match `consumed_by_x:` — the guarantee the
    lookahead exists for, held under CRLF and against the newly-visible EMPTY
    `consumed_by_x:` shape."""
    fm = f"consumed_by_x: v{eol}consumed_by_x:{eol}"
    with pytest.raises(ValueError, match="consumed_by"):
        mig._rename_fm_key(fm, "consumed_by", "claimed_by")


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_insert_raw_line_anchors_on_a_present_but_empty_key(eol: str) -> None:
    """`_insert_raw_line_after` shares the same locator; a missed anchor there
    lands the migration-provenance comment at the wrong position instead."""
    fm = f"title: t{eol}status:{eol}other: v{eol}"
    out = mig._insert_raw_line_after(fm, "status", "# migration: x")
    assert out.splitlines()[2] == "# migration: x"
