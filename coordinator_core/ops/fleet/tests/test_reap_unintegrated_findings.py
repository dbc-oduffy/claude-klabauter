
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import _findings_reap
from coordinator_core.ops.fleet import reap_unintegrated_findings as reaper

_MARKER = "## Integrator Dispositions"
_AGE = reaper._AGE_THRESHOLD_DAYS
_RT_CAP = reaper.REVIEW_TRAIL_RETENTION_DATE_CAP_DAYS


def _dated(days_ago: int) -> str:
    d = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    return d.isoformat()


def _write(root: Path, days_ago: int, *, integrated: bool = False,
           stem: str = "a-finding") -> Path:
    findings = root / ".coordinator-local" / "review-trail" / "findings"
    findings.mkdir(parents=True, exist_ok=True)
    path = findings / f"{_dated(days_ago)}-{stem}.md"
    body = "# Findings\n\nsomething a reviewer said\n"
    if integrated:
        body += f"\n{_MARKER}\n\napplied\n"
    path.write_text(body, encoding="utf-8")
    return path


def test_boundary_is_inclusive_14d_reaped_13d_kept(tmp_path):
    aged = _write(tmp_path, _AGE, stem="aged")
    young = _write(tmp_path, _AGE - 1, stem="young")

    assert reaper.classify_unintegrated(aged) is not None
    assert reaper.classify_unintegrated(young) is None


def test_marker_present_is_kept_however_old(tmp_path):
    integrated = _write(tmp_path, _AGE * 4, integrated=True, stem="integrated")
    assert reaper.classify_unintegrated(integrated) is None


def test_unparseable_filename_fails_closed_to_keep(tmp_path):
    findings = tmp_path / "state" / "review-trail" / "findings"
    findings.mkdir(parents=True, exist_ok=True)
    undated = findings / "no-date-in-this-name.md"
    undated.write_text("# Findings\n", encoding="utf-8")

    assert reaper._extract_authored_date(undated.name) is None
    assert reaper.classify_unintegrated(undated) is None


def test_age_gate_runs_before_the_file_is_opened(tmp_path, monkeypatch):
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
    assert reaper._scan_reapable(tmp_path) == []


@pytest.mark.parametrize("params", [{}, {"dry_run": "true"}, {"dry_run": 1}])
def test_dry_run_must_be_an_explicit_bool(tmp_path, params):
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


def _write_review_trail(root: Path, days_ago: int, *, stem: str = "record") -> Path:
    rt = root / ".coordinator-local" / "review-trail"
    rt.mkdir(parents=True, exist_ok=True)
    path = rt / f"{_dated(days_ago)}-{stem}.json"
    path.write_text("{}", encoding="utf-8")
    return path


def test_review_trail_rest_reaps_aged_uncited_file(tmp_path):
    aged = _write_review_trail(tmp_path, _RT_CAP + 5, stem="aged")
    classify = reaper._make_classify_review_trail_rest(tmp_path)
    assert classify(aged) is not None


def test_review_trail_rest_keeps_too_young_file(tmp_path):
    young = _write_review_trail(tmp_path, _RT_CAP - 1, stem="young")
    classify = reaper._make_classify_review_trail_rest(tmp_path)
    assert classify(young) is None


def test_review_trail_rest_keeps_cited_file_regardless_of_age(tmp_path):
    aged = _write_review_trail(tmp_path, _RT_CAP + 100, stem="cited")
    rel = aged.relative_to(tmp_path / ".coordinator-local" / "review-trail").as_posix()
    citer = tmp_path / "docs" / "citer.md"
    citer.parent.mkdir(parents=True, exist_ok=True)
    citer.write_text(f"see .coordinator-local/review-trail/{rel}\n", encoding="utf-8")

    classify = reaper._make_classify_review_trail_rest(tmp_path)
    assert classify(aged) is None


def test_review_trail_rest_unparseable_filename_fails_closed_to_keep(tmp_path):
    rt = tmp_path / ".coordinator-local" / "review-trail"
    rt.mkdir(parents=True, exist_ok=True)
    undated = rt / "no-date-here.json"
    undated.write_text("{}", encoding="utf-8")

    classify = reaper._make_classify_review_trail_rest(tmp_path)
    assert classify(undated) is None


def test_scan_review_trail_rest_excludes_the_findings_subtree(tmp_path):
    aged = _write_review_trail(tmp_path, _RT_CAP + 5, stem="aged")
    findings_file = (
        tmp_path / ".coordinator-local" / "review-trail" / "findings"
        / f"{_dated(_RT_CAP + 5)}-a-finding.md"
    )
    findings_file.parent.mkdir(parents=True, exist_ok=True)
    findings_file.write_text("# Findings\n", encoding="utf-8")

    classify = reaper._make_classify_review_trail_rest(tmp_path)
    found = {path for path, _note in _findings_reap.scan_review_trail_rest(tmp_path, classify)}
    assert found == {aged}


def test_review_trail_rest_missing_directory_is_not_an_error(tmp_path):
    classify = reaper._make_classify_review_trail_rest(tmp_path)
    assert _findings_reap.scan_review_trail_rest(tmp_path, classify) == []


def test_review_trail_rest_dry_run_lists_candidates_and_mutates_nothing(tmp_path, monkeypatch):
    aged = _write_review_trail(tmp_path, _RT_CAP + 5, stem="aged")
    _write_review_trail(tmp_path, 1, stem="young")
    monkeypatch.setattr(reaper, "main_worktree_root", lambda _common: tmp_path)
    monkeypatch.setattr(reaper, "check_repo_root", lambda _param, _common: None)

    result = asyncio.run(
        reaper._handler_review_trail_rest({"dry_run": True}, repo_root=tmp_path / ".git")
    )

    assert result["exit_code"] == 0 and result["dry_run"] is True
    assert [c["id"] for c in result["candidates"]] == [
        aged.relative_to(tmp_path).as_posix()
    ]
    assert result["reaped"] == [] and result["failed"] == []
    assert aged.exists(), "dry_run:true deleted a file"


@pytest.mark.parametrize("params", [{}, {"dry_run": "true"}, {"dry_run": 1}])
def test_review_trail_rest_dry_run_must_be_an_explicit_bool(tmp_path, params):
    result = asyncio.run(
        reaper._handler_review_trail_rest(params, repo_root=tmp_path / ".git")
    )
    assert result["exit_code"] == 1
    assert result["reaped"] == [] and result["failed"] == []


def test_review_trail_rest_missing_repo_root_is_a_setup_error_not_a_reap(tmp_path):
    result = asyncio.run(
        reaper._handler_review_trail_rest({"dry_run": False}, repo_root=None)
    )
    assert result["exit_code"] == 1
    assert result["reaped"] == []
