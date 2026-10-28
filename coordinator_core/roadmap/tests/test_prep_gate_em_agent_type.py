"""The plan gate refuses a row declaring `agent_type: em` and names `performer: em`."""

from __future__ import annotations

import json

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _CLEAN_FM, _write_plan

_ROW = """- id: C1
  title: merge row
  body: The EM merges.
  change_kind: code-edit
  surface: coordinator_core/b.py
  writes: [coordinator_core/b.py]
  {owner}
  queue_scope: project
  disposition: open
"""


def _verdict(tmp_path, owner: str, slug: str) -> str:
    (tmp_path / "coordinator_core").mkdir(exist_ok=True)
    plan = _write_plan(
        tmp_path, slug, frontmatter=_CLEAN_FM, spine=_ROW.format(owner=owner)
    )
    return json.dumps(pg.gate_plan(tmp_path, plan))


def test_agent_type_em_is_refused_naming_performer_em(tmp_path):
    out = _verdict(tmp_path, "agent_type: em", "2026-10-23-agent-em.md")
    assert "performer: em" in out
    assert "agent_type 'em'" in out


def test_unresolvable_agent_type_is_refused_with_emitter_message(tmp_path, monkeypatch):
    from coordinator_core.hooks import block_unenumerated_agent_type as bua

    monkeypatch.setattr(bua, "plugin_agent_model", lambda agent_type, **kw: (None, None))
    out = _verdict(tmp_path, "agent_type: coordinator:nosuchagent", "2026-10-27-unresolvable.md")
    assert "MalformedAgentOverrideError" in out
    assert "no entry in _AGENT_MODELS" in out


def test_agent_type_with_model_pin_still_passes(tmp_path):
    out = _verdict(tmp_path, "agent_type: coordinator:executor", "2026-10-27-resolvable.md")
    assert "MalformedAgentOverrideError" not in out
    assert "no entry in _AGENT_MODELS" not in out


def test_em_owned_agent_type_is_not_refused_as_unroutable(tmp_path, monkeypatch):
    from coordinator_core.hooks import block_unenumerated_agent_type as bua

    monkeypatch.setattr(bua, "plugin_agent_model", lambda agent_type, **kw: (None, None))
    out = _verdict(tmp_path, "agent_type: coordinator:em", "2026-10-27-em-owned.md")
    assert "MalformedAgentOverrideError" not in out
    assert "no entry in _AGENT_MODELS" not in out


def test_performer_em_is_not_refused_for_agent_type(tmp_path):
    out = _verdict(tmp_path, "performer: em", "2026-10-23-performer-em.md")
    assert "agent_type 'em'" not in out
