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
    Run `pnpm vitest run` five times.
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
    assert "C1-V" in cls["repair"] and "`performer: em`" in cls["repair"]


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
    assert "keeps its writes" in detail and "`<id>-V` verify row" in detail
    assert "`<id>-V` verify row" in result["message"]


def test_row_without_writes_keeps_the_declare_test_runner_remedy(tmp_path):
    spine = _ROW.replace("writes: [src/a.py]", "writes: []")
    detail = _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["detail"]
    assert "Fix: declare `agent_type: coordinator:test-runner` on the row." in detail
    assert "`<id>-V` verify row" not in detail


def test_no_writes_hit_is_a_mechanical_repair_naming_the_row(tmp_path):
    cls = _gate(tmp_path, _ROW.replace("writes: [src/a.py]", "writes: []"))["classes"]["EXECUTOR_HEAVY"]
    assert cls["mechanical"] is True
    assert "row C1: declare" in cls["repair"] and "`performer: em`" in cls["repair"]


def test_writes_bearing_hit_is_a_mechanical_split_repair(tmp_path):
    cls = _gate(tmp_path, _ROW)["classes"]["EXECUTOR_HEAVY"]
    assert cls["mechanical"] is True
    assert "C1" in cls["repair"] and "C1-V" in cls["repair"]


def test_verify_row_id_skips_ids_already_in_the_spine(tmp_path):
    taken = _ROW.replace("C1", "C1-V").replace("writes: [src/a.py]", "writes: []").replace("vitest run", "echo")
    cls = _gate(tmp_path, _ROW + taken)["classes"]["EXECUTOR_HEAVY"]
    assert "C1-V2" in cls["repair"] and "add row C1-V " not in cls["repair"]


def test_mixed_hits_are_a_mechanical_repair_naming_both_rows(tmp_path):
    second = _ROW.replace("C1", "C2").replace("writes: [src/a.py]", "writes: []")
    cls = _gate(tmp_path, _ROW + second)["classes"]["EXECUTOR_HEAVY"]
    assert set(cls["withheld"]) == {"C1", "C2"}
    assert cls["mechanical"] is True
    assert "row C1:" in cls["repair"] and "row C2:" in cls["repair"] and "C1-V" in cls["repair"]


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


def _cls(tmp_path, body_text):
    spine = _ROW.replace("Run `pnpm vitest run` five times.", body_text)
    return _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["status"]


def test_quoted_command_mid_sentence_or_in_text_fence_is_not_an_act(tmp_path):
    assert _cls(tmp_path, "The old step was `pnpm vitest run`; do not repeat it.") == "PASS"
    assert _cls(tmp_path, "Do not run `pnpm vitest run` here.") == "PASS"
    assert _cls(tmp_path, "Measure with `pnpm vitest run`.") == "PASS"
    assert _cls(tmp_path, "```yaml\n    note: pnpm vitest run\n    ```") == "PASS"


def test_run_step_bare_span_and_shell_fence_are_still_refused(tmp_path):
    assert _cls(tmp_path, "- Run `pnpm vitest run`.") == "DEFECT"
    assert _cls(tmp_path, "`pnpm vitest run`") == "DEFECT"
    assert _cls(tmp_path, "Step 2: execute `pnpm vitest run`") == "DEFECT"
    assert _cls(tmp_path, "```sh\n    pnpm vitest run\n    ```") == "DEFECT"
    assert _cls(tmp_path, "```\n    pnpm vitest run\n    ```") == "DEFECT"


def test_class_order(tmp_path):
    result = _gate(tmp_path, _ROW)
    assert tuple(result["classes"]) == pg.CLASS_ORDER
    assert pg.CLASS_ORDER.index("EXECUTOR_HEAVY") == pg.CLASS_ORDER.index("EXECUTOR_COMMIT") + 1
