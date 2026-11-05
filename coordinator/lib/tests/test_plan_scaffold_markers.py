import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plan_scaffold_markers as psm  # noqa: E402


def test_whole_value_placeholder_is_flagged():
    assert psm.scan("title: PLACEHOLDER\n") == {"PLACEHOLDER": 1}
    assert psm.scan("# PLACEHOLDER\n") == {"PLACEHOLDER": 1}
    assert psm.scan("- PLACEHOLDER\n") == {"PLACEHOLDER": 1}


def test_prose_mention_and_code_span_are_not_flagged():
    assert psm.scan("keeps the PLACEHOLDER comment\n") == {}
    assert psm.scan("the value `PLACEHOLDER` is a template\n") == {}


def test_other_markers_flag_even_in_code_span():
    assert psm.scan("see `path/to/file`\n") == {"path/to/file": 1}
    assert psm.scan("<REPLACE: x>\n") == {"<REPLACE:": 1}


def test_frontmatter_comment_lines_skipped_body_hash_counts():
    text = "---\n# path/to/file doc\nk: v\n---\n# path/to/file\n"
    assert psm.scan(text) == {"path/to/file": 5}


def test_main_exit_codes(tmp_path, capsys):
    clean = tmp_path / "clean.md"
    clean.write_text("fine\n", encoding="utf-8")
    dirty = tmp_path / "dirty.md"
    dirty.write_text("title: PLACEHOLDER\n", encoding="utf-8")
    assert psm.main(["x", str(clean)]) == 0
    assert psm.main(["x", str(dirty)]) == 3
    assert "PLAN-SCAFFOLD-UNFILLED" in capsys.readouterr().out
    assert psm.main(["x", str(tmp_path / "missing.md")]) == 2
    assert psm.main(["x"]) == 2
