"""EXECUTOR_COMMIT: a row an executor is told to commit or deploy in is a prep defect."""

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _CLEAN_FM, _write_plan

_COMMIT_ROW = """- id: C2
  title: Write then commit
  change_kind: code-edit
  surface: src/a.py
  writes: [src/a.py]
  disposition: open
  body: |
    Edit the file, then run `git commit -m x` and `pnpm deploy:hosting`.
"""

_SPLIT_ROWS = """- id: C1
  title: Write
  change_kind: code-edit
  surface: src/a.py
  writes: [src/a.py]
  disposition: open
  body: |
    Edit the file.
- id: C2
  title: Commit then deploy
  change_kind: code-edit
  surface: src/a.py
  performer: em
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
  disposition: open
  body: |
    Run `git commit -m x` and `pnpm deploy:hosting`.
"""


def _gate(tmp_path, spine):
    (tmp_path / "src").mkdir(exist_ok=True)
    return pg.gate_plan(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))


def test_commit_row_is_a_defect_naming_the_split(tmp_path):
    result = _gate(tmp_path, _COMMIT_ROW)
    cls = result["classes"]["EXECUTOR_COMMIT"]
    assert result["verdict"] == pg.NOT_PREPPED
    assert cls["status"] == "DEFECT"
    assert cls["kind"] == "executor-commit-row"
    assert "C2" in cls["withheld"]
    assert "EM-owned commit boundary" in cls["detail"]
    assert "performer: em" in cls["detail"]


def test_split_into_em_row_passes(tmp_path):
    result = _gate(tmp_path, _SPLIT_ROWS)
    assert result["classes"]["EXECUTOR_COMMIT"]["status"] == "PASS"


def test_wont_do_row_passes(tmp_path):
    spine = _COMMIT_ROW.replace("disposition: open", "disposition: wont_do")
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_COMMIT"]["status"] == "PASS"


def test_prose_mention_passes(tmp_path):
    spine = _COMMIT_ROW.replace(
        "run `git commit -m x` and `pnpm deploy:hosting`", "commit and deploy afterwards"
    )
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_COMMIT"]["status"] == "PASS"


def test_message_omits_converter_when_only_failing_class(tmp_path):
    result = _gate(tmp_path, _COMMIT_ROW)
    failing = [k for k, v in result["classes"].items() if v["status"] != "PASS"]
    assert failing == ["EXECUTOR_COMMIT"]
    assert "mise-prep-upgrade" not in result["message"]
    assert "EM-owned commit boundary" in result["message"]


def test_class_order(tmp_path):
    result = _gate(tmp_path, _COMMIT_ROW)
    assert tuple(result["classes"]) == pg.CLASS_ORDER
    assert pg.CLASS_ORDER[-1] == "REGISTER"
    assert pg.CLASS_ORDER.index("EXECUTOR_COMMIT") == pg.CLASS_ORDER.index("CI_RETIRED") + 1
