
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

spec = importlib.util.spec_from_file_location(
    "safe_commit_head_deletion_under_test", _BIN_DIR / "coordinator-safe-commit.py"
)
assert spec is not None and spec.loader is not None
safe_commit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = safe_commit
spec.loader.exec_module(safe_commit)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    (root / "pkg").mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "pkg" / "kept.py").write_text("kept\n", encoding="utf-8")
    (root / "pkg" / "gone.py").write_text("gone\n", encoding="utf-8")
    _git(root, "add", "--", "pkg/kept.py", "pkg/gone.py")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def test_present_stays_present_and_a_tracked_absence_is_a_deletion(tmp_path):
    root = _repo(tmp_path)
    (root / "pkg" / "gone.py").unlink()

    present, deleted = safe_commit._split_paths_for_commit_v2(
        str(root), ["pkg/kept.py", "pkg/gone.py"]
    )

    assert present == ["pkg/kept.py"]
    assert deleted == ["pkg/gone.py"]


def test_a_path_in_neither_the_worktree_nor_head_is_refused(tmp_path, capsys):
    """THE P0 REGRESSION. Before the fix this returned (["pkg/kept.py"],
    ["pkg/never.py"]) -- a deletion declared for a path nobody ever had."""
    root = _repo(tmp_path)

    with pytest.raises(SystemExit) as exc:
        safe_commit._split_paths_for_commit_v2(
            str(root), ["pkg/kept.py", "pkg/never.py"]
        )

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert (
        "BLOCKED: pkg/never.py is neither in the worktree nor in HEAD -- "
        "refusing to commit it as a deletion." in err
    )


def test_head_is_not_consulted_when_nothing_is_missing(tmp_path, monkeypatch):
    root = _repo(tmp_path)

    def _never(*_args, **_kwargs):
        raise AssertionError("_paths_tracked_at_head spawned with nothing missing")

    monkeypatch.setattr(safe_commit, "_paths_tracked_at_head", _never)

    present, deleted = safe_commit._split_paths_for_commit_v2(
        str(root), ["pkg/kept.py", "pkg/gone.py"]
    )

    assert present == ["pkg/kept.py", "pkg/gone.py"]
    assert deleted == []


def test_an_unanswerable_head_probe_fails_closed(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)

    class _Failed:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a valid object name HEAD\n"

    monkeypatch.setattr(
        safe_commit.subprocess, "run", lambda *a, **k: _Failed()
    )

    with pytest.raises(SystemExit) as exc:
        safe_commit._paths_tracked_at_head(str(root), ["pkg/kept.py"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "BLOCKED: could not read HEAD to tell a deletion from a bad path" in err
    assert "fatal: not a valid object name HEAD" in err


def test_a_mixed_pathspec_refuses_and_names_only_the_bogus_path(tmp_path, capsys):
    root = _repo(tmp_path)
    (root / "pkg" / "gone.py").unlink()

    with pytest.raises(SystemExit) as exc:
        safe_commit._split_paths_for_commit_v2(
            str(root), ["pkg/kept.py", "pkg/gone.py", "pkg/never.py"]
        )

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "BLOCKED: pkg/never.py is neither in the worktree nor in HEAD" in err
    assert "pkg/gone.py is neither" not in err
    assert "pkg/kept.py is neither" not in err


def test_an_absent_directory_is_refused_not_called_a_deleted_file(tmp_path, capsys):
    root = _repo(tmp_path)
    import shutil

    shutil.rmtree(root / "pkg")

    with pytest.raises(SystemExit) as exc:
        safe_commit._split_paths_for_commit_v2(str(root), ["pkg"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "BLOCKED: pkg is neither in the worktree nor in HEAD" in err


def test_percolate_round_never_classifies_a_head_absent_path_as_a_deletion(tmp_path):
    import importlib.util as _ilu

    round_spec = _ilu.spec_from_file_location(
        "percolate_round_census_under_test", _BIN_DIR / "percolate-round.py"
    )
    assert round_spec is not None and round_spec.loader is not None
    round_mod = _ilu.module_from_spec(round_spec)
    sys.modules[round_spec.name] = round_mod
    round_spec.loader.exec_module(round_mod)

    root = tmp_path / "dest"
    root.mkdir()
    (root / "kept.py").write_text("kept\n", encoding="utf-8")

    present, deletions, declined = round_mod._partition_pathspec_for_commit(
        ["kept.py", "tracked-gone.py", "never-existed.py"],
        str(root),
        head_tracked={"tracked-gone.py"},
    )

    assert present == ["kept.py"]
    assert deletions == ["tracked-gone.py"]
    assert [d["path"] for d in declined] == ["never-existed.py"]


def test_cli_and_engine_refuse_the_same_phantom_path(tmp_path):
    from coordinator_core.git.commit import PhantomDeletionDeclared, commit_paths

    root = _repo(tmp_path)

    with pytest.raises(SystemExit) as exc:
        safe_commit._split_paths_for_commit_v2(str(root), ["pkg/never.py"])
    assert exc.value.code == 1

    with pytest.raises(PhantomDeletionDeclared) as engine_exc:
        commit_paths(str(root), [], "engine refusal fixture", deleted_paths=["pkg/never.py"])
    assert "pkg/never.py" in str(engine_exc.value)


def test_ls_tree_still_reports_the_files_a_real_deletion_names(tmp_path):
    root = _repo(tmp_path)
    (root / "pkg" / "gone.py").unlink()

    present, deleted = safe_commit._split_paths_for_commit_v2(
        str(root), ["pkg/kept.py", "pkg/gone.py"]
    )

    assert present == ["pkg/kept.py"]
    assert deleted == ["pkg/gone.py"]
