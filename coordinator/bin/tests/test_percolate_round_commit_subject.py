"""test_percolate_round_commit_subject — pins the commit-subject shape fix
(a real-run commit whose subject reported the SIZE OF A COMPARISON IT DID
NOT COMMIT: `_summarize_change_lines(real_changes)`, where `real_changes`
is publish.py's own dest-working-tree `filecmp` scrape, not the derived
commit pathspec). `_build_commit_subject` now names both numbers, labelled;
`_report_commit_residual` surfaces the gap between them on stderr rather
than discarding it silently.

Unit-level only, same posture as test_percolate_round_commit_pathspec.py:
exercises `_build_commit_subject` / `_report_commit_residual` directly
against captured (tag, path) shapes, never via a subprocess or a real
percolate round (must not run a real publish round or touch a live
mirror).

Spec backlink: this dispatch's brief (percolate-round.py subject
construction, real-run leg) — pln n/a, ad-hoc chunk.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_commit_subject.py -q
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_commit_subject", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def test_subject_reports_pathspec_size_not_raw_change_line_count():
    """The originating defect: 3-4 files actually named in the pathspec,
    but ~2000 raw change lines scraped from publish.py's dest-comparison
    output. The subject must show the pathspec's own size (what is about
    to be committed), never present the raw scrape count as if it were
    that.
    """
    real_changes = [("UPDATE", f"file{i}.py") for i in range(1875)] + [
        ("NEW", f"new{i}.py") for i in range(45)
    ] + [("REMOVE", f"gone{i}.py") for i in range(39)]
    pathspec = ["file1.py", "file2.py", "new1.py"]

    subject = _mod._build_commit_subject("claude-klabauter", real_changes, pathspec)

    assert "3 file(s) to commit" in subject
    assert "45 added" in subject
    assert "1875 modified" in subject
    assert "39 removed" in subject
    # The raw scrape total (1959) must never appear standing in for the
    # commit's own file count.
    assert "1959 file(s) to commit" not in subject


def test_subject_matches_when_pathspec_equals_real_changes():
    real_changes = [("NEW", "a.py"), ("UPDATE", "b.py")]
    pathspec = ["a.py", "b.py"]

    subject = _mod._build_commit_subject("t", real_changes, pathspec)

    assert "2 file(s) to commit" in subject
    assert "1 added" in subject
    assert "1 modified" in subject
    assert "0 removed" in subject


def test_residual_report_silent_when_counts_agree(capsys):
    real_changes = [("NEW", "a.py"), ("UPDATE", "b.py")]
    pathspec = ["a.py", "b.py"]

    _mod._report_commit_residual("t", real_changes, pathspec)

    captured = capsys.readouterr()
    assert captured.err == ""


def test_residual_report_names_both_numbers_when_they_diverge(capsys):
    real_changes = [("UPDATE", f"file{i}.py") for i in range(1875)]
    pathspec = ["file1.py", "file2.py", "file3.py"]

    _mod._report_commit_residual("claude-klabauter", real_changes, pathspec)

    captured = capsys.readouterr()
    assert "1875 change line(s)" in captured.err
    assert "3 path(s)" in captured.err
    assert "claude-klabauter" in captured.err
    assert captured.out == ""
