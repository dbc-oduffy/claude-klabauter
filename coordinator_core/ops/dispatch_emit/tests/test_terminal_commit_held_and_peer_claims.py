"""dispatch.terminal_commit never lands a held chunk's declared writes, and refuses a path
carrying a live peer session's write claim."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import liveness
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PEER = "held-peer"
SELF = "held-self"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


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
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return root


def _run(repo: Path, request: CommitRequest, **params) -> dict:
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


def _two_chunks(repo: Path, second_paths=("b.py",)) -> CommitRequest:
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    return CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t1", paths=("a.py",)),
            ChunkCommit(id="C2", title="t2", paths=tuple(second_paths)),
        )
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def test_held_chunk_paths_are_subtracted_from_the_commit(repo):
    # C2 is not in incomplete_chunks: the held list alone must keep b.py out.
    out = _run(repo, _two_chunks(repo), held=[{"chunk": "C2", "reason": "halted"}])
    assert out["committed"] is True, out
    assert _committed(repo) == ["a.py"]
    assert out["stranded"] == {"C2": ["b.py"]}
    assert out["incomplete_reasons"] == {"C2": "held: halted"}


def test_held_spine_writes_are_subtracted_even_when_the_marker_lacks_the_chunk(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(
        "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: H1\n  title: held\n  change_kind: code-edit\n  surface: docs/held.md\n"
        "  writes:\n    - docs/held.md\n  disposition: open\n  deferred: false\n```\n",
        encoding="utf-8", newline="\n",
    )
    (repo / "docs" / "held.md").write_text("held\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path="docs/plan.md",
    )
    out = _run(
        repo, request, held=[{"chunk": "H1", "reason": "halted"}],
        plan_path="docs/plan.md",
    )
    assert out["committed"] is True, out
    assert "docs/held.md" not in _committed(repo)
    assert "a.py" in _committed(repo)


def test_a_path_declared_by_held_and_landed_chunks_refuses(repo):
    request = _two_chunks(repo, second_paths=("b.py", "a.py"))
    head = _git(["rev-parse", "HEAD"], repo)
    out = _run(repo, request, held=[{"chunk": "C2", "reason": "halted"}])
    assert out["committed"] is False
    assert out["refused"] == "held-landed-overlap"
    assert out["overlap"] == {"a.py": {"held": ["C2"], "landed": ["C1"]}}
    assert "a.py" in out["error"] and "C1" in out["error"] and "C2" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_the_overlap_refusal_prints_a_release_param_that_lands(repo):
    import re

    request = _two_chunks(repo, second_paths=("b.py", "a.py"))
    held = [{"chunk": "C2", "reason": "halted"}]
    refused = _run(repo, request, held=held)
    assert refused["refused"] == "held-landed-overlap"
    printed = re.search(r"params\.release_held=(\[[^\]]*\])", refused["error"])
    assert printed is not None, refused["error"]
    assert json.loads(printed.group(1)) == ["C2"]
    out = _run(repo, request, held=held, release_held=json.loads(printed.group(1)))
    assert out["committed"] is True, out
    assert "a.py" in _committed(repo)


def test_a_live_peer_write_claim_refuses_the_commit(repo):
    request = _two_chunks(repo)
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    head = _git(["rev-parse", "HEAD"], repo)
    out = _run(repo, request)
    assert out["committed"] is False
    assert out["refused"] == "peer-claim"
    assert out["peer_claims"] == [{"path": "a.py", "session": PEER}]
    assert "a.py" in out["error"] and PEER in out["error"] and "Wait for the peer" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_two_peers_over_two_paths_refuse_with_the_path_level_contract(repo):
    request = _two_chunks(repo)
    other = "held-peer-2"
    session_core.init(PEER, cwd=str(repo))
    session_core.init(other, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    session_scope.touch(other, "b.py", cwd=str(repo))
    head = _git(["rev-parse", "HEAD"], repo)
    out = _run(repo, request)
    assert out["refused"] == "peer-claim"
    assert out["peer_claim_contract"] == "path-level"
    assert out["peer_claims_by_session"] == [
        {"session": PEER, "paths": ["a.py"]}, {"session": other, "paths": ["b.py"]},
    ]
    assert [r["route"] for r in out["peer_claim_routes"]] == ["withhold", "peer-commits", "release"]
    assert f"a.py held by {PEER}" in out["error"] and f"b.py held by {other}" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_an_abandoned_live_peer_claim_does_not_refuse_the_commit(repo, monkeypatch):
    request = _two_chunks(repo)
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "a.py", cwd=str(repo))
    monkeypatch.setattr(
        commit_v2.session_liveness, "session_live", lambda sid, cwd=None: True
    )
    sdir = Path(session_core.session_dir(PEER, str(repo)))
    meta = sdir / "meta.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["last_activity"] = "2020-01-01T00:00:00Z"
    meta.write_text(json.dumps(data), encoding="utf-8")
    old = time.time() - 2 * liveness._ABANDONMENT_WINDOW_SEC
    for f in sdir.iterdir():
        if f.is_file():
            os.utime(f, (old, old))

    out = _run(repo, request)

    assert out["committed"] is True, out
    assert _committed(repo) == ["a.py", "b.py"]
    body = _git(["log", "-1", "--format=%B"], repo)
    assert f"Absorbed-Peer-Claim {PEER} a.py" in body, body


def _peer_run(repo: Path, run_id: str, session: str, writes: list, minted_at=None) -> None:
    from coordinator_core.ops.dispatch_emit import run_token

    session_core.init(session, cwd=str(repo))
    rdir = repo / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "manifest.json").write_text(json.dumps({"rows": [{"writes": writes}]}), encoding="utf-8")
    token = run_token.mint(repo, run_id, "run.mjs", session_id=session)
    if minted_at is not None:
        path = run_token._record_path(repo, run_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["minted_at"] = minted_at
        path.write_text(json.dumps(record), encoding="utf-8")
    return token


def test_a_live_run_manifest_path_is_peer_held_not_undeclared_dirty(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "x.py").write_text("x\n", encoding="utf-8")
    (repo / "y.py").write_text("y\n", encoding="utf-8")
    _peer_run(repo, "peer-run-1", PEER, ["x.py"])
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),), session_id=SELF,
    )
    out = terminal_commit._undeclared_dirty(repo, request)
    assert out["undeclared_dirty"] == ["y.py"], out
    assert out["undeclared_dirty_peer_held"] == 1


def test_a_declared_path_a_live_run_manifest_declares_refuses(repo):
    request = _two_chunks(repo)
    _peer_run(repo, "peer-run-1", PEER, ["a.py"])
    head = _git(["rev-parse", "HEAD"], repo)
    out = _run(repo, request)
    assert out["committed"] is False
    assert out["refused"] == "peer-claim"
    assert out["peer_claims"] == [{"path": "a.py", "session": PEER}]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_clean_declared_path_a_peer_holds_does_not_refuse(repo):
    request = _two_chunks(repo)
    _git(["add", "a.py"], repo)
    _git(["commit", "-q", "-m", "a"], repo)
    _peer_run(repo, "peer-run-1", PEER, ["a.py"])
    out = _run(repo, request)
    assert out.get("refused") is None, out
    assert _committed(repo) == ["b.py"]


@pytest.mark.parametrize("committer", ["senior", "junior"])
def test_mutual_declaration_refuses_only_the_junior_run(repo, monkeypatch, committer):
    now = time.time()
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),))
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    tokens = {
        "senior": _peer_run(repo, "run-senior", SELF, ["a.py"], minted_at=now - 100),
        "junior": _peer_run(repo, "run-junior", PEER, ["a.py"], minted_at=now - 10),
    }
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF if committer == "senior" else PEER)
    out = _run(repo, request, run_id=f"run-{committer}", run_token=tokens[committer])
    if committer == "senior":
        assert out["committed"] is True, out
    else:
        assert out["committed"] is False, out
        assert out["refused"] == "peer-claim"
        assert out["peer_claims"] == [{"path": "a.py", "session": SELF}]


@pytest.mark.parametrize("claimed", [True, False])
def test_a_run_never_contests_its_own_manifest_when_its_session_id_is_dropped(repo, claimed):
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),))
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "x.py").write_text("x\n", encoding="utf-8")
    token = _peer_run(repo, "own-run", SELF, ["a.py", "x.py"])
    extra = {"run_token": token} if claimed else {}
    assert terminal_commit._peer_held(repo, None, ["x.py"]) == {"x.py"}
    assert terminal_commit._peer_held(repo, None, ["x.py"], run_id="own-run") == set()
    out = _run(repo, request, run_id="own-run", session_id="not-a-uuid", **extra)
    assert out.get("refused") != "peer-claim", out
    assert out["committed"] is True, out
    assert out.get("undeclared_dirty_peer_held", 0) == 0, out


def test_the_runs_own_session_claim_still_commits(repo):
    request = _two_chunks(repo)
    session_core.init(SELF, cwd=str(repo))
    session_scope.touch(SELF, "a.py", cwd=str(repo))
    out = _run(repo, request)
    assert out["committed"] is True, out
    assert _committed(repo) == ["a.py", "b.py"]
    assert "Absorbed-Peer-Claim" not in _git(["log", "-1", "--format=%B"], repo)


def _plan_run(repo: Path, request: CommitRequest, emitted_at: float, **params) -> dict:
    """A plan run mints no run token: its script's mtime is when it was minted."""
    script = repo / "plan-run.workflow.mjs"
    script.write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    os.utime(script, (emitted_at, emitted_at))
    return terminal_commit._handler(
        {"script_path": script.name, "incomplete_chunks": [], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


_SELF_UUID = "11111111-2222-3333-4444-555555555555"


def _record_write(repo: Path, rel: str, text: str) -> None:
    import hashlib

    from coordinator_core.session import touch_record

    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / _SELF_UUID)
    touch_record.append_event(
        sink, session_id=_SELF_UUID, agent_id=None, verb="T", path=rel,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), kind="w",
    )


def test_a_tokenless_plan_run_emitted_before_a_junior_peer_run_commits(repo):
    now = time.time()
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),))
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    _peer_run(repo, "run-junior", PEER, ["a.py"], minted_at=now - 10)
    out = _plan_run(repo, request, now - 100)
    assert out["committed"] is True, out


def test_a_tokenless_plan_run_emitted_after_a_peer_run_still_refuses(repo):
    now = time.time()
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),))
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    _peer_run(repo, "run-senior", PEER, ["a.py"], minted_at=now - 100)
    out = _plan_run(repo, request, now - 10)
    assert out["refused"] == "peer-claim", out


def test_a_junior_runs_hunk_in_a_carried_file_still_refuses_on_blob_drift(repo, monkeypatch):
    """Seniority drops the junior's declaration, never its bytes: a hunk it already wrote into a
    file the senior commits whole differs from the senior's recorded write and refuses."""
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SELF_UUID)
    now = time.time()
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),))
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    _record_write(repo, "a.py", "a\n")
    _peer_run(repo, "run-junior", PEER, ["a.py"], minted_at=now - 10)
    (repo / "a.py").write_text("a\njunior_hunk = 1\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)
    out = _plan_run(repo, request, now - 100, session_id=_SELF_UUID)
    assert out["committed"] is False, out
    assert out["refused"] == "blob-drift", out
    assert _git(["rev-parse", "HEAD"], repo) == head
