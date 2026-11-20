"""Family-stage codex argv: the unrestricted builder keeps its pin; the scoped builder never bypasses."""
from __future__ import annotations

import json

import pytest

from coordinator_core.codex_runner.resolve import codex_family_stage_argv, codex_scoped_stage_argv

BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def test_family_stage_argv_carries_bypass_and_no_sandbox_flag() -> None:
    argv = codex_family_stage_argv(["node", "codex.js"], "--ephemeral", "-o", "last.txt", "-")
    assert argv == ["node", "codex.js", "exec", "--json", BYPASS, "--ephemeral", "-o", "last.txt", "-"]
    assert "--sandbox" not in argv and "workspace-write" not in argv and "read-only" not in argv


def test_family_stage_argv_with_no_tail_is_the_default_shape() -> None:
    assert codex_family_stage_argv(["codex"]) == ["codex", "exec", "--json", BYPASS]


def test_scoped_argv_is_workspace_write_with_exactly_the_given_roots() -> None:
    roots = ["/work tree", "/work tree/scratch/codex-tmp"]
    argv = codex_scoped_stage_argv(["node", "codex.js"], roots, "--ephemeral", "-")
    assert argv[:7] == ["node", "codex.js", "exec", "--json", "--sandbox", "workspace-write", "-c"]
    assert argv[7] == "sandbox_workspace_write.writable_roots=" + json.dumps(roots)
    assert argv[8:] == ["-c", "project_doc_max_bytes=0", "--ephemeral", "-"]
    assert BYPASS not in argv


def test_scoped_argv_roots_round_trip_as_a_toml_string_array() -> None:
    tomllib = pytest.importorskip("tomllib")
    roots = ["a\\b", "/x/y z"]
    value = codex_scoped_stage_argv(["codex"], roots)[6].split("=", 1)[1]
    assert tomllib.loads(f"r = {value}")["r"] == roots


def test_scoped_argv_refuses_no_roots() -> None:
    with pytest.raises(ValueError):
        codex_scoped_stage_argv(["codex"], [])
