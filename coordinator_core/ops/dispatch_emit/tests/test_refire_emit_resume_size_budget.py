"""A refire `dispatch.emit` of a halted run with an incident-sized resume record costs under the 500ms bar.

Fixture: the halted `review_blocked` run of `benchmarks.submit_hook_resume_emit` whose X1 manifest row
records thousands of write paths, so the halt-time `resume-script.workflow.mjs` and `resume.json` reach
the 2026-11-18 incident's sizes (434KB / 139KB). The measured call is `dispatch.emit`'s default refire
shape (`ask` + `sizing_path`) with fragment hoisting at its production default, process time only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import submit_hook_resume_emit as bench
from coordinator_core.ops.dispatch_emit import ask_compose, op, prompt_fragments
from coordinator_core.roadmap import prep_gate

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

BAR_S = 0.5
SCRIPT_BYTES = 434_000
RESUME_JSON_BYTES = 139_000
RECORDED_WRITES = 7000


class _PhaseClock:
    """Process time per named phase, inclusive of nested phases (the hoist runs inside the compose)."""

    def __init__(self) -> None:
        self.ms: dict[str, float] = {}

    def wrap(self, mp: pytest.MonkeyPatch, owner: object, name: str, label: str) -> None:
        real = getattr(owner, name)

        def timed(*a, **k):
            start = time.process_time()
            try:
                return real(*a, **k)
            finally:
                self.ms[label] = self.ms.get(label, 0.0) + (time.process_time() - start) * 1000.0

        mp.setattr(owner, name, timed)

    def report(self) -> str:
        return ", ".join(f"{k}={v:.0f}ms" for k, v in sorted(self.ms.items())) or "no phase ran"


@pytest.fixture
def halted_incident_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed halted run whose halt-time build ran at production hoisting over RECORDED_WRITES paths."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", bench._SESSION_ID)
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    repo = tmp_path / "repo"
    repo.mkdir()
    for top in range(30):
        for sub in range(15):
            (repo / f"top{top}" / f"sub{sub}").mkdir(parents=True)
    run_dir = repo / "state" / "warp" / bench.RUN_ID
    real_halt = bench._halt_with_record

    def halt_with_recorded_writes(root: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        rows = [{"id": "X1", "writes": [f"pkg/mod{i}/file{i}.py" for i in range(RECORDED_WRITES)]}]
        (run_dir / "manifest.json").write_text(json.dumps({"rows": rows}), encoding="utf-8", newline="\n")
        real_halt(root)

    monkeypatch.setattr(bench, "_halt_with_record", halt_with_recorded_writes)
    bench.build_fixture(repo, 8)
    monkeypatch.chdir(repo)
    return repo


def _refire(repo: Path) -> dict:
    return op._dispatch_emit(
        {"ask": True, "sizing_path": bench.SIZING_REL, "session_id": bench._SESSION_ID}, repo
    )


def test_fixture_is_the_incident_size(halted_incident_run: Path) -> None:
    run_dir = halted_incident_run / "state" / "warp" / bench.RUN_ID
    assert (run_dir / "resume-script.workflow.mjs").stat().st_size >= SCRIPT_BYTES
    assert (run_dir / "resume.json").stat().st_size >= RESUME_JSON_BYTES


def test_a_refire_emit_of_the_incident_sized_run_stays_inside_the_brightline(
    halted_incident_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = halted_incident_run
    _refire(repo)  # warm the engine's imports and caches; the bar is the warm engine's
    bench.reset_resume_fixture(repo)

    clock = _PhaseClock()
    clock.wrap(monkeypatch, prep_gate, "repo_nested_names", "prep_gate_nested_scan")
    clock.wrap(monkeypatch, ask_compose, "compose_ask_script", "script_render")
    clock.wrap(monkeypatch, prompt_fragments, "hoist_repeated_fragments", "hoist_in_render")
    clock.wrap(monkeypatch, op, "atomic_write_bytes", "atomic_write")

    start = time.process_time()
    reply = _refire(repo)
    elapsed = time.process_time() - start

    assert reply.get("ok") and reply.get("path"), reply
    emitted = Path(reply["path"]).stat().st_size
    assert emitted >= SCRIPT_BYTES, emitted
    assert elapsed < BAR_S, f"refire emit {elapsed * 1000:.0f}ms process time over {BAR_S * 1000:.0f}ms; {clock.report()}"
