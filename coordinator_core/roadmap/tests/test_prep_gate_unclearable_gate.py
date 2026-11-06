"""An external_gate with no clearance path is a defect; a plan where no schedulable row can
ever clear is NOT-PREPPED; a row merely waiting on something satisfiable still passes and withholds.
"""

from __future__ import annotations

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _CLEAN_FM, _write_plan

_GATED = """- id: {id}
  title: gated row
  change_kind: code-edit
  surface: coordinator_core/a.py
  writes: [coordinator_core/a.py]
  queue_scope: project
  disposition: open
  external_gate:
    - owner_repo: example-retrieval-repo
      condition: peer lands
      requires: {requires}
{extra}"""

_PLAIN = """- id: {id}
  title: plain row
  change_kind: code-edit
  surface: coordinator_core/b.py
  writes: [coordinator_core/b.py]
  queue_scope: project
  disposition: open
"""

_DONE = """- id: {id}
  title: finished row
  change_kind: code-edit
  surface: coordinator_core/c.py
  writes: [coordinator_core/c.py]
  queue_scope: project
  disposition: coded
"""

_KEY = "      closure_key: {kind: deliverable, id: d-1}\n"


def _ext(tmp_path, spine: str, slug: str) -> dict:
    (tmp_path / "coordinator_core").mkdir(exist_ok=True)
    plan = _write_plan(tmp_path, slug, frontmatter=_CLEAN_FM, spine=spine)
    return pg.gate_plan(tmp_path, plan)["classes"]["EXTERNAL_DEPS"]


def test_unkeyed_landed_gate_is_a_defect_even_beside_a_free_row(tmp_path):
    spine = _GATED.format(id="C1", requires="landed-work", extra="") + _PLAIN.format(id="C2")
    ext = _ext(tmp_path, spine, "2026-10-03-unkeyed-partial.md")
    assert ext["status"] == "DEFECT"
    assert ext["kind"] == "external-gate-unclearable"
    assert "C1" in ext["detail"] and "closure_key" in ext["detail"]


def test_partial_withhold_with_keyed_gate_still_passes(tmp_path):
    spine = _GATED.format(id="C1", requires="landed-work", extra=_KEY) + _PLAIN.format(id="C2")
    ext = _ext(tmp_path, spine, "2026-10-03-keyed-partial.md")
    assert ext["status"] == "PASS"
    assert ext["withheld"] == ["C1"]


def test_keyed_gate_on_every_row_still_passes_and_withholds(tmp_path):
    spine = _GATED.format(id="C1", requires="landed-work", extra=_KEY)
    ext = _ext(tmp_path, spine, "2026-10-03-keyed-all.md")
    assert ext["status"] == "PASS"
    assert ext["withheld"] == ["C1"]


def test_commit_gated_sole_row_names_the_owner_repo_route(tmp_path):
    spine = _GATED.format(id="C1", requires="commit-in-owner-repo", extra="")
    ext = _ext(tmp_path, spine, "2026-10-03-commit-all.md")
    assert ext["status"] == "DEFECT"
    assert ext["kind"] == "no-row-can-clear"
    assert "cross-repo commit" in ext["detail"]
    assert "engine-ask" in ext["detail"] and "project-rag" in ext["detail"]


def test_commit_gated_row_beside_a_free_row_passes(tmp_path):
    spine = _GATED.format(id="C1", requires="commit-in-owner-repo", extra="") + _PLAIN.format(id="C2")
    ext = _ext(tmp_path, spine, "2026-10-03-commit-partial.md")
    assert ext["status"] == "PASS"
    assert ext["withheld"] == ["C1"]


def test_commit_gated_row_beside_only_a_finished_row_is_still_a_defect(tmp_path):
    spine = _GATED.format(id="C1", requires="commit-in-owner-repo", extra="") + _DONE.format(id="C2")
    ext = _ext(tmp_path, spine, "2026-10-03-commit-done.md")
    assert ext["status"] == "DEFECT"
    assert ext["kind"] == "no-row-can-clear"


def test_commit_gated_row_beside_a_keyed_gated_row_passes(tmp_path):
    spine = _GATED.format(id="C1", requires="commit-in-owner-repo", extra="") + _GATED.format(
        id="C2", requires="landed-work", extra=_KEY
    )
    ext = _ext(tmp_path, spine, "2026-10-03-commit-keyed.md")
    assert ext["status"] == "PASS"
    assert sorted(ext["withheld"]) == ["C1", "C2"]
