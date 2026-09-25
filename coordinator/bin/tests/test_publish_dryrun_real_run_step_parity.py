"""coordinator/bin/tests/test_publish_dryrun_real_run_step_parity.py — P079-C2
regression guard (`docs/plans/2026-09-11-publish-build-verify-swap-one-staging-pa.md`
chunk C2).

With C1 landed, every percolate-engine phase `process_target` dispatches runs
in both `--dry-run` and a real run against the staging copy (§ C1) — the ONE
exception is `_swap_publish_staging_into_dest`, withheld under dry-run and
declared as the sole real-run-only `process_target`-owned phase via
`publish.PROCESS_TARGET_REAL_RUN_ONLY_PHASES`.

This file drives the REAL `process_target` (never a stub of the fork itself)
twice over the same fixture row — once with `dry_run=True`, once with
`dry_run=False` — threading a `timing_sink` through both runs to collect the
exact `_time_phase` labels `process_target` dispatches in each mode, and
asserts the two label sets differ by EXACTLY
`publish.PROCESS_TARGET_REAL_RUN_ONLY_PHASES` — not merely that the swap
label is present/absent, but that no OTHER phase silently gained or lost
dry-run reachability as a side effect of this refactor.

Percolate-engine dispatch functions are monkeypatched to lightweight fakes
(mirrors `test_publish_dryrun_builds_staging.py`'s own pattern) so the run
reaches every phase without needing a real percolate engine wired up.

Run: python -m pytest coordinator/bin/tests/test_publish_dryrun_real_run_step_parity.py -x -q
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_dryrun_real_run_step_parity_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(args: "list[str]", cwd: Path) -> None:
    # popup-safe-env-suppressed
    subprocess.run(
        [
            "git",
            "-c", "user.email=publish-step-parity-test@example.invalid",
            "-c", "user.name=publish-step-parity-test",
            "-c", "commit.gpgsign=false",
            "-c", "init.defaultBranch=main",
            *args,
        ],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    (path / "widget.py").write_text("original payload\n", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "seed"], path)
    head = publish._git_head(path)
    assert head, f"fixture setup failed: {path} has no resolvable HEAD"
    return head


def _wire_common_fakes(monkeypatch, *, src_dir: Path):
    monkeypatch.setattr(
        publish,
        "run_pre_sync_gates",
        lambda *a, **k: publish.GateResult(proceed=True, source_dir=src_dir),
    )
    monkeypatch.setattr(publish, "dispatch_percolate_pre_rsync", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_standalone_guards", lambda *a, **k: None)
    monkeypatch.setattr(publish, "sync_manifest", lambda src, dst, totals, dry_run, out: True)
    monkeypatch.setattr(publish, "write_lastsync_marker", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_preswap_function_gate", lambda *a, **k: True)
    monkeypatch.setattr(
        publish,
        "dispatch_preswap_payload_parity_gate",
        lambda *a, **k: True,
    )

    def fake_post_rsync(
        engine_ctx,
        store_path,
        sync_target,
        effective_source_dir,
        *,
        visited_sink=None,
        sync_changed_paths=None,
    ):
        staged_path = sync_target.dest_dir / "widget.py"
        staged_path.write_bytes(b"published payload\n")
        if visited_sink is not None:
            visited_sink.add(staged_path)
        return None, None

    monkeypatch.setattr(publish, "dispatch_percolate_post_rsync", fake_post_rsync)
    monkeypatch.setattr(publish, "dispatch_percolate_inject", lambda *a, **k: ())
    monkeypatch.setattr(publish, "dispatch_percolate_pre_ci", lambda *a, **k: None)
    monkeypatch.setattr(
        publish,
        "_swap_publish_staging_into_dest",
        lambda *a, **k: None,
    )


def _run_process_target(tmp_path, monkeypatch, *, row_name: str, dry_run: bool) -> "list[tuple]":
    src_dir = tmp_path / f"{row_name}-source"
    src_dir.mkdir()
    dest_dir = tmp_path / f"{row_name}-dest"
    _init_git_repo(dest_dir)

    target = publish.ResolvedTarget(
        name=row_name,
        mode="manifest",
        source_dir=src_dir,
        dest_dir=dest_dir,
    )

    _wire_common_fakes(monkeypatch, src_dir=src_dir)

    totals = publish.RunTotals()
    out = io.StringIO()
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=object(), store={})
    timing_sink: "list[tuple]" = []

    publish.process_target(
        target,
        tmp_path,
        totals,
        identity_file_exists=True,
        identity=None,
        dry_run=dry_run,
        engine_ctx=engine_ctx,
        percolate_store_path=tmp_path / f"{row_name}-store.yaml",
        out=out,
        timing_sink=timing_sink,
    )
    return timing_sink


def test_dryrun_and_real_run_phase_sets_differ_by_exactly_the_swap(tmp_path, monkeypatch):
    dry_run_timings = _run_process_target(
        tmp_path, monkeypatch, row_name="dry-row", dry_run=True
    )
    real_run_timings = _run_process_target(
        tmp_path, monkeypatch, row_name="real-row", dry_run=False
    )

    dry_run_phases = {phase for (_row, phase, _wall, _cpu) in dry_run_timings}
    real_run_phases = {phase for (_row, phase, _wall, _cpu) in real_run_timings}

    # Every phase reached under dry-run is also reached under a real run —
    # C1's whole point (staging-tree parity between the two modes).
    assert dry_run_phases.issubset(real_run_phases)

    # The ONLY phase a real run dispatches that dry-run withholds is exactly
    # the declared real-run-only set — not a superset (some OTHER phase
    # silently gained real-run-only status) and not a subset (the swap
    # itself leaked into dry-run).
    assert real_run_phases - dry_run_phases == publish.PROCESS_TARGET_REAL_RUN_ONLY_PHASES

    # Sanity: the declared set is reachable at all under a real run (a typo
    # in the constant that named a phase label never dispatched would
    # otherwise pass the subtraction above vacuously if both sides changed
    # together).
    assert publish.PROCESS_TARGET_REAL_RUN_ONLY_PHASES <= real_run_phases
    assert "_swap_publish_staging_into_dest" in real_run_phases
    assert "_swap_publish_staging_into_dest" not in dry_run_phases
