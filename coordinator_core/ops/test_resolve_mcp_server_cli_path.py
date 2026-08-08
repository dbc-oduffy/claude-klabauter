"""Tests for coordinator_core.ops.resolve_mcp_server_cli_path
(op mcp.resolve_server_cli_path).

Covers success resolution (cli_path/project_root extraction from a
`mcpServers.<name>.args` list), each failure mode (missing file, malformed
JSON, missing server, missing/empty args, no .py/cli-suffixed arg), the
async register_op handler contract, and the CC-4/AC7 double-invocation
idempotency proof (pure read-only parse — a second call with identical
inputs is a no-op that yields byte-identical output).
"""
from __future__ import annotations

import json

from coordinator_core.ops.resolve_mcp_server_cli_path import (
    _handler,
    resolve_cli_path_and_root,
)


def _write_claude_json(tmp_path, servers: dict):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def test_resolves_cli_path_and_project_root(tmp_path):
    path = _write_claude_json(
        tmp_path,
        {
            "example-retrieval-repo": {
                "command": "python3",
                "args": ["-u", "/opt/example-retrieval-repo/server.py", "/repos/myproject"],
            }
        },
    )

    result = resolve_cli_path_and_root(path, "example-retrieval-repo")

    assert result == {
        "cli_path": "/opt/example-retrieval-repo/server.py",
        "project_root": "/repos/myproject",
    }


def test_resolves_cli_suffixed_arg_without_py_extension(tmp_path):
    path = _write_claude_json(
        tmp_path,
        {"my-server": {"args": ["/usr/local/bin/my-server-cli", "/repos/other"]}},
    )

    result = resolve_cli_path_and_root(path, "my-server")

    assert result == {
        "cli_path": "/usr/local/bin/my-server-cli",
        "project_root": "/repos/other",
    }


def test_missing_claude_json_file_reports_error(tmp_path):
    result = resolve_cli_path_and_root(tmp_path / "nope" / ".claude.json", "example-retrieval-repo")

    assert result["cli_path"] is None
    assert result["project_root"] is None
    assert "not found" in result["error"]


def test_malformed_json_reports_error(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text("{not valid json", encoding="utf-8")

    result = resolve_cli_path_and_root(path, "example-retrieval-repo")

    assert result["cli_path"] is None
    assert "error" in result


def test_missing_server_reports_error(tmp_path):
    path = _write_claude_json(tmp_path, {"other-server": {"args": ["/a.py", "/b"]}})

    result = resolve_cli_path_and_root(path, "example-retrieval-repo")

    assert result["cli_path"] is None
    assert "mcpServers" in result["error"]


def test_missing_args_reports_error(tmp_path):
    path = _write_claude_json(tmp_path, {"example-retrieval-repo": {"command": "python3"}})

    result = resolve_cli_path_and_root(path, "example-retrieval-repo")

    assert result["cli_path"] is None
    assert "args" in result["error"]


def test_empty_args_reports_error(tmp_path):
    path = _write_claude_json(tmp_path, {"example-retrieval-repo": {"args": []}})

    result = resolve_cli_path_and_root(path, "example-retrieval-repo")

    assert result["cli_path"] is None
    assert "args" in result["error"]


def test_no_py_or_cli_suffixed_arg_reports_error(tmp_path):
    path = _write_claude_json(tmp_path, {"example-retrieval-repo": {"args": ["--flag", "/repos/x"]}})

    result = resolve_cli_path_and_root(path, "example-retrieval-repo")

    assert result["cli_path"] is None
    assert result["project_root"] is None
    assert "error" in result


def test_handler_success_via_claude_json_path_override(tmp_path):
    path = _write_claude_json(
        tmp_path,
        {"example-retrieval-repo": {"args": ["/opt/pr/cli.py", "/repos/x"]}},
    )

    result = _handler({"server_name": "example-retrieval-repo", "claude_json_path": str(path)})

    assert result == {"cli_path": "/opt/pr/cli.py", "project_root": "/repos/x"}


def test_handler_missing_server_name_is_a_usage_error():
    result = _handler({})

    assert result["cli_path"] is None
    assert "server_name" in result["error"]


def test_double_invocation_is_idempotent_no_op(tmp_path):
    path = _write_claude_json(
        tmp_path,
        {"example-retrieval-repo": {"args": ["/opt/pr/cli.py", "/repos/x"]}},
    )
    params = {"server_name": "example-retrieval-repo", "claude_json_path": str(path)}

    first = _handler(params)
    second = _handler(params)

    assert first == second == {"cli_path": "/opt/pr/cli.py", "project_root": "/repos/x"}
