"""A review wave's own fix lands at its fixed bytes even when the reviewer's whole-second ``verified_at``
stamp truncates below the fix's file mtime; a foreign edit past that second still refuses blob-drift."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_run, terminal_commit_review
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (  # noqa: F401 -- repo is a fixture
    _WAVE_FIXED,
    SID,
    _drifted_a,
    _fire,
    _row,
    _wave_sidecar,
    repo,
)
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_STAMPED = 3000.0


def test_a_fix_written_in_the_second_the_reviewer_stamped_lands(repo, monkeypatch):
    _drifted_a(repo, mtime=_STAMPED + 0.6)
    _wave_sidecar(repo, _STAMPED, [_row("a.py")])
    out, seen = _fire(repo, monkeypatch, _WAVE_FIXED)
    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"] == {"a.py": touch_record.compute_content_hash(repo / "a.py")}


def test_a_foreign_edit_after_that_second_still_refuses(repo, monkeypatch):
    _drifted_a(repo, b"FOREIGN\n", mtime=_STAMPED + 2.0)
    _wave_sidecar(repo, _STAMPED, [_row("a.py")])
    out, _ = _fire(repo, monkeypatch, _WAVE_FIXED)
    assert out["refused"] == "blob-drift"


def test_a_fractional_stamp_keeps_its_exact_cutoff(repo, monkeypatch):
    _drifted_a(repo, b"LATE\n", mtime=_STAMPED + 0.6)
    _wave_sidecar(repo, _STAMPED, [_row("a.py")])
    sidecar = repo / ".coordinator-local" / "subagent-share" / SID / "reviewer.md"
    sidecar.write_text(
        sidecar.read_text(encoding="utf-8").replace("1970-01-01T00:50:00Z", "1970-01-01T00:50:00.250Z"),
        encoding="utf-8", newline="",
    )
    out, _ = _fire(repo, monkeypatch, _WAVE_FIXED)
    assert out["refused"] == "blob-drift"


def test_an_applier_write_long_after_the_stamp_that_carries_the_fix_lands(repo, monkeypatch):
    _drifted_a(repo, b"x = 'new'\n", mtime=_STAMPED + 101.0)
    _wave_sidecar(repo, _STAMPED, [_row("a.py")])
    out, seen = _fire(repo, monkeypatch, _WAVE_FIXED)
    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"] == {"a.py": touch_record.compute_content_hash(repo / "a.py")}


def test_a_foreign_write_long_after_the_stamp_without_the_fix_still_refuses(repo, monkeypatch):
    _drifted_a(repo, b"x = 'FOREIGN'\n", mtime=_STAMPED + 101.0)
    _wave_sidecar(repo, _STAMPED, [_row("a.py")])
    out, _ = _fire(repo, monkeypatch, _WAVE_FIXED)
    assert out["refused"] == "blob-drift"


def test_a_pure_removal_row_lands_only_once_the_before_text_is_gone(repo, monkeypatch):
    _drifted_a(repo, b"x = 1\n", mtime=_STAMPED + 101.0)
    _wave_sidecar(repo, _STAMPED, [_row("a.py", before="dead line", after="")])
    out, _ = _fire(repo, monkeypatch, _WAVE_FIXED)
    assert out["committed"] is True, out


def test_a_pure_removal_row_refuses_while_the_before_text_remains(repo, monkeypatch):
    _drifted_a(repo, b"x = 'dead line'  # FOREIGN\n", mtime=_STAMPED + 101.0)
    _wave_sidecar(repo, _STAMPED, [_row("a.py", before="dead line", after="")])
    out, _ = _fire(repo, monkeypatch, _WAVE_FIXED)
    assert out["refused"] == "blob-drift"


def test_an_integration_record_wave_keeps_the_ledger_rows(repo):
    import os

    _wave_sidecar(repo, _STAMPED, [_row("a.py")])
    integ = repo / "integration.md"
    integ.write_text("---\nprep:\n  slice_files:\n    - a.py\n---\n", encoding="utf-8")
    os.utime(integ, (_STAMPED + 5, _STAMPED + 5))
    review = {**_WAVE_FIXED, "integration": {"sidecar": "integration.md"}}
    at, paths = terminal_commit_review._fixes_integrated_at(repo, review)
    assert at == _STAMPED + 5
    assert "a.py" in paths
    assert paths.fix_rows == {"a.py": [("old", "new")]}


@pytest.mark.parametrize(
    "name",
    ["_fixes_integrated_at", "_reviewer_applied_fixes", "_wave_slice_files", "_epoch_of"],
)
def test_land_run_holds_no_second_copy_of_the_own_fix_helpers(name):
    assert getattr(land_run, name) is getattr(terminal_commit_review, name)
