"""Replay of the a67e0d shape through default ``dispatch.terminal_commit``: a row with ``writes: []`` and one
prefix, a null session, 26 untracked files, and a report that lists them after a bare ``created-under-prefix:``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import credit_gate, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PREFIX = "docs/decisions/"
OTHER = "other.txt"
DRS = [f"{PREFIX}DR-{n:03d}-a67e0d.md" for n in range(1, 27)]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
        monkeypatch.delenv(var, raising=False)
    (root / PREFIX).mkdir(parents=True)
    for rel in DRS:
        (root / rel).write_text(f"# {rel}\n", encoding="utf-8")
    (root / OTHER).write_text("other row product\n", encoding="utf-8")
    return root


def _fire(repo: Path, report_text: str) -> dict:
    (repo / "report-C1.md").write_text(report_text, encoding="utf-8")
    (repo / "report-C2.md").write_text("DONE: C2\n", encoding="utf-8")
    request = CommitRequest(chunks=(
        ChunkCommit(id="C1", title="C1", prefixes=(PREFIX,), report="report-C1.md"),
        ChunkCommit(id="C2", title="C2", paths=(OTHER,), report="report-C2.md"),
    ))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    review = {**reviewed(), "prep": {"slice_files": [*DRS, OTHER]}}
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": review}, repo_root=repo / ".git",
    )


def test_a_bare_label_with_the_paths_on_the_lines_after_it_lands_all_26_files(repo):
    out = _fire(repo, "DONE: C1\ncreated-under-prefix:\n" + "\n".join(DRS) + "\n")

    assert out.get("committed") is True, out
    landed = set(_git(["show", "--name-only", "--format=", out["sha"]], repo).split())
    assert set(DRS) <= landed
    assert _git(["log", "-1", "--format=%s", out["sha"]], repo).startswith("C1, C2")
    assert not (set(DRS) & set(_git(["status", "--porcelain", "--untracked-files=all", "--", PREFIX], repo).split()))


def test_a_report_claiming_none_while_26_files_sit_under_the_prefix_is_refused_and_names_them(repo):
    before = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "DONE: C1\ncreated-under-prefix: none\n")

    assert out.get("refused") == credit_gate.REFUSED_CREDITED_PREFIX_ZERO_PATHS == "credited-prefix-row-zero-paths", out
    assert out["unlanded"] == {"C1": sorted(DRS)}
    assert _git(["rev-parse", "HEAD"], repo) == before


def test_an_unreadable_worktree_status_leaves_the_zero_path_refusal_unfired(repo, monkeypatch):
    monkeypatch.setattr(terminal_commit, "_worktree_snapshot", lambda *a, **k: None)

    out = _fire(repo, "DONE: C1\ncreated-under-prefix: none\n")

    assert out.get("refused") != credit_gate.REFUSED_CREDITED_PREFIX_ZERO_PATHS, out
