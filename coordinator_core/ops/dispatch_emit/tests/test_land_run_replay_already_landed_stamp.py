"""A replayed landing appends no review row its evidence sidecar already holds."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit import land_run

ROW = {"leg": "code-review", "plan_id": "p1", "findings_count": 2, "max_severity": "P1", "applied": 1}


def test_firings_not_recorded_skips_held_rows(tmp_path: Path) -> None:
    sidecar = tmp_path / "p.evidence.yaml"
    sidecar.write_text(yaml.safe_dump({"reviews": [{**ROW, "recorded_at": "t"}]}), encoding="utf-8")
    other = {**ROW, "findings_count": 5}
    assert land_run._firings_not_recorded(sidecar, [ROW, other]) == [other]


def test_firings_not_recorded_keeps_everything_without_a_sidecar(tmp_path: Path) -> None:
    assert land_run._firings_not_recorded(tmp_path / "none.yaml", [ROW, ROW]) == [ROW, ROW]


def test_firings_not_recorded_is_a_multiset(tmp_path: Path) -> None:
    sidecar = tmp_path / "p.evidence.yaml"
    sidecar.write_text(yaml.safe_dump({"reviews": [{**ROW, "recorded_at": "t"}]}), encoding="utf-8")
    assert land_run._firings_not_recorded(sidecar, [ROW, ROW]) == [ROW]


def test_append_evidence_twice_leaves_one_review_block(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    plan_rel = "docs/plans/plan.md"
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / plan_rel).write_text("---\ntitle: p\n---\n", encoding="utf-8")
    first = land_run._append_evidence(tmp_path, plan_rel, [ROW])
    assert first is not None
    sidecar = tmp_path / "docs" / "plans" / "plan.evidence.yaml"
    held = sidecar.read_bytes()
    assert len(yaml.safe_load(held)["reviews"]) == 1
    assert land_run._append_evidence(tmp_path, plan_rel, [ROW]) is None
    assert sidecar.read_bytes() == held
    assert len(yaml.safe_load(held)["reviews"]) == 1


def test_terminal_commit_unrecorded_review_rows_skips_held_rows(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import terminal_commit

    (tmp_path / "p.evidence.yaml").write_text(
        yaml.safe_dump({"reviews": [{**ROW, "recorded_at": "t"}]}), encoding="utf-8"
    )
    other = {**ROW, "findings_count": 5}
    assert terminal_commit._unrecorded_review_rows(tmp_path, "p.evidence.yaml", [ROW, other]) == [other]
    assert terminal_commit._unrecorded_review_rows(tmp_path, "none.yaml", [ROW]) == [ROW]


from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as _product
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: E402,F401  (fixture)


def test_a_replayed_land_run_drops_ignored_untracked_files_and_commits_none_of_them(repo: Path) -> None:
    """Default-param `dispatch.land_run` twice over the same run: the replay refuses nothing and the
    gitignored untracked run files stay untracked."""
    ignored = [".coordinator-local/review-targets/t.json", ".coordinator-local/memo-outbox/m.md"]
    _product._write(repo, ".gitignore", ".coordinator-local/\n")
    _product._git(["add", ".gitignore"], repo)
    _product._git(["commit", "-q", "-m", "ignore"], repo)
    _product._author(repo, "src/a.py", "a = 1\n")
    _product._declare(repo, "src/a.py")
    _product._record_dir(repo)
    for rel in ignored:
        _product._write(repo, rel, "x\n")

    first = _product._land(repo)
    second = _product._land(repo)

    assert first["committed"] is True, first
    assert not second.get("refused") and not second.get("error"), second
    tracked = set(_product._git(["ls-files"], repo).split())
    assert not tracked & set(ignored)
