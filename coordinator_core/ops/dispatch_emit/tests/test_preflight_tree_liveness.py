"""Item 22 (part 1), docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md:
the preflight probe must report FAIL, not PASS, when the target tree is not
live (a missing root, or a root that exists but is not a git work tree).

Before this fix, `_preflight_agent_call` composed a tree-liveness check into
the dispatched agent's prompt ONLY `if repo_root` was truthy
(`_PREFLIGHT_CD_AND_ROOT_CHECK`). A preflight composed with no resolved
anchor -- `compose_script` called directly, or any caller that never
resolved a `plan_context` -- carried NO liveness check at all: the agent
went straight to judging pathspec claimability with no idea whether it was
even standing inside a live git work tree, and could report
`PREFLIGHT-CLEAR` regardless. `_PREFLIGHT_ROOT_CHECK_NO_ANCHOR` is the
sibling check for exactly that case -- same refusal token, no `-C {root}`.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import (
    _PREFLIGHT_BLOCKED_TOKEN,
    _preflight_agent_call,
)


def test_no_repo_root_still_carries_a_liveness_check():
    call = _preflight_agent_call(["a.py"], "Preflight: commit claimability")
    assert "git rev-parse --show-toplevel" in call
    assert "no live git work tree" in call
    assert _PREFLIGHT_BLOCKED_TOKEN in call


def test_no_repo_root_check_names_no_live_git_work_tree():
    call = _preflight_agent_call(["a.py"], "Preflight: commit claimability")
    assert "no live git work tree at the current directory" in call


def test_no_repo_root_never_emits_the_anchored_cd_form():
    call = _preflight_agent_call(["a.py"], "Preflight: commit claimability")
    assert "git -C " not in call


def test_a_repo_root_still_carries_the_anchored_liveness_check():
    call = _preflight_agent_call(
        ["a.py"], "Preflight: commit claimability", repo_root="/some/repo"
    )
    assert "git -C /some/repo rev-parse --show-toplevel" in call
    assert "PREFLIGHT-BLOCKED git root did not resolve from /some/repo" in call


def test_a_repo_root_composes_exactly_one_liveness_clause_not_both():
    call = _preflight_agent_call(
        ["a.py"], "Preflight: commit claimability", repo_root="/some/repo"
    )
    no_anchor_text = "no live git work tree at the current directory"
    assert no_anchor_text not in call
