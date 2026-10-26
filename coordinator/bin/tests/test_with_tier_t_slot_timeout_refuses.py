"""with-tier-t-slot fails closed on slot-wait timeout: exit 75, holder named on
stderr, wrapped command never completes, and no flag runs it anyway."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WRAPPER = _REPO_ROOT / "coordinator" / "bin" / "with-tier-t-slot"

# The child sleeps before writing the sentinel: this proves it is terminated on
# refusal, NOT that it never started (the wrapper launches it before the wait).
_CHILD_SRC = (
    "import sys, time, pathlib; time.sleep(3); "
    "pathlib.Path(sys.argv[1]).write_text('ran')"
)


def _run(tmp_path: Path, wrapper_args: list[str], sentinel: Path):
    env = dict(os.environ, COORDINATOR_SETTINGS_HOME=str(tmp_path))
    return subprocess.run(
        [sys.executable, str(_WRAPPER), *wrapper_args, "--",
         sys.executable, "-c", _CHILD_SRC, str(sentinel)],
        env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def all_slots_held(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    from coordinator_core.testing import tier_t_slots

    held = []
    for _ in range(tier_t_slots.slot_cap()):
        slot = tier_t_slots.acquire_slot(
            "holder-owner-xyz", "held-cmd-marker", 0.0, pid=os.getpid()
        )
        assert slot is not None
        held.append(slot)
    yield held
    for slot in held:
        tier_t_slots.release_slot(slot)


def test_timeout_refuses_with_75_naming_holder_and_child_not_run(
    tmp_path, all_slots_held
):
    sentinel = tmp_path / "sentinel.txt"
    result = _run(tmp_path, ["--wait", "0.2"], sentinel)

    assert result.returncode == 75, result.stderr
    assert "holder-owner-xyz" in result.stderr
    assert "refusing" in result.stderr
    time.sleep(4)
    assert not sentinel.exists()


def test_free_slot_still_runs_child_and_passes_exit_code(tmp_path):
    sentinel = tmp_path / "sentinel.txt"
    env = dict(os.environ, COORDINATOR_SETTINGS_HOME=str(tmp_path))
    result = subprocess.run(
        [sys.executable, str(_WRAPPER), "--wait", "5", "--", sys.executable,
         "-c", "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('ran')",
         str(sentinel)],
        env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    assert sentinel.read_text() == "ran"


@pytest.mark.parametrize("flag", ["--force", "--unserialized", "--no-wait"])
def test_no_bypass_flag_exists(tmp_path, flag):
    sentinel = tmp_path / "sentinel.txt"
    result = _run(tmp_path, [flag], sentinel)
    assert result.returncode != 0
    assert "unrecognized flag" in result.stderr
    assert not sentinel.exists()
