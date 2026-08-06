"""
Tests for coordinator_core.ops.assert_no_dangling_plan_backlinks.

Spec backlink: archive/specs/2026-06/2026-06-23-programmatic-terminal-plan-archival.md § AC9 / C6
"""
from __future__ import annotations

import os

import pytest

from coordinator_core.ops.assert_no_dangling_plan_backlinks import main


def _write(root: str, rel: str, content: str) -> None:
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)


def _make_moved_plan_tree(root: str) -> None:
    _write(root, "archive/specs/2026-01/2026-01-01-foo-plan.md", "# Foo plan\n")
    _write(root, "archive/specs/2026-01/2026-01-01-foo-plan.review.md", "# sidecar\n")
    _write(root, "docs/plans/2026-01-02-bar-plan.md", "# Bar plan\n")


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
    root = str(tmp_path)
    _make_moved_plan_tree(root)
    _write(
        root,
        "coordinator/wiki/citer3.md",
        "spec_backlink: docs/plans/2026-01-02-bar-plan.md\n",
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
