"""test_sentinel_blocks_cli — pytest for coordinator_core.text.sentinel_blocks_cli.

Byte-parity target: coordinator/bin/lib/sentinel-blocks-cli.js (extract command,
usage/error contract). See sentinel_blocks_cli.py module docstring for the
"no coordinator-claude-side trampoline" disposition.
"""
from __future__ import annotations

from coordinator_core.text.sentinel_blocks_cli import main


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


BEGIN = "<!-- BEGIN alpha -->"
END = "<!-- END alpha -->"


def test_extract_prints_block_and_exits_0(tmp_path, capsys):
    path = _write(tmp_path, "f.md", f"before\n{BEGIN}\nfoo bar\n{END}\nafter\n")
    rc = main(["extract", path, BEGIN, END])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == "foo bar\n"


def test_extract_missing_markers_exits_1(tmp_path, capsys):
    path = _write(tmp_path, "f.md", "no markers here\n")
    rc = main(["extract", path, BEGIN, END])
    assert rc == 1
    err = capsys.readouterr().err
    assert "markers not found" in err


def test_extract_missing_file_exits_1(tmp_path, capsys):
    missing = str(tmp_path / "does-not-exist.md")
    rc = main(["extract", missing, BEGIN, END])
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot read file" in err


def test_no_command_exits_1_with_usage(capsys):
    rc = main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Usage:" in err


def test_unknown_command_exits_1(capsys):
    rc = main(["bogus"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown command: bogus" in err


def test_extract_missing_args_exits_1(capsys):
    rc = main(["extract", "onlyfile"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Usage:" in err


def test_extract_empty_block(tmp_path, capsys):
    path = _write(tmp_path, "f.md", f"{BEGIN}\n{END}\n")
    rc = main(["extract", path, BEGIN, END])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_usage_string_pins_retired_js_filename(capsys):
    # Review: code-reviewer — usage string hardcodes the retired
    # sentinel-blocks-cli.js filename deliberately, for byte-parity with the
    # JS CLI's own usage string. Pin the exact text so a future "modernize
    # away from .js" edit fails loud instead of silently drifting from the
    # parity contract.
    rc = main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert (
        "Usage: sentinel-blocks-cli.js extract <file> <begin-marker> <end-marker>"
        in err
    )
