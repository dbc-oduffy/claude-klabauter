from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from coordinator_core.worker_contracts import worker


@pytest.fixture
def admitted(monkeypatch, tmp_path):
    events = []
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(worker.slot_grant, "check_slot_grant", lambda *a, **kw: (True, {}))
    monkeypatch.setattr(worker.capacity, "admit_before_spawn", lambda *a, **kw:
                        events.append("reserve") or SimpleNamespace(admitted=True, peak_gib=2))
    monkeypatch.setattr(worker.capacity, "release_spawn", lambda *a: events.append("release"))
    monkeypatch.setattr(worker.roster, "write_record", lambda *a, **kw: events.append("roster"))
    monkeypatch.setattr(worker.roster, "remove_record", lambda *a: events.append("remove"))
    monkeypatch.setattr(worker.admission_post, "post", lambda phase, *a, **kw:
                        events.append((phase, kw)) or {"delivered": True})
    monkeypatch.setattr(worker, "_child_env", lambda _: {
        **{key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if key in os.environ},
        **{key: "parent" for key in ("CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "COORDINATOR_SESSION_ID")}})
    return events


ASSIGNMENT = {"session_id": "parent", "run": "review", "declared_peak_mb": 1024}


@pytest.mark.parametrize("provider,identity", [("openai", "codex"), ("anthropic", "claude"),
                                               ("google", "agy"), ("xai", "grok"),
                                               ("mistral", "mistral"), ("deepseek", "deepseek")])
def test_six_real_provider_children_receive_identity_and_fresh_session_environment(tmp_path, admitted, provider, identity):
    script = "import json,os; print(json.dumps({k:v for k,v in os.environ.items() if k in ['COORDINATOR_AGENT_PROVIDER','CODEX_THREAD_ID','CLAUDE_SESSION_ID','CLAUDE_CODE_SESSION_ID','COORDINATOR_SESSION_ID']}))"
    output = tmp_path / "env.json"
    with output.open("w", encoding="utf-8") as sink:
        assert worker.supervise(tmp_path, "read-only probe", ASSIGNMENT, [sys.executable, "-c", script],
                                 provider=provider, mode="persona", stdout=sink) == 0
    assert json.loads(output.read_text()) == {"COORDINATOR_AGENT_PROVIDER": identity}
    assert admitted[0] == "reserve"
    assert admitted[-2] == "remove"
    start = next(data for phase, data in (e for e in admitted if isinstance(e, tuple)) if phase == "START")
    assert start["declared_peak_mb"] == 2048


@pytest.mark.parametrize("stage", ["spawn", "callback", "start-refused", "start-raises", "communicate"])
def test_primary_failure_reaps_child_and_runs_all_cleanup(tmp_path, monkeypatch, admitted, stage):
    class Process:
        pid = 456
        returncode = 0
        calls = 0
        def kill(self):
            admitted.append("kill")
            raise ProcessLookupError("already exited")
        def communicate(self, prompt=None):
            self.calls += 1
            if stage == "communicate" and self.calls == 1:
                raise RuntimeError("primary")
    process = Process()
    def spawn(*a, **kw):
        if stage == "spawn":
            raise RuntimeError("primary")
        return process
    def callback(pid):
        if stage == "callback":
            raise RuntimeError("primary")
    original_post = worker.admission_post.post
    def post(phase, *a, **kw):
        if phase == "START" and stage == "start-refused":
            return {"delivered": False}
        if phase == "START" and stage == "start-raises":
            raise RuntimeError("primary")
        return original_post(phase, *a, **kw)
    monkeypatch.setattr(worker.admission_post, "post", post)
    with pytest.raises((RuntimeError, ValueError)):
        worker.supervise(tmp_path, "probe", ASSIGNMENT, ["unused"], provider="codex",
                          mode="persona", popen=spawn, on_spawn=callback)
    assert "release" in admitted
    if stage != "spawn":
        assert "kill" in admitted and "remove" in admitted
        assert any(isinstance(e, tuple) and e[0] == "END" for e in admitted)


def test_cleanup_failures_do_not_mask_success_or_skip_remaining_legs(tmp_path, monkeypatch, admitted):
    class Process:
        pid = 456
        returncode = 7
        def communicate(self, prompt=None):
            return None, None
    def bad_release(*a):
        raise OSError("release diagnostic")
    def bad_roster(*a):
        raise OSError("roster diagnostic")
    monkeypatch.setattr(worker.capacity, "release_spawn", bad_release)
    monkeypatch.setattr(worker.roster, "remove_record", bad_roster)
    assert worker.supervise(tmp_path, "probe", ASSIGNMENT, ["unused"], provider="codex",
                             mode="persona", popen=lambda *a, **kw: Process()) == 7
    assert any(isinstance(e, tuple) and e[0] == "END" for e in admitted)


def test_registered_start_worker_request_runs_shared_launch_and_returns_result(tmp_path, monkeypatch, admitted):
    from pathlib import Path
    from coordinator_core.ops import codex_runner as operations
    from coordinator_core.codex_runner.__main__ import run_worker
    from coordinator_core.codex_runner.contract import RunRequest
    repo = tmp_path / "assigned-checkout"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/codex/assigned")
    task = repo / "task.txt"
    task.write_text("Produce the assigned evidence.")
    fake = tmp_path / "native-codex.py"
    fake.write_text("import sys\nprint(sys.stdin.read())\n")
    assignment = {**ASSIGNMENT, "source": "pm", "branch": "codex/assigned",
                  "quiet_box": True, "quiet_max_sessions": 2, "evidence": "PM task reference"}
    params = {"mode": "worker", "repo_root": str(repo), "task_file": str(task), "assignment": assignment}
    with monkeypatch.context() as startup:
        startup.setattr(operations.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(pid=999))
        started = operations._codex_runner_start(params)
    monkeypatch.setattr(worker.peer_roster, "build_roster", lambda *a, **kw: [object()])
    monkeypatch.setattr(worker, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(worker, "resolve_codex", lambda: [sys.executable, str(fake)])
    result = run_worker(Path(started["run_dir"]))
    assert result.status == "ok" and result.codex_pid > 0
    assert "Produce the assigned evidence." in result.output
    assert worker.POLICY in result.output.replace("\r\n", "\n")
    assert operations._codex_runner_result({"run_id": started["run_id"]})["output"] == result.output
    with pytest.raises(ValueError, match="installed Codex defaults"):
        RunRequest.from_dict({**params, "provider": "google"})
