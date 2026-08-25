"""
Tests for coordinator_core.ops.check_no_monolith_completion_append.

Mirrors the bash oracle's own test suite — same eight scenarios, ported to
pytest against the Python module's scan()/main() directly (no subprocess, no
bash dependency).

Port of: test-check-no-monolith-completion-append.sh (DoE 894d4bc6, 2026-07-22)
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.check_no_monolith_completion_append import main, scan


def _make_root(tmp_path, **files):
    root = tmp_path / "coordinator_root"
    for d in ("skills", "commands", "agents", "pipelines", "hooks"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        full = root / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return root


def test_forbidden_literal_path_exits_1(tmp_path):
    root = _make_root(
        tmp_path,
        **{
            "skills/test-skill.md": (
                "## Completion\n\n"
                "Write the entry to `archive/completed/2026-05.md` at the end of the session.\n"
            )
        },
    )
    hits, unreadable, rc = scan(str(root))
    assert rc == 1
    assert any("test-skill.md" in h for h in hits)


def test_clean_per_entry_path_exits_0(tmp_path):
    root = _make_root(
        tmp_path,
        **{
            "skills/session-end-clean.md": (
                "## Completion\n\nWrite the entry to `archive/completed/2026-05/my-task.md`.\n"
            )
        },
    )
    hits, unreadable, rc = scan(str(root))
    assert rc == 0
    assert hits == []


def test_all_six_call_shapes_detected(tmp_path):
    root = _make_root(
        tmp_path,
        **{
            "skills/all-shapes.md": (
                "Shape 1 literal: archive/completed/2026-05.md in markdown body.\n"
                "Shape 2 redirect: >> archive/completed/2026-05.md appends to monolith.\n"
                "Shape 3 heredoc: cat >> archive/completed/2026-05.md <<'DONE' pipes content.\n"
                "Shape 4 tee: tee -a archive/completed/2026-05.md reads stdin.\n"
                "Shape 5 node: writeFileSync('archive/completed/2026-05.md', content)\n"
                "Shape 6 gitm: git mv archive/completed/2026-05.md archive/completed/legacy/2026-05.md\n"
            ),
            "agents/gitm-shape.md": (
                "git mv archive/completed/2026-05.md some-other-destination.md\n"
            ),
        },
    )
    hits, unreadable, rc = scan(str(root))
    assert rc == 1
    assert any("all-shapes.md" in h for h in hits)
    assert any("gitm-shape.md" in h for h in hits)


def test_exception_legacy_path_excluded(tmp_path):
    root = _make_root(
        tmp_path,
        **{
            "skills/migration-step.md": (
                "git mv archive/completed/2026-05.md archive/completed/legacy/2026-05.md\n"
            )
        },
    )
    hits, unreadable, rc = scan(str(root))
    assert rc == 0
    assert hits == []


def test_exception_override_line_excluded(tmp_path):
    root = _make_root(
        tmp_path,
        **{
            "skills/override-doc.md": (
                "If COORDINATOR_OVERRIDE_LEGACY_MONOLITH=1 is set, skip the git mv "
                "of archive/completed/2026-05.md.\n"
            )
        },
    )
    hits, unreadable, rc = scan(str(root))
    assert rc == 0
    assert hits == []


def test_exception_tripwire_comment_excluded(tmp_path):
    root = _make_root(
        tmp_path,
        **{
            "skills/tripwire-comment.md": (
                "<!-- TRIPWIRE: NO monolithic append — archive/completed/2026-05.md "
                "writes outside legacy/ are forbidden. -->\n"
            )
        },
    )
    hits, unreadable, rc = scan(str(root))
    assert rc == 0
    assert hits == []


def test_exception_docs_wiki_excluded_but_skills_still_flagged(tmp_path):
    root = _make_root(
        tmp_path,
        **{
            "docs/wiki/completion-log.md": (
                "The legacy monolith format used archive/completed/2026-05.md as its write target.\n"
            ),
            "skills/ref-doc.md": (
                "See docs/wiki/completion-log.md for the old archive/completed/2026-05.md format.\n"
            ),
        },
    )
    hits, unreadable, rc = scan(str(root))
    assert rc == 1
    assert not any("/docs/wiki/completion-log.md:" in h for h in hits)
    assert any("ref-doc.md" in h for h in hits)


def test_exception_hooks_scripts_tests_fixture_excluded(tmp_path):
    root = _make_root(
        tmp_path,
        **{
            "hooks/scripts/tests/test-block.sh": (
                '\'{"file_path":"/tmp/archive/completed/2026-05.md"}\'\n'
            )
        },
    )
    hits, unreadable, rc = scan(str(root))
    assert rc == 0
    assert hits == []


def test_root_not_a_directory_exits_2(tmp_path):
    hits, unreadable, rc = scan(str(tmp_path / "nonexistent"))
    assert rc == 2
    assert hits == []


def test_root_with_no_recognized_subdirs_exits_2(tmp_path):
    root = tmp_path / "empty_root"
    root.mkdir()
    hits, unreadable, rc = scan(str(root))
    assert rc == 2
    assert hits == []


def test_unreadable_file_fails_closed_rc1(tmp_path):
    """BEHAVIOUR CHANGE regression: a file the scan cannot open must not be
    silently skipped — this is the failure-path case for the 2026-07-22
    break-class fix (previously a bare `continue` let this return rc 0)."""
    root = _make_root(
        tmp_path,
        **{"skills/clean.md": "archive/completed/2026-05/my-task.md\n"},
    )
    unreadable_path = root / "skills" / "unreadable.md"
    unreadable_path.write_text("placeholder\n", encoding="utf-8")
    os.chmod(unreadable_path, 0o000)
    try:
        hits, unreadable, rc = scan(str(root))
    finally:
        os.chmod(unreadable_path, 0o644)
    assert rc == 1
    assert hits == []
    assert any("unreadable.md" in u for u in unreadable)


def test_main_unreadable_file_only_exits_1_with_scan_incomplete_message(tmp_path, capsys):
    root = _make_root(
        tmp_path,
        **{"skills/clean.md": "archive/completed/2026-05/my-task.md\n"},
    )
    unreadable_path = root / "skills" / "unreadable.md"
    unreadable_path.write_text("placeholder\n", encoding="utf-8")
    os.chmod(unreadable_path, 0o000)
    try:
        rc = main(["--root", str(root)])
    finally:
        os.chmod(unreadable_path, 0o644)
    captured = capsys.readouterr()
    assert rc == 1
    assert "SCAN INCOMPLETE" in captured.err
    assert "unreadable.md" in captured.err


def test_main_missing_root_flag_exits_2(capsys):
    rc = main([])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--root is required" in captured.err


def test_main_unknown_option_exits_2(tmp_path, capsys):
    rc = main(["--bogus"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "Unknown option" in captured.err


def test_main_root_missing_value_exits_2():
    rc = main(["--root"])
    assert rc == 2


def test_main_clean_exits_0_and_prints_clean_banner(tmp_path, capsys):
    root = _make_root(tmp_path)
    rc = main(["--root", str(root)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "CLEAN" in captured.out


def test_main_dirty_exits_1_and_prints_hits_to_stderr(tmp_path, capsys):
    root = _make_root(
        tmp_path,
        **{"skills/bad.md": "archive/completed/2026-05.md\n"},
    )
    rc = main(["--root", str(root)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "UNAUTHORIZED MONOLITH WRITE DETECTED" in captured.err
    assert "bad.md" in captured.err
