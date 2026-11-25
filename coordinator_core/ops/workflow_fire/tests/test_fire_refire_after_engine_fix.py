"""A fire that ended on an engine defect is re-fired in place by `refire`; a live fire never is."""
import hashlib
from pathlib import Path

import pytest

from coordinator_core.hooks.tests.test_submit_engine_ask_real_emit import repo  # noqa: F401
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


def test_authority_refused_mint_keeps_its_run_and_fires_later_from_a_verified_em(repo, monkeypatch) -> None:
    import json

    from coordinator_core.invoke.__main__ import _dispatch_argv
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.workflow_fire import authority, headless
    from coordinator_core.ops.workflow_fire.op import _workflow_fire
    from coordinator_core.session import external_registry

    root = repo
    sid = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(authority, "caller_principal", lambda *a: authority.Principal(sid, "unknown", {}))
    monkeypatch.setattr(external_registry, "lookup", lambda s: None)
    started = []
    monkeypatch.setattr(headless, "start", lambda *a, **kw: started.append((a, kw)) or {"fire_id": "later"})
    params = {"ask": "add an offline fixture", "ask_source": "relayed", "caller": "grok-em",
              "session_id": sid, "cwd": str(root), "backend": "grok"}
    stdout, _, _ = _dispatch_argv(
        ["engine_ask.submit", json.dumps(params), "--repo", str(root), "--bare"], str(root.parent), allow_warm=False)
    refused = json.loads(stdout)
    assert "verified EM" in refused["fire_error"] and not started
    script = Path(refused["script_path"])
    assert script.is_file()
    assert hashlib.sha256(script.read_bytes()).hexdigest() == refused["sha256"]
    # A raw ask mints deferred: the refused mint's start args are its deferred script and the run's token marker;
    # the run dir and run script land only when that script runs.
    assert run_token._read_rec(root, refused["run_id"]) is not None

    monkeypatch.setattr(authority, "caller_principal", lambda *a: authority.Principal(sid, "em", {}))
    fire_params = {"script_path": str(script), "expected": refused["sha256"], "executor": "headless",
                   "backend": "grok", "caller_session_id": sid, "run_id": refused["run_id"]}
    if refused.get("fire_args"):
        fire_params["args"] = refused["fire_args"]
    fired = _workflow_fire(fire_params, root)
    assert fired["fire_id"] == "later" and len(started) == 1


def test_refire_never_duplicates_a_live_fire(target) -> None:
    script, digest, started = target
    previous = fire.fire_workflow(str(script), expected=digest)
    again = fire.fire_workflow(str(script), expected=digest, refire=True)
    assert again["fire_id"] == previous["fire_id"] and again["already_fired"]
    assert "refire_hint" not in again
    assert len(started) == 1
