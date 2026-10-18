"""prep_gate reads em-performance through em_only_act.row_is_em_performed, not the literal performer key."""

from __future__ import annotations

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _write_plan

_FM = """census: []
prime_exit_criterion:
  statement: the build row B1 runs and validates
  derived_from: state/sizings/2026-10-18-em-only-act-fixture.yaml
"""

_B1 = """- id: B1
  title: Build the plugin
  change_kind: verification
  surface: scripts/build-plugin.ps1
  writes: []
  agent_type: coordinator:executor
  body: |
    Run `pwsh -NoProfile -File scripts/build-plugin.ps1 -EngineVersion 5.8`.
  queue_scope: project
  disposition: open
"""

_B1_WRITES = _B1.replace("writes: []", "writes: [coordinator_core/a.py]")

_EDGE = (
    "  depends_on_plan:\n"
    "    - {plan: docs/plans/2026-10-18-gone.md, chunk: C1, gate_kind: output-consumption-runtime}\n"
)


def test_forbidden_sole_act_row_skips_depends_on_plan_edges(tmp_path):
    row = {"id": "B1", "writes": [], "body": "`pwsh -File scripts/build-plugin.ps1`",
           "depends_on_plan": [{"plan": "docs/plans/2026-10-18-gone.md", "chunk": "C1"}]}
    assert pg._sibling_plan_edges(row, "B1", tmp_path, {}) == (frozenset(), [], [])


def test_writes_non_empty_row_still_reads_dangling_edge(tmp_path):
    row = {"id": "B1", "writes": ["a.py"], "body": "`pwsh -File scripts/build-plugin.ps1`",
           "depends_on_plan": [{"plan": "docs/plans/2026-10-18-gone.md", "chunk": "C1"}]}
    _exempt, _waiting, dangling = pg._sibling_plan_edges(row, "B1", tmp_path, {})
    assert dangling


def test_gate_raises_no_dangling_finding_for_derived_em_row(tmp_path):
    (tmp_path / ".git").mkdir()
    plan = _write_plan(tmp_path, "2026-10-18-em-edge.md", frontmatter=_FM, spine=_B1 + _EDGE)
    ext = pg.gate_plan(tmp_path, plan)["classes"]["EXTERNAL_DEPS"]
    assert ext["kind"] != "predecessor-plan-dangling"


def test_gate_still_flags_dangling_edge_on_writes_non_empty_row(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "coordinator_core").mkdir()
    plan = _write_plan(tmp_path, "2026-10-18-agent-edge.md", frontmatter=_FM, spine=_B1_WRITES + _EDGE)
    ext = pg.gate_plan(tmp_path, plan)["classes"]["EXTERNAL_DEPS"]
    assert ext["kind"] == "predecessor-plan-dangling"


def test_only_em_owned_row_being_derived_is_reported_in_exit_warnings(tmp_path):
    plan = _write_plan(tmp_path, "2026-10-18-em-only.md", frontmatter=_FM, spine=_B1)
    text = plan.read_text(encoding="utf-8")
    fm = {"prime_exit_criterion": {"statement": "the build row B1 runs", "derived_from": "x"}}
    warnings = pg._em_owned_exit_warnings(plan, text, fm)
    assert len(warnings) == 1
    assert "B1" in warnings[0] and "EM-owned" in warnings[0]


def test_writes_non_empty_row_is_not_em_owned_in_exit_warnings(tmp_path):
    (tmp_path / "coordinator_core").mkdir()
    plan = _write_plan(tmp_path, "2026-10-18-agent-only.md", frontmatter=_FM, spine=_B1_WRITES)
    text = plan.read_text(encoding="utf-8")
    fm = {"prime_exit_criterion": {"statement": "the build row B1 runs", "derived_from": "x"}}
    assert pg._em_owned_exit_warnings(plan, text, fm) == []
