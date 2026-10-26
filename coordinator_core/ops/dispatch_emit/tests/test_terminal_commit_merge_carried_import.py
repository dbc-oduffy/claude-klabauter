"""dispatch.terminal_commit import-closure over a merge: an import between two files the open merge
stages lands as a two-parent commit; an import of an undeclared worktree edit outside a merge refuses."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

_REVIEWED = reviewed()
_PLAN = "docs/plans/p.md"
_SIZING = "state/sizings/20260101-a.yaml"
_SIZING_TEXT = (
    "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
    "route: dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\npremise:\n"
    "  provenance: read\n  evidence: test fixture\n"
)


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check, **no_console_creationflags()
    ).stdout


def _init(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (root / "pkg" / "b_mod.py").write_text("old = 0\n", encoding="utf-8", newline="\n")
    plan = root / _PLAN
    plan.parent.mkdir(parents=True)
    plan.write_text(f"---\nsizing_object: {_SIZING}\n---\n# p\n", encoding="utf-8", newline="\n")
    sizing = root / _SIZING
    sizing.parent.mkdir(parents=True)
    sizing.write_text(_SIZING_TEXT, encoding="utf-8", newline="\n")


def _call(root: Path, paths: tuple, prefixes: tuple = ()) -> dict:
    chunk = ChunkCommit(id="C1", title="t1", paths=paths, prefixes=prefixes)
    request = CommitRequest(chunks=(chunk,), plan_path=_PLAN)
    (root / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8", newline="\n")
    params = {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED}
    return terminal_commit._handler(params, repo_root=root / ".git")


@pytest.fixture
def merging(tmp_path):
    """`theirs` adds a_new.py importing `new_name`, which only `theirs` adds to b_mod.py; a.py conflicts."""
    root = tmp_path / "repo"
    _init(root)
    (root / "a.py").write_text("x = 0\n", encoding="utf-8", newline="\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "checkout", "-q", "-b", "theirs")
    (root / "a.py").write_text("x = 2\n", encoding="utf-8", newline="\n")
    (root / "pkg" / "b_mod.py").write_text("old = 0\nnew_name = 1\n", encoding="utf-8", newline="\n")
    (root / "pkg" / "a_new.py").write_text("from pkg.b_mod import new_name\n", encoding="utf-8", newline="\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "theirs")
    _git(root, "checkout", "-q", "main")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    _git(root, "commit", "-q", "-am", "ours")
    _git(root, "merge", "theirs", check=False)
    assert (root / ".git" / "MERGE_HEAD").exists()
    return root


def test_a_merge_staged_import_between_merge_files_lands_two_parents(merging):
    ours = _git(merging, "rev-parse", "HEAD").strip()
    theirs = _git(merging, "rev-parse", "theirs").strip()
    (merging / "a.py").write_text("x = 3\n", encoding="utf-8", newline="\n")
    out = _call(merging, paths=("a.py",))
    assert out["committed"] is True, out
    assert _git(merging, "rev-list", "--parents", "-n1", out["sha"]).split() == [out["sha"], ours, theirs]
    assert not (merging / ".git" / "MERGE_HEAD").exists()
    assert _git(merging, "show", f"{out['sha']}:pkg/a_new.py") == "from pkg.b_mod import new_name\n"
    assert "new_name" in _git(merging, "show", f"{out['sha']}:pkg/b_mod.py")


def test_a_landing_that_removes_a_name_its_own_importer_uses_refuses_outside_a_merge(tmp_path):
    root = tmp_path / "plain"
    _init(root)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    head = _git(root, "rev-parse", "HEAD").strip()
    (root / "pkg" / "b_mod.py").write_text("other = 1\n", encoding="utf-8", newline="\n")
    (root / "pkg" / "a_new.py").write_text("from pkg.b_mod import old\n", encoding="utf-8", newline="\n")
    out = _call(root, paths=("pkg/b_mod.py", "pkg/a_new.py"))
    assert out["committed"] is False, out
    assert _git(root, "rev-parse", "HEAD").strip() == head
    assert "import" in str(out).lower(), out
