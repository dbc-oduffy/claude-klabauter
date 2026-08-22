"""coordinator/bin/tests/test_publish_staging_sweep_on_refused_row.py — C3
regression guard (`state/dispatch-briefs/2026-08-21-the-payload-proves-itself-
before-it-overwrites-the-engine/C3.md`).

Before this fix, `_sweep_stale_publish_staging_dirs(target.dest_dir, ...)`
sat inside `process_target` one line before `_create_publish_staging_dir`,
behind several early returns (source absent, pre-sync gate declined, dest
not on declared ref, engine root not viable, plus two inside the `try`). A
row that hit ANY of those returns before reaching the sweep left a prior
run's orphaned `.{dest_dir.name}.publish-staging-*` directory sitting in
`dest_dir.parent` — cleanup for a failed row depended on a LATER row
succeeding.

This file drives the REAL `process_target` (never a stub of the sweep
itself) through two distinct refusal shapes and asserts the orphan is gone
either way:

  * the source-absent early return (the very first gate, reached before
    `run_pre_sync_gates` is even called), and
  * the pre-sync-gate-declined early return — the LIVE driver of this
    defect per the dispatch brief (`--delta` is recorded dead on mirror
    rows and is deliberately not used to drive this test).

Out of remit, per the brief: `.fleet-env.prior` / `.fleet-env.gen-*` match
`_FLEET_ENV_STAGING_SKIP_RE` and are fleet-env provisioner artifacts, not
publish orphans — ownership belongs to that surface, not asserted here.

Run: python -m pytest coordinator/bin/tests/test_publish_staging_sweep_on_refused_row.py -x -q
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.cadence

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_staging_sweep_on_refused_row_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _seed_stale_orphan(dest_dir: Path, *, age_seconds: float = 7200.0) -> Path:
    """Mints an orphan staging dir in the exact `_create_publish_staging_dir`
    shape (RE-ENUMERATED here, not assumed — the inventory moves, per the
    brief), then backdates its mtime past `_sweep_stale_publish_staging_dirs`'
    one-hour `max_age_seconds` default so it reads as orphaned rather than a
    live concurrent row."""
    orphan = publish._create_publish_staging_dir(dest_dir)
    stale_time = time.time() - age_seconds
    os.utime(orphan, (stale_time, stale_time))
    return orphan


def _base_target(tmp_path: Path, *, source_exists: bool) -> "publish.ResolvedTarget":
    src_dir = tmp_path / "source"
    if source_exists:
        src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    return publish.ResolvedTarget(
        name="c3-row",
        mode="manifest",
        source_dir=src_dir,
        dest_dir=dest_dir,
    )


def test_source_absent_refusal_still_sweeps_prior_run_orphan(tmp_path):
    target = _base_target(tmp_path, source_exists=False)
    orphan = _seed_stale_orphan(target.dest_dir)
    assert orphan.exists()

    totals = publish.RunTotals()
    out = io.StringIO()
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=object(), store={})

    publish.process_target(
        target,
        tmp_path,
        totals,
        identity_file_exists=True,
        identity=None,
        dry_run=False,
        engine_ctx=engine_ctx,
        percolate_store_path=tmp_path / "store.yaml",
        out=out,
    )

    assert not orphan.exists()
    remaining = list(target.dest_dir.parent.glob(f".{target.dest_dir.name}.publish-staging-*"))
    assert remaining == []


def test_dry_run_reports_but_does_not_remove_stale_orphan(tmp_path):
    # Finding 1, s3-sweep-and-dirty review: the C3 move made the sweep
    # unconditional with respect to ROW DISPOSITION (this file's whole
    # point), but a prior version of that move also escaped the `dry_run`
    # gate every other write in publish.py obeys — under --dry-run the
    # orphan must survive, and the would-sweep line must still be reported.
    target = _base_target(tmp_path, source_exists=False)
    orphan = _seed_stale_orphan(target.dest_dir)
    assert orphan.exists()

    totals = publish.RunTotals()
    out = io.StringIO()
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=object(), store={})

    publish.process_target(
        target,
        tmp_path,
        totals,
        identity_file_exists=True,
        identity=None,
        dry_run=True,
        engine_ctx=engine_ctx,
        percolate_store_path=tmp_path / "store.yaml",
        out=out,
    )

    assert orphan.exists()
    remaining = list(target.dest_dir.parent.glob(f".{target.dest_dir.name}.publish-staging-*"))
    assert remaining == [orphan]
    assert "would sweep" in out.getvalue()
    assert str(orphan) in out.getvalue()


def test_gate_declined_refusal_still_sweeps_prior_run_orphan(tmp_path, monkeypatch):
    # Per the brief: the pre-sync-gate-declined path is the LIVE driver of
    # this defect — --delta is unconditionally dead on mirror rows and is
    # deliberately not used here.
    target = _base_target(tmp_path, source_exists=True)
    orphan = _seed_stale_orphan(target.dest_dir)
    assert orphan.exists()

    monkeypatch.setattr(
        publish,
        "run_pre_sync_gates",
        lambda *a, **k: publish.GateResult(proceed=False, source_dir=target.source_dir),
    )

    totals = publish.RunTotals()
    out = io.StringIO()
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=object(), store={})

    publish.process_target(
        target,
        tmp_path,
        totals,
        identity_file_exists=True,
        identity=None,
        dry_run=False,
        engine_ctx=engine_ctx,
        percolate_store_path=tmp_path / "store.yaml",
        out=out,
    )

    assert not orphan.exists()
    remaining = list(target.dest_dir.parent.glob(f".{target.dest_dir.name}.publish-staging-*"))
    assert remaining == []
