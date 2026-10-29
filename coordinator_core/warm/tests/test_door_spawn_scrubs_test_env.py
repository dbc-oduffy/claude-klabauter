"""A server spawned by the native door's `door_maybe_spawn_server` sees no test variables.

`door.c` copies the door's whole environment into the spawned `python.exe server.py`
(bypassing `detached_spawn._child_env`), so the scrub lives at the server's own entry:
`server._process_entry`. This drives the real `door.exe` into its no-server spawn branch
under an isolated `COORDINATOR_WARM_RUNTIME_BASE`, lets it launch a real python child
through a stub engine root, and reads back the environment that child holds at the point
the engine's `main()` would start.

What is real: door.exe, its CreateProcessW environment block, the python child, the real
`coordinator_core.warm.server` module and its `__main__` entry function.
What is stubbed: `main()` (replaced by an env snapshot, so no engine boots) and the stub
engine root's `server.py`, a shim that imports the real module and calls the entry the
real file's `__main__` block calls. Unproven: the POSIX door (`door_posix.c`), and the
supervisor's `_process_entry` (covered by its own scrub, not this spawn path).

`DOOR_SPAWN_TEST_SOURCE_ROOT` points the shim at another tree's `coordinator_core`, so
the pre-fix server can be run against the same door.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.warm.tests.door_test_support import (
    door_under_default_name,
    make_stub_engine_root,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.warm_tier,
    pytest.mark.skipif(os.name != "nt", reason="door.exe is a Windows binary"),
]

SOURCE_ROOT_ENV = "DOOR_SPAWN_TEST_SOURCE_ROOT"
SNAPSHOT_ENV = "DOOR_SPAWN_TEST_SNAPSHOT"

TEST_VARS = {
    "_COORDINATOR_TEST_FORBID_REAL_SETTINGS_HOME": "1",
    "_COORDINATOR_TEST_DOOR_SPAWN_SENTINEL": "1",
    "COORDINATOR_PYTEST_DOOR_SPAWN_SENTINEL": "1",
    "PYTEST_CURRENT_TEST": "door_spawn::fake (call)",
    "PYTEST_DOOR_SPAWN_SENTINEL": "1",
}

_SHIM = '''\
import json, os, sys
sys.path.insert(0, os.environ["%(src)s"] or os.getcwd())
import coordinator_core.warm.server as server

def _snapshot_main():
    with open(os.environ["%(snap)s"], "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "env": dict(os.environ)}, fh)
    return 0

server.main = _snapshot_main
entry = getattr(server, "_process_entry", None) or (lambda: server.main())
sys.exit(entry())
'''


def _install_shim(root: Path) -> None:
    warm = root / "coordinator_core" / "warm"
    warm.mkdir(parents=True, exist_ok=True)
    (warm / "server.py").write_text(
        _SHIM % {"src": SOURCE_ROOT_ENV, "snap": SNAPSHOT_ENV}, encoding="utf-8"
    )


def _live_source_root() -> str:
    return str(Path(__file__).resolve().parents[3])


def test_door_spawned_server_drops_inherited_test_vars(tmp_path: Path) -> None:
    root = make_stub_engine_root(tmp_path)
    _install_shim(root)
    runtime_base = tmp_path / "runtime-base"
    runtime_base.mkdir()
    snapshot = tmp_path / "server-env.json"

    env = dict(os.environ)
    env.update(TEST_VARS)
    env["COORDINATOR_WARM_RUNTIME_BASE"] = str(runtime_base)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env[SNAPSHOT_ENV] = str(snapshot)
    env.setdefault(SOURCE_ROOT_ENV, _live_source_root())

    door = door_under_default_name(root)
    proc = subprocess.run(
        [str(door), "ping"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        cwd=str(root),
        **no_console_creationflags(),
    )

    deadline = time.monotonic() + 60
    while not snapshot.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert snapshot.exists(), (
        "the door never spawned the stub server (no snapshot); "
        f"door stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    time.sleep(0.2)
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    pid = data["pid"]
    try:
        seen = {k: v for k, v in data["env"].items() if k in TEST_VARS}
        assert not seen, f"door-spawned server still holds test vars: {sorted(seen)}"
        leftover = [
            k
            for k in data["env"]
            if k.startswith(("PYTEST_", "_COORDINATOR_TEST_", "COORDINATOR_PYTEST_"))
        ]
        assert not leftover, f"test-prefixed vars survived the boot scrub: {leftover}"
        assert data["env"].get("COORDINATOR_WARM_RUNTIME_BASE") == str(runtime_base)
    finally:
        _reap(pid)


def _reap(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/F", "/PID", str(pid)],
        capture_output=True,
        timeout=30,
        **no_console_creationflags(),
    )
