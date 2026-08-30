"""
coordinator_core.ops.tests.test_distill_curation_status

Tests for the "distill.curation_status" op (C11; DEC-1).

Import guard: coordinator_core.ops.distill_curation_status MUST be imported at
module load time so @register_op("distill.curation_status") fires and populates
_REGISTRY.

Coverage:
  (a) registry assertion — op name present in _REGISTRY after import
  (b) repo_root=None raises ValueError (no silent fallback to meta-repo, AC5-style)
  (c) bare compute call (emit absent/false) returns a schema-valid artifact and does
      NOT write state/ceremony/curation-status.json
  (d) emit=True writes the schema_version-pinned artifact atomically to
      state/ceremony/curation-status.json and reply["emitted"] is True
  (e) age math frozen-clock: last_run_id/last_run_age_seconds are derived from the
      canonical distillation log (state/distillation-log.md) — its last parsed row's
      run_id (which IS the timestamp — no mtime seam) against a
      monkeypatched "now" — never from this op's own prior emission
      (2026-07-23 the Staff Engineer post-execution review § 3: the previous "derived" field
      group was actually read back from curation-status.json's own prior emission,
      a self-reference that let "derived, never stored" quietly collapse into
      "stored")
  (f) no-prior-run case — an absent/unparseable distillation log degrades to
      last_run_id=None, last_run_age_seconds=None, never an error
  (g) the log wins — a stale/malformed PRIOR EMISSION of curation-status.json must
      have zero influence on last_run_id/last_run_age_seconds; only the canonical
      log is consulted

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C11
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires @register_op("distill.curation_status") as a side-effect.
# ---------------------------------------------------------------------------
import coordinator_core.ops.distill_curation_status as dcs  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_OP_NAME = "distill.curation_status"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.distill_curation_status @register_op did not fire"
)


def _seed_repo(tmp_path: Path) -> Path:
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "ripe-plan.md").write_text("---\nstatus: implemented\n---\nbody\n")

    specs_dir = tmp_path / "archive" / "specs" / "2026-07"
    specs_dir.mkdir(parents=True)
    (specs_dir / "harvested-plan.md").write_text("dummy\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "distillation-log.md").write_text(
        "## Run 2026-07-23-11h58\n"
        "- archive/specs/2026-07/harvested-plan.md -> DISTILLED, folded (run: 2026-07-23-11h58)\n"
    )
    return tmp_path


def _seed_git_repo(tmp_path: Path) -> Path:
    """Like _seed_repo, but also `git init`s tmp_path — required for any test
    that exercises the emit=True path, since locked_write.locked_rmw's lock
    sidecar is keyed off git_common_dir(repo_root)."""
    repo_root = _seed_repo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True, **no_console_passthrough_kwargs())
    return repo_root


def test_op_is_registered():
    assert _OP_NAME in _REGISTRY


def test_repo_root_none_raises():
    with pytest.raises(ValueError):
        dcs._distill_curation_status({}, repo_root=None)


def test_bare_compute_does_not_write(tmp_path):
    repo_root = _seed_repo(tmp_path)
    common_dir = repo_root / ".git"
    reply = dcs._distill_curation_status({}, repo_root=common_dir)

    assert reply["emitted"] is False
    assert reply["schema_version"] == 1
    assert not (repo_root / "state" / "ceremony" / "curation-status.json").exists()


def test_default_run_id_branch_mints_wallclock_id_when_omitted(tmp_path):
    # 2026-07-23 code review finding: distill.curation_status is a named
    # exemption from the "run_id is a required param, never datetime.now()"
    # rule its siblings (distill.scope, memo.fate_partition) enforce, because
    # this op's write target is a fixed path (never a run_id-keyed directory)
    # — see the exemption comment in _distill_curation_status. This test
    # covers the previously-untested default branch directly, asserting the
    # minted id's shape rather than merely exercising the code path.
    repo_root = _seed_repo(tmp_path)
    common_dir = repo_root / ".git"
    reply = dcs._distill_curation_status({}, repo_root=common_dir)

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{2}h\d{2}m\d{2}s", reply["run_id"])


def test_explicit_run_id_is_never_overridden_by_wallclock(tmp_path):
    repo_root = _seed_repo(tmp_path)
    common_dir = repo_root / ".git"
    reply = dcs._distill_curation_status({"run_id": "caller-supplied-id"}, repo_root=common_dir)
    

    assert reply["run_id"] == "caller-supplied-id"


def test_emit_writes_schema_valid_artifact(tmp_path):
    repo_root = _seed_git_repo(tmp_path)
    common_dir = repo_root / ".git"
    reply = dcs._distill_curation_status({"emit": True, "run_id": "r-test-1"}, repo_root=common_dir)

    assert reply["emitted"] is True
    out_path = repo_root / "state" / "ceremony" / "curation-status.json"
    assert out_path.is_file()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1
    assert on_disk["run_id"] == "r-test-1"
    assert "archive/specs/2026-07/harvested-plan.md" in on_disk["artifacts"]

    from coordinator_core.distill.manifest_schema import validate_curation_status

    assert validate_curation_status(on_disk) == []


def test_age_math_frozen_clock(tmp_path, monkeypatch):
    # _seed_repo seeds state/distillation-log.md with exactly one row under
    # "## Run 2026-07-23-11h58" — that is the real disk truth this op must derive
    # last_run_id/last_run_age_seconds from, never from its own prior emission.
    # The run-id IS the timestamp; no mtime seam is involved.
    repo_root = _seed_git_repo(tmp_path)
    common_dir = repo_root / ".git"

    t_log = _dt.datetime(2026, 7, 23, 11, 58, 0, tzinfo=_dt.timezone.utc)
    now = t_log + _dt.timedelta(seconds=100)
    monkeypatch.setattr(dcs, "_now_utc", lambda: now)

    reply = dcs._distill_curation_status({"emit": True, "run_id": "r-caller"}, repo_root=common_dir)

    assert reply["last_run_id"] == "2026-07-23-11h58"
    assert reply["last_run_age_seconds"] == pytest.approx(100.0)


def _seed_bare_repo(tmp_path: Path) -> Path:
    """A minimal git repo with docs/plans/ but no archive/specs/ and no
    state/distillation-log.md — exercises the "no distill run has ever landed"
    degrade path without tripping compute_harvest_debt's fail-loud missing-log
    contract (that contract only fires when archive/specs/ itself exists)."""
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


def test_absent_log_degrades_to_no_last_run(tmp_path):
    repo_root = _seed_bare_repo(tmp_path)
    common_dir = repo_root / ".git"

    reply = dcs._distill_curation_status({}, repo_root=common_dir)
    assert reply["last_run_id"] is None
    assert reply["last_run_age_seconds"] is None


def test_log_wins_over_stale_prior_emission(tmp_path, monkeypatch):
    # A stale/fabricated PRIOR EMISSION of curation-status.json must have zero
    # influence on last_run_id/last_run_age_seconds — only the canonical log
    # (state/distillation-log.md) is a legitimate source now. This is the direct
    # regression test for the self-reference this fix retires.
    repo_root = _seed_git_repo(tmp_path)
    common_dir = repo_root / ".git"
    ceremony_dir = repo_root / "state" / "ceremony"
    ceremony_dir.mkdir(parents=True)
    stale_prior = {
        "schema_version": 1,
        "run_id": "stale-emission-id",
        "generated_at": "2000-01-01T00:00:00+00:00",
        "artifacts": {},
        "unharvested_ripe_count": 0,
        "last_run_id": None,
        "last_run_age_seconds": None,
        "prunable": [],
    }
    (ceremony_dir / "curation-status.json").write_text(json.dumps(stale_prior))

    t_log = _dt.datetime(2026, 7, 23, 11, 58, 0, tzinfo=_dt.timezone.utc)
    now = t_log + _dt.timedelta(seconds=42)
    monkeypatch.setattr(dcs, "_now_utc", lambda: now)

    reply = dcs._distill_curation_status({}, repo_root=common_dir)

    assert reply["last_run_id"] == "2026-07-23-11h58"
    assert reply["last_run_id"] != stale_prior["run_id"]
    assert reply["last_run_age_seconds"] == pytest.approx(42.0)


def test_fresh_mtime_does_not_fake_a_recent_run(tmp_path, monkeypatch):
    """THE regression test for this fix: a log whose FILE MTIME is 'now' but whose
    last run-id is old must report the OLD age.

    The first cut derived last_run_at from the log file's mtime. git does not
    preserve mtime, so on a fresh clone (or any checkout/rewrite — log_normalize
    rewrites this file in place) mtime becomes checkout time and the age collapses
    toward zero: "we just distilled", when the last real run may be months old. For
    the cadence trigger M1 points at this field, a falsely-fresh age means the
    ceremony silently never fires. This test fails on the mtime implementation and
    passes on the run-id one.
    """
    repo_root = _seed_git_repo(tmp_path)
    common_dir = repo_root / ".git"

    log_path = repo_root / "state" / "distillation-log.md"
    log_path.write_text(
        "## Run 2026-01-02-03h04\n"
        "- archive/specs/2026-07/harvested-plan.md -> DISTILLED, folded "
        "(run: 2026-01-02-03h04)\n",
        encoding="utf-8",
    )
    # Simulate the fresh-clone / rewrite case: mtime is NOW, content is old.
    now = _dt.datetime(2026, 7, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)
    os.utime(log_path, (now.timestamp(), now.timestamp()))
    monkeypatch.setattr(dcs, "_now_utc", lambda: now)

    reply = dcs._distill_curation_status({}, repo_root=common_dir)

    assert reply["last_run_id"] == "2026-01-02-03h04"
    # ~202 days, derived from the run-id's own bytes — NOT ~0 from the fresh mtime.
    expected = (now - _dt.datetime(2026, 1, 2, 3, 4, tzinfo=_dt.timezone.utc)).total_seconds()
    assert reply["last_run_age_seconds"] == pytest.approx(expected)
    assert reply["last_run_age_seconds"] > 86_400 * 180


def test_unparseable_run_id_reports_known_run_unknown_age(tmp_path):
    """A run-id that will not parse must fail SOFT: the run is known, the age is not.
    A fabricated or zero age would be a confidently wrong freshness reading, which is
    strictly worse than an absent one for a cadence consumer."""
    repo_root = _seed_git_repo(tmp_path)
    common_dir = repo_root / ".git"
    (repo_root / "state" / "distillation-log.md").write_text(
        "## Run legacy-nondate-id\n"
        "- archive/specs/2026-07/harvested-plan.md -> DISTILLED, folded "
        "(run: legacy-nondate-id)\n",
        encoding="utf-8",
    )

    reply = dcs._distill_curation_status({}, repo_root=common_dir)

    assert reply["last_run_id"] == "legacy-nondate-id"
    assert reply["last_run_age_seconds"] is None
