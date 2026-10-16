"""A composed ask script halts on a BLOCKED slice review with unapplied findings, before test, ledger and commit."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.win_portability import no_console_creationflags

from .test_ask_compose import _JUDGE_KW, _compose

_SIZING = "state/sizings/x.yaml"
_HALT = ask_compose._review_blocked_halt_js(None)


def test_halt_sits_between_the_review_and_the_test_ledger_and_commit_stages():
    script = _compose(**_JUDGE_KW)

    assert script.count(_HALT) == 1
    halt_at = script.index(_HALT)
    assert script.index("phase('Review wave')") < halt_at
    assert halt_at < script.index("phase('Verify')")
    assert halt_at < script.index("label: 'verify'")
    assert halt_at < script.index("const _closeable = ")


def _run_halt(wave) -> dict | None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    source = (
        f"let _halted = null; const _runId = 'r1'; const _sizingRel = {_SIZING!r}; const _reviewWave = "
        + json.dumps(wave)
        + ";\n"
        + _HALT
        + "\nconsole.log(JSON.stringify(_halted));\n"
    )
    proc = subprocess.run(
        [node, "-e", source], capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _slice(verdict: str, findings: int, applied: int, sid: str = "s1") -> dict:
    return {"slice_id": sid, "verdict": verdict, "findings": findings, "applied": applied, "sidecar_path": f"{sid}.md"}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_blocked_slice_with_unapplied_findings_halts_naming_findings_and_recovery():
    halted = _run_halt([_slice("OK", 0, 0, "s0"), _slice("BLOCKED", 6, 0, "s1")])

    assert halted["halted"] == ask_compose.HALT_REVIEW_BLOCKED
    assert halted["unapplied_findings"] == 6
    assert halted["slices"] == [{"slice_id": "s1", "findings": 6, "applied": 0, "sidecar_path": "s1.md"}]
    assert "Fix" in halted["next_action"]
    assert "resumeFromRunId" not in halted["next_action"]
    assert ask_compose.fire_command(_SIZING) in halted["next_action"]


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize(
    "wave",
    [
        [_slice("BLOCKED", 3, 3)],
        [_slice("OK", 2, 0), _slice("WARN", 4, 1)],
        [None, {"verdict": "REJECTED", "findings": 5, "sidecar_path": "k.md"}, {"verdict": "FAIL"}],
        [],
        None,
    ],
)
def test_applied_clean_degraded_or_whole_diff_waves_do_not_halt(wave):
    assert _run_halt(wave) is None
