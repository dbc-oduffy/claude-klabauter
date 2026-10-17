"""since_base_overlap: a run's own re-fire checkpoints (plan match + stamped base on the walked
chain) never read as foreign; no-trailer, other-plan and off-chain commits still do."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git import since_base_overlap
from coordinator_core.git.tests.test_since_base_overlap import PATH, PLAN, _base_lines, _join, _Repo
from coordinator_core.ops.dispatch_emit.landing_integrity import SINCE_BASE_OVERLAP

OFF_CHAIN = "deadbeefdeadbeef"


def _scenario(tmp_path: Path, message_for):
    """Run base B0, peer commit P, then a commit adding one line whose message is
    ``message_for(P)``; the landing drops that line."""
    repo = _Repo(tmp_path)
    base_lines = _base_lines()
    base = repo.commit({PATH: _join(base_lines)}, "base")
    peer = repo.commit({PATH: _join(base_lines), "other.txt": "peer\n"}, "peer commit")
    added = list(base_lines) + ["checkpoint_line = 1\n"]
    repo.commit({PATH: _join(added), "other.txt": "peer\n"}, message_for(peer))
    repo.land(_join(base_lines))
    return base


def _trailers(plan: str, base: str) -> str:
    return f"checkpoint(C1): t\n\nCheckpoint-Plan: {plan}\nCheckpoint-Base: {base}"


def _findings(tmp_path: Path, base: str, plan):
    return since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None, plan=plan)


def test_own_checkpoint_stamped_with_an_on_chain_base_is_not_foreign(tmp_path: Path) -> None:
    base = _scenario(tmp_path, lambda peer: _trailers(PLAN, peer[:12]))

    assert _findings(tmp_path, base, PLAN) == []


def test_own_checkpoint_stamped_with_the_run_base_is_not_foreign(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    base_lines = _base_lines()
    base = repo.commit({PATH: _join(base_lines)}, "base")
    repo.commit({PATH: _join(base_lines + ["checkpoint_line = 1\n"])}, _trailers(PLAN, base[:12]))
    repo.land(_join(base_lines))

    assert _findings(tmp_path, base, PLAN) == []


def test_a_commit_without_trailers_is_foreign(tmp_path: Path) -> None:
    base = _scenario(tmp_path, lambda peer: "plain foreign commit")

    assert [f.reason for f in _findings(tmp_path, base, PLAN)] == [SINCE_BASE_OVERLAP]


def test_another_plans_checkpoint_is_foreign(tmp_path: Path) -> None:
    base = _scenario(tmp_path, lambda peer: _trailers("docs/plans/other-plan.md", peer[:12]))

    assert [f.reason for f in _findings(tmp_path, base, PLAN)] == [SINCE_BASE_OVERLAP]


def test_same_plan_checkpoint_with_an_off_chain_base_is_foreign(tmp_path: Path) -> None:
    base = _scenario(tmp_path, lambda peer: _trailers(PLAN, OFF_CHAIN))

    assert [f.reason for f in _findings(tmp_path, base, PLAN)] == [SINCE_BASE_OVERLAP]


def test_without_a_plan_only_the_exact_run_base_is_own(tmp_path: Path) -> None:
    base = _scenario(tmp_path, lambda peer: _trailers(PLAN, peer[:12]))

    assert [f.reason for f in _findings(tmp_path, base, None)] == [SINCE_BASE_OVERLAP]


def test_without_a_plan_the_exact_run_base_is_still_own(tmp_path: Path) -> None:
    repo = _Repo(tmp_path)
    base_lines = _base_lines()
    base = repo.commit({PATH: _join(base_lines)}, "base")
    repo.commit({PATH: _join(base_lines + ["checkpoint_line = 1\n"])}, _trailers(PLAN, base[:12]))
    repo.land(_join(base_lines))

    assert _findings(tmp_path, base, None) == []
