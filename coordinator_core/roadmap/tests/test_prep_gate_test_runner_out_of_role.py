"""TEST_RUNNER_OUT_OF_ROLE: a test-runner-declared timing or broad-suite row is a prep defect."""

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _CLEAN_FM, _write_plan

_ROW = """- id: C0
  title: Timing baseline
  change_kind: verification timing measurement
  surface: src/a.py
  writes: []
  agent_type: coordinator:test-runner
  disposition: open
  body: |
    Alone runs, then a load generator.
"""

_VERIFY_ROW = """- id: C1
  title: Verify
  change_kind: verification
  surface: src/a.py
  writes: []
  agent_type: coordinator:test-runner
  disposition: open
  body: |
    Run `python -m pytest src/test_a.py`.
"""


def _gate(tmp_path, spine):
    (tmp_path / "src").mkdir(exist_ok=True)
    return pg.gate_plan(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))


def _cls(tmp_path, spine):
    return _gate(tmp_path, spine)["classes"]["TEST_RUNNER_OUT_OF_ROLE"]


def test_timing_measurement_row_is_refused_naming_performer_em(tmp_path):
    result = _gate(tmp_path, _ROW)
    cls = result["classes"]["TEST_RUNNER_OUT_OF_ROLE"]
    assert result["verdict"] == pg.NOT_PREPPED
    assert cls["status"] == "DEFECT"
    assert cls["kind"] == "test-runner-out-of-role-row"
    assert "C0" in cls["withheld"]
    assert "performer: em" in cls["detail"]
    assert "coordinator_core.benchmarks.measure" in cls["detail"]
    assert "performer: em" in result["message"]


def test_broad_suite_row_is_refused(tmp_path):
    spine = _ROW.replace("verification timing measurement", "verification").replace(
        "Alone runs, then a load generator.", "Run `pnpm vitest run`."
    )
    cls = _cls(tmp_path, spine)
    assert cls["status"] == "DEFECT"
    assert "C0" in cls["withheld"]


def test_verification_string_full_suite_is_read(tmp_path):
    spine = _VERIFY_ROW.replace("`python -m pytest src/test_a.py`", "the suite").replace(
        "  disposition: open", "  verification: pnpm vitest run\n  disposition: open"
    )
    assert _cls(tmp_path, spine)["status"] == "DEFECT"


def test_targeted_verify_row_passes(tmp_path):
    assert _cls(tmp_path, _VERIFY_ROW)["status"] == "PASS"


def test_targeted_vitest_file_with_worker_cap_passes(tmp_path):
    spine = _VERIFY_ROW.replace(
        "`python -m pytest src/test_a.py`", "`pnpm vitest run src/a.test.ts --maxWorkers=2`"
    )
    assert _cls(tmp_path, spine)["status"] == "PASS"


def test_pnpm_test_with_path_passes_and_bare_pnpm_test_is_refused(tmp_path):
    scoped = _VERIFY_ROW.replace("`python -m pytest src/test_a.py`", "`pnpm test src/a.test.ts`")
    assert _cls(tmp_path, scoped)["status"] == "PASS"
    bare = _VERIFY_ROW.replace("`python -m pytest src/test_a.py`", "`pnpm test`")
    assert _cls(tmp_path, bare)["status"] == "DEFECT"
    flags_only = _VERIFY_ROW.replace("`python -m pytest src/test_a.py`", "`pnpm test --coverage`")
    assert _cls(tmp_path, flags_only)["status"] == "DEFECT"


def test_pnpm_test_with_path_stays_test_tier_for_admission():
    from coordinator_core.bash_guards._heavy_command_class import HeavyClass, classify

    cls = classify("pnpm test src/a.test.ts", None, ".", 1 << 30)
    assert cls is not None and cls.heavy_class is HeavyClass.TEST_TIER


def test_timing_row_not_declaring_test_runner_passes(tmp_path):
    spine = _ROW.replace("  agent_type: coordinator:test-runner\n", "")
    assert _cls(tmp_path, spine)["status"] == "PASS"


def test_timing_row_declaring_performer_em_passes(tmp_path):
    spine = _ROW.replace("  disposition: open", "  performer: em\n  disposition: open")
    assert _cls(tmp_path, spine)["status"] == "PASS"


def test_wont_do_row_passes(tmp_path):
    spine = _ROW.replace("disposition: open", "disposition: wont_do")
    assert _cls(tmp_path, spine)["status"] == "PASS"


def _body_cls(tmp_path, text):
    spine = _VERIFY_ROW.replace("Run `python -m pytest src/test_a.py`.", text)
    return _cls(tmp_path, spine)["status"]


def test_quoted_broad_command_mid_sentence_passes(tmp_path):
    assert _body_cls(tmp_path, "Earlier we ran `pnpm vitest run` and it was slow.") == "PASS"
    assert _body_cls(tmp_path, "```yaml\n    note: pnpm vitest run\n    ```") == "PASS"


def test_broad_command_as_step_span_or_shell_fence_is_refused(tmp_path):
    assert _body_cls(tmp_path, "Run `pnpm vitest run`.") == "DEFECT"
    assert _body_cls(tmp_path, "`pnpm vitest run`") == "DEFECT"
    assert _body_cls(tmp_path, "```sh\n    pnpm vitest run\n    ```") == "DEFECT"


def test_sh_fenced_path_scoped_pnpm_test_passes(tmp_path):
    assert _body_cls(tmp_path, "```sh\n    pnpm test src/a.test.ts\n    ```") == "PASS"


def test_refusal_names_rule_and_both_repair_forms(tmp_path):
    result = _gate(tmp_path, _ROW)
    cls = result["classes"]["TEST_RUNNER_OUT_OF_ROLE"]
    for text in (cls["detail"], result["message"]):
        assert "a dispatched subagent never runs one" in text
        assert "writes nothing declares `performer: em`" in text
        assert "keeps its writes and loses the command" in text
        assert "depends on it" in text


def test_class_is_mechanical_with_repair(tmp_path):
    cls = _cls(tmp_path, _ROW)
    assert cls["mechanical"] is True
    assert "performer: em" in cls["repair"] and "C0" in cls["repair"]


def test_class_order(tmp_path):
    result = _gate(tmp_path, _ROW)
    assert tuple(result["classes"]) == pg.CLASS_ORDER
    order = pg.CLASS_ORDER
    assert order.index("TEST_RUNNER_OUT_OF_ROLE") == order.index("EXECUTOR_HEAVY") + 1
