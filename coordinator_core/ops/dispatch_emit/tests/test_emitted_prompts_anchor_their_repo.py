
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import (
    PlanContext,
    _plan_context_preamble,
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
    assert "run `cd /home/user/claude-klabauter` as its own standalone Bash call" in prompt


def test_the_anchor_leads_the_preamble():
    preamble = _plan_context_preamble(_context("/repo"))
    assert preamble.splitlines()[0].startswith("Repo root: /repo")


def test_no_repo_root_declares_no_anchor_rather_than_inventing_one():
    prompt = _row_prompt(_row(), "docs/plans/p.md", _context(None))
    assert "Repo root:" not in prompt
    assert "Plan: A plan" in prompt


def test_derive_plan_context_carries_the_root_through():
    context = derive_plan_context("# t\n", fallback_title="t", repo_root="/repo")
    assert context.repo_root == "/repo"
    assert derive_plan_context("# t\n", fallback_title="t").repo_root is None


def test_an_off_path_claude_is_named_in_the_brief(monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import emit

    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    context = derive_plan_context("# t\n", fallback_title="t", repo_root="/repo")
    assert context.claude_bin_dir == tmp_path.as_posix()
    prompt = _row_prompt(_row(), "docs/plans/p.md", context)
    assert f"claude CLI: at {tmp_path.as_posix()}" in prompt
    assert "does not mean it is missing" in prompt
    assert emit._off_path_claude_dir() == tmp_path.as_posix()


def test_a_claude_on_a_system_path_or_absent_adds_no_line(monkeypatch):
    from coordinator_core.ops.dispatch_emit import emit

    monkeypatch.setattr(emit.shutil, "which", lambda *a, **k: "/usr/local/bin/claude")
    assert emit._off_path_claude_dir() is None
    monkeypatch.setattr(emit.shutil, "which", lambda *a, **k: None)
    assert emit._off_path_claude_dir() is None
    prompt = _row_prompt(_row(), "docs/plans/p.md", _context("/repo"))
    assert "claude CLI:" not in prompt
