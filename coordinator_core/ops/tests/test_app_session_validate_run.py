"""Tests for the detached ``app_session.validate`` runner: fake driver, ``sys.executable`` fake build and server."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import _app_session_build_guard as guard
from coordinator_core.ops import _app_session_validate_contract as contract
from coordinator_core.ops import _app_session_validate_run as runner
from coordinator_core.ops import app_session

SECRET = "s3cr3t-value-XYZZY-91"
KEY = "web"
HANDLE_KEY = "web@validate"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


class FakeDriver:
    def __init__(self, raise_on_sweep: bool = False, defect_path: str = "") -> None:
        self.raise_on_sweep = raise_on_sweep
        self.defect_path = defect_path
        self.calls = []
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True
        return None

    def sweep_route(self, base_url, route, width, height, headers, screenshot_path):
        if self.raise_on_sweep:
            raise RuntimeError("sweep exploded")
        self.calls.append((base_url, route.name, width, height, dict(headers)))
        Path(screenshot_path).write_bytes(b"png")
        bad = route.path == self.defect_path
        return contract.RouteResult(
            route=route.name,
            path=route.path,
            width=width,
            status=200,
            console_errors=["boom"] if bad else [],
            failed_responses=[],
            broken_images=[],
            overflow_px=0,
            overflow_culprit=None,
            screenshot=str(screenshot_path),
            error=None,
        )


def _ok_probe():
    return contract.BrowserAvailability(ok=True)


def _no_browser_probe():
    return contract.BrowserAvailability(
        ok=False, reason="chromium not installed", install_hint=f"{sys.executable} -m playwright install chromium"
    )


class Setup:
    def __init__(self, repo: Path, run_dir: Path, port: int, lock: Path) -> None:
        self.repo = repo
        self.run_dir = run_dir
        self.port = port
        self.lock = lock


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "/") + '"'


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo = tmp_path / "consuming-repo"
    repo.mkdir()
    common = repo / ".git"
    common.mkdir()
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda root: common)
    port = _free_port()
    (repo / "build_ok.py").write_text("print('built')\n", encoding="utf-8")
    (repo / "build_fail.py").write_text("import sys\nprint('nope')\nsys.exit(3)\n", encoding="utf-8")
    (repo / "server.py").write_text(
        "import http.server, sys\n"
        "http.server.test(HandlerClass=http.server.SimpleHTTPRequestHandler,"
        " port=int(sys.argv[1]), bind='127.0.0.1')\n",
        encoding="utf-8",
    )
    (repo / "never.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock = guard.lock_path(common / "coordinator-app-sessions", repo)
    return Setup(repo, run_dir, port, lock)


def _prepare(env: Setup, monkeypatch, *, build="build_ok.py", start=None, ready_timeout=20):
    start_cmd = f"{_quote(sys.executable)} server.py {env.port}" if start is None else start
    build_cmd = f"{_quote(sys.executable)} {build}"
    body = (
        "app_session:\n"
        f"  {KEY}:\n"
        f"    port: {env.port}\n"
        f"    build: {build_cmd}\n"
        f"    start: {start_cmd}\n"
        f"    ready_timeout_s: {ready_timeout}\n"
        "    build_timeout_s: 30\n"
        "    routes:\n"
        "      index: /\n"
        "      other: /server.py\n"
        "    env:\n"
        f"      APP_SECRET: {SECRET}\n"
    )
    (env.repo / "coordinator.local.md").write_text(f"---\n{body}---\n", encoding="utf-8")
    target = app_session._resolve_target_config(str(env.repo), KEY)
    config = contract.parse_validate_config(target)
    assert isinstance(config, contract.ValidateConfig), config
    spec = contract.RunSpec(
        key=KEY,
        variant=None,
        repo_root=str(env.repo),
        worktree=str(env.repo),
        config=contract.config_spec_dict(config),
        lock_path=str(env.lock),
        handle_key=HANDLE_KEY,
        created_at="2026-10-22T00:00:00+00:00",
    )
    contract.write_run_spec(env.run_dir, spec)
    # the op holds the lock under its own pid before spawning the runner
    res = guard.acquire_tree_lock(env.lock.parent, env.repo, 999999, 1.0, lambda p, e: False)
    assert res.acquired
    return config


def _all_text(run_dir: Path) -> str:
    chunks = []
    for p in run_dir.rglob("*"):
        if p.is_file():
            chunks.append(p.read_bytes().decode("utf-8", "replace"))
    return "\n".join(chunks)


def _assert_torn_down(env: Setup) -> None:
    assert not env.lock.exists()
    assert app_session._read_handle(str(env.repo), HANDLE_KEY) is None
    assert not _port_open(env.port)


def test_clean_run_writes_report_pngs_and_tears_down(env, monkeypatch):
    _prepare(env, monkeypatch)
    driver = FakeDriver()
    result = runner.run(env.run_dir, driver_factory=lambda: driver, probe=_ok_probe)

    assert result["ok"] is True and result["defect_count"] == 0
    assert contract.read_result(env.run_dir) == result
    report = json.loads((env.run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["env_keys"] == ["APP_SECRET"]
    assert len(report["results"]) == 4
    assert sorted(p.name for p in (env.run_dir / "screenshots").iterdir()) == [
        "index-1440.png",
        "index-390.png",
        "other-1440.png",
        "other-390.png",
    ]
    assert [c[2] for c in driver.calls] == [1440, 390, 1440, 390]
    assert driver.entered and driver.exited
    assert report["build"]["ran"] is True and report["build"]["exit_code"] == 0
    _assert_torn_down(env)


def test_defects_are_counted_and_do_not_fail_the_run(env, monkeypatch):
    _prepare(env, monkeypatch)
    result = runner.run(env.run_dir, driver_factory=lambda: FakeDriver(defect_path="/"), probe=_ok_probe)
    assert result["ok"] is True
    assert result["defect_count"] == 2
    _assert_torn_down(env)


def test_secret_env_value_appears_in_no_file_under_run_dir(env, monkeypatch):
    _prepare(env, monkeypatch)
    runner.run(env.run_dir, driver_factory=FakeDriver, probe=_ok_probe)
    assert SECRET not in _all_text(env.run_dir)
    assert SECRET not in (env.run_dir / "spec.json").read_text(encoding="utf-8")


def test_chromium_absent_stops_at_browser_before_any_build(env, monkeypatch):
    _prepare(env, monkeypatch)
    result = runner.run(env.run_dir, driver_factory=FakeDriver, probe=_no_browser_probe)
    assert result["ok"] is False and result["stage"] == "browser"
    assert "playwright install chromium" in result["install_hint"]
    assert sys.executable in result["install_hint"]
    assert not (env.run_dir / "build.log").exists()
    assert not (env.run_dir / "server.log").exists()
    _assert_torn_down(env)


def test_failed_build_reports_build_log_and_never_starts_the_server(env, monkeypatch):
    _prepare(env, monkeypatch, build="build_fail.py")
    result = runner.run(env.run_dir, driver_factory=FakeDriver, probe=_ok_probe)
    assert result["ok"] is False and result["stage"] == "build"
    assert result["build_log"] == str(env.run_dir / "build.log")
    assert "nope" in (env.run_dir / "build.log").read_text(encoding="utf-8")
    assert not (env.run_dir / "server.log").exists()
    _assert_torn_down(env)


def test_ready_timeout_reports_ready_and_stops_the_server(env, monkeypatch):
    never = f"{_quote(sys.executable)} never.py"
    _prepare(env, monkeypatch, start=never, ready_timeout=1)
    result = runner.run(env.run_dir, driver_factory=FakeDriver, probe=_ok_probe)
    assert result["ok"] is False and result["stage"] == "ready"
    _assert_torn_down(env)


def test_server_that_exits_early_reports_start(env, monkeypatch):
    quick = f"{_quote(sys.executable)} -c pass"
    _prepare(env, monkeypatch, start=quick, ready_timeout=10)
    result = runner.run(env.run_dir, driver_factory=FakeDriver, probe=_ok_probe)
    assert result["ok"] is False and result["stage"] == "start"
    _assert_torn_down(env)


def test_teardown_still_runs_when_the_sweep_raises(env, monkeypatch):
    _prepare(env, monkeypatch)
    result = runner.run(
        env.run_dir, driver_factory=lambda: FakeDriver(raise_on_sweep=True), probe=_ok_probe
    )
    assert result["ok"] is False and result["stage"] == "sweep"
    assert "sweep exploded" in result["error"]
    _assert_torn_down(env)


def test_result_json_is_written_after_teardown(env, monkeypatch):
    _prepare(env, monkeypatch)
    seen = {}
    real_release = guard.release_tree_lock

    def spy(path, pid):
        seen["result_before_release"] = (env.run_dir / contract.RESULT_FILE).exists()
        return real_release(path, pid)

    monkeypatch.setattr(runner.guard, "release_tree_lock", spy)
    runner.run(env.run_dir, driver_factory=FakeDriver, probe=_ok_probe)
    assert seen["result_before_release"] is False
    assert (env.run_dir / contract.RESULT_FILE).exists()


def test_lock_is_restamped_to_the_runner_pid_during_the_run(env, monkeypatch):
    _prepare(env, monkeypatch)
    stamped = {}

    class Spy(FakeDriver):
        def sweep_route(self, *a, **kw):
            stamped["pid"] = json.loads(env.lock.read_text(encoding="utf-8"))["pid"]
            return super().sweep_route(*a, **kw)

    runner.run(env.run_dir, driver_factory=Spy, probe=_ok_probe)
    assert stamped["pid"] == os.getpid()


def test_handle_is_repointed_to_the_server_pid_while_running(env, monkeypatch):
    _prepare(env, monkeypatch)
    seen = {}

    class Spy(FakeDriver):
        def sweep_route(self, *a, **kw):
            seen["handle"] = app_session._read_handle(str(env.repo), HANDLE_KEY)
            return super().sweep_route(*a, **kw)

    runner.run(env.run_dir, driver_factory=Spy, probe=_ok_probe)
    assert seen["handle"]["pid"] != os.getpid()
    assert seen["handle"]["key"] == HANDLE_KEY


def test_missing_target_config_reports_config_stage(env, monkeypatch):
    _prepare(env, monkeypatch)
    (env.repo / "coordinator.local.md").write_text("---\napp_session:\n  other:\n    port: 1\n---\n", encoding="utf-8")
    result = runner.run(env.run_dir, driver_factory=FakeDriver, probe=_ok_probe)
    assert result["ok"] is False and result["stage"] == "config"
    _assert_torn_down(env)


def test_main_parses_run_dir_and_writes_result(env, monkeypatch):
    _prepare(env, monkeypatch)
    assert runner.main(["--run-dir", str(env.run_dir)], driver_factory=FakeDriver, probe=_ok_probe) == 0
    assert contract.read_result(env.run_dir)["ok"] is True
