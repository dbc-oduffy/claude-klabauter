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
    """Load `percolate-round.py` by path, or skip this module if it is absent.

    NEGATIVE SPEC: the skip is not defensive padding around a file that is
    always there -- it is load-bearing in the PUBLISHED artifact. This test
    is carried into the klabauter mirror by the `claude-klabauter-coordinator-
    tests` row while `coordinator/bin/percolate-round.py` is NOT published at
    all, so an unguarded module-level `exec_module` raises at COLLECTION in
    the mirror: not one failing test but a hard collection error taking the
    whole file down, in an artifact nobody in this repo runs. Reported by
    example-cockpit-repo-30 / doe-claude-em, 2026-09-04.

    Do not "simplify" this back to a bare load on the grounds that the file
    is obviously present -- it is present HERE, which is exactly the reason
    the breakage was invisible for as long as it was. Skipping at module
    level keeps the published copy collectable and says why it skipped;
    whether `percolate-round.py` ought to be published is a separate
    publish-scope question this guard deliberately does not decide.
    """
    script = _BIN_DIR / "percolate-round.py"
    if not script.is_file():
        pytest.skip(
            "percolate-round.py is not present beside this test (published "
            "mirror carries the test without its subject)",
            allow_module_level=True,
        )
    spec = importlib.util.spec_from_file_location(
        "percolate_round_commit_subject", script
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
    # The triple sizes off what the pathspec CARRIES, never off the full
    # scrape: two of the three carried paths are UPDATEs, one is a NEW, and
    # not one of the 39 REMOVEs is in the pathspec.
    assert "1 added" in subject
    assert "2 modified" in subject
    assert "0 removed" in subject
    assert "1956 reported change(s) not carried" in subject
    # The raw scrape total (1959) must never appear standing in for the
    # commit's own file count.
    assert "1959 file(s) to commit" not in subject


def test_subject_never_claims_removals_the_commit_does_not_carry():
    """The public-history defect the DoE-claude memo (2026-08-26) reported:
    a mirror commit whose subject read "dest diverged on 646 added, 0
    modified, 67 removed" while the removal side was gated off downstream,
    so the commit removed nothing. The mirror's git history is public — a
    subject that asserts removals that never happened is a permanent
    false record.
    """
    real_changes = [("NEW", f"a{i}.py") for i in range(3)] + [
        ("REMOVE", f"gone{i}.py") for i in range(67)
    ]
    pathspec = ["a0.py", "a1.py", "a2.py"]

    subject = _mod._build_commit_subject("coordinator-claude", real_changes, pathspec)

    assert "3 added" in subject
    assert "0 removed" in subject
    assert "67 removed" not in subject
    assert "67 reported change(s) not carried" in subject


def test_subject_matches_when_pathspec_equals_real_changes():
    real_changes = [("NEW", "a.py"), ("UPDATE", "b.py")]
    pathspec = ["a.py", "b.py"]

    subject = _mod._build_commit_subject("t", real_changes, pathspec)

    assert "2 file(s) to commit" in subject
    assert "1 added" in subject
    assert "1 modified" in subject
    assert "0 removed" in subject
    assert "not carried" not in subject


def test_residual_report_silent_when_counts_agree(capsys):
    real_changes = [("NEW", "a.py"), ("UPDATE", "b.py")]
    pathspec = ["a.py", "b.py"]

    assert _mod._report_commit_residual("t", real_changes, pathspec) is None

    captured = capsys.readouterr()
    assert captured.err == ""


def test_residual_report_names_both_numbers_when_they_diverge(capsys):
    real_changes = [("UPDATE", f"file{i}.py") for i in range(1875)]
    pathspec = ["file1.py", "file2.py", "file3.py"]

    warning = _mod._report_commit_residual("claude-klabauter", real_changes, pathspec)

    captured = capsys.readouterr()
    assert "1875 change line(s)" in captured.err
    assert "3 path(s)" in captured.err
    assert "claude-klabauter" in captured.err
    assert captured.out == ""
    # Stderr alone let a dropped-change round print a bare PASS: the
    # verdict block counts what this returns.
    assert warning is not None
    assert "1872 change(s) the real run reported were NOT committed" in warning


def test_residual_warning_still_counts_removals_with_the_gate_open(capsys):
    """Re-pointed at the post-flip behaviour, as its predecessor instructed
    (`_REMOVAL_SIDE_ENABLED` is now True, PM 2026-08-26).

    The gate-note branch is correctly silent — the flag is no longer why a
    removal went uncarried — but the DIVERGENCE itself must still be counted
    and named. That is the whole point of the DoE-claude memo this reporting
    exists to answer: a round that drops changes and prints a bare PASS. With
    the gate open a dropped removal means something else (Leg A's on-disk
    skip, or one of `_filter_commit_pathspec`'s three safety filters), and an
    operator needs it surfaced just as loudly."""
    assert _mod._REMOVAL_SIDE_ENABLED is True
    real_changes = [("NEW", "kept.py")] + [
        ("REMOVE", f"whoami/f{i}.py") for i in range(23)
    ]
    pathspec = ["kept.py"]

    warning = _mod._report_commit_residual("coordinator-claude", real_changes, pathspec)

    assert warning is not None
    assert "23 change(s) the real run reported were NOT committed" in warning
    # The flag is no longer the explanation, so it must not be blamed.
    assert "_REMOVAL_SIDE_ENABLED" not in warning
    assert "intent vs commit pathspec diverge" in capsys.readouterr().err


def test_carried_partition_is_by_path_not_by_count():
    """A same-size pathspec carrying DIFFERENT paths is still a drop — the
    old length-only comparison read that as agreement and reported nothing.
    """
    real_changes = [("NEW", "a.py"), ("REMOVE", "b.py")]
    pathspec = ["a.py", "c.py"]

    carried, dropped = _mod._partition_carried_changes(real_changes, pathspec)

    assert carried == [("NEW", "a.py")]
    assert dropped == [("REMOVE", "b.py")]
    assert _mod._report_commit_residual("t", real_changes, pathspec) is not None


def test_round_warnings_degrade_a_clean_pass():
    """The verdict an operator reads must count the residual gap. Before
    this, `has_review_warnings` was the ONLY input to the verdict, so a
    round that dropped every removal still printed `PASS`.
    """
    assert _mod._round_warnings(has_review_warnings=False, residual_warning=None) == []

    only_residual = _mod._round_warnings(
        has_review_warnings=False, residual_warning="57 change(s) NOT committed"
    )
    assert only_residual == ["57 change(s) NOT committed"]

    both = _mod._round_warnings(
        has_review_warnings=True, residual_warning="57 change(s) NOT committed"
    )
    assert len(both) == 2
    assert any("REVIEW warnings" in w for w in both)
