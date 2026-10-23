"""`COORDINATOR_WARM=0` on the native door entry runs a MUTATING op cold, never dialling the warm pipe.

Spec: docs/plans/2026-10-23-coordinator-warm-0-exe-forwarder-cold-9f9bff.md § C1.

Drives a locally built `door.exe` installed as `coordinator-invoke.exe` with the
Example-retrieval-repo-d1 argv shape (`ceremony.commit_v2 --repo R --params-file -`, payload on
stdin, variable set only in the child env). A `ReplyingServer` listens on the pipe the
door would dial: a request reaching it means the gate failed. The cold child reports the
execution-route env it inherited, and that value feeds `_timeout_error_envelope`, so the
cold-timeout wording is checked on the route the door actually produced.

Scope: this proves the source, not the installed settings-home exe; a stale published
exe stays red-invisible here until it is republished.

Positive control: without the variable the same invocation reaches the server, so a gate
that always falls through cold cannot pass.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.ipc import _timeout_error_envelope
from coordinator_core.temp_layout import managed_tempdir
from coordinator_core.telemetry import op_latency
from coordinator_core.warm.door import build as door_build
from coordinator_core.warm.door.tests.test_windows_door_cold_leg_route import (
    _release_unconnected_server,
)
from coordinator_core.warm.tests.door_test_support import (
    FALLBACK_MARKER,
    ReplyingServer,
    WINDOWS_ONLY,
    candidate_source_roots,
    make_stub_engine_root,
    pipe_name_for,
    resolve_stamped_source_root,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.warm_tier,
]

_OP = "ceremony.commit_v2"
_ROUTE_PREFIX = "ROUTE="
_WARM_REPLY = (
    '{"jsonrpc":"2.0","id":1,"result":'
    '{"stdout":"served-warm\\n","stderr":"","exit_code":0}}\n'
).encode("utf-8")

_BUILT_DOOR: list = []


def _locally_built_door() -> Path:
    if not _BUILT_DOOR:
        source_root = resolve_stamped_source_root()
        if source_root is None:
            pytest.skip(
                "no candidate engine root carries a valid build stamp among "
                f"{candidate_source_roots()!r}"
            )
        tmpdir = tempfile.TemporaryDirectory(prefix="warm-zero-door-", dir=managed_tempdir())
        atexit.register(tmpdir.cleanup)
        _BUILT_DOOR.append(door_build.build(source_root, output=Path(tmpdir.name) / "door.exe"))
    return _BUILT_DOOR[0]


def _install_invoke_door(root: Path) -> Path:
    installed = root / "coordinator-invoke.exe"
    shutil.copy2(_locally_built_door(), installed)
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text(
        "import os, sys\n"
        f"print({FALLBACK_MARKER!r})\n"
        f"print({_ROUTE_PREFIX!r} + os.environ.get({op_latency.ROUTE_ENV!r}, ''))\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    return installed


def _run(
    door: Path, root: Path, repo: Path, *, warm: str | None, params_stdin: bool = False
) -> subprocess.CompletedProcess:
    """`params_stdin` adds `--params-file -`, which the door routes cold by itself;
    the discriminating legs leave it off so only COORDINATOR_WARM can decide."""
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env.pop("COORDINATOR_DOOR_STDIN_MODE", None)
    env.pop("COORDINATOR_WARM", None)
    env.pop(op_latency.ROUTE_ENV, None)
    if warm is not None:
        env["COORDINATOR_WARM"] = warm
    return subprocess.run(
        [str(door), _OP, "--repo", str(repo)] + (["--params-file", "-"] if params_stdin else []),
        input=b"{}",
        capture_output=True,
        env=env,
        timeout=60,
        cwd=str(repo),
        **no_console_creationflags(),
    )


@WINDOWS_ONLY
def test_warm_zero_runs_a_mutating_op_cold_without_dialling_the_pipe(tmp_path: Path) -> None:
    root = make_stub_engine_root(tmp_path)
    door = _install_invoke_door(root)

    server = ReplyingServer(pipe_name_for(root), _WARM_REPLY)
    try:
        proc = _run(door, root, tmp_path, warm="0")
    finally:
        _release_unconnected_server(root)
        server.close()

    assert not server.request, "COORDINATOR_WARM=0 still delivered the mutating op to the warm pipe"
    assert FALLBACK_MARKER.encode() in proc.stdout
    assert b"served-warm" not in proc.stdout
    assert b"warm dispatch indeterminate" not in proc.stdout + proc.stderr


@WINDOWS_ONLY
def test_the_example_retrieval_repo_repro_shape_runs_cold_with_warm_zero(tmp_path: Path) -> None:
    root = make_stub_engine_root(tmp_path)
    door = _install_invoke_door(root)

    server = ReplyingServer(pipe_name_for(root), _WARM_REPLY)
    try:
        proc = _run(door, root, tmp_path, warm="0", params_stdin=True)
    finally:
        _release_unconnected_server(root)
        server.close()

    assert not server.request
    assert FALLBACK_MARKER.encode() in proc.stdout
    assert b"warm dispatch indeterminate" not in proc.stdout + proc.stderr


@WINDOWS_ONLY
def test_without_the_variable_the_same_invocation_reaches_the_pipe(tmp_path: Path) -> None:
    root = make_stub_engine_root(tmp_path)
    door = _install_invoke_door(root)

    server = ReplyingServer(pipe_name_for(root), _WARM_REPLY)
    try:
        proc = _run(door, root, tmp_path, warm=None)
    finally:
        server.close()

    assert server.request, "control invocation did not dial the pipe; the gate test above proves nothing"
    assert proc.stdout == b"served-warm\n"


@WINDOWS_ONLY
def test_a_cold_timeout_on_the_warm_zero_route_does_not_say_warm_dispatch(tmp_path: Path) -> None:
    root = make_stub_engine_root(tmp_path)
    door = _install_invoke_door(root)

    server = ReplyingServer(pipe_name_for(root), _WARM_REPLY)
    try:
        proc = _run(door, root, tmp_path, warm="0")
    finally:
        _release_unconnected_server(root)
        server.close()

    route = next(
        line[len(_ROUTE_PREFIX):]
        for line in proc.stdout.decode("utf-8", "replace").splitlines()
        if line.startswith(_ROUTE_PREFIX)
    )
    assert route not in (op_latency.WARM_SERVER, op_latency.HTTP_SERVER), (
        f"the cold child inherited execution route {route!r}"
    )

    prior = os.environ.get(op_latency.ROUTE_ENV)
    try:
        if route:
            os.environ[op_latency.ROUTE_ENV] = route
        else:
            os.environ.pop(op_latency.ROUTE_ENV, None)
        message = _timeout_error_envelope(_OP, 30.0, 1)["error"]["message"]
    finally:
        if prior is None:
            os.environ.pop(op_latency.ROUTE_ENV, None)
        else:
            os.environ[op_latency.ROUTE_ENV] = prior

    assert "warm dispatch indeterminate" not in message
    assert "inside the warm engine" not in message
    assert "ran cold" in message
