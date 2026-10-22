"""The main EM re-verifying an executor's work: second pytest run and scratch .py writes deny."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from coordinator_core.bash_guards import guard_post_executor_verification as g
from coordinator_core.hooks import nudge_em_code_dispatch as nd
from coordinator_core.hooks.dispatch_escape import ESCAPE_LOG_NAME

SID = "sess-verify-1"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(nd, "_is_bootstrap_or_out_of_repo", lambda p: False)
    monkeypatch.setattr(nd, "_session_sizing_state", lambda payload, sid: (True, False))
    monkeypatch.setattr(nd, "resolve_mode", lambda k, sid: False)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "pkg").mkdir()
    (tmp_path / "repo" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "repo" / "tools").mkdir()
    g._PKG_CACHE.clear()


@pytest.fixture
def repo(tmp_path):
    return tmp_path / "repo"


def _executor_ran(repo, agent_type="coordinator:executor") -> None:
    d = repo / ".git" / "coordinator-sessions" / SID
    d.mkdir(parents=True)
    (d / "dispatched-agents.txt").write_text(f"abcdef0123456789\tsonnet\t{agent_type}\t1\n")


def _bash(repo, cmd: str) -> dict:
    return {"tool_name": "Bash", "session_id": SID, "cwd": str(repo), "tool_input": {"command": cmd}}


_VERIFYING = "import pkg, pytest\npytest.main(['pkg'])\n"


def _write(repo, path: str, tool="Write", content: str = _VERIFYING) -> dict:
    return {
        "tool_name": tool,
        "session_id": SID,
        "cwd": str(repo),
        "tool_input": {"file_path": path, "content": content},
    }


def _denied(out) -> bool:
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_no_executor_yet_pytest_is_free(repo) -> None:
    for _ in range(3):
        assert g.check(_bash(repo, "python -m pytest a.py")) is None


def test_first_pytest_allowed_second_denied(repo) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "python -m pytest a.py")) is None
    out = g.check(_bash(repo, "pytest b.py -q"))
    assert _denied(out)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "engine-ask" not in reason and "sizing" not in reason
    assert "test-runner" in reason
    assert "No self-escape" in reason and "honoured once." in reason


def test_grep_pytest_not_counted(repo) -> None:
    _executor_ran(repo)
    for _ in range(3):
        assert g.check(_bash(repo, "grep -rn pytest coordinator_core")) is None
    assert g.check(_bash(repo, "pytest a.py")) is None


def test_non_executor_dispatch_does_not_arm(repo) -> None:
    _executor_ran(repo, "coordinator:test-runner")
    assert g.check(_bash(repo, "pytest a.py")) is None
    assert g.check(_bash(repo, "pytest a.py")) is None


def test_subagent_exempt(repo) -> None:
    _executor_ran(repo)
    p = _bash(repo, "pytest a.py")
    p["agent_id"] = "a1"
    assert g.check(p) is None and g.check(p) is None


def test_autonomous_still_routes_bash_verification(repo, monkeypatch) -> None:
    _executor_ran(repo)
    monkeypatch.setattr("coordinator_core.session.mode_resolution.resolve_mode", lambda k, sid: True)
    assert g.check(_bash(repo, "pytest a.py")) is None
    assert _denied(g.check(_bash(repo, "pytest a.py")))


def test_autonomous_scratch_write_exempt(repo, monkeypatch) -> None:
    _executor_ran(repo)
    monkeypatch.setattr(nd, "resolve_mode", lambda k, sid: True)
    assert nd.op(_write(repo, str(repo / "scratch" / "probe.py"))) is None


def _sidecar(repo, status: str) -> None:
    from coordinator_core.session.machinery_paths import share_dir

    d = share_dir(str(repo), SID)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "coordinator-executor.a1.md"), "w", encoding="utf-8") as fh:
        fh.write(f"---\nstatus: {status}\nagent_type: coordinator:executor\n---\n")


def _armed_with_reason(repo) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "pytest a.py")) is None
    with open(nd.dispatch_nudge_sentinel_path(SID), "w", encoding="utf-8") as fh:
        fh.write("need a wider run\n")


def test_reason_without_grant_or_failure_denied_not_consumed(repo) -> None:
    _armed_with_reason(repo)
    assert _denied(g.check(_bash(repo, "pytest a.py")))
    assert os.path.exists(nd.dispatch_nudge_sentinel_path(SID))


def test_non_failed_executor_with_reason_denied(repo) -> None:
    _armed_with_reason(repo)
    _sidecar(repo, "complete")
    assert _denied(g.check(_bash(repo, "pytest a.py")))
    assert os.path.exists(nd.dispatch_nudge_sentinel_path(SID))


def test_pm_grant_honoured(repo) -> None:
    from coordinator_core.session import guard_unlock_sentinel as gus

    _executor_ran(repo)
    assert g.check(_bash(repo, "pytest a.py")) is None
    gus.sentinel_path(SID, g.GUARD_NAME).write_text("")
    assert g.check(_bash(repo, "pytest a.py")) is None
    assert _denied(g.check(_bash(repo, "pytest a.py")))


def test_reasoned_sentinel_allows_and_logs(repo) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "pytest a.py")) is None
    assert _denied(g.check(_bash(repo, "pytest a.py")))
    with open(nd.dispatch_nudge_sentinel_path(SID), "w", encoding="utf-8") as fh:
        fh.write("need a wider run\n")
    _sidecar(repo, "blocked")
    assert g.check(_bash(repo, "pytest a.py")) is None
    logs = list((repo / ".coordinator-local").rglob(ESCAPE_LOG_NAME))
    assert len(logs) == 1
    assert json.loads(logs[0].read_text().splitlines()[0])["reason"] == "need a wider run"
    assert not os.path.exists(nd.dispatch_nudge_sentinel_path(SID))
    assert _denied(g.check(_bash(repo, "pytest a.py")))


def test_scratch_py_write_denied_after_executor(repo) -> None:
    _executor_ran(repo)
    out = nd.op(_write(repo, str(repo / "scratch" / "probe.py")))
    assert _denied(out)
    assert "test-runner" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_scratch_launcher_config_edit_allowed_after_executor(repo, tmp_path) -> None:
    _executor_ran(repo)
    launcher = tmp_path / "other-repo" / "scratch" / "metahuman-affordance" / "mh-run-full.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("import os\nimport pkg\n", encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "session_id": SID,
        "cwd": str(repo),
        "tool_input": {
            "file_path": str(launcher),
            "old_string": "import os\n",
            "new_string": 'import os\nos.environ["EXAMPLE_RETRIEVAL_REPO_MEM_CAP_GIB_DERIVE"] = "6"\n',
        },
    }
    assert nd.op(payload) is None


def test_scratch_non_test_probe_write_allowed_after_executor(repo) -> None:
    _executor_ran(repo)
    probe = "import pkg\nprint(pkg.__name__)\n"
    assert nd.op(_write(repo, str(repo / "scratch" / "probe.py"), content=probe)) is None


def test_scratch_edit_adding_runner_to_repo_importing_file_denied(repo) -> None:
    _executor_ran(repo)
    path = repo / "scratch" / "probe.py"
    path.parent.mkdir()
    path.write_text("import pkg\n", encoding="utf-8")
    payload = _write(repo, str(path), tool="Edit")
    payload["tool_input"] = {"file_path": str(path), "old_string": "import pkg\n", "new_string": "import pkg\nimport pytest\npytest.main([])\n"}
    assert _denied(nd.op(payload))


def test_scratch_write_running_measure_against_repo_denied(repo) -> None:
    _executor_ran(repo)
    content = "import pkg\nimport subprocess\nsubprocess.run(['python', '-m', 'pkg.benchmarks.measure', '--', 'x'])\n"
    assert _denied(nd.op(_write(repo, str(repo / "scratch" / "probe.py"), content=content)))


_GLOB_HELPER = "import glob, json\nprint(json.dumps(sorted(glob.glob('docs/plans/*.md'))))\n"


@pytest.mark.parametrize("content", [
    _GLOB_HELPER,
    "import pkg\nprint(pkg)\n",
    "import pytest\nprint(pytest)\n",
])
def test_scratch_py_write_not_naming_runner_and_repo_allowed_after_executor(repo, content) -> None:
    _executor_ran(repo)
    assert nd.op(_write(repo, str(repo / "scratch" / "mise-plans.py"), content=content)) is None


def test_scratch_py_edit_adding_verification_denied_after_executor(repo) -> None:
    _executor_ran(repo)
    payload = _write(repo, str(repo / "scratch" / "probe.py"), tool="Edit")
    payload["tool_input"] = {"file_path": str(repo / "scratch" / "probe.py"), "new_string": _VERIFYING}
    assert _denied(nd.op(payload))


def test_scratch_py_write_allowed_before_executor(repo) -> None:
    assert nd.op(_write(repo, str(repo / "scratch" / "probe.py"))) is None


def test_scratch_md_write_allowed_after_executor(repo) -> None:
    _executor_ran(repo)
    assert nd.op(_write(repo, str(repo / "scratch" / "notes.md"))) is None


def test_scratch_py_sentinel_allows_and_logs(repo) -> None:
    _executor_ran(repo)
    with open(nd.dispatch_nudge_sentinel_path(SID), "w", encoding="utf-8") as fh:
        fh.write("one-off probe\n")
    _sidecar(repo, "blocked")
    assert nd.op(_write(repo, str(repo / "scratch" / "probe.py"))) is None
    assert len(list((repo / ".coordinator-local").rglob(ESCAPE_LOG_NAME))) == 1
    assert _denied(nd.op(_write(repo, str(repo / "scratch" / "probe.py"))))


def test_free_first_run_does_not_spend_the_sentinel(repo) -> None:
    _executor_ran(repo)
    with open(nd.dispatch_nudge_sentinel_path(SID), "w", encoding="utf-8") as fh:
        fh.write("saved for later\n")
    assert g.check(_bash(repo, "pytest a.py")) is None
    assert os.path.exists(nd.dispatch_nudge_sentinel_path(SID))


@pytest.mark.parametrize("cmd", [
    "SP=/s; with-suite-mutex -- timeout 900 python3 -m pytest tests/ -q -k ledger > $SP/a 2>&1",
    "n=$(timeout 120 python3 -m pytest t.py --collect-only -q | wc -l)",
    "for f in a.py b.py; do n=$(timeout 120 python3 -m pytest $f -q | tail -1); echo $n; done",
    "with-suite-mutex -- timeout 600 bash -c 'for i in 1 2; do python3 -m pytest a.py -q; done'",
    "nice -n 5 nohup time pytest a.py",
])
def test_wrapped_pytest_is_counted(cmd) -> None:
    assert g.runs_pytest(cmd)


@pytest.mark.parametrize("cmd", [
    "timeout 5 grep -rn pytest x",
    "for f in a; do grep pytest $f; done",
    "x=$(git log | grep pytest)",
    "bash -c 'echo pytest'",
])
def test_wrapped_non_pytest_not_counted(cmd) -> None:
    assert not g.runs_pytest(cmd)


@pytest.mark.parametrize("cmd", [
    "python3 -c 'from pkg.mod import f; import pytest; pytest.main([f()])'",
    "python3 -c 'import os, pkg, unittest'",
    "timeout 60 python3 -c 'import pkg.x as m, pytest; m.run()'",
    "n=$(python3 -c 'from pkg import a; import pytest' | wc -l)",
    "python3 -I /elsewhere/scratchpad/census.py",
    "python3 -I ../scratch/probe.py",
    "bash -c 'python3 -c \"import pkg, pytest\"'",
    "ls scratch && python3 - <<'PY'\nimport importlib.util, pytest\nm = load(\"pkg/mod.py\", \"a\")\nPY",
    "python3 <<PY\nfrom pkg import a\nimport unittest\nPY",
    "python3 -c 'import pytest; spec_from_file_location(\"m\", \"./pkg/mod.py\")'",
])
def test_probe_forms_counted(repo, cmd, tmp_path) -> None:
    for rel in ("elsewhere/scratchpad/census.py", "scratch/probe.py"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("import pkg, pytest\npytest.main(['a.py'])\n")
    cmd = cmd.replace("/elsewhere/", (tmp_path / "elsewhere").as_posix() + "/").replace("../scratch", (tmp_path / "scratch").as_posix())
    assert g.runs_verification(cmd, str(repo), str(repo))


def _script(tmp_path, rel: str, source: str) -> str:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path.as_posix()


@pytest.mark.parametrize("rel,source", [
    ("scratchpad/census.py", "import pytest\nfrom pkg.mod import f\nf()\n"),
    ("scratch/probe.py", "import os, pkg, pytest\n"),
    ("tmp/probe.py", "import pytest\nspec_from_file_location('m', './pkg/mod.py')\n"),
])
def test_scratch_script_importing_repo_counted(repo, tmp_path, rel, source) -> None:
    path = _script(tmp_path, rel, source)
    assert g.runs_verification(f"python3 -I {path}", str(repo), str(repo))


def test_scratch_script_relative_path_counted(repo, tmp_path) -> None:
    _script(tmp_path, "scratch/probe.py", "import pkg, pytest\n")
    assert g.runs_verification("python3 -I ../scratch/probe.py", str(repo), str(repo))


@pytest.mark.parametrize("rel,source", [
    ("scratchpad/legs.py", "import json, os\nprint(json.dumps({}))\n"),
    ("tmp/audit.py", "import numpy\n"),
])
def test_stdlib_only_scratch_script_not_counted(repo, tmp_path, rel, source) -> None:
    path = _script(tmp_path, rel, source)
    assert not g.runs_verification(f"python3 -I {path}", str(repo), str(repo))


def test_unreadable_scratch_script_fails_open(repo, tmp_path) -> None:
    assert not g.runs_verification(
        f"python3 {tmp_path / 'scratchpad' / 'missing.py'}", str(repo), str(repo)
    )
    assert not g.runs_verification(f"python3 {tmp_path / 'scratchpad'}.py", str(repo), str(repo))


def test_probe_read_is_size_capped(repo, tmp_path) -> None:
    path = _script(tmp_path, "scratchpad/big.py", "#" * (g._PROBE_READ_CAP + 10) + "\nimport pkg\n")
    assert not g.runs_verification(f"python3 {path}", str(repo), str(repo))


@pytest.mark.parametrize("cmd", [
    "python3 -c 'from pkg.workstream_complete import apply; apply(plan=1)'",
    "python3 -c 'import pkg'",
    "python3 - <<'PY'\nfrom pkg import register\nprint(register.pin())\nPY",
])
def test_bookkeeping_code_not_verification(repo, cmd) -> None:
    assert not g.runs_verification(cmd, str(repo), str(repo))


@pytest.mark.parametrize("cmd", [
    "python3 - <<'PY'\nfrom pkg import fm\n# keep the pytest-collected description lines\n"
    "fm.rewrite('description: run pytest first')\nPY\npython3 tools/doc-index.py --write",
    "python3 -c 'from pkg import a; a.note(\"unittest and doctest\")'",
    "python3 - <<'PY'\nfrom pkg import a\n\"\"\"docstring naming pytest\"\"\"\nPY",
])
def test_runner_named_only_in_text_not_verification(repo, cmd) -> None:
    assert not g.runs_verification(cmd, str(repo), str(repo))


@pytest.mark.parametrize("cmd", [
    "python3 - <<'PY'\nfrom pkg import a\nimport subprocess\nsubprocess.run(['pytest', 'a.py'])\nPY",
    "python3 -c 'from pkg import a; import pytest; pytest.main()'",
    "python3 -m pytest a.py",
    "python3 -c 'from pkg import a; __import__(\"pytest\").main()'",
    "python3 -c 'from pkg import a; import os; os.system(\"pytest a.py\")'",
    "python3 -c 'from pkg import a; x = (\"pytest\"'",
])
def test_runner_invoked_in_code_is_verification(repo, cmd) -> None:
    assert g.runs_verification(cmd, str(repo), str(repo))


def test_script_naming_runner_only_in_text_not_counted(repo, tmp_path) -> None:
    path = _script(tmp_path, "scratchpad/doc.py", 'import pkg\n"""mentions pytest"""\n# unittest\n')
    assert not g.runs_verification(f"python3 {path}", str(repo), str(repo))


def test_read_only_probe_script_is_not_verification(repo, tmp_path) -> None:
    pin = tmp_path / "tmp" / "register_pin.py"
    pin.write_text("from pkg import register\nprint(register.read())\n")
    assert not g.runs_verification(f"python3 {pin}", str(repo), str(repo))
    _executor_ran(repo)
    assert g.check(_bash(repo, "pytest a.py")) is None
    assert g.check(_bash(repo, f"python3 {pin}")) is None
    assert g.check(_bash(repo, "python3 -c 'from pkg.workstream_complete import apply; apply()'")) is None
    assert _denied(g.check(_bash(repo, "pytest b.py")))


@pytest.mark.parametrize("cmd", [
    "python3 -c 'import os, json; print(1)'",
    "python3 -c 'import numpy'",
    "python3 tools/x.py",
    "python3 -I ./pkg/run.py --flag",
    "python3 -m json.tool f.json",
    "python3 /opt/engine/scripts/rag_work_target_index.py --status",
    "ls pkg",
    "python3 - <<'PY'\nimport json\nprint(json.load(open(\"pkg/data.json\")))\nPY",
    "cat <<EOF > notes.txt\nimport pkg\nEOF",
])
def test_non_probe_forms_not_counted(repo, cmd) -> None:
    assert not g.runs_verification(cmd, str(repo), str(repo))


def _execute_plan_session(repo, status="executing") -> None:
    d = repo / ".git" / "coordinator-sessions" / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "session-shape.json").write_text(json.dumps({"plan": {"path": "docs/plans/p.md"}}))
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans" / "p.md").write_text(f"---\nstatus: {status}\n---\nbody\n")


def test_scoped_pytest_in_execute_plan_session_never_denied(repo) -> None:
    _executor_ran(repo)
    _execute_plan_session(repo)
    (repo / "pkg" / "tests").mkdir(parents=True, exist_ok=True)
    for cmd in ("python -m pytest pkg/test_a.py::t -q", "pytest pkg/tests -q", "pytest pkg/test_a.py -k x"):
        assert g.check(_bash(repo, cmd)) is None


def test_scoped_pytest_without_execute_plan_signal_still_denied(repo) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "pytest pkg/test_a.py")) is None
    assert _denied(g.check(_bash(repo, "pytest pkg/test_a.py")))


def test_non_executing_plan_does_not_exempt(repo) -> None:
    _executor_ran(repo)
    _execute_plan_session(repo, "landed")
    assert g.check(_bash(repo, "pytest pkg/test_a.py")) is None
    assert _denied(g.check(_bash(repo, "pytest pkg/test_a.py")))


@pytest.mark.parametrize("cmd", [
    "pytest",
    "pytest -k foo",
    "pytest -m slow",
    "pytest --pyargs pkg",
    "pytest /elsewhere/test_a.py",
    "pytest ../elsewhere/test_a.py",
    "pytest pkg/test_a.py && pytest",
    "pytest tools -q",
    "pytest coordinator_core",
    "pytest pkg",
])
def test_unscoped_pytest_in_execute_plan_session_still_denied(repo, cmd) -> None:
    _executor_ran(repo)
    _execute_plan_session(repo)
    for d in ("tools", "coordinator_core"):
        (repo / d).mkdir(exist_ok=True)
    assert g.check(_bash(repo, "pytest pkg/test_a.py -k exempt_leaves_counter_untouched")) is None
    # An exempt run does not bump the counter; the probe is the first (free) bump so
    # that ``cmd`` is the second and would be denied.
    assert g.check(_bash(repo, "python3 -c 'import pkg, pytest'")) is None
    assert _denied(g.check(_bash(repo, cmd)))


def _workflow_run_record(monkeypatch, tmp_path) -> None:
    from coordinator_core.hooks import flag_em_poll_in_flight as poll

    d = tmp_path / "wf"
    d.mkdir()
    (d / f"workflow-run-{SID}-t1.json").write_text(json.dumps({"run_id": "run-1"}))
    monkeypatch.setattr(poll, "_tmpdir", lambda: str(d))


def test_scoped_pytest_after_workflow_run_never_denied(repo, monkeypatch, tmp_path) -> None:
    _executor_ran(repo)
    _workflow_run_record(monkeypatch, tmp_path)
    for _ in range(3):
        assert g.check(_bash(repo, "pytest pkg/test_a.py -q")) is None


def test_broad_scoped_pytest_after_workflow_run_counts(repo, monkeypatch, tmp_path) -> None:
    _executor_ran(repo)
    _workflow_run_record(monkeypatch, tmp_path)
    cmd = "pytest " + " ".join(f"pkg/test_a.py::t{i}" for i in range(11))
    assert g.check(_bash(repo, cmd)) is None
    assert _denied(g.check(_bash(repo, cmd)))


def test_unscoped_pytest_after_workflow_run_still_denied(repo, monkeypatch, tmp_path) -> None:
    _executor_ran(repo)
    _workflow_run_record(monkeypatch, tmp_path)
    assert g.check(_bash(repo, "pytest")) is None
    out = g.check(_bash(repo, "pytest"))
    assert _denied(out)
    assert "engine-ask" not in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_probe_shares_pytest_counter(repo) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "pytest a.py")) is None
    out = g.check(_bash(repo, "python3 -c 'import pkg, pytest'"))
    assert _denied(out)
    assert "honoured once." in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_probe_first_free_then_probe_denied(repo, tmp_path) -> None:
    _executor_ran(repo)
    path = _script(tmp_path, "scratchpad/census.py", "import pkg, pytest\n")
    assert g.check(_bash(repo, "python3 -c 'import os'")) is None
    assert g.check(_bash(repo, f"python3 {path}")) is None
    assert _denied(g.check(_bash(repo, f"python3 {path}")))


def test_stdlib_scratch_audit_never_denied_after_executor(repo, tmp_path) -> None:
    _executor_ran(repo)
    path = _script(tmp_path, "scratchpad/legs.py", "import json\n")
    for _ in range(3):
        assert g.check(_bash(repo, f"python3 -I {path}")) is None


def test_no_executor_record_python_script_then_directory_pytest_never_denied(repo, tmp_path) -> None:
    script = tmp_path / "tmp" / "probe.py"
    script.write_text("print(1)\n")
    (repo / "tests").mkdir()
    for cmd in (f"python {script}", "python -m pytest tests", "pytest tests -q"):
        assert g.check(_bash(repo, cmd)) is None
    assert g.check(_bash(repo, "pytest tests -q")) is None


def test_payload_without_session_id_never_adopts_a_process_scoped_ledger(repo, monkeypatch) -> None:
    from coordinator_core.hooks import dispatch_escape

    fallback = dispatch_escape._resolve_session_id_op({})
    d = repo / ".git" / "coordinator-sessions" / fallback
    d.mkdir(parents=True)
    (d / "dispatched-agents.txt").write_text("abcdef0123456789\tsonnet\tcoordinator:executor\t1\n")
    anon = {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "pytest tests -q"}}
    for _ in range(3):
        assert g.check(anon) is None


def _stamp(repo, executor_epoch: int, commit_epoch: int | None) -> None:
    d = repo / ".git" / "coordinator-sessions" / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "dispatched-agents.txt").write_text(f"abcdef0123456789\tsonnet\tcoordinator:executor\t{executor_epoch}\n")
    if commit_epoch is not None:
        ev = d / "commit-scope-events.jsonl"
        ev.write_text("{}\n")
        os.utime(ev, (commit_epoch, commit_epoch))


def test_executor_older_than_last_commit_does_not_arm(repo) -> None:
    _stamp(repo, 1000, 2000)
    for _ in range(3):
        assert g.check(_bash(repo, "python -m pytest a.py")) is None


def test_executor_newer_than_last_commit_still_arms(repo) -> None:
    _stamp(repo, 3000, 2000)
    assert g.check(_bash(repo, "python -m pytest a.py")) is None
    assert _denied(g.check(_bash(repo, "pytest b.py -q")))


def test_no_commit_signal_stays_armed(repo) -> None:
    _stamp(repo, 1000, None)
    assert g.check(_bash(repo, "python -m pytest a.py")) is None
    assert _denied(g.check(_bash(repo, "pytest b.py -q")))


def test_stale_counter_before_newest_arming_row_is_ignored(repo) -> None:
    _stamp(repo, 1000, None)
    assert g.check(_bash(repo, "python -m pytest a.py")) is None
    counter = next((repo / ".git").rglob(g.COUNTER_NAME), None) or next(repo.parent.rglob(g.COUNTER_NAME))
    os.utime(counter, (500, 500))
    assert g.check(_bash(repo, "pytest b.py -q")) is None


_MEASURE = "python -m coordinator_core.benchmarks.measure"


def _counter(repo):
    return next(repo.parent.rglob(g.COUNTER_NAME), None) or next((repo / ".git").rglob(g.COUNTER_NAME), None)


@pytest.mark.parametrize("cmd", [
    f"{_MEASURE} -- python -X importtime -c 'import pkg'",
    f"{_MEASURE} --k 3 --once -- python tools/x.py",
    f"{_MEASURE} --json -- python -c 'import pkg, pytest; pkg.run()'",
    f"{_MEASURE} --",
    _MEASURE,
])
def test_measure_wrapped_non_pytest_neither_counted_nor_denied(repo, cmd) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "python -m pytest a.py")) is None
    before = _counter(repo).read_text()
    assert before == "1"
    assert g.check(_bash(repo, cmd)) is None
    assert _counter(repo).read_text() == before


@pytest.mark.parametrize("cmd", [
    f"{_MEASURE} -- python -m pytest a.py",
    f"{_MEASURE} --once -- pytest a.py -q",
    f"{_MEASURE} -- timeout 60 python3 -m pytest a.py",
    "python -mcoordinator_core.benchmarks.measure -- pytest a.py",
])
def test_measure_wrapped_pytest_is_still_counted_and_denied(repo, cmd) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "python -m pytest a.py")) is None
    out = g.check(_bash(repo, cmd))
    assert _denied(out)


def test_measure_wrapped_scoped_pytest_gets_the_scoped_exemption(repo) -> None:
    _executor_ran(repo)
    _execute_plan_session(repo)
    assert g.check(_bash(repo, "python -m pytest pkg/test_a.py")) is None
    assert g.check(_bash(repo, f"{_MEASURE} -- python -m pytest pkg/test_a.py -q")) is None
    assert g.check(_bash(repo, "python3 -c 'import pkg, pytest'")) is None
    assert _denied(g.check(_bash(repo, f"{_MEASURE} -- pytest")))


def test_both_deny_halves_name_the_measurement_route(repo) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "pytest a.py")) is None
    bash_reason = g.check(_bash(repo, "pytest b.py"))["hookSpecificOutput"]["permissionDecisionReason"]
    write_reason = nd.op(_write(repo, str(repo / "scratch" / "probe.py")))["hookSpecificOutput"]["permissionDecisionReason"]
    for reason in (bash_reason, write_reason):
        assert "coordinator_core.benchmarks.measure -- <argv>" in reason
        assert "test-runner" in reason
        assert "No self-escape" in reason and "honoured once." in reason


_SUITE = "with-suite-mutex -- python3 -m pytest coordinator/tests coordinator/bin/tests setup"


def _grant(monkeypatch, granted: bool) -> None:
    from coordinator_core.session import grant

    monkeypatch.setattr(grant, "check_tier_u_grant", lambda cwd=None, *, session_id=None: (granted, None))


def test_mutex_suite_under_live_grant_never_denied(repo, monkeypatch) -> None:
    _executor_ran(repo)
    _grant(monkeypatch, True)
    for _ in range(3):
        assert g.check(_bash(repo, _SUITE)) is None


def test_mutex_suite_without_grant_still_denied(repo, monkeypatch) -> None:
    _executor_ran(repo)
    _grant(monkeypatch, False)
    assert g.check(_bash(repo, _SUITE)) is None
    assert _denied(g.check(_bash(repo, _SUITE)))


def test_grant_does_not_exempt_a_bare_suite_beside_a_mutex_one(repo, monkeypatch) -> None:
    _executor_ran(repo)
    _grant(monkeypatch, True)
    cmd = f"{_SUITE}; python3 -m pytest coordinator/tests"
    assert g.check(_bash(repo, cmd)) is None
    assert _denied(g.check(_bash(repo, cmd)))
