"""`import_closure_gate`: a changed `.py` whose intra-repo import is absent from the landed tree is refused.

Fixtures reproduce the shapes of three example-game-repo commits (7379135bc0, d5ff5e54b5, 9693dd80d8) as
in-memory trees; the gate reads only through `read_source`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.ceremony import commit_v2

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_pre_commit_gates_refuses_import_of_symbol_bound_nowhere(tmp_path: Path):
    # A symbol bound nowhere (not even in the worktree) is refused by the `leak_gate` import_closure
    # detector, which runs ahead of `import_closure_gate` and so supplies the refusal text.
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "pkg/__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg/stage.py").write_text("OLD = 1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout

    (repo / "pkg/user.py").write_text("from pkg.stage import ARM_BYPASS\n", encoding="utf-8")
    result = commit_v2._handler({"paths": ["pkg/user.py"], "message": "land user\n"}, repo_root=repo / ".git")
    assert result["committed"] is False, result
    assert "import_closure" in result["error"] and "ARM_BYPASS" in result["error"]
    assert "pkg/stage.py" in result["error"]
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout == head

    (repo / "pkg/stage.py").write_text("OLD = 1\nARM_BYPASS = 2\n", encoding="utf-8")
    result = commit_v2._handler(
        {"paths": ["pkg/user.py", "pkg/stage.py"], "message": "land both\n"}, repo_root=repo / ".git"
    )
    assert result["committed"] is True, result
