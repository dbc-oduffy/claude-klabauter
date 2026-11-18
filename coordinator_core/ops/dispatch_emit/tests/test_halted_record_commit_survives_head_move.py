"""A halt commit whose ref compare-and-swap is lost to a peer commit lands on the new HEAD, or refuses on overlap."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.run_artifacts import outcome_rel
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _call,
    _git,
    _write_record,
    repo,
)
from coordinator_core.warm import entry_seam

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CAS_LOST = (
    "compare-and-swap failed -- refs/heads/work/machine-a/2026-10-20 moved since 29b97d6c1a was captured. "
    "Refusing to retry silently: a retry needs the tree rebuilt against the new HEAD"
)


def _race_once(repo, racing_rel: str, monkeypatch) -> list:
    """The first ``ceremony.commit_v2`` call loses its ref CAS to a peer commit of ``racing_rel``."""
    calls: list = []
    real = entry_seam.reentrant_dispatch

    def racing(op, params, **kw):
        calls.append((op, list(params.get("paths", ()))))
        if op == "ceremony.commit_v2" and sum(1 for o, _ in calls if o == op) == 1:
            target = repo / racing_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("peer\n", encoding="utf-8")
            _git(["add", racing_rel], repo)
            _git(["commit", "-q", "-m", "peer lands while the halt commit runs"], repo)
            return {"committed": False, "sha": None, "error": CAS_LOST}
        return real(op, params, **kw)

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", racing)
    return calls


def test_a_halt_commit_raced_by_a_disjoint_commit_lands_its_artifacts_on_the_new_head(repo, monkeypatch):
    _write_record(repo, RUN)
    calls = _race_once(repo, "docs/peer.md", monkeypatch)

    out = _call(repo)

    assert out["committed"] is True and not out.get("error") and not out.get("refused")
    assert [o for o, _ in calls].count("ceremony.commit_v2") >= 2
    head = _git(["rev-parse", "HEAD"], repo).strip()
    assert out["sha"] == head
    assert _git(["show", "--name-only", "--format=", "HEAD"], repo).split() == [outcome_rel(RUN)]
    assert _git(["show", "HEAD~1:docs/peer.md"], repo) == "peer\n"
    assert _git(["log", "-2", "--format=%s"], repo).splitlines()[1] == "peer lands while the halt commit runs"
    assert "Halted-By: refusal" in _git(["log", "-1", "--format=%B"], repo)


def test_a_halt_commit_raced_by_a_commit_of_its_own_path_still_refuses_and_keeps_the_record(repo, monkeypatch):
    _write_record(repo, RUN)
    _race_once(repo, outcome_rel(RUN), monkeypatch)

    out = _call(repo)
    peer_head = _git(["rev-parse", "HEAD"], repo).strip()

    assert out["committed"] is False
    assert out["refused"] == "ref-moved-overlap"
    assert _git(["log", "-1", "--format=%s"], repo).strip() == "peer lands while the halt commit runs"
    assert _git(["show", f"{peer_head}:{outcome_rel(RUN)}"], repo) == "peer\n"
    for name in ("ask.md", "pm-intent.md", "run.json", "halted.json"):
        assert (repo / RECORD_DIR_ROOT / RUN / name).is_file()
