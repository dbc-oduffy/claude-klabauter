"""A concurrent run's dirty file that no declaration of this run names never reaches the terminal
commit's integrity gate: it is left dirty, reported under ``foreign_excluded``, and the landing
proceeds. A since-base overlap on a path the run itself declares still refuses.

Incident shape (XS run, coordinator-content-repo 2026-10-21): the run declares docs, one of them in the directory
that also holds a test file; a foreign commit after the run base adds lines to that test file, and a
later uncommitted edit drops them. The admission arm that put such a file into ``all_paths`` is
run-owned dirt: the review prep's ``slice_files`` lists every file changed since the run base, a
concurrent run's included, ``run_touched`` reads them as this run's touched set, and
``_run_owned_dirt`` admits a touched file that sits in a declared directory."""

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
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c3"
FOREIGN = "coordinator/tests/test_foreign_shape.py"
DOCS = ("docs/a.md", "docs/b.md", "coordinator/tests/notes.md")


def _review(*slice_files: str) -> dict:
    return {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_reviews": reviewed()["wave_reviews"],
        "prep": {"slice_files": list(slice_files)},
    }


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _write(root, "README.md", "seed\n")
    _write(root, FOREIGN, "def test_a():\n    assert True\n")
    for doc in DOCS:
        _write(root, doc, "doc\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _foreign_overlap(repo: Path) -> str:
    """Return the run base; a foreign commit adds lines to FOREIGN after it, then an uncommitted
    edit drops them."""
    base = _git(["rev-parse", "HEAD"], repo).strip()
    path = repo / FOREIGN
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "def test_b():\n    assert True\n\ndef test_c():\n    assert True\n",
                    encoding="utf-8", newline="")
    _git(["commit", "-q", "-am", "foreign adds lines"], repo)
    path.write_text(original + "# in-flight edit\n", encoding="utf-8", newline="")
    return base


def _edit_docs(repo: Path) -> None:
    for doc in DOCS:
        _write(repo, doc, "doc\nedited\n")
        session_scope.touch(SELF, doc, cwd=str(repo))


def _run(repo: Path, paths=DOCS, **extra) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="X1", title="docs", paths=tuple(paths)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    params = {
        "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _review(FOREIGN),
        "session_id": SELF,
    }
    params.update(extra)
    return terminal_commit._handler(params, repo_root=repo / ".git")


def _committed(repo: Path) -> list:
    """Paths the last terminal commit landed: HEAD plus the product commit a follow-on HEAD
    (`Own-Left-Behind-Of:` trailer) is attributed to."""
    import re

    def files(rev: str) -> set:
        out = _git(["show", "--numstat", "--format=", rev], repo)
        return {ln.split("\t")[-1] for ln in out.splitlines() if ln.strip()}

    landed = files("HEAD")
    product = re.search(r"Own-Left-Behind-Of: ([0-9a-f]{40})", _git(["log", "-1", "--format=%B", "HEAD"], repo))
    if product:
        landed |= files(product.group(1))
    return sorted(landed)


def test_a_control_without_the_overlap_lands_the_run_owned_file(repo):
    _edit_docs(repo)
    _write(repo, FOREIGN, "def test_a():\n    assert True\n# mine\n")
    session_scope.touch(SELF, FOREIGN, cwd=str(repo))
    out = _run(repo)
    assert out["committed"] is True, out
    assert FOREIGN in _committed(repo)
    assert "foreign_excluded" not in out


def test_a_foreign_dirty_file_is_left_dirty_and_the_landing_proceeds(repo):
    base = _foreign_overlap(repo)
    foreign_bytes = (repo / FOREIGN).read_bytes()
    _edit_docs(repo)
    out = _run(repo, run_base_sha=base)
    assert out.get("refused") != "since-base-overlap", out
    assert out["committed"] is True, out
    assert _committed(repo) == sorted(DOCS)
    assert out["foreign_excluded"] == [FOREIGN]
    assert (repo / FOREIGN).read_bytes() == foreign_bytes
    assert FOREIGN in _git(["status", "--porcelain"], repo)


def test_a_session_claimed_foreign_file_is_not_carried_after_the_exclusion(repo):
    base = _foreign_overlap(repo)
    session_scope.touch(SELF, FOREIGN, cwd=str(repo))
    _edit_docs(repo)
    out = _run(repo, run_base_sha=base)
    assert out["committed"] is True, out
    assert _committed(repo) == sorted(DOCS)
    assert FOREIGN in _git(["status", "--porcelain"], repo)


def test_an_overlap_on_a_declared_path_still_refuses(repo):
    base = _foreign_overlap(repo)
    session_scope.touch(SELF, FOREIGN, cwd=str(repo))
    out = _run(repo, paths=(FOREIGN,), run_base_sha=base)
    assert out.get("refused") == "since-base-overlap", out


def test_an_arm_outside_the_allow_list_is_excluded_not_landed(repo, monkeypatch):
    _edit_docs(repo)
    _write(repo, "stray/loose.txt", "loose\n")
    monkeypatch.setattr(
        terminal_commit, "_own_prefix_files", lambda root, chunk, cache: ["stray/loose.txt"]
    )
    gated: list = []
    real_gate = terminal_commit._landing_integrity_gate

    def spy(root, all_paths, *rest, **kwargs):
        gated.extend(all_paths)
        return real_gate(root, all_paths, *rest, **kwargs)

    monkeypatch.setattr(terminal_commit, "_landing_integrity_gate", spy)
    out = _run(repo)
    assert out["committed"] is True, out
    assert out["foreign_excluded"] == ["stray/loose.txt"]
    assert "stray/loose.txt" not in gated
    assert "stray/loose.txt" not in _committed(repo)
