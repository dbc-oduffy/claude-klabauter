"""dispatch.emit composes a pinned type's row at the pin when the row overrides agent_model."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.emit import emit_script, effective_agent_model
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW

FIXTURE = Path(__file__).parent / "fixtures" / "fc0a73_pinned_override_plan.md"


def _row(agent_model):
    return WaveRow(
        id="A1", title="t", surface="s", writes=["some/A1.py"], reads=[], depends_on=[],
        agent_type="coordinator:executor", agent_model=agent_model,
    )


def test_fc0a73_plan_emits_through_the_real_entry_at_the_pin(tmp_path):
    script = emit_script(FIXTURE, repo_root=tmp_path, **REVIEW_KW)
    assert "agent_model haiku dropped: coordinator:executor is pinned */ model: 'sonnet'" in script


def test_override_differing_from_the_pin_is_dropped_and_reported():
    assert effective_agent_model("coordinator:executor", "haiku") == (None, "haiku")


def test_override_equal_to_the_pin_is_unchanged_and_not_reported():
    assert effective_agent_model("coordinator:executor", "sonnet") == ("sonnet", None)
    call = emit._agent_call_over("'p'", _row("sonnet"), None, label_id="A1")
    assert "model: 'sonnet'" in call and "dropped" not in call


def test_unpinned_type_keeps_its_override():
    assert effective_agent_model("acme:custom-agent", "haiku") == ("haiku", None)


def test_no_override_passes_through():
    assert effective_agent_model("coordinator:executor", None) == (None, None)


def test_malformed_override_on_a_pinned_type_still_refuses():
    with pytest.raises(emit.MalformedAgentOverrideError):
        effective_agent_model("coordinator:executor", "bad model!")


def test_resolve_agent_model_still_refuses_a_differing_override():
    with pytest.raises(emit.MalformedAgentOverrideError):
        emit.resolve_agent_model("coordinator:executor", "haiku")


def test_agent_call_over_carries_the_pin_for_a_differing_override():
    call = emit._agent_call_over("'p'", _row("haiku"), None, label_id="A1")
    assert "model: 'sonnet'" in call
    assert "model: 'haiku'" not in call


def test_two_pinned_rows_one_overriding_emit_without_raising(tmp_path):
    plan = tmp_path / "plan.md"
    rows = "".join(
        f"- id: A{i}\n  title: T{i}\n  change_kind: code-edit\n  surface: s/f{i}.py\n"
        f"  writes: [s/f{i}.py]\n  queue_scope: project\n  disposition: open\n"
        f"  agent_type: coordinator:executor\n{extra}  body: |\n    b\n"
        for i, extra in ((1, ""), (2, "  agent_model: haiku\n"))
    )
    plan.write_text("# p\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "```\n", encoding="utf-8")
    script = emit_script(plan, repo_root=tmp_path, **REVIEW_KW)
    assert script.count("agent_model haiku dropped:") == 1
