"""dispatch.terminal_commit refuses a landing that deletes a module (or removes a top-level name)
a dirty residue ``.py`` it leaves behind still imports."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

_REVIEWED = reviewed()


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "cc").mkdir()
    (root / "cc" / "__init__.py").write_text("", encoding="utf-8")
    (root / "cc" / "_probe.py").write_text("def _old():\n    return 1\n", encoding="utf-8")
    (root / "cc" / "mod.py").write_text(
        "def keep():\n    return 1\n\n\ndef gone():\n    return 2\n", encoding="utf-8"
    )
    (root / "cc" / "user.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _fire(repo: Path, done_paths, user_text: str) -> dict:
    """Done chunk C1 lands ``done_paths``; incomplete chunk C2 declares the dirty ``cc/user.py``."""
    (repo / "cc" / "user.py").write_text(user_text, encoding="utf-8")
    request = CommitRequest(chunks=(
        ChunkCommit(id="C1", title="t1", paths=tuple(done_paths)),
        ChunkCommit(id="C2", title="t2", paths=("cc/user.py",)),
    ))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": ["C2"]}
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_a_deleted_module_a_residue_file_imports_is_refused_naming_the_importer(repo):
    (repo / "cc" / "_probe.py").unlink()
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, ["cc/_probe.py"], "from cc._probe import _old\n")

    assert out["committed"] is False, out
    assert out["refused"] == landing_integrity.IMPORT_CLOSURE_UNTRACKED
    assert out["integrity"][0]["path"] == "cc/user.py"
    assert "_probe" in out["integrity"][0]["detail"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_removed_top_level_name_a_residue_file_imports_is_refused(repo):
    (repo / "cc" / "mod.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, ["cc/mod.py"], "from cc.mod import gone\n")

    assert out["committed"] is False, out
    assert out["refused"] == landing_integrity.IMPORT_CLOSURE_UNTRACKED
    assert out["integrity"][0]["path"] == "cc/user.py"
    assert "absent from HEAD" in out["integrity"][0]["detail"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_the_clean_counterpart_commits(repo):
    (repo / "cc" / "_probe.py").unlink()

    out = _fire(repo, ["cc/_probe.py"], "x = 2\n")

    assert out["committed"] is True, out
    assert not (repo / "cc" / "_probe.py").exists()
    assert "cc/_probe.py" not in _git(["ls-files"], repo)
