"""A 19-path ceremony.commit_v2 replies under the 500ms process budget or its phase row names the blocked leg."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.ceremony import commit_v2_phases as cp
from coordinator_core.win_portability import no_console_creationflags

_SID = "11111111-2222-4333-8444-555555555555"


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _clocks(monkeypatch, cpu, wall):
    c, w = iter(cpu), iter(wall)
    monkeypatch.setattr(cp.time, "thread_time", lambda: next(c))
    monkeypatch.setattr(cp.time, "monotonic", lambda: next(w))


def test_wait_leg_past_budget_is_named(monkeypatch):
    _clocks(monkeypatch, [0, 0.01, 0.01, 0.02], [0, 0.01, 0.01, 2.0])
    led = cp.PhaseLedger()
    with led.phase("cheap"):
        pass
    with led.phase("claim_poll"):
        pass
    blocked = led.blocked_leg()
    assert blocked["leg"] == "claim_poll"
    assert blocked["kind"] == "wait"
    assert blocked["wait_ms"] > 1900


def test_compute_over_budget_names_costliest_phase(monkeypatch):
    _clocks(monkeypatch, [0, 0.1, 0.1, 0.9], [0, 0.1, 0.1, 0.9])
    led = cp.PhaseLedger()
    with led.phase("small"):
        pass
    with led.phase("big"):
        pass
    assert led.blocked_leg()["leg"] == "big"
    assert led.blocked_leg()["kind"] == "compute"


def test_in_budget_names_no_leg(monkeypatch):
    _clocks(monkeypatch, [0, 0.01], [0, 0.05])
    led = cp.PhaseLedger()
    with led.phase("x"):
        pass
    assert led.blocked_leg() is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_default_commit_of_19_paths_is_in_budget_or_names_its_leg(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OP_LATENCY_DISABLE", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "README.md"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    names = [f"f{i}.txt" for i in range(19)]
    for n in names:
        (root / n).write_text(n + "\n", encoding="utf-8")

    result = commit_v2._handler(
        {"paths": names, "message": "add nineteen\n", "session_id": _SID},
        repo_root=root / ".git",
    )
    assert result["committed"] is True

    sink = root / ".git" / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    rows = [json.loads(x) for x in sink.read_text(encoding="utf-8").splitlines() if x]
    (row,) = [r for r in rows if r.get("kind") == "phase_time"]
    assert row["paths"] == 19
    assert row["total_ms"] < cp.BUDGET_MS or row["blocked_leg"]["leg"] in row["phases_ms"]
