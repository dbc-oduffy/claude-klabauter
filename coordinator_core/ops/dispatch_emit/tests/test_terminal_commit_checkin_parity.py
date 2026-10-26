"""dispatch.terminal_commit lands the blob `git hash-object` writes for a CRLF file under
core.autocrlf=true (no attribute and `* text=auto`), and the path reads clean afterwards."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CRLF = b"line one\r\nline two\r\nline three\r\n"
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
_REVIEWED = reviewed()


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _repo(tmp_path: Path, attrs) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "true"], root)
    (root / "README.md").write_bytes(b"seed\n")
    _git(["add", "README.md"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    if attrs is not None:
        (root / ".gitattributes").write_bytes(attrs)
        _git(["add", ".gitattributes"], root)
        _git(["commit", "-q", "-m", "attrs"], root)
    return root


@pytest.mark.parametrize("attrs", [None, b"* text=auto\n"], ids=["no_attribute", "text_auto"])
def test_landed_blob_equals_git_hash_object_and_path_reads_clean(tmp_path, attrs):
    repo = _repo(tmp_path, attrs)
    (repo / "crlf.txt").write_bytes(CRLF)
    want = _git(["hash-object", "crlf.txt"], repo)

    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("crlf.txt",)),))
    (repo / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8")

    out = terminal_commit._handler(
        {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []},
        repo_root=repo / ".git",
    )
    assert out["committed"] is True, out

    landed = _git(["ls-tree", "HEAD", "--", "crlf.txt"], repo).split()[2]
    assert landed == want
    assert _git(["status", "--porcelain", "--", "crlf.txt"], repo) == ""
