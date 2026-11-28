"""door.c names the warm pipe by the engine OWNER's SID (`door.owner-sid.txt` beside the exe), so a
client running as another user -- the codex sandbox SID -- dials the owner's pipe; an absent or
malformed record keeps the caller's own SID.

Each leg runs the real image against a stub pipe server named for a given SID.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.warm import election, skew
from coordinator_core.warm.door import build as door_build
from coordinator_core.warm.tests.door_test_support import (
    FALLBACK_EXIT,
    FALLBACK_MARKER,
    WINDOWS_ONLY,
    ReplyingServer,
    door_under_default_name,
    make_stub_engine_root,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.warm_tier]

_FOREIGN_SID = "S-1-5-21-111111111-222222222-333333333-1006"
_PONG = (
    '{"jsonrpc":"2.0","id":1,"result":'
    '{"stdout":"pong\\n","stderr":"","exit_code":0}}\n'
).encode("utf-8")


def _pipe_for(root: Path, sid: str) -> str:
    return election.pipe_name(skew.compute_client_token(root), engine_clone=root, user_sid=sid)


def _door(root: Path, record: str | None) -> subprocess.CompletedProcess:
    exe = door_under_default_name(root)
    if record is not None:
        (exe.parent / door_build.OWNER_SID_SIDECAR_FILENAME).write_text(record, encoding="utf-8")
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    return subprocess.run(
        [str(exe), "ping"], capture_output=True, text=True, env=env, timeout=60,
        cwd=str(root), **no_console_creationflags(),
    )


@WINDOWS_ONLY
def test_door_dials_the_pipe_named_by_the_owner_sid_record(tmp_path: Path) -> None:
    root = make_stub_engine_root(tmp_path)
    server = ReplyingServer(_pipe_for(root, _FOREIGN_SID), _PONG)
    try:
        proc = _door(root, _FOREIGN_SID + "\n")
    finally:
        server.close()

    assert json.loads(server.request.decode("utf-8").strip())["method"] == "invoke.from_argv"
    assert proc.returncode == 0
    assert proc.stdout == "pong\n"


@WINDOWS_ONLY
def test_door_without_a_record_does_not_reach_a_foreign_sid_pipe(tmp_path: Path) -> None:
    root = make_stub_engine_root(tmp_path)
    server = ReplyingServer(_pipe_for(root, _FOREIGN_SID), _PONG)
    try:
        proc = _door(root, None)
    finally:
        # Releases the server thread blocked in ConnectNamedPipe, which a handle close alone would not.
        with open(_pipe_for(root, _FOREIGN_SID), "r+b", buffering=0):
            pass
        server.close()

    assert server.request == b""
    assert FALLBACK_MARKER in proc.stdout
    assert proc.returncode == FALLBACK_EXIT


@WINDOWS_ONLY
@pytest.mark.parametrize("record", ["not-a-sid\n", "S-1-5-21-1;calc\n", "S-1-\n", "   \n"])
def test_malformed_record_falls_back_to_the_callers_own_sid(tmp_path: Path, record: str) -> None:
    root = make_stub_engine_root(tmp_path)
    server = ReplyingServer(_pipe_for(root, election.current_user_sid()), _PONG)
    try:
        proc = _door(root, record)
    finally:
        server.close()

    assert proc.returncode == 0
    assert proc.stdout == "pong\n"


def test_door_c_reads_the_record_the_python_side_writes() -> None:
    source = (Path(door_build.__file__).parent / "door.c").read_text(encoding="utf-8")
    assert f'#define OWNER_SID_SIDECAR_FILENAME L"{election.OWNER_SID_RECORD_NAME}"' in source
    assert "wchar_t *sid_w = owner_sid_w();" in source


@WINDOWS_ONLY
def test_door_reading_the_record_adds_no_child_process_and_stays_cheap(tmp_path: Path) -> None:
    import psutil

    root = make_stub_engine_root(tmp_path)
    server = ReplyingServer(_pipe_for(root, _FOREIGN_SID), _PONG)
    exe = door_under_default_name(root)
    (exe.parent / door_build.OWNER_SID_SIDECAR_FILENAME).write_text(_FOREIGN_SID + "\n", encoding="utf-8")
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    try:
        proc = psutil.Popen(
            [str(exe), "ping"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            cwd=str(root), **no_console_creationflags(),
        )
        children = []
        cpu_secs = 0.0
        while proc.poll() is None:
            try:
                children.extend(c.name() for c in proc.children(recursive=True))
                times = proc.cpu_times()
                cpu_secs = times.user + times.system
            except psutil.NoSuchProcess:
                break
        out, _ = proc.communicate(timeout=60)
    finally:
        server.close()

    assert out == b"pong\n"
    assert [c for c in children if c.lower() != "conhost.exe"] == []
    assert cpu_secs < 0.2
