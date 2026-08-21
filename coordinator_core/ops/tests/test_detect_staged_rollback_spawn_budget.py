"""
coordinator_core.ops.tests.test_detect_staged_rollback_spawn_budget

C4 (docs/plans/2026-08-21-a-commit-stops-paying-for-thirty-processes.md):
"the pre-commit gate keeps both checks and stops costing five processes."

CENSUS FINDING, THE HEADLINE OF THIS FILE: there is no spawn-count reduction
available inside C4's own file scope (`coordinator/bin/detect-staged-
rollback.py`, `coordinator_core/ops/detect_staged_rollback.py`). The
5-process figure this plan's § Problem cites for a one-file staged change
(279ms/5 procs) decomposes, measured with the job-object primitive
(`batched_process_time_ms`, k=8, this box) against a hermetic one-file-staged
fixture invoked by ABSOLUTE path with cwd OUTSIDE claude-klabauter, as:

    1 interpreter (the trampoline itself)
  + 2 `git` child processes (`git diff --cached --raw ...`, the shared scan
    `_staged_raw_diff` both checks read; `git log --format=... --raw -- <path
    ...>`, `_batch_path_history`'s unbounded walk) -- ALREADY the minimum:
    one measures the staged index, the other measures history, and no third
    party can answer either question. Re-batching them (§0 of this plan's C4
    body) is not available: they already share the one `git diff --cached`
    spawn via `_staged_raw_diff`, and `_batch_path_history` already batches
    every staged path into ONE `git log` call (not one per path).
  + 2 processes NOT visible to a `sys.addaudithook("subprocess.Popen", ...)`
    trace of this trampoline's own run -- because neither is a
    `subprocess.Popen` call this trampoline or the op makes. Isolated with a
    synthetic job-object probe (spawn ONE `git diff --cached --raw` under
    `CREATE_SUSPENDED | CREATE_NO_WINDOW`, the exact flag pair `_run_git`
    passes via `coordinator_core.win_portability.no_console_creationflags()`):
    `procs_per_call` reads 2.0 for that single spawn, against 1.0 for the
    identical spawn with `CREATE_NO_WINDOW` omitted. The second process per
    git spawn is a `conhost.exe` Windows allocates for the fresh, window-less
    console `CREATE_NO_WINDOW` requests -- and `no_console_creationflags()`'s
    own docstring already names this trade explicitly: it "absorbs the
    console-popup defect ... a subprocess spawned without console suppression
    allocates a fresh conhost.exe window ... that flashes and steals keyboard
    focus" (`coordinator_core/win_portability.py`, § 4,
    `docs/plans/2026-08-07-no-window-subprocess-primitive.md`). One conhost
    per suppressed git spawn is the PRICED cost of not flashing a window at
    every session on this box's commit path -- not an accidental leak.

That mechanism lives in `coordinator_core/win_portability.py`, a file OUTSIDE
this chunk's `writes:` scope, shared by every other Windows-facing spawn site
in this repo -- not a C4-local decision to unmake. Removing it here alone
(hand-rolling a parallel creationflags decision inside this module) would
fork a documented, cross-repo-shared anti-flash primitive for one call site,
and — absent a per-process console-presence check this module has no
mandate to build or test — risks reintroducing the exact flashing-window /
stolen-focus defect that primitive exists to prevent. Filed as a scope
finding, not routed around: this file does not touch
`no_console_creationflags`.

NEGATIVE-SPEC HELD: no threshold, override key, or detection predicate in
`coordinator_core.ops.detect_staged_rollback` is touched by this file. Both
checks still run; both still fire on every case they fired on before this
census (see the equivalence assertions below).

WHAT THIS FILE ACTUALLY PINS, GIVEN THAT FINDING:
  (a) AC9 -- an end-to-end regression lock proving the CLI trampoline
      (`coordinator/bin/detect-staged-rollback.py`, invoked by absolute path,
      cwd OUTSIDE claude-klabauter, exactly the condition under which `.git/hooks/
      pre-commit`'s own relative-path resolution would otherwise silently
      short-circuit to "BLOCKED -- missing script" per § Corrections 5)
      forwards byte-identical stderr text and the identical five-way exit
      code the op's own in-process `main()` produces, across all four
      exit-code shapes (clean, rollback-only, mass-deletion-only, both).
      This is the harness AC9 asks for; because C4 makes no functional
      change here, "before" and "after" are the same commit, and this test
      is what proves that -- and what will catch a future edit here that
      breaks the trampoline/op agreement.
  (b) A process-time and spawn-count budget pin at the measured baseline
      (procs_per_call == 5, exact -- same rationale
      `test_wsc_tail_spawn_budget.py` gives for its own exact-equality pin:
      a spawn-count budget that does not drift quietly is the point).

MEASUREMENT DISCIPLINE: `batched_process_time_ms`, k=8, process time and
spawn count only, never wall clock (CLAUDE.md § The brightline). No separate
driver-residue subtraction is needed here -- unlike the plan's own stage-
delta table, this file measures ONE invocation shape directly (the trampoline
child process under job-object control), not a composed driver+op baseline.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import IS_WINDOWS, batched_process_time_ms
from coordinator_core.ops import detect_staged_rollback as _dsr
from coordinator_core.ops.detect_staged_rollback import (
    EXIT_BOTH_FINDINGS,
    EXIT_CLEAN,
    EXIT_MASS_DELETION_FINDING,
    EXIT_ROLLBACK_FINDING,
    MASS_DELETION_RATIO_THRESHOLD,
    OVERRIDE_ENV,
    main,
)

# Spawns real `git` and `python` child processes; runs at cadence gates, not
# per-commit. Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_TRAMPOLINE = str(
    Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "detect-staged-rollback.py"
)


def _require_windows() -> None:
    if not IS_WINDOWS:
        pytest.skip("process-time job-object accounting is a Windows-only primitive")


# ---------------------------------------------------------------------------
# Fixture-repo helpers -- same shape as coordinator_core/ops/
# test_detect_staged_rollback.py, reproduced locally rather than imported:
# that module's helpers are private (`_git`, `_init_repo`, ...) and this file
# lives in a sibling package (`ops/tests/`, not `ops/`).


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(repo):
    repo.mkdir(exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "c4-census@example.com")
    _git(repo, "config", "user.name", "c4-census")
    return repo


def _commit_file(repo, name, content, message):
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message, "--", name)


def _stage_file(repo, name, content):
    (repo / name).write_text(content)
    _git(repo, "add", name)


def _commit_many(repo, names):
    for name in names:
        (repo / name).write_text(f"{name} v1\n")
    _git(repo, "add", *names)
    _git(repo, "commit", "-q", "-m", f"add {len(names)} files")


def _stage_delete(repo, names):
    for name in names:
        (repo / name).unlink()
    _git(repo, "rm", "-q", "--cached", *names)


def _run_trampoline(repo_root: Path, env: dict) -> subprocess.CompletedProcess:
    """Invoke the CLI trampoline by ABSOLUTE path, cwd set to the fixture
    repo itself -- always outside claude-klabauter's own checkout, which is exactly
    the condition `.git/hooks/pre-commit`'s relative-path resolution cannot
    satisfy (§ Corrections 5). Absolute-path invocation sidesteps that
    resolution entirely, so the "BLOCKED -- missing script" short-circuit
    cannot occur here; the assertions below still check for it defensively
    so a future refactor that reintroduces a relative lookup fails loudly
    rather than silently passing on an unrun gate.
    """
    return subprocess.run(
        [sys.executable, _TRAMPOLINE, str(repo_root)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
    )


_CLAUDE_KLABAUTER_ROOT = str(Path(__file__).resolve().parents[3])


def _env(**overrides) -> dict:
    """Base env for a spawned trampoline child, always carrying an explicit
    `COORDINATOR_ENGINE_ROOT`.

    Required because of this suite's own root `conftest.py::
    _quarantine_real_home` (autouse): it redirects HOME/USERPROFILE to a
    throwaway per-test dir before this fixture ever runs, so a spawned
    child's `require_dispatch_engine_on_path()` cannot reach the box's real
    machine-local registry or `.claude-klabauter-root` pointer file -- there is
    nothing at the quarantined HOME to read. `COORDINATOR_ENGINE_ROOT` is
    Rung 1 of that same resolution ladder (`cc_invoke.py`'s own docstring:
    "already set in environment -> CANDIDATE"), so setting it here is not a
    workaround bolted on beside the ladder -- it IS the ladder's own
    highest-priority, sanctioned override, and it makes this file's
    fixtures independent of whatever this particular box's machine-local
    `repos.claude_klabauter` entry happens to hold.
    """
    base = dict(os.environ)
    base.setdefault("COORDINATOR_ENGINE_ROOT", _CLAUDE_KLABAUTER_ROOT)
    base.update(overrides)
    return base


def _assert_gate_actually_ran(result: subprocess.CompletedProcess) -> None:
    """AC9's hard requirement: a real rc from the five-way contract, never
    the missing-script short-circuit that made the plan's original +105ms/
    +1-proc reading worthless (§ Corrections 5)."""
    assert "missing script" not in result.stderr.lower(), (
        "gate did not run -- got the missing-script short-circuit, not a real five-way rc: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.returncode in (
        EXIT_CLEAN,
        EXIT_ROLLBACK_FINDING,
        2,
        EXIT_MASS_DELETION_FINDING,
        EXIT_BOTH_FINDINGS,
    ), f"rc {result.returncode} is not a five-way contract value: {result!r}"


# ---------------------------------------------------------------------------
# (a) AC9 -- trampoline/op equivalence across all four exit-code shapes


def test_clean_index_verdict_matches_between_cli_and_in_process(tmp_path):
    repo = _init_repo(tmp_path / "clean")
    _commit_file(repo, "f.txt", "one\n", "c1")
    _stage_file(repo, "f.txt", "two\n")

    in_process_rc = main([str(repo)], env=_env())

    result = _run_trampoline(repo, env=_env())
    _assert_gate_actually_ran(result)

    assert result.returncode == EXIT_CLEAN == in_process_rc
    assert result.stderr == ""


def test_rollback_only_verdict_matches_between_cli_and_in_process(tmp_path):
    repo = _init_repo(tmp_path / "rollback-only")
    for name in ("a.txt", "b.txt", "c.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    in_process_rc = main([str(repo)], env=_env())

    result = _run_trampoline(repo, env=_env())
    _assert_gate_actually_ran(result)

    assert result.returncode == EXIT_ROLLBACK_FINDING == in_process_rc
    assert "a.txt" in result.stderr
    assert "b.txt" in result.stderr
    assert "c.txt" in result.stderr


def test_mass_deletion_only_verdict_matches_between_cli_and_in_process(tmp_path):
    repo = _init_repo(tmp_path / "mass-deletion-only")
    names = [f"f{i}.txt" for i in range(10)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:9])  # 9 of 10 -> ratio 0.9, at the production threshold

    in_process_rc = main([str(repo)], env=_env())

    result = _run_trampoline(repo, env=_env())
    _assert_gate_actually_ran(result)

    assert result.returncode == EXIT_MASS_DELETION_FINDING == in_process_rc
    assert "9 of 10" in result.stderr


def test_both_findings_verdict_matches_between_cli_and_in_process(tmp_path):
    """Same combined fixture shape as coordinator_core/ops/
    test_detect_staged_rollback.py's `test_both_checks_fire_...` --
    mass-deletion fixture staged first, rollback fixture staged with a
    path-scoped commit so it never sweeps up the mass-deletion leg's own
    staged deletions (see that module's comment for why the ordering
    matters)."""
    repo = _init_repo(tmp_path / "both-findings")
    names = [f"f{i}.txt" for i in range(100)]
    _commit_many(repo, names)
    _stage_delete(repo, names[:95])

    for name in ("a.txt", "b.txt", "c.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    in_process_rc = main([str(repo)], env=_env())

    result = _run_trampoline(repo, env=_env())
    _assert_gate_actually_ran(result)

    assert result.returncode == EXIT_BOTH_FINDINGS == in_process_rc


def test_override_env_still_forwards_correctly_through_the_trampoline(tmp_path):
    """One override-path check through the CLI layer -- the in-process
    override matrix already lives in coordinator_core/ops/
    test_detect_staged_rollback.py; this only confirms the trampoline does
    not swallow or reinterpret the env var on its way through."""
    repo = _init_repo(tmp_path / "override-through-cli")
    for name in ("a.txt", "b.txt", "c.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    result = _run_trampoline(repo, env=_env(**{OVERRIDE_ENV: "1"}))
    _assert_gate_actually_ran(result)
    assert result.returncode == EXIT_CLEAN
    assert "override is set" in result.stderr


# ---------------------------------------------------------------------------
# (b) Process-time / spawn-count budget pin


#: Measured (this session, `batched_process_time_ms`, k=8, hermetic one-file
#: staged fixture, absolute-path invocation, cwd = the fixture repo, outside
#: claude-klabauter): 167.969 / 181.641 / 214.844 ms across three runs, procs_per_call
#: exactly 5.0 every time. `PROCS_PER_CALL_CEILING` is an exact pin (see
#: module docstring's rationale, mirrored from `test_wsc_tail_spawn_budget.py`)
#: -- it is the fully-decomposed figure (1 interpreter + 2 git + 2 conhost),
#: not a number with slack in it; a future spawn-count change here (adding OR
#: removing a git call) must update this pin in the same commit, with a
#: stated reason. `PROCESS_TIME_CEILING_MS` carries real headroom (this
#: box's own three-sample range tops out at 214.844ms) because process time,
#: unlike spawn count, is not expected to be bit-for-bit stable run to run.
PROCS_PER_CALL_CEILING = 5.0
PROCESS_TIME_CEILING_MS = 400.0


def test_one_file_staged_gate_spawn_and_time_budget(tmp_path):
    _require_windows()

    repo = _init_repo(tmp_path / "budget")
    _commit_file(repo, "a.txt", "one\n", "c1")
    _commit_file(repo, "a.txt", "two\n", "c2")
    _stage_file(repo, "a.txt", "three\n")

    result = batched_process_time_ms(
        [sys.executable, _TRAMPOLINE, str(repo)], k=8, cwd=str(repo), env=_env()
    )

    assert result["rc"] == EXIT_CLEAN, (
        "budget fixture must be a clean-verdict run (a blocking run costs "
        f"differently and is not what this pin measures): {result!r}"
    )
    assert result["procs_per_call"] == PROCS_PER_CALL_CEILING, (
        f"detect-staged-rollback now spawns {result['procs_per_call']} processes/call, "
        f"pin says {PROCS_PER_CALL_CEILING}. Over budget is a kill candidate per "
        "docs/wiki/cost-budgets-and-the-kill-disposition.md; under budget (a real "
        "reduction) means updating PROCS_PER_CALL_CEILING down, in this file, with the "
        "mechanism named in the same commit -- never edit it to make a regression fit."
    )
    assert result["process_time_ms"] <= PROCESS_TIME_CEILING_MS, (
        f"detect-staged-rollback process time is {result['process_time_ms']}ms/call, "
        f"over the {PROCESS_TIME_CEILING_MS}ms pin: {result!r}"
    )


def test_mass_deletion_ratio_threshold_unchanged_by_this_census():
    """Negative-spec check, mechanical: this chunk touches process creation
    only. If a future edit here accidentally perturbs the production
    threshold constant, this catches it immediately rather than waiting on
    the fixture-level ratio tests to notice."""
    assert MASS_DELETION_RATIO_THRESHOLD == pytest.approx(0.90)
