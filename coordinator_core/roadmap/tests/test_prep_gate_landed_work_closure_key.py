"""An uncleared ``landed-work`` gate with no ``closure_key`` is warned about in the
EXTERNAL_DEPS result and never changes the verdict, status or withheld rows.
"""

from __future__ import annotations

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _CLEAN_FM, _write_plan

_ROW = """- id: C1
  title: gated row
  change_kind: code-edit
  surface: coordinator_core/a.py
  writes: [coordinator_core/a.py]
  queue_scope: project
  disposition: open
  external_gate:
    - owner_repo: example-retrieval-repo
      condition: peer lands
      requires: landed-work
{extra}"""


def _ext(tmp_path, extra: str, slug: str) -> dict:
    (tmp_path / "coordinator_core").mkdir(exist_ok=True)
    plan = _write_plan(tmp_path, slug, frontmatter=_CLEAN_FM, spine=_ROW.format(extra=extra))
    return pg.gate_plan(tmp_path, plan)["classes"]["EXTERNAL_DEPS"]


def test_unkeyed_landed_work_gate_warns_without_changing_verdict(tmp_path):
    ext = _ext(tmp_path, "", "2026-10-03-unkeyed.md")
    assert ext["status"] == "PASS"
    assert ext["withheld"] == ["C1"]
    assert len(ext["warnings"]) == 1
    assert ext["warnings"][0].startswith("C1: external_gate[0] requires landed-work")
    assert "closure_key" in ext["warnings"][0]


def test_keyed_gate_carries_no_warning(tmp_path):
    extra = "      closure_key: {kind: deliverable, id: d-1}\n"
    assert _ext(tmp_path, extra, "2026-10-03-keyed.md")["warnings"] == []


def test_cleared_gate_carries_no_warning(tmp_path):
    ext = _ext(tmp_path, "      cleared: true\n", "2026-10-03-cleared.md")
    assert ext["warnings"] == []
    assert ext["withheld"] == []
