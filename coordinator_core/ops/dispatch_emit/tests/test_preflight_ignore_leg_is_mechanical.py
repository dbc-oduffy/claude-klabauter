"""
P161-C4: the preflight prompt's model-judged ignore-rule leg is redundant for
a CONCRETE path once the emit-time `_gitignored_paths` filter has already
dropped every ignored one from the pathspec. Asking the dispatched
commit-agent to re-judge ignore rules on paths that never reached it is a
model-judged check standing in for a mechanical one that already ran.

Spec: docs/plans/2026-09-22-wsc-and-wave-commit-residual-defects.md (C4)
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.emit import _preflight_agent_call, compose_script
from .test_emit import _gitignore_repo, _preflight_body, _wave_row


def test_undegraded_preflight_prompt_drops_the_concrete_path_ignore_rule_leg():
    prompt_block = _preflight_agent_call(
        ["a.py"],
        "Preflight: commit claimability",
        gitignore_filter_degraded=False,
    )
    assert "an ignore rule" not in prompt_block
    assert "already checked mechanically at emit" in prompt_block


def test_degraded_preflight_prompt_keeps_the_concrete_path_ignore_rule_leg():
    prompt_block = _preflight_agent_call(
        ["a.py"],
        "Preflight: commit claimability",
        gitignore_filter_degraded=True,
    )
    assert "an ignore rule" in prompt_block
    assert "already checked mechanically at emit" not in prompt_block


def test_prefix_clause_is_byte_identical_regardless_of_degraded():
    def _prefix_clause(prompt_block: str) -> str:
        start = prompt_block.index("Entries in [")
        end = prompt_block.index("DIRECTORY-SHAPED WRITE")
        return prompt_block[start:end]

    undegraded = _preflight_agent_call(
        ["a.py"],
        "Preflight: commit claimability",
        prefixes=["state/handoffs/"],
        gitignore_filter_degraded=False,
    )
    degraded = _preflight_agent_call(
        ["a.py"],
        "Preflight: commit claimability",
        prefixes=["state/handoffs/"],
        gitignore_filter_degraded=True,
    )
    assert _prefix_clause(undegraded) == _prefix_clause(degraded)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_undegraded_emit_drops_the_ignore_rule_leg_and_the_pathspec_is_post_filter(
    tmp_path,
):
    """End-to-end: the emitter's own `_gitignored_paths` run (undegraded, a
    real git repo) both drops the ignored concrete path from the pathspec
    that reaches the preflight prompt AND removes the model-judged
    ignore-rule refusal for it -- the two facts are one and the same
    mechanical guarantee, not two independent claims."""
    repo = _gitignore_repo(tmp_path)
    waves = [[_wave_row("C1", ["registry/registry.db", "a.py"])]]
    script = compose_script(waves, name="wf", description="undegraded", repo_root=repo)
    preflight_body = _preflight_body(script)
    assert "registry/registry.db" not in preflight_body
    assert "a.py" in preflight_body
    assert "an ignore rule" not in preflight_body
    assert "already checked mechanically at emit" in preflight_body


def test_a_degraded_emit_keeps_the_ignore_rule_leg_in_the_composed_script(
    tmp_path, monkeypatch
):
    from coordinator_core.git.run import GitResult

    def _fake_run_git(*args, **kwargs):
        return GitResult(
            returncode=127, timed_out=False, stdout="", stderr="", stdout_bytes=b""
        )

    monkeypatch.setattr("coordinator_core.git.run.run_git", _fake_run_git)

    waves = [[_wave_row("C1", ["registry/registry.db"])]]
    script = compose_script(waves, name="wf", description="degraded", repo_root=tmp_path)
    preflight_body = _preflight_body(script)
    assert "an ignore rule" in preflight_body
    assert "already checked mechanically at emit" not in preflight_body
