"""Every review and reverify agent() call carries exactly one sidecar clause."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import agent_sidecar
from coordinator_core.ops.dispatch_emit.tests.test_leg_register_ratchet import _review_scripts, _reverify_scripts
from coordinator_core.ops.review_mint.compose import with_sidecar_clause
from coordinator_core.ops.dispatch_emit import leg_register as lr


def _assert_one_clause_per_call(script: str) -> None:
    calls = len(lr.emitted_legs(script))
    assert calls > 0
    assert script.count(agent_sidecar.SIDECAR_DIR_MARKER_PREFIX) == calls


def test_review_scripts_carry_one_clause_per_call():
    for script in _review_scripts()["review"]:
        _assert_one_clause_per_call(script)
        assert ".coordinator-local/workflow-sidecars/review/" in script


def test_reverify_scripts_carry_one_clause_per_call(tmp_path):
    for script in _reverify_scripts(tmp_path):
        _assert_one_clause_per_call(script)
        assert ".coordinator-local/workflow-sidecars/re" in script


def test_clause_names_heredoc_fallback():
    script = _review_scripts()["review"][0]
    assert "Bash heredoc" in script and "never skip it" in script


def test_with_sidecar_clause_refuses_a_call_without_opts():
    with pytest.raises(ValueError):
        with_sidecar_clause("agent('x')", "review", "plan", "prep-x")


def test_with_sidecar_clause_lands_before_opts():
    call = "agent('body', { label: 'review:x', phase: 'p' })"
    out = with_sidecar_clause(call, "review", "plan", "prep-x")
    assert out.endswith(", { label: 'review:x', phase: 'p' })")
    assert "sidecar_dir: .coordinator-local/workflow-sidecars/review/plan/prep-x/" in out
