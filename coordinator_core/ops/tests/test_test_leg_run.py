"""test_leg_run over a throwaway repo: the test-shaped verdict, the spawn budget, the one deadline."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.review_reverify import _read_base_export

FAIL_ID = "tests/test_a.py::test_fails"
PASS_ID = "tests/test_a.py::test_passes"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text(
        "def test_passes():\n    assert True\n\ndef test_fails():\n    assert 1 == 2\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def _spawns(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = subprocess.run

    def counting(argv, *a, **kw):
        if "pytest" in argv:
            seen.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(tlr.subprocess, "run", counting)
    return seen


def _main(argv: list[str], out: Path) -> tuple[int, dict]:
    cut = argv.index("--")
    code = tlr.main([*argv[:cut], "--out", str(out), *argv[cut:]])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_red_scope_reports_failing_ids_in_test_shape(repo, tmp_path):
    code, data = _main(["--", "tests/test_a.py"], tmp_path / "o.json")
    assert code == 0
    assert data["status_after"] == "fail"
    assert data["failing"] == [FAIL_ID]
    assert data["failing_detail"][0]["node_id"] == FAIL_ID and data["failing_detail"][0]["message"]
    assert data["passed_alone"] == [] and data["pre_existing"] == [] and data["passed_at_base"] == []
    assert data["reverted"] == [] and data["diff_named"] == []
    assert data["over_budget"] is False and data["budget_s"] == tlr.TEST_LEG_MINUTES_BUDGET * 60


def test_all_green_scope_spawns_exactly_one_pytest(repo, tmp_path, monkeypatch):
    seen = _spawns(monkeypatch)
    code, data = _main(["--", PASS_ID], tmp_path / "o.json")
    assert code == 0 and data["status_after"] == "pass" and data["failing"] == []
    assert len(seen) == 1


def test_red_scope_spawns_two_pytests_and_one_measure(repo, tmp_path, monkeypatch):
    seen = _spawns(monkeypatch)
    measured = []

    def fake_measure(sha, head_sha, ids, cwd, timeout_s=None):
        measured.append((sha, head_sha, list(ids), timeout_s))
        return {"red": [FAIL_ID], "export": "ok", "runner_exit": 1, "passed_at_base": []}

    monkeypatch.setattr(tlr.reverify_base_measure, "measure", fake_measure)
    _code, data = _main(["--base-sha", "b" * 40, "--", "tests/test_a.py"], tmp_path / "o.json")
    assert len(seen) == 2
    assert seen[1][-1] == FAIL_ID
    assert len(measured) == 1 and measured[0][:3] == ("b" * 40, None, [FAIL_ID])
    assert measured[0][3] is not None and measured[0][3] <= tlr.TEST_LEG_MINUTES_BUDGET * 60
    assert data["pre_existing"] == [FAIL_ID]


def test_head_sha_is_passed_through_to_the_measure(repo, tmp_path, monkeypatch):
    measured = []

    def fake_measure(sha, head_sha, ids, cwd, timeout_s=None):
        measured.append(head_sha)
        return {"red": [], "export": "ok", "runner_exit": 0, "passed_at_base": [FAIL_ID]}

    monkeypatch.setattr(tlr.reverify_base_measure, "measure", fake_measure)
    _code, data = _main(["--base-sha", "b" * 40, "--head-sha", "HEAD", "--", FAIL_ID], tmp_path / "o.json")
    assert measured == ["HEAD"] and data["passed_at_base"] == [FAIL_ID]


def test_no_base_sha_never_measures(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(tlr.reverify_base_measure, "measure",
                        lambda *a, **k: pytest.fail("measure called without --base-sha"))
    _main(["--", FAIL_ID], tmp_path / "o.json")


def test_flaky_red_that_passes_on_the_rerun_is_passed_alone(repo, tmp_path):
    (repo / "tests" / "test_flaky.py").write_text(
        "import pathlib\n\ndef test_flaky():\n    m = pathlib.Path(__file__).with_name('seen')\n"
        "    first = not m.exists()\n    m.write_text('x')\n    assert not first\n", encoding="utf-8")
    _code, data = _main(["--", "tests/test_flaky.py"], tmp_path / "o.json")
    assert data["failing"] == ["tests/test_flaky.py::test_flaky"]
    assert data["passed_alone"] == ["tests/test_flaky.py::test_flaky"]


def test_pytest_exit_with_no_parsed_red_is_a_fail_never_a_pass(repo, tmp_path):
    (repo / "tests" / "empty_dir").mkdir()
    code, data = _main(["--", "tests/empty_dir"], tmp_path / "o.json")
    assert code == 0 and data["status_after"] == "fail" and data["failing"] == []
    assert "exit 5" in data["failing_detail"][0]["message"]


def test_absent_only_targets_spawn_no_pytest_and_stamp_missing_target(repo, tmp_path, monkeypatch):
    seen = _spawns(monkeypatch)
    code, data = _main(["--", "tests/test_gone.py", "tests/test_gone2.py::test_x"], tmp_path / "o.json")
    assert seen == [] and code == 0 and data["over_budget"] is False
    assert data["zero_run_cause"] == ltt.ZERO_RUN_MISSING_TARGET == "missing-target"
    assert data["tests_run"] == 0 and data["status"] == "error" and data["status_after"] == "fail"
    assert data["missing_targets"] == ["tests/test_gone.py", "tests/test_gone2.py::test_x"]
    assert "tests/test_gone.py" in data["summary"] and "tests/test_gone2.py" in data["summary"]
    assert "engine-side" in ltt._ZERO_RUN_CAUSES[ltt.ZERO_RUN_MISSING_TARGET]


def test_mixed_present_and_absent_targets_run_only_the_present_ones(repo, tmp_path, monkeypatch):
    seen = _spawns(monkeypatch)
    _code, data = _main(["--", PASS_ID, "tests/test_gone.py"], tmp_path / "o.json")
    assert len(seen) == 1 and "tests/test_gone.py" not in seen[0] and PASS_ID in seen[0]
    assert data["missing_targets"] == ["tests/test_gone.py"]
    assert (data["tests_run"], data["status"], data["status_after"]) == (1, "pass", "pass")
    assert "zero_run_cause" not in data


def test_all_present_targets_record_no_missing_targets(repo, tmp_path):
    _code, data = _main(["--", PASS_ID], tmp_path / "o.json")
    assert "missing_targets" not in data


def test_collection_error_is_a_fail(repo, tmp_path):
    (repo / "tests" / "test_broken.py").write_text("import not_a_real_module_xyz\n", encoding="utf-8")
    _code, data = _main(["--", "tests/test_broken.py"], tmp_path / "o.json")
    assert data["status_after"] == "fail"


def test_over_budget_reports_timeout_and_exits_three(repo, tmp_path):
    (repo / "tests" / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8")
    code, data = _main(["--budget-s", "1", "--", "tests/test_slow.py"], tmp_path / "o.json")
    assert code == tlr.EXIT_TIMEOUT
    assert data["over_budget"] is True and data["status_after"] == "fail"
    assert data["summary"].startswith("timeout:") and data["failing_detail"][0]["message"].startswith("timeout:")
    assert data["elapsed_s"] < 20


def test_one_deadline_spans_the_steps(repo, tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(tlr.time, "monotonic", lambda: clock[0])
    timeouts = []

    def fake_pytest(targets, cwd, remaining):
        timeouts.append(remaining)
        clock[0] += 40.0
        return 1, [f"FAILED {FAIL_ID} - assert"]

    monkeypatch.setattr(tlr, "_pytest", fake_pytest)
    data = tlr.run(["tests"], base_sha=None, head_sha=None, base_export_out=None, budget_s=100.0, cwd=str(repo))
    assert timeouts == [100.0, 60.0] and data["over_budget"] is False


def test_base_measure_timeout_is_over_budget(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(tlr.reverify_base_measure, "measure",
                        lambda *a, **k: {"red": [], "export": "timeout", "runner_exit": None, "passed_at_base": []})
    code, data = _main(["--base-sha", "b" * 40, "--", FAIL_ID], tmp_path / "o.json")
    assert code == tlr.EXIT_TIMEOUT and data["over_budget"] is True and data["summary"].startswith("timeout:")


def test_real_base_measure_fills_pre_existing_and_writes_the_export(repo, tmp_path):
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_a.py").write_text(
        "from pkg import VALUE\n\ndef test_red_at_base():\n    assert VALUE == 2\n", encoding="utf-8")
    (repo / "tests" / "test_b.py").write_text(
        "from pkg import VALUE\n\ndef test_green_at_base():\n    assert VALUE == 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pkg" / "__init__.py").write_text("VALUE = 3\n", encoding="utf-8")
    export = tmp_path / "export.txt"
    _code, data = _main(["--base-sha", base, "--base-export-out", str(export), "--", "tests"], tmp_path / "o.json")
    assert sorted(data["failing"]) == ["tests/test_a.py::test_red_at_base", "tests/test_b.py::test_green_at_base"]
    assert data["pre_existing"] == ["tests/test_a.py::test_red_at_base"]
    assert data["passed_at_base"] == ["tests/test_b.py::test_green_at_base"]
    assert _read_base_export(repo, str(export)) == {"tests/test_a.py::test_red_at_base"}


@pytest.mark.parametrize("argv", [
    ["--out", "o.json"],
    ["--out", "o.json", "--"],
    ["--", "tests"],
    ["--out", "o.json", "--bogus", "x", "--", "tests"],
])
def test_malformed_argv_is_a_usage_error(argv, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert tlr.main(argv) == tlr.EXIT_USAGE
    assert not (tmp_path / "o.json").exists()


@pytest.fixture
def landing(tmp_path, monkeypatch):
    """A git repo: `pkg/mod.py` with a direct importer test and a pre-existing red; returns (root, base sha).
    The mirror's system Temp is private to the test."""
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_importer.py").write_text(
        "from pkg import mod\n\ndef test_uses_value():\n    assert mod.VALUE == 1\n\n"
        "def test_always_red():\n    assert False\n", encoding="utf-8")
    (root / "tests" / "test_unrelated.py").write_text("def test_other():\n    assert 1 == 2\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    monkeypatch.chdir(root)
    return root, _git(root, "rev-parse", "HEAD")


def _land(root: Path, rel: str, text: str) -> str:
    (root / rel).write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "land")
    return _git(root, "rev-parse", "HEAD")


def test_default_leg_reports_a_direct_importers_new_red_and_spares_a_pre_existing_one(landing, tmp_path):
    root, base = landing
    post = _land(root, "pkg/mod.py", "VALUE = 2\n")
    (root / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")  # worktree dirt the clone must not see

    code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "pkg/mod.py", "--", "tests/test_unrelated.py"],
                       tmp_path / "o.json")

    assert code == 0 and data["over_budget"] is False
    assert data["importer_targets"] == ["tests/test_importer.py"]
    assert data["new_red"] == ["tests/test_importer.py::test_uses_value"]
    assert sorted(data["pre_existing"]) == ["tests/test_importer.py::test_always_red", "tests/test_unrelated.py::test_other"]
    assert data["status_after"] == "fail"
    assert (cov.mirror_root(root) / root.name / ".git").is_dir()


def test_default_leg_passes_when_every_red_is_red_at_base(landing, tmp_path):
    root, base = landing
    post = _land(root, "pkg/mod.py", "VALUE = 1\nOTHER = 3\n")

    _code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "pkg/mod.py", "--", "tests/test_unrelated.py"],
                        tmp_path / "o.json")

    assert data["new_red"] == [] and data["status_after"] == "pass"
    assert "tests/test_importer.py::test_always_red" in data["pre_existing"]


def test_a_new_test_red_with_no_counterpart_at_base_is_a_new_red(landing, tmp_path):
    root, base = landing
    (root / "tests" / "test_fresh.py").write_text("def test_fresh():\n    assert False\n", encoding="utf-8")
    post = _land(root, "pkg/mod.py", "VALUE = 1\n# landed\n")

    _code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "tests/test_fresh.py", "--", "tests/test_fresh.py"],
                        tmp_path / "o.json")

    assert data["new_red"] == ["tests/test_fresh.py::test_fresh"]


def test_touched_without_a_base_sha_excuses_no_red(landing, tmp_path):
    root, _base = landing
    post = _land(root, "pkg/mod.py", "VALUE = 1\n# landed\n")

    _code, data = _main(["--head-sha", post, "--touched", "pkg/mod.py", "--", "tests/test_unrelated.py"], tmp_path / "o.json")

    assert "tests/test_importer.py::test_always_red" in data["new_red"] and data["pre_existing"] == []


def test_without_touched_the_leg_keeps_running_in_cwd_and_reports_no_new_red(landing, tmp_path):
    _code, data = _main(["--", "tests/test_unrelated.py"], tmp_path / "o.json")
    assert "new_red" not in data and data["failing"] == ["tests/test_unrelated.py::test_other"]


def test_red_and_green_scopes_report_tests_run_and_status(repo, tmp_path):
    _code, red = _main(["--", "tests/test_a.py"], tmp_path / "r.json")
    assert (red["tests_run"], red["tests_failed"], red["status"]) == (2, 1, "fail")
    assert "zero_run_cause" not in red
    _code, green = _main(["--", PASS_ID], tmp_path / "g.json")
    assert (green["tests_run"], green["tests_failed"], green["status"]) == (1, 0, "pass")
    assert "zero_run_cause" not in green


def test_timeout_reports_zero_tests_budget_exhausted(repo, tmp_path):
    (repo / "tests" / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8")
    _code, data = _main(["--budget-s", "1", "--", "tests/test_slow.py"], tmp_path / "o.json")
    assert data["tests_run"] == 0 and data["status"] == "error"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_BUDGET == "budget-exhausted"


def test_collection_error_reports_collection_failure(repo, tmp_path):
    (repo / "tests" / "test_broken.py").write_text("import not_a_real_module_xyz\n", encoding="utf-8")
    _code, data = _main(["--", "tests/test_broken.py"], tmp_path / "o.json")
    assert data["tests_run"] == 0 and data["status"] == "error"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_COLLECTION == "collection-failure"


def test_empty_selection_reports_empty_selection(repo, tmp_path):
    (repo / "tests" / "empty_dir").mkdir()
    _code, data = _main(["--", "tests/empty_dir"], tmp_path / "o.json")
    assert data["tests_run"] == 0 and data["status"] == "error"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_EMPTY == "empty-selection"


def test_killed_child_reports_killed(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(tlr, "_pytest", lambda targets, cwd, remaining: (-9, []))
    _code, data = _main(["--", PASS_ID], tmp_path / "o.json")
    assert data["tests_run"] == 0 and data["status"] == "error"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_KILLED == "killed"
    assert "killed" in ltt.zero_run_cause_note({"zero_run_cause": "killed"}, "x")


def test_clone_leg_with_only_absent_targets_is_a_missing_target_zero_run(landing, tmp_path):
    root, _base = landing
    post = _land(root, "README.md", "x\n")
    _code, data = _main(["--head-sha", post, "--touched", "README.md", "--", "tests/nonexistent_dir_placeholder.py"],
                        tmp_path / "o.json")
    assert data["tests_run"] == 0 and data["status"] == "error"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_MISSING_TARGET
    assert data["missing_targets"] == ["tests/nonexistent_dir_placeholder.py"]


def test_clone_leg_runs_the_present_target_and_records_the_absent_one(landing, tmp_path):
    root, _base = landing
    post = _land(root, "README.md", "x\n")
    _code, data = _main(["--head-sha", post, "--touched", "README.md", "--", "tests/test_unrelated.py", "tests/gone.py"],
                        tmp_path / "o.json")
    assert data["missing_targets"] == ["tests/gone.py"]
    assert data.get("zero_run_cause") != ltt.ZERO_RUN_MISSING_TARGET and data["tests_run"] == 1


def test_clone_leg_runs_only_the_touched_tests_of_a_touched_file_and_skips_slow_tiers(landing, tmp_path):
    root, base = landing
    (root / "tests" / "test_pair.py").write_text(
        "import pytest\n\n\ndef test_a():\n    assert True\n\n\ndef test_b():\n    assert False\n\n\n"
        "@pytest.mark.cadence\ndef test_slow():\n    assert False\n", encoding="utf-8")
    base = _land(root, "pkg/mod.py", "VALUE = 1\n# pair\n")
    post = _land(root, "tests/test_pair.py", (root / "tests" / "test_pair.py").read_text(encoding="utf-8").replace("assert True", "assert 1"))

    _code, data = _main(["--base-sha", base, "--head-sha", post, "--touched", "tests/test_pair.py", "--", "tests/test_pair.py"],
                        tmp_path / "o.json")

    assert data["targets_run"] == ["tests/test_pair.py::test_a"], data
    assert data["new_red"] == [] and data["status_after"] == "pass" and data["tests_run"] == 1


def test_clone_leg_over_only_slow_tier_tests_passes_instead_of_failing_on_exit_5(landing, tmp_path):
    root, _base = landing
    (root / "tests" / "test_slow_only.py").write_text(
        "import pytest\n\n\n@pytest.mark.cadence\ndef test_slow():\n    assert False\n", encoding="utf-8")
    post = _land(root, "tests/test_slow_only.py", (root / "tests" / "test_slow_only.py").read_text(encoding="utf-8"))

    _code, data = _main(["--head-sha", post, "--touched", "tests/test_slow_only.py", "--", "tests/test_slow_only.py"],
                        tmp_path / "o.json")

    assert data["status_after"] == "pass" and data["new_red"] == [], data


def test_the_pytest_child_has_no_shell_and_uses_the_running_interpreter(repo, tmp_path, monkeypatch):
    seen = _spawns(monkeypatch)
    _main(["--", PASS_ID], tmp_path / "o.json")
    assert seen[0][0] == sys.executable and seen[0][1:3] == ["-m", "pytest"]
