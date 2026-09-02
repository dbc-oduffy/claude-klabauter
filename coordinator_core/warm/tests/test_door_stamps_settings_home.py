"""The native door tells the server which settings home its caller named.

Bug backlog: state/bug-backlog/2026-08-29-the-warm-server-answers-against-its-spaw-
f1bcc4154ca4.yaml (P0) -- step 1. The Python client's half of this is unit-tested in
`test_settings_home_mismatch_refusal.py`; this file is the half that can only be
proved by running the SHIPPED BINARY, because the frame `door.c` builds is C string
assembly that no Python test can stand in for. The defect was reproduced through
`coordinator-invoke.exe` as well as `.cmd`, so a fix that stamps only from the Python
client leaves the door exactly as silent as it was.

WHY THE COLD FALL-THROUGH IS THE DOOR'S RIGHT ANSWER, and is asserted here as a
property rather than left to `door_core_selftest.c`'s classification check alone: the
server refuses -32008 strictly before it dispatches, so re-running is safe; and the
cold leg runs `coordinator_core.invoke` in the door's OWN process, where
`settings_home()` resolves the home the caller actually named. Falling through does
not merely avoid the wrong answer, it produces the right one. The selftest pins the
classification; only this file pins what the binary does with it.

Reuses `test_door_read_deadline.py`'s fixtures -- the stub engine root, the
`election.elect()`-created pipe, and the `coordinator-invoke.exe` install -- rather
than building a second set. Same reason that module gives for using `election.elect`:
a stub that builds its own input tests a door production never opens.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.warm import settings_home_claim
from coordinator_core.warm.tests.test_door_read_deadline import (
    _FALLBACK_EXIT,
    _FALLBACK_MARKER,
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

_OK_REPLY = (
    '{"jsonrpc":"2.0","id":1,"result":{"stdout":"pong\\n","stderr":"","exit_code":0}}\n'
).encode("utf-8")


def _exchange(root: Path, settings_home: str | None, reply: bytes = _OK_REPLY):
    """Run the door once against a one-shot replying pipe; return (request, proc).

    `settings_home` is applied to the DOOR's environment only -- this process's own
    `COORDINATOR_SETTINGS_HOME` is never touched, so a test cannot repoint the
    settings home of the session running it.
    """
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    if settings_home is None:
        env.pop(settings_home_claim.SETTINGS_HOME_ENV, None)
    else:
        env[settings_home_claim.SETTINGS_HOME_ENV] = settings_home

    server = _ReplyingServer(_pipe_name_for(root), reply)
    try:
        proc = subprocess.run(
            [str(_door_under_default_name(root)), "ping"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            cwd=str(root),
        **no_console_creationflags())
    finally:
        server.close()

    request = server.request.decode("utf-8").strip()
    return (json.loads(request) if request else {}), proc


def test_door_stamps_the_home_its_caller_named(tmp_path: Path) -> None:
    """The defect's door-side half. Without this field the server has no way to
    know the caller named a home at all, and answers against its own."""
    root = _make_stub_engine_root(tmp_path)
    named_home = str(tmp_path / "an-overridden-settings-home")

    request, _ = _exchange(root, named_home)

    assert request[settings_home_claim.SETTINGS_HOME_FIELD] == named_home


def test_the_stamp_is_envelope_level_not_an_op_param(tmp_path: Path) -> None:
    """Sibling of `_engine_token`, never inside `params` -- the opposite of
    `entrypoint`'s placement, and deliberately so. A field that lands in `params`
    reaches the op as an argument; this one is transport metadata the server pops
    before it dispatches. `entrypoint` shipped in the wrong half of this same
    envelope once already (door.c's own note, 2026-08-27)."""
    root = _make_stub_engine_root(tmp_path)

    request, _ = _exchange(root, str(tmp_path / "home"))

    assert settings_home_claim.SETTINGS_HOME_FIELD not in request["params"]


def test_no_override_stamps_nothing(tmp_path: Path) -> None:
    """Every ordinary invocation on every box. The field must be ABSENT, not
    present-and-empty: absence is what the server reads as "this caller has no
    opinion", and an empty claim would refuse traffic that works today."""
    root = _make_stub_engine_root(tmp_path)

    request, proc = _exchange(root, None)

    assert settings_home_claim.SETTINGS_HOME_FIELD not in request
    assert proc.returncode == 0
    assert proc.stdout == "pong\n"


def test_a_refused_mismatch_runs_the_call_cold(tmp_path: Path) -> None:
    """-32008 is proof the server never dispatched, so the door may re-run the
    request cold -- and cold is where the caller's own settings home resolves.
    A door that treated this as post-delivery doubt would emit -32004 and fail
    the invocation outright, which is the worse of the two available answers."""
    root = _make_stub_engine_root(tmp_path)
    refusal = (
        '{"jsonrpc":"2.0","id":1,"error":'
        '{"code":-32008,"message":"warm dispatch refused: settings home"}}\n'
    ).encode("utf-8")

    _, proc = _exchange(root, str(tmp_path / "home"), reply=refusal)

    assert _FALLBACK_MARKER in proc.stdout
    assert proc.returncode == _FALLBACK_EXIT
    assert "-32004" not in proc.stdout
