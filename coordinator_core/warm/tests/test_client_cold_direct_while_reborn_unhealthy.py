"""A reborn engine that leaves a call unanswered inside its restart backoff goes cold-direct.

Drives the real `try_warm_dispatch` with a fake pipe under a tmp runtime base and a restart log
seeded by `hang_restart._append`; no live warm engine is touched.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, client, hang_restart
from coordinator_core.warm.engine_root import current_engine_clone

_TOKEN = "faketoken"
_READ_ONLY = client._RECONCILE_POLL_METHOD
_MUTATING = "no.such.op"


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path))
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: _TOKEN)
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    monkeypatch.setattr(client, "_unhealthy_mark", None)
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: 0.04)
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda *a, **k: True)
    svc = breadcrumb.svc_dir(current_engine_clone())
    svc.mkdir(parents=True)
    (svc / breadcrumb.BREADCRUMB_FILENAME).write_text(
        json.dumps({"pipe": "p", "pid": 424242, "stable_pid_start_epoch": 1}), encoding="utf-8"
    )
    monkeypatch.setattr(hang_restart, "exit_server", lambda pid: None)
    opened: list = []

    def _refuse(name):
        opened.append(name)
        raise TimeoutError

    monkeypatch.setattr(client, "_open_pipe", _refuse)
    return svc, opened


def _msg(method: str) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}


def _seed_restart(svc: Path, age_secs: float) -> None:
    hang_restart._append(
        svc / hang_restart.HANG_RESTART_LOG_FILENAME, time.time() - age_secs, 111, "restart", 8, 60.0, []
    )


def test_log_filename_matches_hang_restart() -> None:
    assert client._HANG_RESTART_LOG_FILENAME == hang_restart.HANG_RESTART_LOG_FILENAME


def test_unanswered_call_inside_backoff_sends_the_next_read_only_call_cold_direct(engine) -> None:
    svc, opened = engine
    _seed_restart(svc, 10.0)
    assert client.try_warm_dispatch(_msg(_READ_ONLY)) is None
    assert len(opened) == 1
    record = json.loads(client._unanswered_path().read_text(encoding="utf-8"))
    assert record["unhealthy_until"] > time.time() + 30.0

    started = time.monotonic()
    assert client.try_warm_dispatch(_msg(_READ_ONLY)) is None
    assert len(opened) == 1, "the read-only call never connected"
    assert time.monotonic() - started < 1.0


def test_the_mark_survives_in_a_fresh_process_via_the_unanswered_file(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, opened = engine
    _seed_restart(svc, 10.0)
    client.try_warm_dispatch(_msg(_READ_ONLY))
    monkeypatch.setattr(client, "_unhealthy_mark", None)
    client.try_warm_dispatch(_msg(_READ_ONLY))
    assert len(opened) == 1


def test_a_mutating_call_still_connects_while_unhealthy(engine) -> None:
    svc, opened = engine
    _seed_restart(svc, 10.0)
    client.try_warm_dispatch(_msg(_READ_ONLY))
    assert len(opened) == 1
    client.try_warm_dispatch(_msg(_MUTATING))
    assert len(opened) == 2


def test_no_restart_in_the_log_leaves_the_engine_healthy(engine) -> None:
    _svc, opened = engine
    client.try_warm_dispatch(_msg(_READ_ONLY))
    client.try_warm_dispatch(_msg(_READ_ONLY))
    assert len(opened) == 2
    assert "unhealthy_until" not in json.loads(client._unanswered_path().read_text(encoding="utf-8"))


def test_a_restart_older_than_its_backoff_leaves_the_engine_healthy(engine) -> None:
    svc, opened = engine
    _seed_restart(svc, hang_restart.RESTART_MIN_INTERVAL_SECS + 30.0)
    client.try_warm_dispatch(_msg(_READ_ONLY))
    client.try_warm_dispatch(_msg(_READ_ONLY))
    assert len(opened) == 2


def test_an_expired_mark_connects_again(engine) -> None:
    _svc, opened = engine
    client._unanswered_path().write_text(
        json.dumps({"token": _TOKEN, "count": 1, "last": time.time(), "unhealthy_until": time.time() - 1.0}),
        encoding="utf-8",
    )
    client.try_warm_dispatch(_msg(_READ_ONLY))
    assert len(opened) == 1


def test_another_engine_token_is_not_marked(engine) -> None:
    _svc, opened = engine
    client._unanswered_path().write_text(
        json.dumps({"token": "other", "count": 1, "last": time.time(), "unhealthy_until": time.time() + 60.0}),
        encoding="utf-8",
    )
    client.try_warm_dispatch(_msg(_READ_ONLY))
    assert len(opened) == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_pre_connect_check_imports_neither_hang_restart_nor_the_op_registry() -> None:
    code = (
        "import sys, time, json, tempfile, os;"
        "d = tempfile.mkdtemp(); os.environ['COORDINATOR_WARM_RUNTIME_BASE'] = d;"
        "from coordinator_core.warm import client;"
        "client._unanswered_path().parent.mkdir(parents=True, exist_ok=True);"
        "client._unanswered_path().write_text(json.dumps({'token': 't', 'count': 1, 'last': time.time(),"
        " 'unhealthy_until': time.time() + 60}));"
        "client._cold_direct_while_unhealthy('t', {'jsonrpc': '2.0', 'id': 1, 'method': client._RECONCILE_POLL_METHOD, 'params': {}});"
        "print(int('coordinator_core.warm.hang_restart' in sys.modules),"
        " len([m for m in sys.modules if m.startswith('coordinator_core.ops')]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.stdout.split() == ["0", "0"], result.stderr
