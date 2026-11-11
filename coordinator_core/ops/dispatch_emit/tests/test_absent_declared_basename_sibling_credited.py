"""A declared write absent from the worktree and HEAD whose basename matches exactly one edited
path in the same package is credited to the chunk and the sibling lands; zero or several candidates
keep the stranded refusal (nothing is guessed). Both commit ops are driven at default arguments."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = reviewed()
MOD = "pkg/mod.py"
DECLARED = "pkg/tests/test_x.py"
SIBLING = "pkg/test_x.py"
OTHER_SIBLING = "pkg/sub/test_x.py"
_PARTIAL_REPORT = (
    "PARTIAL: X1\n"
    "gate-blocker: outside-footprint: failures only outside the footprint\n"
    "<exit-status>PARTIAL</exit-status>\n"
)


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, MOD, "x = 1\n")
    _write(root, SIBLING, "def test_x():\n    pass\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    _write(root, MOD, "x = 2\n")
    _write(root, "report.md", _PARTIAL_REPORT)
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=(MOD, DECLARED), report="report.md"),)
    )
    _write(root, "run.mjs", "// emitted\n" + render_marker(request) + "\n")
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _params(slice_files) -> dict:
    review = {
        **_REVIEWED,
        "prep": {"slice_files": list(slice_files), "product_files": 1, "foreign_claims": []},
        "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
        "integration": {"unresolved": 0, "confinement_violations": []},
        "tests": {"status": "pass", "run": 1, "failed": 0, "sidecar": "t.md"},
    }
    return {"script_path": "run.mjs", "incomplete_chunks": ["X1"], "inline_review": review}


def _head_files(root: Path) -> set:
    return set(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


@pytest.mark.parametrize("op", [terminal_commit, land_run])
def test_the_unique_edited_sibling_is_credited_and_lands(repo, op):
    _write(repo, SIBLING, "def test_x():\n    assert True\n")

    out = op._handler(_params([MOD, SIBLING]), repo_root=repo / ".git")

    assert out["committed"] is True, out
    assert {MOD, SIBLING} <= _head_files(repo)
    assert _git(["show", f"HEAD:{SIBLING}"], repo) == "def test_x():\n    assert True\n"


@pytest.mark.parametrize("op", [terminal_commit, land_run])
def test_no_edited_sibling_keeps_the_stranded_refusal(repo, op):
    out = op._handler(_params([MOD]), repo_root=repo / ".git")

    assert out.get("committed") is not True, out
    assert _git(["rev-list", "--count", "HEAD"], repo).strip() == "1"


@pytest.mark.parametrize("op", [terminal_commit, land_run])
def test_two_edited_siblings_guess_nothing(repo, op):
    _write(repo, SIBLING, "def test_x():\n    assert True\n")
    _write(repo, OTHER_SIBLING, "def test_x():\n    assert True\n")

    out = op._handler(_params([MOD, SIBLING, OTHER_SIBLING]), repo_root=repo / ".git")

    assert out.get("committed") is not True, out
    assert _git(["rev-list", "--count", "HEAD"], repo).strip() == "1"
