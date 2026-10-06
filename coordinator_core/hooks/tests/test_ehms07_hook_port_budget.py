"""Each of the 8 ehms-07 hook ops fits the HOOK_PORT band on its dominant path with zero spawns.

The context_pressure_precompact case measures only the no-valid-session_id path; its real
PreCompact path spawns git (`_run_git`) and is not covered here.
"""

from __future__ import annotations

import importlib
import subprocess

import pytest

from coordinator_core.benchmarks.budget import resolve_budget
from coordinator_core.benchmarks.process_time import in_process_time_ms


def _non_matching_command() -> dict:
    return {"command_name": "not-a-family-command", "command_args": "", "session_id": "sid"}


def _tripwire_steady_state(tmp_path) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "coordinator" / "hooks").mkdir(parents=True)
    (repo / "coordinator" / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    payload = {"session_id": "sess-abc12345", "agent_id": "", "cwd": str(repo)}
    importlib.import_module("coordinator_core.hooks.runtime_tripwire_em_check")._handler(
        {"payload": payload}
    )
    return {"payload": payload}


CASES = [
    ("hooks.pickup_autofire", "pickup_autofire", lambda tp: _non_matching_command()),
    ("hooks.mise_autofire", "mise_autofire", lambda tp: _non_matching_command()),
    (
        "hooks.handoff_segment_inject",
        "handoff_segment_inject",
        lambda tp: _non_matching_command(),
    ),
    ("hooks.group_em_autofire", "group_em_autofire", lambda tp: _non_matching_command()),
    (
        "hooks.runtime_tripwire_em_check",
        "runtime_tripwire_em_check",
        _tripwire_steady_state,
    ),
    (
        "hooks.context_pressure_precompact",
        "context_pressure_precompact",
        lambda tp: {"transcript_path": "/x"},
    ),
    (
        "hooks.nudge_cross_repo_cwd_boundary",
        "nudge_cross_repo_cwd_boundary",
        lambda tp: {"old_cwd": str(tp / "a"), "new_cwd": str(tp / "b")},
    ),
    (
        "hooks.guard_config_change_hookstack_selfdefence",
        "guard_config_change_hookstack_selfdefence",
        lambda tp: {"source": "local_settings", "file_path": "/x"},
    ),
]

_IDS = [
    "context_pressure_precompact[no-session-id-path-only]"
    if op == "hooks.context_pressure_precompact"
    else op
    for op, _m, _f in CASES
]


@pytest.mark.parametrize(("op", "module", "factory"), CASES, ids=_IDS)
def test_dominant_path_fits_hook_port_band_with_zero_spawns(
    op, module, factory, tmp_path, monkeypatch
):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    handler = importlib.import_module(f"coordinator_core.hooks.{module}")._handler
    payload = factory(tmp_path)

    spawns = []

    def _counting_spawn(*args, **kwargs):
        spawns.append(args)
        raise AssertionError(f"{op} spawned a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "run", _counting_spawn)
    monkeypatch.setattr(subprocess, "Popen", _counting_spawn)

    measured = in_process_time_ms(lambda: handler(payload))["process_time_ms"]

    budget = resolve_budget(op, "HOOK_PORT")
    limit = budget["target_ms"] * (1 + budget["tolerance"]["value"])
    assert len(spawns) == 0, f"{op}: {len(spawns)} spawn(s) on the dominant path"
    assert measured <= limit, f"{op}: {measured} ms process time > HOOK_PORT band {limit} ms"
