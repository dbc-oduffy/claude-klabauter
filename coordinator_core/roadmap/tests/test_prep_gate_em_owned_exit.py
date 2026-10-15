"""PRIME_EXIT warns, without changing the verdict, when every row delivering the criterion is EM-owned."""

from __future__ import annotations

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _write_plan

_FM = """census: []
prime_exit_criterion:
  statement: the merge row C2 lands and validates
  derived_from: state/sizings/2026-10-15-em-owned-fixture.yaml
"""

_CODE_ROW = """- id: C1
  title: code row
  body: Edit the module.
  change_kind: code-edit
  surface: coordinator_core/a.py
  writes: [coordinator_core/a.py]
  queue_scope: project
  disposition: open
"""

_EM_ROW = """- id: C2
  title: merge row
  body: The EM merges.
  change_kind: code-edit
  surface: coordinator_core/b.py
  writes: [coordinator_core/b.py]
  performer: em
  queue_scope: project
  disposition: open
"""


def _prime(tmp_path, spine: str, slug: str, fm: str = _FM) -> dict:
    (tmp_path / "coordinator_core").mkdir(exist_ok=True)
    plan = _write_plan(tmp_path, slug, frontmatter=fm, spine=spine)
    return pg.gate_plan(tmp_path, plan)["classes"]["PRIME_EXIT"]


def test_criterion_resting_only_on_em_owned_rows_warns_and_keeps_verdict(tmp_path):
    prime = _prime(tmp_path, _CODE_ROW + _EM_ROW, "2026-10-15-em-only.md")
    assert prime["status"] == "PASS"
    assert len(prime["warnings"]) == 1
    assert "C2" in prime["warnings"][0] and "EM-owned" in prime["warnings"][0]
    assert "C1" not in prime["warnings"][0]


def test_criterion_citing_a_dispatchable_row_carries_no_warning(tmp_path):
    fm = _FM.replace("row C2", "rows C1 and C2")
    assert _prime(tmp_path, _CODE_ROW + _EM_ROW, "2026-10-15-mixed.md", fm)["warnings"] == []


def test_plan_with_no_em_owned_row_carries_no_warning(tmp_path):
    assert _prime(tmp_path, _CODE_ROW, "2026-10-15-none.md")["warnings"] == []
