"""A warm server whose pool worker is stalled dies with its whole tree on its own watchdog and is reborn.

Drives the production path end to end: a real warm server booted from a hardlink clone of this tree
on an isolated runtime base, called through `coordinator/bin/coordinator-invoke.py` with default
arguments (the client autostarts the replacement server). Nothing in the test kills the stalled
tree. Two clone-local test knobs shorten the clock and are the only departures from production:
`stall_secs` (1.5s) and a restart threshold of 1 stall record. The stall itself is a pool worker
that sleeps before taking its claim, only while a marker file names its parent server pid.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator, NamedTuple

import psutil
import pytest

from coordinator_core.benchmarks import isolated_clone as ic
from coordinator_core.warm import breadcrumb, hang_restart
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.warm_tier,
    pytest.mark.deliberate_wall_clock(reason="a watchdog's wait on a stalled worker is the behaviour under test"),
]

_OP = "diagnostics.always_succeeds"
_STALL_SECS = "1.5"
_BOOT_WAIT_SECS = 90.0
_TREE_DEATH_SECS = 45.0
_CALL_SECS = 120.0
_LOG_WAIT_SECS = 40.0
_TELEMETRY_WAIT_SECS = 15.0
_STALL_SNAPSHOT_SECS = 1.0

_ENV_STALL_SECS = "HRE2E_STALL_SECS"
_ENV_MARKER = "HRE2E_STALL_MARKER"

_SHIM_MODULE = "_hre2e_shim"
_SHIM_SOURCE = f'''\
"""Clone-local test knobs for test_hung_engine_reborn_end_to_end."""
import functools
import os
import time


def _stalled_parent(marker):
    try:
        with open(marker, "r", encoding="ascii") as fh:
            return fh.read().strip() == str(os.getppid())
    except OSError:
        return False


def install_server(ns):
    ns["STALL_DUMP_SECS"] = float(os.environ["{_ENV_STALL_SECS}"])
    marker = os.environ["{_ENV_MARKER}"]
    real = ns["_pool_dispatch_worker"]

    @functools.wraps(real)
    def worker(*args, **kwargs):
        if _stalled_parent(marker):
            time.sleep(3600)
        return real(*args, **kwargs)

    ns["_pool_dispatch_worker"] = worker


def install_hang_restart(ns):
    for name in ("maybe_restart", "note_stall"):
        def wrap(real):
            @functools.wraps(real)
            def one_record(*args, **kwargs):
                kwargs.setdefault("threshold", 1)
                return real(*args, **kwargs)

            return one_record

        ns[name] = wrap(ns[name])
'''
_SERVER_HOOK = f"import {_SHIM_MODULE} as _hre2e; _hre2e.install_server(globals())\n\n\n"
_HANG_RESTART_HOOK = f"\n\nimport {_SHIM_MODULE} as _hre2e  # noqa: E402\n_hre2e.install_hang_restart(globals())\n"


class _Clone(NamedTuple):
    root: Path
    env: dict
    marker: Path


def _replace_file(path: Path, text: str) -> None:
    """Break the hardlink to the source tree, then write: an in-place write would edit the source."""
    path.unlink()
    path.write_text(text, encoding="utf-8", newline="\n")


def _install_shim(root: Path, shim_dir: Path) -> None:
    shim_dir.mkdir()
    (shim_dir / f"{_SHIM_MODULE}.py").write_text(_SHIM_SOURCE, encoding="utf-8", newline="\n")
    warm = root / "coordinator_core" / "warm"
    server_py = warm / "server.py"
    text = server_py.read_text(encoding="utf-8")
    anchor = text.rfind('\nif __name__ == "__main__":')
    assert anchor != -1, "server.py lost its __main__ block; the test hook has no anchor"
    _replace_file(server_py, text[: anchor + 1] + _SERVER_HOOK + text[anchor + 1 :])
    hang_py = warm / "hang_restart.py"
    _replace_file(hang_py, hang_py.read_text(encoding="utf-8") + _HANG_RESTART_HOOK)


def _server_pid(root: Path):
    crumb = breadcrumb.read_breadcrumb(engine_root=root)
    return int(crumb["pid"]) if crumb and crumb.get("pid") else None


def _wait_for(predicate, secs: float, what: str):
    gate = threading.Event()
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        gate.wait(0.25)
    raise AssertionError(f"timed out after {secs:.0f}s waiting for {what}")


def _terminate_tree(pid) -> None:
    if pid is None:
        return
    with contextlib.suppress(psutil.Error):
        parent = psutil.Process(pid)
        kids = parent.children(recursive=True)
        for proc in (*kids, parent):
            with contextlib.suppress(psutil.Error):
                proc.terminate()
        psutil.wait_procs([parent, *kids], timeout=5)


@pytest.fixture
def clone(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clone]:
    source_root = Path(__file__).resolve().parents[3]
    tmp_parent = ic.mkdtemp_for_clone(source_root, prefix="hre2e-")
    root = tmp_parent / "clone"
    runtime_base = tmp_parent / "rt"
    marker = tmp_parent / "stall-marker"
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(runtime_base))
    env = {
        **ic.engine_env(root),
        "COORDINATOR_ENGINE_ROOT": str(root),
        "COORDINATOR_CLONE_ROOT": str(root),
        "COORDINATOR_WARM_RUNTIME_BASE": str(runtime_base),
        "PYTHONPATH": os.pathsep.join(filter(None, (str(tmp_parent / "shim"), os.environ.get("PYTHONPATH")))),
        _ENV_STALL_SECS: _STALL_SECS,
        _ENV_MARKER: str(marker),
    }
    proc = job = None
    seen: set = set()
    try:
        ic.clone_engine(source_root, root, entrypoint="coordinator-invoke")
        _install_shim(root, tmp_parent / "shim")
        proc, job = ic.spawn_clone_server(root, env)
        yield _Clone(root, env, marker)
    finally:
        pid = _server_pid(root)
        if pid is not None:
            seen.add(pid)
        for server_pid in seen:
            _terminate_tree(server_pid)
        if proc is not None and proc.poll() is None:
            proc.terminate()
        ic.close_clone_job(job)
        reaped = ic.reap_processes_under(tmp_parent)
        shutil.rmtree(breadcrumb.svc_dir(engine_root=root), ignore_errors=True)
        ic.rmtree_or_raise(tmp_parent, label="hung-engine-reborn-e2e", reaped=reaped)


def _invoke(clone: _Clone) -> subprocess.Popen:
    """The production client: `coordinator-invoke.py <op>`, default arguments."""
    script = clone.root / "coordinator" / "bin" / "coordinator-invoke.py"
    return subprocess.Popen(
        [sys.executable, str(script), _OP],
        cwd=str(clone.root),
        env=clone.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **no_console_creationflags(),
    )


def _finish(call: subprocess.Popen, what: str) -> str:
    try:
        out, err = call.communicate(timeout=_CALL_SECS)
    except subprocess.TimeoutExpired:
        call.kill()
        out, err = call.communicate()
        raise AssertionError(f"{what} did not return in {_CALL_SECS:.0f}s; stderr: {err[-600:]}")
    assert call.returncode == 0, f"{what} exited {call.returncode}; stdout: {out[-400:]}; stderr: {err[-600:]}"
    assert "always_succeeds" in out, f"{what} returned no op reply: {out[-400:]}"
    return out


def _log_lines(root: Path) -> list:
    log = breadcrumb.svc_dir(engine_root=root) / hang_restart.HANG_RESTART_LOG_FILENAME
    try:
        return log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _answered_by(root: Path, pid: int) -> bool:
    for path in breadcrumb.svc_dir(engine_root=root).glob("request-telemetry*.jsonl"):
        with contextlib.suppress(OSError):
            for line in path.read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(ValueError):
                    if json.loads(line).get("pid") == pid:
                        return True
    return False


def test_stalled_pool_worker_kills_the_server_tree_and_a_fresh_server_answers_and_a_rehang_backs_off(
    clone: _Clone,
) -> None:
    root = clone.root
    stalled_pid = _wait_for(lambda: _server_pid(root), _BOOT_WAIT_SECS, "the first server's breadcrumb")
    _finish(_invoke(clone), "the baseline call")
    assert _server_pid(root) == stalled_pid

    clone.marker.write_text(str(stalled_pid), encoding="ascii")
    call = _invoke(clone)
    with contextlib.suppress(subprocess.TimeoutExpired):
        call.wait(timeout=_STALL_SNAPSHOT_SECS)
    server = psutil.Process(stalled_pid)
    tree = [server, *server.children(recursive=True)]
    assert len(tree) > 1, "the stalled server had no pool workers to kill with it"

    _gone, alive = psutil.wait_procs(tree, timeout=_TREE_DEATH_SECS)
    if alive:
        call.kill()
    assert not alive, f"the stalled server tree outlived its own watchdog: {[p.pid for p in alive]}"
    _finish(call, "the call made while the worker was stalled")
    assert any(f"pid={stalled_pid} action=restart" in line for line in _log_lines(root)), _log_lines(root)

    reborn_pid = _wait_for(
        lambda: (p if (p := _server_pid(root)) not in (None, stalled_pid) and psutil.pid_exists(p) else None),
        _BOOT_WAIT_SECS,
        "a reborn server's breadcrumb",
    )
    assert reborn_pid != stalled_pid
    _finish(_invoke(clone), "the call answered by the reborn server")
    _wait_for(lambda: _answered_by(root, reborn_pid), _TELEMETRY_WAIT_SECS, "a telemetry row from the reborn server")

    clone.marker.write_text(str(reborn_pid), encoding="ascii")
    second = _invoke(clone)
    try:
        _wait_for(
            lambda: any(f"pid={reborn_pid} action=suppressed" in line for line in _log_lines(root)),
            _LOG_WAIT_SECS,
            "a suppressed line for the second stall",
        )
        assert psutil.pid_exists(reborn_pid) and _server_pid(root) == reborn_pid, "the re-hang restarted inside its backoff"
        assert not any(f"pid={reborn_pid} action=restart" in line for line in _log_lines(root))
    finally:
        second.kill()
        second.communicate()
