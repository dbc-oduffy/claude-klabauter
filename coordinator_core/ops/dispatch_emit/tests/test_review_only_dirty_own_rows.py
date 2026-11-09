"""A `--review-only` emit carries a dirty write-set path that only its named rows declare, and
threads it onto the marker; any other dirty path still refuses."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli, dirty_write_set
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.tests._git_fixture import _git as _shared_git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = "docs/plans/p.md"
_REAL_GUARD = dirty_write_set.guard_against_dirty_write_set


@pytest.fixture(autouse=True)
def _real_guard(monkeypatch):
    """The suite-wide conftest stubs the guard; this module exercises the real one."""
    monkeypatch.setattr(dirty_write_set, "guard_against_dirty_write_set", _REAL_GUARD)


def _row(rid: str, writes: list[str], disposition: str = "open") -> str:
    w = "".join(f"    - {p}\n" for p in writes)
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: {writes[0]}\n"
        f"  writes:\n{w}  queue_scope: project\n  disposition: {disposition}\n"
        f"  body: |\n    Do {rid}.\n"
    )


def _plan(rows: str) -> str:
    return (
        '---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n'
        f"## Tasks\n\n```yaml plan-tasks\n{rows}```\n"
    )


def _git(repo: Path, *args: str) -> str:
    return _shared_git(list(args), repo).strip()


def _make_repo(tmp_path, monkeypatch, rows: str) -> tuple[Path, str]:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.example")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "docs" / "plans").mkdir(parents=True)
    (r / "docs" / "x").mkdir(parents=True)
    (r / _PLAN).write_text(_plan(rows), encoding="utf-8")
    for name in ("C1", "C2", "free"):
        (r / "docs" / "x" / f"{name}.md").write_text("v1\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    base = _git(r, "rev-parse", "HEAD")
    (r / "landed.txt").write_text("l\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "landed")
    monkeypatch.chdir(r)
    return r, base


def _emit(repo: Path, *extra: str) -> int:
    return cli.main(["--plan", str(repo / _PLAN), "--out", str(repo / "p.workflow.mjs"), *extra])


def _review_only(repo: Path, base: str, rows: str = "C1") -> int:
    return _emit(repo, "--review-only", "--rows", rows, "--run-base", base)


_C1 = _row("C1", ["docs/x/C1.md"])
_C2 = _row("C2", ["docs/x/C2.md"])


def test_review_only_carries_the_named_rows_own_dirty_path(tmp_path, monkeypatch, capsys):
    repo, base = _make_repo(tmp_path, monkeypatch, _C1 + _C2)
    (repo / "docs" / "x" / "C1.md").write_text("drift\n", encoding="utf-8")

    code = _review_only(repo, base)

    err = capsys.readouterr().err
    assert code == cli.EXIT_OK, err
    assert "C1: 1 uncommitted path(s) in its own writes carried, withheld from this review-only landing" in err
    marker = parse_marker((repo / "p.workflow.mjs").read_text(encoding="utf-8"))
    assert marker is not None and marker.review_only
    assert marker.review_carried == ("docs/x/C1.md",)


def test_same_tree_without_review_only_still_refuses(tmp_path, monkeypatch, capsys):
    repo, _ = _make_repo(tmp_path, monkeypatch, _C1 + _C2)
    (repo / "docs" / "x" / "C1.md").write_text("drift\n", encoding="utf-8")

    code = _emit(repo)

    assert code == cli.EXIT_DATA_ERROR
    assert "already modified, staged, or untracked" in capsys.readouterr().err


def test_path_also_declared_by_a_row_outside_rows_still_refuses(tmp_path, monkeypatch, capsys):
    shared = _C1 + _row("C2", ["docs/x/C1.md"])
    repo, base = _make_repo(tmp_path, monkeypatch, shared)
    (repo / "docs" / "x" / "C1.md").write_text("drift\n", encoding="utf-8")

    code = _review_only(repo, base)

    assert code == cli.EXIT_DATA_ERROR
    assert "already modified, staged, or untracked" in capsys.readouterr().err


def test_path_outside_every_row_never_masks_a_refusing_sibling_path(tmp_path, monkeypatch, capsys):
    repo, base = _make_repo(tmp_path, monkeypatch, _C1 + _C2)
    (repo / "docs" / "x" / "free.md").write_text("drift\n", encoding="utf-8")
    (repo / "docs" / "x" / "C2.md").write_text("drift\n", encoding="utf-8")

    code = _review_only(repo, base)

    assert code == cli.EXIT_DATA_ERROR
    assert "docs/x/C2.md" in capsys.readouterr().err


def test_emit_script_refuses_carried_paths_without_review_only_rows(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.emit import emit_script

    repo, _ = _make_repo(tmp_path, monkeypatch, _C1)

    with pytest.raises(ValueError, match="review_carried_paths"):
        emit_script(repo / _PLAN, repo_root=repo, review_carried_paths=["a.py"])


def _task_output(repo: Path, incomplete: list, landed: list) -> Path:
    import json

    out = repo / "task-output.json"
    params = {"incomplete_chunks": incomplete, "landed_chunks": landed}
    out.write_text(json.dumps({"next_action": {"kind": "land_run", "params": params}}), encoding="utf-8")
    return out


def _dirty_all(repo: Path, names: list) -> None:
    for n in names:
        (repo / "docs" / "x" / f"{n}.md").write_text("drift\n", encoding="utf-8")


_C3 = _row("C3", ["docs/x/C3.md"])


def test_review_only_carries_every_row_the_task_output_names(tmp_path, monkeypatch, capsys):
    repo, base = _make_repo(tmp_path, monkeypatch, _C1 + _C2 + _C3)
    (repo / "docs" / "x" / "C3.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c3")
    _dirty_all(repo, ["C1", "C2", "C3"])
    run = _task_output(repo, ["C2", "C3"], ["C1"])

    code = _emit(repo, "--review-only", str(run), "--rows", "C1", "--run-base", base)

    err = capsys.readouterr().err
    assert code == cli.EXIT_OK, err
    marker = parse_marker((repo / "p.workflow.mjs").read_text(encoding="utf-8"))
    assert marker is not None and marker.review_only
    assert marker.review_carried == ("docs/x/C1.md", "docs/x/C2.md", "docs/x/C3.md")


def test_review_only_still_refuses_dirt_of_a_row_the_task_output_does_not_name(
    tmp_path, monkeypatch, capsys
):
    repo, base = _make_repo(tmp_path, monkeypatch, _C1 + _C2 + _C3)
    (repo / "docs" / "x" / "C3.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c3")
    _dirty_all(repo, ["C1", "C2", "C3"])
    run = _task_output(repo, ["C2"], ["C1"])

    code = _emit(repo, "--review-only", str(run), "--rows", "C1", "--run-base", base)

    assert code == cli.EXIT_DATA_ERROR
    assert "docs/x/C3.md" in capsys.readouterr().err


def test_review_only_task_output_does_not_excuse_a_path_a_foreign_row_also_declares(
    tmp_path, monkeypatch, capsys
):
    repo, base = _make_repo(tmp_path, monkeypatch, _C1 + _C2 + _row("C3", ["docs/x/C2.md"]))
    _dirty_all(repo, ["C2"])
    run = _task_output(repo, ["C2"], ["C1"])

    code = _emit(repo, "--review-only", str(run), "--rows", "C1", "--run-base", base)

    assert code == cli.EXIT_DATA_ERROR
    assert "docs/x/C2.md" in capsys.readouterr().err


def test_review_only_carries_fourteen_dirty_run_owned_paths(tmp_path, monkeypatch, capsys):
    names = [f"R{i:02d}" for i in range(14)]
    rows = "".join(_row(n, [f"docs/x/{n}.md"]) for n in names)
    repo, base = _make_repo(tmp_path, monkeypatch, rows)
    for n in names:
        (repo / "docs" / "x" / f"{n}.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rows")
    _dirty_all(repo, names)
    run = _task_output(repo, names[1:], names[:1])

    code = _emit(repo, "--review-only", str(run), "--rows", names[0], "--run-base", base)

    err = capsys.readouterr().err
    assert code == cli.EXIT_OK, err
    marker = parse_marker((repo / "p.workflow.mjs").read_text(encoding="utf-8"))
    assert marker is not None and marker.review_only
    assert len(marker.review_carried) == 14


def test_coded_review_row_with_its_file_dirty_is_accepted(tmp_path, monkeypatch, capsys):
    # read_spine's default exclusions drop a `coded` row from the guard's union, so its dirty
    # file is never seen by the guard: the emit is accepted with nothing carried.
    repo, base = _make_repo(tmp_path, monkeypatch, _row("C1", ["docs/x/C1.md"], "coded") + _C2)
    (repo / "docs" / "x" / "C1.md").write_text("drift\n", encoding="utf-8")

    code = _review_only(repo, base)

    assert code == cli.EXIT_OK, capsys.readouterr().err
    assert _REAL_GUARD(repo / _PLAN, repo, own=frozenset({"C1"})) is None
