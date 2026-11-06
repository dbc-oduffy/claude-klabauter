"""An uncleared ``landed-work`` gate with no ``closure_key`` has no clearance path: EXTERNAL_DEPS
refuses it as ``external-gate-unclearable``, and a keyed or cleared gate stays clean.
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


def test_unkeyed_landed_work_gate_is_a_defect_naming_the_repair(tmp_path):
    ext = _ext(tmp_path, "", "2026-10-03-unkeyed.md")
    assert ext["status"] == "DEFECT"
    assert ext["kind"] == "external-gate-unclearable"
    assert "C1: external_gate[0] requires landed-work" in ext["detail"]
    assert "closure_key: {kind: deliverable|memo-thread, id}" in ext["detail"]


def test_keyed_gate_passes_and_withholds(tmp_path):
    extra = "      closure_key: {kind: deliverable, id: d-1}\n"
    ext = _ext(tmp_path, extra, "2026-10-03-keyed.md")
    assert ext["status"] == "PASS"
    assert ext["withheld"] == ["C1"]


def test_cleared_gate_passes_and_withholds_nothing(tmp_path):
    ext = _ext(tmp_path, "      cleared: true\n", "2026-10-03-cleared.md")
    assert ext["status"] == "PASS"
    assert ext["withheld"] == []
