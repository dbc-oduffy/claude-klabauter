"""retest_targeted writes a fresh verdict the landing gate accepts, and refuses to run with no test."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import retest_targeted
from coordinator_core.testing.git_write import write_seen_by_git

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run-abc"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


_REPO_TEMPLATE: list = []


@pytest.fixture
def repo(tmp_path, monkeypatch):
    import shutil
    import tempfile

    if not _REPO_TEMPLATE:
        tpl = Path(tempfile.mkdtemp(prefix="retest-tpl-")) / "repo"
        tpl.mkdir()
        _git(tpl, "init", "-q")
        _git(tpl, "config", "user.email", "t@example.invalid")
        _git(tpl, "config", "user.name", "t")
        (tpl / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
        _git(tpl, "add", "test_a.py")
        _git(tpl, "commit", "-q", "-m", "init")
        _REPO_TEMPLATE.append(tpl)
    root = tmp_path / "repo"
    shutil.copytree(_REPO_TEMPLATE[0], root)
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    return root


def _task_output(tmp_path: Path, baseline=None) -> Path:
    params = {
        "run_id": RUN_ID,
        "targeted_tests": {"status": "fail", "tests_failed": 1, "baseline": baseline},
    }
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    return out


def test_pass_writes_verdict_the_gate_accepts(repo, tmp_path):
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0
    assert seen[0][-1] == "test_a.py"
    record = li.fresh_targeted_verdict(repo, RUN_ID)
    assert record is not None and record["status"] == "pass"
    assert record["tests"] == ["test_a.py"]
    assert set(record["covered"]) == {"test_a.py"}
    assert li.landing_test_gate(repo, {"targeted_tests": {"status": "fail", "baseline": None}}, RUN_ID)[1] == "pass-on-retest"


def test_fail_writes_status_fail_and_exits_1(repo, tmp_path):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: 1,
    )
    assert rc == 1
    assert li.fresh_targeted_verdict(repo, RUN_ID)["status"] == "fail"


def test_caused_node_ids_name_the_tests(repo, tmp_path):
    out = _task_output(tmp_path, baseline={"caused": ["test_a.py::test_a"]})
    seen = []
    retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert seen and seen[0][-1] == "test_a.py"


def test_no_targeted_set_writes_stale_note(repo, tmp_path, capsys):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: pytest.fail("must not run"),
        spawn=lambda *a, **k: pytest.fail("must not spawn"),
    )
    assert rc == 0
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == "stale" and "stale" in record["note"]
    assert "stale" in capsys.readouterr().out


def test_pathless_failure_reruns_recorded_targeted_set(repo, tmp_path):
    out = _task_output(tmp_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    data["next_action"]["params"]["targeted_tests"]["tests"] = ["test_a.py::test_a"]
    out.write_text(json.dumps(data), encoding="utf-8")
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0 and seen[0][-1] == "test_a.py"
    assert li.fresh_targeted_verdict(repo, RUN_ID)["status"] == "pass"


@pytest.mark.parametrize("value", ["test_a.py test_b.py", "test_a.py,test_b.py", "test_a.py, test_b.py"])
def test_combined_tests_value_is_split_into_paths(repo, tmp_path, value):
    (repo / "test_b.py").write_text("def test_b():\n    pass\n", encoding="utf-8")
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", value, "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0 and seen[0][-2:] == ["test_a.py", "test_b.py"]
    assert li.fresh_targeted_verdict(repo, RUN_ID)["tests"] == ["test_a.py", "test_b.py"]


def test_missing_target_is_dropped_with_a_note_and_the_rest_runs(repo, tmp_path, capsys):
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py,nope.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0 and seen[0][-1] == "test_a.py" and "nope.py" not in seen[0]
    assert "nope.py" in capsys.readouterr().err
    record = li.fresh_targeted_verdict(repo, RUN_ID)
    assert record["status"] == "pass" and record["tests"] == ["test_a.py"]
    assert record["dropped"] == ["nope.py"] and "nope.py" in record["note"]


def test_all_targets_missing_records_no_tests_not_error(repo, tmp_path):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "nope.py", "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: pytest.fail("must not run"),
        spawn=lambda *a, **k: pytest.fail("must not spawn"),
    )
    assert rc == 0
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == "no-tests" and record["tests_run"] == 0
    assert record["dropped"] == ["nope.py"] and "nope.py" in record["note"]


def test_launcher_collecting_nothing_records_no_tests(repo, tmp_path):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: 5,
    )
    assert rc == 0
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == "no-tests" and record["tests_run"] == 0 and "test_a.py" in record["reason"]


def test_marker_filter_is_stripped_so_a_named_cadence_file_runs(repo, tmp_path, monkeypatch):
    (repo / "test_slow.py").write_text(
        "import pytest\n\n@pytest.mark.cadence\ndef test_slow():\n    pass\n", encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\nmarkers = cadence\n", encoding="utf-8")
    (Path(repo) / "coordinator.local.md").write_text(
        "---\nfast_test_cmd: \"python3 -m pytest -q -m 'not cadence'\"\n---\n", encoding="utf-8")
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_slow.py", "--repo-root", str(repo), "--detached-child"],
    )
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert rc == 0 and record["status"] == "pass"


def test_strip_drops_marker_filter_in_both_spellings(repo):
    argv = retest_targeted._strip_collection_roots(repo, "python -m pytest -m 'not cadence' -mfoo -q --maxprocesses=7")
    assert argv == ["python", "-m", "pytest", "-q", "--maxprocesses=7"]


def test_failed_record_carries_node_ids_from_the_log(repo, tmp_path):
    def run(argv, cwd, *, log=None, **_kw):
        log.write_text("FAILED test_a.py::test_a - assert 0\n", encoding="utf-8")
        return 1

    retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=run,
    )
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == "fail" and record[li.FAILED_TESTS_PARAM] == ["test_a.py::test_a"] and record["head"]


def test_dirty_paths_reads_untracked_and_modified(repo):
    (repo / "new.txt").write_text("x", encoding="utf-8")
    (repo / "test_a.py").write_text("changed\n", encoding="utf-8")
    assert sorted(retest_targeted.dirty_paths(repo)) == ["new.txt", "test_a.py"]


def test_covered_drops_unrelated_untracked_keeps_tracked_edits_and_new_modules(repo, tmp_path):
    (repo / ".salvage-staged-embed").mkdir()
    (repo / ".salvage-staged-embed" / "x.py").write_text("x", encoding="utf-8")
    (repo / "notes.txt").write_text("x", encoding="utf-8")
    (repo / "newmod.py").write_text("y = 1\n", encoding="utf-8")
    (repo / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: 0,
    )
    assert rc == 0
    record = li.fresh_targeted_verdict(repo, RUN_ID)
    assert set(record["covered"]) == {"test_a.py", "newmod.py"}


def test_covered_excludes_plan_evidence_sidecar_tracked_or_new(repo):
    (repo / "docs" / "plans").mkdir(parents=True)
    tracked = repo / "docs" / "plans" / "p.evidence.yaml"
    tracked.write_text("a: 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "e")
    write_seen_by_git(tracked, "a: 2\n")
    write_seen_by_git(repo / "docs" / "plans" / "q.evidence.yaml", "b: 1\n")
    write_seen_by_git(repo / "test_a.py", "changed\n")
    assert retest_targeted.covering_dirty_paths(repo) == ["test_a.py"]


def _declare(repo: Path, **keys: str) -> None:
    body = "".join(f"{k}: {v}\n" for k, v in keys.items())
    (repo / "coordinator.local.md").write_text(f"---\n{body}---\n", encoding="utf-8")


def test_declared_launcher_outranks_fast_test_cmd(repo, tmp_path):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run.py").write_text("", encoding="utf-8")
    _declare(repo, targeted_test_launcher="scripts/run.py --tier targeted", fast_test_cmd="scripts/tier.py --tier sufficient")
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0
    assert seen[0][1:] == ["scripts/run.py", "--tier", "targeted", "test_a.py"]
    assert "sufficient" not in " ".join(seen[0])


def test_nonzero_launcher_exit_other_than_test_failure_records_error_with_reason(repo, tmp_path):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: 126,
    )
    assert rc == 2
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == "error" and "126" in record["reason"]


def test_undeclared_launcher_file_records_error(repo, tmp_path):
    _declare(repo, targeted_test_launcher="scripts/missing.py")
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: pytest.fail("must not run"),
    )
    assert rc == 2
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == "error" and "missing.py" in record["reason"]


def test_node_id_passes_through_and_covered_names_the_file(repo, tmp_path):
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py::test_a", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0
    assert seen[0][-1] == "test_a.py::test_a"
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["covered"]["test_a.py"] != "absent"
    assert not any("::" in k for k in record["covered"])
    assert li.fresh_targeted_verdict(repo, RUN_ID) is not None


def test_gate_on_error_record_says_rerun_retest_not_failed_tests(repo, tmp_path):
    from coordinator_core.git.git_state import head_sha

    record = {"run_id": RUN_ID, "head": head_sha(repo), "tests": ["test_a.py"], "covered": {}, "status": "error",
              "reason": "launcher denied"}
    path = li.fresh_verdict_path(repo, RUN_ID)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    findings, check = li.landing_test_gate(repo, {"targeted_tests": {"status": "fail", "baseline": None}}, RUN_ID)
    assert check == "failed"
    assert "launcher denied" in findings[0].detail and "--retest-targeted" in findings[0].detail
    assert "pass params" not in findings[0].detail


def test_gate_fresh_fail_record_honours_failed_tests_param(repo, tmp_path):
    from coordinator_core.git.git_state import head_sha

    record = {"run_id": RUN_ID, "head": head_sha(repo), "tests": ["test_a.py"], "covered": {}, "status": "fail"}
    path = li.fresh_verdict_path(repo, RUN_ID)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    findings, check = li.landing_test_gate(repo, {"failed_tests": ["test_a.py::test_a"]}, RUN_ID)
    assert check == "pre-existing-only" or findings[0].path == "test_a.py"
    assert check != "failed" or "no baseline" not in findings[0].detail


@pytest.mark.parametrize(
    "script_path",
    ["scratch/warp/run-x/wf/run-x.workflow.js", "scratch\\warp\\run-x\\wf\\run-x.workflow.js"],
)
def test_run_id_from_script_stem_drops_workflow_suffix(script_path):
    assert retest_targeted._run_id_of({"script_path": script_path}) == "run-x"
    assert retest_targeted._run_id_of({"script_path": script_path, "run_id": "real"}) == "real"


def test_record_without_run_id_is_keyed_by_the_real_run_id(repo, tmp_path):
    params = {"script_path": "s/run-x.workflow.js", "targeted_tests": {"status": "fail", "baseline": None}}
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
    )
    assert rc == 0
    rec = json.loads(li.fresh_verdict_path(repo, "run-x").read_text(encoding="utf-8"))
    assert rec["run_id"] == "run-x"
    assert [p.name for p in li.fresh_verdict_path(repo, "run-x").parent.glob("*.json")] == ["run-x.json"]


@pytest.mark.parametrize("run_id", ["../escape", "a/b", ".."])
def test_unsafe_run_id_refuses_before_any_write(repo, tmp_path, run_id):
    params = {"run_id": run_id, "script_path": "x/y", "targeted_tests": {"status": "fail", "baseline": None}}
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: pytest.fail("must not run"),
    )
    assert rc == 2
    assert not (repo / li.RETEST_DIR).exists() and not (tmp_path / "escape.json").exists()


@pytest.mark.parametrize("test_path", ["../outside.py", "../../x.py::t"])
def test_test_path_escaping_the_repo_refuses_without_running(repo, tmp_path, test_path):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", test_path, "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: pytest.fail("must not run"),
    )
    assert rc == 2


def _launcher(repo: Path, body: str) -> None:
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "run.py").write_text(body, encoding="utf-8")
    _declare(repo, targeted_test_launcher="scripts/run.py")


def _real_args(tmp_path: Path, repo: Path) -> list[str]:
    return ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"]


def test_hung_launcher_ends_as_error_verdict_within_the_bound(repo, tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(retest_targeted, "RETEST_CHILD_TIMEOUT_SECS", 1.0)
    _launcher(repo, "import time\ntime.sleep(60)\n")
    start = time.monotonic()
    rc = retest_targeted.main(_real_args(tmp_path, repo))
    assert time.monotonic() - start < 20
    assert rc == 2
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == "error" and "launcher exceeded 1s" in record["reason"]


def test_launcher_that_reads_stdin_returns_at_once(repo, tmp_path):
    _launcher(repo, "import sys\nsys.stdin.read()\n")
    assert retest_targeted.main(_real_args(tmp_path, repo)) == 0


def test_verdict_log_holds_the_childs_output(repo, tmp_path):
    _launcher(repo, "import sys\nprint('out-marker')\nprint('err-marker', file=sys.stderr)\n")
    assert retest_targeted.main(_real_args(tmp_path, repo)) == 0
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert Path(record["log"]) == li.fresh_verdict_path(repo, RUN_ID).with_suffix(".log")
    text = Path(record["log"]).read_text(encoding="utf-8")
    assert "out-marker" in text and "err-marker" in text


def test_fast_test_cmd_roots_are_dropped_and_only_targets_run(repo, tmp_path, monkeypatch):
    for d in ("roots_a", "roots_b"):
        (repo / d).mkdir()
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "python -m pytest -q roots_a roots_b")
    seen = []
    rc = retest_targeted.main(_real_args(tmp_path, repo), run=lambda argv, cwd, **_kw: seen.append(argv) or 0)
    assert rc == 0
    assert seen[0][1:] == ["-m", "pytest", "-q", "test_a.py"]


def _two_roots(repo: Path, targets: str) -> list[str]:
    for d, name in (("rootx", "X"), ("rooty", "Y")):
        (repo / d).mkdir()
        (repo / d / "conftest.py").write_text(f"class Fake{name}:\n    pass\n", encoding="utf-8")
        (repo / d / f"test_{d}.py").write_text(
            f"from conftest import Fake{name}\n\ndef test_it():\n    assert Fake{name}\n", encoding="utf-8")
    return ["--tests", targets]


def _run_two_roots(repo, tmp_path):
    args = _two_roots(repo, "rootx/test_rootx.py,rooty/test_rooty.py")
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), *args, "--repo-root", str(repo), "--detached-child"])
    return rc, json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))


def test_two_test_roots_run_in_separate_sessions_and_pass(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "python -m pytest -q -p no:cacheprovider")
    rc, record = _run_two_roots(repo, tmp_path)
    assert rc == 0 and record["status"] == "pass", record


def test_runner_tier_option_is_dropped_so_no_default_collection_is_added(repo, tmp_path, monkeypatch):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run-tests.py").write_text(
        "import sys, pytest\n"
        "a = sys.argv[1:]\n"
        "if '--tier' in a:\n"
        "    i = a.index('--tier'); del a[i:i + 2]; a.append('tests')\n"
        "sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', *a]))\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "conftest.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_default.py").write_text("def test_default():\n    assert False\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "python scripts/run-tests.py --tier fast")
    rc, record = _run_two_roots(repo, tmp_path)
    assert rc == 0 and record["status"] == "pass", record


def test_scopes_split_one_command_per_conftest_root(repo):
    _two_roots(repo, "")
    _base, groups = retest_targeted._launch_plan(repo, ["rootx/test_rootx.py", "rooty/test_rooty.py", "test_a.py"])
    assert [g[-1] for g in groups] == ["rootx/test_rootx.py", "rooty/test_rooty.py", "test_a.py"]


def test_runner_script_survives_root_stripping(repo):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run-tests.py").write_text("", encoding="utf-8")
    argv = retest_targeted._strip_collection_roots(repo, "python3 scripts/run-tests.py --tier fast")
    assert argv == ["python3", "scripts/run-tests.py", "--tier", "fast"]
    (repo / "roots_a").mkdir()
    assert retest_targeted._strip_collection_roots(repo, "pytest roots_a -q") == ["pytest", "-q"]


def test_option_values_survive_root_stripping(repo):
    (repo / "roots_a").mkdir()
    (repo / "setup.cfg").write_text("", encoding="utf-8")
    argv = retest_targeted._strip_collection_roots(
        repo, "python -m pytest --rootdir . -c setup.cfg roots_a"
    )
    assert argv == ["python", "-m", "pytest", "--rootdir", ".", "-c", "setup.cfg"]


def _warm_args(tmp_path: Path, repo: Path) -> list[str]:
    return _real_args(tmp_path, repo)[:-1]


def _no_launch(monkeypatch):
    monkeypatch.setattr(retest_targeted, "_launch_plan", lambda *a, **k: pytest.fail("warm path must not launch"))


def test_warm_entry_records_running_and_spawns_without_launching(repo, tmp_path, monkeypatch):
    import time

    from coordinator_core.ops import invoke_from_argv

    _no_launch(monkeypatch)
    spawned = []
    ran = []
    rc = retest_targeted.main(
        _warm_args(tmp_path, repo),
        run=lambda *a, **k: ran.append(a),
        spawn=lambda *a, **k: spawned.append(a) or True,
    )
    assert rc == 0 and not ran
    argv = spawned[0]
    assert argv[1] == str(Path(retest_targeted.__file__).resolve())
    assert "--detached-child" in argv[2] and argv[2][argv[2].index("--repo-root") + 1] == str(repo)
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == li.RETEST_RUNNING and record["tests"] == ["test_a.py"]
    assert record["deadline"] > record["at"] and record["log"].endswith(f"{RUN_ID}.log")

    li.fresh_verdict_path(repo, RUN_ID).unlink()
    monkeypatch.setattr(retest_targeted, "spawn_detached", lambda *a, **k: True)
    before = time.process_time()
    result = invoke_from_argv._run_entrypoint(
        "emit-dispatch-workflow", ["--retest-targeted", *_warm_args(tmp_path, repo)], str(repo),
    )
    assert time.process_time() - before < 0.5
    assert result["exit_code"] == 0
    assert json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))["status"] == "running"


def test_failed_spawn_overwrites_running_with_error(repo, tmp_path, monkeypatch):
    _no_launch(monkeypatch)
    rc = retest_targeted.main(_warm_args(tmp_path, repo), spawn=lambda *a, **k: False)
    assert rc == 2
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == "error" and "failed to spawn" in record["reason"]


def test_second_call_while_running_exits_3_without_spawning(repo, tmp_path, monkeypatch):
    _no_launch(monkeypatch)
    assert retest_targeted.main(_warm_args(tmp_path, repo), spawn=lambda *a, **k: True) == 0
    rc = retest_targeted.main(_warm_args(tmp_path, repo), spawn=lambda *a, **k: pytest.fail("must not spawn"))
    assert rc == 3


def test_second_call_past_deadline_respawns(repo, tmp_path, monkeypatch):
    _no_launch(monkeypatch)
    assert retest_targeted.main(_warm_args(tmp_path, repo), spawn=lambda *a, **k: True) == 0
    path = li.fresh_verdict_path(repo, RUN_ID)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["deadline"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(record), encoding="utf-8")
    spawned = []
    assert retest_targeted.main(_warm_args(tmp_path, repo), spawn=lambda *a, **k: spawned.append(a) or True) == 0
    assert spawned


def _params_file(repo: Path, **extra) -> Path:
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import params_path

    path = params_path(repo, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": RUN_ID, "run_token": "t", "script_path": "s.mjs", **extra}), encoding="utf-8")
    return path


def test_params_file_input_writes_the_verdict_the_gate_accepts(repo):
    seen = []
    rc = retest_targeted.main(
        ["--params-file", str(_params_file(repo)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0 and seen[0][-1] == "test_a.py"
    assert li.fresh_targeted_verdict(repo, RUN_ID)["status"] == "pass"


def test_params_file_naming_a_task_output_expands_it_and_the_file_keys_win(repo, tmp_path):
    out = _task_output(tmp_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    data["next_action"]["params"]["targeted_tests"]["tests"] = ["test_a.py::test_a"]
    out.write_text(json.dumps(data), encoding="utf-8")
    seen = []
    rc = retest_targeted.main(
        ["--params-file", str(_params_file(repo, task_output_path=str(out), run_id="other-run")), "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0 and seen[0][-1] == "test_a.py"
    assert li.fresh_verdict_path(repo, "other-run").exists() and not li.fresh_verdict_path(repo, RUN_ID).exists()


def test_absent_params_file_exits_2_naming_its_writer_and_writes_no_record(repo, capsys):
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import params_path

    missing = params_path(repo, RUN_ID)
    rc = retest_targeted.main(
        ["--params-file", str(missing), "--tests", "test_a.py", "--repo-root", str(repo)],
        spawn=lambda *a, **k: pytest.fail("must not spawn"),
    )
    err = capsys.readouterr().err.strip()
    assert rc == 2 and "was never written" in err and len(err.splitlines()) == 1
    assert not li.fresh_verdict_path(repo, RUN_ID).exists()


def test_unreadable_params_file_exits_2_without_a_traceback(repo, tmp_path, capsys):
    bad = tmp_path / "params.json"
    bad.write_text("[1]", encoding="utf-8")
    rc = retest_targeted.main(["--params-file", str(bad), "--repo-root", str(repo)])
    assert rc == 2 and "does not hold a JSON object" in capsys.readouterr().err
    bad.write_text("{nope", encoding="utf-8")
    assert retest_targeted.main(["--params-file", str(bad), "--repo-root", str(repo)]) == 2


def test_both_inputs_or_neither_is_a_usage_error(repo, tmp_path):
    for argv in (["--from-task-output", str(_task_output(tmp_path)), "--params-file", str(_params_file(repo))], []):
        with pytest.raises(SystemExit) as exc:
            retest_targeted.main(argv)
        assert exc.value.code == 2


def test_the_poll_line_and_child_argv_carry_the_params_file_flag(repo, capsys):
    params = _params_file(repo)
    spawned = []
    args = ["--params-file", str(params), "--tests", "test_a.py", "--repo-root", str(repo)]
    assert retest_targeted.main(args, spawn=lambda *a, **k: spawned.append(a) or True) == 0
    assert f"--poll --params-file {params}" in capsys.readouterr().out.replace("\\\\", "\\")
    child = spawned[0][2]
    assert child[child.index("--params-file") + 1] == str(params.resolve()) and "--from-task-output" not in child
    assert retest_targeted.main([*args, "--poll"]) == 3


def _poll_exit(repo, tmp_path, record):
    path = li.fresh_verdict_path(repo, RUN_ID)
    if record is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
    before = path.read_bytes() if path.exists() else None
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--repo-root", str(repo), "--poll"],
        spawn=lambda *a, **k: pytest.fail("poll must not spawn"),
    )
    assert (path.read_bytes() if path.exists() else None) == before
    return rc


def test_poll_exit_codes(repo, tmp_path, capsys):
    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert _poll_exit(repo, tmp_path, None) == 2
    assert _poll_exit(repo, tmp_path, {"status": "running", "deadline": future}) == 3
    assert _poll_exit(repo, tmp_path, {"status": "running", "deadline": past}) == 2
    assert "abandoned past deadline" in capsys.readouterr().out
    assert _poll_exit(repo, tmp_path, {"status": "pass"}) == 0
    assert _poll_exit(repo, tmp_path, {"status": "fail"}) == 1
    assert _poll_exit(repo, tmp_path, {"status": "error", "reason": "x"}) == 2
    assert _poll_exit(repo, tmp_path, {"status": "stale"}) == 2


@pytest.mark.parametrize("cmd, expected", [
    ("py -3 scripts/run-tests.py --tier fast", ["py", "-3", "scripts/run-tests.py", "--tier", "fast"]),
    ("uv run scripts/x.py -q", ["uv", "run", "scripts/x.py", "-q"]),
    ("pytest.exe -q roots_a", ["pytest.exe", "-q"]),
    ("py.test.exe -m 'not cadence' roots_a", ["py.test.exe"]),
    ("python -m pytest -q roots_a", ["python", "-m", "pytest", "-q"]),
    ("uv run pytest roots_a -q", ["uv", "run", "pytest", "-q"]),
])
def test_launcher_shapes_keep_the_runner_script_and_treat_pytest_exe_as_pytest(repo, cmd, expected):
    (repo / "scripts").mkdir()
    for name in ("run-tests.py", "x.py"):
        (repo / "scripts" / name).write_text("", encoding="utf-8")
    (repo / "roots_a").mkdir()
    assert retest_targeted._strip_collection_roots(repo, cmd) == expected


def _two_conftest_launcher(repo: Path):
    _two_roots(repo, "")
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "run.py").write_text("", encoding="utf-8")
    _declare(repo, targeted_test_launcher="scripts/run.py")


def test_declared_launcher_runs_once_with_all_ids_when_scopes_do_not_conflict(repo):
    (repo / "sub").mkdir()
    (repo / "sub" / "test_s.py").write_text("def test_s():\n    pass\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run.py").write_text("", encoding="utf-8")
    _declare(repo, targeted_test_launcher="scripts/run.py")
    _base, groups = retest_targeted._launch_plan(repo, ["test_a.py", "sub/test_s.py"])
    assert groups == [["test_a.py", "sub/test_s.py"]]


def test_declared_launcher_splits_only_when_conftests_collide(repo):
    _two_conftest_launcher(repo)
    _base, groups = retest_targeted._launch_plan(repo, ["rootx/test_rootx.py", "rooty/test_rooty.py"])
    assert [g[-1] for g in groups] == ["rootx/test_rootx.py", "rooty/test_rooty.py"]


def _one_conftest_sub(repo: Path) -> None:
    (repo / "sub").mkdir()
    (repo / "sub" / "conftest.py").write_text("", encoding="utf-8")
    (repo / "sub" / "test_s.py").write_text("def test_s():\n    pass\n", encoding="utf-8")


def _main_calls(repo, tmp_path, tests):
    calls = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", tests, "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: calls.append(argv) or 0)
    return rc, calls


def test_main_runs_one_session_when_scopes_do_not_conflict(repo, tmp_path):
    _one_conftest_sub(repo)
    rc, calls = _main_calls(repo, tmp_path, "test_a.py,sub/test_s.py")
    assert rc == 0 and len(calls) == 1
    assert calls[0][-2:] == ["test_a.py", "sub/test_s.py"]


def test_main_declared_launcher_runs_one_session_when_scopes_do_not_conflict(repo, tmp_path):
    _one_conftest_sub(repo)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run.py").write_text("", encoding="utf-8")
    _declare(repo, targeted_test_launcher="scripts/run.py")
    rc, calls = _main_calls(repo, tmp_path, "test_a.py,sub/test_s.py")
    assert rc == 0 and len(calls) == 1
    assert calls[0][-2:] == ["test_a.py", "sub/test_s.py"]


def test_main_conflicting_scopes_run_one_session_per_scope(repo, tmp_path):
    _two_roots(repo, "")
    rc, calls = _main_calls(repo, tmp_path, "rootx/test_rootx.py,rooty/test_rooty.py,test_a.py")
    assert rc == 0 and len(calls) == 3
    assert [c[-1] for c in calls] == ["rootx/test_rootx.py", "rooty/test_rooty.py", "test_a.py"]


@pytest.mark.parametrize("conflicting", [False, True])
def test_launch_plan_fast_cmd_keep_tier_changes_base_only(repo, monkeypatch, conflicting):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "python scripts/run-tests.py --tier fast")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run-tests.py").write_text("", encoding="utf-8")
    if conflicting:
        _two_roots(repo, "")
        tests = ["rootx/test_rootx.py", "rooty/test_rooty.py", "test_a.py"]
    else:
        _one_conftest_sub(repo)
        tests = ["test_a.py", "sub/test_s.py"]
    dropped = retest_targeted._launch_plan(repo, tests)
    kept = retest_targeted._launch_plan(repo, tests, keep_tier=True)
    assert "--tier" in kept[0] and "--tier" not in dropped[0]
    assert kept[1] == dropped[1]
    assert len(dropped[1]) == (3 if conflicting else 1)


def test_launch_plan_unresolved_declared_launcher_returns_reason(repo):
    _declare(repo, targeted_test_launcher="scripts/missing.py")
    assert isinstance(retest_targeted._launch_plan(repo, ["test_a.py"]), str)


_TIER_CMD = "python scripts/run-tests.py --tier fast"


def _tier_runner(repo, monkeypatch, tmp_path, codes):
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "run-tests.py").write_text("", encoding="utf-8")
    keep_calls = []
    real = retest_targeted._launch_plan

    def spy(root, tests, *, keep_tier=False):
        if keep_tier:
            keep_calls.append(tests)
        return real(root, tests, keep_tier=keep_tier)

    monkeypatch.setattr(retest_targeted, "_launch_plan", spy)
    rc, calls, rec = _fake_runner(repo, monkeypatch, tmp_path, codes, cmd=_TIER_CMD)
    return rc, calls, rec, keep_calls


def test_multi_group_rc2_retries_only_that_group_with_tier_kept(repo, tmp_path, monkeypatch):
    rc, calls, rec, keep_calls = _tier_runner(repo, monkeypatch, tmp_path, [0, 2, 0, 0])
    assert rc == 0 and rec["status"] == "pass" and len(calls) == 4
    argvs = [c[0] for c in calls]
    assert [("--tier" in a) for a in argvs] == [False, False, True, False]
    assert argvs[2][-1] == "rooty/test_rooty.py" and calls[2][2] is True
    assert argvs[1][-1] == "rooty/test_rooty.py" and argvs[3][-1] == "test_a.py"
    assert len(keep_calls) == 1


def test_two_rc2_groups_plan_keep_tier_once(repo, tmp_path, monkeypatch):
    rc, calls, rec, keep_calls = _tier_runner(repo, monkeypatch, tmp_path, [2, 0, 2, 0, 0])
    assert rc == 0 and len(calls) == 5
    assert len(keep_calls) == 1


def _fake_runner(repo, monkeypatch, tmp_path, codes, tests="rootx/test_rootx.py,rooty/test_rooty.py,test_a.py",
                 cmd="python -m pytest -q"):
    _two_roots(repo, "")
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", cmd)
    calls = []

    def run(argv, cwd, *, log=None, timeout=None, append=False):
        n = len(calls)
        calls.append((argv, timeout, append))
        if codes[n] == 1:
            with open(log, "ab" if append else "wb") as fh:
                fh.write(f"FAILED {argv[-1]}::t - x\n".encode())
        if codes[n] == "collect-error":
            with open(log, "ab" if append else "wb") as fh:
                fh.write(b"ERROR collecting test_a.py\nInterrupted: 1 error during collection\n")
            return 2
        if codes[n] == "raise":
            raise OSError("boom")
        return codes[n]

    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", tests, "--repo-root", str(repo), "--detached-child"],
        run=run)
    return rc, calls, json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))


def test_aggregate_fail_then_pass_is_fail(repo, tmp_path, monkeypatch):
    rc, calls, rec = _fake_runner(repo, monkeypatch, tmp_path, [1, 0, 0])
    assert rc == 1 and rec["status"] == "fail" and len(calls) == 3
    assert rec[li.FAILED_TESTS_PARAM] == ["rootx/test_rootx.py::t"]


def test_aggregate_fail_then_crash_keeps_failed_ids_and_names_both(repo, tmp_path, monkeypatch):
    rc, calls, rec = _fake_runner(repo, monkeypatch, tmp_path, [1, 126, 0])
    assert rc == 2 and rec["status"] == "error"
    assert rec[li.FAILED_TESTS_PARAM] == ["rootx/test_rootx.py::t"]
    assert "126" in rec["reason"] and "rooty/test_rooty.py" in rec["reason"]
    assert "failing session(s)" in rec["reason"] and "rootx/test_rootx.py" in rec["reason"]


def test_aggregate_no_tests_then_pass_is_pass(repo, tmp_path, monkeypatch):
    rc, _calls, rec = _fake_runner(repo, monkeypatch, tmp_path, [5, 0, 5])
    assert rc == 0 and rec["status"] == "pass"


def test_aggregate_all_no_tests_is_no_tests(repo, tmp_path, monkeypatch):
    rc, _calls, rec = _fake_runner(repo, monkeypatch, tmp_path, [5, 5, 5])
    assert rc == 0 and rec["status"] == "no-tests" and rec["tests_run"] == 0


def test_oserror_mid_sequence_is_error_naming_the_session(repo, tmp_path, monkeypatch):
    rc, _calls, rec = _fake_runner(repo, monkeypatch, tmp_path, [0, "raise", 0])
    assert rc == 2 and rec["status"] == "error" and "boom" in rec["reason"]


def test_spent_deadline_launches_nothing_and_states_the_budget(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(retest_targeted, "RETEST_CHILD_TIMEOUT_SECS", 0.0)
    rc, calls, rec = _fake_runner(repo, monkeypatch, tmp_path, [0, 0, 0])
    assert rc == 2 and not calls and rec["status"] == "error"
    assert "not started" in rec["reason"] and "0s budget" in rec["reason"]


def test_timeout_mid_sequence_skips_later_sessions(repo, tmp_path, monkeypatch):
    rc, calls, rec = _fake_runner(repo, monkeypatch, tmp_path, [retest_targeted.LAUNCH_TIMED_OUT, 0, 0])
    assert rc == 2 and len(calls) == 1 and "exceeded 1800s" in rec["reason"]
    assert all(t <= retest_targeted.RETEST_CHILD_TIMEOUT_SECS for _a, t, _p in calls)


def test_tier_runner_that_needs_tier_falls_back_to_it(repo, tmp_path, monkeypatch):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run-tests.py").write_text("", encoding="utf-8")
    rc, calls, rec = _fake_runner(
        repo, monkeypatch, tmp_path, [2, 0], tests="test_a.py",
        cmd="python scripts/run-tests.py --tier fast")
    assert len(calls) == 2 and "--tier" not in calls[0][0]
    assert calls[1][0][1:5] == ["scripts/run-tests.py", "--tier", "fast", "test_a.py"] and calls[1][2] is True
    assert rc == 0 and rec["status"] == "pass"


def test_tier_is_not_retried_when_the_cmd_has_none(repo, tmp_path, monkeypatch):
    rc, calls, rec = _fake_runner(repo, monkeypatch, tmp_path, [2], tests="test_a.py")
    assert len(calls) == 1 and rc == 2 and rec["status"] == "error"


def test_tier_is_not_retried_after_a_pytest_collection_error(repo, tmp_path, monkeypatch):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run-tests.py").write_text("", encoding="utf-8")
    rc, calls, rec = _fake_runner(
        repo, monkeypatch, tmp_path, ["collect-error", 0], tests="test_a.py",
        cmd="python scripts/run-tests.py --tier fast")
    assert len(calls) == 1 and rc == 2 and rec["status"] == "error"


def test_targeted_set_is_shared_with_the_landing_gate():
    params = {"targeted_tests": {"status": "fail", "baseline": {"caused": ["a/test_x.py::t1"]}},
              "targeted_test_paths": ["b/test_y.py"]}
    assert retest_targeted._tests_to_run([], params) == li.targeted_set(params, li.targeted_test_verdict(params))
    assert retest_targeted._tests_to_run([], params) == ["a/test_x.py"]


def test_errored_retest_with_failed_tests_and_no_tests_run_keeps_the_ids(repo, tmp_path, monkeypatch):
    rc, _calls, rec = _fake_runner(repo, monkeypatch, tmp_path, [1, 126, 0])
    assert rec["status"] == "error" and "tests_run" not in rec and rec[li.FAILED_TESTS_PARAM]


def test_covered_is_the_run_manifest_writes_not_foreign_dirt(repo, tmp_path):
    (repo / "scratch" / "warp" / "run-abc").mkdir(parents=True)
    (repo / "scratch" / "warp" / "run-abc" / "manifest.json").write_text(
        json.dumps({"rows": [{"writes": ["mine.py"]}]}), encoding="utf-8")
    (repo / "scratch" / "warp" / "run-abc" / "run.workflow.js").write_text(
        "// coordinator:ask-run-manifest v1 scratch/warp/run-abc/manifest.json\n", encoding="utf-8")
    (repo / "mine.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "foreign.py").write_text("y = 1\n", encoding="utf-8")
    _git(repo, "add", "foreign.py")
    params = {"run_id": RUN_ID, "script_path": "scratch/warp/run-abc/run.workflow.js",
              "targeted_tests": {"status": "fail", "baseline": None}}
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: 0,
        dirty=lambda r: ["foreign.py", "mine.py"],
    )
    assert rc == 0
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert set(record["covered"]) == {"test_a.py", "mine.py"}


_STATE_INVARIANT = "coordinator_core/session/tests/test_machinery_paths.py"
_STATE_NODE = _STATE_INVARIANT + "::test_every_tracked_state_first_segment_is_on_the_allowlist"


def _seed_state_invariant_and_run_artifacts(repo: Path) -> None:
    from coordinator_core.ops.dispatch_emit import ask_contract

    target = repo / _STATE_INVARIANT
    target.parent.mkdir(parents=True)
    target.write_text("def test_every_tracked_state_first_segment_is_on_the_allowlist():\n    pass\n", encoding="utf-8")
    ask_contract.write_run_stage(repo, RUN_ID, "staged")


def test_run_artifacts_owe_no_state_invariant_but_a_chunk_written_state_path_does(repo, tmp_path):
    _seed_state_invariant_and_run_artifacts(repo)
    assert retest_targeted._with_invariants(repo, ["test_a.py"], lambda _root: [], RUN_ID, {}) == ["test_a.py"]
    dirty = lambda _root: ["state/lessons/x.md"]
    assert retest_targeted._with_invariants(repo, ["test_a.py"], dirty, RUN_ID, {}) == ["test_a.py", _STATE_NODE]


def test_without_a_run_the_clean_tree_owes_no_invariant(repo):
    _seed_state_invariant_and_run_artifacts(repo)
    assert retest_targeted._with_invariants(repo, ["test_a.py"], lambda _root: []) == ["test_a.py"]


def test_retest_runs_the_state_invariant_and_the_gate_credits_it(repo, tmp_path):
    _seed_state_invariant_and_run_artifacts(repo)
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo), "--detached-child"],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
        dirty=lambda _root: ["state/lessons/x.md"],
    )
    assert rc == 0
    assert any(_STATE_INVARIANT in str(arg) for arg in seen[0])
    found = li.invariant_findings(repo, ["state/lessons/x.md", "state/warp/%s/run-stage.json" % RUN_ID], RUN_ID, {})
    assert not found
def test_many_node_ids_split_into_bounded_sessions_in_order(repo):
    tests = [f"test_a.py::t{i}" for i in range(retest_targeted.RETEST_MAX_NODES * 2 + 1)]
    _base, groups = retest_targeted._launch_plan(repo, tests)
    assert [len(g) for g in groups] == [retest_targeted.RETEST_MAX_NODES] * 2 + [1]
    assert [t for g in groups for t in g] == tests


def test_node_count_at_the_bound_stays_one_session(repo):
    tests = [f"test_a.py::t{i}" for i in range(retest_targeted.RETEST_MAX_NODES)]
    assert len(retest_targeted._launch_plan(repo, tests)[1]) == 1


@pytest.mark.parametrize("tokens,expected", [
    (["pytest", "-n", "auto", "--maxprocesses=7", "-q"], ["pytest", "-n", "2", "-q"]),
    (["pytest", "-n", "8", "--maxprocesses", "7"], ["pytest", "-n", "2"]),
    (["pytest", "-n1"], ["pytest", "-n1"]),
    (["pytest", "--numprocesses=auto"], ["pytest", "--numprocesses=2"]),
    (["pytest", "-q"], ["pytest", "-q"]),
])
def test_cap_workers_clamps_the_worker_count(tokens, expected):
    assert retest_targeted._cap_workers(tokens, 2) == expected


def test_launch_plan_base_never_exceeds_the_worker_cap(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -n auto --maxprocesses=7")
    base, _groups = retest_targeted._launch_plan(repo, ["test_a.py"])
    assert base[base.index("-n") + 1] == str(retest_targeted.worker_cap())
    assert not any(t.startswith("--maxprocesses") for t in base)
    assert 1 <= retest_targeted.worker_cap() <= retest_targeted.RETEST_MAX_WORKERS


def test_declared_launcher_worker_count_is_clamped(repo):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run.py").write_text("", encoding="utf-8")
    _declare(repo, targeted_test_launcher="scripts/run.py -n 99 --maxprocesses=50")
    base, _groups = retest_targeted._launch_plan(repo, ["test_a.py"])
    assert base[base.index("-n") + 1] == str(retest_targeted.worker_cap())
    assert not any(t.startswith("--maxprocesses") for t in base)


def test_main_launches_one_child_per_node_chunk(repo, tmp_path, monkeypatch):
    real = retest_targeted._chunks
    monkeypatch.setattr(retest_targeted, "_chunks", lambda g, size=2: real(g, 2))
    calls = []

    def run(argv, cwd, **kw):
        calls.append(argv)
        return 0

    out = _task_output(tmp_path)
    ids = [f"test_a.py::t{i}" for i in range(5)]
    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(repo), "--detached-child",
         *[x for t in ids for x in ("--tests", t)]], run=run, dirty=lambda r: [])
    assert rc == 0 and [len([a for a in c if "::" in a]) for c in calls] == [2, 2, 1]


INVARIANT = "test_a.py::test_every_tracked_segment_is_on_the_allowlist"


def test_run_uncaused_invariant_red_in_a_file_with_a_run_new_id_lands_pre_existing(tmp_path, monkeypatch):
    """The export is red on an invariant also red in the HEAD-only export; the run targets its own new
    id in that file (absent at HEAD). The HEAD pass runs the whole file, so caused == [] and the gate
    reads pre-existing-only. A red new id still lands in caused."""
    from coordinator_core.ops.dispatch_emit import landing_export as le
    from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    export_root = tmp_path / "export"
    export_root.mkdir()
    export_log = tmp_path / "export.log"
    new_id = "test_a.py::test_new_in_this_run"
    export_log.write_text(f"FAILED {INVARIANT} - boom\n", encoding="utf-8")

    def build(worktree, out_dir, paths):
        Path(out_dir).mkdir(parents=True)
        (Path(out_dir) / "test_a.py").write_text("", encoding="utf-8")
        return le.LandingExport(Path(out_dir).as_posix(), "h" * 40, (), ())

    monkeypatch.setattr(le, "build_head_export", build)
    seen: list[list[str]] = []

    def run(argv, cwd, *, log=None, timeout=None, append=False):
        seen.append(list(argv))
        if "--collect-only" in argv:
            if any("::" in a for a in argv):
                return 4
            body = f"{INVARIANT}\ntest_a.py::test_other\n"
            rc = 0
        else:
            body = f"FAILED {INVARIANT} - boom\n1 passed, 1 failed in 0.1s\n"
            rc = 1
        with open(log, "ab" if append else "wb") as sink:
            sink.write(body.encode())
        return rc

    got = retest_targeted._head_baseline(
        tmp_path, run, [new_id, INVARIANT], export_root, export_log, tmp_path / "head.log",
        float("inf"),
    )
    baseline = got["baseline"]
    assert baseline["caused"] == [] and baseline["foreign"] == [INVARIANT]
    assert not any("::" in a for argv in seen for a in argv)
    findings, check = ltt.failed_targeted_tests({"status": "fail", "baseline": baseline})
    assert findings == [] and check == "pre-existing-only"

    export_log.write_text(f"FAILED {INVARIANT} - boom\nFAILED {new_id} - boom\n", encoding="utf-8")
    again = retest_targeted._head_baseline(
        tmp_path, run, [new_id, INVARIANT], export_root, export_log, tmp_path / "head.log",
        float("inf"),
    )["baseline"]
    assert again["caused"] == [new_id] and again["foreign"] == [INVARIANT]
    assert ltt.failed_targeted_tests({"status": "fail", "baseline": again})[1] == "failed"


@pytest.mark.parametrize("timeout, expected", [(30, 0), (0.05, retest_targeted.LAUNCH_TIMED_OUT)])
def test_run_argv_frees_its_admission_reservation_on_exit_and_on_timeout(tmp_path, monkeypatch, timeout, expected):
    import sys

    from coordinator_core.bash_guards import _heavy_command_class as hcc

    released = []
    monkeypatch.setattr(hcc, "admit_before_spawn", lambda *a, **k: hcc.SpawnAdmission(True, "", "k1", 1.0))
    monkeypatch.setattr(hcc, "register_spawn", lambda *a, **k: None)
    monkeypatch.setattr(hcc, "release_spawn", lambda key, pid, host=None: released.append(key))
    code = "pass" if expected == 0 else "import time; time.sleep(30)"

    assert retest_targeted._run_argv([sys.executable, "-c", code], tmp_path, timeout=timeout) == expected
    assert released == ["k1"]


def test_release_spawn_frees_the_pair_and_never_raises():
    from coordinator_core.bash_guards import _heavy_command_class as hcc

    calls = []

    class Host:
        def admission_file(self):
            return "adm"

        def release_command(self, path, key, pid):
            calls.append((path, key, pid))

    hcc.release_spawn("k", 7, Host())
    assert calls == [("adm", "k", 7)]

    class Broken(Host):
        def release_command(self, *a):
            raise RuntimeError("boom")

    hcc.release_spawn("k", 7, Broken())
