"""Replay of doe 9bcd34ebb8: moving a heavy command to a test-runner V row must still pull the plan."""

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _CLEAN_FM, _write_plan

_EXEC_ROW = """- id: C1
  title: Build
  change_kind: code-edit
  surface: src/a.py
  writes: [src/a.py]
  disposition: open
  body: |
    Run `cargo build --release`.
"""
_V_ROW = """- id: V1
  title: Verify
  change_kind: verification
  surface: src/a.py
  writes: []
  agent_type: coordinator:test-runner
  disposition: open
  body: |
    Run `{cmd}`.
"""


def _agents(tmp_path, monkeypatch, admits):
    d = tmp_path / "agents"
    d.mkdir()
    for name, fm in admits.items():
        (d / f"{name}.md").write_text(f"---\nname: {name}\n{fm}---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(pg, "_agent_definitions_dir", lambda: d)


def _gate(tmp_path, spine):
    (tmp_path / "src").mkdir(exist_ok=True)
    return pg.gate_plan(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))


def test_build_command_moved_to_test_runner_row_still_pulls(tmp_path, monkeypatch):
    _agents(tmp_path, monkeypatch, {"executor": "", "test-runner": ""})
    result = _gate(tmp_path, _V_ROW.format(cmd="cargo build --release"))
    cls = result["classes"]["TEST_RUNNER_OUT_OF_ROLE"]
    assert result["verdict"] == pg.NOT_PREPPED
    assert cls["status"] == "DEFECT"
    assert "V1" in cls["withheld"]


def test_broad_suite_moved_to_test_runner_row_still_pulls(tmp_path, monkeypatch):
    _agents(tmp_path, monkeypatch, {"executor": "", "test-runner": ""})
    result = _gate(tmp_path, _V_ROW.format(cmd="pnpm vitest run"))
    assert result["classes"]["TEST_RUNNER_OUT_OF_ROLE"]["status"] == "DEFECT"


def test_scoped_test_on_test_runner_row_still_passes(tmp_path, monkeypatch):
    _agents(tmp_path, monkeypatch, {"executor": "", "test-runner": ""})
    result = _gate(tmp_path, _V_ROW.format(cmd="python -m pytest src/test_a.py"))
    assert result["classes"]["TEST_RUNNER_OUT_OF_ROLE"]["status"] == "PASS"


def test_no_role_admits_both_remedy_names_em_and_never_test_runner(tmp_path, monkeypatch):
    _agents(tmp_path, monkeypatch, {"executor": "", "test-runner": "admits: [build]\n"})
    result = _gate(tmp_path, _EXEC_ROW)
    cls = result["classes"]["EXECUTOR_HEAVY"]
    assert cls["status"] == "DEFECT"
    assert "performer: em" in cls["detail"]
    assert "9d7129" in cls["detail"]
    assert "agent_type: coordinator:test-runner" not in cls["detail"]
    assert cls["mechanical"] is True


def test_remedy_names_the_role_the_definitions_admit(tmp_path, monkeypatch):
    _agents(
        tmp_path, monkeypatch,
        {"executor": "", "test-runner": "", "heavy-runner": "admits: [build, measure]\n"},
    )
    cls = _gate(tmp_path, _EXEC_ROW)["classes"]["EXECUTOR_HEAVY"]
    assert "agent_type: coordinator:heavy-runner" in cls["detail"]
    assert "coordinator:test-runner" not in cls["detail"]


def test_row_retyped_to_admitted_role_passes(tmp_path, monkeypatch):
    _agents(tmp_path, monkeypatch, {"heavy-runner": "admits: [build, measure]\n"})
    spine = _EXEC_ROW.replace("  disposition: open", "  agent_type: coordinator:heavy-runner\n  disposition: open")
    assert _gate(tmp_path, spine)["classes"]["EXECUTOR_HEAVY"]["status"] == "PASS"
