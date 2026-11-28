"""Halt close and per-row commits that lose the ref compare-and-swap retry on the new HEAD, or name why they stopped."""

from __future__ import annotations

import pytest

from coordinator_core.git import commit as git_commit
from coordinator_core.git.ref_cas_retry import REF_CAS_ATTEMPTS
from coordinator_core.ops.dispatch_emit import halted_run_close_commit as close_commit
from coordinator_core.ops.dispatch_emit.tests.test_halted_rows_commit import (  # noqa: F401
    _call as _rows_call,
    _git,
    repo as rows_repo,
)
from coordinator_core.warm import entry_seam

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CAS_LOST = (
    "compare-and-swap failed -- refs/heads/work/machine-a/2026-10-20 moved since 29b97d6c1a was captured. "
    "Refusing to retry silently: a retry needs the tree rebuilt against the new HEAD"
)
RUN = "ask-20261122T000000-aaaaaa"
HALTED_REL = f"state/warp/{RUN}/halted.json"


def _peer_commit(root, rel: str, body: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(["add", rel], root)
    _git(["commit", "-q", "-m", "peer lands while the record commit runs"], root)


@pytest.fixture
def close_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / HALTED_REL).parent.mkdir(parents=True)
    (root / HALTED_REL).write_text("{}\n", encoding="utf-8")
    return root


def _lose_commit_paths(root, monkeypatch, racing_rel, times: int = 1) -> list:
    calls: list = []
    real = git_commit.commit_paths

    def racing(r, paths, message, **kw):
        calls.append(list(paths))
        if len(calls) <= times:
            if racing_rel is not None:
                _peer_commit(root, racing_rel, f"peer {len(calls)}\n")
            raise git_commit.CommitRefused(CAS_LOST)
        return real(r, paths, message, **kw)

    monkeypatch.setattr(git_commit, "commit_paths", racing)
    return calls


def _close(root) -> dict:
    return close_commit.commit_close_records(root, RUN, root / "state" / "warp" / RUN, "close", closed=False)


def test_a_close_commit_raced_by_a_disjoint_commit_lands_on_the_new_head(close_repo, monkeypatch):
    calls = _lose_commit_paths(close_repo, monkeypatch, "docs/peer.md")

    out = _close(close_repo)

    assert "records_commit_error" not in out and out["records_commit"] == _git(["rev-parse", "HEAD"], close_repo).strip()
    assert len(calls) == 2
    assert _git(["show", "--name-only", "--format=", "HEAD"], close_repo).split() == [HALTED_REL]
    assert _git(["show", "HEAD~1:docs/peer.md"], close_repo) == "peer 1\n"


def test_a_close_commit_raced_by_a_commit_of_its_own_path_names_overlap(close_repo, monkeypatch):
    calls = _lose_commit_paths(close_repo, monkeypatch, HALTED_REL)

    out = _close(close_repo)

    assert "records_commit" not in out
    assert out["records_commit_refused"] == "ref-moved-overlap" and out["records_commit_error"]
    assert len(calls) == 1


def test_a_close_commit_that_keeps_losing_names_exhaustion(close_repo, monkeypatch):
    calls = _lose_commit_paths(close_repo, monkeypatch, None, times=REF_CAS_ATTEMPTS + 5)

    out = _close(close_repo)

    assert out["records_commit_refused"] == "ref-moved-exhausted" and out["records_commit_error"]
    assert len(calls) == REF_CAS_ATTEMPTS


def _race_rows(root, monkeypatch, racing_rel, times: int = 1) -> list:
    calls: list = []
    real = entry_seam.reentrant_dispatch

    def racing(op, params, **kw):
        if op != "ceremony.commit_v2":
            return real(op, params, **kw)
        calls.append(list(params.get("paths", ())))
        if len(calls) <= times:
            if racing_rel is not None:
                _peer_commit(root, racing_rel, f"peer {len(calls)}\n")
            return {"committed": False, "sha": None, "error": CAS_LOST}
        return real(op, params, **kw)

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", racing)
    return calls


def test_a_row_commit_raced_by_a_disjoint_commit_lands_on_the_new_head(rows_repo, monkeypatch):
    calls = _race_rows(rows_repo, monkeypatch, "docs/peer.md")

    out = _rows_call(rows_repo, ["r2"])

    assert out["ok"] is True, out
    assert len(calls) == 2
    assert out["landed"][0]["sha"] == _git(["rev-parse", "HEAD"], rows_repo).strip()
    assert _git(["show", "--name-only", "--format=", "HEAD"], rows_repo).split() == ["b.py"]


def test_a_row_commit_raced_by_a_commit_of_its_own_path_is_refused_with_overlap(rows_repo, monkeypatch):
    calls = _race_rows(rows_repo, monkeypatch, "b.py")

    out = _rows_call(rows_repo, ["r2"])

    assert out["ok"] is False and out["landed"] == []
    assert out["refused"][0]["refused"] == "ref-moved-overlap" and out["refused"][0]["reason"]
    assert len(calls) == 1


def test_a_row_commit_that_keeps_losing_names_exhaustion(rows_repo, monkeypatch):
    calls = _race_rows(rows_repo, monkeypatch, None, times=REF_CAS_ATTEMPTS + 5)

    out = _rows_call(rows_repo, ["r2"])

    assert out["ok"] is False
    assert out["refused"][0]["refused"] == "ref-moved-exhausted"
    assert len(calls) == REF_CAS_ATTEMPTS
