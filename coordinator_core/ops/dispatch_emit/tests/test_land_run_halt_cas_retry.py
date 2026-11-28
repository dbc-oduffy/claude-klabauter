"""A halt commit that loses its ref compare-and-swap retries on the new HEAD, or names why it stopped."""

from __future__ import annotations

import pytest

from coordinator_core.git import commit as git_commit
from coordinator_core.git.ref_cas_retry import REF_CAS_ATTEMPTS
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_land_run_halt_record import (  # noqa: F401
    RUN,
    _call,
    _git,
    _names,
    _record,
    _write,
    repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

CAS_LOST = (
    "compare-and-swap failed -- refs/heads/work/machine-a/2026-10-20 moved since 29b97d6c1a was captured. "
    "Refusing to retry silently: a retry needs the tree rebuilt against the new HEAD"
)


def _lose_first(repo, monkeypatch, racing_rel: str | None, times: int = 1) -> list:
    """The first ``times`` ``commit_paths`` calls lose the CAS; each first lands a peer commit of ``racing_rel``."""
    calls: list = []
    real = git_commit.commit_paths

    def racing(root, paths, message, **kw):
        calls.append(list(paths))
        if len(calls) <= times:
            if racing_rel is not None:
                _write(repo, racing_rel, f"peer {len(calls)}\n")
                _git(repo, "add", racing_rel)
                _git(repo, "commit", "-q", "-m", "peer lands while the halt commit runs")
            raise git_commit.CommitRefused(CAS_LOST)
        return real(root, paths, message, **kw)

    monkeypatch.setattr(git_commit, "commit_paths", racing)
    return calls


def test_a_halt_commit_raced_by_a_disjoint_commit_lands_on_the_new_head(repo, monkeypatch):
    _record(repo)
    calls = _lose_first(repo, monkeypatch, "docs/peer2.md")

    out = _call(repo, halted={"halted": "refusal", "reason": "r"})

    assert out["committed"] is True and not out.get("error") and not out.get("refused")
    assert len(calls) == 2
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert out["sha"] == head
    assert f"{RECORD_DIR_ROOT}/{RUN}/ask.md" in _names(repo, head)
    assert _git(repo, "show", "HEAD~1:docs/peer2.md") == "peer 1\n"


def test_a_halt_commit_raced_by_a_commit_of_its_own_path_refuses_with_overlap(repo, monkeypatch):
    _record(repo)
    calls = _lose_first(repo, monkeypatch, f"{RECORD_DIR_ROOT}/{RUN}/ask.md")

    out = _call(repo, halted={"halted": "refusal", "reason": "r"})

    assert out["committed"] is False
    assert out["refused"] == "ref-moved-overlap"
    assert out["error"] and len(calls) == 1


def test_a_halt_commit_that_keeps_losing_names_exhaustion(repo, monkeypatch):
    _record(repo)
    calls = _lose_first(repo, monkeypatch, None, times=REF_CAS_ATTEMPTS + 5)

    out = _call(repo, halted={"halted": "refusal", "reason": "r"})

    assert out["committed"] is False
    assert out["refused"] == "ref-moved-exhausted"
    assert out["error"] and len(calls) == REF_CAS_ATTEMPTS
