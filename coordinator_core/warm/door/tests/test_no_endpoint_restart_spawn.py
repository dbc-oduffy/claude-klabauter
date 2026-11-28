"""A hook that finds no warm endpoint at all makes the door spawn the warm server exactly once.

The fixture engine root has no pipe/socket, no boot stamp and no publish marker; its stub
`coordinator_core/warm/server.py` records its argv to a file. The cold entrypoint never answers, so the
hook ends by the door's own deadline: the spawn is asserted on the recorded argv, not on the verdict.
Removing the spawn call from the ERROR_FILE_NOT_FOUND / ENOENT branch leaves the record empty and fails
both cases. The second case makes the svc dir un-creatable (a regular file where its parent directory
belongs): the boot-stamp claim fails, and the spawn must still happen.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from coordinator_core.warm.tests.door_test_support import (
    DOOR_EXE,
    door_under_default_name,
    make_stub_engine_root,
)
from coordinator_core.warm.tests.test_door_read_deadline_posix import (
    _make_stub_engine_root as _make_posix_stub_engine_root,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.warm_tier]

_DOOR_DIR = Path(__file__).resolve().parents[1]
_OP = "hooks.preuse_bash_dispatch"
_HANG = "import time\ntime.sleep(60)\n"
_SPAWN_WAIT_S = 15.0
_SETTLE_S = 1.5


def _payload(command: str) -> bytes:
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    ).encode()


@pytest.fixture(scope="module")
def door(tmp_path_factory) -> Path:
    if os.name == "nt":
        if not DOOR_EXE.exists():
            pytest.skip(f"no built door at {DOOR_EXE}")
        return DOOR_EXE
    compiler = next((shutil.which(c) for c in ("cc", "clang", "gcc") if shutil.which(c)), None)
    if compiler is None:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("door") / "coordinator-invoke"
    subprocess.run(
        [compiler, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', "-o", str(out),
         str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


class _NoEndpointRoot:
    """Fixture engine root with a recording stub server and an empty runtime base."""

    def __init__(self, door: Path, tmp_path: Path) -> None:
        self.root = make_stub_engine_root(tmp_path) if os.name == "nt" else _make_posix_stub_engine_root(tmp_path)
        bin_dir = self.root / "coordinator" / "bin"
        for script in bin_dir.glob("*.py"):
            script.unlink()
        (bin_dir / "coordinator-invoke.py").write_text(_HANG, encoding="utf-8")
        self.record = tmp_path / "server-argv.jsonl"
        server_dir = self.root / "coordinator_core" / "warm"
        server_dir.mkdir(parents=True, exist_ok=True)
        (server_dir / "server.py").write_text(
            "import json, sys\n"
            f"with open({str(self.record)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps(['server'] + sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )
        self.home = tmp_path / "settings-home"
        self.home.mkdir()
        self.runtime = Path(tempfile.mkdtemp(prefix="nep", dir="/tmp" if os.name != "nt" else None))
        self.exe = door_under_default_name(self.root) if os.name == "nt" else door

    def close(self) -> None:
        shutil.rmtree(self.runtime, ignore_errors=True)

    def hook(self) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(
            COORDINATOR_DOOR_ENGINE_ROOT=str(self.root),
            COORDINATOR_WARM_RUNTIME_BASE=str(self.runtime),
            COORDINATOR_SETTINGS_HOME=str(self.home),
            COORDINATOR_DOOR_STDIN_MODE="hook",
        )
        return subprocess.run(
            [str(self.exe), _OP], input=_payload("echo hi"), capture_output=True, env=env,
            cwd=str(self.root), timeout=60, **no_console_creationflags(),
        )

    def spawns(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines() if line]

    def wait_for_spawn(self) -> None:
        end = time.time() + _SPAWN_WAIT_S
        while time.time() < end and not self.spawns():
            time.sleep(0.05)


@pytest.fixture
def no_endpoint(door, tmp_path):
    made = _NoEndpointRoot(door, tmp_path)
    yield made
    made.close()


def _assert_one_spawn(sc: _NoEndpointRoot) -> None:
    sc.wait_for_spawn()
    time.sleep(_SETTLE_S)
    assert sc.spawns() == [["server"]], "the no-endpoint path must spawn the server exactly once"


def test_a_hook_with_no_endpoint_no_stamp_and_no_marker_spawns_the_server_once(no_endpoint):
    assert not (no_endpoint.runtime / "coordinator").exists(), "the fixture must start with no svc dir"
    proc = no_endpoint.hook()
    assert proc.returncode == 0, proc.stderr
    _assert_one_spawn(no_endpoint)


def test_an_uncreatable_svc_dir_still_spawns_the_server_once(no_endpoint):
    """The boot-stamp claim cannot create its directory; a spawn lost to that is a dead engine left dead."""
    (no_endpoint.runtime / "coordinator").write_text("not a directory", encoding="utf-8")
    proc = no_endpoint.hook()
    assert proc.returncode == 0, proc.stderr
    assert (no_endpoint.runtime / "coordinator").is_file()
    _assert_one_spawn(no_endpoint)
