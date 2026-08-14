"""Tests for coordinator_core.ops.session_hierarchy_query.

Port of: query-session-hierarchy.sh (DoE b5a4192c, 2026-07-20).
"""
from __future__ import annotations

import json
import os

import pytest

from coordinator_core.ops.session_hierarchy_query import main

RECORDS = [
    {
        "session_id": "aaaa0000-0000-0000-0000-000000000001",
        "session_type": "session",
        "workstream": "test-ws-a",
        "branch": "work/test/2026-01-01",
        "parent_session_id": None,
    },
    {
        "session_id": "aaaa0000-0000-0000-0000-000000000002",
        "session_type": "session",
        "workstream": "test-ws-a",
        "branch": "work/test/2026-01-02",
        "parent_session_id": "aaaa0000-0000-0000-0000-000000000001",
    },
    {
        # Synthetic workstream-container node — must be excluded from --workstream results.
        "session_id": "workstream:test-ws-a",
        "session_type": "workstream",
        "workstream": "test-ws-a",
        "branch": None,
        "parent_session_id": None,
    },
    {
        "session_id": "bbbb0000-0000-0000-0000-000000000001",
        "session_type": "session",
        "workstream": "test-ws-b",
        "branch": "work/test/2026-01-01",
        "parent_session_id": None,
    },
]


@pytest.fixture()
def shard_dir(tmp_path, monkeypatch):
    shard_file = tmp_path / "session-hierarchy.testmachine.json"
    shard_file.write_text(json.dumps(RECORDS), encoding="utf-8")
    monkeypatch.setenv("QUERY_SHARD_DIR", str(tmp_path))
    return tmp_path


def test_help_flag_prints_usage_exit_0(capsys):
    rc = main(["--help"])
    assert rc == 0
    assert "USAGE" in capsys.readouterr().out


def test_no_args_prints_usage_exit_0(capsys):
    rc = main([])
    assert rc == 0
    assert "USAGE" in capsys.readouterr().out


def test_unknown_flag_exit_2(capsys):
    rc = main(["--bogus", "foo"])
    assert rc == 2
    assert "ERROR: unknown argument: --bogus" in capsys.readouterr().err


def test_workstream_missing_arg_exit_2(capsys):
    rc = main(["--workstream"])
    assert rc == 2
    assert "ERROR: --workstream requires a slug argument" in capsys.readouterr().err


def test_session_missing_arg_exit_2(capsys):
    rc = main(["--session"])
    assert rc == 2
    assert "ERROR: --session requires a session_id argument" in capsys.readouterr().err


def test_workstream_query_excludes_container_node(shard_dir, capsys):
    rc = main(["--workstream", "test-ws-a"])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == [
        "aaaa0000-0000-0000-0000-000000000001",
        "aaaa0000-0000-0000-0000-000000000002",
    ]


def test_workstream_query_no_match_empty_output_exit_0(shard_dir, capsys):
    rc = main(["--workstream", "no-such-workstream"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_session_query_found(shard_dir, capsys):
    rc = main(["--session", "aaaa0000-0000-0000-0000-000000000002"])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "session_id": "aaaa0000-0000-0000-0000-000000000002",
        "session_type": "session",
        "workstream": "test-ws-a",
        "branch": "work/test/2026-01-02",
        "parent_session_id": "aaaa0000-0000-0000-0000-000000000001",
    }


def test_session_query_not_found_exit_1(shard_dir, capsys):
    rc = main(["--session", "does-not-exist"])
    assert rc == 1
    assert "ERROR: session_id not found in any shard: does-not-exist" in capsys.readouterr().err


def test_no_shards_found_exit_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QUERY_SHARD_DIR", str(tmp_path))
    rc = main(["--workstream", "anything"])
    assert rc == 1
    assert "ERROR: no session-hierarchy shards found" in capsys.readouterr().err


def test_union_across_multiple_shards(tmp_path, monkeypatch, capsys):
    (tmp_path / "session-hierarchy.machine-a.json").write_text(
        json.dumps([RECORDS[0]]), encoding="utf-8"
    )
    (tmp_path / "session-hierarchy.machine-b.json").write_text(
        json.dumps([RECORDS[1]]), encoding="utf-8"
    )
    monkeypatch.setenv("QUERY_SHARD_DIR", str(tmp_path))
    rc = main(["--workstream", "test-ws-a"])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == [
        "aaaa0000-0000-0000-0000-000000000001",
        "aaaa0000-0000-0000-0000-000000000002",
    ]


def test_argv_beyond_first_two_ignored(shard_dir, capsys):
    """Faithful reproduction of the bash oracle's arg-parsing quirk: only
    argv[0]/argv[1] are consulted (a `case "${1:-}"` switch) — trailing
    extras are silently ignored, not rejected."""
    rc = main(["--workstream", "test-ws-a", "extra", "args", "ignored"])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == [
        "aaaa0000-0000-0000-0000-000000000001",
        "aaaa0000-0000-0000-0000-000000000002",
    ]


def test_malformed_shard_json_exit_1(tmp_path, monkeypatch, capsys):
    (tmp_path / "session-hierarchy.bad.json").write_text("not json", encoding="utf-8")
    monkeypatch.setenv("QUERY_SHARD_DIR", str(tmp_path))
    rc = main(["--workstream", "anything"])
    assert rc == 1
    assert "ERROR: failed to read session-hierarchy shard(s)" in capsys.readouterr().err
