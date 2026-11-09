from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.worker_contracts import PROVIDERS, persona_prompt, resolve
from coordinator_core.worker_contracts.__main__ import main
from coordinator_core.worker_contracts import worker


def test_all_six_provider_configurations_resolve_without_fallback(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"version": 1, "providers": {
        p: {"opus": {"model": f"configured-{p}", "effort": "configured-effort",
                       "argv": ["provider", "{model}", "{effort}"]}} for p in PROVIDERS}}))
    for provider in PROVIDERS:
        found = resolve(path, provider, "opus")
        assert found.argv == ("provider", f"configured-{provider}", "configured-effort")
        with pytest.raises(ValueError, match="configure reviewed mapping"):
            resolve(path, provider, "mythos")


@pytest.mark.parametrize("value", [[], {"version": 2}, {"version": 1, "providers": {"openai": {"opus": []}}}])
def test_malformed_configuration_fails_before_dispatch(tmp_path, value):
    path = tmp_path / "map.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        resolve(path, "openai", "opus")


def test_real_cli_dispatches_same_definition_to_configured_provider(tmp_path, monkeypatch):
    persona = tmp_path / "reviewer.md"
    persona.write_text("---\nname: configured-reviewer\nmodel: opus\n---\nUnique role instructions.")
    task = tmp_path / "task.txt"
    task.write_text("Review changed code.")
    sink = tmp_path / "result.txt"
    fake = tmp_path / "provider.py"
    fake.write_text("import pathlib,sys\npathlib.Path(sys.argv[1]).write_text(sys.stdin.read())\n")
    config = tmp_path / "map.json"
    config.write_text(json.dumps({"version": 1, "providers": {"openai": {"opus": {
        "model": "explicit-test-model", "effort": "high", "argv": [sys.executable, str(fake), str(sink)]}}}}))
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"session_id": "sid", "run": "persona", "declared_peak_mb": 64}))
    monkeypatch.setattr(worker.slot_grant, "check_slot_grant", lambda *a, **kw: (True, {}))
    monkeypatch.setattr(worker.capacity, "admit_before_spawn", lambda *a, **kw: SimpleNamespace(admitted=True, peak_gib=.0625))
    monkeypatch.setattr(worker.capacity, "release_spawn", lambda *a: None)
    monkeypatch.setattr(worker.roster, "write_record", lambda *a, **kw: None)
    monkeypatch.setattr(worker.roster, "remove_record", lambda *a: None)
    monkeypatch.setattr(worker.admission_post, "post", lambda *a, **kw: {"delivered": True})
    assert main(["--tier-map", str(config), "--provider", "openai", "--persona", str(persona),
                 "--task-file", str(task), "--repo", str(tmp_path), "--assignment", str(assignment)]) == 0
    text = sink.read_text()
    assert persona.read_text() in text
    assert "Review changed code." in text
    with pytest.raises(ValueError, match="opus tier"):
        persona_prompt(persona, "review", "sonnet")


def test_worker_launch_keeps_sandbox_defaults_and_roster_cleanup(tmp_path, monkeypatch):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/codex/assigned\n")
    seen = {}
    monkeypatch.setattr(worker, "resolve_codex", lambda: ["codex-native"])
    monkeypatch.setattr(worker, "preflight_auth", lambda **kwargs: None)
    monkeypatch.setattr(worker, "_child_env", lambda _: {"SAFE": "yes"})
    monkeypatch.setattr(worker.slot_grant, "check_slot_grant", lambda *a, **kw: (True, {}))
    monkeypatch.setattr(worker.peer_roster, "build_roster", lambda *a, **kw: [object()])
    monkeypatch.setattr(worker.capacity, "admit_before_spawn", lambda *a, **kw: SimpleNamespace(admitted=True, peak_gib=1))
    monkeypatch.setattr(worker.capacity, "register_spawn", lambda *a: None)
    monkeypatch.setattr(worker.capacity, "release_spawn", lambda *a: None)
    monkeypatch.setattr(worker.admission_post, "post", lambda *a, **kw: {"delivered": True})
    monkeypatch.setattr(worker.roster, "write_record", lambda *args, **kwargs: seen.update(roster=args))
    monkeypatch.setattr(worker.roster, "remove_record", lambda pid: seen.update(removed=pid))
    class Process:
        pid = 123
        returncode = 0
        def communicate(self, prompt=None):
            seen["prompt"] = prompt
        def kill(self):
            seen["killed"] = True
    def spawn(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return Process()
    assignment = {"source": "pm", "branch": "codex/assigned", "quiet_box": True,
                  "session_id": "owner", "run": "codex-worker", "declared_peak_mb": 1024,
                  "quiet_max_sessions": 2, "evidence": "PM explicit assignment"}
    assert worker.launch(tmp_path, "Do the assigned work", assignment, popen=spawn) == 0
    assert seen["removed"] == 123
    assert seen["roster"][2] == "worker"
    assert seen["argv"] == ["codex-native", "--ask-for-approval", "on-request", "exec",
                            "--sandbox", "workspace-write", "-"]
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["env"]["COORDINATOR_AGENT_PROVIDER"] == "codex"
    assert "--worker" not in seen["argv"]
    assert "Do the assigned work" in seen["prompt"]
    assignment["quiet_box"] = False
    with pytest.raises(ValueError, match="quiet_box"):
        worker.launch(tmp_path, "task", assignment, popen=spawn)


def test_worker_admission_denials_prevent_any_child(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/codex/assigned")
    assignment = {"source": "pm", "branch": "codex/assigned", "quiet_box": True,
                  "session_id": "owner", "run": "codex-worker", "declared_peak_mb": 1024,
                  "quiet_max_sessions": 1, "evidence": "PM explicit assignment"}
    def forbidden(*a, **kw):
        pytest.fail("must not spawn on admission refusal")
    monkeypatch.setattr(worker.peer_roster, "build_roster", lambda *a, **kw: [object()])
    monkeypatch.setattr(worker, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(worker, "resolve_codex", lambda: ["codex-native"])
    monkeypatch.setattr(worker.slot_grant, "check_slot_grant", lambda *a, **kw: (False, None))
    with pytest.raises(ValueError, match="live Group EM"):
        worker.launch(tmp_path, "task", assignment, popen=forbidden)
    monkeypatch.setattr(worker.slot_grant, "check_slot_grant", lambda *a, **kw: (True, {}))
    monkeypatch.setattr(worker.peer_roster, "build_roster", lambda *a, **kw: [object(), object()])
    with pytest.raises(ValueError, match="not quiet"):
        worker.launch(tmp_path, "task", assignment, popen=forbidden)
    monkeypatch.setattr(worker.peer_roster, "build_roster", lambda *a, **kw: [object()])
    monkeypatch.setattr(worker, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(worker, "resolve_codex", lambda: ["codex-native"])
    monkeypatch.setattr(worker.capacity, "admit_before_spawn", lambda *a, **kw: SimpleNamespace(admitted=False, reason="HOLD"))
    with pytest.raises(ValueError, match="HOLD"):
        worker.launch(tmp_path, "task", assignment, popen=forbidden)


def test_registered_runner_persona_mode_preserves_definition_and_returns_output(tmp_path, monkeypatch):
    from coordinator_core.codex_runner.contract import RunRequest
    from coordinator_core.codex_runner.__main__ import run_worker
    persona = tmp_path / "persona.md"
    persona.write_text("---\nname: staff-reviewer\nmodel: opus\n---\nSame role file.")
    task = tmp_path / "task.txt"
    task.write_text("Assigned review.")
    command = tmp_path / "fake.py"
    command.write_text("import sys\nprint(sys.stdin.read())\n")
    config = tmp_path / "map.json"
    config.write_text(json.dumps({"version": 1, "providers": {"openai": {"opus": {
        "model": "fixture", "effort": "high", "argv": [sys.executable, str(command)]}}}}))
    assignment = {"session_id": "parent", "run": "review", "declared_peak_mb": 128}
    request = RunRequest(mode="persona", repo_root=str(tmp_path), persona=str(persona), task_file=str(task),
                         tier_map=str(config), provider="openai", assignment=assignment)
    run_dir = tmp_path / "persona-run"
    run_dir.mkdir()
    (run_dir / "request.json").write_text(json.dumps(request.to_dict()))
    monkeypatch.setattr(worker.slot_grant, "check_slot_grant", lambda *a, **kw: (True, {}))
    monkeypatch.setattr(worker.capacity, "admit_before_spawn", lambda *a, **kw: SimpleNamespace(admitted=True, peak_gib=.125))
    monkeypatch.setattr(worker.capacity, "release_spawn", lambda *a: None)
    monkeypatch.setattr(worker.roster, "write_record", lambda *a, **kw: None)
    monkeypatch.setattr(worker.roster, "remove_record", lambda *a: None)
    monkeypatch.setattr(worker.admission_post, "post", lambda *a, **kw: {"delivered": True})
    result = run_worker(run_dir)
    assert result.status == "ok"
    assert "Same role file." in result.output
    assert "Assigned review." in result.output
    assert "You are a Codex worker" not in result.output
    assert json.loads((run_dir / "result.json").read_text())["output"] == result.output


@pytest.mark.parametrize("head", ["ref: refs/heads/main", "ref: refs/heads/master", "abcdef"])
def test_worker_rejects_shared_default_or_detached_branch(tmp_path, head):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(head)
    with pytest.raises(ValueError):
        worker.branch(tmp_path)
