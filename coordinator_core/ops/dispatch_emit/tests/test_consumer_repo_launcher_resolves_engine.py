"""The scoped-test launcher a leg emits for a repo holding no coordinator_core resolves the installed engine
from that repo's own cwd with no PYTHONPATH and runs its named targets."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

import coordinator_core
from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TARGET = "tests/test_consumer_probe.py"


@pytest.fixture
def consumer_repo(tmp_path):
    root = tmp_path / "consumer"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_consumer_probe.py").write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    (root / ".git").mkdir()
    return root


def _run_emitted(command: str, cwd) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    # The engine under test is the installed engine the launcher resolves; pin it so a box whose
    # published-engine ladder is unstamped still exercises the launcher's bootstrap.
    env["COORDINATOR_ENGINE_ROOT"] = str(Path(coordinator_core.__file__).resolve().parent.parent)
    return subprocess.run(
        shlex.split(command), cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120,
        **no_console_creationflags(),
    )


def test_emitted_command_for_a_repo_without_coordinator_core_runs_its_targets(consumer_repo):
    assert not (consumer_repo / "coordinator_core").exists()
    command = emit._launcher_command([TARGET], TARGET, consumer_repo)
    proc = _run_emitted(command, consumer_repo)
    combined = proc.stdout + proc.stderr
    assert "ModuleNotFoundError" not in combined, combined[-800:]
    ran = re.search(r"(\d+) passed", combined)
    assert proc.returncode == 0 and ran and int(ran.group(1)) > 0, combined[-800:]


def test_the_emitted_command_names_its_allow_list_and_targets(consumer_repo):
    command = emit._launcher_command([TARGET], TARGET, consumer_repo)
    assert f"--allow {TARGET} -- {TARGET}" in command


def test_a_repo_holding_the_engine_launcher_keeps_the_pinned_module_form(tmp_path):
    root = tmp_path / "claude_klabauter_like"
    launcher = root / "coordinator_core" / "testing"
    launcher.mkdir(parents=True)
    (launcher / "scoped_pytest.py").write_text("", encoding="utf-8")
    command = emit._launcher_command([TARGET], TARGET, root)
    assert f"-m {emit._SCOPED_PYTEST_MODULE} " in command
