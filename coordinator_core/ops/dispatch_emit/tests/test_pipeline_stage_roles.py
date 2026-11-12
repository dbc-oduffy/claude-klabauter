"""Tests for the research stage-to-role table and per-run family resolver."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import stage_family, stage_family_binding
from coordinator_core.ops.dispatch_emit.pipeline_stage_roles import ROLE_BY_STAGE, stage_families


def _binding(**roles):
    return stage_family_binding.Binding(runner=stage_family.RUNNER_AGENT, fallback="claude", roles=roles)


def _segment(pipeline, *stage_ids):
    manifest = SimpleNamespace(pipeline=pipeline, stages=tuple(SimpleNamespace(id=i) for i in stage_ids))
    return (manifest, None, None)


def test_table_roles_are_all_binding_roles():
    assert set(ROLE_BY_STAGE.values()) <= set(stage_family.ROLES)
    assert "review" not in ROLE_BY_STAGE.values()


def test_compose_family_role_agrees_with_the_table():
    from coordinator_core.ops.dispatch_emit.pipeline_compose import _family_role

    assert {key: _family_role(key[1]) for key in ROLE_BY_STAGE} == ROLE_BY_STAGE


def test_mapped_stages_resolve_to_bound_family():
    segs = [_segment("scouts", "scout"), _segment("web", "scout", "specialists")]
    got = stage_families(segs, _binding(scout="grok", research="agy"))
    assert got == {("scouts", "scout"): "grok", ("web", "scout"): "grok", ("web", "specialists"): "agy"}


def test_claude_bound_role_yields_no_entry():
    segs = [_segment("repo", "scout-haiku", "specialists")]
    got = stage_families(segs, _binding(scout="claude", research="agy"))
    assert got == {("repo", "specialists"): "agy"}


def test_role_absent_from_binding_yields_no_entry():
    assert stage_families([_segment("repo", "scout-sonnet")], _binding(research="agy")) == {}


def test_unmapped_stage_yields_no_entry():
    segs = [_segment("repo", "synthesis"), _segment("other", "scout")]
    assert stage_families(segs, _binding(scout="grok", research="agy")) == {}


def test_unknown_family_raises():
    with pytest.raises(ValueError):
        stage_families([_segment("scouts", "scout")], _binding(scout="gemini"))
