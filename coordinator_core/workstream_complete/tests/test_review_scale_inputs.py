"""Tests for review_scale_inputs: the review-scale quadruple measured from a SessionHistory."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import review_scale_inputs as rsi
from coordinator_core.workstream_complete import session_history as sh

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "11111111-2222-3333-4444-555555555555"
PEER = "99999999-8888-7777-6666-555555555555"


def _git(root, *args):
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, capture_output=True, text=True, check=True, **no_console_creationflags(),
    )
    return out.stdout.strip()


def _commit(root, rel, text, sid=None, extra=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    msg = f"edit {rel}\n\nbody\n"
    if sid:
        msg += f"\nSession-Id: {sid}\n"
    msg += extra
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def _floor(root, value, sid=SID):
    d = root / ".git" / "coordinator-sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "head_at_start").write_text(value + "\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "seed.txt", "seed\n")
    _floor(root, _git(root, "rev-parse", "HEAD"))
    return root


@pytest.fixture
def spawns(monkeypatch):
    calls = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, args, *a, **k):
            calls.append(args)
            super().__init__(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    return calls


def _measure(root, stage_paths, **kw):
    return rsi.measure_review_scale_inputs(root, sh.read_session_history(root, SID), stage_paths, **kw)


def test_shared_branch_measures_only_own_commits_and_named_uncommitted_paths(repo):
    a = _commit(repo, "a.py", "x = 1\ny = 2\n", SID)
    _commit(repo, "peer.py", "\n".join(f"p{i} = 1" for i in range(30)) + "\n", PEER)
    b = _commit(repo, "b.py", "z = 1\n", SID)
    t = _commit(repo, "state/tasks/t.md", "task\n" * 9, SID)
    (repo / "u.py").write_text("u = 1\nv = 2\nw = 3\n", encoding="utf-8")

    result = _measure(repo, ["u.py"])

    assert [s["sha"] for s in result.commit_slices] == [a, b, t]
    assert [s["diff_loc"] for s in result.commit_slices][:2] == [2, 1]
    assert result.commit_count == 3
    assert result.code_loc == 2 + 1 + 3
    assert result.gross_loc < 30
    assert result.zero_diff_commit_count == 1


def test_merge_is_excluded_from_the_count(repo):
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "side.py", "s = 1\n", SID)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main.py", "m = 1\n", SID)
    _git(repo, "merge", "--no-ff", "-q", "-m", f"merge side\n\nSession-Id: {SID}\n", "side")

    history = sh.read_session_history(repo, SID)
    assert any(c.is_merge for c in history.own)
    result = rsi.measure_review_scale_inputs(repo, history, [])
    assert result.commit_count == 2
    assert len(result.commit_slices) == 2


def test_unresolved_history_gives_the_unresolved_quadruple(repo):
    _commit(repo, "a.py", "x = 1\n", SID)
    unresolved = sh.read_session_history(repo, "00000000-0000-0000-0000-000000000000")
    assert not unresolved.resolved
    result = rsi.measure_review_scale_inputs(repo, unresolved, ["a.py"])
    assert result.quadruple == (None, None, None, None)
    assert result.commit_slices == []
    assert result.zero_diff_commit_count is None
    assert result.unattributed_shas is None


def test_empty_stage_paths_spawn_nothing(repo, spawns):
    _commit(repo, "a.py", "x = 1\n", SID)
    history = sh.read_session_history(repo, SID)
    spawns.clear()
    result = rsi.measure_review_scale_inputs(repo, history, [])
    assert spawns == []
    assert result.quadruple == (1, 1, 1, 1)


def test_named_tracked_path_spawns_exactly_one_git_process(repo, spawns):
    _commit(repo, "a.py", "x = 1\n", SID)
    (repo / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    history = sh.read_session_history(repo, SID)
    spawns.clear()
    result = rsi.measure_review_scale_inputs(repo, history, ["a.py"])
    assert len(spawns) == 1
    assert spawns[0][:2] == ["git", "--no-optional-locks"]
    assert result.gross_loc == 2 and result.commit_count == 1


def test_untracked_only_stage_paths_spawn_nothing(repo, spawns):
    (repo / "new.py").write_text("n = 1\n", encoding="utf-8")
    history = sh.read_session_history(repo, SID)
    spawns.clear()
    result = rsi.measure_review_scale_inputs(repo, history, ["new.py"])
    assert spawns == []
    assert result.quadruple == (1, 1, 0, 1)


def test_unreadable_untracked_path_is_unresolved_not_zero(repo):
    assert _measure(repo, ["ghost.py"]).quadruple == (None, None, None, None)


def test_dispatched_chunk_recovery_folds_untrailered_and_surfaces_foreign(repo):
    _commit(repo, "own.py", "o = 1\n", SID)
    bare = _commit(repo, "chunk.py", "c = 1\nc2 = 2\nc3 = 3\n")
    foreign = _commit(repo, "theirs.py", "t = 1\n", PEER)
    delivered = _commit(repo, "plan.py", "p = 1\n", PEER, extra="Deliverable-Id: dlv-x\n")

    result = _measure(
        repo,
        [],
        chunk_refs=[("C1", bare[:12]), ("C2", foreign), ("C3", delivered), ("C4", "f" * 40)],
        plan_deliverable_id="dlv-x",
    )

    assert [e["sha"] for e in result.attribution_gap["recovered"]] == [bare, delivered]
    assert result.attribution_gap["conflicting"] == [{"chunk": "C2", "sha": foreign, "committed_by": PEER}]
    assert result.commit_count == 3
    assert result.commit_slices[0]["attributed_via"].startswith("plan-spine C1")
    assert result.unattributed_shas == ()


def test_unattributed_shas_lists_window_commits_with_no_session_id(repo):
    bare = _commit(repo, "x.py", "x = 1\n")
    _commit(repo, "y.py", "y = 1\n", PEER)
    assert _measure(repo, []).unattributed_shas == (bare,)
