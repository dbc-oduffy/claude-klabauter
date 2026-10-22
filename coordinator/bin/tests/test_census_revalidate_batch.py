"""coordinator/bin/tests/test_census_revalidate_batch.py -- `_spawn_many`, one shell per batch.

Subject: `_spawn_many` and `_LegCost` in `coordinator/bin/mise-census-revalidate.py`. Each command
in a batch must come back as the `(returncode, stdout, stderr)` a `sh -c` of that command alone
gives. Repos are plain tmp_path directories.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TOOL = Path(__file__).resolve().parents[1] / "mise-census-revalidate.py"


@pytest.fixture(scope="module")
def mcr():
    spec = importlib.util.spec_from_file_location("mise_census_revalidate_batch", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if mod.resolve_posix_shell() is None:
        pytest.skip("no POSIX shell on this host")
    return mod


def _alone(mcr, command: str, cwd: Path):
    proc = subprocess.run(
        [mcr.resolve_posix_shell(), "-c", command], cwd=cwd, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


COMMANDS = [
    "printf 'one\\ntwo\\n'",
    "printf 'no newline'",
    "true",
    "printf 'out\\n'; printf 'err\\n' >&2; exit 3",
    "printf 'only stderr\\n' >&2",
    "printf '@@deadbeef:E:0:0@@\\n@@deadbeef:B:1@@\\n\\n'",
    "grep -c needle hay.txt",
    "printf '\\n\\n'",
]


def test_each_command_in_a_batch_returns_what_it_returns_alone(mcr, tmp_path):
    (tmp_path / "hay.txt").write_text("needle\nneedle\nhay\n", encoding="utf-8")
    got = mcr._spawn_many(COMMANDS, tmp_path, 30, mcr._LegCost())
    assert got == [_alone(mcr, c, tmp_path) for c in COMMANDS]
    assert got[1] == (0, "no newline", "")
    assert got[3] == (3, "out\n", "err\n")


def test_a_batch_costs_one_shell_spawn(mcr, tmp_path, monkeypatch):
    real = mcr.subprocess.Popen
    started: list[list] = []

    def counting(argv, *a, **kw):
        started.append(argv)
        return real(argv, *a, **kw)

    monkeypatch.setattr(mcr.subprocess, "Popen", counting)
    cost = mcr._LegCost()
    got = mcr._spawn_many(["echo a", "echo b", "echo c"], tmp_path, 30, cost)
    assert [g[1] for g in got] == ["a\n", "b\n", "c\n"]
    assert len(started) == 1 and cost.spawns == 1
    assert len(cost.last_run_ms) == 3


def test_a_timeout_kills_the_batch_and_the_unstarted_commands_read_deadline_spent(
    mcr, tmp_path
):
    got = mcr._spawn_many(["echo a", "sleep 30", "echo c", "echo d"], tmp_path, 1, mcr._LegCost())
    assert got[0] == (0, "a\n", "")
    assert got[1]["state"] == mcr.UNRUNNABLE and "timed out" in got[1]["detail"]
    assert all(
        g == {"state": mcr.UNRUNNABLE, "detail": mcr._DEADLINE_SPENT} for g in got[2:]
    )


def test_an_empty_batch_spawns_nothing(mcr, tmp_path):
    cost = mcr._LegCost()
    assert mcr._spawn_many([], tmp_path, 30, cost) == []
    assert cost.spawns == 0


def test_next_batch_takes_a_dear_command_alone_and_caps_strangers_by_the_budget_left(mcr):
    known = {"dear": (500, True), "a": (10, True), "b": (10, True)}

    def estimate(command):
        return known.get(command, (1, False))

    assert mcr._next_batch(["dear", "a"], 0, 100.0, estimate) == ["dear"]
    assert mcr._next_batch(["a", "b"], 0, 100.0, estimate) == ["a", "b"]
    strangers = [f"s{i}" for i in range(10)]
    assert len(mcr._next_batch(strangers, 0, 100.0, estimate)) == mcr._UNMEASURED_SHARE
