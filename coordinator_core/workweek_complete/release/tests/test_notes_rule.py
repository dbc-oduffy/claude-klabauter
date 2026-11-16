"""release-notes rule: a pass and each miss, including a stale placeholder wrapper beside a good one."""

import hashlib

import pytest

from coordinator_core.workweek_complete.release.contract import RuleResult
from coordinator_core.workweek_complete.release.notes_rule import evaluate
from pathlib import Path

ENTRY = "state/completions/a.md"


def _repo(tmp_path, notes: str | None = "# Notes\n- thing (state/completions/a.md)\n", wrapper: str | None = "Release v0.7.0\n"):
    if notes is not None:
        d = tmp_path / "state" / "week-changelog"
        d.mkdir(parents=True)
        (d / "2026-10-12-pending-release.md").write_text(notes, encoding="utf-8")
    w = tmp_path / "archive" / "release-notes"
    w.mkdir(parents=True)
    (w / "2026-09-13-vX.Y.Z.md").write_text("vX.Y.Z stale\n", encoding="utf-8")
    if wrapper is not None:
        (w / "2026-10-12-v0.7.0.md").write_bytes(wrapper.encode("utf-8"))
    return tmp_path


def _run(root, entries=(ENTRY,)) -> RuleResult:
    return evaluate(root, version="0.7.0", confirmed_entries=list(entries))


def test_pass_with_stale_placeholder_beside_good_wrapper(tmp_path: Path) -> None:
    r = _run(_repo(tmp_path))
    assert r.passed
    assert r.evidence["entry_count"] == 1
    assert r.evidence["wrapper"] == "archive/release-notes/2026-10-12-v0.7.0.md"
    assert r.evidence["wrapper_sha256"] == hashlib.sha256(b"Release v0.7.0\n").hexdigest()


def test_notes_missing(tmp_path: Path) -> None:
    assert _run(_repo(tmp_path, notes=None)).reason == "notes-missing"


def test_notes_empty(tmp_path: Path) -> None:
    r = _run(_repo(tmp_path, notes="# Release\n\n## Added\n_none this week_\n"))
    assert r.reason == "notes-empty"


def test_entry_not_cited_lists_missing(tmp_path: Path) -> None:
    r = _run(_repo(tmp_path), entries=(ENTRY, "state/completions/b.md"))
    assert r.reason == "entry-not-cited"
    assert r.evidence["uncited"] == ["state/completions/b.md"]


def test_wrapper_absent_is_placeholder_version(tmp_path: Path) -> None:
    assert _run(_repo(tmp_path, wrapper=None)).reason == "placeholder-version"


def test_wrapper_body_placeholder(tmp_path: Path) -> None:
    assert _run(_repo(tmp_path, wrapper="Release <version>\n")).reason == "placeholder-version"


def test_lint_failed_is_not_a_miss_reason() -> None:
    with pytest.raises(ValueError):
        RuleResult("release-notes", False, "lint-failed")
