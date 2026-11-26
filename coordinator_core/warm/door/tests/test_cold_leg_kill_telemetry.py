"""Each cold-leg kill in hook mode appends exactly one engine-friction row to the settings home.

The cold entrypoint is a stub that never answers, so the door kills it at the hook deadline. The row lives at
`<settings home>/state/engine-friction.jsonl` with `irreversible_shaped` true for a command the door's
irreversible table covers (`git push`) and false for any other. A hook with no kill writes no row.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
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
_ROW_KEYS = {"ts", "kind", "source", "irreversible_shaped"}
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _payload(command: str, tool: str = "Bash") -> bytes:
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}}
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


class _Harness:
    """One fixture engine root plus a tmp_path settings home; `hook` runs the built door once."""

    def __init__(self, door: Path, tmp_path: Path, cold_script: str | None) -> None:
        self.root = make_stub_engine_root(tmp_path) if os.name == "nt" else _make_posix_stub_engine_root(tmp_path)
        bin_dir = self.root / "coordinator" / "bin"
        for script in bin_dir.glob("*.py"):
            script.unlink()
        if cold_script is not None:
            (bin_dir / "coordinator-invoke.py").write_text(cold_script, encoding="utf-8")
        self.home = tmp_path / "settings-home"
        self.home.mkdir()
        self.friction = self.home / "state" / "engine-friction.jsonl"
        self._runtime = Path(tempfile.mkdtemp(prefix="clk", dir="/tmp" if os.name != "nt" else None))
        self._exe = door_under_default_name(self.root) if os.name == "nt" else door

    def close(self) -> None:
        shutil.rmtree(self._runtime, ignore_errors=True)

    def hook(self, command: str, tool: str = "Bash") -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(
            COORDINATOR_DOOR_ENGINE_ROOT=str(self.root),
            COORDINATOR_WARM_RUNTIME_BASE=str(self._runtime),
            COORDINATOR_SETTINGS_HOME=str(self.home),
            COORDINATOR_DOOR_STDIN_MODE="hook",
        )
        return subprocess.run(
            [str(self._exe), _OP], input=_payload(command, tool), capture_output=True, env=env,
            cwd=str(self.root), timeout=60, **no_console_creationflags(),
        )

    def rows(self) -> list[dict]:
        if not self.friction.exists():
            return []
        return [json.loads(line) for line in self.friction.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def killed_cold_leg(door, tmp_path):
    harness = _Harness(door, tmp_path, _HANG)
    yield harness
    harness.close()


def _assert_row(row: dict, irreversible_shaped: bool) -> None:
    assert set(row) == _ROW_KEYS
    assert row["kind"] == "engine-friction"
    assert row["source"] == "door.cold_leg_kill"
    assert row["irreversible_shaped"] is irreversible_shaped
    assert _TS.match(row["ts"])


@pytest.mark.parametrize(
    "command,irreversible_shaped",
    [("git push origin main", True), ("echo hi", False)],
    ids=["irreversible-git-push", "not-irreversible"],
)
@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_a_cold_leg_kill_writes_exactly_one_row_with_the_irreversible_flag(
    killed_cold_leg, command, irreversible_shaped, tool
):
    proc = killed_cold_leg.hook(command, tool)
    assert proc.returncode == 0, proc.stderr
    rows = killed_cold_leg.rows()
    assert len(rows) == 1, rows
    _assert_row(rows[0], irreversible_shaped)


def test_each_kill_appends_one_more_row_in_order(killed_cold_leg):
    killed_cold_leg.hook("git push origin main")
    killed_cold_leg.hook("echo hi")
    rows = killed_cold_leg.rows()
    assert [r["irreversible_shaped"] for r in rows] == [True, False]
    for row, shaped in zip(rows, (True, False)):
        _assert_row(row, shaped)


def test_the_kill_row_does_not_change_the_verdict(killed_cold_leg):
    """The irreversible command is still denied and the other still passes loudly: the row is a side effect."""
    pushed = json.loads(killed_cold_leg.hook("git push origin main").stdout.decode("utf-8", "replace"))
    assert pushed["hookSpecificOutput"]["permissionDecision"] == "deny"
    other = killed_cold_leg.hook("echo hi").stdout.decode("utf-8", "replace")
    assert "returned no verdict in time" in other
    assert '"deny"' not in other


def test_a_hook_whose_cold_leg_is_not_killed_writes_no_row(door, tmp_path):
    """No cold entrypoint resolves, so the door passes loudly with no child to kill."""
    harness = _Harness(door, tmp_path, None)
    try:
        assert harness.hook("echo hi").returncode == 0
        assert harness.rows() == []
    finally:
        harness.close()


def test_a_native_deny_writes_no_row(door, tmp_path):
    harness = _Harness(door, tmp_path, _HANG)
    try:
        out = harness.hook("git worktree add ../x").stdout.decode("utf-8", "replace")
        assert "BLOCKED by block-worktree-creation" in out
        assert harness.rows() == []
    finally:
        harness.close()
