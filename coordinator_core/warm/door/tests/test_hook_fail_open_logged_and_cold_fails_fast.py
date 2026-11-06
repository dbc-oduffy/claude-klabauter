"""Every hook fail-open is logged to <settings>/state/door-fail-open.log, and a not-ready engine fails open inside the bar.

Readiness signal: the door has no engine-side ready flag to read. A missing socket or a stale socket file
(ENOENT / ECONNREFUSED) answers at once through the cold guard; a socket that accepts while the engine is
still importing is cut off by the hook read deadline (500ms), not the old 8s. Both end in a logged fail-open.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.warm.tests.door_test_support import DOOR_POSIX_C, DOOR_WINDOWS_C
from coordinator_core.warm.door.tests.test_hook_mode_post_delivery_names_timeout import (  # noqa: F401 -- fixture
    _posix_hook_run,
    posix_door,
)
from coordinator_core.warm.tests.test_door_read_deadline_posix import runtime_base  # noqa: F401 -- fixture

_BAR_MS = 500
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="drives the POSIX door")

def _define(source: Path, macro: str) -> int:
    match = re.search(rf"^#define\s+{macro}\s+(\d+)\s*$", source.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{macro} missing from {source.name}"
    return int(match.group(1))


@pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)
def test_the_hook_read_deadline_is_the_bar_and_not_the_old_eight_seconds(source):
    assert _define(source, "DOOR_HOOK_READ_DEADLINE_MS") == _BAR_MS
    assert not re.search(r"DOOR_HOOK_READ_DEADLINE_MS\s+8000", source.read_text(encoding="utf-8"))


@pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)
def test_no_fail_open_return_in_the_cold_guard_path_skips_the_log(source):
    text = source.read_text(encoding="utf-8")
    cold = text[text.index("static int hook_fall_through("):]
    cold = cold[: cold.index("\n}\n")]
    advisory_returns = [ln for ln in cold.splitlines() if "if (advisory)" in ln]
    assert advisory_returns
    assert all("door_log_fail_open(" in ln for ln in advisory_returns), advisory_returns
    pass_fn = text[text.index("static int emit_hook_pass_loudly("):]
    assert "door_log_fail_open(" in pass_fn[: pass_fn.index("\n}\n")]


# --- Python twin ---


def _log_lines(settings: Path) -> list[str]:
    log = settings / "state" / "door-fail-open.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_the_python_twin_appends_one_line_in_the_doors_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    mod._log_fail_open("submit hook outran its answer deadline", {"t0": time.monotonic()})
    mod._log_fail_open("second")
    first, second = _log_lines(tmp_path / "settings")
    assert re.match(r"ts=\d{4}-\d{2}-\d{2}T\S+Z deadline_ms=\d+ elapsed_ms=\d+ op=hooks\.submit_engine_ask ", first)
    assert first.endswith("cause=submit hook outran its answer deadline")
    assert second.endswith("cause=second")


def test_the_python_twin_log_failure_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "settings"
    blocker.write_text("a file where the settings home should be", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(blocker))
    mod._log_fail_open("unwritable")


def test_an_overrun_submit_hook_answer_is_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    call = {"t0": time.monotonic(), "wall0": time.time(), "roots": [], "minted": [], "gate": None, "gates": [],
            "session": None, "payload": None}
    monkeypatch.setattr(mod, "_minted_since_start", lambda c: [])
    monkeypatch.setattr(mod, "_spool_envelope", lambda *a, **k: None)
    mod._answer_overran(call)
    (line,) = _log_lines(tmp_path / "settings")
    assert "cause=submit hook outran its answer deadline" in line


# --- POSIX door ---


def _silent_cold_guard(root: Path) -> None:
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text("raise SystemExit(3)\n", encoding="utf-8")


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@_POSIX_ONLY
@pytest.mark.parametrize("stale_socket", [False, True], ids=["no-socket-file", "refused-connect"])
def test_no_engine_listening_fails_open_inside_the_bar_and_logs(posix_door, tmp_path, runtime_base, stale_socket):
    from coordinator_core.warm.tests import test_door_read_deadline_posix as posix

    root = posix._make_stub_engine_root(tmp_path)
    _silent_cold_guard(root)
    if stale_socket:
        sock = posix._socket_path_for(runtime_base, root)
        posix._make_socket_dir(sock)
        corpse = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        corpse.bind(str(sock))
        corpse.close()
    proc, elapsed_ms = _posix_hook_run(posix_door, root, runtime_base, tmp_path / "settings")

    assert "command allowed" in proc.stdout.decode("utf-8") or "guard did not run" in proc.stdout.decode("utf-8")
    assert elapsed_ms < _BAR_MS
    (line,) = _log_lines(tmp_path / "settings")
    assert "cause=" in line and "op=hooks.preuse_bash_dispatch" in line and f"deadline_ms={_BAR_MS}" in line
