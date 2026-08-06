"""
coordinator_core.testing.test_full_runner — scoped tests for the CLI
entrypoint (`full_runner.py`, C3) owning the DEC-1 exit contract.

Purpose: exercises AC2 (process-exit contract, proven via an actual
subprocess invocation — Finding 6), AC7 (--expect-named zero-file family ->
non-zero exit), AC8 (tally line shape), and AC9 (no --expect + empty family
-> exit 0, incl. a --repo . self-run smoke), plus the Finding-5
timeout-surfaces-as-failed discipline, against the shared `fixture_tree`
factory (`conftest.py`, Finding 13).

Port source: none — net-new (DR-059 harness authoring).
Spec backlink: docs/plans/2026-07-19-claude-klabauter-doe-full-test-runner.md § C3

Negative-spec:
    - Does NOT rely solely on in-process `main([...])` return-value checks
      to prove the DEC-1 exit contract (AC2) — those are supplements only;
      the load-bearing assertion is an actual out-of-process invocation of
      `python -m coordinator_core.testing.full_runner ...` (via the `_run_cli`
      helper) asserting on `.returncode` (Finding 6).
    - Does NOT write any dummy `test_*.py` / `.test.py` fixture file anywhere
      under `coordinator_core/` — every fixture suite (passing and failing)
      is materialized under pytest's `tmp_path` at runtime only via
      `conftest.fixture_tree` or local `tmp_path` writes (Finding 9). A
      committed failing dummy would permanently red claude-klabauter's own suite
      (`testpaths=["coordinator_core"]`, `python_files=["test_*.py"]`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.testing.full_runner import main

# _run_cli spawns a real subprocess that imports coordinator_core. That child
# inherits cwd but NOT pytest's rootdir sys.path insertion, so it can only
# resolve the package when cwd is (or is under) the repo root -- from any
# other cwd it dies with ModuleNotFoundError before it can write anything to
# stdout. Pinning cwd to the repo root derived from this file's own path
# makes the subprocess resolvable regardless of the invoking shell's cwd.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    """The load-bearing AC2 helper: an ACTUAL subprocess invocation of the
    module, never an in-process `main()` call (Finding 6)."""
    return subprocess.run(
        [sys.executable, "-m", "coordinator_core.testing.full_runner", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
    )


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# AC2 — process exit is 0 iff every collected suite passes (subprocess-proven)
# ---------------------------------------------------------------------------


def test_subprocess_exit_zero_on_all_pass(fixture_tree) -> None:
    tree = fixture_tree()
    proc = _run_cli(["--repo", str(tree.repo_root), "--jobs", "1"])
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_subprocess_exit_nonzero_on_injected_failure(fixture_tree) -> None:
    tree = fixture_tree(
        extra_files=(("fail.test.py", "import sys\nsys.exit(1)\n"),)
    )
    proc = _run_cli(["--repo", str(tree.repo_root), "--jobs", "1"])
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_inprocess_main_supplement_matches_subprocess_contract(fixture_tree) -> None:
    # Supplement only (Finding 6) — the subprocess tests above are load-bearing.
    tree = fixture_tree()
    assert main(["--repo", str(tree.repo_root), "--jobs", "1"]) == 0

    failing_tree = fixture_tree(
        root=tree.repo_root.parent / "repo-failing",
        extra_files=(("fail.test.py", "import sys\nsys.exit(1)\n"),),
    )
    assert main(["--repo", str(failing_tree.repo_root), "--jobs", "1"]) != 0


# ---------------------------------------------------------------------------
# AC7 — --expect-named zero-file family -> non-zero exit (loud warning)
# ---------------------------------------------------------------------------


def test_expect_named_empty_family_fails(tmp_path: Path) -> None:
    # A bare, minimal repo tree with NO js-suffix suites at all.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write(repo_root / "test_example.py", "def test_ok():\n    assert True\n")

    proc = _run_cli(
        ["--repo", str(repo_root), "--expect", "js-suffix", "--jobs", "1"]
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "js-suffix" in proc.stderr
    assert "WARN" in proc.stderr


def test_expect_all_fails_when_any_family_empty(fixture_tree) -> None:
    tree = fixture_tree()
    # Remove the js-suffix suite so one of the four families is empty.
    tree.family_files["js-suffix"].unlink()

    proc = _run_cli(["--repo", str(tree.repo_root), "--expect", "all", "--jobs", "1"])
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "js-suffix" in proc.stderr


# ---------------------------------------------------------------------------
# AC9 — no --expect + empty family -> exit 0 (warn only, stays green)
# ---------------------------------------------------------------------------


def test_no_expect_empty_family_stays_green(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write(repo_root / "test_example.py", "def test_ok():\n    assert True\n")

    proc = _run_cli(["--repo", str(repo_root), "--jobs", "1"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Still warns loudly even though it doesn't fail.
    assert "WARN" in proc.stderr


def test_repo_dot_self_run_smoke_no_expect(tmp_path: Path) -> None:
    # Hermetic analogue of `--repo .` against claude-klabauter's own tree: a minimal
    # synthetic repo with exactly one non-empty family (py-native) and three
    # legitimately-empty families, no --expect. Must stay green (AC9).
    repo_root = tmp_path / "self-run-repo"
    repo_root.mkdir()
    _write(
        repo_root / "test_smoke.py",
        "def test_smoke():\n    assert True\n",
    )
    proc = _run_cli(["--repo", str(repo_root), "--jobs", "1"])
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_repo_dot_family_warn_semantics_no_expect(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # Belt assertion for the family warn-not-fail path (no --expect): an
    # empty family warns loudly but never flips the exit code, scoped via
    # `--families js-prefix` against a hermetic synthetic repo (tmp_path) —
    # NOT the real `--repo .` tree. A prior version of this test ran
    # literally against claude-klabauter's own repo root on the (once-true) premise
    # that `js-prefix` (`test-*.js`) matched zero real files here; that
    # premise broke when real `test-*.js` suites landed under `coordinator/`
    # (vendored plugin content), some of which genuinely fail for reasons
    # unrelated to this test (e.g. a missing schema fixture) — those real
    # failures correctly flip the DEC-1 exit contract non-zero, exposing the
    # test's now-false assumption rather than a full_runner bug. A synthetic
    # tree with a real py-native suite and a deliberately-absent js-prefix
    # suite proves the same warn-not-fail semantics without depending on
    # `coordinator/`'s mutable real content ever staying family-empty.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write(repo_root / "test_example.py", "def test_ok():\n    assert True\n")

    rc = main(["--repo", str(repo_root), "--families", "js-prefix", "--jobs", "1"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "js-prefix" in captured.err
    assert "WARN" in captured.err


# ---------------------------------------------------------------------------
# AC8 — tally line shape
# ---------------------------------------------------------------------------


def test_tally_line_shape(fixture_tree) -> None:
    tree = fixture_tree()
    proc = _run_cli(["--repo", str(tree.repo_root), "--jobs", "1"])
    lines = proc.stdout.splitlines()
    tally_lines = [
        line for line in lines if "passed" in line and "failed" in line and "across" in line
    ]
    assert len(tally_lines) == 1, proc.stdout
    tally = tally_lines[0]
    assert " passed, " in tally
    assert " failed across " in tally
    assert " families (" in tally
    assert tally.rstrip().endswith("s)")


def test_per_suite_pass_fail_streamed(fixture_tree) -> None:
    tree = fixture_tree(
        extra_files=(("fail.test.py", "import sys\nsys.exit(1)\n"),)
    )
    proc = _run_cli(["--repo", str(tree.repo_root), "--jobs", "1"])
    assert "[PASS]" in proc.stdout
    assert "[FAIL]" in proc.stdout


# ---------------------------------------------------------------------------
# Timeout discipline (Finding 5) — surfaces as FAILED, not skipped
# ---------------------------------------------------------------------------


def test_timeout_surfaces_as_failed_in_tally(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write(repo_root / "slow.test.py", "import time\ntime.sleep(5)\n")

    proc = _run_cli(["--repo", str(repo_root), "--jobs", "1", "--timeout", "1"])
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "[FAIL]" in proc.stdout


# ---------------------------------------------------------------------------
# DR-088 layer 6 (R10, 2026-07-28) -- mutex wiring.
#
# The mutex module shipped well-tested and the PreToolUse guard consumed
# `holder()` correctly, but no production code ever called `acquire()`: the
# lock was never taken, `holder()` always returned None, and the leg that was
# supposed to serialize concurrent suite runs against a shared tree had never
# once fired. These tests exist so that regression cannot recur silently --
# "the module is tested" is what made it look shipped for five days.
# ---------------------------------------------------------------------------

def test_run_holds_the_suite_mutex_for_the_duration_of_the_run(fixture_tree, monkeypatch):
    """The take side. `holder()` must be non-None WHILE the suites run --
    asserting only that `acquire` was called would pass against a lock taken
    and released before the run it is meant to cover."""
    from coordinator_core.testing import full_runner, suite_mutex

    observed = {}
    real_run_suites = full_runner.run_suites

    def spy(*args, **kwargs):
        observed["holder_during_run"] = suite_mutex.holder()
        return real_run_suites(*args, **kwargs)

    tree = fixture_tree()
    monkeypatch.setattr(full_runner, "run_suites", spy)
    assert full_runner.main(["--repo", str(tree.repo_root), "--jobs", "1"]) == 0

    assert observed["holder_during_run"] is not None, (
        "suite mutex was not held during the run — layer 6 is inert again"
    )
    assert suite_mutex.holder() is None, "mutex not released after the run"


def test_mutex_released_when_the_run_raises(fixture_tree, monkeypatch):
    """A crashed run must not leave the machine-wide lock held — every later
    suite run on this machine would queue behind a process that is gone."""
    from coordinator_core.testing import full_runner, suite_mutex

    def boom(*args, **kwargs):
        raise RuntimeError("injected")

    tree = fixture_tree()
    monkeypatch.setattr(full_runner, "run_suites", boom)
    with pytest.raises(RuntimeError):
        full_runner.main(["--repo", str(tree.repo_root), "--jobs", "1"])

    assert suite_mutex.holder() is None


def test_contended_run_proceeds_unserialized_rather_than_failing(fixture_tree, monkeypatch):
    """Fail-OPEN on contention, matching the guard's own mutex leg. A second
    runner that cannot take the lock within the wait budget still runs: a
    resource control must not become a correctness-irrelevant failure."""
    from coordinator_core.testing import full_runner

    tree = fixture_tree()
    monkeypatch.setattr(full_runner, "_MUTEX_WAIT_SECS", 0.0)
    monkeypatch.setattr(full_runner.suite_mutex, "acquire", lambda *a, **k: False)
    assert full_runner.main(["--repo", str(tree.repo_root), "--jobs", "1"]) == 0
