"""test_stub_file_gate — pytest tests for coordinator/bin/stub-file-gate.py.

Spec backlink: scratchpad/scout-D-claude-klabauter-sizing.md § Item 7a (deep-research
GATE_FAIL block, repo-driver.md:338).

Coverage:
    test_all_pass_exits_zero
    test_missing_file_exits_one
    test_short_file_exits_one
    test_mixed_pass_and_fail_exits_one_reports_both
    test_min_lines_negative_exits_two
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "stub_file_gate",
        _BIN_DIR / "stub-file-gate.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def test_all_pass_exits_zero(tmp_path, capsys):
    f = tmp_path / "long.txt"
    f.write_text("\n".join(str(i) for i in range(30)) + "\n")

    rc = _mod.main(["--min-lines", "30", str(f)])

    assert rc == 0
    captured = capsys.readouterr()
    assert "GATE PASS" in captured.out


def test_missing_file_exits_one(tmp_path, capsys):
    rc = _mod.main(["--min-lines", "5", str(tmp_path / "nope.txt")])

    assert rc == 1
    captured = capsys.readouterr()
    assert "GATE FAIL" in captured.out
    assert "does not exist" in captured.out


def test_short_file_exits_one(tmp_path, capsys):
    f = tmp_path / "short.txt"
    f.write_text("a\nb\nc\n")

    rc = _mod.main(["--min-lines", "30", str(f)])

    assert rc == 1
    captured = capsys.readouterr()
    assert "GATE FAIL" in captured.out
    assert "has 3 line(s)" in captured.out


def test_mixed_pass_and_fail_exits_one_reports_both(tmp_path, capsys):
    good = tmp_path / "good.txt"
    good.write_text("\n".join(str(i) for i in range(10)) + "\n")
    bad = tmp_path / "bad.txt"
    bad.write_text("only one line\n")

    rc = _mod.main(["--min-lines", "5", str(good), str(bad)])

    assert rc == 1
    captured = capsys.readouterr()
    assert "GATE PASS: " + str(good) in captured.out
    assert "GATE FAIL: " + str(bad) in captured.out


def test_min_lines_negative_exits_two(tmp_path, capsys):
    f = tmp_path / "x.txt"
    f.write_text("a\n")

    rc = _mod.main(["--min-lines", "-1", str(f)])

    assert rc == 2
