"""End to end: another run's `dispatch.terminal_commit` never lands a file a live peer run's
manifest declares (`rows[].writes`) as `run_owned`, even with no touch claim on it, and the control
(peer token expired, or its session dead) admits the same file, so the test discriminates."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit import review_edit_source, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
PEER = "5e1f0000-0000-4000-8000-0000000000b2"
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

_REVIEWED = reviewed()
_PEER_EDIT = "peer_half_written_edit = 1\n"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "src").mkdir()
    (root / "src" / "op.py").write_text("original\n", encoding="utf-8")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    session_core.init(SELF, cwd=str(root))
    session_core.init(PEER, cwd=str(root))
    (root / "src" / "a.py").write_text("a\n", encoding="utf-8")
    return root


def _peer_run_declares(repo: Path, *writes: str) -> None:
    """Run B: another session's live run whose manifest names `writes`; no touch claim is made."""
    manifest_dir = repo / "state" / "warp" / "run-b"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": list(writes)}], "doc_writes": []}),
        encoding="utf-8",
    )
    run_token.mint(repo, "run-b", "b.mjs", session_id=PEER)
    (repo / "src" / "op.py").write_text(_PEER_EDIT, encoding="utf-8")


def _run_a(repo: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    review = {**_REVIEWED, "prep": {"slice_files": ["src/op.py"]}}
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": review, "session_id": SELF},
        repo_root=repo / ".git",
    )


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


def test_a_live_peer_runs_manifest_path_is_never_landed_as_run_owned(repo):
    _peer_run_declares(repo, "src/op.py")
    out = _run_a(repo)
    assert out["committed"] is True, out
    assert "src/op.py" not in out["run_owned"]
    assert _committed(repo) == ["src/a.py"]
    assert _git(["show", "HEAD:src/op.py"], repo) == "original\n"
    assert (repo / "src" / "op.py").read_text(encoding="utf-8") == _PEER_EDIT


def test_control_an_expired_peer_token_admits_the_same_path(repo):
    _peer_run_declares(repo, "src/op.py")
    record_path = run_token._record_path(repo, "run-b")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["minted_at"] = 1.0
    record_path.write_text(json.dumps(record), encoding="utf-8")
    out = _run_a(repo)
    assert out["committed"] is True, out
    assert out["run_owned"] == ["src/op.py"]
    assert _git(["show", "HEAD:src/op.py"], repo) == _PEER_EDIT


def test_control_a_dead_peer_session_admits_the_same_path(repo, monkeypatch):
    _peer_run_declares(repo, "src/op.py")
    real = touch_record.session_live
    monkeypatch.setattr(
        touch_record, "session_live", lambda sid, cwd=None: False if sid == PEER else real(sid, cwd)
    )
    out = _run_a(repo)
    assert out["committed"] is True, out
    assert out["run_owned"] == ["src/op.py"]


def _live_same_session_sibling(repo: Path, declared: str, *dirtied: str) -> None:
    """Run B: a RUNNING (fresh token, live session, no halted marker) sibling run of the same
    session in the same tree. It declares `declared` and has also dirtied each of `dirtied`
    without declaring it; every file carries the session's touch claim."""
    from coordinator_core.session import scope as session_scope

    manifest_dir = repo / "state" / "warp" / "run-b"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [declared]}], "doc_writes": []}),
        encoding="utf-8",
    )
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    (repo / "other").mkdir(exist_ok=True)
    for rel in (declared, *dirtied):
        (repo / rel).write_text("sibling\n", encoding="utf-8")
        session_scope.touch(SELF, rel, cwd=str(repo))


def _run_a_same_session(repo: Path) -> dict:
    from coordinator_core.session import scope as session_scope

    session_scope.touch(SELF, "src/a.py", cwd=str(repo))
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),))
    (repo / "run-a.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run-a.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SELF},
        repo_root=repo / ".git",
    )


def test_a_running_same_session_sibling_keeps_declared_and_dirtied_files_out_of_the_commit(repo):
    _live_same_session_sibling(repo, "src/b.py", "src/c.py")
    out = _run_a_same_session(repo)
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/a.py"]
    assert _git(["ls-files", "src/b.py", "src/c.py"], repo).strip() == ""
    assert (repo / "src" / "b.py").read_text(encoding="utf-8") == "sibling\n"
    assert (repo / "src" / "c.py").read_text(encoding="utf-8") == "sibling\n"
    porcelain = _git(["status", "--porcelain", "--untracked-files=all"], repo)
    assert "src/b.py" in porcelain and "src/c.py" in porcelain


def test_a_running_same_session_sibling_declared_path_outside_the_shared_dir_stays_out(repo):
    _live_same_session_sibling(repo, "other/b.py")
    out = _run_a_same_session(repo)
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/a.py"]
    assert _git(["ls-files", "other/b.py"], repo).strip() == ""


def test_control_a_stale_same_session_sibling_token_releases_its_declared_path(repo):
    _live_same_session_sibling(repo, "other/b.py")
    record_path = run_token._record_path(repo, "run-b")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["minted_at"] = 1.0
    record_path.write_text(json.dumps(record), encoding="utf-8")
    out = _run_a_same_session(repo)
    assert out["committed"] is True, out
    assert "other/b.py" in _committed(repo)


def test_each_claim_check_lists_the_token_store_once(repo, monkeypatch):
    _peer_run_declares(repo, "src/op.py")
    listings: list = []
    checks: list = []
    real_held = run_token.held_records
    real_claims = warp_run_deliverable.live_run_write_claims
    real_sole = review_edit_source.sole_run_attested_writes
    in_sole: list = []

    def held(r):
        listings.append(1)
        if in_sole:
            sole.append(1)
        return real_held(r)

    def sole_run(*a, **k):
        in_sole.append(1)
        try:
            return real_sole(*a, **k)
        finally:
            in_sole.pop()

    sole: list = []
    monkeypatch.setattr(run_token, "held_records", held)
    monkeypatch.setattr(review_edit_source, "sole_run_attested_writes", sole_run)
    monkeypatch.setattr(
        warp_run_deliverable, "live_run_write_claims",
        lambda *a, **k: checks.append(1) or real_claims(*a, **k),
    )
    out = _run_a(repo)
    assert out["committed"] is True, out
    assert checks
    # 3f98cefad2 added one listing that is not a claim check: the sole-run review-writes attribution
    # (`review_edit_source.sole_run_attested_writes`). Every claim check still lists the store once.
    assert len(sole) == 1
    assert len(listings) - len(sole) == len(checks)
