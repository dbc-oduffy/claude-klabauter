"""Tests for agent_pipe.post: agy-originated mailbox lines carry provenance."""
from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))


def test_post_imports_neither_ipc_nor_ops() -> None:
    code = (
        "import sys; import coordinator_core.agent_pipe.post;"
        "bad=[m for m in sys.modules if m=='coordinator_core.ipc' or m=='coordinator_core.ops' or m.startswith('coordinator_core.ops.')];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], **no_console_creationflags()).returncode == 0
