
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import (
    PlanContext,
    _commit_agent_call,
    _plan_context_preamble,
    _preflight_agent_call,
    _row_prompt,
    derive_plan_context,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _row() -> WaveRow:
    return WaveRow(
        id="C1",
        title="do the thing",
        surface="a/one.py",
        writes=["a/one.py"],
        reads=[],
        depends_on=[],
    )


def _context(root):
    return PlanContext(
        title="A plan", goal=None, problem_excerpt=None, repo_root=root
    )


def test_the_executor_prompt_names_the_repo_root():
    prompt = _row_prompt(_row(), "docs/plans/p.md", _context("/home/user/claude-klabauter"))
    assert "Repo root: /home/user/claude-klabauter" in prompt
    assert "NOT necessarily the directory you start in" in prompt


def test_the_anchor_leads_the_preamble():
    preamble = _plan_context_preamble(_context("/repo"))
    assert preamble.splitlines()[0].startswith("Repo root: /repo")


def test_no_repo_root_declares_no_anchor_rather_than_inventing_one():
    prompt = _row_prompt(_row(), "docs/plans/p.md", _context(None))
    assert "Repo root:" not in prompt
    assert "Plan: A plan" in prompt


def test_the_commit_agent_gets_the_same_anchor():
    call = _commit_agent_call(
        ["docs/plans/p.md"], "Commit wave 1", 0, ["C1"], repo_root="/home/user/claude-klabauter"
    )
    assert "Repo root: /home/user/claude-klabauter" in call


def test_the_preflight_agent_gets_the_same_anchor():
    call = _preflight_agent_call(
        ["docs/plans/p.md"], "Preflight", repo_root="/home/user/claude-klabauter"
    )
    assert "Repo root: /home/user/claude-klabauter" in call


def test_commit_and_preflight_omit_the_anchor_when_none_was_resolved():
    assert "Repo root:" not in _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert "Repo root:" not in _preflight_agent_call(["a.py"], "Preflight")


def test_derive_plan_context_carries_the_root_through():
    context = derive_plan_context("# t\n", fallback_title="t", repo_root="/repo")
    assert context.repo_root == "/repo"
    assert derive_plan_context("# t\n", fallback_title="t").repo_root is None
