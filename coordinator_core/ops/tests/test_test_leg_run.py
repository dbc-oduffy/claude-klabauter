"""test_leg_run over a throwaway repo: the test-shaped verdict, the spawn budget, the one deadline."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import test_leg_run as tlr
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

FAIL_ID = "tests/test_a.py::test_fails"
PASS_ID = "tests/test_a.py::test_passes"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text(
        "def test_passes():\n    assert True\n\ndef test_fails():\n    assert 1 == 2\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def _view(argv) -> list[str]:
    """`argv` with an ids-runner spawn read back as `[exe, -m, pytest, *flags, *ids]`; any other argv unchanged."""
    argv = list(argv)
    if tlr._IDS_RUNNER not in argv:
        return argv
    at = argv.index(tlr._IDS_RUNNER)
    ids = [i for i in Path(argv[at + 1]).read_text(encoding="utf-8").split("\n") if i]
    return [*argv[:at - 1], "-m", "pytest", *argv[at + 2:], *ids]


def _spawns(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = subprocess.run

    def counting(argv, *a, **kw):
        view = _view(argv)
        if "pytest" in view:
            seen.append(view)
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


def _raw_pytest(monkeypatch, rc, lines, stderr="", stdout=""):
    """Stub `_pytest` the way the production one reports: (rc, lines), the raw streams on `_RAW`."""
    def fake(*_args, **_kw):
        tlr._RAW.append((stderr, stdout))
        return rc, lines

    monkeypatch.setattr(tlr, "_pytest", fake)


def test_usage_error_with_named_nodes_records_raw_phase_diagnostics(repo, tmp_path, monkeypatch):
    calls = []

    def usage_error(targets, *_):
        calls.append(targets)
        tlr._RAW.append(("x" * 5000 + "bad option", ""))
        return 4, [f"ERROR {PASS_ID}"]

    monkeypatch.setattr(tlr, "_pytest", usage_error)
    _code, data = _main(["--", PASS_ID], tmp_path / "o.json")
    assert data["failing"] == [PASS_ID]
    assert data["status_after"] == "fail"
    assert data["pytest_phases"] == [{"exit_code": 4, "stderr": "x" * 4086 + "bad option",
                                       "stdout": "", "encoding": "utf-8"}]
    assert calls == [[PASS_ID]]


def test_delivery_markdown_is_excluded_from_pytest_targets(repo, tmp_path, monkeypatch):
    delivery = "delivery.md"
    (repo / delivery).write_text("# Delivery\n", encoding="utf-8")
    seen = _spawns(monkeypatch)
    _code, data = _main(["--", delivery, PASS_ID], tmp_path / "o.json")
    assert (data["status"], data["tests_run"]) == ("pass", 1)
    assert data["not_a_test"] == [delivery]
    assert seen[0][-1] == PASS_ID and delivery not in seen[0]


def test_only_fixture_setup_errors_do_not_rerun_or_measure_named_tests(repo, tmp_path, monkeypatch):
    (repo / "tests" / "conftest.py").write_text(
        "import pytest\n@pytest.fixture(autouse=True)\ndef broken():\n    raise RuntimeError('setup broke')\n",
        encoding="utf-8",
    )
    seen = _spawns(monkeypatch)
    _code, data = _main(["--", PASS_ID], tmp_path / "o.json")
    assert (data["status"], data["tests_run"], len(seen)) == ("error", 0, 1)


def test_pytest_inherits_explicit_coordinator_settings_home(repo, tmp_path, monkeypatch):
    settings = str(tmp_path / "explicit-settings")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", settings)
    (repo / "tests" / "test_a.py").write_text(
        f"import os\ndef test_passes():\n    assert os.environ['COORDINATOR_SETTINGS_HOME'] == {settings!r}\n",
        encoding="utf-8",
    )
    _code, data = _main(["--", PASS_ID], tmp_path / "o.json")
    assert (data["status"], data["tests_run"]) == ("pass", 1)


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


def test_usage_failure_keeps_bounded_stderr_and_process_exit(repo, monkeypatch):
    _raw_pytest(monkeypatch, 4, [], "x" * 5000 + "bad option")
    data = tlr.run([PASS_ID], base_sha=None, head_sha=None, base_export_out=None,
                   budget_s=20, cwd=str(repo))
    assert data["tests_run"] == 0
    assert data["zero_run_cause"] == ltt.ZERO_RUN_PROCESS_ERROR
    assert data["pytest_exit_code"] == 4
    assert len(data["pytest_stderr"]) == 4096 and data["pytest_stderr"].endswith("bad option")


def _engine_import_error(repo: Path) -> None:
    (repo / "coordinator_core").mkdir()
    (repo / "coordinator_core" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "coordinator_core" / "dirty.py").write_text("from coordinator_core.clean import _GONE\n", encoding="utf-8")
    (repo / "coordinator_core" / "clean.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "tests" / "test_dirty.py").write_text(
        "from coordinator_core import dirty\n\ndef test_a():\n    assert True\n", encoding="utf-8")


def test_engine_module_import_error_reports_import_error_with_the_dirty_path(repo, tmp_path):
    _engine_import_error(repo)
    _code, data = _main(["--", "tests/test_dirty.py"], tmp_path / "o.json")
    assert data["tests_run"] == 0 and data["status"] == "error"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_IMPORT_ERROR == "import-error"
    assert data["import_error"] == {"path": "coordinator_core/dirty.py", "run_id": None}
    assert "import-error" in ltt.zero_run_cause_note({"zero_run_cause": "import-error"}, "x")


def test_import_error_run_id_is_the_newest_record_holding_the_worktree_blob(repo, tmp_path):
    from coordinator_core.ops.dispatch_emit import kept_work, stranded_sibling

    _engine_import_error(repo)
    blob = kept_work.fingerprint(repo, "coordinator_core/dirty.py")
    for name, run in (("run-old", blob), ("run-other", "0" * 40)):
        stranded_sibling.write(repo / "state" / "warp" / name, {"coordinator_core/dirty.py": {"base": None, "run": run}})
    _code, data = _main(["--", "tests/test_dirty.py"], tmp_path / "o.json")
    assert data["import_error"] == {"path": "coordinator_core/dirty.py", "run_id": "run-old"}


def test_import_error_outside_the_engine_stays_collection_failure(repo, tmp_path):
    (repo / "tests" / "test_broken.py").write_text("import not_a_real_module_xyz\n", encoding="utf-8")
    _code, data = _main(["--", "tests/test_broken.py"], tmp_path / "o.json")
    assert data["zero_run_cause"] == ltt.ZERO_RUN_COLLECTION
    assert "import_error" not in data


@pytest.mark.parametrize("lines, path", [
    (["coordinator_core\\ops\\a.py:3: in <module>", "E   ImportError: cannot import name 'x'"], "coordinator_core/ops/a.py"),
    (['  File "C:\\r\\coordinator_core\\b.py", line 2, in <module>', "ModuleNotFoundError: No module named 'q'"],
     "coordinator_core/b.py"),
    (["coordinator_core/a.py:1: in <module>", "tests/t.py:2: in <module>", "E   ImportError: x"], "coordinator_core/a.py"),
    (["tests/t.py:2: in <module>", "E   ImportError: x"], None),
    (["E   ImportError: no frame"], None),
])
def test_import_error_path_parse_matches_both_separators_and_picks_the_engine_frame(lines, path):
    assert tlr._import_error_path(lines) == path


def test_empty_selection_reports_empty_selection(repo, tmp_path):
    (repo / "tests" / "empty_dir").mkdir()
    _code, data = _main(["--", "tests/empty_dir"], tmp_path / "o.json")
    assert data["tests_run"] == 0 and data["status"] == "error"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_EMPTY == "empty-selection"


def test_successful_pytest_exit_without_completed_tests_is_infrastructure_error(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(tlr, "_pytest", lambda targets, cwd, remaining: (0, ["1 deselected in 0.01s"]))
    _code, data = _main(["--", PASS_ID], tmp_path / "o.json")
    assert (data["tests_run"], data["status"], data["status_after"]) == (0, "error", "fail")
    assert data["zero_run_cause"] == ltt.ZERO_RUN_EMPTY
    assert data["failing_detail"] == [{"node_id": "test_leg_run", "message": "ran zero tests (empty-selection)"}]


def test_a_leg_whose_only_test_is_a_strict_xfail_ran_one_test(repo, tmp_path):
    (repo / "tests" / "test_xf.py").write_text(
        "import pytest\n\n@pytest.mark.xfail(strict=True, reason='known')\ndef test_known():\n    assert 1 == 2\n",
        encoding="utf-8")
    _code, data = _main(["--", "tests/test_xf.py"], tmp_path / "o.json")
    assert (data["tests_run"], data["tests_failed"], data["status"], data["status_after"]) == (1, 0, "pass", "pass")
    assert "zero_run_cause" not in data


def test_xfailed_and_xpassed_summary_counts_are_completed_tests(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(tlr, "_pytest", lambda targets, cwd, remaining: (0, ["1 xfailed, 1 xpassed in 0.01s"]))
    _code, data = _main(["--", PASS_ID], tmp_path / "o.json")
    assert data["tests_run"] == 2 and "zero_run_cause" not in data


def test_pytest_usage_error_retains_stderr_in_test_receipt(repo, tmp_path, monkeypatch):
    def usage_error(*args, **kwargs):
        return tlr.subprocess.CompletedProcess(args[0], 4, "", "ERROR: fixture conftest could not be loaded\n")

    monkeypatch.setattr(tlr.subprocess, "run", usage_error)
    _code, data = _main(["--", PASS_ID], tmp_path / "o.json")
    assert (data["tests_run"], data["status"]) == (0, "error")
    assert "ERROR: fixture conftest could not be loaded" in data["failing_detail"][0]["message"]


@pytest.mark.parametrize("prefix", ["", "    E   "])
def test_bare_settings_exception_is_reported_as_environment_detail(prefix):
    node = "tests/test_round_trip_translation_loop_lift.py::test_round_trip"
    exc = "SettingsKeyUnresolved: repos.project_rag_ue_addon is not set"
    output = f"_____ test_round_trip _____\n{prefix}{exc}\n===== short test summary info =====\nFAILED {node}"
    assert tlr._detail(output, [node])[node] == f"environment: machine-local settings key unreadable ({exc})"


def test_killed_child_reports_killed(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(tlr, "_pytest", lambda targets, cwd, remaining: (-9, []))
    _code, data = _main(["--", PASS_ID], tmp_path / "o.json")
    assert data["tests_run"] == 0 and data["status"] == "error"
    assert data["zero_run_cause"] == ltt.ZERO_RUN_KILLED == "killed"
    assert "killed" in ltt.zero_run_cause_note({"zero_run_cause": "killed"}, "x")


BIN_TEST = "coordinator/bin/tests/test_bin_tool.py"


def test_the_pytest_child_has_no_shell_and_uses_the_running_interpreter(repo, tmp_path, monkeypatch):
    seen = _spawns(monkeypatch)
    _main(["--", PASS_ID], tmp_path / "o.json")
    assert seen[0][0] == sys.executable and seen[0][1:3] == ["-m", "pytest"]


OVERFLOW = 32 * 1024


def _many_reds(repo: Path, n: int = 500) -> str:
    (repo / "tests" / "test_many.py").write_text(
        "".join(f"def test_red_{i:04d}_{'x' * 40}():\n    assert False\n" for i in range(n)), encoding="utf-8")
    return "tests/test_many.py"


def test_a_reds_list_over_32k_chars_settles_with_no_id_on_argv(repo, monkeypatch):
    target = _many_reds(repo)
    argvs: list[list[str]] = []
    real = subprocess.run

    def watch(argv, *a, **kw):
        argvs.append(list(argv))
        return real(argv, *a, **kw)

    measured: list[list[str]] = []

    def fake_measure(sha, head_sha, ids, cwd, timeout_s=None):
        measured.append(list(ids))
        return {"red": list(ids), "export": "ok", "runner_exit": 1, "passed_at_base": []}

    monkeypatch.setattr(tlr.subprocess, "run", watch)
    monkeypatch.setattr(tlr.reverify_base_measure, "measure", fake_measure)
    data = tlr.run([target], base_sha=BASE, head_sha=None, base_export_out=None, budget_s=300.0, cwd=str(repo))
    assert len(data["failing"]) == 500 and sum(len(i) for i in data["failing"]) > OVERFLOW
    assert data["status_after"] == "fail" and data["over_budget"] is False and data["tests_run"] == 500
    assert data["passed_alone"] == [] and data["pre_existing"] == data["failing"]
    assert measured == [data["failing"]]
    assert len(argvs) == 2 and all(sum(len(a) for a in argv) < OVERFLOW for argv in argvs)


def test_a_spawn_oserror_is_a_fail_verdict_naming_the_error(repo, monkeypatch):
    def too_long(targets, cwd, remaining):
        raise OSError(206, "The filename or extension is too long")

    monkeypatch.setattr(tlr, "_pytest", too_long)
    data = tlr.run(["tests/test_a.py"], base_sha=None, head_sha=None, base_export_out=None, budget_s=60.0, cwd=str(repo))
    assert data["status_after"] == "fail" and data["over_budget"] is False and data["status"] == "error"
    assert data["failing_detail"] and "too long" in data["failing_detail"][-1]["message"]
    assert "OSError" in data["summary"] and data["zero_run_cause"]


def test_a_spawn_oserror_in_the_clone_leg_is_a_fail_verdict_too(repo, monkeypatch):
    def boom(*a, **k):
        raise OSError(206, "command line too long")

    monkeypatch.setattr(tlr, "_clone_leg", boom)
    data = tlr.run(["tests/test_a.py"], base_sha=None, head_sha=None, base_export_out=None, budget_s=60.0,
                   cwd=str(repo), touched=["pkg/mod.py"])
    assert data["status_after"] == "fail" and data["new_red"] == []
    assert "command line too long" in data["failing_detail"][-1]["message"]


def test_the_clone_leg_argv_carries_its_ids_in_a_file_under_the_run_holder(repo, tmp_path):
    ids = [f"tests/test_many.py::test_red_{i:04d}_{'x' * 40}" for i in range(500)]
    holder = tmp_path / "holder"
    bt = holder / "bt"
    bt.mkdir(parents=True)
    argv = tlr._argv(repo, repo, bt, ids)
    assert sum(len(a) for a in argv) < OVERFLOW and not set(ids) & set(argv)
    assert argv[:3] == [argv[0], "-c", tlr._IDS_RUNNER] and "--continue-on-collection-errors" in argv
    assert Path(argv[3]).parent == holder and Path(argv[3]).read_text(encoding="utf-8").split() == ids


BASE = "b" * 40
TRIPWIRE = "tests/test_tripwire.py"


def _tripwires(monkeypatch, files):
    monkeypatch.setattr(tlr.reverify_scope, "select_tests",
                        lambda root, base, head="HEAD": {"tests": [], "changed": [], "fully_mapped": True,
                                                         "notice": "", "tripwires": list(files)})


def _write_tripwire(repo: Path, body: str) -> None:
    (repo / TRIPWIRE).write_text(body, encoding="utf-8")


def test_a_tripwire_overrun_keeps_the_edited_file_results_with_no_budget_flag(repo, tmp_path, monkeypatch):
    _tripwires(monkeypatch, [TRIPWIRE])
    _write_tripwire(repo, "import time\n\ndef test_slow():\n    time.sleep(60)\n")
    monkeypatch.setattr(tlr, "TEST_LEG_MINUTES_BUDGET", 20 / 60)
    monkeypatch.setattr(tlr, "TRIPWIRE_BUDGET_S", 8.0)
    code, data = _main(["--base-sha", BASE, "--", PASS_ID, TRIPWIRE], tmp_path / "o.json")
    assert code == tlr.EXIT_OK and data["over_budget"] is False
    assert data["tests_run"] > 0 and data["status"] == "pass" and data["status_after"] == "pass"
    assert data[ltt.TRIPWIRES_NOT_RUN_KEY] == [TRIPWIRE] and data[ltt.IMPORTER_OVERRUN_KEY] is True
    assert data["failing"] == []


def test_a_phase_one_overrun_is_still_over_budget(repo, tmp_path, monkeypatch):
    _tripwires(monkeypatch, [TRIPWIRE])
    _write_tripwire(repo, "def test_t():\n    assert True\n")
    (repo / "tests" / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(tlr, "TRIPWIRE_BUDGET_S", 1.0)
    code, data = _main(["--base-sha", BASE, "--budget-s", "4", "--", "tests/test_slow.py", TRIPWIRE],
                       tmp_path / "o.json")
    assert code == tlr.EXIT_TIMEOUT and data["over_budget"] is True and data["tests_run"] == 0
    assert ltt.TRIPWIRES_NOT_RUN_KEY not in data


def test_a_completed_tripwire_phase_merges_counts_and_charges_its_reds(repo, tmp_path, monkeypatch):
    _tripwires(monkeypatch, [TRIPWIRE])
    _write_tripwire(repo, "def test_t():\n    assert False\n")
    tw_id = f"{TRIPWIRE}::test_t"
    measured = []

    def fake_measure(sha, head, ids, cwd, timeout_s=None):
        measured.append(list(ids))
        return {"red": list(ids), "export": "ok", "runner_exit": 1, "passed_at_base": []}

    monkeypatch.setattr(tlr.reverify_base_measure, "measure", fake_measure)
    out = tmp_path / "export.txt"
    _code, data = _main(["--base-sha", BASE, "--base-export-out", str(out), "--", PASS_ID, TRIPWIRE], tmp_path / "o.json")
    assert data["failing"] == [tw_id] and data["pre_existing"] == [tw_id] and measured == [[tw_id]]
    assert (data["tests_run"], data["tests_failed"]) == (2, 1)
    assert ltt.IMPORTER_OVERRUN_KEY not in data and ltt.TRIPWIRES_NOT_RUN_KEY not in data
    assert f"FAILED {tw_id}" in out.read_text(encoding="utf-8")


def test_a_tripwire_settle_overrun_keeps_the_red_charged(repo, tmp_path, monkeypatch):
    _tripwires(monkeypatch, [TRIPWIRE])
    _write_tripwire(repo, "def test_t():\n    assert False\n")
    monkeypatch.setattr(tlr.reverify_base_measure, "measure",
                        lambda *a, **k: {"red": [], "export": "timeout", "runner_exit": None, "passed_at_base": []})
    _code, data = _main(["--base-sha", BASE, "--", PASS_ID, TRIPWIRE], tmp_path / "o.json")
    assert data["failing"] == [f"{TRIPWIRE}::test_t"] and data["over_budget"] is False
    assert data[ltt.IMPORTER_OVERRUN_KEY] is True and data[ltt.TRIPWIRES_NOT_RUN_KEY] == [TRIPWIRE]
    assert data["status_after"] == "fail" and data["pre_existing"] == [] and data["tests_run"] == 2


def test_phase_one_gets_the_deadline_less_the_tripwire_reserve(repo, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(tlr.time, "monotonic", lambda: clock[0])
    _tripwires(monkeypatch, [TRIPWIRE])
    _write_tripwire(repo, "def test_t():\n    assert True\n")
    monkeypatch.setattr(tlr, "TRIPWIRE_BUDGET_S", 30.0)
    timeouts = []

    def fake_pytest(targets, cwd, remaining):
        timeouts.append(remaining)
        clock[0] += 10.0
        return 0, ["1 passed in 1s"]

    monkeypatch.setattr(tlr, "_pytest", fake_pytest)
    tlr.run([PASS_ID, TRIPWIRE], base_sha=BASE, head_sha=None, base_export_out=None, budget_s=100.0, cwd=str(repo))
    assert timeouts == [70.0, 90.0]


def test_a_budget_not_above_the_reserve_gives_phase_one_the_whole_deadline(repo, monkeypatch):
    _tripwires(monkeypatch, [TRIPWIRE])
    _write_tripwire(repo, "def test_t():\n    assert True\n")
    monkeypatch.setattr(tlr, "TRIPWIRE_BUDGET_S", 150.0)
    timeouts = []
    monkeypatch.setattr(tlr, "_pytest", lambda targets, cwd, remaining: (timeouts.append(remaining), (0, ["1 passed in 1s"]))[1])
    tlr.run([PASS_ID, TRIPWIRE], base_sha=BASE, head_sha=None, base_export_out=None, budget_s=100.0, cwd=str(repo))
    assert timeouts[0] > 99.0


@pytest.mark.parametrize("base_sha, scope", [(None, "fail"), (BASE, "raise")])
def test_no_base_or_an_unreadable_diff_stays_one_phase(repo, monkeypatch, base_sha, scope):
    _write_tripwire(repo, "def test_t():\n    assert True\n")

    def select(root, base, head="HEAD"):
        if scope == "fail":
            raise AssertionError("select_tests must not run without a base_sha")
        raise ValueError("unknown base")

    monkeypatch.setattr(tlr.reverify_scope, "select_tests", select)
    calls = []
    monkeypatch.setattr(tlr, "_pytest", lambda targets, cwd, remaining: (calls.append(list(targets)), (0, ["2 passed in 1s"]))[1])
    tlr.run([PASS_ID, TRIPWIRE], base_sha=base_sha, head_sha=None, base_export_out=None, budget_s=100.0, cwd=str(repo))
    assert calls == [[PASS_ID, TRIPWIRE]]


def test_a_selection_lacking_the_tripwires_key_runs_one_phase_over_every_target(repo, monkeypatch):
    _write_tripwire(repo, "def test_t():\n    assert True\n")
    monkeypatch.setattr(tlr.reverify_scope, "select_tests",
                        lambda root, base, head="HEAD": {"tests": [], "changed": [], "fully_mapped": True, "notice": ""})
    calls = []
    monkeypatch.setattr(tlr, "_pytest", lambda targets, cwd, remaining: (calls.append(list(targets)), (0, ["2 passed in 1s"]))[1])
    data = tlr.run([PASS_ID, TRIPWIRE], base_sha=BASE, head_sha=None, base_export_out=None, budget_s=100.0, cwd=str(repo))
    assert calls == [[PASS_ID, TRIPWIRE]] and data["status_after"] == "pass"


@pytest.mark.parametrize("selection_extra", [{}, {"tripwires": None}])
def test_a_selection_without_tripwires_still_runs_every_target_through_the_real_pytest(
        repo, tmp_path, monkeypatch, selection_extra):
    _write_tripwire(repo, "def test_t():\n    assert True\n")
    selection = {"tests": [PASS_ID, TRIPWIRE], "changed": [], "fully_mapped": True, "notice": "", **selection_extra}
    monkeypatch.setattr(tlr.reverify_scope, "select_tests", lambda root, base, head="HEAD": selection)
    code, data = _main(["--base-sha", BASE, "--", PASS_ID, TRIPWIRE], tmp_path / "o.json")
    assert code == tlr.EXIT_OK and data["tests_run"] == 2 and data["status_after"] == "pass"
    assert ltt.TRIPWIRES_NOT_RUN_KEY not in data


def _fake_pytest_then(monkeypatch, replies):
    """`_pytest` answering each call from `replies` in order; an exception instance is raised."""
    calls: list[list[str]] = []
    queue = list(replies)

    def fake(targets, cwd, remaining):
        calls.append(list(targets))
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return tuple(reply)

    monkeypatch.setattr(tlr, "_pytest", fake)
    return calls


def test_a_second_phase_timeout_keeps_the_phase_one_count_and_names_the_unreached_files(repo, monkeypatch):
    _tripwires(monkeypatch, [TRIPWIRE])
    _write_tripwire(repo, "def test_t():\n    assert True\n")
    calls = _fake_pytest_then(monkeypatch, [(0, ["3 passed in 1s"]), subprocess.TimeoutExpired("pytest", 1)])
    data = tlr.run([PASS_ID, TRIPWIRE], base_sha=BASE, head_sha=None, base_export_out=None, budget_s=100.0,
                   cwd=str(repo))
    assert calls == [[PASS_ID], [TRIPWIRE]]
    assert data["tests_run"] == 3 and ltt.ZERO_RUN_CAUSE_KEY not in data
    assert data[ltt.TRIPWIRES_NOT_RUN_KEY] == [TRIPWIRE] and data["over_budget"] is False


def test_a_deadline_after_phase_one_keeps_its_count_instead_of_zeroing_it(repo, monkeypatch):
    _tripwires(monkeypatch, [TRIPWIRE])
    _write_tripwire(repo, "def test_t():\n    assert True\n")
    red = f"FAILED {FAIL_ID} - assert 1 == 2"
    _fake_pytest_then(monkeypatch, [(1, [red, "1 passed, 1 failed in 1s"]), (1, [red, "1 failed in 1s"])])
    monkeypatch.setattr(tlr.reverify_base_measure, "measure",
                        lambda *a, **k: {"red": [], "export": "timeout", "runner_exit": None, "passed_at_base": []})
    data = tlr.run([PASS_ID, FAIL_ID, TRIPWIRE], base_sha=BASE, head_sha=None, base_export_out=None,
                   budget_s=100.0, cwd=str(repo))
    assert data["over_budget"] is True and data["tests_run"] == 2 and data["tests_failed"] == 1
    assert ltt.ZERO_RUN_CAUSE_KEY not in data
    assert data[ltt.TRIPWIRES_NOT_RUN_KEY] == [TRIPWIRE] and data[ltt.IMPORTER_OVERRUN_KEY] is True


def test_a_phase_one_deadline_with_no_completed_phase_still_reports_zero_tests(repo, monkeypatch):
    _tripwires(monkeypatch, [TRIPWIRE])
    _write_tripwire(repo, "def test_t():\n    assert True\n")
    _fake_pytest_then(monkeypatch, [subprocess.TimeoutExpired("pytest", 1)])
    data = tlr.run([PASS_ID, TRIPWIRE], base_sha=BASE, head_sha=None, base_export_out=None, budget_s=100.0,
                   cwd=str(repo))
    assert data["over_budget"] is True and data["tests_run"] == 0
    assert data[ltt.ZERO_RUN_CAUSE_KEY] == ltt.ZERO_RUN_BUDGET and ltt.TRIPWIRES_NOT_RUN_KEY not in data


def test_an_over_cap_importer_stays_in_phase_two_through_tripwires_not_selected(repo, monkeypatch):
    pool = [f"tests/test_imp_{i:02d}.py" for i in range(tlr.reverify_scope.TRIPWIRE_BOUND + 5)]
    cap = tlr.reverify_scope.TRIPWIRE_BOUND
    monkeypatch.setattr(tlr.reverify_scope, "select_tests",
                        lambda root, base, head="HEAD": {"tests": [], "tripwires": pool[:cap],
                                                         "tripwires_not_selected": pool[cap:]})
    first, second = tlr._split_phases([PASS_ID, *pool], BASE, None, str(repo))
    assert first == [PASS_ID] and second == pool
    monkeypatch.setattr(tlr.reverify_scope, "select_tests",
                        lambda root, base, head="HEAD": {"tests": [], "tripwires": pool[:cap]})
    first, second = tlr._split_phases([PASS_ID, *pool], BASE, None, str(repo))
    assert first == [PASS_ID, *pool[cap:]] and second == pool[:cap]

