"""Tests for the hand-shipped sizing sweep leg."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.ops import promote_hand_shipped_sizings as subject
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "11111111-1111-1111-1111-111111111111"
OTHER_SESSION = "22222222-2222-2222-2222-222222222222"
SIZING = "state/sizings/a.yaml"
DID = "dlv-run-1"
WRITES = ("src/a.py", "src/b.py")


def _git(repo, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        env={**os.environ, **(env or {})}, **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    _git(tmp_path, "add", "--", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed", env={"GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"})
    return tmp_path


def _sizing(repo, status="routed"):
    path = repo / SIZING
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join([
        "schema: sizing-object",
        "intent: Test intent, verbatim.",
        "estimate:",
        "  tshirt: M",
        "  provisional: true",
        "route: spec-dispatch",
        "detents: []",
        "fork: null",
        "xl_exit: null",
        f"status: {status}",
        f'deliverable_id: "{DID}"',
        "premise:",
        "  provenance: read",
        "  evidence: test fixture, no real premise verified",
    ])
    path.write_text(body + "\n", encoding="utf-8")


def _run_dir(repo, run_id="r1", writes=WRITES, submitted_at=None, reconciled=False):
    run_dir = repo / "scratch/warp" / run_id
    run_dir.mkdir(parents=True)
    if writes:
        marker = render_marker(CommitRequest(chunks=(ChunkCommit(id="C", title="t", paths=tuple(writes)),), version=1))
        (run_dir / "commit-request.txt").write_text((marker or "") + "\n", encoding="utf-8")
    (run_dir / "submission.json").write_text(
        json.dumps({
            "session_id": SESSION,
            "sizing_rel": SIZING,
            "submitted_at": submitted_at if submitted_at is not None else time.time() - 3600,
        }),
        encoding="utf-8",
    )
    if reconciled:
        (run_dir / "reconciled.json").write_text("{}", encoding="utf-8")
    return run_dir


def _commit(repo, paths, session=SESSION, did="dlv-pickup", date=None):
    for rel in paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(target.read_text(encoding="utf-8") + "x" if target.exists() else "x", encoding="utf-8")
    _git(repo, "add", "--", *paths)
    message = f"work\n\nSession-Id: {session}\nDeliverable-Id: {did}\n"
    env = {"GIT_COMMITTER_DATE": date} if date else None
    _git(repo, "commit", "-q", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _status(repo):
    return subject.sizing_scalar(repo, SIZING, "status")


def test_hand_shipped_session_match_with_wrong_trailer_ships_sizing(repo):
    _sizing(repo)
    run_dir = _run_dir(repo)
    sha = _commit(repo, WRITES)
    counters = subject.run(repo)
    assert _status(repo) == "shipped"
    assert counters["promoted"] == 1
    marker = json.loads((run_dir / "reconciled.json").read_text(encoding="utf-8"))
    assert marker["shas"] == [sha] and marker["deliverable_id"] == DID


def test_hand_shipped_deliverable_match_from_other_session_ships_sizing(repo):
    _sizing(repo)
    _run_dir(repo)
    _commit(repo, WRITES, session=OTHER_SESSION, did=DID)
    assert subject.run(repo)["promoted"] == 1
    assert _status(repo) == "shipped"


def test_hand_shipped_union_across_commits_covers_writes(repo):
    _sizing(repo)
    run_dir = _run_dir(repo)
    first = _commit(repo, WRITES[:1])
    second = _commit(repo, WRITES[1:])
    subject.run(repo)
    assert json.loads((run_dir / "reconciled.json").read_text(encoding="utf-8"))["shas"] == [second, first]


def test_hand_shipped_partial_writes_stay_routed(repo):
    _sizing(repo)
    _run_dir(repo)
    _commit(repo, WRITES[:1])
    assert subject.run(repo)["promoted"] == 0
    assert _status(repo) == "routed"


def test_hand_shipped_commit_before_submission_stays_routed(repo):
    _sizing(repo)
    _run_dir(repo)
    _commit(repo, WRITES, date="2021-01-01T00:00:00Z")
    assert subject.run(repo)["promoted"] == 0
    assert _status(repo) == "routed"


def test_hand_shipped_unrelated_session_and_deliverable_stays_routed(repo):
    _sizing(repo)
    _run_dir(repo)
    _commit(repo, WRITES, session=OTHER_SESSION)
    assert subject.run(repo)["promoted"] == 0


def test_hand_shipped_already_reconciled_is_untouched(repo):
    _sizing(repo)
    _run_dir(repo, reconciled=True)
    _commit(repo, WRITES)
    assert subject.run(repo)["candidates"] == 0
    assert _status(repo) == "routed"


def test_hand_shipped_declined_sizing_is_not_a_candidate(repo):
    _sizing(repo, status="declined")
    _run_dir(repo)
    _commit(repo, WRITES)
    assert subject.run(repo)["candidates"] == 0
    assert _status(repo) == "declined"


def test_hand_shipped_ship_refusal_is_counted_and_not_reconciled(repo, monkeypatch):
    _sizing(repo)
    run_dir = _run_dir(repo)
    _commit(repo, WRITES)
    monkeypatch.setattr(subject, "_ship", lambda root, rel: "refused: superseded")
    counters = subject.run(repo)
    assert counters["refused"] == 1 and counters["promoted"] == 0
    assert not (run_dir / "reconciled.json").exists()


def test_hand_shipped_no_declared_writes_is_never_promoted(repo):
    _sizing(repo)
    _run_dir(repo, writes=())
    _commit(repo, WRITES)
    counters = subject.run(repo)
    assert counters["no_declared_writes"] == 1 and counters["promoted"] == 0
    assert _status(repo) == "routed"


def test_hand_shipped_zero_candidates_spawns_nothing(repo, monkeypatch):
    spawns = []
    monkeypatch.setattr(subject, "run_git", lambda *a, **k: spawns.append(a))
    assert subject.run(repo)["candidates"] == 0
    assert spawns == []


def test_hand_shipped_one_git_spawn_for_many_candidates(repo, monkeypatch):
    _sizing(repo)
    _run_dir(repo, "r1")
    _run_dir(repo, "r2")
    _commit(repo, WRITES)
    calls = []
    real = git_run.run_git
    monkeypatch.setattr(subject, "run_git", lambda *a, **k: calls.append(a) or real(*a, **k))
    subject.run(repo)
    assert len(calls) == 1
