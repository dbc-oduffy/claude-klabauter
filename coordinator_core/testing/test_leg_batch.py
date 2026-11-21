"""Batching and partial credit for the reverify test leg (`leg_batch`, `leg_reporter`, `test_leg_run`)."""
from __future__ import annotations

import json
import subprocess
import textwrap

import pytest
from pathlib import Path

from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.testing import leg_batch


@pytest.fixture(autouse=True)
def _svc(tmp_path_factory, monkeypatch):
    from coordinator_core.warm import breadcrumb

    d = tmp_path_factory.mktemp("svc")
    monkeypatch.setattr(breadcrumb, "svc_dir", lambda engine_root=None: d)
    return d


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "tests").mkdir()
    for name, body in files.items():
        (tmp_path / "tests" / name).write_text(textwrap.dedent(body), encoding="utf-8")
    return tmp_path


def test_plan_splits_known_durations_to_fit_the_cap_and_names_an_over_cap_file():
    durs = {"a.py": 300.0, "b.py": 300.0, "c.py": 100.0, "huge.py": 1000.0}
    legs, slow = leg_batch.plan_legs(["a.py", "b.py", "c.py", "huge.py"], durs, 540.0)
    assert legs == [["a.py"], ["b.py", "c.py"]]
    assert slow == [{"file": "huge.py", "seconds": 1000.0}]


def test_plan_without_durations_splits_by_file_count_and_keeps_a_small_set_in_one_leg():
    many = [f"t{i}.py" for i in range(45)]
    legs, slow = leg_batch.plan_legs(many, {}, 540.0)
    assert [len(x) for x in legs] == [20, 20, 5] and slow == []
    assert leg_batch.plan_legs(many[:3], {}, 540.0)[0] == [many[:3]]


def test_plan_keeps_node_ids_of_a_slow_file_runnable():
    legs, slow = leg_batch.plan_legs(["huge.py::test_one"], {"huge.py": 1000.0}, 540.0)
    assert legs == [["huge.py::test_one"]] and slow == []


def test_batching_runs_one_pytest_per_leg_never_one_per_test(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake(targets, cwd, remaining):
        calls.append(list(targets))
        return 0, ["1 passed in 0.1s"]

    names = [f"tests/t{i}.py" for i in range(45)]
    rc, _, info = leg_batch.run_legs(names, str(tmp_path), fake, lambda: 500.0, 540.0, tmp_path / "s")
    assert rc == 0 and info["legs"] == 3 == len(calls)
    assert sum(len(c) for c in calls) == 45


def test_a_timeout_credits_every_finished_test_and_names_the_slow_file(tmp_path):
    root = _repo(tmp_path, {
        "test_fast.py": """
            def test_ok():
                assert True
            def test_bad():
                assert 0, "boom"
        """,
        "test_slow.py": """
            import time
            def test_sleeps():
                time.sleep(60)
        """,
    })
    result = tlr.run(["tests/test_fast.py", "tests/test_slow.py"], base_sha=None, head_sha=None,
                     base_export_out=None, budget_s=12.0, cwd=str(root))
    assert result["over_budget"] is True
    assert result["tests_run"] == 2 and result["tests_failed"] == 1
    assert result["failing"] == ["tests/test_fast.py::test_bad"]
    assert result["leg_type"] == "test_infra"
    assert any(s["file"] == "tests/test_slow.py" and s["seconds"] > 0 for s in result["slow_targets"])
    assert "tests/test_slow.py" in " ".join(result["unrun_targets"])
    assert "tests/test_fast.py" not in " ".join(result["unrun_targets"])


def test_a_single_over_cap_file_is_named_with_its_duration_and_not_run(tmp_path):
    root = _repo(tmp_path, {"test_a.py": "def test_ok():\n    assert True\n",
                            "test_big.py": "def test_never():\n    raise SystemExit(9)\n"})
    leg_batch._save_durations(str(root), {"tests/test_big.py": 900.0})
    result = tlr.run(["tests/test_a.py", "tests/test_big.py"], base_sha=None, head_sha=None,
                     base_export_out=None, budget_s=60.0, cwd=str(root))
    assert result["tests_run"] == 1
    assert result["slow_targets"] == [{"file": "tests/test_big.py", "seconds": 900.0}]
    assert result["unrun_targets"] == ["tests/test_big.py"]
    assert result["leg_type"] == "test_infra" and result["status"] != "pass"
    assert any(d["node_id"] == "tests/test_big.py" for d in result["failing_detail"])


def test_durations_are_recorded_from_a_completed_leg(tmp_path):
    root = _repo(tmp_path, {"test_a.py": "def test_ok():\n    assert True\n"})
    tlr.run(["tests/test_a.py"], base_sha=None, head_sha=None, base_export_out=None, budget_s=60.0, cwd=str(root))
    stored = leg_batch.load_durations(str(root))
    assert "tests/test_a.py" in stored


def test_batching_adds_one_process_per_leg(tmp_path, monkeypatch):
    spawns: list[list] = []
    real = subprocess.run

    def counting(argv, *a, **k):
        spawns.append(argv)
        return real(argv, *a, **k)

    root = _repo(tmp_path, {f"test_{i}.py": "def test_ok():\n    assert True\n" for i in range(25)})
    monkeypatch.setattr(tlr.subprocess, "run", counting)
    result = tlr.run([f"tests/test_{i}.py" for i in range(25)], base_sha=None, head_sha=None,
                     base_export_out=None, budget_s=120.0, cwd=str(root))
    assert result["tests_run"] == 25 and len(spawns) == 2


def test_a_timeout_with_no_finished_test_keeps_the_zero_run_budget_cause(tmp_path):
    root = _repo(tmp_path, {"test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(60)\n"})
    result = tlr.run(["tests/test_slow.py"], base_sha=None, head_sha=None, base_export_out=None, budget_s=3.0,
                     cwd=str(root))
    assert result["tests_run"] == 0 and result["zero_run_cause"] == "budget-exhausted"


def test_nothing_is_written_under_the_repo_root(tmp_path, _svc):
    root = _repo(tmp_path, {"test_a.py": "def test_ok():\n    assert True\n"})
    before = {p.relative_to(root) for p in root.rglob("*")}
    tlr.run(["tests/test_a.py"], base_sha=None, head_sha=None, base_export_out=None, budget_s=60.0, cwd=str(root))
    after = {p.relative_to(root) for p in root.rglob("*") if "__pycache__" not in p.parts}
    assert after - before == set()
    assert list((_svc / "test-leg-durations").glob("*.json"))
