"""A failed existence probe is not a deletion declaration.

`_split_paths_for_commit_v2` used to treat ANY path absent from the worktree as
a deletion. That inference was the committer-P0: a negative existence probe --
from a wrong cwd, or from `os.path.exists` swallowing an OSError such as a
Windows sharing violation on a file one of the ~50 concurrent peers holds open
-- became a positive `params.deleted_paths` entry, and the commit route
faithfully deleted a file nobody deleted. Root-caused 2026-08-31,
`state/audits/2026-08-31-committer-p0-root-cause-cwd-probe-becomes-deletion.md`.

It now consults HEAD for the missing set only, in one batched spawn, and
refuses what is in neither place.

Run: python -m pytest coordinator/bin/tests/test_a_missing_path_is_a_deletion_only_if_head_has_it.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent

# Spawns a real external process (git, in a throwaway repo); runs at cadence
# gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
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
    """A throwaway repo with one commit carrying pkg/kept.py and pkg/gone.py."""
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
    """Zero spawns on the ordinary path: every declared path is on disk, so the
    one batched `git ls-tree` is never reached."""
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
    """A non-zero `ls-tree` refuses. Returning an empty set instead would dress
    a guess as a fact -- every missing path would read as "not tracked"."""
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
