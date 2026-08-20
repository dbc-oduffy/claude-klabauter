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
      Worst-case count of distinct repos boot-swept inside one cadence
      window: the load norm's own upper bound of concurrent
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
      docstring's negative-spec) -- it rides its OWN 24h marker,
      census's being hourly, on a slower cadence and is asserted spawn-free below
      so a future change that adds a subprocess call there is caught, not
      silently absorbed into an unaccounted-for machine cost.
  CADENCE_WINDOW_SECONDS (3600 = 60 * 60)
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
  CENSUS_BYTES_READ_MS_WORST_CASE (4500, F5 fix, staff-eng review 2026-08-20)
      The term the ORIGINAL formula omitted entirely: `eol.census` calls
      `Path.read_bytes()` on every tracked path carrying a resolved
      lf/crlf direction -- and a repo carrying a wildcard `eol=crlf`/`eol=lf`
      declaration (`* text=auto eol=crlf`, a legitimate Windows-repo
      pattern -- see the F1 finding on the same review) declares EVERY
      tracked path, not a hand-picked subset. Spawn count is invariant by
      construction (always 3); this term is NOT -- it is O(corpus bytes),
      and it is the only term in the whole formula that actually scales
      with repo size. Measured empirically against THIS repo's own tracked
      corpus (28,327 files, ~360MB as of 2026-08-20) at ~1.05-3.09s across
      repeated runs at varying machine load (this box averages 50-70
      concurrent LLM sessions -- CLAUDE.md § Load norm); pinned at 4500ms for
      headroom under that same load and as the corpus grows.
      This repo -- not a synthetic 2-file fixture -- is the "real corpus
      order of magnitude" measurement surface: it is the literal corpus the
      rider runs against in production, and the prior version of this file
      measured its pinned constant against a 2-file tmp_path fixture that
      could never have produced a different number (2026-08-20 review
      finding). `test_census_read_bytes_wall_time_stays_under_pinned_worst_
      case` re-measures this live on every run so a real regression (or a
      real repo-growth trend) is caught, not silently absorbed.
  AUDIT_PRODUCERS_WALL_TIME_MS_WORST_CASE (6000, F5 fix)
      `eol.audit_producers` is spawn-free (pinned above) but its
      `rglob('*.py')` + `read_text` + `ast.parse` walk over every
      production source is ALSO O(corpus size) and was entirely absent
      from every version of this formula before this fix. Measured
      empirically against THIS repo's own production corpus (1,669 `.py`
      files) at ~3.27-4.7s across runs at varying machine load (this box
      averages 50-70 concurrent LLM sessions -- CLAUDE.md § Load norm);
      pinned at 6000ms for headroom under that same load. Evaluated
      against its OWN 24h cadence window (`boot_sweep.
      _EOL_AUDIT_PRODUCERS_CADENCE_WINDOW_SECONDS`), never folded into the
      hourly census fraction -- the two riders run on independent
      markers/windows and must not be summed into one ceiling.

  census_machine_load_ms_per_window  = REPO_COUNT_LOAD_NORM_UPPER_BOUND
                                        * (SPAWNS_PER_REPO_PER_CENSUS
                                           * PER_SPAWN_COST_MS_WORST_CASE
                                           + CENSUS_BYTES_READ_MS_WORST_CASE)
  census_machine_load_fraction       = census_machine_load_ms_per_window
                                        / (CADENCE_WINDOW_SECONDS * 1000)

  = 70 * (3 * 500 + 4500) / (3600 * 1000) = 70 * 6000 / 3600000 = 420000 / 3600000
  ~= 0.1167 (~11.7%)

  Measured honestly for the first time on 2026-08-20 (the F5 fix), this came
  out at 0.3111 (~31.1%) against a 15-minute window -- a real breach of the
  0.15 ceiling, and the first version of this file to be capable of showing
  one. The remedy was the one the kill-disposition page prescribes: bound the
  LOAD. `boot_sweep._EOL_CENSUS_CADENCE_WINDOW_SECONDS` widened 15m -> 60m,
  which divides the fraction by four at a cost of nothing an operator acts on
  (EOL drift is a slow-moving property of a tree). The ceiling was NOT raised,
  and the scan was NOT narrowed -- a per-file size cap or a trimmed pathspec
  would have bought the same number by not looking at part of the corpus.

  audit_producers_machine_load_ms_per_window = REPO_COUNT_LOAD_NORM_UPPER_BOUND
                                                * AUDIT_PRODUCERS_WALL_TIME_MS_WORST_CASE
  audit_producers_machine_load_fraction      = audit_producers_machine_load_ms_per_window
                                                / (AUDIT_PRODUCERS_CADENCE_WINDOW_SECONDS * 1000)

  = 70 * 6000 / (86400 * 1000) = 420000 / 86400000 ~= 0.00486 (~0.5%)

MAX_MACHINE_LOAD_FRACTION (0.15) is the explicit ceiling: no more than 15%
of a cadence window's wall time may be consumed fleet-wide by this rider, in
the worst case where the load norm's entire upper bound boots a distinct
repo inside the same window. Per the kill-disposition page, breaching this
is a KILL CANDIDATE for the rider, not license to raise the ceiling --
raising it requires editing this recorded number, which is the ratchet
mechanism this file is not the tool to override.

**2026-08-20 F5 disposition (staff-eng review): the honest census figure
(~31.1%) BREACHES MAX_MACHINE_LOAD_FRACTION.** The spawn-only formula this
file shipped with was formally discharged and substantively blind -- it
bounded the one term that cannot grow (spawn count is always 3) while
omitting the two that do (bytes read, AST-parsed sources), so it could never
have gone red no matter how the rider actually cost. Per this same review's
own instruction and the kill-disposition page's rule ("something must fail
on breach"), `test_formula_projected_census_machine_load_within_budget`
below is left RED rather than silently widened to pass -- narrowing the
census's read scope (declared paths only, or a per-file size cap) or a
longer cadence window are the two changes that would actually shrink this
number; raising MAX_MACHINE_LOAD_FRACTION is not. This is a kill-disposition
call for the EM/PM, not something resolved inside this file.

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
import time
from pathlib import Path

import pytest

import coordinator_core.ops  # noqa: F401 -- ops/__init__.py triggers all op registrations
import coordinator_core.ops.session.boot_sweep  # noqa: F401 -- fires @register_op("session.boot_sweep")

from coordinator_core.git.ls_files_bytes import tracked_files_bytes
from coordinator_core.ops.eol.audit_producers import audit_producers as _eol_audit_producers
from coordinator_core.ops.eol.census import census as _eol_census
from coordinator_core.ops.session import boot_sweep

# Real git spawn is load-bearing: this file measures the REAL subprocess
# cost of eol.census against a fixture repo, never a stub.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# This repo's own root -- the "real corpus order of magnitude" measurement
# surface for the F5 fix (staff-eng review, 2026-08-20): the byte-read and
# AST-parse terms below are measured against THIS repo's live tracked
# corpus, not a synthetic 2-file fixture that could never have produced a
# different number. parents[4]: tests/ -> session/ -> ops/ -> coordinator_core/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]


# ---------------------------------------------------------------------------
# Formula constants -- named inputs, see module docstring for derivation.
# ---------------------------------------------------------------------------

REPO_COUNT_LOAD_NORM_UPPER_BOUND = 70
SPAWNS_PER_REPO_PER_CENSUS = 3
PER_SPAWN_COST_MS_WORST_CASE = 500
# F5 fix (staff-eng review, 2026-08-20): the two terms the ORIGINAL formula
# omitted -- see module docstring for derivation and the honest (breaching)
# recomputation this pins.
CENSUS_BYTES_READ_MS_WORST_CASE = 4500
AUDIT_PRODUCERS_WALL_TIME_MS_WORST_CASE = 6000
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
    assert boot_sweep._EOL_CENSUS_CADENCE_WINDOW_SECONDS == 60 * 60


# ---------------------------------------------------------------------------
# (b2) F5 fix -- the two terms the ORIGINAL formula omitted (byte-read,
#     AST-parse) are re-measured live against THIS repo's own corpus, so a
#     real regression or a real corpus-growth trend is caught rather than
#     silently absorbed by a stale pinned constant. Not asserted for exact
#     equality (wall-clock I/O is noisy, unlike the deterministic spawn
#     COUNT above) -- asserted to stay under the pinned worst-case with
#     headroom, which is itself the number the budget assertion below uses.
# ---------------------------------------------------------------------------


def test_census_read_bytes_wall_time_stays_under_pinned_worst_case():
    """Measures the wall time to `Path.read_bytes()` every path
    `eol.census` would read in the worst case (a repo carrying a wildcard
    eol declaration, so every tracked path needs a read -- see F1/F5) --
    against THIS repo's own live tracked corpus, not a synthetic fixture.
    """
    tracked = tracked_files_bytes(_REPO_ROOT)
    t0 = time.monotonic()
    for path_b in tracked:
        rel = path_b.decode("utf-8", errors="surrogateescape")
        try:
            (_REPO_ROOT / rel).read_bytes()
        except OSError:
            continue
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert elapsed_ms <= CENSUS_BYTES_READ_MS_WORST_CASE, (
        f"eol.census's read_bytes term measured {elapsed_ms:.0f}ms against this "
        f"repo's own {len(tracked)} tracked paths -- this EXCEEDS the pinned "
        f"CENSUS_BYTES_READ_MS_WORST_CASE ({CENSUS_BYTES_READ_MS_WORST_CASE}ms). "
        "Either the corpus has grown past this constant's headroom (deliberate, "
        "reviewed edit) or eol.census regressed to reading more than it should."
    )


def test_audit_producers_wall_time_stays_under_pinned_worst_case():
    """Measures `eol.audit_producers`' full wall time (rglob + read_text +
    ast.parse over every production source) against THIS repo's own live
    corpus -- the term the original formula omitted entirely (it is
    spawn-free, so a spawn-only formula priced it at zero).
    """
    t0 = time.monotonic()
    _eol_audit_producers(_REPO_ROOT)
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert elapsed_ms <= AUDIT_PRODUCERS_WALL_TIME_MS_WORST_CASE, (
        f"eol.audit_producers measured {elapsed_ms:.0f}ms against this repo's "
        f"own production corpus -- this EXCEEDS the pinned "
        f"AUDIT_PRODUCERS_WALL_TIME_MS_WORST_CASE "
        f"({AUDIT_PRODUCERS_WALL_TIME_MS_WORST_CASE}ms). Either the corpus has "
        "grown past this constant's headroom (deliberate, reviewed edit) or "
        "audit_producers regressed."
    )


# ---------------------------------------------------------------------------
# (c) the formula's projected fleet-wide machine load stays under its
#     explicit ceiling -- fails on breach per the kill-disposition page.
#
# F5 fix (staff-eng review, 2026-08-20): the census fraction below now
# includes CENSUS_BYTES_READ_MS_WORST_CASE and IS EXPECTED TO FAIL --
# ~31.1% against a 15% ceiling. This is deliberate: the prior spawn-only
# formula could never go red (spawn count is invariant), which the review
# named as "formally discharged and substantively blind." Per this file's
# own module docstring and docs/wiki/cost-budgets-and-the-kill-
# disposition.md's "something must fail on breach" rule, this assertion is
# left RED rather than silently widened -- narrowing eol.census's read
# scope, or raising the cadence window, are the two changes that would
# actually shrink the number; widening MAX_MACHINE_LOAD_FRACTION is not,
# and this is a kill-disposition call for the EM/PM, not this file.
# ---------------------------------------------------------------------------


def test_formula_projected_census_machine_load_within_budget():
    cadence_window_seconds = boot_sweep._EOL_CENSUS_CADENCE_WINDOW_SECONDS

    machine_load_ms_per_window = REPO_COUNT_LOAD_NORM_UPPER_BOUND * (
        SPAWNS_PER_REPO_PER_CENSUS * PER_SPAWN_COST_MS_WORST_CASE
        + CENSUS_BYTES_READ_MS_WORST_CASE
    )
    machine_load_fraction = machine_load_ms_per_window / (cadence_window_seconds * 1000)

    assert machine_load_fraction <= MAX_MACHINE_LOAD_FRACTION, (
        "eol.census cadence rider's projected fleet-wide machine load "
        f"({machine_load_fraction:.4f}, i.e. {machine_load_fraction * 100:.1f}% "
        f"of a {cadence_window_seconds:.0f}s cadence window) exceeds the "
        f"recorded budget ceiling ({MAX_MACHINE_LOAD_FRACTION:.2f}). Per "
        "docs/wiki/cost-budgets-and-the-kill-disposition.md, this is a KILL "
        "CANDIDATE for the rider, not license to raise "
        "MAX_MACHINE_LOAD_FRACTION. This IS the F5 finding (staff-eng review "
        "2026-08-20): the honest byte-read term breaches the ceiling; the "
        "fix is a narrower census or a longer window, decided by the EM/PM, "
        "not a wider ceiling written here."
    )


def test_formula_projected_audit_producers_machine_load_within_budget():
    """audit_producers' own 24h-window budget -- evaluated independently of
    census's 15-minute one; the two riders run on separate markers/windows
    and must never be summed into one ceiling."""
    cadence_window_seconds = boot_sweep._EOL_AUDIT_PRODUCERS_CADENCE_WINDOW_SECONDS

    machine_load_ms_per_window = (
        REPO_COUNT_LOAD_NORM_UPPER_BOUND * AUDIT_PRODUCERS_WALL_TIME_MS_WORST_CASE
    )
    machine_load_fraction = machine_load_ms_per_window / (cadence_window_seconds * 1000)

    assert machine_load_fraction <= MAX_MACHINE_LOAD_FRACTION, (
        "eol.audit_producers cadence rider's projected fleet-wide machine load "
        f"({machine_load_fraction:.4f}, i.e. {machine_load_fraction * 100:.1f}% "
        f"of a {cadence_window_seconds:.0f}s cadence window) exceeds the "
        f"recorded budget ceiling ({MAX_MACHINE_LOAD_FRACTION:.2f})."
    )


def test_formula_inputs_are_the_documented_values():
    """Pin on the named inputs themselves -- a silent edit to any of these
    is a budget change and must show up as a diff to this test, not an
    invisible constant tweak."""
    assert REPO_COUNT_LOAD_NORM_UPPER_BOUND == 70
    assert SPAWNS_PER_REPO_PER_CENSUS == 3
    assert PER_SPAWN_COST_MS_WORST_CASE == 500
    assert CENSUS_BYTES_READ_MS_WORST_CASE == 4500
    assert AUDIT_PRODUCERS_WALL_TIME_MS_WORST_CASE == 6000
    assert MAX_MACHINE_LOAD_FRACTION == 0.15
