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
