"""
Tests for coordinator_core.updatedocs.directory_md.

Spec backlink: pln-bucket-2-extraction-four-deter-e121fa (chunk C2)
"""

from __future__ import annotations

from datetime import date

import pytest

from coordinator_core.updatedocs.directory_md import (
    DirectoryMdUnavailable,
    compute_directory_md_drift,
)

REAL_REFRESHED_LINE = (
    "Last refreshed: 2026-08-06 (spot-check only — see coverage note above)."
)


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_absent_file_raises_typed_unavailable(tmp_path):
    missing = tmp_path / "DIRECTORY.md"
    with pytest.raises(DirectoryMdUnavailable) as excinfo:
        compute_directory_md_drift(missing)
    assert excinfo.value.path == missing


def test_real_trailing_parenthetical_line_is_parsed(tmp_path):
    doc = _write(tmp_path, "DIRECTORY.md", REAL_REFRESHED_LINE + "\n")
    drift = compute_directory_md_drift(doc)
    assert drift.refreshed_on == date(2026, 8, 6)
    assert drift.age_days == (date.today() - date(2026, 8, 6)).days
    assert drift.age_days >= 0


def test_malformed_last_refreshed_line_yields_none_not_zero(tmp_path):
    doc = _write(tmp_path, "DIRECTORY.md", "Last refreshed: not-a-date\n")
    drift = compute_directory_md_drift(doc)
    assert drift.refreshed_on is None
    assert drift.age_days is None


def test_absent_last_refreshed_line_yields_none_not_zero(tmp_path):
    doc = _write(tmp_path, "DIRECTORY.md", "No refresh line here at all.\n")
    drift = compute_directory_md_drift(doc)
    assert drift.refreshed_on is None
    assert drift.age_days is None


def test_count_claim_matches_when_actual_equals_asserted(tmp_path):
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "conftest.py").write_text("", encoding="utf-8")

    doc = _write(
        tmp_path,
        "DIRECTORY.md",
        REAL_REFRESHED_LINE + "\n"
        "2 `conftest.py`/test-support files across the tree.\n",
    )
    drift = compute_directory_md_drift(doc)
    assert len(drift.count_claims) == 1
    claim = drift.count_claims[0]
    assert claim.asserted == 2
    assert claim.actual == 2
    assert claim.matches is True


def test_count_claim_mismatch_is_reported_not_hidden(tmp_path):
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")

    doc = _write(
        tmp_path,
        "DIRECTORY.md",
        REAL_REFRESHED_LINE + "\n"
        "19 `conftest.py`/test-support files across the tree.\n",
    )
    drift = compute_directory_md_drift(doc)
    claim = drift.count_claims[0]
    assert claim.asserted == 19
    assert claim.actual == 1
    assert claim.matches is False
    assert drift.has_drift is True


def test_no_count_assertions_yields_empty_list(tmp_path):
    doc = _write(tmp_path, "DIRECTORY.md", REAL_REFRESHED_LINE + "\n")
    drift = compute_directory_md_drift(doc)
    assert drift.count_claims == []


def test_never_infers_a_count_the_document_does_not_state(tmp_path):
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "b.py").write_text("", encoding="utf-8")
    doc = _write(
        tmp_path,
        "DIRECTORY.md",
        REAL_REFRESHED_LINE + "\nSome prose mentioning files but no digit assertion.\n",
    )
    drift = compute_directory_md_drift(doc)
    assert drift.count_claims == []


def test_real_repo_directory_md_reports_drift():
    real_path = (
        __import__("pathlib").Path(__file__).resolve().parents[2] / "DIRECTORY.md"
    )
    drift = compute_directory_md_drift(real_path)
    assert drift.refreshed_on == date(2026, 8, 6)
    assert drift.age_days is not None
    assert drift.age_days > 14
    assert drift.has_drift is True
