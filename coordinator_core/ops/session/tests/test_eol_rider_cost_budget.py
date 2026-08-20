"""
coordinator_core.ops.session.tests.test_eol_rider_cost_budget

Purpose: C9 (docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md
§ C9, AC9) -- the eol census/audit_producers cadence rider inside
`session.boot_sweep` (C8) has no cost budget on record, which
`docs/wiki/cost-budgets-and-the-kill-disposition.md` requires for any
machinery landing on the boot path. This file supplies the budget as a
FORMULA with named inputs and a test that FAILS when either (a) the real
code's measured per-repo spawn cost exceeds its pinned constant, or (b) the
formula's own projected fleet-wide load exceeds its explicit ceiling.

The budget is a MACHINE-LOAD budget, not a first-token-latency budget: the
rider fires on the ASYNC boot path (`session.boot_sweep`) with stdout
discarded, so nothing downstream is waiting on it -- the cost being bounded
is aggregate spawn count / wall-time injected onto a box already running the
50-70-concurrent-session load norm, never the latency of any one boot.

Formula (named inputs, `docs/wiki/machine-load-norm.md`'s 50-70-concurrent-
session norm is the ONLY place `REPO_COUNT_LOAD_NORM_UPPER_BOUND` is derived
from -- it is not fitted to what the code happens to cost):

  REPO_COUNT_LOAD_NORM_UPPER_BOUND (70)
      Worst-case count of distinct repos boot-swept inside one 15-minute
      cadence window: the load norm's own upper bound of concurrent
      sessions, credited one distinct repo per session (no repo-sharing
      discount -- the worse case, per the KILL CONDITION math, not the
      typical one). Each repo holds its OWN cadence marker in its OWN
      `common_dir` (`boot_sweep._EOL_CENSUS_MARKER_NAME`), so cost is
      `repo_count x` the per-repo constant, never a flat per-repo number --
      the plan's earlier formula under-derived by an order of magnitude by
      omitting this input (2026-08-20 review finding, cited in C9's body).
  SPAWNS_PER_REPO_PER_CENSUS (3, pinned)
      `eol.census`'s own documented, measured spawn count -- `ls-files`,
      `check-attr --stdin`, `status --porcelain`, ALWAYS exactly three,
      independent of tracked-path count (`coordinator_core/ops/eol/
      census.py`'s module docstring and AC3). `eol.audit_producers` is
      pure `pathlib`/`ast` and contributes ZERO spawns (its own module
      docstring's negative-spec) -- it rides the SAME 24h marker as
      census's 15m one on a slower cadence and is asserted spawn-free below
      so a future change that adds a subprocess call there is caught, not
      silently absorbed into an unaccounted-for machine cost.
  CADENCE_WINDOW_SECONDS (900 = 15 * 60)
      `boot_sweep._EOL_CENSUS_CADENCE_WINDOW_SECONDS` -- the census rider's
      own cadence divisor, read off the module under test rather than
      re-stated, so the two can never silently drift apart.
  PER_SPAWN_COST_MS_WORST_CASE (500)
      Upper end of the ~200-500ms Windows per-spawn cost named in C9's body
      (process creation + git startup on this OS -- see
      `docs/reference/machine-load-norm.md`-adjacent Windows-first-class
      doctrine in this repo's CLAUDE.md). The worst-case end is used
      deliberately: a budget sized to the median is a budget that regresses
      on the day some process happens to run slower for reasons outside the
      rider's own code.

  machine_load_ms_per_window  = REPO_COUNT_LOAD_NORM_UPPER_BOUND
                                 * SPAWNS_PER_REPO_PER_CENSUS
                                 * PER_SPAWN_COST_MS_WORST_CASE
  machine_load_fraction        = machine_load_ms_per_window
                                  / (CADENCE_WINDOW_SECONDS * 1000)

  = 70 * 3 * 500 / (900 * 1000) = 105000 / 900000 ~= 0.1167 (~11.7%)

MAX_MACHINE_LOAD_FRACTION (0.15) is the explicit ceiling: no more than 15%
of a cadence window's wall time may be consumed fleet-wide by this rider, in
the worst case where the load norm's entire upper bound boots a distinct
repo inside the same window. Per the kill-disposition page, breaching this
is a KILL CANDIDATE for the rider, not license to raise the ceiling --
raising it requires editing this recorded number, which is the ratchet
mechanism this file is not the tool to override.

Retired number, NOT used here: the ~15.6ms windows_timer_quantum_floor is
RETRACTED as a harness artifact (DoE-claude docs/research/
2026-08-19-hook-class-route-budget.md) and must not be cited as a per-spawn
cost.

Spec backlinks:
  - Plan chunk C9: docs/plans/2026-08-20-every-repo-detects-its-own-eol-
    drift.md § C9, AC9.
  - docs/wiki/cost-budgets-and-the-kill-disposition.md -- the formula shape
    and "something must fail on breach" rule this file discharges.
  - docs/reference/eol-drift-detection.md -- prose restatement of this same
    formula plus the kill condition, for a reader who lands on the doc
    before the test.

Negative-spec:
  - Does NOT re-measure `eol.census`'s internal violation-detection
    behavior -- `coordinator_core/ops/eol/tests/test_census.py`'s remit.
  - Does NOT exercise the cadence-gate/degrade-safe wiring inside
    `session.boot_sweep._handler` -- `test_boot_sweep_eol_rider.py`'s
    remit; this file only measures and bounds COST.
  - Does NOT implement or test the K=8-clean-census self-demotion policy
    C9's body also names -- that is a `boot_sweep.py` behavior change
    outside this chunk's `writes:` list (test-edit only); see
    `docs/reference/eol-drift-detection.md` for its documented-but-not-
    yet-wired status.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops  # noqa: F401 -- ops/__init__.py triggers all op registrations
import coordinator_core.ops.session.boot_sweep  # noqa: F401 -- fires @register_op("session.boot_sweep")

from coordinator_core.ops.eol.audit_producers import audit_producers as _eol_audit_producers
from coordinator_core.ops.eol.census import census as _eol_census
from coordinator_core.ops.session import boot_sweep

# Real git spawn is load-bearing: this file measures the REAL subprocess
# cost of eol.census against a fixture repo, never a stub.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Formula constants -- named inputs, see module docstring for derivation.
# ---------------------------------------------------------------------------

REPO_COUNT_LOAD_NORM_UPPER_BOUND = 70
SPAWNS_PER_REPO_PER_CENSUS = 3
PER_SPAWN_COST_MS_WORST_CASE = 500
MAX_MACHINE_LOAD_FRACTION = 0.15


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / ".gitattributes").write_bytes(b"*.txt eol=lf\n")
    (root / "a.txt").write_bytes(b"one\ntwo\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


@pytest.fixture
def repo(tmp_path) -> Path:
    return _init_repo(tmp_path / "repo")


def _install_counting_run(monkeypatch) -> dict:
    """Wraps the REAL `subprocess.run` to count calls -- never replaces its
    behavior. Installed only around the measured call, never during fixture
    setup, so repo-seeding I/O is never mistaken for the rider's own cost."""
    call_count = {"n": 0}
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        call_count["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)
    return call_count


# ---------------------------------------------------------------------------
# (a) real measured per-repo spawn cost is pinned to the formula's own
#     SPAWNS_PER_REPO_PER_CENSUS constant -- a regression in eol.census's
#     spawn count is what actually breaches the budget in practice.
# ---------------------------------------------------------------------------


def test_census_measured_spawn_count_matches_pinned_constant(repo, monkeypatch):
    call_count = _install_counting_run(monkeypatch)

    verdict = _eol_census(repo)

    assert verdict["violation_count"] == 0
    assert call_count["n"] == SPAWNS_PER_REPO_PER_CENSUS, (
        "eol.census's measured git spawn count "
        f"({call_count['n']}) no longer matches the pinned formula input "
        f"SPAWNS_PER_REPO_PER_CENSUS ({SPAWNS_PER_REPO_PER_CENSUS}) -- this "
        "IS a budget breach: either eol.census regressed to more spawns, or "
        "this constant needs a deliberate, reviewed edit, never a silent "
        "widening to match."
    )


def test_audit_producers_measured_spawn_count_is_zero(repo, monkeypatch):
    """eol.audit_producers contributes ZERO spawns to the formula -- pinned
    here so a future subprocess call added there is caught as an unbudgeted
    machine cost rather than silently absorbed."""
    call_count = _install_counting_run(monkeypatch)

    _eol_audit_producers(repo)

    assert call_count["n"] == 0, (
        f"eol.audit_producers now spawns {call_count['n']} subprocess(es) "
        "-- the cost-budget formula assumes it is spawn-free; either revert "
        "the new spawn or update the formula's inputs deliberately."
    )


# ---------------------------------------------------------------------------
# (b) the rider's own cadence window constant stays in sync with the
#     formula's CADENCE_WINDOW_SECONDS input -- read off the module under
#     test, not re-stated, so the two cannot silently drift apart.
# ---------------------------------------------------------------------------


def test_formula_cadence_window_matches_rider_constant():
    assert boot_sweep._EOL_CENSUS_CADENCE_WINDOW_SECONDS == 15 * 60


# ---------------------------------------------------------------------------
# (c) the formula's projected fleet-wide machine load stays under its
#     explicit ceiling -- fails on breach per the kill-disposition page.
# ---------------------------------------------------------------------------


def test_formula_projected_machine_load_within_budget():
    cadence_window_seconds = boot_sweep._EOL_CENSUS_CADENCE_WINDOW_SECONDS

    machine_load_ms_per_window = (
        REPO_COUNT_LOAD_NORM_UPPER_BOUND
        * SPAWNS_PER_REPO_PER_CENSUS
        * PER_SPAWN_COST_MS_WORST_CASE
    )
    machine_load_fraction = machine_load_ms_per_window / (cadence_window_seconds * 1000)

    assert machine_load_fraction <= MAX_MACHINE_LOAD_FRACTION, (
        "eol.census cadence rider's projected fleet-wide machine load "
        f"({machine_load_fraction:.4f}, i.e. {machine_load_fraction * 100:.1f}% "
        f"of a {cadence_window_seconds:.0f}s cadence window) exceeds the "
        f"recorded budget ceiling ({MAX_MACHINE_LOAD_FRACTION:.2f}). Per "
        "docs/wiki/cost-budgets-and-the-kill-disposition.md, this is a KILL "
        "CANDIDATE for the rider, not license to raise "
        "MAX_MACHINE_LOAD_FRACTION."
    )


def test_formula_inputs_are_the_documented_values():
    """Pin on the named inputs themselves -- a silent edit to any of these
    is a budget change and must show up as a diff to this test, not an
    invisible constant tweak."""
    assert REPO_COUNT_LOAD_NORM_UPPER_BOUND == 70
    assert SPAWNS_PER_REPO_PER_CENSUS == 3
    assert PER_SPAWN_COST_MS_WORST_CASE == 500
    assert MAX_MACHINE_LOAD_FRACTION == 0.15
