"""
coordinator_core.ops.fleet.tests.test_reap_unintegrated_findings

The assertions `fleet.reap_unintegrated_findings` has never had. Filed as
`state/debt-backlog/2026-08-23-reap-unintegrated-findings-is-registered-untested-and-hostless.yaml`
piece (a) — "cheap, no design needed, no dependency on the memo" — and taken
2026-08-27.

Why it was missing: the op's only coverage was two boot-wiring tests inside
`test_boot_sweep.py`, and deleting that composite's corpus (`bd23bcff6`) took
this op's last assertions with it. The triage that surfaced the gap originally
cited a `tests/test_reap_unintegrated_findings.py` as proof the op was
"independently tested"; that path did not exist and the citation was retracted
at `dfaec1649`. This file is that path, for real.

What is covered, and why each of these and not others — the op's whole risk is
that it DELETES files, so every case here is aimed at the predicate that
decides what gets deleted:

  - the inclusive 14-day boundary, both sides (13d kept, 14d reaped) — the one
    place an off-by-one silently widens a delete
  - every fail-closed-to-keep arm of `classify_unintegrated`: unparseable
    filename, marker present, too young. Each returns None (KEEP), and a
    regression in any of them reaps a file that should have survived
  - the age gate runs BEFORE any file is opened (the module calls this
    ordering load-bearing) — asserted by reading a too-young file that would
    raise if opened
  - `_scan_reapable` over a mixed directory, and over a missing directory,
    which is documented as not-an-error
  - the handler's fail-closed `dry_run` validation: omission and a wrong type
    must NOT fall through to the destructive act path
  - the `dry_run: true` envelope mutates nothing

Negative-spec:
  - Does NOT run a real `git init` or any subprocess. The `dry_run: false` act
    path goes through `_common.rm_and_commit`, whose git mechanics belong
    behind the non-spawning mover seam (`archive_git_free_seam.py`) and are
    that helper's own tests' subject, not this file's. What this file owns is
    the predicate deciding WHICH paths reach it.
  - Does NOT assert the cockpit two-phase mode/candidate_ids envelope — this
    op deliberately does not implement it (DEC-1, module docstring).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import reap_unintegrated_findings as reaper

_MARKER = "## Integrator Dispositions"
_AGE = reaper._AGE_THRESHOLD_DAYS


def _dated(days_ago: int) -> str:
    d = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    return d.isoformat()


def _write(root: Path, days_ago: int, *, integrated: bool = False,
           stem: str = "a-finding") -> Path:
    findings = root / "state" / "review-trail" / "findings"
    findings.mkdir(parents=True, exist_ok=True)
    path = findings / f"{_dated(days_ago)}-{stem}.md"
    body = "# Findings\n\nsomething a reviewer said\n"
    if integrated:
        body += f"\n{_MARKER}\n\napplied\n"
    path.write_text(body, encoding="utf-8")
    return path


def test_boundary_is_inclusive_14d_reaped_13d_kept(tmp_path):
    """The threshold the module documents as inclusive: exactly _AGE days old
    already qualifies. One day younger does not. This pair is the whole
    off-by-one surface."""
    aged = _write(tmp_path, _AGE, stem="aged")
    young = _write(tmp_path, _AGE - 1, stem="young")

    assert reaper.classify_unintegrated(aged) is not None
    assert reaper.classify_unintegrated(young) is None


def test_marker_present_is_kept_however_old(tmp_path):
    """Marker-present sidecars are integrated and belong to DoE's leg (a).
    Age never overrides that — reaping one here would delete the other leg's
    work."""
    integrated = _write(tmp_path, _AGE * 4, integrated=True, stem="integrated")
    assert reaper.classify_unintegrated(integrated) is None


def test_unparseable_filename_fails_closed_to_keep(tmp_path):
    """No extractable authored date means the age is unknown, and unknown age
    must never reap. Fail-closed-to-keep, never fail-open-to-delete."""
    findings = tmp_path / "state" / "review-trail" / "findings"
    findings.mkdir(parents=True, exist_ok=True)
    undated = findings / "no-date-in-this-name.md"
    undated.write_text("# Findings\n", encoding="utf-8")

    assert reaper._extract_authored_date(undated.name) is None
    assert reaper.classify_unintegrated(undated) is None


def test_age_gate_runs_before_the_file_is_opened(tmp_path, monkeypatch):
    """The module calls age-gate-first ordering load-bearing: a sweep over
    hundreds of sidecars must read content for only the aged minority. Asserted
    by making any read of a too-young file explode."""
    young = _write(tmp_path, 1, stem="young")

    def _explode(*_args, **_kwargs):
        raise AssertionError("classify_unintegrated opened a file the age gate rejected")

    monkeypatch.setattr(Path, "read_text", _explode)
    assert reaper.classify_unintegrated(young) is None


def test_scan_reapable_selects_only_the_aged_unintegrated(tmp_path):
    aged = _write(tmp_path, _AGE + 5, stem="aged")
    _write(tmp_path, 1, stem="young")
    _write(tmp_path, _AGE + 5, integrated=True, stem="integrated")

    found = {path for path, _note in reaper._scan_reapable(tmp_path)}
    assert found == {aged}


def test_missing_findings_directory_is_not_an_error(tmp_path):
    """Documented explicitly in the handler: a repo with no
    state/review-trail/findings/ tree returns clean, never raises."""
    assert reaper._scan_reapable(tmp_path) == []


@pytest.mark.parametrize("params", [{}, {"dry_run": "true"}, {"dry_run": 1}])
def test_dry_run_must_be_an_explicit_bool(tmp_path, params):
    """Fail-closed validation: omission or a wrong type must NOT silently
    default to False, which is the destructive git-rm path."""
    result = asyncio.run(reaper._handler(params, repo_root=tmp_path / ".git"))
    assert result["exit_code"] == 1
    assert result["reaped"] == [] and result["failed"] == []


def test_missing_repo_root_is_a_setup_error_not_a_reap(tmp_path):
    result = asyncio.run(reaper._handler({"dry_run": False}, repo_root=None))
    assert result["exit_code"] == 1
    assert result["reaped"] == []


def test_dry_run_true_lists_candidates_and_mutates_nothing(tmp_path, monkeypatch):
    aged = _write(tmp_path, _AGE + 5, stem="aged")
    _write(tmp_path, 1, stem="young")
    monkeypatch.setattr(reaper, "main_worktree_root", lambda _common: tmp_path)
    monkeypatch.setattr(reaper, "check_repo_root", lambda _param, _common: None)

    result = asyncio.run(reaper._handler({"dry_run": True}, repo_root=tmp_path / ".git"))

    assert result["exit_code"] == 0 and result["dry_run"] is True
    assert [c["id"] for c in result["candidates"]] == [
        aged.relative_to(tmp_path).as_posix()
    ]
    assert result["reaped"] == [] and result["failed"] == []
    assert aged.exists(), "dry_run:true deleted a file"
