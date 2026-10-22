"""Detached runner for ``app_session.validate``: build, start, ready-wait, sweep, report, teardown.

Entry point ``python -m coordinator_core.ops._app_session_validate_run --run-dir <dir>``.
The op spawns this after writing ``spec.json``; the runner owns the tree lock from
the moment it re-stamps it, and a ``finally`` always stops the server it started,
removes the ``<key>@validate`` handle, releases the lock, and writes ``result.json``
last (atomically), so a reader that sees the result sees a fully torn-down run.

Env values are re-resolved from coordinator.local.md in-process and handed only to
the build and server children; no file this module writes carries one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from coordinator_core.ops import _app_session_build_guard as guard
from coordinator_core.ops import _app_session_validate_contract as contract
from coordinator_core.ops import app_session
from coordinator_core.ops._app_session_validate_contract import (
    BrowserAvailability,
    BrowserDriver,
    RouteResult,
    ValidateConfig,
)
from coordinator_core.win_portability import no_console_creationflags

DriverFactory = Callable[[], BrowserDriver]
BrowserProbe = Callable[[], BrowserAvailability]

BUILD_LOG = "build.log"
SERVER_LOG = "server.log"
REPORT_FILE = "report.json"
SCREENSHOTS_DIR = "screenshots"

_READY_POLL_S = 0.25
_READY_REQUEST_TIMEOUT_S = 5
_TERM_GRACE_S = 5


class _Stage(Exception):
    """A stage failure carrying the result fields the runner reports."""

    def __init__(self, stage: str, error: str, **extra: Any) -> None:
        super().__init__(error)
        self.stage = stage
        self.error = error
        self.extra = extra


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, body: Dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
    os.replace(tmp, path)


_create_epoch = app_session._create_epoch


def _default_probe() -> BrowserAvailability:
    from coordinator_core.ops import _app_session_browser

    return _app_session_browser.probe_browser()


def _default_driver_factory() -> BrowserDriver:
    from coordinator_core.ops import _app_session_browser

    return _app_session_browser.PlaywrightDriver()


def _kill_tree(pid: int, expected_epoch: Optional[int]) -> None:
    """Terminate ``pid`` and its descendants; no-op when the create time no longer matches.

    The create-time re-check is the same recycled-PID guard ``app_session._teardown``
    applies before signalling.
    """
    try:
        import psutil

        proc = psutil.Process(pid)
        if expected_epoch is not None and int(proc.create_time()) != int(expected_epoch):
            return
        victims = proc.children(recursive=True) + [proc]
        for p in victims:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(victims, timeout=_TERM_GRACE_S)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(alive, timeout=_TERM_GRACE_S)
    except Exception:
        # Trap: best-effort; a missing psutil also lands here and leaves the tree running.
        pass


def _child_env(config: ValidateConfig) -> Dict[str, str]:
    return {**os.environ, **config.env}


def _run_build(config: ValidateConfig, worktree: str, run_dir: Path) -> Tuple[bool, Optional[int], Optional[str]]:
    """Run the configured build; returns (ran, exit_code, log path). Raises ``_Stage`` on failure."""
    if not config.build:
        return False, None, None
    log_path = run_dir / BUILD_LOG
    with open(log_path, "wb") as log:
        try:
            proc = subprocess.Popen(
                config.build,
                cwd=worktree,
                env=_child_env(config),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **no_console_creationflags(),
            )
        except OSError as exc:
            raise _Stage("build", f"build could not start: {type(exc).__name__}", build_log=str(log_path))
        epoch = _create_epoch(proc.pid)
        try:
            code = proc.wait(timeout=config.build_timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid, epoch)
            proc.wait()
            raise _Stage(
                "build",
                f"build exceeded build_timeout_s={config.build_timeout_s}",
                build_log=str(log_path),
            )
    if code != 0:
        raise _Stage("build", f"build exited {code}", build_log=str(log_path))
    return True, code, str(log_path)


def _start_server(
    config: ValidateConfig, worktree: str, run_dir: Path, repo_root: str, handle_key: str
) -> Tuple["subprocess.Popen[bytes]", Optional[int]]:
    if not config.start:
        raise _Stage("start", "no start command configured")
    log_path = run_dir / SERVER_LOG
    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = no_console_creationflags().get("creationflags", 0)
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "wb") as log:
        try:
            proc = subprocess.Popen(
                config.start,
                cwd=worktree,
                env=_child_env(config),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
        except OSError as exc:
            raise _Stage("start", f"server could not start: {type(exc).__name__}")
    epoch = _create_epoch(proc.pid)
    app_session._write_handle(
        repo_root,
        handle_key,
        {
            "key": handle_key,
            "pid": proc.pid,
            "start_epoch": epoch,
            "argv": config.start,
            "cwd": worktree,
            "launched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    return proc, epoch


def _wait_ready(config: ValidateConfig, server: "subprocess.Popen[bytes]") -> None:
    url = f"http://127.0.0.1:{config.port}{config.ready_path}"
    deadline = time.monotonic() + config.ready_timeout_s
    while True:
        code = server.poll()
        if code is not None:
            raise _Stage("start", f"server exited {code} before ready")
        req = urllib.request.Request(url, headers=dict(config.request_headers))
        try:
            with urllib.request.urlopen(req, timeout=_READY_REQUEST_TIMEOUT_S) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (urllib.error.URLError, OSError, ValueError):
            status = None
        if status is not None and status < 500:
            return
        if time.monotonic() >= deadline:
            raise _Stage("ready", f"no status below 500 from {config.ready_path} within ready_timeout_s={config.ready_timeout_s}")
        time.sleep(_READY_POLL_S)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "route"


def _sweep(
    config: ValidateConfig, run_dir: Path, driver_factory: DriverFactory
) -> Tuple[List[RouteResult], int, Path]:
    shots = run_dir / SCREENSHOTS_DIR
    shots.mkdir(parents=True, exist_ok=True)
    base_url = f"http://127.0.0.1:{config.port}"
    results: List[RouteResult] = []
    defects = 0
    with driver_factory() as driver:
        for route in config.routes:
            for width, height in contract.WIDTHS:
                shot = shots / f"{_safe_name(route.name)}-{width}.png"
                res = driver.sweep_route(
                    base_url, route, width, height, dict(config.request_headers), shot
                )
                results.append(res)
                if contract.is_defect(res, route):
                    defects += 1
    return results, defects, shots


class _Run:
    """State accumulated across stages so the report and the ``finally`` see the same facts."""

    def __init__(self, run_dir: Path, spec: contract.RunSpec, config: ValidateConfig) -> None:
        self.run_dir = run_dir
        self.spec = spec
        self.config = config
        self.started_at = _now()
        self.build_ran = False
        self.build_exit: Optional[int] = None
        self.build_log: Optional[str] = None
        self.results: List[RouteResult] = []
        self.defects = 0
        self.shots_dir = run_dir / SCREENSHOTS_DIR
        self.report_path: Optional[Path] = None

    def write_report(self) -> Path:
        body = contract.report_dict(
            key=self.spec.key,
            variant=self.spec.variant,
            started_at=self.started_at,
            finished_at=_now(),
            base_url=f"http://127.0.0.1:{self.config.port}",
            build_ran=self.build_ran,
            build_exit_code=self.build_exit,
            build_log=self.build_log,
            env_keys=list(self.config.env),
            results=self.results,
            defect_count=self.defects,
            screenshots_dir=str(self.shots_dir),
        )
        path = self.run_dir / REPORT_FILE
        _atomic_write_json(path, body)
        self.report_path = path
        return path


def run(
    run_dir: Path,
    *,
    driver_factory: Optional[DriverFactory] = None,
    probe: Optional[BrowserProbe] = None,
) -> Dict[str, Any]:
    """Execute one validate run and return the result dict (also written to ``result.json``).

    Trap: ``read_run_spec`` and the lock re-stamp run before the guarded block, so a
    corrupt ``spec.json`` raises with no ``result.json`` and the op-held lock unreleased.
    """
    run_dir = Path(run_dir)
    spec = contract.read_run_spec(run_dir)
    pid = os.getpid()
    lock_path = Path(spec.lock_path)
    guard.transfer_tree_lock(lock_path, pid, float(_create_epoch(pid) or 0))

    server: Optional["subprocess.Popen[bytes]"] = None
    server_epoch: Optional[int] = None
    state: Optional[_Run] = None
    result: Dict[str, Any]
    try:
        try:
            target = app_session._resolve_target_config(spec.repo_root, spec.key)
            if target is None:
                raise _Stage("config", f"no app_session.{spec.key} entry in coordinator.local.md")
            config = contract.parse_validate_config(target, spec.variant)
            if isinstance(config, contract.ConfigError):
                raise _Stage("config", f"{config.field}: {config.reason}")
            state = _Run(run_dir, spec, config)

            avail = (probe or _default_probe)()
            if not avail.ok:
                raise _Stage(
                    "browser",
                    avail.reason or "headless browser unavailable",
                    install_hint=avail.install_hint,
                )

            state.build_ran, state.build_exit, state.build_log = _run_build(
                config, spec.worktree, run_dir
            )
            server, server_epoch = _start_server(
                config, spec.worktree, run_dir, spec.repo_root, spec.handle_key
            )
            _wait_ready(config, server)
            try:
                state.results, state.defects, state.shots_dir = _sweep(
                    config, run_dir, driver_factory or _default_driver_factory
                )
            except Exception as exc:
                raise _Stage("sweep", f"{type(exc).__name__}: {exc}")
            report = state.write_report()
            result = {
                "ok": True,
                "configured": True,
                "key": spec.key,
                "variant": spec.variant,
                "report": str(report),
                "screenshots_dir": str(state.shots_dir),
                "defect_count": state.defects,
            }
        except _Stage as stage:
            result = {
                "ok": False,
                "configured": True,
                "stage": stage.stage,
                "error": stage.error,
                **stage.extra,
            }
            if state is not None and state.build_log and "build_log" not in result:
                result["build_log"] = state.build_log
            if stage.stage == "sweep" and state is not None:
                try:
                    result["report"] = str(state.write_report())
                except OSError:
                    pass
        except Exception as exc:
            result = {
                "ok": False,
                "configured": True,
                "stage": "sweep" if server is not None else ("build" if state is not None else "config"),
                "error": f"{type(exc).__name__}: {exc}",
            }
    finally:
        if server is not None:
            _kill_tree(server.pid, server_epoch)
            try:
                server.wait(timeout=_TERM_GRACE_S)
            except Exception:
                pass
        try:
            app_session._remove_handle(spec.repo_root, spec.handle_key)
        except OSError:
            pass
        guard.release_tree_lock(lock_path, pid)
    _atomic_write_json(run_dir / contract.RESULT_FILE, result)
    return result


def main(
    argv: Optional[List[str]] = None,
    *,
    driver_factory: Optional[DriverFactory] = None,
    probe: Optional[BrowserProbe] = None,
) -> int:
    parser = argparse.ArgumentParser(prog="_app_session_validate_run")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    run(Path(args.run_dir), driver_factory=driver_factory, probe=probe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
