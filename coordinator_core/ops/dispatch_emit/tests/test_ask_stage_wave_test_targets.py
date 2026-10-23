"""ask_stage emits a cumulative declared-test scope per non-last wave."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_stage import stage

PLAN_REL = "docs/plans/2026-10-01-wave-targets.md"


def _plan(rows: list[tuple[str, str, str | None]]) -> str:
    out = ["---", "deliverable_id: dlv-wave-abc123", "---", "# Wave targets", "", "## Problem", "", "x", "",
           "## Tasks", "", "```yaml plan-tasks"]
    for rid, path, dep in rows:
        out += [f"- id: {rid}", f"  title: row {rid}", "  change_kind: code-edit", f"  surface: {path}",
                "  writes:", f"    - {path}"]
        if dep:
            out += ["  depends_on:", f"    - chunk: {dep}", "      gate_kind: output-consumption-runtime"]
    out += ["```", ""]
    return "\n".join(out)


def _repo(tmp_path, rows):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_plan(rows), encoding="utf-8", newline="\n")
    for _, path, _ in rows:
        if path.endswith(".py"):
            tests = tmp_path / "pkg" / "tests"
            tests.mkdir(parents=True, exist_ok=True)
            (tests / f"test_{path.rsplit('/', 1)[-1]}").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def three_wave(tmp_path):
    return _repo(tmp_path, [("C1", "pkg/a.py", None), ("C2", "pkg/b.py", "C1"), ("C3", "pkg/c.py", "C2")])


def test_three_wave_stage_keys_every_wave_but_the_last_and_is_cumulative(three_wave):
    m = stage(three_wave, run_id="r1", plan_rel=PLAN_REL)
    assert {r.id: r.wave for r in m.rows} == {"C1": 0, "C2": 1, "C3": 2}
    assert set(m.wave_test_targets) == {"0", "1"}
    assert set(m.wave_test_targets["0"]) < set(m.wave_test_targets["1"])
    assert set(m.wave_test_targets["1"]) < set(m.test_targets)
    assert "pkg/tests/test_a.py" in m.wave_test_targets["0"]


def test_wave_without_a_testable_write_has_no_key(tmp_path):
    root = _repo(tmp_path, [("C1", "docs/note.md", None), ("C2", "pkg/b.py", "C1"), ("C3", "pkg/c.py", "C2")])
    m = stage(root, run_id="r1", plan_rel=PLAN_REL)
    assert "0" not in m.wave_test_targets
    assert m.wave_test_targets["1"] == ("pkg/tests/test_b.py",)


def test_single_wave_stage_has_no_wave_targets(tmp_path):
    root = _repo(tmp_path, [("C1", "pkg/a.py", None)])
    assert stage(root, run_id="r1", plan_rel=PLAN_REL).wave_test_targets == {}
