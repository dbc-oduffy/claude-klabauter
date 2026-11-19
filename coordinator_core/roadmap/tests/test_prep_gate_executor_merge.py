"""EXECUTOR_MERGE: a row an executor is told to git merge in is a prep defect."""

from pathlib import Path

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _CLEAN_FM, _write_plan

_MERGE_ROW = """- id: C2
  title: Write then merge
  change_kind: code-edit
  surface: src/a.py
  writes: [src/a.py]
  disposition: open
  body: |
    Edit the file, then run `git merge --no-ff feature`.
"""

_SPLIT_ROWS = """- id: C1
  title: Merge the branch
  change_kind: code-edit
  surface: src/a.py
  performer: em
  disposition: open
  body: |
    Run `git merge --no-ff feature` via branch.merge_into_workstream.
- id: C2
  title: Write
  change_kind: code-edit
  surface: src/a.py
  writes: [src/a.py]
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
  disposition: open
  body: |
    Edit the file.
"""


def _gate(tmp_path, spine):
    (tmp_path / "src").mkdir(exist_ok=True)
    return pg.gate_plan(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))


def test_merge_row_is_a_defect_naming_the_split(tmp_path: Path) -> None:
    result = _gate(tmp_path, _MERGE_ROW)
    cls = result["classes"]["EXECUTOR_MERGE"]
    assert result["verdict"] == pg.NOT_PREPPED
    assert cls["status"] == "DEFECT"
    assert cls["kind"] == "executor-merge-row"
    assert "C2" in cls["withheld"]
    assert "branch.merge_into_workstream" in cls["detail"]
    assert "performer: em" in cls["detail"]


def test_split_into_em_row_passes(tmp_path: Path) -> None:
    result = _gate(tmp_path, _SPLIT_ROWS)
    assert result["classes"]["EXECUTOR_MERGE"]["status"] == "PASS"


def test_ff_only_and_prose_pass(tmp_path: Path) -> None:
    ff = _MERGE_ROW.replace("git merge --no-ff feature", "git merge --ff-only feature")
    assert _gate(tmp_path, ff)["classes"]["EXECUTOR_MERGE"]["status"] == "PASS"
    prose = _MERGE_ROW.replace("run `git merge --no-ff feature`", "merge the branch afterwards")
    assert _gate(tmp_path, prose)["classes"]["EXECUTOR_MERGE"]["status"] == "PASS"


def test_wont_do_row_passes(tmp_path: Path) -> None:
    spine = _MERGE_ROW.replace("disposition: open", "disposition: wont_do")
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_MERGE"]["status"] == "PASS"


def test_message_names_split_and_omits_converter(tmp_path: Path) -> None:
    result = _gate(tmp_path, _MERGE_ROW)
    failing = [k for k, v in result["classes"].items() if v["status"] != "PASS"]
    assert failing == ["EXECUTOR_MERGE"]
    assert "mise-prep-upgrade" not in result["message"]
    assert "branch.merge_into_workstream" in result["message"]


def test_class_order(tmp_path: Path) -> None:
    result = _gate(tmp_path, _MERGE_ROW)
    assert tuple(result["classes"]) == pg.CLASS_ORDER
    assert "EXECUTOR_MERGE" in pg.CLASS_ORDER
