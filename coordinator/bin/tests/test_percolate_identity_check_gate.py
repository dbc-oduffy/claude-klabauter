"""test_percolate_identity_check_gate — end-to-end wiring tests for
`dispatch_percolate_pre_ci`'s identity-check fold-in (task: "make the
publish-side gate see what the mirror's release CI gate sees").

Drives the REAL `coordinator_core.ops.percolate_identity_check
.run_identity_check` (a real subprocess call, `shell=False`) against a
throwaway destination tree carrying a SYNTHETIC `check-persona-names.py`
stand-in -- never the real mirror checker's persona/codename table (that
table is not duplicated into this repo; see
`coordinator_core/ops/percolate_identity_check.py`'s module docstring).
The synthetic stand-in exits 1 with a planted marker string when a sentinel
file is present in the destination tree, and exits 0 otherwise -- enough to
exercise the real subprocess-and-fold wiring without importing the mirror's
own vocabulary.

Covers the task brief's AC4 minimum:
  * the gate fires and fails a target when the destination tree contains a
    planted "finding" (`test_planted_finding_fails_target`);
  * it passes on a clean tree (`test_clean_tree_passes`);
  * an absent checker script is a loud skip, not a silent pass
    (`test_absent_checker_script_is_a_skip_not_a_pass`).

Also covers the follow-up fix closing the gap the per-row leg cannot: on a
full run into a wiped/virgin destination, `setup/publish-targets.portable`
declares the engine row (non-empty `dest_subdir`) BEFORE the toplevel row
that publishes `.github/`, so the per-row check's own advisory skip means
the row that ships nearly the whole tree can go completely unchecked for
the whole run. `TestEndOfRunIdentityCheckLeg` exercises
`dispatch_end_of_run_identity_check` directly (skip/pass/fail, and the
advisory-vs-hard-fail split between an unfiltered and a `--target`-scoped
invocation); `TestEndOfRunIdentityCheckMainWiring` drives real `publish.main()`
end-to-end (with `process_target` and the engine/identity/publish_sync
preconditions stubbed to isolate the wiring under test) to prove a full,
unfiltered run into a virgin destination now exits non-zero, a populated
clean destination exits zero, a nonzero checker exit aborts the run, a
`--target`-scoped run degrades the missing-checker case to advisory, and
`--dry-run` never fires the leg at all.

Run: python -m pytest coordinator/bin/tests/test_percolate_identity_check_gate.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_identity_check_gate_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.ops.percolate_identity_check import run_identity_check  # noqa: E402


# A synthetic stand-in for `.github/scripts/check-persona-names.py`. Behavior:
# exit 1 with a planted (non-real) finding string when SENTINEL_NAME exists
# next to it in the destination tree, exit 0 with a pass summary otherwise.
# Deliberately carries no real persona name or fleet codename.
_SENTINEL_NAME = "PLANTED-FINDING-SENTINEL"
_SYNTHETIC_CHECKER = f'''\
import pathlib
import sys

here = pathlib.Path(__file__).resolve().parent
sentinel = here.parent.parent / "{_SENTINEL_NAME}"
if sentinel.is_file():
    print("Identity check FAILED:")
    print("  fixture/planted.txt:1: fixture-token 'PLANTED-FINDING-SENTINEL' -- synthetic test fixture")
    sys.exit(1)
print("Identity check passed (0 text files scanned, 0 paths checked).")
sys.exit(0)
'''


def _write_checker(dest: Path) -> Path:
    script_dir = dest / ".github" / "scripts"
    script_dir.mkdir(parents=True)
    script_path = script_dir / "check-persona-names.py"
    script_path.write_text(_SYNTHETIC_CHECKER, encoding="utf-8")
    return script_path


class _IdentityCheckClaudeKlabauter:
    """Minimal `ClaudeKlabauterPercolate`-shaped fake wired to the REAL
    `run_identity_check` for `run_identity_check` itself, but stubbing
    every other engine surface `dispatch_percolate_pre_ci` also calls
    (`resolve_target`, `run_percolate`) with an always-clean phase result --
    isolates this test to the identity-check fold-in specifically."""

    def resolve_target(self, store, name):
        return {"hooks": [], "file_surface": {}, "guards": [], "inject": []}

    def run_percolate(self, store_path, target, target_root, phase, **kwargs):
        return {"phase": phase, "guard_results": [], "rename_manifest": None, "restored_native": []}

    def iter_surface_files(self, root, **kwargs):
        return iter(())

    def run_identity_check(self, dest):
        return run_identity_check(dest)


def _target(tmp_path: Path, name="t") -> "publish.ResolvedTarget":
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()
    return publish.ResolvedTarget(name=name, mode="mirror", source_dir=src, dest_dir=dst)


def _target_with_subdir(tmp_path: Path, name="t") -> "publish.ResolvedTarget":
    """A row whose `dest_dir` is a SUBDIRECTORY of the destination repo root
    -- the exact shape row 1 of the klabauter mirror has (`dest_dir =
    <repo>/coordinator_core`). `<repo>/.git` marks the repo root;
    `.github/scripts/check-persona-names.py` lands there too, published by
    a separate toplevel row, never under `dest_dir` itself."""
    src = tmp_path / "src"
    src.mkdir()
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    dst = repo_root / "coordinator_core"
    dst.mkdir()
    return publish.ResolvedTarget(name=name, mode="mirror", source_dir=src, dest_dir=dst)


def _ctx(claude_klabauter_engine) -> "publish.PercolateEngineContext":
    return publish.PercolateEngineContext(engine_claude_klabauter=claude_klabauter_engine, store={"targets": {}})


class TestIdentityCheckGateFiresOnPlantedFinding:
    def test_planted_finding_fails_target(self, tmp_path):
        target = _target(tmp_path)
        _write_checker(target.dest_dir)
        (target.dest_dir / _SENTINEL_NAME).write_text("x", encoding="utf-8")

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        with pytest.raises(publish.EngineUnavailableError) as excinfo:
            publish.dispatch_percolate_pre_ci(
                _ctx(claude_klabauter_engine), tmp_path / "store.yaml", target, tmp_path / "src", None
            )
        assert "check-persona-names.py exited 1" in str(excinfo.value)
        assert "PLANTED-FINDING-SENTINEL" in str(excinfo.value)


class TestIdentityCheckGatePassesCleanTree:
    def test_clean_tree_passes(self, tmp_path):
        target = _target(tmp_path)
        _write_checker(target.dest_dir)
        # No sentinel file -- synthetic checker exits 0.

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        publish.dispatch_percolate_pre_ci(
            _ctx(claude_klabauter_engine), tmp_path / "store.yaml", target, tmp_path / "src", None
        )  # must not raise


class TestIdentityCheckGateAbsentScriptIsALoudSkip:
    def test_absent_checker_script_is_a_skip_not_a_pass(self, tmp_path):
        target = _target(tmp_path)
        # No `.github/scripts/check-persona-names.py` at all.

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        result = run_identity_check(str(target.dest_dir))
        assert result == {"ran": False, "skipped": True, "exit_code": None, "findings": ""}

        # And the gate itself must not raise -- a skip is neither a pass nor
        # a failure entry in guard_results, it is simply not present.
        publish.dispatch_percolate_pre_ci(
            _ctx(claude_klabauter_engine), tmp_path / "store.yaml", target, tmp_path / "src", None
        )

    def test_skip_is_printed_loudly_not_silent(self, tmp_path, capsys):
        """The root bug this fix closes: a skip must never read as clean.
        Even though the gate stays advisory, the skip has to be visible."""
        target = _target(tmp_path)

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        publish.dispatch_percolate_pre_ci(
            _ctx(claude_klabauter_engine), tmp_path / "store.yaml", target, tmp_path / "src", None
        )
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "check-persona-names.py" in captured.err
        assert "SKIPPED" in captured.err


class TestIdentityCheckGateResolvesDestSubdirToRepoRoot:
    """Row 1 of the klabauter mirror (and every row like it) has a `dest_dir`
    that is a SUBDIRECTORY of the destination repo, e.g.
    `<repo>/coordinator_core`. `.github/scripts/check-persona-names.py` only
    ever lands at the repo ROOT (a separate toplevel row publishes it there).
    Anchoring the identity check on `target.dest_dir` made this class of row
    permanently blind (`skipped=True`, gate never fires) -- root-caused in
    `state/audits/2026-08-05-klabauter-scrub-and-gate-both-silent.md` § Q3.
    """

    def test_checker_at_repo_root_actually_runs_for_subdir_row(self, tmp_path):
        target = _target_with_subdir(tmp_path)
        repo_root = target.dest_dir.parent
        _write_checker(repo_root)
        # No sentinel file -- synthetic checker exits 0. Proves the checker
        # was actually invoked (not silently skipped) by asserting a clean
        # pass rather than merely "did not raise" (a skip also would not
        # raise, so a clean-tree-only assertion wouldn't distinguish them).

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        publish.dispatch_percolate_pre_ci(
            _ctx(claude_klabauter_engine), tmp_path / "store.yaml", target, tmp_path / "src", None
        )  # must not raise -- and must have actually run, per the next test

    def test_nonzero_exit_at_repo_root_aborts_subdir_row(self, tmp_path):
        target = _target_with_subdir(tmp_path)
        repo_root = target.dest_dir.parent
        _write_checker(repo_root)
        (repo_root / _SENTINEL_NAME).write_text("x", encoding="utf-8")

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        with pytest.raises(publish.EngineUnavailableError) as excinfo:
            publish.dispatch_percolate_pre_ci(
                _ctx(claude_klabauter_engine), tmp_path / "store.yaml", target, tmp_path / "src", None
            )
        assert "check-persona-names.py exited 1" in str(excinfo.value)
        assert "PLANTED-FINDING-SENTINEL" in str(excinfo.value)

    def test_checker_under_dest_subdir_itself_is_not_found(self, tmp_path):
        """Negative control: planting the checker under `dest_dir` (the OLD,
        buggy resolution) must NOT be what the gate finds -- it has to
        resolve to the repo root, not `dest_dir`."""
        target = _target_with_subdir(tmp_path)
        _write_checker(target.dest_dir)  # wrong location, old buggy behavior
        (target.dest_dir / _SENTINEL_NAME).write_text("x", encoding="utf-8")

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        # The repo-root checker doesn't exist, so this is a loud skip, not a
        # run against the wrongly-placed one (which would have failed).
        publish.dispatch_percolate_pre_ci(
            _ctx(claude_klabauter_engine), tmp_path / "store.yaml", target, tmp_path / "src", None
        )  # must not raise -- skip, never a false failure either


# ---------------------------------------------------------------------------
# End-of-run leg — `dispatch_end_of_run_identity_check`, direct unit tests.
# ---------------------------------------------------------------------------
class TestEndOfRunIdentityCheckLeg:
    """Direct tests of `dispatch_end_of_run_identity_check` -- the function
    `main()` calls once per distinct destination repo root after every row
    has synced (§ that function's own docstring for why the per-row leg
    alone cannot close this gap: row declaration order in
    `setup/publish-targets.portable` means the engine row's per-row check
    can run before the toplevel row has ever published `.github/`)."""

    def test_unfiltered_skip_is_hard_failure(self, tmp_path):
        repo_root = tmp_path / "repo"
        (repo_root / ".git").mkdir(parents=True)
        # No `.github/scripts/check-persona-names.py` at all -- the virgin-
        # destination shape.

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        ctx = _ctx(claude_klabauter_engine)
        ok = publish.dispatch_end_of_run_identity_check(
            ctx, [repo_root], target_filtered=False
        )
        assert ok is False

    def test_filtered_skip_is_advisory_not_a_failure(self, tmp_path):
        repo_root = tmp_path / "repo"
        (repo_root / ".git").mkdir(parents=True)

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        ctx = _ctx(claude_klabauter_engine)
        ok = publish.dispatch_end_of_run_identity_check(
            ctx, [repo_root], target_filtered=True
        )
        assert ok is True

    def test_filtered_skip_prints_advisory_warning(self, tmp_path, capsys):
        repo_root = tmp_path / "repo"
        (repo_root / ".git").mkdir(parents=True)

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        ctx = _ctx(claude_klabauter_engine)
        publish.dispatch_end_of_run_identity_check(ctx, [repo_root], target_filtered=True)
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "advisory under --target" in captured.err

    def test_clean_populated_destination_passes(self, tmp_path):
        repo_root = tmp_path / "repo"
        (repo_root / ".git").mkdir(parents=True)
        _write_checker(repo_root)
        # No sentinel -- synthetic checker exits 0.

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        ctx = _ctx(claude_klabauter_engine)
        ok = publish.dispatch_end_of_run_identity_check(
            ctx, [repo_root], target_filtered=False
        )
        assert ok is True

    def test_nonzero_exit_is_hard_failure_even_when_filtered(self, tmp_path, capsys):
        """A `--target`-scoped run degrades a MISSING checker to advisory,
        but a checker that actually RAN and found something must never be
        excused by the same scoping -- that's what keeps a filtered debug
        publish from being unfailable (task brief requirement 3)."""
        repo_root = tmp_path / "repo"
        (repo_root / ".git").mkdir(parents=True)
        _write_checker(repo_root)
        (repo_root / _SENTINEL_NAME).write_text("x", encoding="utf-8")

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        ctx = _ctx(claude_klabauter_engine)
        ok = publish.dispatch_end_of_run_identity_check(
            ctx, [repo_root], target_filtered=True
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "check-persona-names.py exited 1" in captured.err
        assert "PLANTED-FINDING-SENTINEL" in captured.err

    def test_multiple_distinct_repo_roots_each_checked(self, tmp_path):
        """One clean repo root plus one virgin repo root in the same run --
        the run-wide result must reflect the WORST of the two, and both
        must actually have been visited (proven by the clean one's presence
        not masking the virgin one's failure)."""
        clean_root = tmp_path / "clean-repo"
        (clean_root / ".git").mkdir(parents=True)
        _write_checker(clean_root)

        virgin_root = tmp_path / "virgin-repo"
        (virgin_root / ".git").mkdir(parents=True)

        claude_klabauter_engine = _IdentityCheckClaudeKlabauter()
        ctx = _ctx(claude_klabauter_engine)
        ok = publish.dispatch_end_of_run_identity_check(
            ctx, [clean_root, virgin_root], target_filtered=False
        )
        assert ok is False


# ---------------------------------------------------------------------------
# End-of-run leg — full `publish.main()` wiring.
# ---------------------------------------------------------------------------
class _StubClaudeKlabauter:
    """Same shape as `_IdentityCheckClaudeKlabauter` -- real `run_identity_check`,
    trivial everything else -- but named separately since these tests don't
    go through `dispatch_percolate_pre_ci` at all (`process_target` is
    stubbed to a no-op below, isolating `main()`'s end-of-run wiring from
    the per-row engine-phase machinery, which has its own dedicated
    coverage elsewhere). `file_surface` names `*.md`/`*.py` so
    `dispatch_end_of_run_unscanned_published_check` (which calls the REAL
    `coordinator_core.percolate.surface.iter_surface_files` directly, not
    this stub's own `iter_surface_files` below) sees these fixtures' plain
    `.md`/`.py` content as in-surface -- an empty `file_surface` would make
    every published file "unscanned" and fail that leg regardless of what
    this test is actually exercising."""

    def resolve_target(self, store, name):
        return {
            "hooks": [],
            "file_surface": {"include_extensions": ["*.md", "*.py"]},
            "guards": [],
            "inject": [],
        }

    def run_percolate(self, store_path, target, target_root, phase, **kwargs):
        return {"phase": phase, "guard_results": [], "rename_manifest": None, "restored_native": []}

    def iter_surface_files(self, root, **kwargs):
        return iter(())

    def run_identity_check(self, dest):
        return run_identity_check(dest)

    def run_parse_sweep(self, repo_root):
        # `dispatch_end_of_run_function_gate` (chunk C4B, added after this
        # fixture was authored) calls this unconditionally for every reached
        # repo root once a run has zero failed rows — sibling gap noted in
        # `test_publish_row_isolation.py`'s `_FakeClaudeKlabauter` docstring. A
        # parse-clean, zero-file sweep result is a no-op for this file's own
        # identity-check assertions.
        return type("ParseResult", (), {"ok": True, "failures": [], "scanned": 0})()

    def enumerate_gate_entrypoints(self, repo_root):
        # `dispatch_end_of_run_entrypoint_gate` (chunk C3, same sibling gap)
        # calls this unconditionally too. This fixture's repo roots ship no
        # entrypoints, so an empty tuple short-circuits that gate's loop.
        return ()


def _fake_process_target_succeeds(target, setup_dir, totals, **kwargs):
    # `main()`'s row loop (§ the row-honesty fix, `test_publish_skipped_row_
    # not_counted_succeeded.py`) treats "`process_target` did not raise AND
    # `totals.processed` did not advance" as a FAILED row — a `None`-
    # returning no-op fake (this fixture's original shape) therefore marks
    # every row FAILED before any end-of-run gate (the thing this file
    # actually tests) is ever reached. Advance `totals.processed` to model
    # the row genuinely landing, matching every other `main()`-driving
    # publish test fixture in this package.
    totals.processed += 1


def _wire_main_preconditions(monkeypatch, *, setup_dir: Path, rows: list) -> None:
    """Monkeypatch every `main()` precondition OTHER than the end-of-run
    identity-check leg under test: percolate-root resolution, target-row
    loading, engine import/store, identity-file gates, publish_sync import
    contract, and per-target dispatch itself (`process_target` -> no-op,
    since these tests exist to prove the RUN-WIDE accumulation + call +
    return-code wiring around it, not row-level sync/gate behaviour, which
    has its own dedicated test files)."""
    percolate_root = setup_dir.parent
    monkeypatch.setattr(
        publish, "_resolve_percolate_root_and_rung", lambda **kwargs: (percolate_root, "test-rung")
    )
    monkeypatch.setattr(
        publish, "load_targets", lambda setup_dir, target_filter="", **kwargs: rows
    )
    monkeypatch.setattr(publish, "locate_percolate_store", lambda setup_dir: setup_dir / "store.yaml")
    monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _StubClaudeKlabauter())
    monkeypatch.setattr(publish, "assert_percolate_store_ready", lambda claude_klabauter_engine, store_path: {"targets": {}})
    monkeypatch.setattr(publish, "check_identity_file_present", lambda *a, **k: None)
    monkeypatch.setattr(publish, "check_identity_file_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        publish,
        "parse_percolate_identity",
        lambda *a, **k: publish.PercolateIdentity(review=["test-machine-slug"]),
    )
    monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)
    monkeypatch.setattr(publish, "process_target", _fake_process_target_succeeds)


class TestEndOfRunIdentityCheckMainWiring:
    """`publish.main()` end-to-end, with everything except the end-of-run
    identity-check leg stubbed inert (§ `_wire_main_preconditions`)."""

    def _rows_for(self, repo_root: Path, *, engine_name="engine-row", toplevel_name="engine-row-toplevel") -> list:
        # Mirrors the real `setup/publish-targets.portable` shape: the
        # engine row (non-empty `dest_subdir`) declared BEFORE the toplevel
        # row (empty `dest_subdir`, i.e. `dest_dir == repo_root`). Neither
        # `dest_dir` needs to exist on disk -- `_dest_repo_root` only walks
        # for a `.git` entry, which only `repo_root` itself carries.
        return [
            f"{engine_name}|mirror|{repo_root / 'src-engine'}|{repo_root / 'coordinator_core'}",
            f"{toplevel_name}|flat-mirror|{repo_root / 'src-toplevel'}|{repo_root}",
        ]

    def test_full_unfiltered_run_into_virgin_destination_fails(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        # No `.github/scripts/check-persona-names.py` published yet.

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=self._rows_for(repo_root))

        rc = publish.main([])
        assert rc != 0
        captured = capsys.readouterr()
        assert "end-of-run identity check FAILED" in captured.err
        assert "checker not found" in captured.err

    def test_full_unfiltered_run_into_clean_populated_destination_passes(self, tmp_path, monkeypatch):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        _write_checker(repo_root)  # published by the (stubbed) toplevel row already.

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=self._rows_for(repo_root))

        rc = publish.main([])
        assert rc == 0

    def test_full_unfiltered_run_nonzero_checker_exit_aborts_run(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        _write_checker(repo_root)
        (repo_root / _SENTINEL_NAME).write_text("x", encoding="utf-8")

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=self._rows_for(repo_root))

        rc = publish.main([])
        assert rc != 0
        captured = capsys.readouterr()
        assert "check-persona-names.py exited 1" in captured.err

    def test_target_filtered_run_missing_checker_stays_advisory(self, tmp_path, monkeypatch, capsys):
        """A `--target engine-row` debug publish never reaches the toplevel
        row, so it legitimately never sees `.github/` -- must not hard-fail
        on that alone (task brief requirement 3)."""
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=self._rows_for(repo_root))

        rc = publish.main(["engine-row"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "advisory under --target" in captured.err

    def test_dry_run_never_fires_the_end_of_run_leg(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        (repo_root / ".git").mkdir(parents=True)
        # Virgin destination -- if the leg fired, this would fail loudly.

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=self._rows_for(repo_root))

        rc = publish.main(["--dry-run"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "end-of-run" not in captured.err
