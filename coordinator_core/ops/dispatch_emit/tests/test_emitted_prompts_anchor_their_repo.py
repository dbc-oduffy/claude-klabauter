
from __future__ import annotations

import sys

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


_ROOT_EXPR = "\x01_repoRoot\x01"


def test_the_executor_prompt_anchors_to_the_runtime_root_never_a_literal():
    prompt = _row_prompt(_row(), "docs/plans/p.md", _context("/home/user/claude-klabauter"))
    assert f"Repo root: {_ROOT_EXPR}" in prompt
    assert "NOT necessarily the directory you start in" in prompt
    assert f"run `cd {_ROOT_EXPR}` as its own standalone Bash call" in prompt
    assert "/home/user" not in prompt


def test_the_anchor_leads_the_preamble():
    preamble = _plan_context_preamble(_context("/repo"))
    assert preamble.splitlines()[0].startswith(f"Repo root: {_ROOT_EXPR}")


def test_no_repo_root_declares_no_anchor_rather_than_inventing_one():
    prompt = _row_prompt(_row(), "docs/plans/p.md", _context(None))
    assert "Repo root:" not in prompt
    assert "Plan: A plan" in prompt


def test_derive_plan_context_carries_the_root_through():
    context = derive_plan_context("# t\n", fallback_title="t", repo_root="/repo")
    assert context.repo_root == "/repo"
    assert derive_plan_context("# t\n", fallback_title="t").repo_root is None


def test_an_off_path_claude_is_flagged_without_naming_its_directory(monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import emit

    binary = tmp_path / ("claude.exe" if sys.platform == "win32" else "claude")
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    context = derive_plan_context("# t\n", fallback_title="t", repo_root="/repo")
    assert context.claude_off_path is True
    prompt = _row_prompt(_row(), "docs/plans/p.md", context)
    assert "claude CLI:" in prompt
    assert "not missing" in prompt
    assert tmp_path.as_posix() not in prompt
    assert emit._claude_is_off_default_path() is True


def test_a_claude_on_a_system_path_or_absent_adds_no_line(monkeypatch):
    from coordinator_core.ops.dispatch_emit import emit

    monkeypatch.setattr(emit.shutil, "which", lambda *a, **k: "/usr/local/bin/claude")
    assert emit._claude_is_off_default_path() is False
    monkeypatch.setattr(emit.shutil, "which", lambda *a, **k: None)
    assert emit._claude_is_off_default_path() is False
    prompt = _row_prompt(_row(), "docs/plans/p.md", _context("/repo"))
    assert "claude CLI:" not in prompt
