"""Tests for the cheap stages of ``app_session.validate`` (config, browser probe,
guard, port, runner spawn). The runner itself is not exercised here."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.ops import app_session
from coordinator_core.ops import _app_session_validate_contract as contract
from coordinator_core.ops._app_session_validate_contract import BrowserAvailability

SECRET = "s3cr3t-value-xyz"


class _FakeProc:
    pid = os.getpid()


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "consuming-repo"
    repo.mkdir()
    common = repo / ".git"
    common.mkdir()
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda root: common)
    monkeypatch.setattr(app_session, "_probe_browser_import", lambda: BrowserAvailability(True))
    return repo


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(repo: Path, port: int, extra: str = "") -> None:
    body = (
        "app_session:\n"
        "  web:\n"
        f"    port: {port}\n"
        "    build: echo build\n"
        "    start: echo start\n"
        "    routes:\n"
        "      index: /\n"
        "    env:\n"
        f"      TOKEN: {SECRET}\n"
        f"{extra}"
    )
    (repo / "coordinator.local.md").write_text(f"---\n{body}---\n", encoding="utf-8")


def _fake_spawn(calls):
    def spawn(argv, cwd, log_path):
        calls.append((argv, cwd, log_path))
        return _FakeProc()

    return spawn


def _validate(repo, **params):
    return app_session._validate({"key": "web", "repo_root": str(repo), **params})


def test_started_shape_and_spec_has_no_env_value(repo, monkeypatch):
    _config(repo, _free_port())
    calls = []
    monkeypatch.setattr(app_session, "_spawn_runner", _fake_spawn(calls))
    res = _validate(repo)
    assert res["ok"] is True and res["started"] is True and res["configured"] is True
    run_dir = Path(res["run_dir"])
    assert res["runner_pid"] == os.getpid()
    assert res["wait_budget_s"] > 0
    assert calls and calls[0][0][-2:] == ["--run-dir", str(run_dir)]
    spec_text = (run_dir / contract.SPEC_FILE).read_text(encoding="utf-8")
    assert SECRET not in spec_text
    assert "TOKEN" in json.loads(spec_text)["config"]["env"]
    assert SECRET not in json.dumps(res)
    handles = app_session._list_handles(str(repo))
    assert [h["key"] for h in handles] == ["web@validate"]
    assert handles[0]["pid"] == os.getpid()


def test_not_configured(repo):
    res = _validate(repo)
    assert res["ok"] is True and res["configured"] is False


def test_missing_key(repo):
    assert app_session._validate({"repo_root": str(repo)})["ok"] is False


def test_config_error_refuses(repo, monkeypatch):
    monkeypatch.setattr(app_session, "_spawn_runner", _fake_spawn([]))
    _config(repo, _free_port())
    res = _validate(repo, variant="nope")
    assert (res["ok"], res["stage"]) == (False, "config")


def test_browser_import_leg(repo, monkeypatch):
    _config(repo, _free_port())
    hint = f"{sys.executable} -m pip install playwright"
    monkeypatch.setattr(
        app_session,
        "_probe_browser_import",
        lambda: BrowserAvailability(False, "playwright not importable", hint),
    )
    calls = []
    monkeypatch.setattr(app_session, "_spawn_runner", _fake_spawn(calls))
    res = _validate(repo)
    assert (res["ok"], res["stage"]) == (False, "browser")
    assert sys.executable in res["install_hint"]
    assert calls == []


def test_second_validate_refused_while_lock_held(repo, monkeypatch):
    _config(repo, _free_port())
    calls = []
    monkeypatch.setattr(app_session, "_spawn_runner", _fake_spawn(calls))
    assert _validate(repo)["started"] is True
    res = _validate(repo)
    assert (res["ok"], res["stage"]) == (False, "guard")
    assert str(os.getpid()) in res["error"]
    assert len(calls) == 1


def test_stale_lock_is_reclaimed(repo, monkeypatch):
    _config(repo, _free_port())
    monkeypatch.setattr(app_session, "_spawn_runner", _fake_spawn([]))
    assert _validate(repo)["started"] is True
    lock = next(app_session._handle_dir(str(repo)).glob("validate-*.lock"))
    rec = json.loads(lock.read_text(encoding="utf-8"))
    rec["pid"] = 2**22 + 12345
    rec["start_epoch"] = 1
    lock.write_text(json.dumps(rec), encoding="utf-8")
    assert _validate(repo)["started"] is True


def test_live_launch_handle_refuses_and_releases_lock(repo, monkeypatch):
    _config(repo, _free_port())
    calls = []
    monkeypatch.setattr(app_session, "_spawn_runner", _fake_spawn(calls))
    app_session._write_handle(
        str(repo),
        "desk",
        {
            "key": "desk",
            "pid": os.getpid(),
            "start_epoch": app_session._create_epoch(os.getpid()),
            "cwd": str(repo),
        },
    )
    res = _validate(repo)
    assert (res["ok"], res["stage"]) == (False, "guard")
    assert "desk" in res["error"]
    assert list(app_session._handle_dir(str(repo)).glob("validate-*.lock")) == []
    assert calls == []


def test_build_isolated_ignores_launch_handle(repo, monkeypatch):
    _config(repo, _free_port(), extra="    build_isolated: true\n")
    monkeypatch.setattr(app_session, "_spawn_runner", _fake_spawn([]))
    app_session._write_handle(
        str(repo),
        "desk",
        {
            "key": "desk",
            "pid": os.getpid(),
            "start_epoch": app_session._create_epoch(os.getpid()),
            "cwd": str(repo),
        },
    )
    assert _validate(repo)["started"] is True


def test_port_already_accepting_refuses(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(app_session, "_spawn_runner", _fake_spawn(calls))
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        _config(repo, srv.getsockname()[1])
        res = _validate(repo)
    assert (res["ok"], res["stage"]) == (False, "start")
    assert calls == []
    assert list(app_session._handle_dir(str(repo)).glob("validate-*.lock")) == []


def test_spawn_failure_releases_lock(repo, monkeypatch):
    _config(repo, _free_port())

    def boom(*_a, **_k):
        raise OSError("no exec")

    monkeypatch.setattr(app_session, "_spawn_runner", boom)
    res = _validate(repo)
    assert (res["ok"], res["stage"]) == (False, "start")
    assert list(app_session._handle_dir(str(repo)).glob("validate-*.lock")) == []


def test_ac11_handler_process_time_under_bar_with_real_spawn(repo, monkeypatch):
    _config(repo, _free_port())
    monkeypatch.setattr(
        contract, "runner_argv", lambda run_dir: [sys.executable, "-c", "pass"]
    )
    start = time.process_time()
    res = _validate(repo)
    elapsed = time.process_time() - start
    assert res["started"] is True
    assert elapsed < 0.5, f"handler process time {elapsed:.3f}s"
