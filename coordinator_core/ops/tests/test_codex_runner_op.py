"""codex_runner.start / codex_runner.result op tests (child process stubbed)."""
from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import codex_runner as op


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


class _FakeProc:
    pid = 31337


@pytest.fixture
def popen(monkeypatch):
    calls = []

    def fake(argv, **kw):
        calls.append((argv, kw))
        return _FakeProc()

    monkeypatch.setattr(op.subprocess, "Popen", fake)
    return calls


def _review(tmp_path, **kw):
    return {"mode": "review", "repo_root": str(tmp_path), "artifact": "a.md", **kw}


def test_start_writes_request_and_spawns_detached(home, popen, tmp_path):
    out = op._codex_runner_start(_review(tmp_path, parent_session_id="s1"))

    assert out["pid"] == 31337 and out["run_id"].startswith("review-")
    run_dir = home / "codex-runs" / out["run_id"]
    assert out["run_dir"] == str(run_dir)
    req = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
    assert req["mode"] == "review" and req["artifact"] == "a.md" and req["parent_session_id"] == "s1"
    assert json.loads((run_dir / "child.json").read_text(encoding="utf-8")) == {"pid": 31337}

    argv, kw = popen[0]
    assert argv[1:] == ["-m", "coordinator_core.codex_runner", str(run_dir)]
    assert kw.get("start_new_session") is True or "creationflags" in kw
    assert kw["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "bogus"},
        {"mode": "review"},
        {"mode": "execute", "plan": "p.md"},
        {"mode": "review", "artifact": 5},
    ],
)
def test_start_rejects_invalid_params(home, popen, tmp_path, bad):
    params = {"repo_root": str(tmp_path), **bad}
    with pytest.raises(CallerFacingValidationError):
        op._codex_runner_start(params)
    assert popen == []


def test_start_rejects_missing_repo_root_dir(home, popen, tmp_path):
    with pytest.raises(CallerFacingValidationError):
        op._codex_runner_start(_review(tmp_path, repo_root=str(tmp_path / "nope")))
    assert popen == []


@pytest.mark.parametrize('mode,assignment', [
    ('worker', {}),
    ('worker', {'session_id':'s', 'run':'r', 'declared_peak_mb':128}),
    ('persona', {}),
    ('persona', {'session_id':' ', 'run':'r', 'declared_peak_mb':128}),
    ('persona', {'session_id':'s', 'run':'r', 'declared_peak_mb':True}),
])
def test_malformed_assignment_rejected_before_wrapper_spawn(home, popen, tmp_path, mode, assignment):
    params = {'mode':mode, 'repo_root':str(tmp_path), 'task_file':'task.txt', 'assignment':assignment}
    if mode == 'persona':
        params.update(persona='role.md', tier_map='map.json', provider='openai')
    with pytest.raises(CallerFacingValidationError, match='assignment'):
        op._codex_runner_start(params)
    assert not popen
    assert not (home / 'codex-runs').exists()


@pytest.mark.parametrize('mode,provider,expected', [
    ('review', 'grok', 'codex'), ('agy-review', 'codex', 'agy'), ('grok-review', 'codex', 'grok'),
])
def test_fixed_mode_roster_provider_comes_from_actual_backend(tmp_path, monkeypatch, mode, provider, expected):
    from coordinator_core.codex_runner import __main__ as worker, contract, roster
    request = contract.RunRequest(mode=mode, repo_root=str(tmp_path), artifact='a.md', provider=provider)
    (tmp_path / 'request.json').write_text(json.dumps(request.to_dict()), encoding='utf-8')
    registrations = []
    monkeypatch.setattr(roster, 'write_record', lambda *args, **kwargs: registrations.append(kwargs))
    monkeypatch.setattr(roster, 'remove_record', lambda pid: None)
    def backend(req, run_dir, on_spawn):
        on_spawn(123)
        return contract.RunResult(run_id=run_dir.name, status='ok')
    monkeypatch.setattr(worker, '_mode_run', lambda mode: backend)
    worker.run_worker(tmp_path)
    assert registrations == [{'provider':expected}]


def test_worker_persona_output_cap_counts_utf8_bytes_and_reports_truncation(tmp_path, monkeypatch):
    from coordinator_core.codex_runner import output
    monkeypatch.setattr(output, 'OUTPUT_LIMIT_BYTES', 4)
    path = tmp_path / 'stdout'
    path.write_bytes('€€'.encode('utf-8'))
    text, usage = output.read_output(path)
    assert text == '€' and len(text.encode('utf-8')) <= 4
    assert usage == {'output_truncated':True, 'output_limit_bytes':4, 'output_bytes':3}


def test_start_process_time_under_500ms_with_child_stubbed(home, popen, tmp_path):
    t0 = time.process_time()
    op._codex_runner_start(_review(tmp_path))
    assert time.process_time() - t0 < 0.5


def _started(tmp_path):
    return op._codex_runner_start(_review(tmp_path))["run_id"]


def test_result_running_while_worker_alive(home, popen, tmp_path, monkeypatch):
    run_id = _started(tmp_path)
    monkeypatch.setattr(op, "_pid_alive", lambda pid: True)
    assert op._codex_runner_result({"run_id": run_id}) == {"run_id": run_id, "status": "running"}


def test_result_reads_result_json(home, popen, tmp_path, monkeypatch):
    run_id = _started(tmp_path)
    monkeypatch.setattr(op, "_pid_alive", lambda pid: True)
    body = {"run_id": run_id, "status": "ok", "findings": [], "verdict": "Ready"}
    (home / "codex-runs" / run_id / "result.json").write_text(json.dumps(body), encoding="utf-8")
    assert op._codex_runner_result({"run_id": run_id}) == body


def test_result_dead_worker_without_result_is_failed(home, popen, tmp_path, monkeypatch):
    run_id = _started(tmp_path)
    monkeypatch.setattr(op, "_pid_alive", lambda pid: False)
    out = op._codex_runner_result({"run_id": run_id})
    assert out["status"] == "failed" and "result.json" in out["error"]


def test_result_running_before_child_pid_recorded(home, popen, tmp_path):
    run_id = _started(tmp_path)
    (home / "codex-runs" / run_id / "child.json").unlink()
    assert op._codex_runner_result({"run_id": run_id})["status"] == "running"


@pytest.mark.parametrize("run_id", ["../x", "", None, 3])
def test_result_rejects_bad_run_id(home, run_id):
    with pytest.raises(CallerFacingValidationError):
        op._codex_runner_result({"run_id": run_id})


def test_result_unknown_run_id(home):
    with pytest.raises(CallerFacingValidationError):
        op._codex_runner_result({"run_id": "review-nope"})


def test_ops_registered_and_classified():
    from coordinator_core.authz.classification import OpClass, OP_CLASSIFICATION
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_MODULE_MAP["codex_runner.start"] == "coordinator_core.ops.codex_runner"
    assert OP_MODULE_MAP["codex_runner.result"] == "coordinator_core.ops.codex_runner"
    assert OP_CLASSIFICATION["codex_runner.start"] is OpClass.MUTATING
    assert OP_CLASSIFICATION["codex_runner.result"] is OpClass.COMPUTE_ONLY


def test_named_review_roundtrips_through_detached_worker_and_result(home, popen, tmp_path, monkeypatch):
    from coordinator_core.codex_runner import __main__ as worker, named_review
    params = dict(mode="named-review", repo_root=str(tmp_path), agent_type="coordinator:staff-eng",
                  provider="codex", model="gpt-6-astra", brief="Review the request contract", verdicts=["READY"])
    out = op._codex_runner_start(params)
    assert out["run_id"].startswith("named-review-") and len(popen) == 1
    def fake_reviewer(agent_type, brief, choice, **kwargs):
        assert (agent_type, brief, choice.model) == (params["agent_type"], params["brief"], params["model"])
        assert kwargs["verdicts"] == ("READY",) and kwargs["cwd"] == str(tmp_path)
        kwargs["on_spawn"](31338)
        return {"verdict": "READY", "findings": [{"finding": "contract accepted"}]}
    monkeypatch.setattr(named_review, "run_reviewer", fake_reviewer)
    worker.run_worker(home / "codex-runs" / out["run_id"])
    result = op._codex_runner_result({"run_id": out["run_id"]})
    assert result["status"] == "ok" and result["verdict"] == "READY"
    assert result["findings"] == [{"finding": "contract accepted"}]
