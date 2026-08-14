"""
Tests for coordinator_core.ops.read_frontmatter_field.

Port of: read-frontmatter-field.test.sh (DoE 3a561713, 2026-07-22) — mirrors
each bash-oracle fixture (T1-T10) 1:1 plus the CLI-entry (main()) exit-code contract.
"""

from __future__ import annotations

from coordinator_core.ops.read_frontmatter_field import main, read_frontmatter_field


def _write(tmp_path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_t1_double_quoted_no_comment(tmp_path):
    p = _write(tmp_path, "t1.md", '---\ndeliverable_id: "dlv-abc-1"\ntitle: "test"\n---\n\n# Body\n')
    assert read_frontmatter_field(p, "deliverable_id") == "dlv-abc-1"


def test_t2_double_quoted_with_trailing_comment(tmp_path):
    p = _write(
        tmp_path,
        "t2.md",
        '---\ntitle: "test"\ninitiative: "init-7"  # FK to state/initiatives/<id>.yaml; null when no named initiative\n---\n\n# Body\n',
    )
    assert read_frontmatter_field(p, "initiative") == "init-7"


def test_t3_null_with_trailing_comment(tmp_path):
    p = _write(
        tmp_path,
        "t3.md",
        '---\ntitle: "test"\ninitiative: null  # FK to state/initiatives/<id>.yaml; null when no named initiative\n---\n\n# Body\n',
    )
    assert read_frontmatter_field(p, "initiative") == ""


def test_t4_bare_null(tmp_path):
    p = _write(tmp_path, "t4.md", '---\ntitle: "test"\ninitiative: null\n---\n\n# Body\n')
    assert read_frontmatter_field(p, "initiative") == ""


def test_t5_bare_unquoted_value(tmp_path):
    p = _write(tmp_path, "t5.md", "---\ndeliverable_id: dlv-bare-2\n---\n\n# Body\n")
    assert read_frontmatter_field(p, "deliverable_id") == "dlv-bare-2"


def test_t6_single_quoted_value(tmp_path):
    p = _write(tmp_path, "t6.md", "---\nworkstream: 'foo-bar'\n---\n\n# Body\n")
    assert read_frontmatter_field(p, "workstream") == "foo-bar"


def test_t7_absent_field(tmp_path):
    p = _write(tmp_path, "t7.md", '---\ntitle: "some plan"\nworkstream: "my-ws"\n---\n\n# Body\n')
    assert read_frontmatter_field(p, "deliverable_id") == ""


def test_t8_nonexistent_file(tmp_path):
    missing = str(tmp_path / "does-not-exist-at-all.md")
    assert read_frontmatter_field(missing, "deliverable_id") == ""


def test_t9_double_quoted_null_literal(tmp_path):
    p = _write(tmp_path, "t9.md", '---\ninitiative: "null"\n---\n\n# Body\n')
    assert read_frontmatter_field(p, "initiative") == ""


def test_t10_explicitly_empty_double_quoted(tmp_path):
    p = _write(tmp_path, "t10.md", '---\ndeliverable_id: ""\n---\n\n# Body\n')
    assert read_frontmatter_field(p, "deliverable_id") == ""


def test_hash_with_no_preceding_space_still_strips(tmp_path):
    p = _write(tmp_path, "t11.md", "---\nworkstream: foo#bar\n---\n\n# Body\n")
    assert read_frontmatter_field(p, "workstream") == "foo"


def test_missing_file_path_argument():
    assert read_frontmatter_field("", "deliverable_id") == ""


def test_main_cli_entry_returns_zero_and_writes_stdout(tmp_path, capsys):
    p = _write(tmp_path, "t12.md", "---\ndeliverable_id: dlv-cli-1\n---\n")
    rc = main([p, "deliverable_id"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "dlv-cli-1"


def test_main_cli_entry_missing_args_returns_zero(capsys):
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
