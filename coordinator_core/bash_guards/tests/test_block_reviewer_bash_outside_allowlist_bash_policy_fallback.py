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
        lambda git_root, agent_id: subagent_type,
    )


def _assert_denied(result):
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# AC11 -- lookup-miss / unreadable / malformed all fall back to the PRIOR
# hardcoded confinement, never to ALLOW. `git commit` is the pinned probe:
# it is on no read-only allowlist, hardcoded or policy-declared, in any of
# these scenarios, so a regression to fail-OPEN would flip this to allow.
# ---------------------------------------------------------------------------


def test_bash_policy_path_absent_still_denies_git_commit(tmp_path, monkeypatch):
    _confine(monkeypatch)
    missing_path = tmp_path / "does-not-exist.yaml"
    payload = _payload('git commit -m "x"')
    reason = _assert_denied(guard.check(payload, policy_path=str(missing_path)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_file_unreadable_still_denies_git_commit(tmp_path, monkeypatch):
    _confine(monkeypatch)
    # A directory at the policy path is unreadable as a file -- read_text()
    # raises IsADirectoryError (an OSError subclass), the same failure shape
    # engine.load_policy() catches for a genuinely permission-denied file.
    unreadable = tmp_path / "policy-is-a-directory"
    unreadable.mkdir()
    payload = _payload('git commit -m "x"')
    reason = _assert_denied(guard.check(payload, policy_path=str(unreadable)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_malformed_top_level_still_denies_git_commit(tmp_path, monkeypatch):
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    # Top-level YAML is a list, not a mapping -- engine.load_policy()
    # returns an empty Policy for any non-dict top-level document.
    policy_file.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    payload = _payload('git commit -m "x"')
    reason = _assert_denied(guard.check(payload, policy_path=str(policy_file)))
    assert "coordinator-doc-new" in reason


def test_bash_policy_entry_missing_keys_still_denies_git_commit(tmp_path, monkeypatch):
    # NOTE (review finding 3, 2026-07-27): this fixture is intentionally a
    # single-key dict, so it exercises _validate_ruleset's `except (KeyError,
    # TypeError)` MISSING-KEY path, not the isinstance/_is_str_list
    # type-validation branches -- those are covered separately below by the
    # *_still_denies_git_commit/_unlisted_command tests fed by
    # _well_formed_ruleset_with_override, which supply a fixture that is
    # otherwise complete and well-formed but carries exactly one type
    # violation. (Renamed from
    # test_bash_policy_malformed_entry_value_still_denies_git_commit, whose
    # old name implied it probed value-type validation; it never reached
    # that code.)
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    # bash_policy: is a dict, and carries a dict-valued entry for the
    # confined type (survives engine.load_policy()'s non-dict-value filter)
    # -- but the entry itself is missing every required ruleset key.
    # _validate_ruleset must reject this and _resolve_ruleset must fall back
    # to _default_ruleset(), not raise, and not silently drop enforcement.
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
    # Same missing-key fixture shape as above (see that test's NOTE), but
    # probing the Tier B scaffolder path (an arbitrary non-allowlisted
    # command) instead of the git Tier A path -- confirms the fallback
    # ruleset governs BOTH tiers, not just the git-specific one. This
    # fixture also short-circuits on the KeyError path, not the
    # isinstance/_is_str_list branches -- see
    # test_bash_policy_entry_missing_keys_still_denies_git_commit's NOTE.
    # (Renamed from test_bash_policy_malformed_entry_still_denies_unlisted_command.)
    _confine(monkeypatch)
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "bash_policy:\n"
        f"  {_CONFINED_TYPE}:\n"
        "    scaffolder_binary: 123\n",  # wrong type -- must be a non-empty str
        encoding="utf-8",
    )
    payload = _payload("rm -rf /")
    reason = _assert_denied(guard.check(payload, policy_path=str(policy_file)))
    assert "coordinator-doc-new" in reason


# ---------------------------------------------------------------------------
# AC11, type-validation branches (review finding 3, 2026-07-27): the two
# tests above both short-circuit on _validate_ruleset's missing-key
# `except (KeyError, TypeError)` leg, never reaching the isinstance/
# _is_str_list checks. Each fixture below is otherwise COMPLETE and
# well-formed -- every required key present, every other value valid -- but
# carries exactly ONE type violation, so it can only be caught by the
# isinstance/_is_str_list branches these tests exist to pin. A regression
# that accidentally widened one of those checks (e.g. dropped the per-item
# str check on a list, or the isinstance(..., dict) check on
# git_global_options) would not be caught by the missing-key tests above,
# but must be caught here.
# ---------------------------------------------------------------------------

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
    # Otherwise-well-formed entry; git_readonly_subcommands carries one
    # non-str element (an int) inside an otherwise-valid str list --
    # _is_str_list must reject this per-element, not just check "is a list".
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
    # Otherwise-well-formed entry; scaffolder_binary is a str (passes
    # isinstance) but empty -- the `and scaffolder_binary` truthiness check
    # must reject it, not just the isinstance check.
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
    # Otherwise-well-formed entry; git_global_options is a plain string
    # instead of the required {value_taking: [...], no_value: [...]}
    # mapping -- the `isinstance(git_global, dict)` guard must reject this
    # before ever calling .get() on it.
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


# ---------------------------------------------------------------------------
# Divergence 14 (2026-08-10): the enforced ruleset is now hard-pinned in
# code (`_default_ruleset()`/`_DEFAULT_RULESET_TYPE_OVERRIDES`) -- a
# well-formed `bash_policy:` YAML entry for a confined type's RULESET no
# longer changes the ALLOW/DENY decision at all, in EITHER direction. This
# is the fix for the confinement-editable-by-its-own-subject defect (a
# confined agent's own Edit tool could rewrite this YAML and the very next
# Bash call honoured the rewrite) -- these two tests used to prove the
# opposite (that the policy genuinely widened/narrowed the surface); they
# now pin that a YAML ruleset entry is INERT for enforcement, which is the
# whole point of the fix. See _resolve_ruleset's own comment.
# ---------------------------------------------------------------------------


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

    # Without an injected policy path, the hardcoded fallback governs --
    # "sed" is not in _READONLY_FS_BINARIES, so this denies.
    assert _assert_denied(guard.check(payload)) is not None

    # A YAML policy that adds "sed" to readonly_fs_binaries: no longer has
    # any effect -- the ruleset is code-pinned now (Divergence 14), so the
    # SAME command still denies even with that entry present.
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

    # Hardcoded fallback: "log" is a read-only subcommand -- allowed.
    assert guard.check(payload) is None

    # A YAML policy whose git_readonly_subcommands omits "log" (only "show"
    # is declared) no longer narrows the surface (Divergence 14) -- the
    # SAME command still allows, because the ruleset is code-pinned and this
    # YAML entry is never consulted for enforcement.
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(_well_formed_policy_yaml(["ls"]), encoding="utf-8")
    result = guard.check(payload, policy_path=str(policy_file))
    assert result is None


# ---------------------------------------------------------------------------
# Regression, updated for Divergence 14 (2026-08-10): a pre-existing
# ``bash_policy:`` YAML entry for a confined type -- authored before
# ``_DEFAULT_RULESET_TYPE_OVERRIDES`` grew an entry for that same type --
# has never been able to shadow the Python-side override, and as of
# Divergence 14 this is unconditionally true: the YAML ruleset entry is
# never consulted for enforcement at all (see ``_resolve_ruleset``'s own
# comment), so there is nothing left for it to shadow. This test now pins
# that outcome directly rather than via the original green-tests-inert-
# production layering story (retained in the comment below for history).
# ---------------------------------------------------------------------------


def test_preexisting_policy_entry_does_not_shadow_newer_interpreter_override(
    tmp_path, monkeypatch
):
    _confine(monkeypatch)
    payload = _payload("python3 -m pytest -q")

    # A well-formed bash_policy: entry for coordinator:code-reviewer that
    # carries NEITHER interpreter_allowed_modules NOR
    # interpreter_allow_scripts -- exactly what a policy row authored before
    # the pytest grant existed looks like (both keys are OPTIONAL per
    # _validate_ruleset, defaulting to the conservative deny-more value so
    # an already-deployed row does not fail validation). Under Divergence 14
    # this entry is never consulted for enforcement at all, so its presence
    # is inert either way -- the pytest allowance comes exclusively from
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

    # The rest of that pre-existing policy row's OWN declared surface is now
    # INERT for enforcement (Divergence 14) -- "log" IS in the hardcoded
    # fallback's git_readonly_subcommands regardless of what this fixture's
    # YAML declares, so it allows, not denies, unlike the pre-Divergence-14
    # version of this test.
    other_payload = _payload("git log")
    other_result = guard.check(other_payload, policy_path=str(policy_file))
    assert other_result is None

    # And the unconditional python3 -c/-e inline-code deny is untouched by
    # this layering -- it is never gated by ruleset content at all.
    inline_payload = _payload('python3 -c "import os"')
    inline_result = guard.check(inline_payload, policy_path=str(policy_file))
    assert inline_result is not None
    assert inline_result["hookSpecificOutput"]["permissionDecision"] == "deny"
