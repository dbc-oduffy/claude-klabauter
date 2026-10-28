"""spine_read reads a row's model tag, refuses an unknown one, and warns on unread row keys."""

from __future__ import annotations

import logging

import pytest

from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.spine_read import (
    KNOWN_ROW_KEYS,
    ROW_KEY_ALIASES,
    InvalidFieldTypeError,
    read_spine,
)

PLAN_REL = "docs/plans/2026-10-01-keys.md"


def _plan(tmp_path, extra: str, rid: str = "C1", agent_type: str = "coordinator:executor"):
    body = (
        f"- id: {rid}\n  title: row\n  change_kind: code-edit\n  surface: pkg/a.py\n"
        f"  writes:\n    - pkg/a.py\n  agent_type: {agent_type}\n{extra}"
    )
    plan = (
        "---\ndeliverable_id: dlv-keys-abc123\n---\n# Keys\n\n## Tasks\n\n```yaml plan-tasks\n"
        + body + "```\n"
    )
    (tmp_path / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    path = tmp_path / PLAN_REL
    path.write_text(plan, encoding="utf-8", newline="\n")
    return path


def test_model_alias_reaches_the_emitter_row(tmp_path):
    _plan(tmp_path, "  model: opus\n  effort: high\n")
    assert read_spine(tmp_path / PLAN_REL)[0].agent_model == "opus"


def test_model_alias_stages_opus_row_for_unpinned_type(tmp_path):
    _plan(tmp_path, "  model: opus\n  effort: high\n", agent_type="coordinator:workflow-maker")
    m = stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)
    assert m.rows[0].model == "opus"


def test_untagged_row_keeps_default(tmp_path):
    _plan(tmp_path, "")
    assert read_spine(tmp_path / PLAN_REL)[0].agent_model is None


def test_unknown_model_refused_naming_row(tmp_path):
    _plan(tmp_path, "  model: gpt5\n")
    with pytest.raises(InvalidFieldTypeError, match="C1"):
        read_spine(tmp_path / PLAN_REL)


def test_conflicting_model_tags_refused(tmp_path):
    _plan(tmp_path, "  model: opus\n  agent_model: sonnet\n")
    with pytest.raises(InvalidFieldTypeError, match="C1"):
        read_spine(tmp_path / PLAN_REL)


def test_unknown_key_stages_with_warning_naming_key_and_row(tmp_path, caplog):
    _plan(tmp_path, "  mystery_knob: 1\n")
    with caplog.at_level(logging.WARNING):
        rows = read_spine(tmp_path / PLAN_REL)
    assert [r.id for r in rows] == ["C1"]
    assert any("mystery_knob" in r.getMessage() and "'C1'" in r.getMessage() for r in caplog.records)


def test_known_key_does_not_warn(tmp_path, caplog):
    _plan(tmp_path, "  model: opus\n")
    with caplog.at_level(logging.WARNING):
        read_spine(tmp_path / PLAN_REL)
    assert not any("does not read" in r.getMessage() for r in caplog.records)


def test_disjoint_parallel_true_reaches_the_emitter_row_without_warning(tmp_path, caplog):
    _plan(tmp_path, "  disjoint_parallel: true\n")
    with caplog.at_level(logging.WARNING):
        rows = read_spine(tmp_path / PLAN_REL)
    assert rows[0].disjoint_parallel is True
    assert not any("does not read" in r.getMessage() for r in caplog.records)


def test_disjoint_parallel_absent_defaults_false(tmp_path):
    _plan(tmp_path, "")
    assert read_spine(tmp_path / PLAN_REL)[0].disjoint_parallel is False


def test_exports_for_readiness_consumers():
    assert ROW_KEY_ALIASES["model"] == "agent_model"
    assert {"model", "agent_model", "effort"} <= KNOWN_ROW_KEYS
