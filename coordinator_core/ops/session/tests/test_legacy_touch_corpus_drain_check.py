"""
coordinator_core.ops.session.tests.test_legacy_touch_corpus_drain_check —
coverage for the C9 drain-measurement gate (AC8).

Spec backlink: docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-
repointing-its-writers.md § AC8, chunk C9.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.session.legacy_touch_corpus_drain_check import (
    check_drain,
    main,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def test_empty_sessions_base_reports_zero(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"

    report = check_drain(sessions_base)

    assert report.scanned == 0
    assert report.undrained_count == 0
    assert report.drained_count == 0


def test_missing_sessions_base_reports_zero(tmp_path):
    sessions_base = tmp_path / "does-not-exist"

    report = check_drain(sessions_base)

    assert report.scanned == 0
    assert report.undrained_count == 0


def test_touched_only_dir_counts_as_undrained(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "a/b.py\n")

    report = check_drain(sessions_base)

    assert report.scanned == 1
    assert report.undrained_count == 1
    assert report.drained_count == 0
    assert sessions_base / "sid-1" / "touched.txt" in report.undrained


def test_dir_with_sibling_record_counts_as_drained(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "a/b.py\n")
    _write(sessions_base / "sid-1" / "touch-record.jsonl", '{"v":1,"verb":"T","ts":1.0,"sid":"sid-1","agent":null,"path":"a/b.py"}\n')

    report = check_drain(sessions_base)

    assert report.scanned == 1
    assert report.undrained_count == 0
    assert report.drained_count == 1


def test_agent_keyed_dirs_are_scanned(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / ".agents" / "aid-1" / "touched.txt", "a/b.py\n")

    report = check_drain(sessions_base)

    assert report.scanned == 1
    assert report.undrained_count == 1


def test_archive_component_is_excluded(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / ".archive" / "sid-old" / "touched.txt", "a/b.py\n")

    report = check_drain(sessions_base)

    assert report.scanned == 0
    assert report.undrained_count == 0


def test_mixed_corpus_reports_correct_count(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "a/b.py\n")
    _write(sessions_base / "sid-2" / "touched.txt", "c/d.py\n")
    _write(sessions_base / "sid-2" / "touch-record.jsonl", '{"v":1,"verb":"T","ts":1.0,"sid":"sid-2","agent":null,"path":"c/d.py"}\n')
    _write(sessions_base / ".agents" / "aid-1" / "touched.txt", "e/f.py\n")

    report = check_drain(sessions_base)

    assert report.scanned == 3
    assert report.undrained_count == 2
    assert report.drained_count == 1
    undrained_names = {p.parent.name for p in report.undrained}
    assert undrained_names == {"sid-1", "aid-1"}


def test_main_exits_nonzero_when_undrained(tmp_path, capsys):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "a/b.py\n")

    exit_code = main(["--sessions-base", str(sessions_base)])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "undrained (touched.txt only, no sibling): 1" in out


def test_main_exits_zero_when_fully_drained(tmp_path, capsys):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "a/b.py\n")
    _write(sessions_base / "sid-1" / "touch-record.jsonl", '{"v":1,"verb":"T","ts":1.0,"sid":"sid-1","agent":null,"path":"a/b.py"}\n')

    exit_code = main(["--sessions-base", str(sessions_base)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "undrained (touched.txt only, no sibling): 0" in out


def test_empty_sibling_over_real_claims_is_undrained(tmp_path):
    """An EMPTY touch-record.jsonl beside a touched.txt that still holds
    claims is UNDRAINED, not drained.

    Regression, 2026-08-27. The predicate was `record_path.exists()`, so a
    zero-byte sink left by a failed migration read as success. Measured on
    claude-klabauter: 133 dirs scanned, `undrained: []` reported, while eight
    sessions held 167 legacy claims against empty siblings -- claims
    compute_scope cannot see, on paths every peer's scope check therefore
    treats as orphans and is free to sweep. This module gates removal of
    the legacy union-read, so a false green here is unrecoverable loss.
    """
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "a/b.py\n")
    _write(sessions_base / "sid-1" / "touch-record.jsonl", "")

    report = check_drain(sessions_base)

    assert report.scanned == 1
    assert report.undrained_count == 1
