"""AC11 dedicated fallback tests for
coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist's
Divergence-7 (2026-07-27) ``bash_policy``-table refactor.

Companion to ``test_block_reviewer_bash_outside_allowlist.py`` (the AC5
oracle, left untouched by this change -- see that suite and the guard
module's own Divergence 7 docstring section). This file is scoped narrowly
to the ONE property AC11 requires structurally verified, separate from the
oracle: a ``bash_policy`` lookup-miss, an unreadable policy file, or a
malformed per-``effective_type`` policy value must degrade to the PRIOR
hardcoded enforcement (the allowlist constants + ``_helpers.
_CONFINED_FINDINGS_AGENTS``) -- NEVER to ALLOW. Fail-open is correct for an
eligibility lookup like ``report_sidecar`` (a miss provisions nothing,
harmless); it is the wrong default for this guard, a deny-guard, where a
miss must degrade to the *prior* enforcement, not to permissiveness.

The last two tests additionally prove the refactor is not decorative: a
well-formed policy genuinely widens/narrows the allowed surface relative to
the hardcoded fallback, both directions (a command the hardcoded fallback
would deny is allowed under a policy that adds it; the SAME command still
denies when that policy is unavailable).

Spec backlink: coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py
  § Divergence 7 (AC11 fail-open inversion)
docs/plans/2026-07-27-structural-policy-enforcement.md § C6, AC11
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import yaml

from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as guard,
)

_CONFINED_TYPE = "coordinator:code-reviewer"


def _payload(command: str, agent_id: str = "deadbeef0123", agent_type: str = _CONFINED_TYPE) -> Dict[str, Any]:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": None,
        "agent_id": agent_id,
        "agent_type": agent_type,
    }


def _confine(monkeypatch, subagent_type: str = _CONFINED_TYPE) -> None:
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id, **kw: subagent_type,
    )


def _assert_denied(result):
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_policy_path_absent_still_denies_git_commit(tmp_path, monkeypatch):
    _confine(monkeypatch)
    missing_path = tmp_path / "does-not-exist.yaml"
    payload = _payload('git commit -m "x"')
    reason = _assert_denied(guard.check(payload, policy_path=str(missing_path)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_file_unreadable_still_denies_git_commit(tmp_path, monkeypatch):
    _confine(monkeypatch)
    unreadable = tmp_path / "policy-is-a-directory"
    unreadable.mkdir()
    payload = _payload('git commit -m "x"')
    reason = _assert_denied(guard.check(payload, policy_path=str(unreadable)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_malformed_top_level_still_denies_git_commit(tmp_path, monkeypatch):
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    payload = _payload('git commit -m "x"')
    reason = _assert_denied(guard.check(payload, policy_path=str(policy_file)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_entry_missing_keys_still_denies_git_commit(tmp_path, monkeypatch):
    # TypeError)` MISSING-KEY path, not the isinstance/_is_str_list
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "bash_policy:\n"
        f"  {_CONFINED_TYPE}:\n"
        "    git_readonly_subcommands: not-a-list\n",
        encoding="utf-8",
    )
    payload = _payload('git commit -m "x"')
    reason = _assert_denied(guard.check(payload, policy_path=str(policy_file)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_entry_missing_keys_still_denies_unlisted_command(tmp_path, monkeypatch):
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "bash_policy:\n"
        f"  {_CONFINED_TYPE}:\n"
        "    scaffolder_binary: 123\n",
        encoding="utf-8",
    )
    payload = _payload("rm -rf /")
    reason = _assert_denied(guard.check(payload, policy_path=str(policy_file)))
    assert "coordinator-doc-new" in reason


# _is_str_list checks. Each fixture below is otherwise COMPLETE and

_WELL_FORMED_RULESET: Dict[str, Any] = {
    "git_readonly_subcommands": ["show", "log"],
    "git_global_options": {"value_taking": [], "no_value": []},
    "git_subcommand_denied_options": ["--output"],
    "readonly_fs_binaries": ["ls", "cat"],
    "find_denied_options": [],
    "scaffolder_binary": "coordinator-doc-new",
    "scaffolder_required_arg": "--type review-findings",
}


def _well_formed_ruleset_with_override(**overrides: Any) -> str:
    """Render a COMPLETE, otherwise-well-formed ``bash_policy:`` entry for
    ``_CONFINED_TYPE``, with exactly the keys in ``overrides`` replaced by a
    type-invalid value. Every key not named in ``overrides`` keeps its
    well-formed default from ``_WELL_FORMED_RULESET`` -- the point is
    isolating ONE type violation per fixture, per review finding 3."""
    ruleset = copy.deepcopy(_WELL_FORMED_RULESET)
    ruleset.update(overrides)
    return yaml.safe_dump({"bash_policy": {_CONFINED_TYPE: ruleset}}, sort_keys=False)


def test_bash_policy_non_str_list_element_still_denies_git_commit(tmp_path, monkeypatch):
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        _well_formed_ruleset_with_override(git_readonly_subcommands=["show", 123]),
        encoding="utf-8",
    )
    payload = _payload('git commit -m "x"')
    reason = _assert_denied(guard.check(payload, policy_path=str(policy_file)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_non_str_list_element_still_denies_unlisted_command(tmp_path, monkeypatch):
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        _well_formed_ruleset_with_override(readonly_fs_binaries=["ls", 123]),
        encoding="utf-8",
    )
    payload = _payload("rm -rf /")
    reason = _assert_denied(guard.check(payload, policy_path=str(policy_file)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_empty_scaffolder_field_still_denies_git_commit(tmp_path, monkeypatch):
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        _well_formed_ruleset_with_override(scaffolder_binary=""),
        encoding="utf-8",
    )
    payload = _payload('git commit -m "x"')
    reason = _assert_denied(guard.check(payload, policy_path=str(policy_file)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_empty_scaffolder_field_still_denies_unlisted_command(tmp_path, monkeypatch):
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        _well_formed_ruleset_with_override(scaffolder_required_arg=""),
        encoding="utf-8",
    )
    payload = _payload("rm -rf /")
    reason = _assert_denied(guard.check(payload, policy_path=str(policy_file)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_non_dict_git_global_options_still_denies_git_commit(tmp_path, monkeypatch):
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        _well_formed_ruleset_with_override(git_global_options="not-a-mapping"),
        encoding="utf-8",
    )
    payload = _payload('git commit -m "x"')
    reason = _assert_denied(guard.check(payload, policy_path=str(policy_file)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_non_dict_git_global_options_still_denies_unlisted_command(tmp_path, monkeypatch):
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        _well_formed_ruleset_with_override(git_global_options=["not", "a", "mapping"]),
        encoding="utf-8",
    )
    payload = _payload("rm -rf /")
    reason = _assert_denied(guard.check(payload, policy_path=str(policy_file)))
    assert "coordinator-doc-new" in reason


# code (`_default_ruleset()`/`_DEFAULT_RULESET_TYPE_OVERRIDES`) -- a


def _well_formed_policy_yaml(readonly_fs_binaries) -> str:
    binaries = "\n".join(f"      - {b}" for b in readonly_fs_binaries)
    return (
        "bash_policy:\n"
        f"  {_CONFINED_TYPE}:\n"
        "    git_readonly_subcommands:\n"
        "      - show\n"
        "    git_global_options:\n"
        "      value_taking: []\n"
        "      no_value: []\n"
        "    git_subcommand_denied_options:\n"
        "      - \"--output\"\n"
        "    readonly_fs_binaries:\n"
        f"{binaries}\n"
        "    find_denied_options: []\n"
        "    scaffolder_binary: coordinator-doc-new\n"
        "    scaffolder_required_arg: \"--type review-findings\"\n"
    )


def test_well_formed_policy_ruleset_does_not_grant_a_binary_the_hardcoded_fallback_denies(
    tmp_path, monkeypatch
):
    _confine(monkeypatch)
    payload = _payload("sed -n 1p some-file.txt")

    # "sed" is not in _READONLY_FS_BINARIES, so this denies.
    assert _assert_denied(guard.check(payload)) is not None

    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        _well_formed_policy_yaml(["ls", "sed"]), encoding="utf-8"
    )
    result = guard.check(payload, policy_path=str(policy_file))
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_well_formed_policy_ruleset_does_not_narrow_git_subcommands_relative_to_fallback(
    tmp_path, monkeypatch
):
    _confine(monkeypatch)
    payload = _payload("git log")

    assert guard.check(payload) is None

    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(_well_formed_policy_yaml(["ls"]), encoding="utf-8")
    result = guard.check(payload, policy_path=str(policy_file))
    assert result is None


# ``_DEFAULT_RULESET_TYPE_OVERRIDES`` grew an entry for that same type --


def test_preexisting_policy_entry_does_not_shadow_newer_interpreter_override(
    tmp_path, monkeypatch
):
    _confine(monkeypatch)
    payload = _payload("python3 -m pytest -q")

    # the pytest grant existed looks like (both keys are OPTIONAL per
    # _DEFAULT_RULESET_TYPE_OVERRIDES via _default_ruleset() now.
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(_well_formed_policy_yaml(["ls"]), encoding="utf-8")

    result = guard.check(payload, policy_path=str(policy_file))
    assert result is None, (
        "coordinator:code-reviewer's pytest allowance "
        "(_DEFAULT_RULESET_TYPE_OVERRIDES) must survive even when a "
        "pre-existing bash_policy: YAML row for this exact type is present "
        "and validates -- that row predates the grant and does not know "
        "about it; it must not silently shadow it."
    )

    other_payload = _payload("git log")
    other_result = guard.check(other_payload, policy_path=str(policy_file))
    assert other_result is None

    inline_payload = _payload('python3 -c "import os"')
    inline_result = guard.check(inline_payload, policy_path=str(policy_file))
    assert inline_result is not None
    assert inline_result["hookSpecificOutput"]["permissionDecision"] == "deny"
