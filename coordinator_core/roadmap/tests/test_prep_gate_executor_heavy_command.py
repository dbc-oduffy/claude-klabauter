"""EXECUTOR_HEAVY: an executor-routed row running a heavy command is a prep defect."""

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _CLEAN_FM, _write_plan

_ROW = """- id: C1
  title: Baseline
  change_kind: code-edit
  surface: src/a.py
  writes: [src/a.py]
  disposition: open
  body: |
    Measure with `pnpm vitest run` five times.
"""


def _gate(tmp_path, spine):
    (tmp_path / "src").mkdir(exist_ok=True)
    return pg.gate_plan(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))


def test_executor_row_running_vitest_is_refused_naming_test_runner(tmp_path):
    result = _gate(tmp_path, _ROW)
    cls = result["classes"]["EXECUTOR_HEAVY"]
    assert result["verdict"] == pg.NOT_PREPPED
    assert cls["status"] == "DEFECT"
    assert cls["kind"] == "executor-heavy-command-row"
    assert "C1" in cls["withheld"]
    assert "agent_type: coordinator:test-runner" in cls["detail"]
    assert "agent_type: coordinator:test-runner" in result["message"]


def test_same_row_declaring_test_runner_passes(tmp_path):
    spine = _ROW.replace("  disposition: open", "  agent_type: coordinator:test-runner\n  disposition: open")
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["status"] == "PASS"


def test_verification_string_is_read(tmp_path):
    spine = _ROW.replace("`pnpm vitest run`", "the suite").replace(
        "  disposition: open", "  verification: pnpm vitest run\n  disposition: open"
    )
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["status"] == "DEFECT"


def test_prose_and_light_commands_pass(tmp_path):
    spine = _ROW.replace("`pnpm vitest run`", "vitest, then `git status`")
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["status"] == "PASS"


def test_lone_noemit_tsc_is_the_guard_carve_out_and_passes(tmp_path):
    spine = _ROW.replace("`pnpm vitest run`", "`tsc --noEmit`")
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["status"] == "PASS"


def test_other_declared_agent_type_is_not_executor_routed(tmp_path):
    spine = _ROW.replace("  disposition: open", "  agent_type: coordinator:code-reviewer\n  disposition: open")
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["status"] == "PASS"


def test_wont_do_row_passes(tmp_path):
    spine = _ROW.replace("disposition: open", "disposition: wont_do")
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["status"] == "PASS"


def test_row_writing_only_an_immutable_plan_body_is_not_executor_routed(tmp_path):
    spine = _ROW.replace("surface: src/a.py", "surface: docs/plans/x.md").replace(
        "writes: [src/a.py]", "writes: [docs/plans/x.md]"
    )
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["status"] == "PASS"


def test_single_file_node_test_is_admitted(tmp_path):
    spine = _ROW.replace("`pnpm vitest run`", "`node --test test/x.test.js`")
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["status"] == "PASS"


def test_writing_row_running_heavy_command_gets_the_split_remedy(tmp_path):
    result = _gate(tmp_path, _ROW)
    detail = result["classes"]["EXECUTOR_HEAVY"]["detail"]
    assert "split it into" in detail
    assert "authoring row" in detail and "coordinator:test-runner" in detail
    assert "split it into" in result["message"]


def test_row_without_writes_keeps_the_declare_test_runner_remedy(tmp_path):
    spine = _ROW.replace("writes: [src/a.py]", "writes: []")
    detail = _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["detail"]
    assert "Fix: declare `agent_type: coordinator:test-runner` on the row." in detail
    assert "split it into" not in detail


def test_classification_root_is_the_plan_root_not_process_cwd(tmp_path, monkeypatch):
    seen = []
    from coordinator_core.bash_guards import _heavy_command_class as hcc

    real = hcc.classify
    monkeypatch.setattr(hcc, "classify", lambda c, t=None, cwd=None, *a: seen.append(cwd) or real(c, t, cwd, *a))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _gate(tmp_path, _ROW)
    assert seen and all(c == str(tmp_path) for c in seen)


def test_class_order(tmp_path):
    result = _gate(tmp_path, _ROW)
    assert tuple(result["classes"]) == pg.CLASS_ORDER
    assert pg.CLASS_ORDER.index("EXECUTOR_HEAVY") == pg.CLASS_ORDER.index("EXECUTOR_COMMIT") + 1
