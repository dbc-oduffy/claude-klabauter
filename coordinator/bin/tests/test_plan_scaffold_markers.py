import sys
from pathlib import Path

LIB = Path(__file__).resolve().parents[2] / "lib"
sys.path.insert(0, str(LIB))
import plan_scaffold_markers as m  # noqa: E402


def test_unfilled_scaffold_flagged():
    text = "---\n# scope:\n#   - path/to/file\n---\n| C1 | PLACEHOLDER |\nwrites `path/to/file/x.py`\n<REPLACE: x>\n"
    assert m.scan(text) == {"PLACEHOLDER": 5, "path/to/file": 6, "<REPLACE:": 7}


def test_commented_template_lines_and_clean_plan_pass():
    assert m.scan("---\n# author: <REPLACE: n>\n---\n## Spine\nreal rows\n") == {}


def test_cli_exit_codes(tmp_path, capsys):
    bad, good = tmp_path / "b.md", tmp_path / "g.md"
    bad.write_text("PLACEHOLDER", encoding="utf-8")
    good.write_text("ok", encoding="utf-8")
    assert m.main(["x", str(good)]) == 0
    assert m.main(["x", str(bad)]) == 3
    assert "PLAN-SCAFFOLD-UNFILLED" in capsys.readouterr().out


def test_placeholder_as_a_template_value_is_flagged():
    for line in ('title: "PLACEHOLDER — replace with x"', "  - PLACEHOLDER  # replace", "reviewer: PLACEHOLDER",
                 "# PLACEHOLDER — replace with x", "| C1 | PLACEHOLDER |", "PLACEHOLDER"):
        assert m.scan("---\nk: v\n---\n" + line + "\n") == {"PLACEHOLDER": 4}, line


def test_placeholder_named_in_prose_or_a_code_span_is_content():
    body = ("statement: 'without --name it keeps the PLACEHOLDER comment'\n"
            "- `sizing-assemble --write` still reads `# name: PLACEHOLDER`.\n")
    assert m.scan("---\nk: v\n---\n" + body) == {}


def test_template_paths_stay_flagged_inside_a_code_span():
    text = "---\nk: v\n---\nwrites `path/to/file/x.py` and `<REPLACE: y>`\n"
    assert m.scan(text) == {"path/to/file": 4, "<REPLACE:": 4}
