"""codex_family_stage_argv: the codex family-stage write argv runs unrestricted and keeps the --json stream."""
from __future__ import annotations

from coordinator_core.codex_runner.resolve import codex_family_stage_argv

BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def test_family_stage_argv_carries_bypass_and_no_sandbox_flag() -> None:
    argv = codex_family_stage_argv(["node", "codex.js"], "--ephemeral", "-o", "last.txt", "-")
    assert argv == ["node", "codex.js", "exec", "--json", BYPASS, "--ephemeral", "-o", "last.txt", "-"]
    assert "--sandbox" not in argv and "workspace-write" not in argv and "read-only" not in argv


def test_family_stage_argv_with_no_tail_is_the_default_shape() -> None:
    assert codex_family_stage_argv(["codex"]) == ["codex", "exec", "--json", BYPASS]
