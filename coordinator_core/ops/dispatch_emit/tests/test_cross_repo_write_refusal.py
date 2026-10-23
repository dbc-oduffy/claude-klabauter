from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
    CrossRepoApprovalNeeded,
    CrossRepoWriteError,
    check_cross_repo_writes,
    split_sibling_paths,
)

LOCAL = {"CLAUDECODE": "1"}
CLOUD = {"CLAUDE_CODE_REMOTE": "true"}
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _row(row_id: str, writes, writes_under=()) -> WaveRow:
    return WaveRow(
        id=row_id,
        title=row_id,
        surface="doc-edit",
        writes=writes,
        reads=[],
        depends_on=[],
        writes_under=tuple(writes_under),
    )


def _make_sibling_repo(tmp_path: Path, name: str) -> Path:
    sibling = tmp_path / name
    (sibling / ".git").mkdir(parents=True)
    return sibling


def test_no_repo_root_is_noop() -> None:
    rows = [_row("M5a", ["claude-klabauter/coordinator_core/x.py"])]
    check_cross_repo_writes(rows, None)  # does not raise


def test_sibling_write_on_local_asks_pm(tmp_path: Path) -> None:
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    _make_sibling_repo(tmp_path, "claude-klabauter")
    rows = [
        _row("M5a", ["claude-klabauter/coordinator_core/ops/foo.py"]),
        _row("M5b", ["claude-klabauter/coordinator_core/ops/bar.py"]),
        _row("M6", ["docs/plans/x.md"]),
    ]
    with pytest.raises(CrossRepoApprovalNeeded) as excinfo:
        check_cross_repo_writes(rows, repo_root, env=LOCAL)
    message = str(excinfo.value)
    assert "approve_cross_repo_write" in message and "Ask the PM" in message
    assert "claude-klabauter" in message
    assert "M5a" in message
    assert "M5b" in message
    assert "M6" not in message


def test_writes_under_prefix_also_checked(tmp_path: Path) -> None:
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    _make_sibling_repo(tmp_path, "sibling-repo")
    rows = [_row("R1", UNDECLARED, writes_under=["sibling-repo/state/"])]
    with pytest.raises(CrossRepoApprovalNeeded) as excinfo:
        check_cross_repo_writes(rows, repo_root, env=LOCAL)
    assert "sibling-repo" in str(excinfo.value)


def test_write_under_own_repo_is_fine(tmp_path: Path) -> None:
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    rows = [_row("C1", ["coordinator/bin/foo.py"])]
    check_cross_repo_writes(rows, repo_root)  # does not raise


def test_absolute_and_escaping_paths_refused_and_names_each(tmp_path: Path) -> None:
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    rows = [
        _row("A", ["C:\\elsewhere\\a.md"]),  # abs-path-ok: drive-letter fixture for the predicate
        _row("B", ["/opt/other/b.md"]),
        _row("C", ["../CoordinatorContentRepo2/a.md"]),  # not a git checkout
        _row("D", ["coordinator_core/../../x.md"]),
    ]
    with pytest.raises(CrossRepoWriteError) as excinfo:
        check_cross_repo_writes(rows, repo_root)
    message = str(excinfo.value)
    for needle in ("elsewhere", "/opt/other/b.md", "CoordinatorContentRepo2", "x.md"):
        assert needle in message


def test_session_scratchpad_write_is_not_a_cross_repo_touch(tmp_path: Path, monkeypatch) -> None:
    import tempfile

    temp = tmp_path / "tmp"
    pad = temp / "claude" / "E--proj" / "sess-1" / "scratchpad"
    pad.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    _make_sibling_repo(tmp_path, "claude-klabauter")
    inside = (pad / "out.md").as_posix()
    other_temp = (temp / "elsewhere" / "x.md").as_posix()
    not_pad = (temp / "claude" / "E--proj" / "sess-1" / "other" / "x.md").as_posix()

    check_cross_repo_writes([_row("S", [inside])], repo_root, env=LOCAL)  # does not raise
    with pytest.raises(CrossRepoWriteError):
        check_cross_repo_writes([_row("T", [other_temp])], repo_root, env=LOCAL)
    with pytest.raises(CrossRepoWriteError):
        check_cross_repo_writes([_row("U", [not_pad])], repo_root, env=LOCAL)
    escaping = (pad / ".." / ".." / "x.md").as_posix()
    with pytest.raises(CrossRepoWriteError):
        check_cross_repo_writes([_row("V", [escaping])], repo_root, env=LOCAL)
    with pytest.raises(CrossRepoApprovalNeeded):
        check_cross_repo_writes(
            [_row("S", [inside]), _row("M", ["claude-klabauter/x.py"])], repo_root, env=LOCAL
        )


def test_paths_outside_repo_root_accepts_inside_paths(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
        paths_outside_repo_root,
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    inside_abs = str(repo_root / "a" / "b.py")
    assert paths_outside_repo_root(
        ["coordinator_core/x.py", inside_abs, "a/../b.py"], repo_root
    ) == []


def test_paths_outside_repo_root_preserves_input_order(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
        paths_outside_repo_root,
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _make_sibling_repo(tmp_path, "peer")
    bad = ["peer/a.md", "ok.py", "../x.md", "Z:/q.md", "/usr/q.md"]  # abs-path-ok: drive-letter fixture
    assert paths_outside_repo_root(bad, repo_root) == [
        "peer/a.md", "../x.md", "Z:/q.md", "/usr/q.md",  # abs-path-ok: drive-letter fixture
    ]


def test_no_sibling_dir_on_disk_is_fine(tmp_path: Path) -> None:
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    rows = [_row("M5a", ["claude-klabauter/coordinator_core/x.py"])]
    check_cross_repo_writes(rows, repo_root)  # no such dir on disk -> no-op


def test_sibling_write_proceeds_in_cloud_or_when_approved(tmp_path: Path) -> None:
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    _make_sibling_repo(tmp_path, "claude-klabauter")
    rows = [_row("M5a", ["claude-klabauter/a.py", "../claude-klabauter/b.py"])]
    assert check_cross_repo_writes(rows, repo_root, env=CLOUD) == ["claude-klabauter"]
    assert check_cross_repo_writes(rows, repo_root, approved=True, env=LOCAL) == ["claude-klabauter"]


def test_unreachable_write_refused_even_in_cloud(tmp_path: Path) -> None:
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    with pytest.raises(CrossRepoWriteError) as excinfo:
        check_cross_repo_writes([_row("B", ["/opt/other/b.md"])], repo_root, env=CLOUD)
    assert not isinstance(excinfo.value, CrossRepoApprovalNeeded)


def test_split_sibling_paths_buckets_per_repo(tmp_path: Path) -> None:
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    peer = _make_sibling_repo(tmp_path, "peer")
    home, siblings = split_sibling_paths(
        ["a.md", "peer/x/y.py", "../peer/z.py", str(peer / "w.py")], repo_root
    )
    assert home == ["a.md"]
    assert siblings == {peer: ["x/y.py", "z.py", "w.py"]}


def test_read_spine_cleared_gate_owners_are_per_call_not_shared(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import spine_read

    plan = tmp_path / "plan.md"
    plan.write_text(
        "---\nplan_id: pln-iso-abc123\n---\n# P\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: G1\n  title: a\n  change_kind: code-edit\n  surface: a.py\n  writes:\n    - a.py\n"
        "  external_gate:\n    - owner_repo: repo-a\n      requires: x\n"
        "- id: G2\n  title: b\n  change_kind: code-edit\n  surface: b.py\n  writes:\n    - b.py\n"
        "  external_gate:\n    - owner_repo: repo-b\n      requires: x\n```\n",
        encoding="utf-8",
    )

    def ids(owners):
        return sorted(r.id for r in spine_read.read_spine(plan, cleared_gate_owners=frozenset(owners)))

    assert ids({"repo-a"}) == ["G1"]
    assert ids({"repo-b"}) == ["G2"]
    assert ids(set()) == []
