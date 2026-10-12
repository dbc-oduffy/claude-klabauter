"""release-notes rule: a pass and each miss, including a stale placeholder wrapper beside a good one."""

import hashlib

from coordinator_core.workweek_complete.release.notes_rule import evaluate

ENTRY = "state/completions/a.md"
LINT_OK = [{"id": "lint", "status": "ran", "exit_code": 0}]


def _repo(tmp_path, notes="# Notes\n- thing (state/completions/a.md)\n", wrapper="Release v0.7.0\n"):
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


def _run(root, rows=LINT_OK, entries=(ENTRY,)):
    return evaluate(root, version="0.7.0", confirmed_entries=list(entries), ledger_rows=rows)


def test_pass_with_stale_placeholder_beside_good_wrapper(tmp_path):
    r = _run(_repo(tmp_path))
    assert r.passed
    assert r.evidence["entry_count"] == 1
    assert r.evidence["wrapper"] == "archive/release-notes/2026-10-12-v0.7.0.md"
    assert r.evidence["wrapper_sha256"] == hashlib.sha256(b"Release v0.7.0\n").hexdigest()


def test_notes_missing(tmp_path):
    assert _run(_repo(tmp_path, notes=None)).reason == "notes-missing"


def test_notes_empty(tmp_path):
    r = _run(_repo(tmp_path, notes="# Release\n\n## Added\n_none this week_\n"))
    assert r.reason == "notes-empty"


def test_entry_not_cited_lists_missing(tmp_path):
    r = _run(_repo(tmp_path), entries=(ENTRY, "state/completions/b.md"))
    assert r.reason == "entry-not-cited"
    assert r.evidence["uncited"] == ["state/completions/b.md"]


def test_wrapper_absent_is_placeholder_version(tmp_path):
    assert _run(_repo(tmp_path, wrapper=None)).reason == "placeholder-version"


def test_wrapper_body_placeholder(tmp_path):
    assert _run(_repo(tmp_path, wrapper="Release <version>\n")).reason == "placeholder-version"


def test_lint_failed(tmp_path):
    root = _repo(tmp_path)
    assert _run(root, rows=[]).reason == "lint-failed"
    bad = [{"id": "lint", "status": "ran", "exit_code": 1}]
    assert _run(root, rows=bad).reason == "lint-failed"
