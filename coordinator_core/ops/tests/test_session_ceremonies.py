"""Six native routes, detached admission, durable truth, and completion push."""
import json
from pathlib import Path

import pytest

from coordinator_core import session_ceremonies as worker
from coordinator_core.ops import session_ceremonies as ops


@pytest.fixture(autouse=True)
def reset_retry_state(monkeypatch):
    from types import SimpleNamespace
    from contextlib import nullcontext
    from coordinator_core.bash_guards import _heavy_command_class as capacity
    monkeypatch.setattr(capacity, "_load_box_memory_host", lambda: SimpleNamespace(
        admission_file=lambda: "fixture", admission_lock=lambda path: nullcontext()))
    monkeypatch.setattr(capacity, "admit_before_spawn", lambda *a, **k: SimpleNamespace(admitted=True, reason="admitted", peak_gib=0.25))
    monkeypatch.setattr(capacity, "register_spawn", lambda *a, **k: True)
    monkeypatch.setattr(capacity, "release_spawn", lambda *a, **k: None)
    if worker._pending_scan is not None:
        worker._pending_scan.close()
    worker._pending_scan = None
    worker._pending_active.clear()
    yield
    if worker._pending_scan is not None:
        worker._pending_scan.close()
    worker._pending_scan = None
    worker._pending_active.clear()


@pytest.fixture
def ceremony_request(tmp_path):
    return {"ceremony": "pickup", "phase": "brief", "artifact_path": "state/handoffs/test.md",
            "repo_root": str(tmp_path), "session_id": "01a1b63c-040a-76f2-a9b9-929d9a0d0098", "fire": True}


@pytest.mark.parametrize("name,phase,entry,leading", [
    ("pickup", "brief", "pickup-assemble", ["brief"]),
    ("handoff", "brief", "baton-assemble", ["brief", "handoff"]),
    ("workday_start", "launch", "ceremony-run", ["workday-start", "launch"]),
    ("workday_complete", "launch", "ceremony-run", ["workday-complete", "launch"]),
    ("workstream_start", "brief", "orient-assemble", ["brief", "--cadence", "session"]),
    ("quick_wrap", "brief", "quick-wrap-assemble", ["brief"]),
])
def test_all_six_use_existing_native_implementation(ceremony_request, name, phase, entry, leading):
    ceremony_request.update(ceremony=name, phase=phase)
    target, argv = worker.native_invocation(ceremony_request)
    assert target == entry
    assert argv[:len(leading)] == leading


def test_decisions_carried_without_reinterpretation(ceremony_request):
    ceremony_request.update(phase="apply", decisions={"coast": {"disposition": "proceed"}})
    _, argv = worker.native_invocation(ceremony_request)
    assert json.loads(argv[argv.index("--decisions") + 1]) == ceremony_request["decisions"]
    assert argv[-2:] == ["--session-id", ceremony_request["session_id"]]


def test_ceremony_cannot_be_mistaken_for_done(ceremony_request, monkeypatch):
    decision = {"judgment_points": [{"id": "confirm-scope"}], "directives": [{"id": "stamp"}]}
    monkeypatch.setattr(worker, "_run_entrypoint", lambda *a: {"exit_code": 0, "stdout": json.dumps(decision), "stderr": ""})
    result = worker.perform(ceremony_request)
    assert result["state"] == "phase-done"
    assert result["ceremony_completed"] is False
    assert result["pending_actions"]["judgment_points"] == decision["judgment_points"]
    assert result["pending_actions"]["directives"] == decision["directives"]


def test_workflow_tool_is_replaced_by_real_engine_fire(ceremony_request, monkeypatch):
    monkeypatch.setattr(worker, "_run_entrypoint", lambda *a: {"exit_code": 0, "stdout": 'Workflow({scriptPath: "C:/repo/run.mjs"})', "stderr": ""})
    calls = []
    monkeypatch.setattr(worker, "_fire", lambda script, req: calls.append(script) or {"fire_id": "fire-1"})
    result = worker.perform(ceremony_request)
    assert calls == ["C:/repo/run.mjs"]
    assert result["workflow_fires"] == [{"fire_id": "fire-1"}]
    assert result["ceremony_completed"] is False


def test_result_persisted_before_completion_notice(ceremony_request, tmp_path, monkeypatch):
    ceremony_request["notify_to"] = "codex-test"
    (tmp_path / "request.json").write_text(json.dumps(ceremony_request), encoding="utf-8")
    monkeypatch.setattr(worker, "perform", lambda req: {"state": "phase-done", "ceremony_completed": False})
    class Delivery:
        status = "queued"
    def route(message):
        assert json.loads((tmp_path / "result.json").read_text())["state"] == "phase-done"
        return Delivery()
    monkeypatch.setattr(worker.bridge, "route", route)
    assert worker.run(tmp_path)["notify_status"] == "queued"


def test_exception_is_durable_and_not_success(ceremony_request, tmp_path, monkeypatch):
    (tmp_path / "request.json").write_text(json.dumps(ceremony_request), encoding="utf-8")
    def fail(req):
        raise RuntimeError("native gate failed")
    monkeypatch.setattr(worker, "perform", fail)
    result = worker.run(tmp_path)
    assert result["state"] == "phase-failed"
    assert "native gate failed" in result["error"]


def test_admission_detaches_without_wait(ceremony_request, tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "_runs", lambda: tmp_path / "runs")
    captured = {}
    class Child:
        import io
        stdin = io.BytesIO()
        pid = 4567
    def spawn(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return Child()
    monkeypatch.setattr(ops.subprocess, "Popen", spawn)
    result = ops._launch("pickup", {"artifact_path": ceremony_request["artifact_path"], "session_id": ceremony_request["session_id"]}, tmp_path)
    assert result["state"] == "accepted"
    assert result["pid"] == 4567
    assert "-m" in captured["argv"]
    assert captured["env"]["COORDINATOR_SESSION_ID"] == ceremony_request["session_id"]
    assert (Path(result["run_dir"]) / "request.json").is_file()


@pytest.mark.parametrize("params", [{"phase": "fake"}, {"phase": "finish"}, {"command": "shell"}])
def test_invalid_request_rejected_before_spawn(params, tmp_path):
    with pytest.raises(ValueError):
        ops.build_request("workday_start", params, tmp_path)


@pytest.mark.parametrize("bad", [None, 7, [], "../escape"])
def test_result_rejects_invalid_ids(bad):
    with pytest.raises(ValueError):
        ops.result({"run_id": bad})


def test_advance_executes_only_selected_durable_directives(tmp_path, monkeypatch, ceremony_request):
    monkeypatch.setattr(ops, "_runs", lambda: tmp_path)
    prior = tmp_path / ("a" * 32)
    prior.mkdir()
    ceremony_request.update(ceremony="workstream_start", phase="advance", prior_run_id="a" * 32,
                            directive_ids=["refresh"], decisions={"choose": {"disposition": "defer"}})
    point = {"id": "choose", "dispositions": [{"value": "defer"}]}
    directive = {"id": "refresh", "cli": "generate-repomap", "args": []}
    (prior / "request.json").write_text(json.dumps(ceremony_request), encoding="utf-8")
    (prior / "result.json").write_text(json.dumps({"decision_object": {
        "directives": [directive], "judgment_points": [point]}}), encoding="utf-8")
    calls = []
    def native(cli, argv, cwd):
        calls.append(cli)
        obj = {"directives": [directive] if calls.count("orient-assemble") == 1 else [], "judgment_points": [point]} if cli == "orient-assemble" else {}
        return {"exit_code": 0, "stdout": json.dumps(obj), "stderr": ""}
    monkeypatch.setattr(worker, "_run_entrypoint", native)
    result = worker.perform(ceremony_request)
    assert calls == ["orient-assemble", "generate-repomap", "orient-assemble"]
    assert result["decisions"] == ceremony_request["decisions"]
    assert result["ceremony_completed"] is True
    ceremony_request["directive_ids"] = ["injected-shell"]
    with pytest.raises(ValueError, match="unknown native directive"):
        worker.perform(ceremony_request)


def test_quick_wrap_requires_native_gate_and_work_finished(ceremony_request):
    ceremony_request.update(ceremony="quick_wrap", phase="advance", work_finished=True)
    result = {"exit_code": 0, "pending_actions": {"directives": [], "judgment_points": []},
              "decision_object": {"gates": {"entry_test": {"computed_failures": []},
                                            "run_close": {"verdict": "not-applicable", "outstanding": []},
                                            "commit_outcome": {"status": "empty"}}}}
    assert worker._completed(ceremony_request, result)
    ceremony_request["work_finished"] = False
    assert not worker._completed(ceremony_request, result)
    ceremony_request["work_finished"] = True
    result["decision_object"]["gates"]["run_close"]["verdict"] = "indeterminate"
    assert not worker._completed(ceremony_request, result)


def test_dead_worker_never_remains_running(tmp_path, monkeypatch):
    import psutil
    monkeypatch.setattr(ops, "_runs", lambda: tmp_path)
    run_dir = tmp_path / ("b" * 32)
    run_dir.mkdir()
    (run_dir / "request.json").write_text("{}")
    (run_dir / "child.json").write_text(json.dumps({"pid": 99, "birth": 12}))
    def vanished(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil, "Process", vanished)
    assert ops.result({"run_id": "b" * 32})["state"] == "phase-failed"


def test_notification_failure_retained_for_bounded_idle_retry(tmp_path, monkeypatch, ceremony_request):
    monkeypatch.setattr(ops, "_runs", lambda: tmp_path)
    run_dir = tmp_path / ("c" * 32)
    run_dir.mkdir()
    ceremony_request["notify_to"] = "codex-test"
    (run_dir / "request.json").write_text(json.dumps(ceremony_request))
    monkeypatch.setattr(worker, "perform", lambda req: {"state": "phase-done", "ceremony_completed": False})
    class Delivery:
        status = "refused"
    monkeypatch.setattr(worker.bridge, "route", lambda message: Delivery())
    assert worker.run(run_dir)["notify_status"] == "refused"
    marker = tmp_path / "pending-notifications" / (run_dir.name + ".json")
    assert marker.is_file()
    Delivery.status = "queued"
    worker.retry_pending_notifications()
    assert not marker.exists()
    assert json.loads((run_dir / "result.json").read_text())["notified_at"]


def test_refused_notification_does_not_starve_other_records(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "_runs", lambda: tmp_path)
    pending = tmp_path / "pending-notifications"
    pending.mkdir()
    for key in ("d", "e"):
        path = tmp_path / (key * 32)
        path.mkdir()
        (path / "result.json").write_text(json.dumps({"state": "phase-done"}))
        (pending / (path.name + ".json")).write_text(json.dumps({"run_dir": str(path), "target": key}))
    calls = []
    def notify(path, target, marker):
        calls.append(target)
        if target == "e":
            marker.unlink()
    monkeypatch.setattr(worker, "_notify", notify)
    worker.retry_pending_notifications()
    worker.retry_pending_notifications()
    assert set(calls) == {"d", "e"}


def test_transient_retry_io_error_retains_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "_runs", lambda: tmp_path)
    pending = tmp_path / "pending-notifications"
    pending.mkdir()
    marker = pending / ("f" * 32 + ".json")
    marker.write_text(json.dumps({"run_dir": str(tmp_path), "target": "codex"}))
    def fail(*args):
        raise OSError("temporary disk failure")
    monkeypatch.setattr(worker, "_notify", fail)
    assert worker.retry_pending_notifications() is True
    assert marker.exists()


@pytest.mark.parametrize("name", ["workday_start", "workday_complete"])
def test_workday_complete_requires_native_finished_receipt_and_no_failed_rows(tmp_path, monkeypatch, ceremony_request, name):
    from coordinator_core.ceremony_common.run import ledger, spec
    ceremony_request.update(ceremony=name, phase="finish", run_id="native-run")
    observed = []
    def native_path(repo, directory, run_id):
        observed.append(directory)
        return tmp_path
    monkeypatch.setattr(ledger, "run_dir", native_path)
    monkeypatch.setattr(ledger, "read", lambda native: [{"status": "ran"}])
    result = {"exit_code": 0, "pending_actions": {}}
    assert not worker._completed(ceremony_request, result)
    (tmp_path / "finished.json").write_text("{}")
    assert worker._completed(ceremony_request, result)
    assert observed[-1] == spec.load_spec(name).run_dir_name
    monkeypatch.setattr(ledger, "read", lambda native: [{"status": "failed"}])
    assert not worker._completed(ceremony_request, result)


def test_memory_hold_spawns_nothing(tmp_path, monkeypatch, ceremony_request):
    from types import SimpleNamespace
    from coordinator_core.bash_guards import _heavy_command_class as capacity
    monkeypatch.setattr(capacity, "admit_before_spawn", lambda *a, **k: SimpleNamespace(admitted=False, reason="HOLD"))
    def forbidden(*a, **k):
        raise AssertionError("no spawn on HOLD")
    monkeypatch.setattr(ops.subprocess, "Popen", forbidden)
    assert ops._launch("workstream_start", {"session_id": ceremony_request["session_id"]}, tmp_path)["state"] == "held"


def test_pending_workflow_never_counts_as_completed(ceremony_request):
    ceremony_request.update(ceremony="workstream_start", phase="advance")
    assert not worker._completed(ceremony_request, {
        "exit_code": 0, "pending_workflows": ["script.mjs"], "pending_actions": {}})


def test_failed_child_reservation_is_terminal_and_never_runs_native(tmp_path, monkeypatch, ceremony_request):
    from coordinator_core.bash_guards import _heavy_command_class as capacity
    monkeypatch.setattr(ops, "_runs", lambda: tmp_path / "runs")
    monkeypatch.setattr(capacity, "register_spawn", lambda *a, **k: False)
    called = []
    class Child:
        pid = 4567
        def terminate(self):
            called.append("terminate")
        def wait(self, timeout):
            called.append("wait")
    monkeypatch.setattr(ops.subprocess, "Popen", lambda *a, **k: Child())
    result = ops._launch("workstream_start", {"session_id": ceremony_request["session_id"]}, tmp_path)
    assert result["state"] == "phase-failed"
    run_dir = tmp_path / "runs" / result["run_id"]
    assert (run_dir / "result.json").is_file()
    assert not (run_dir / "admitted").exists()
    assert ops.result({"run_id": result["run_id"]})["state"] == "phase-failed"
    assert called == ["terminate", "wait"]


@pytest.mark.parametrize("change", ["unresolved-judgment", "changed-target"])
def test_advance_revalidates_native_target_and_shared_judgment_gate_before_mutation(tmp_path, monkeypatch, ceremony_request, change):
    monkeypatch.setattr(ops, "_runs", lambda: tmp_path)
    prior = tmp_path / ("a" * 32)
    prior.mkdir()
    ceremony_request.update(ceremony="workstream_start", phase="advance", prior_run_id="a" * 32, directive_ids=["repair"])
    directive = {"id": "repair", "cli": "native-repair", "args": ["original"], "depends_on": ["allow"]}
    point = {"id": "allow", "dispositions": [{"value": "yes", "resolves": ["repair"]}]}
    (prior / "request.json").write_text(json.dumps(ceremony_request))
    (prior / "result.json").write_text(json.dumps({"decision_object": {"directives": [directive], "judgment_points": [point]}}))
    if change == "changed-target":
        directive["args"] = ["different"]
        ceremony_request["decisions"] = {"allow": "yes"}
    def native(cli, argv, cwd):
        assert cli == "orient-assemble", "mutated before revalidation/gate acceptance"
        return {"exit_code": 0, "stdout": json.dumps({"directives": [directive], "judgment_points": [point]})}
    monkeypatch.setattr(worker, "_run_entrypoint", native)
    with pytest.raises(ValueError, match="unresolved judgments|changed since"):
        worker.perform(ceremony_request)


@pytest.mark.parametrize("token,expected", [(b"admitted\n", "phase-done"), (b"", "phase-failed")])
def test_startup_pipe_admission_or_eof_has_no_wall_deadline(tmp_path, monkeypatch, ceremony_request, token, expected):
    from types import SimpleNamespace
    ceremony_request["admission_key"] = "fixture"
    (tmp_path / "request.json").write_text(json.dumps(ceremony_request))
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=SimpleNamespace(readline=lambda: token)))
    monkeypatch.setattr(worker.time, "monotonic", lambda: 999999999.0)
    monkeypatch.setattr(worker, "perform", lambda req: {"state": "phase-done", "exit_code": 0})
    result = worker.run(tmp_path)
    assert result["state"] == expected
    assert not (tmp_path / "admitted").exists(), "worker admission depends on pipe, not clock or marker timing"


def test_known_ended_recipient_preserves_disk_without_keepalive(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "_runs", lambda: tmp_path)
    monkeypatch.setattr(worker, "_recipient_ended", lambda target: target == "ended")
    pending = tmp_path / "pending-notifications"
    pending.mkdir()
    marker = pending / ("e" * 32 + ".json")
    marker.write_text(json.dumps({"run_dir": str(tmp_path), "target": "ended"}))
    (tmp_path / "result.json").write_text(json.dumps({"state": "phase-done"}))
    monkeypatch.setattr(worker, "_notify", lambda *a: pytest.fail("sent to ended recipient"))
    assert worker.retry_pending_notifications() is False
    assert marker.exists()


@pytest.mark.parametrize("name", list(ops.CEREMONIES) + ["result"])
def test_real_cold_describe_exposes_typed_ceremony_contract(name):
    import io
    from contextlib import redirect_stdout
    from coordinator_core.invoke.__main__ import _dispatch_argv_body
    output = io.StringIO()
    with redirect_stdout(output), pytest.raises(SystemExit) as exit_:
        _dispatch_argv_body(["ceremony." + name, "--describe"], ".", allow_warm=False)
    assert exit_.value.code == 0
    result = json.loads(output.getvalue())
    schema = result["params_schema"]
    assert schema["additionalProperties"] is False
    if name == "result":
        assert schema["required"] == ["run_id"]
    else:
        assert schema["properties"]["phase"]["enum"] == list(ops.CEREMONIES[name])
        assert "notify_to" in schema["properties"]
