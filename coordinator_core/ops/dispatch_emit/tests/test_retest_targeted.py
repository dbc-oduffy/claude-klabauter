"""retest_targeted writes a fresh verdict the landing gate accepts, and refuses to run with no test."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import retest_targeted

pytestmark = pytest.mark.spawns_process

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
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0
    assert seen[0][-1] == "test_a.py"
    record = li.fresh_targeted_verdict(repo, RUN_ID)
    assert record is not None and record["status"] == "pass"
    assert record["tests"] == ["test_a.py"]
    assert set(record["covered"]) == {"test_a.py", "dirty.txt"}
    assert li.landing_test_gate(repo, {"targeted_tests": {"status": "fail", "baseline": None}}, RUN_ID)[1] == "pass-on-retest"


def test_fail_writes_status_fail_and_exits_1(repo, tmp_path):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: 1,
    )
    assert rc == 1
    assert li.fresh_targeted_verdict(repo, RUN_ID)["status"] == "fail"


def test_caused_node_ids_name_the_tests(repo, tmp_path):
    out = _task_output(tmp_path, baseline={"caused": ["test_a.py::test_a"]})
    seen = []
    retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert seen and seen[0][-1] == "test_a.py"


def test_no_targeted_set_writes_stale_note_the_gate_does_not_block_on(repo, tmp_path, capsys):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: pytest.fail("must not run"),
    )
    assert rc == 0
    record = li.fresh_targeted_verdict(repo, RUN_ID)
    assert record["status"] == "stale" and "stale" in record["note"]
    assert "stale" in capsys.readouterr().out
    findings, check = li.landing_test_gate(repo, {"targeted_tests": {"status": "fail", "baseline": None}}, RUN_ID)
    assert findings == []


def test_pathless_failure_reruns_recorded_targeted_set(repo, tmp_path):
    out = _task_output(tmp_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    data["next_action"]["params"]["targeted_tests"]["tests"] = ["test_a.py::test_a"]
    out.write_text(json.dumps(data), encoding="utf-8")
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0 and seen[0][-1] == "test_a.py"
    assert li.fresh_targeted_verdict(repo, RUN_ID)["status"] == "pass"


@pytest.mark.parametrize("value", ["test_a.py test_b.py", "test_a.py,test_b.py", "test_a.py, test_b.py"])
def test_combined_tests_value_is_split_into_paths(repo, tmp_path, value):
    (repo / "test_b.py").write_text("def test_b():\n    pass\n", encoding="utf-8")
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", value, "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0 and seen[0][-2:] == ["test_a.py", "test_b.py"]
    assert li.fresh_targeted_verdict(repo, RUN_ID)["tests"] == ["test_a.py", "test_b.py"]


def test_missing_test_path_is_refused_by_name_before_running(repo, tmp_path, capsys):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py,nope.py", "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: pytest.fail("must not run"),
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "nope.py" in err and "test_a.py," not in err and "does not exist" in err
    assert li.fresh_targeted_verdict(repo, RUN_ID) is None


def test_dirty_paths_reads_untracked_and_modified(repo):
    (repo / "new.txt").write_text("x", encoding="utf-8")
    (repo / "test_a.py").write_text("changed\n", encoding="utf-8")
    assert sorted(retest_targeted.dirty_paths(repo)) == ["new.txt", "test_a.py"]


def _declare(repo: Path, **keys: str) -> None:
    body = "".join(f"{k}: {v}\n" for k, v in keys.items())
    (repo / "coordinator.local.md").write_text(f"---\n{body}---\n", encoding="utf-8")


def test_declared_launcher_outranks_fast_test_cmd(repo, tmp_path):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run.py").write_text("", encoding="utf-8")
    _declare(repo, targeted_test_launcher="scripts/run.py --tier targeted", fast_test_cmd="scripts/tier.py --tier sufficient")
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: seen.append(argv) or 0,
    )
    assert rc == 0
    assert seen[0][1:] == ["scripts/run.py", "--tier", "targeted", "test_a.py"]
    assert "sufficient" not in " ".join(seen[0])


def test_nonzero_launcher_exit_other_than_test_failure_records_error_with_reason(repo, tmp_path):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: 126,
    )
    assert rc == 2
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == "error" and "126" in record["reason"]


def test_undeclared_launcher_file_records_error(repo, tmp_path):
    _declare(repo, targeted_test_launcher="scripts/missing.py")
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: pytest.fail("must not run"),
    )
    assert rc == 2
    record = json.loads(li.fresh_verdict_path(repo, RUN_ID).read_text(encoding="utf-8"))
    assert record["status"] == "error" and "missing.py" in record["reason"]


def test_node_id_passes_through_and_covered_names_the_file(repo, tmp_path):
    seen = []
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py::test_a", "--repo-root", str(repo)],
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
    findings, check = li.landing_test_gate(repo, {"failed_tests": ["test_a.py::test_a"]}, RUN_ID)
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


@pytest.mark.parametrize("run_id", ["../escape", "a/b", ".."])
def test_unsafe_run_id_refuses_before_any_write(repo, tmp_path, run_id):
    params = {"run_id": run_id, "script_path": "x/y", "targeted_tests": {"status": "fail", "baseline": None}}
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--tests", "test_a.py", "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: pytest.fail("must not run"),
    )
    assert rc == 2
    assert not (repo / li.RETEST_DIR).exists() and not (tmp_path / "escape.json").exists()


@pytest.mark.parametrize("test_path", ["../outside.py", "../../x.py::t"])
def test_test_path_escaping_the_repo_refuses_without_running(repo, tmp_path, test_path):
    rc = retest_targeted.main(
        ["--from-task-output", str(_task_output(tmp_path)), "--tests", test_path, "--repo-root", str(repo)],
        run=lambda argv, cwd, **_kw: pytest.fail("must not run"),
    )
    assert rc == 2


def _launcher(repo: Path, body: str) -> None:
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "run.py").write_text(body, encoding="utf-8")
    _declare(repo, targeted_test_launcher="scripts/run.py")


def _real_args(tmp_path: Path, repo: Path) -> list[str]:
    return ["--from-task-output", str(_task_output(tmp_path)), "--tests", "test_a.py", "--repo-root", str(repo)]


def test_hung_launcher_ends_as_error_verdict_within_the_bound(repo, tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(retest_targeted, "RETEST_LAUNCH_TIMEOUT_SECS", 1.0)
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


def test_option_values_survive_root_stripping(repo):
    (repo / "roots_a").mkdir()
    (repo / "setup.cfg").write_text("", encoding="utf-8")
    argv = retest_targeted._strip_collection_roots(
        repo, "python -m pytest --rootdir . -c setup.cfg roots_a"
    )
    assert argv == ["python", "-m", "pytest", "--rootdir", ".", "-c", "setup.cfg"]


def test_warm_entry_returns_an_exit_code_within_the_bound(repo, tmp_path, monkeypatch):
    import time

    from coordinator_core.ops import invoke_from_argv

    monkeypatch.setattr(retest_targeted, "RETEST_LAUNCH_TIMEOUT_SECS", 1.0)
    _launcher(repo, "import time\ntime.sleep(60)\n")
    start = time.monotonic()
    result = invoke_from_argv._run_entrypoint(
        "emit-dispatch-workflow", ["--retest-targeted", *_real_args(tmp_path, repo)], str(repo),
    )
    assert time.monotonic() - start < 20
    assert result["exit_code"] == 2
