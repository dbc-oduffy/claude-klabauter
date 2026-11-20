"""A fire that ended on an engine defect is re-fired in place by `refire`; a live fire never is."""
import hashlib
from pathlib import Path

import pytest

from coordinator_core.ops.workflow_fire import fire


@pytest.fixture
def target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "flow.mjs"
    script.write_text("// emitted flow\n", encoding="utf-8")
    registry = tmp_path / "fires"
    monkeypatch.setattr(fire, "_resolve_target_repo", lambda *a: str(tmp_path))
    monkeypatch.setattr(fire, "_registry_dir", lambda *a: registry)
    monkeypatch.setattr(fire, "resolve_plugin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(fire, "_publish_lag_message", lambda *a: None)
    monkeypatch.setattr(fire, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(fire.time, "sleep", lambda *a: None)
    started = []

    class Child:
        pid = 4242

        def __init__(self, *a, **kw) -> None:
            started.append(kw)

        def poll(self) -> None:
            return None

    monkeypatch.setattr(fire.subprocess, "Popen", Child)
    return script, hashlib.sha256(script.read_bytes()).hexdigest(), started


def _end(previous: dict) -> None:
    path = fire._record_path(fire._registry_dir(), previous["fire_id"])
    fire._write_record(path, {**previous, "state": "exited", "execution_started": True, "exit_code": 1})


def test_terminal_match_names_the_refire_route(target) -> None:
    script, digest, started = target
    previous = fire.fire_workflow(str(script), expected=digest)
    _end(previous)
    again = fire.fire_workflow(str(script), expected=digest)
    assert again["already_fired"] and again["fire_id"] == previous["fire_id"]
    assert "refire: true" in again["refire_hint"]
    assert len(started) == 1


def test_refire_starts_a_new_fire_past_an_ended_one(target) -> None:
    script, digest, started = target
    previous = fire.fire_workflow(str(script), expected=digest)
    _end(previous)
    again = fire.fire_workflow(str(script), expected=digest, refire=True)
    assert again["fire_id"] != previous["fire_id"] and not again.get("already_fired")
    assert len(started) == 2


def test_refire_never_duplicates_a_live_fire(target) -> None:
    script, digest, started = target
    previous = fire.fire_workflow(str(script), expected=digest)
    again = fire.fire_workflow(str(script), expected=digest, refire=True)
    assert again["fire_id"] == previous["fire_id"] and again["already_fired"]
    assert "refire_hint" not in again
    assert len(started) == 1
