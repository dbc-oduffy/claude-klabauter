"""A repo-relative path does not become a deletion because of where you stood.

`do_pathspec` computed its worktree root as `os.getcwd()` while
`ceremony.commit_v2` resolves `main_worktree_root(repo_root)`. Invoked from a
SUBDIRECTORY the two disagreed: `_split_paths_for_commit_v2` probed
`<cwd>/<repo-relative path>`, missed, and forwarded the miss as
`params.deleted_paths` -- a negative existence probe becoming a positive
deletion declaration for a file nobody deleted. Root-caused
2026-08-31, `state/audits/2026-08-31-committer-p0-*`.

Run: python -m pytest coordinator/bin/tests/test_pathspec_root_is_the_repo_not_the_cwd.py -q
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

# This file now builds a real repo and `_split_paths_for_commit_v2` spawns
# one `git ls-tree`, so it declares itself to the spawn ratchet.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if hasattr(subprocess, "CREATE_NO_WINDOW")
    else {}
)

_BIN_DIR = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "safe_commit_pathspec_root_under_test", _BIN_DIR / "coordinator-safe-commit.py"
)
assert spec is not None and spec.loader is not None
safe_commit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = safe_commit
spec.loader.exec_module(safe_commit)


def _repo(tmp_path):
    """A REAL repo, not a bare `.git` mkdir.

    Upgraded 2026-08-31: `_split_paths_for_commit_v2` now asks HEAD whether a
    missing path is a deletion or a bad path, so a fixture whose `.git` is an
    empty directory cannot answer and every probe fails closed. `pkg/gone.py`
    is committed and then removed, which is what 'a genuinely absent path'
    has to mean once absence alone stopped being sufficient.
    """
    root = tmp_path / "r"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "kept.py").write_text("x\n", encoding="utf-8")
    (root / "pkg" / "gone.py").write_text("y\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "base"],
    ):
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            **_NO_WINDOW,
        )
    (root / "pkg" / "gone.py").unlink()
    return root


def test_the_root_is_walked_up_from_a_subdirectory(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.chdir(root / "pkg" / "sub")
    assert Path(safe_commit._worktree_root_from_cwd()) == root.resolve()


def test_a_present_path_is_not_declared_deleted_from_a_subdirectory(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.chdir(root / "pkg" / "sub")

    present, deleted = safe_commit._split_paths_for_commit_v2(
        safe_commit._worktree_root_from_cwd(), ["pkg/kept.py"]
    )

    assert present == ["pkg/kept.py"]
    assert deleted == []
    # The regression stays legible, but the old shape is no longer reachable
    # to pin: `_split_paths_for_commit_v2` stopped inferring a deletion from
    # a failed probe (committer-P0 fix 2), so the wrong-cwd call now REFUSES
    # where it used to return `([], ["pkg/kept.py"])` and delete the file.
    # Asserting the refusal pins strictly more than the old tuple did -- it
    # says the silent-deletion path is gone, not merely that it was wrong.
    with pytest.raises(SystemExit) as exc:
        safe_commit._split_paths_for_commit_v2(os.getcwd(), ["pkg/kept.py"])
    assert exc.value.code == 1


def test_a_genuinely_absent_path_is_still_a_deletion(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)

    present, deleted = safe_commit._split_paths_for_commit_v2(
        safe_commit._worktree_root_from_cwd(), ["pkg/gone.py"]
    )

    assert present == []
    assert deleted == ["pkg/gone.py"]


def test_outside_a_repo_it_degrades_to_the_cwd(tmp_path, monkeypatch):
    plain = tmp_path / "no-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert Path(safe_commit._worktree_root_from_cwd()) == plain.resolve()
