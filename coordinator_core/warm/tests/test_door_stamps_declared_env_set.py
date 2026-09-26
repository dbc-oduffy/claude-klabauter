
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session.core import SESSION_ENV_PRECEDENCE
from coordinator_core.warm.tests.test_door_read_deadline import (
    _ReplyingServer,
    _door_under_default_name,
    _make_stub_engine_root,
    _pipe_name_for,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.warm_tier,
    pytest.mark.skipif(os.name != "nt", reason="door.exe is a Windows binary"),
]

_ENV_FIELD = "_env"

#: The full declared set (`door_env_set.h` / `env_forwarding.FORWARDING_SET`),
_DECLARED_NAMES = (
    "COORDINATOR_SETTINGS_HOME",
    "COORDINATOR_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "MACHINE_LOCAL_REGISTRY_DIR",
    "CLAUDE_HOME",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_CONFIG_DIR",
    "MACHINE_LOCAL_IMPL",
    "COORDINATOR_ROOT",
    "DOE_ROOT",
    "CLAUDE_PROJECT_DIR",
)

_OK_REPLY = (
    '{"jsonrpc":"2.0","id":1,"result":{"stdout":"pong\\n","stderr":"","exit_code":0}}\n'
).encode("utf-8")


def _exchange(root: Path, env_overrides: "dict[str, str] | None"):
    """Run the door once against a one-shot replying pipe; return (request, proc).

    Every declared name is POPPED before `env_overrides` is applied -- this
    process (the test runner) has real values for several of these (a real
    `CLAUDE_CODE_SESSION_ID`, sometimes a real `COORDINATOR_SETTINGS_HOME`),
    and a test that let them leak into the child would assert against the
    runner's own environment instead of the fixture's.
    """
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    for name in _DECLARED_NAMES:
        env.pop(name, None)
    env.update(env_overrides or {})

    server = _ReplyingServer(_pipe_name_for(root), _OK_REPLY)
    try:
        proc = subprocess.run(
            [str(_door_under_default_name(root)), "ping"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            cwd=str(root),
            **no_console_creationflags(),
        )
    finally:
        server.close()

    request = server.request.decode("utf-8").strip()
    return (json.loads(request) if request else {}), proc


def test_a_resolved_name_is_stamped_by_its_own_key(tmp_path: Path) -> None:
    root = _make_stub_engine_root(tmp_path)
    named_home = str(tmp_path / "an-overridden-settings-home")

    request, _ = _exchange(root, {"COORDINATOR_SETTINGS_HOME": named_home})

    assert request[_ENV_FIELD]["COORDINATOR_SETTINGS_HOME"] == named_home


def test_every_resolved_declared_name_gets_its_own_key(tmp_path: Path) -> None:
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(
        root,
        {
            "COORDINATOR_SESSION_ID": "a",
            "CLAUDE_SESSION_ID": "b",
            "CLAUDE_CODE_SESSION_ID": "c",
            "MACHINE_LOCAL_REGISTRY_DIR": str(tmp_path / "registry"),
        },
    )

    env_obj = request[_ENV_FIELD]
    assert env_obj["COORDINATOR_SESSION_ID"] == "a"
    assert env_obj["CLAUDE_SESSION_ID"] == "b"
    assert env_obj["CLAUDE_CODE_SESSION_ID"] == "c"
    assert env_obj["MACHINE_LOCAL_REGISTRY_DIR"] == str(tmp_path / "registry")


def test_an_unresolved_name_is_omitted_never_an_empty_string(tmp_path: Path) -> None:
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(root, {"COORDINATOR_SETTINGS_HOME": str(tmp_path / "home")})

    assert "MACHINE_LOCAL_REGISTRY_DIR" not in request[_ENV_FIELD]
    for name in SESSION_ENV_PRECEDENCE:
        assert name not in request[_ENV_FIELD]


def test_no_declared_name_resolved_omits_env_entirely(tmp_path: Path) -> None:
    root = _make_stub_engine_root(tmp_path)

    request, proc = _exchange(root, None)

    assert _ENV_FIELD not in request
    assert proc.returncode == 0
    assert proc.stdout == "pong\n"


def test_the_stamp_is_envelope_level_not_an_op_param(tmp_path: Path) -> None:
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(root, {"COORDINATOR_SETTINGS_HOME": str(tmp_path / "home")})

    assert _ENV_FIELD not in request["params"]


def test_the_legacy_settings_home_field_is_no_longer_stamped(tmp_path: Path) -> None:
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(root, {"COORDINATOR_SETTINGS_HOME": str(tmp_path / "home")})

    assert "_settings_home" not in request


def test_the_legacy_caller_session_id_field_is_no_longer_stamped(tmp_path: Path) -> None:
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(root, {"CLAUDE_CODE_SESSION_ID": "8b40d62c-55ef-4702-83ce-0cd8dc6513e3"})

    assert "session_id" not in request.get("_caller", {})


def test_caller_pid_is_still_stamped_unconditionally(tmp_path: Path) -> None:
    root = _make_stub_engine_root(tmp_path)

    request, proc = _exchange(root, None)

    assert "pid" in request["_caller"]
    assert proc.returncode == 0


def test_a_value_too_long_for_its_probed_buffer_is_omitted(tmp_path: Path) -> None:
    """Sizing (HARD AC): the two-call length-probe-then-read pattern is kept,
    generic over the table -- a truncated path is the worst possible value to
    stamp, so a read that will not fit sends NOTHING for that name rather than
    a truncated one. A value comfortably inside MAX_PATH*2 wide chars is used
    as the 'fits' control; door.c's own probe buffer is the only place an
    actual truncation ceiling could be exercised without a multi-KB fixture,
    so this asserts the shape (present-when-it-fits) rather than the ceiling
    itself."""
    root = _make_stub_engine_root(tmp_path)
    named_home = str(tmp_path / "home")

    request, _ = _exchange(root, {"COORDINATOR_SETTINGS_HOME": named_home})

    assert request[_ENV_FIELD]["COORDINATOR_SETTINGS_HOME"] == named_home
