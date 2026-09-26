"""
coordinator_core.tests.test_assembler_cli_boundary_budget

Purpose: C1 (docs/plans/2026-09-06-three-assembler-briefs-under-the-brightline.md)
lands the cold CLI-boundary axis -- cold end-to-end process time and tree
spawn count at the `coordinator/bin/<name>-assemble.py brief` boundary -- as
its OWN cadence gate. This row runs FIRST and blocks every other row in that
plan: the quantity the plan's missed ACs are stated on has no committed gate
today.

This row takes NO measurement of its own. The hard bar it asserts
(`GATE_HARD_CEILING_MS = 500.0`) is DR-344's brightline, a doctrine constant,
never derived from a sample. The per-brief regression high-water is landed
here as an explicit PLACEHOLDER, named as such -- C2 runs the one real `n>=10`
cold-CLI measurement campaign this plan performs, and C6 sets the first real
high-water off C2's figures. Freezing today's ad hoc single-sample figures as
a high-water here would commit a ceiling nobody measured on purpose.

Instrument: `coordinator_core.benchmarks.process_time ::
single_invocation_tree_process_time`, called once per brief per sample,
against `coordinator/bin/<name>-assemble.py brief` run as a REAL cold
interpreter start (never `subprocess.Popen.__init__` monkeypatching -- blind
to a separately-spawned cold child -- and never an in-process
`time.process_time()` delta, which is a floor, not a total: an op that shells
out N times reads as if those N processes were free). `process_time_ms` is
the root's own CPU plus every descendant's; `procs` counts every process in
the tree, root included.

PLATFORM BOUNDARY, STATED IN CODE. `single_invocation_tree_process_time`
dispatches Windows job-object / Darwin kqueue+`wait4()` / `NotImplementedError`
-- and on the `NotImplementedError` leg (every platform but those two,
including this container's Linux) the ENTIRE call raises before producing ANY
figure; there is no separately-callable process-time half to still run
without authoring a second accounting mechanism, which this plan's
Anti-scope forbids ("No new measurement mechanism, and no second accounting
... Extending the Darwin-specific spawn-count half to Linux is PM-gated").
`coordinator_core/benchmarks/tests/test_single_invocation_tree_process_time.py`
-- the primitive's OWN pin -- takes the identical position: it
`skipif(not (IS_WINDOWS or IS_DARWIN))`s its entire file rather than
attempting a partial split. This file honours that precedent: on
`NotImplementedError`, BOTH assertions for that brief SKIP together, with the
reason printed, naming the gap plainly rather than silently narrowing the
skip to "only the spawn half" when the underlying primitive offers no such
half. Do NOT "fix" this by adding a Linux getrusage/audit-hook path here --
that IS the extension the plan's Anti-scope defers.

No wall-clock assertion anywhere. Process time and spawn count only
(DR-344 § 7, CLAUDE.md § "The brightline").

TIER: `cadence` + `spawns_process`, module-level `pytestmark`, fully
marker-declared per `coordinator_core/tests/test_no_new_spawning_tests.py`
Rules 2 and 4 -- this gate freezes its ceilings AT A CADENCE GATE and
nowhere else; it is not a per-commit gate.

Invocation (serial, never `-n auto` -- a ~50-peer box's load norm forbids a
parallel real-interpreter-spawning run):
    python -m pytest coordinator_core/tests/test_assembler_cli_boundary_budget.py -m cadence -p no:randomly

Negative spec:
    - This module does not re-derive the three CLIs' own business-correctness
      (exit codes, decision-object shape) -- that is `test_ceremony_brief_budget.py`
      (in-process) and each package's own test suite. It asserts cost only.
    - This module does not touch `coordinator_core/benchmarks/process_time.py`
      or `coordinator_core/op_census/meter.py` -- both are read-only imports.
    - The gate's own cost is budgeted, as numbers, and asserted in this same
      run (`GATE_SAMPLES_PER_BRIEF`, `GATE_TOTAL_PROCESS_TIME_CEILING_MS`,
      `GATE_TOTAL_PROCS_CEILING`) -- a gate whose own cost is unbudgeted is
      the defect it exists to catch (CLAUDE.md § Load norm).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    single_invocation_tree_process_time,
)
from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BIN_DIR = _REPO_ROOT / "coordinator" / "bin"

GATE_HARD_CEILING_MS: float = 500.0

# `subprocess.run` + `resource.getrusage(RUSAGE_CHILDREN)`, since
# platform -- see this module's own docstring, "PLATFORM BOUNDARY"). Headroom:
HIGH_WATER_PICKUP_MS: float = 220.0
HIGH_WATER_BATON_MS: float = 250.0
HIGH_WATER_WSC_MS: float = 190.0

# `GATE_SAMPLES_PER_BRIEF` is the gate's own per-run sample count, NOT C2's
GATE_SAMPLES_PER_BRIEF: int = 3
_BRIEF_COUNT = 3
GATE_TOTAL_PROCESS_TIME_CEILING_MS: float = (
    GATE_HARD_CEILING_MS * GATE_SAMPLES_PER_BRIEF * _BRIEF_COUNT * 1.5
)
GATE_TOTAL_PROCS_CEILING: int = 8 * GATE_SAMPLES_PER_BRIEF * _BRIEF_COUNT


def _skip_reason_for_platform() -> Optional[str]:
    if IS_WINDOWS or IS_DARWIN:
        return None
    return (
        "single_invocation_tree_process_time raises NotImplementedError "
        f"outright on {sys.platform!r} (no job-object/kqueue primitive) -- "
        "both the process-time and spawn-count assertions for this brief "
        "skip together, since the primitive produces no figure at all on "
        "this leg. Extending it to Linux is PM-gated (module docstring); "
        "not taken here."
    )


def _seed_pickup_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    repo = tmp_path / "pickup-repo"
    _init_repo(repo)
    handoff = repo / "state" / "handoffs" / "h1.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        'title: "Test Handoff h1.md"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
    )
    handoff.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(handoff.relative_to(repo)))
    _git(repo, "commit", "-m", "add h1.md")
    script = _BIN_DIR / "pickup-assemble.py"
    return repo, [sys.executable, str(script), "brief", "state/handoffs/h1.md"]


def _seed_baton_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    repo = tmp_path / "baton-repo"
    _init_repo(repo)
    artifact = repo / "state" / "handoffs" / "h1.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    fm = 'deliverable_id: "DEL-C1-1"\npredecessor: "none"\n'
    artifact.write_text(f"---\n{fm}---\n\n# Artifact\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(artifact.relative_to(repo)))
    _git(repo, "commit", "-m", "add h1.md")
    script = _BIN_DIR / "baton-assemble.py"
    return repo, [
        sys.executable,
        str(script),
        "brief",
        "handoff",
        "state/handoffs/h1.md",
    ]


def _seed_wsc_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    repo = tmp_path / "wsc-repo"
    _init_repo(repo)
    (repo / "README.md").write_text("placeholder\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "seed")
    script = _BIN_DIR / "workstream-complete-assemble.py"
    return repo, [sys.executable, str(script), "brief"]


def _run_gate_for_brief(op_name: str, repo: Path, cmd: list[str], placeholder_ms: float, tmp_path: Path) -> None:
    skip_reason = _skip_reason_for_platform()
    total_process_time_ms = 0.0
    total_procs = 0
    for i in range(GATE_SAMPLES_PER_BRIEF):
        if skip_reason is not None:
            pytest.skip(f"{op_name}: {skip_reason}")
            return
        result = single_invocation_tree_process_time(
            cmd,
            cwd=str(repo),
            stdout_path=str(tmp_path / f"{op_name}-{i}.out"),
            stderr_path=str(tmp_path / f"{op_name}-{i}.err"),
        )
        total_process_time_ms += result["process_time_ms"]
        total_procs += result["procs"]

        assert result["process_time_ms"] <= GATE_HARD_CEILING_MS, (
            f"{op_name}: cold process time {result['process_time_ms']}ms exceeds "
            f"the DR-344 brightline hard ceiling ({GATE_HARD_CEILING_MS}ms) -- "
            "this is a kill-bar breach, not a tunable"
        )
        assert result["process_time_ms"] <= placeholder_ms, (
            f"{op_name}: cold process time {result['process_time_ms']}ms exceeds "
            f"this brief's regression high-water ({placeholder_ms}ms, set by C6 off "
            "the combined n>=10 C2 + C6 cold-CLI campaigns, headroom 1.5x the "
            "observed max) -- bump deliberately, with a stated reason, if this "
            "growth is intended"
        )

    assert total_process_time_ms <= GATE_TOTAL_PROCESS_TIME_CEILING_MS / _BRIEF_COUNT, (
        f"{op_name}: this gate's own accumulated process time "
        f"{total_process_time_ms}ms exceeds its per-brief share of the gate's "
        f"declared budget -- the gate's own cost must stay budgeted too"
    )
    assert total_procs <= GATE_TOTAL_PROCS_CEILING / _BRIEF_COUNT, (
        f"{op_name}: this gate's own accumulated proc count {total_procs} "
        "exceeds its per-brief share of the gate's declared budget"
    )


def test_pickup_brief_cold_cli_boundary(tmp_path):
    repo, cmd = _seed_pickup_repo(tmp_path)
    _run_gate_for_brief(
        "pickup-assemble brief", repo, cmd, HIGH_WATER_PICKUP_MS, tmp_path
    )


def test_baton_brief_cold_cli_boundary(tmp_path):
    repo, cmd = _seed_baton_repo(tmp_path)
    _run_gate_for_brief(
        "baton-assemble brief", repo, cmd, HIGH_WATER_BATON_MS, tmp_path
    )


def test_workstream_complete_brief_cold_cli_boundary(tmp_path):
    repo, cmd = _seed_wsc_repo(tmp_path)
    _run_gate_for_brief(
        "workstream-complete-assemble brief", repo, cmd, HIGH_WATER_WSC_MS, tmp_path
    )
