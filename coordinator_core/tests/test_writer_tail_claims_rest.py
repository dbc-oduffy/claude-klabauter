"""
coordinator_core.tests.test_writer_tail_claims_rest — AC5 for C7 (migration
batch C: every other package's to-fix writers onto the seam).

One behavioural test per distinct (entry-path class x seam entry point) pair
this batch migrated, driven through its real entry, asserting the claim
lands. Every migration in this batch is the same entry-path class —
"direct-call function body" (a plain Python call, no CLI/hook process
boundary in between, matching C5's ``append_goal`` shape) — so the three
tests below (one per seam entry point this batch used) cover all ten
migrated modules' pairs:

  - ``append_claimed_line``: represented here by
    ``backlog_grind_assemble.apply._append_backlog_note``. Same pair also
    covers ``engine_provenance_counter.record_engine_provenance``,
    ``group_em.send_pass._record_offer``/``record_offers``/``decline``, and
    ``telemetry.cost_census._append_row``.
  - ``replace_text``: represented here by
    ``fact_contract_gate.engine_gap_ratchet.write_baseline``. Same pair also
    covers ``distill.wiki_log_migrate.migrate_wiki_log``,
    ``tracker_store.rotate_month``, and
    ``workstream_complete.directives_commit_tail.revert_ship_stamps``/
    ``revert_close_stamps``.
  - ``create_exclusive``: represented here by
    ``roadmap.blitz_land.mint_replan_baton``.

Watched RED with each represented seam call swapped back for the raw
primitive it replaced (``_append_backlog_note``'s
``log_path.open("a") + fh.write``, ``write_baseline``'s
``baseline_path.write_text(...)``, ``mint_replan_baton``'s
``out.write_text(...)``) — none of those raw shapes call ``declare_write``,
so the collected-declarations list below stays empty.

Spec backlink: docs/plans/2026-09-11-state-writers-claim-through-one-seam.md
§ C7.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.backlog_grind_assemble import apply as backlog_grind_apply
from coordinator_core.roadmap import blitz_land
from coordinator_core.session import declared_writes

pytestmark = [pytest.mark.cadence]


def test_append_backlog_note_claims_through_the_seam(tmp_path):
    """entry-path class: direct-call function body x seam entry point:
    append_claimed_line. Drives ``_append_backlog_note`` (real entry) inside
    a ``declared_writes.collecting()`` scope and asserts the append is
    declared."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with declared_writes.collecting() as declared:
        backlog_grind_apply._append_backlog_note(repo_root, "claim lands via the seam")

    log_path = repo_root / backlog_grind_apply._NON_PASS_NOTE_LOG
    assert log_path.exists()
    assert "claim lands via the seam" in log_path.read_text(encoding="utf-8")

    assert str(log_path) in declared, (
        "backlog_grind_assemble.apply._append_backlog_note's write must be "
        "declared through the seam (session/claimed_write.py::"
        "append_claimed_line), not a raw open()+write that never calls "
        "declare_write"
    )


def test_write_baseline_claims_through_the_seam(tmp_path):
    """entry-path class: direct-call function body x seam entry point:
    replace_text. Drives ``write_baseline`` (real entry) inside a
    ``declared_writes.collecting()`` scope and asserts the replace is
    declared."""
    # `pytest.importorskip`, not a plain import, and not merely a
    # function-local one: `fact_contract_gate` is NOT in the published
    # engine's restricted tree, and this module IS published, so an UNGUARDED
    # import of it fails the publish import-closure gate and takes the whole
    # engine row down with it. Function-local is not a guard -- that gate's
    # own docstring records a prior version which exempted function-local
    # imports outright and laundered a real fatal-at-call-time defect past
    # itself. The discriminator is guarded vs unguarded. This form emits no
    # import statement at all: where the module is present (here) the test
    # runs unchanged, and where it is absent (the mirror, collect-only) this
    # one test skips instead of erroring.
    write_baseline = pytest.importorskip(
        "coordinator_core.fact_contract_gate.engine_gap_ratchet"
    ).write_baseline

    baseline_path = tmp_path / "state" / "fact-contract-gate" / "engine-gap-baseline.json"

    with declared_writes.collecting() as declared:
        write_baseline(baseline_path, 3, "claim lands via the seam")

    assert baseline_path.exists()
    assert "claim lands via the seam" in baseline_path.read_text(encoding="utf-8")

    assert str(baseline_path) in declared, (
        "fact_contract_gate.engine_gap_ratchet.write_baseline's write must "
        "be declared through the seam (session/claimed_write.py::"
        "replace_text), not a raw write_text() that never calls "
        "declare_write"
    )


def test_mint_replan_baton_claims_through_the_seam(tmp_path):
    """entry-path class: direct-call function body x seam entry point:
    create_exclusive. Drives ``mint_replan_baton`` (real entry) inside a
    ``declared_writes.collecting()`` scope and asserts the create is
    declared."""
    worktree_root = tmp_path / "repo"
    (worktree_root / "state" / "handoffs").mkdir(parents=True)

    with declared_writes.collecting() as declared:
        result = blitz_land.mint_replan_baton(
            worktree_root=worktree_root,
            title="claim lands via the seam",
            branch="feature/x",
            summary="a replan baton minted for this test",
            deliverable_id="dlv-test",
            handoff_id="hoff-test",
            source_baton_path="state/handoffs/source.md",
            brief="brief body",
        )

    out_path = worktree_root / result["path"]
    assert out_path.exists()

    assert str(out_path) in declared, (
        "roadmap.blitz_land.mint_replan_baton's write must be declared "
        "through the seam (session/claimed_write.py::create_exclusive), "
        "not a raw write_text() that never calls declare_write"
    )
