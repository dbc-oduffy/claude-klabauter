"""spine_read reads a row's `stage_role` onto the emitter row and WaveRow, refusing bad shapes."""

from __future__ import annotations

import logging

import pytest

from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.ops.dispatch_emit.spine_read import (
    KNOWN_ROW_KEYS,
    InvalidFieldTypeError,
    read_spine,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow, build_waves

PLAN_REL = "docs/plans/2026-11-01-role.md"


def _plan(tmp_path, extra: str, agent_type: str = "coordinator:executor"):
    agent = f"  agent_type: {agent_type}\n" if agent_type else ""
    body = (
        "- id: C1\n  title: row\n  change_kind: code-edit\n  surface: pkg/a.py\n"
        f"  writes:\n    - pkg/a.py\n{agent}{extra}"
    )
    plan = (
        "---\ndeliverable_id: dlv-role-abc123\n---\n# Role\n\n## Tasks\n\n```yaml plan-tasks\n"
        + body + "```\n"
    )
    (tmp_path / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    path = tmp_path / PLAN_REL
    path.write_text(plan, encoding="utf-8", newline="\n")
    return path


@pytest.mark.parametrize("role", ["scout", "research", "review"])
def test_role_reaches_rows_unchanged_otherwise(tmp_path, role, caplog):
    _plan(tmp_path, f"  stage_role: {role}\n")
    with caplog.at_level(logging.WARNING):
        (row,) = read_spine(tmp_path / PLAN_REL)
    assert row.stage_role == role
    assert row.agent_type == "coordinator:executor"
    assert "family:" not in row.body
    assert not any("stage_role" in r.getMessage() for r in caplog.records)
    (wave,) = build_waves([row])
    assert wave[0].stage_role == role


def test_absent_role_is_none_and_wave_row_default_holds(tmp_path):
    _plan(tmp_path, "")
    assert read_spine(tmp_path / PLAN_REL)[0].stage_role is None
    wr = WaveRow(id="C1", title="t", surface="s", writes=[], reads=[], depends_on=[])
    assert wr.stage_role is None


def test_unknown_role_refused_naming_row(tmp_path):
    _plan(tmp_path, "  stage_role: size\n")
    with pytest.raises(InvalidFieldTypeError, match="C1.*scout\\|research\\|review"):
        read_spine(tmp_path / PLAN_REL)


def test_role_beside_family_refused(tmp_path):
    _plan(tmp_path, "  stage_role: scout\n  family: grok\n", agent_type="")
    with pytest.raises(InvalidFieldTypeError, match="keep one"):
        read_spine(tmp_path / PLAN_REL)


def test_role_is_a_known_key_and_matches_stage_family_roles():
    assert "stage_role" in KNOWN_ROW_KEYS
    from coordinator_core.ops.dispatch_emit import spine_read

    assert spine_read._STAGE_ROLES == stage_family.ROLES
