"""
coordinator_core.ops.ceremony.tests.test_commit_v2_pre_commit_gates

Tests for the two gates reinstated into `ceremony.commit_v2` -- `carry_gate`
and `op_scope_coverage_gate` -- discharging the implementation half of the P1
`the-commit-v2-route-runs-none-of-the-fou`, whose spike verdict is
docs/research/spike-verdicts/2026-08-30-what-the-four-commit-gates-actually-cost.md.

WHAT THESE ASSERT, and the ordering assertion is the one that matters. A gate
that refuses AFTER the commit has landed is not a gate, so the refusal tests
check HEAD is unmoved as well as checking the returned envelope. The relay
step tested by the sibling file is the deliberate opposite (runs after, may
never fail the commit); the two live either side of the same `commit_paths`
call and it would be easy to wire one like the other.

ALSO ASSERTED: the two gates NOT reinstated stay out. `deletion_block_gate`
would refuse ~86% of this repo's commits (346 of the last 400 delete a path;
none carries the "Step 2.67" block its Assertion-3 requires -- that block is a
`workstream-complete` ceremony convention and `commit_v2` is the general
committer). `dirty_tree_gate`'s only scope-reachable axis is already refused
earlier and louder by `commit_paths` itself. Both omissions are load-bearing
rather than incidental, so a regression test pins them: re-adding either
without revisiting the measurement is the failure mode.

Gate refusals are provoked by patching the gate function, not by hand-building
a repo state that trips the real predicate. The predicates have their own unit
coverage in `test_commit_gates.py`; what is untested without this file is
whether `_handler` consults them at all, and whether it does so before landing.

All git operations run against a throwaway repo created fresh under
`tmp_path` -- never the working repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.ceremony.commit_gates import GateOutcome

# Spawns real external `git` processes; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    seed = repo / "seed.md"
    seed.write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "seed.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _head(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _edit(repo: Path, rel: str = "seed.md") -> str:
    (repo / rel).write_text("changed\n", encoding="utf-8")
    return rel


def test_ordinary_commit_passes_both_gates(tmp_path: Path):
    """The gates are wired but do not refuse an ordinary in-scope commit."""
    repo = _init_repo(tmp_path)
    rel = _edit(repo)
    before = _head(repo)

    result = _call(repo, {"paths": [rel], "message": "ordinary edit\n"})

    assert result["committed"] is True, result
    assert _head(repo) != before


@pytest.mark.parametrize("gate_name", ["carry_gate", "op_scope_coverage_gate"])
def test_a_failing_gate_refuses_and_nothing_lands(tmp_path: Path, monkeypatch, gate_name):
    """A gate refusal returns a structured error AND leaves HEAD unmoved.

    HEAD is the assertion that distinguishes a gate from a report: the
    envelope alone would look identical if the gate ran after `commit_paths`
    and merely relabelled a commit that had already landed.
    """
    repo = _init_repo(tmp_path)
    rel = _edit(repo)
    before = _head(repo)

    monkeypatch.setattr(
        commit_v2,
        gate_name,
        lambda *a, **k: GateOutcome(
            passed=False, skipped=False, diagnostics=["synthetic refusal"]
        ),
    )

    result = _call(repo, {"paths": [rel], "message": "should not land\n"})

    assert result["committed"] is False, result
    assert gate_name in result["error"]
    assert "synthetic refusal" in result["error"]
    assert _head(repo) == before, "gate refused but the commit landed anyway"


def test_empty_gate_scope_skips_the_gates(tmp_path: Path, monkeypatch):
    """No in-scope paths means no gate call at all, not a gate call with an
    empty set -- `commit_paths` owns the empty-pathspec refusal and says so
    better than a gate would.
    """
    repo = _init_repo(tmp_path)
    called = []
    monkeypatch.setattr(
        commit_v2, "carry_gate",
        lambda *a, **k: called.append("carry") or GateOutcome(True, False, []),
    )

    result = _call(repo, {"paths": [], "message": "nothing in scope\n"})

    assert result["committed"] is False
    assert called == []


def test_the_two_omitted_gates_stay_omitted():
    """Pins the omissions the spike measured. Re-adding either of these to
    `_pre_commit_gates` without revisiting that measurement is the regression
    this guards: `deletion_block_gate` would refuse ~86% of this repo's
    commits, and `dirty_tree_gate` re-derives at ~40ms an answer
    `commit_paths` already gives at zero.
    """
    import inspect

    source = inspect.getsource(commit_v2._pre_commit_gates)
    body = source.split('"""')[-1]
    assert "deletion_block_gate(" not in body
    assert "dirty_tree_gate(" not in body
    assert not hasattr(commit_v2, "deletion_block_gate")
    assert not hasattr(commit_v2, "dirty_tree_gate")
