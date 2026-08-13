"""Parity tests for coordinator_core.ops.verify_orientation_cache_sync.

Golden oracle: the bash original, snapshotted against a positive fixture +
a battery of negative fixtures (schema violations covering every check the
bash script performs) plus UE-detector-guard fixtures (uproject
present/absent x trust-caveats present/absent), pre-port. This test suite
reproduces the same fixtures and asserts byte-identical violation text +
exit-code parity.

Port of: verify-orientation-cache-sync.sh (coordinator-claude b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

C6 (2026-07-30): the positive fixture and its heading-shape negative tests
were updated for the writer's purpose-map rewrite -- ``## Project`` /
``## Counters`` are no longer schema-legal headings; ``## Wiki`` (one of the
four replacement routing sections) stands in for them here.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.verify_orientation_cache_sync import main, verify

_POSITIVE = """---
generated_by: update-docs
generated_at: 2026-07-13T09:41:49Z
git_head_at_generation: ae65840f
---

# Orientation Cache

## Active workstreams
1. Coordinator-claude authoring repo build & doctrine migration

## Pinboard
- 2026-07-16 doe-porter: some note here.

## Auto-push health
- ⚠ 3 unpushed commit(s) on work/foo.

## Branch
`work/machine-b/2026-07-10to12` — 746/0 vs origin/main

## Wiki
- `docs/wiki/` — doctrine/reference material; browse before assuming absence.
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_positive_fixture_passes(tmp_path: Path) -> None:
    cache = _write(tmp_path, "cache.md", _POSITIVE)
    violations, line_count = verify(str(cache), str(tmp_path))
    assert violations == []
    assert line_count == 22


def test_no_frontmatter(tmp_path: Path) -> None:
    cache = _write(
        tmp_path,
        "cache.md",
        "# Orientation Cache\n\n## Wiki\n- `docs/wiki/` — no frontmatter here.\n",
    )
    violations, _ = verify(str(cache), str(tmp_path))
    assert violations == [
        "frontmatter not found (expected --- delimited block at top of file)"
    ]


def test_out_of_schema_heading(tmp_path: Path) -> None:
    cache = _write(
        tmp_path,
        "cache.md",
        "---\ngenerated_by: update-docs\ngenerated_at: 2026-07-13T09:41:49Z\n"
        "git_head_at_generation: ae65840f\n---\n\n## NotASchemaHeading\n- something\n",
    )
    violations, _ = verify(str(cache), str(tmp_path))
    assert violations == ["out-of-schema heading: '## NotASchemaHeading'"]


def test_project_counters_priorities_headings_are_retired(tmp_path: Path) -> None:
    """C6: the three census/answer-shaped headings the writer retired must
    now be flagged the same as any other out-of-schema heading, not silently
    accepted as legacy content."""
    cache = _write(
        tmp_path,
        "cache.md",
        "---\ngenerated_by: update-docs\ngenerated_at: 2026-07-13T09:41:49Z\n"
        "git_head_at_generation: ae65840f\n---\n\n"
        "## Project\n> stale re-quote\n\n"
        "## Counters\n- **Handoffs ready:** 4.\n\n"
        "## Priorities\n- **Widget work:** urgent (explicit)\n",
    )
    violations, _ = verify(str(cache), str(tmp_path))
    assert violations == [
        "out-of-schema heading: '## Project'",
        "out-of-schema heading: '## Counters'",
        "out-of-schema heading: '## Priorities'",
    ]


def test_wiki_pointer_line_must_be_a_bullet(tmp_path: Path) -> None:
    cache = _write(
        tmp_path,
        "cache.md",
        "---\ngenerated_by: update-docs\ngenerated_at: 2026-07-13T09:41:49Z\n"
        "git_head_at_generation: ae65840f\n---\n\n## Wiki\n"
        "docs/wiki/ (not a bullet)\n",
    )
    violations, _ = verify(str(cache), str(tmp_path))
    assert violations == [
        "'## Wiki' line fails bullet shape '- ...': 'docs/wiki/ (not a bullet)'"
    ]


def test_pinboard_too_many_lines(tmp_path: Path) -> None:
    cache = _write(
        tmp_path,
        "cache.md",
        "---\ngenerated_by: update-docs\ngenerated_at: 2026-07-13T09:41:49Z\n"
        "git_head_at_generation: ae65840f\n---\n\n## Pinboard\n"
        "- 2026-07-16 doe-porter: note one.\n- 2026-07-16 doe-porter: note two.\n",
    )
    violations, _ = verify(str(cache), str(tmp_path))
    assert violations == ["Pinboard section has 2 lines (max 1 — one-slot only)"]


def test_generated_by_not_a_slug(tmp_path: Path) -> None:
    cache = _write(
        tmp_path,
        "cache.md",
        "---\ngenerated_by: patched by update-docs (manual fix)\n"
        "generated_at: 2026-07-13T09:41:49Z\ngit_head_at_generation: ae65840f\n---\n\n"
        "## Wiki\n- `docs/wiki/` — hi\n",
    )
    violations, _ = verify(str(cache), str(tmp_path))
    assert violations == [
        "frontmatter generated_by='patched by update-docs (manual fix)' is not a "
        "single lowercase slug (no parentheticals, no 'patched by' prose allowed)"
    ]


def test_file_length_ceiling(tmp_path: Path) -> None:
    from coordinator_core.ops.verify_orientation_cache_sync import LINE_CEILING

    n_lines = LINE_CEILING + 10
    body_lines = "\n".join(f"- line{i}" for i in range(1, n_lines + 1))
    content = (
        "---\ngenerated_by: update-docs\ngenerated_at: 2026-07-13T09:41:49Z\n"
        "git_head_at_generation: ae65840f\n---\n\n## Wiki\n" + body_lines + "\n"
    )
    cache = _write(tmp_path, "cache.md", content)
    violations, line_count = verify(str(cache), str(tmp_path))
    assert line_count > LINE_CEILING
    assert f"file length {line_count} exceeds {LINE_CEILING}-line ceiling" in violations


def test_uproject_present_missing_trust_caveats(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "sub" / "Game.uproject").write_text("", encoding="utf-8")
    cache = _write(
        tmp_path,
        "cache.md",
        "---\ngenerated_by: update-docs\ngenerated_at: 2026-07-13T09:41:49Z\n"
        "git_head_at_generation: ae65840f\n---\n\n"
        "## Wiki\n- `docs/wiki/` — a UE project with no trust caveats section\n",
    )
    violations, _ = verify(str(cache), str(repo))
    assert len(violations) == 1
    assert violations[0].startswith("detector-output missing: *.uproject present in repo (")
    assert violations[0].endswith(
        "sub/Game.uproject) but ## Trust caveats section absent — regenerate "
        "via bin/regenerate-orientation-cache"
    )


def test_uproject_present_with_correct_trust_caveats(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "sub" / "Game.uproject").write_text("", encoding="utf-8")
    cache = _write(
        tmp_path,
        "cache.md",
        "---\ngenerated_by: update-docs\ngenerated_at: 2026-07-13T09:41:49Z\n"
        "git_head_at_generation: ae65840f\n---\n\n"
        "## Trust caveats\n- Unreal Engine project detected — verify via example-retrieval-repo.\n\n"
        "## Wiki\n- `docs/wiki/` — fine\n",
    )
    violations, _ = verify(str(cache), str(repo))
    assert violations == []


def test_no_uproject_no_guard_fired(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = _write(tmp_path, "cache.md", _POSITIVE)
    violations, _ = verify(str(cache), str(repo))
    assert violations == []


def test_main_ok_exit_zero(tmp_path: Path, capsys) -> None:
    cache = _write(tmp_path, "cache.md", _POSITIVE)
    rc = main([str(cache), str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("verify-orientation-cache-sync: OK (")
    assert "22 lines" in out


def test_main_fail_exit_one(tmp_path: Path, capsys) -> None:
    cache = _write(
        tmp_path,
        "cache.md",
        "# Orientation Cache\n\n## Wiki\n- `docs/wiki/` — no frontmatter here.\n",
    )
    rc = main([str(cache), str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "verify-orientation-cache-sync: FAIL (" in err
    assert "frontmatter not found" in err
    assert "Fix: regenerate the cache via bin/regenerate-orientation-cache" in err


def test_main_usage_error_exit_two(capsys) -> None:
    rc = main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "usage:" in err
