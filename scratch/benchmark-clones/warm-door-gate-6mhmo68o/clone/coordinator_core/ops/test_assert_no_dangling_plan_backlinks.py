"""
Tests for coordinator_core.ops.assert_no_dangling_plan_backlinks.

Spec backlink: archive/specs/2026-06/2026-06-23-programmatic-terminal-plan-archival.md § AC9 / C6
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.assert_no_dangling_plan_backlinks import main, run_gate, scan_missing_ids
from coordinator_core.ops.spec_backlink_resolve import resolve

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _write(root: str, rel: str, content: str) -> None:
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)


def _fm(title: str, plan_id: str = None, deliverable_id: str = None) -> str:
    lines = ["---", f"title: {title}"]
    if plan_id is not None:
        lines.append(f"plan_id: {plan_id}")
    if deliverable_id is not None:
        lines.append(f"deliverable_id: {deliverable_id}")
    lines.append("---")
    lines.append(f"# {title}")
    lines.append("")
    return "\n".join(lines)


def _make_moved_plan_tree(root: str) -> None:
    # C5-fix: both records carry a real id -- mint-at-creation is wired into
    # main()'s default flow now, so a shared fixture without ids would trip
    # MISSING-ID on every test that reuses this helper.
    _write(
        root,
        "archive/specs/2026-01/2026-01-01-foo-plan.md",
        _fm("Foo plan", plan_id="pln-foo-plan-999001"),
    )
    _write(root, "archive/specs/2026-01/2026-01-01-foo-plan.review.md", "# sidecar\n")
    _write(
        root,
        "docs/plans/2026-01-02-bar-plan.md",
        _fm("Bar plan", plan_id="pln-bar-plan-999002"),
    )


def test_no_archive_specs_dir_returns_zero(tmp_path, capsys):
    root = str(tmp_path)
    rc = main(["--root", root])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no archive/specs" in out


def test_no_moved_plans_returns_zero(tmp_path, capsys):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "archive", "specs"))
    os.makedirs(os.path.join(root, "docs", "plans"))
    rc = main(["--root", root])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no moved plans" in out


def test_dangling_backlink_detected(tmp_path, capsys):
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    _write(
        root,
        "coordinator/wiki/citer1.md",
        "Some doc.\n\nspec_backlink: docs/plans/2026-01-01-foo-plan.md\n",
    )
    rc = main(["--root", root])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL: 1 dangling" in captured.err
    assert "DANGLING in: coordinator/wiki/citer1.md" in captured.err


def test_prose_mention_not_flagged(tmp_path, capsys):
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    _write(
        root,
        "coordinator/wiki/citer2.md",
        "Discussion referencing docs/plans/2026-01-01-foo-plan.md in prose, no convention line.\n",
    )
    rc = main(["--root", root])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK: no dangling" in out


def test_live_plan_citation_not_flagged(tmp_path, capsys):
    """C5-fix: bar-plan.md now carries a real id (mint-at-creation wiring),
    so citing it must use id-form -- a path-form citation would now
    correctly trip the (pre-existing, untouched) UNGRANDFATHERED-PATH axis,
    since bar-plan is indexed locally. This test's intent -- "a live plan
    citation resolves cleanly, not dangling" -- is preserved via id-form."""
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    _write(
        root,
        "coordinator/wiki/citer3.md",
        "spec_backlink: pln-bar-plan-999002\n",
    )
    rc = main(["--root", root])
    assert rc == 0


@pytest.mark.parametrize("excluded_dir", ["tasks", "state"])
def test_excluded_root_trees_not_flagged(tmp_path, excluded_dir):
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    _write(
        root,
        f"{excluded_dir}/citer.md",
        "spec_backlink: docs/plans/2026-01-01-foo-plan.md\n",
    )
    rc = main(["--root", root])
    assert rc == 0


def test_archive_tree_citation_not_flagged(tmp_path):
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    _write(
        root,
        "archive/other/citer.md",
        "spec_backlink: docs/plans/2026-01-01-foo-plan.md\n",
    )
    rc = main(["--root", root])
    assert rc == 0


@pytest.mark.parametrize("excluded_dir", ["build", "scratch", "scratchpad", "node_modules"])
def test_gitignored_build_scratch_trees_not_flagged(tmp_path, excluded_dir):
    """C5-fix gap 1: build/, scratch/, scratchpad/ are gitignored artifact
    trees (session snapshots, other sessions' baselines) -- a citation in
    them must not fail the gate. `tmp_path` is not itself a git worktree,
    so this exercises the `_EXCLUDED_ROOT_PREFIXES` backstop path (the
    fallback used when `_git_tracked_md_files` returns None)."""
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    _write(
        root,
        f"{excluded_dir}/citer.md",
        "spec_backlink: docs/plans/2026-01-01-foo-plan.md\n",
    )
    rc = main(["--root", root])
    assert rc == 0


def test_dedup_multiple_citations_same_file_same_plan(tmp_path, capsys):
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    _write(
        root,
        "coordinator/wiki/citer.md",
        (
            "spec_backlink: docs/plans/2026-01-01-foo-plan.md\n"
            "another line\n"
            "spec_backlink: docs/plans/2026-01-01-foo-plan.md\n"
        ),
    )
    rc = main(["--root", root])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL: 1 dangling" in captured.err


def test_fix_heals_and_rerun_is_clean(tmp_path, capsys):
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    target = "coordinator/wiki/citer1.md"
    _write(
        root,
        target,
        "Some doc.\n\nspec_backlink: docs/plans/2026-01-01-foo-plan.md\n",
    )
    rc = main(["--root", root, "--fix"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "healed 1 dangling" in captured.out

    with open(os.path.join(root, target), encoding="utf-8") as fh:
        healed = fh.read()
    assert "spec_backlink: archive/specs/2026-01/2026-01-01-foo-plan.md" in healed
    assert "docs/plans/2026-01-01-foo-plan.md" not in healed

    rc2 = main(["--root", root])
    assert rc2 == 0


def test_fix_leaves_prose_mention_untouched(tmp_path):
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    target = "coordinator/wiki/citer2.md"
    content = "Discussion referencing docs/plans/2026-01-01-foo-plan.md in prose, no convention line.\n"
    _write(root, target, content)
    main(["--root", root, "--fix"])
    with open(os.path.join(root, target), encoding="utf-8") as fh:
        assert fh.read() == content


def test_no_root_resolvable_returns_zero(tmp_path, monkeypatch, capsys):
    non_git_dir = tmp_path / "not-a-repo"
    non_git_dir.mkdir()
    monkeypatch.chdir(non_git_dir)
    monkeypatch.setattr(
        "coordinator_core.ops.assert_no_dangling_plan_backlinks._resolve_root",
        lambda explicit_root: None,
    )
    rc = main([])
    assert rc == 0


def test_sidecar_review_file_not_treated_as_moved_plan(tmp_path, capsys):
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    _write(
        root,
        "coordinator/wiki/citer.md",
        "spec_backlink: docs/plans/2026-01-01-foo-plan.review.md\n",
    )
    rc = main(["--root", root])
    assert rc == 0


@pytest.mark.skipif(os.name == "nt", reason="chmod-based unreadable-file fixture is POSIX-only")
def test_unreadable_candidate_file_fails_loud_even_with_zero_hits(tmp_path, capsys):
    """BEHAVIOUR CHANGE regression (2026-07-22): the AC9 gate must never
    report "no dangling backlinks" when a candidate file could not be
    scanned — a dangling backlink could be hiding inside it. Previously
    this silently returned 0."""
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    blocked_rel = "coordinator/wiki/blocked.md"
    _write(root, blocked_rel, "placeholder\n")
    blocked_path = os.path.join(root, blocked_rel)
    os.chmod(blocked_path, 0o000)
    try:
        rc = main(["--root", root])
    finally:
        os.chmod(blocked_path, 0o644)
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL:" in captured.err
    assert "could not be scanned" in captured.err
    assert "blocked.md" in captured.err


# ---------------------------------------------------------------------------
# C5: id-form axis + path-form-outside-grandfathering axis.
# ---------------------------------------------------------------------------


def test_id_form_resolving_to_nothing_fails(tmp_path, capsys):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "archive", "specs"))
    _write(
        root,
        "docs/plans/2026-02-01-baz-plan.md",
        _fm("Baz plan", plan_id="pln-baz-plan-111111"),
    )
    _write(
        root,
        "coordinator/wiki/citer.md",
        "spec_backlink: pln-does-not-exist-000000\n",
    )
    rc = main(["--root", root])
    captured = capsys.readouterr()
    assert rc == 1
    assert "UNRESOLVED-ID" in captured.err
    assert "pln-does-not-exist-000000" in captured.err
    assert "id-form spec_backlink citation" in captured.err


def test_id_form_resolving_to_ambiguity_fails(tmp_path, capsys):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "archive", "specs"))
    _write(
        root,
        "docs/plans/2026-02-02-dup-a.md",
        _fm("Dup A", deliverable_id="dlv-dup-abc123"),
    )
    _write(
        root,
        "docs/plans/2026-02-03-dup-b.md",
        _fm("Dup B", deliverable_id="dlv-dup-abc123"),
    )
    _write(
        root,
        "coordinator/wiki/citer.md",
        "spec_backlink: dlv-dup-abc123\n",
    )
    rc = main(["--root", root])
    captured = capsys.readouterr()
    assert rc == 1
    assert "UNRESOLVED-ID" in captured.err
    assert "ambiguity" in captured.err


def test_id_form_resolving_to_nothing_fails_with_no_archive_specs_dir(tmp_path, capsys):
    """Review-integration P1: the id-form axis must run even when
    archive/specs/ does not exist yet -- a corpus that has never archived a
    plan can still carry a dangling id-form citation, and the gate must not
    report OK on an axis it never evaluated."""
    root = str(tmp_path)
    assert not os.path.isdir(os.path.join(root, "archive", "specs"))
    _write(
        root,
        "coordinator/wiki/citer.md",
        "spec_backlink: pln-does-not-exist-000000\n",
    )
    rc = main(["--root", root])
    captured = capsys.readouterr()
    assert rc == 1
    assert "UNRESOLVED-ID" in captured.err
    assert "pln-does-not-exist-000000" in captured.err


def test_grandfathered_path_form_passes(tmp_path, capsys):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "archive", "specs"))
    os.makedirs(os.path.join(root, "docs", "plans"))
    _write(
        root,
        "coordinator/wiki/citer.md",
        "spec_backlink: docs/plans/2026-02-04-never-existed-locally.md\n",
    )
    rc = main(["--root", root])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out


def test_ungrandfathered_path_form_fails(tmp_path, capsys):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "archive", "specs"))
    _write(
        root,
        "docs/plans/2026-03-01-qux-plan.md",
        _fm("Qux plan", plan_id="pln-qux-plan-222222"),
    )
    _write(
        root,
        "coordinator/wiki/citer.md",
        "spec_backlink: docs/plans/2026-03-01-qux-plan.md\n",
    )
    rc = main(["--root", root])
    captured = capsys.readouterr()
    assert rc == 1
    assert "UNGRANDFATHERED-PATH" in captured.err
    assert "docs/plans/2026-03-01-qux-plan.md" in captured.err


# ---------------------------------------------------------------------------
# C5: mint-at-creation assertion (standalone -- not wired into main()'s
# default flow, see module docstring).
# ---------------------------------------------------------------------------


def test_scan_missing_ids_flags_plan_with_no_id(tmp_path):
    root = str(tmp_path)
    _write(root, "docs/plans/2026-04-01-no-id-plan.md", "---\ntitle: No id\n---\n# No id\n")
    missing = scan_missing_ids(root)
    assert "docs/plans/2026-04-01-no-id-plan.md" in missing


def test_scan_missing_ids_ignores_sidecar_and_undated_docs(tmp_path):
    root = str(tmp_path)
    _write(root, "docs/plans/INDEX.md", "# Index\n")
    _write(root, "docs/plans/README.md", "# Readme\n")
    _write(
        root,
        "docs/plans/2026-04-02-has-sidecar.md",
        _fm("Has sidecar", plan_id="pln-has-sidecar-333333"),
    )
    _write(
        root,
        "docs/plans/2026-04-02-has-sidecar.review.md",
        "# sidecar, no id\n",
    )
    missing = scan_missing_ids(root)
    assert missing == []


def test_default_flow_flags_new_plan_missing_id(tmp_path, capsys):
    """C5-fix gap 2: mint-at-creation must gate the default flow, not just
    --check-mint -- a new plan landing without plan_id/deliverable_id fails
    a plain `main(["--root", root])` invocation."""
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "archive", "specs"))
    _write(root, "docs/plans/2026-04-05-no-id-plan.md", "---\ntitle: No id\n---\n# No id\n")
    rc = main(["--root", root])
    captured = capsys.readouterr()
    assert rc == 1
    assert "MISSING-ID" in captured.err
    assert "2026-04-05-no-id-plan.md" in captured.err


def test_default_flow_ignores_review_sidecar_and_undated_doc(tmp_path):
    """C5-fix gap 2: the default-flow mint check must not fire on the
    legitimate exclusions the backfill itself never mints onto -- a review
    sidecar and an undated non-plan doc under docs/plans/."""
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "archive", "specs"))
    _write(root, "docs/plans/INDEX.md", "# Index\n")
    _write(
        root,
        "docs/plans/2026-04-06-has-id.md",
        _fm("Has id", plan_id="pln-has-id-555001"),
    )
    _write(root, "docs/plans/2026-04-06-has-id.review.md", "# sidecar, no id\n")
    rc = main(["--root", root])
    assert rc == 0


def test_check_mint_cli_mode_fails_on_missing_id(tmp_path, capsys):
    root = str(tmp_path)
    _write(root, "docs/plans/2026-04-03-uncovered.md", "---\ntitle: Uncovered\n---\n# Uncovered\n")
    rc = main(["--root", root, "--check-mint"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "MISSING-ID" in captured.err
    assert "2026-04-03-uncovered.md" in captured.err


# ---------------------------------------------------------------------------
# C5 AC7: archive-round-trip + wiring into fleet.archive_completed_plans' act
# phase.
# ---------------------------------------------------------------------------


def test_archive_round_trip_id_citation_survives_and_gate_stays_clean(tmp_path):
    """Simulates the exact move fleet.archive_completed_plans' act phase
    performs (docs/plans/<name>.md -> archive/specs/YYYY-MM/<name>.md) for a
    plan carrying a real plan_id, then re-resolves an id-form citation to it
    from a THIRD file. The id-form citation must still resolve post-move
    (the whole point of C1's resolver), and run_gate (the function wired
    into the act phase, C5 point 4) must report the corpus clean."""
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "archive", "specs", "2026-05"))
    # Pre-move state: live plan at docs/plans/.
    plan_rel = "docs/plans/2026-05-01-moved-plan.md"
    _write(root, plan_rel, _fm("Moved plan", plan_id="pln-moved-plan-444444"))
    _write(
        root,
        "coordinator/wiki/citer.md",
        "spec_backlink: pln-moved-plan-444444\n",
    )

    # Pre-move: id-form citation resolves to the live docs/plans/ path.
    outcome = resolve(Path(root), "pln-moved-plan-444444")
    assert outcome["outcome"] == "hit"
    assert outcome["path"].replace(os.sep, "/").endswith(plan_rel)

    # Simulate the archive move (git-mv equivalent for this test's purposes).
    dest_rel = "archive/specs/2026-05/2026-05-01-moved-plan.md"
    os.rename(os.path.join(root, plan_rel), os.path.join(root, dest_rel))

    # Post-move: the id-form citation still resolves — now to the archive path.
    outcome_after = resolve(Path(root), "pln-moved-plan-444444")
    assert outcome_after["outcome"] == "hit"
    assert outcome_after["path"].replace(os.sep, "/").endswith(dest_rel)

    # The gate wired into the act phase (run_gate) reports the corpus clean:
    # the id-form citation resolves, there's no leftover path-form citation
    # to heal, and no ungrandfathered path-form citation either.
    assert run_gate(root) == 0


def test_archive_plans_act_phase_wires_the_citation_gate():
    """C5 point 4: fleet.archive_completed_plans' act phase must call the
    same run_gate this module exposes — not a re-derived check."""
    from coordinator_core.ops.fleet import archive_plans

    assert archive_plans._run_backlink_gate is run_gate
