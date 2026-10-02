from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
    CrossRepoWriteError,
    check_cross_repo_writes,
)
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


def test_write_under_a_sibling_git_checkout_refuses(tmp_path: Path) -> None:
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    _make_sibling_repo(tmp_path, "claude-klabauter")
    rows = [
        _row("M5a", ["claude-klabauter/coordinator_core/ops/foo.py"]),
        _row("M5b", ["claude-klabauter/coordinator_core/ops/bar.py"]),
        _row("M6", ["docs/plans/x.md"]),
    ]
    with pytest.raises(CrossRepoWriteError) as excinfo:
        check_cross_repo_writes(rows, repo_root)
    message = str(excinfo.value)
    assert "claude-klabauter" in message
    assert "M5a" in message
    assert "M5b" in message
    assert "M6" not in message


def test_writes_under_prefix_also_checked(tmp_path: Path) -> None:
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    _make_sibling_repo(tmp_path, "sibling-repo")
    rows = [_row("R1", UNDECLARED, writes_under=["sibling-repo/state/"])]
    with pytest.raises(CrossRepoWriteError) as excinfo:
        check_cross_repo_writes(rows, repo_root)
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
        _row("C", ["../CoordinatorContentRepo2/a.md"]),
        _row("D", ["coordinator_core/../../x.md"]),
    ]
    with pytest.raises(CrossRepoWriteError) as excinfo:
        check_cross_repo_writes(rows, repo_root)
    message = str(excinfo.value)
    for needle in ("elsewhere", "/opt/other/b.md", "CoordinatorContentRepo2", "x.md"):
        assert needle in message


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
