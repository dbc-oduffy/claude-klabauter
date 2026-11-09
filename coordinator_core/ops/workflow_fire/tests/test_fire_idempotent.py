"""One emitted script digest launches one driver, including concurrent callers."""
from concurrent.futures import ThreadPoolExecutor
import hashlib

import pytest

from coordinator_core.ops.workflow_fire import fire


@pytest.fixture
def target(tmp_path, monkeypatch):
    script = tmp_path / "flow.mjs"
    script.write_text("// emitted flow\n", encoding="utf-8")
    registry = tmp_path / "fires"
    monkeypatch.setattr(fire, "_resolve_target_repo", lambda *a: str(tmp_path))
    monkeypatch.setattr(fire, "_registry_dir", lambda *a: registry)
    monkeypatch.setattr(fire, "resolve_plugin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(fire, "_publish_lag_message", lambda *a: None)
    # This fixture's fake driver has no real OS pid; exercise idempotency
    # without making Windows tasklist pass through its mocked Popen.
    monkeypatch.setattr(fire, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(fire.time, "sleep", lambda *a: None)
    started = []

    class Child:
        pid = 4242

        def __init__(self, *a, **kw):
            started.append(kw)

        def poll(self):
            return None

    monkeypatch.setattr(fire.subprocess, "Popen", Child)
    return script, hashlib.sha256(script.read_bytes()).hexdigest(), started


def test_repeated_digest_returns_original_fire_without_spawning(target):
    script, digest, started = target
    first = fire.fire_workflow(str(script), expected=digest, notify_to="peer")
    second = fire.fire_workflow(str(script), expected=digest, notify_to="peer")
    assert second["fire_id"] == first["fire_id"] and second["already_fired"]
    assert len(started) == 1
    assert first["sha256"] == digest and first["notify_to"] == "peer"
    assert started[0]["cwd"] == str(script.parent)


def test_concurrent_fires_share_a_reservation(target):
    script, digest, started = target
    with ThreadPoolExecutor(max_workers=2) as pool:
        handles = list(pool.map(lambda _: fire.fire_workflow(str(script), expected=digest), range(2)))
    assert handles[0]["fire_id"] == handles[1]["fire_id"]
    assert len(started) == 1


def test_known_pre_execution_failure_can_be_fired_again(target):
    script, digest, started = target
    previous = fire.fire_workflow(str(script), expected=digest)
    path = fire._record_path(fire._registry_dir(), previous["fire_id"])
    fire._write_record(path, {**previous, "state": "exited", "execution_started": False})
    retried = fire.fire_workflow(str(script), expected=digest)
    assert retried["fire_id"] != previous["fire_id"] and len(started) == 2


def test_terminal_executed_driver_is_not_fired_twice(target):
    script, digest, started = target
    previous = fire.fire_workflow(str(script), expected=digest)
    path = fire._record_path(fire._registry_dir(), previous["fire_id"])
    fire._write_record(path, {**previous, "state": "exited", "execution_started": True})
    assert fire.fire_workflow(str(script), expected=digest)["already_fired"]
    assert len(started) == 1


def test_driver_prompt_quotes_windows_paths_with_spaces(monkeypatch):
    monkeypatch.setattr(fire.sys, "platform", "win32")
    prompt = fire.render_prompt(r"E:\Team Work\O'Brien\flow.mjs")
    assert "--params-file 'E:/Team Work/O''Brien/flow.mjs.terminal-params.json'" in prompt
    assert "PowerShell on Windows" in prompt and "\\" not in prompt


@pytest.mark.parametrize("exit_code", [0, 7])
def test_fast_terminal_child_keeps_digest_handle(target, monkeypatch, exit_code):
    script, digest, started = target
    class FastChild:
        pid = 4242
        def __init__(self, *a, **kw):
            started.append(kw)
        def poll(self):
            return exit_code
    monkeypatch.setattr(fire.subprocess, "Popen", FastChild)
    first = fire.fire_workflow(str(script), expected=digest)
    second = fire.fire_workflow(str(script), expected=digest)
    assert first["state"] == "exited" and first["exit_code"] == exit_code
    assert second["fire_id"] == first["fire_id"] and second["already_fired"]
    assert len(started) == 1


def test_expired_reservation_reports_execution_ambiguity_without_respawn(target):
    script, digest, started = target
    path = fire._record_path(fire._registry_dir(), "lost-spawner")
    fire._write_record(path, {"fire_id": "lost-spawner", "state": "pending", "reserved_at": 0,
                             "script_path": str(script), "sha256": digest})
    result = fire.fire_workflow(str(script), expected=digest)
    assert result["state"] == "exited" and result["execution_started"] is None
    assert "cannot be determined" in result["execution_note"]
    assert result["already_fired"] and not started


def test_cap_lock_never_routes_completion_messages(target, monkeypatch):
    from coordinator_core.agent_pipe import bridge
    script, digest, _ = target
    path = fire._record_path(fire._registry_dir(), "finished")
    fire._write_record(path, {"fire_id": "finished", "state": "exited", "notify_to": "offline-peer"})
    monkeypatch.setattr(bridge, "route", lambda *a: pytest.fail("transport inside cap lock"))
    assert fire.fire_workflow(str(script), expected=digest)["state"] == "running"
