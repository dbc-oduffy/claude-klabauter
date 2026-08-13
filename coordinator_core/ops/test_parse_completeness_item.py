"""
Tests for coordinator_core.ops.parse_completeness_item.

Mirrors coordinator/tests/completeness-checklist.bats (coordinator-claude) case-for-case,
plus additional coverage for the stdin-truncation oracle quirk (module docstring
"Oracle quirk" note) and the CLI main() exit-code/output contract.
"""

from __future__ import annotations

import io

import pytest

from coordinator_core.ops.parse_completeness_item import main, parse_completeness_item


def test_live_no_probe():
    item_class, assertion, probe = parse_completeness_item(
        "live: claude mcp list shows example-game-repo-native-mcp connected"
    )
    assert item_class == "live"
    assert assertion == "claude mcp list shows example-game-repo-native-mcp connected"
    assert probe == ""


def test_restart_gated_with_probe():
    item_class, assertion, probe = parse_completeness_item(
        "restart-gated: example-game-repo MCP connects after restart "
        "[probe: claude mcp list | grep -q example-game-repo-native-mcp]"
    )
    assert item_class == "restart-gated"
    assert assertion == "example-game-repo MCP connects after restart"
    assert probe == "claude mcp list | grep -q example-game-repo-native-mcp"


def test_unknown_class_malformed():
    with pytest.raises(Exception):
        parse_completeness_item("pending: coordinator plugin enabled in settings.json")


def test_missing_class_prefix_malformed():
    with pytest.raises(Exception):
        parse_completeness_item("coordinator plugin enabled in settings.json")


def test_unterminated_probe_malformed():
    with pytest.raises(Exception):
        parse_completeness_item(
            "live: coordinator running [probe: claude mcp list | grep -q coordinator"
        )


def test_escaped_bracket_in_probe():
    item_class, assertion, probe = parse_completeness_item(
        r"live: check jq output [probe: jq '.[0\]' /tmp/data.json]"
    )
    assert item_class == "live"
    assert assertion == "check jq output"
    assert probe == "jq '.[0]' /tmp/data.json"


def test_classification_for_hoist():
    c1, _, _ = parse_completeness_item("live: coordinator hooks are registered")
    c2, _, _ = parse_completeness_item(
        "live: skills discovery preconditions met "
        "[probe: grep -q description skills/pickup/SKILL.md]"
    )
    c3, _, _ = parse_completeness_item(
        "restart-gated: example-game-repo MCP connects [probe: claude mcp list | grep example-game-repo]"
    )
    c4, _, _ = parse_completeness_item("restart-gated: LSP server starts on project open")
    assert c1 == c2 == "live"
    assert c3 == c4 == "restart-gated"
    assert c1 != c3


def test_trailing_whitespace_stripped():
    item_class, assertion, probe = parse_completeness_item("live: coordinator running   ")
    assert item_class == "live"
    assert assertion == "coordinator running"
    assert probe == ""


def test_empty_probe_command_malformed():
    with pytest.raises(Exception):
        parse_completeness_item("live: some assertion [probe: ]")


def test_bare_class_colon_malformed():
    with pytest.raises(Exception):
        parse_completeness_item("live:")


# --- main() CLI contract: argv path ----------------------------------------


def test_main_argv_live_no_probe(capsys):
    rc = main(["live: x"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "class=live\nassertion=x\nprobe=\n"


def test_main_argv_malformed_exit_1(capsys):
    rc = main(["bogus"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "parse-completeness-item: malformed item:" in captured.err


# --- main() CLI contract: stdin path ----------------------------------------


def test_main_stdin_input(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("live: x\n"))
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "class=live\nassertion=x\nprobe=\n"


def test_main_stdin_multiline_malformed(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("live: x\nlive: y\n"))
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "multi-line input not supported" in captured.err


def test_main_stdin_blank_lines_skipped(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n\nlive: x\n\n"))
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "class=live\nassertion=x\nprobe=\n"


def test_main_stdin_unterminated_final_line_dropped(monkeypatch, capsys):
    """Oracle quirk (module docstring): a stdin payload with no trailing "\\n"
    is dropped entirely by the `read` loop -- yields an empty item, which then
    surfaces as the ordinary "empty input" malformed error. Regression test
    for the read-loop-truncation edge the bash oracle silently exhibits."""
    monkeypatch.setattr("sys.stdin", io.StringIO("live: no trailing newline test"))
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "empty input" in captured.err


def test_main_stdin_empty_input_malformed(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "empty input" in captured.err
