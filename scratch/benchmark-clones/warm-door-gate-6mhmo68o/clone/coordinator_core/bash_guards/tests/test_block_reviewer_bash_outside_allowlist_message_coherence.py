"""Deny-text/ruleset coherence tests for
coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist.

The property under test: every allowlist enumeration the guard PRINTS to a
denied findings-agent is derived-equivalent to the ruleset the guard actually
ENFORCES. The deny text is prose, hand-maintained alongside the constants it
describes, and nothing structurally tied the two together -- so the message
could (and did) describe a rule the code did not implement.

Why this suite exists rather than being folded into the AC5 oracle: the
oracle pins deny-reason strings byte-identical, which catches an *unintended*
message edit but is silent on the inverse and more dangerous direction -- a
ruleset constant gaining or losing a member while the message keeps its old
enumeration. A byte-pin passes happily through that drift; a set-equality
assertion against the constant does not. Empirical trigger: the 2026-07-28
Divergence 8 false positives (unquoted `|` and `2>/dev/null` denied) went
unreported for days partly because the deny message described a rule the code
did not implement, so reviewers reading it had no reason to suspect the guard
rather than their own command (DoE-claude cross-repo memo
``2026-07-28-doe-claude-em-reviewer-bash-guard-pipeline-carveout.md``, which
landed the carve-outs and flagged this residual back to this repo).

Scope note -- coherence is asserted against the DEFAULT ruleset
(``_default_ruleset``/the module constants), which is what these messages
describe. The guard's own docstring already records the KNOWN LIMITATION that
a custom, well-formed ``bash_policy:`` entry diverging from the defaults would
still see default-describing prose in two ``_evaluate_git_tier_a`` deny
reasons; that gap is deliberately deferred there and is NOT re-litigated here.

Negative spec: an anchor-not-found failure is a real failure, not test
brittleness. These helpers fail loud when a message is reformatted past
recognition precisely so a reformat forces a deliberate re-read of whether the
new text still matches the ruleset -- silently degrading to "anchor absent, so
nothing to check" would reinstate exactly the unpoliced drift this suite
exists to catch.

Spec backlink: coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py
  § Divergence 8 (deny-message/ruleset drift)
"""

from __future__ import annotations

import re
from typing import List

import pytest

from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as guard,
)


def _full_deny_message() -> str:
    # Review: coordinator:code-reviewer (Finding 4, guard-message-size-
    # discipline) -- `agent_id` dropped from `_deny_reason`'s signature, it
    # was a dead parameter since message-size compression stopped echoing
    # it (see that function's own docstring).
    return guard._deny_reason(
        "coordinator:code-reviewer", "some denied command", "some reason"
    )


def _sole_line_matching(text: str, pattern: str) -> str:
    """Return the one line of ``text`` matching ``pattern``, failing loud on
    zero or multiple matches -- either shape means the message was reformatted
    and the enumeration this suite checks can no longer be located."""
    matches = [line for line in text.splitlines() if re.search(pattern, line)]
    assert len(matches) == 1, (
        f"expected exactly one line matching {pattern!r}, found {len(matches)}: "
        f"{matches!r} -- the deny message was reformatted; re-verify the "
        "enumeration still matches the ruleset, then update this anchor"
    )
    return matches[0]


def _split_enumeration(raw: str, separator: str) -> List[str]:
    return [item.strip() for item in raw.split(separator) if item.strip()]


def test_offer_block_git_subcommands_match_the_enforced_set():
    """The "Did you mean" git line offers exactly the read-only subcommands
    the guard admits -- no missing offer, no offer for a subcommand that in
    fact denies."""
    line = _sole_line_matching(_full_deny_message(), r"^\s+git show / diff /")
    offered = set(_split_enumeration(line.strip()[len("git ") :].rstrip(". "), "/"))
    assert offered == set(guard._GIT_READONLY_SUBCOMMANDS)


def test_offer_block_fs_binaries_match_the_enforced_set():
    """Likewise for the read-only filesystem binary line."""
    line = _sole_line_matching(_full_deny_message(), r"^\s+ls / cat / head /")
    offered = set(_split_enumeration(line.strip().rstrip(". "), "/"))
    assert offered == set(guard._READONLY_FS_BINARIES)


def test_pipeline_segment_reason_enumerations_match_the_enforced_sets():
    """``_pipeline_segment_deny_reason`` carries its own second copy of both
    Tier A enumerations -- the copy most likely to be forgotten, since it is
    reached only on a pipeline denial."""
    reason = guard._pipeline_segment_deny_reason("cowsay moo")
    git_run = re.search(r"show/diff/log[a-z\-/]*", reason)
    fs_run = re.search(r"ls/cat/head[a-z/]*", reason)
    assert git_run is not None and fs_run is not None, (
        "could not locate the Tier A enumerations in the pipeline-segment deny "
        f"reason: {reason!r}"
    )
    assert set(_split_enumeration(git_run.group(0), "/")) == set(
        guard._GIT_READONLY_SUBCOMMANDS
    )
    assert set(_split_enumeration(fs_run.group(0), "/")) == set(
        guard._READONLY_FS_BINARIES
    )


@pytest.mark.parametrize(
    "text_name",
    ["full_message", "metacharacter_reason"],
)
def test_metacharacter_enumeration_lists_every_unconditional_deny(text_name):
    """Both places that enumerate the banned metacharacters list exactly the
    unconditionally-denying members of ``_METACHARACTERS`` -- i.e. the full set
    minus the two Divergence 8 carve-outs (``|`` and ``>``), which are
    conditionally allowed and must NOT be advertised as unconditional denies."""
    if text_name == "full_message":
        text = _sole_line_matching(
            _full_deny_message(), r"shell-chaining metacharacter \("
        )
    else:
        text = guard._METACHARACTER_REASON
    parenthetical = re.search(r"\((.*?) or newline", text)
    assert parenthetical is not None, (
        f"could not locate the metacharacter enumeration in {text_name}: {text!r}"
    )
    listed = set(parenthetical.group(1).split())
    carve_outs = {"|", ">"}
    assert listed == set(guard._METACHARACTERS) - carve_outs
    assert not (listed & carve_outs), (
        "the conditionally-allowed pipe/redirect operators are advertised as "
        "unconditional denies"
    )


def test_find_write_flag_examples_are_real_members_of_the_denylist():
    """The offer block names two ``find`` write/execute flags by example; both
    must actually be denied. (Subset, not equality -- the message says "such
    as", so it is deliberately not exhaustive.)"""
    line = _sole_line_matching(_full_deny_message(), r"write/execute flag such as")
    named = set(re.findall(r"-[a-z]+", line.split("such as", 1)[1]))
    assert named, f"no example flags found in {line!r}"
    assert named <= set(guard._FIND_WRITE_FLAGS)


def test_scaffolder_offer_matches_the_enforced_tier_b_contract():
    """Every Tier B invocation form the message offers names the binary the
    guard admits and carries the argument it requires -- a stale offer here is
    a trap, sending a confined agent to run a command that will deny."""
    message = _full_deny_message()
    offered_forms = [
        line.strip()
        for line in message.splitlines()
        if guard._ALLOWED_BINARY_SUFFIX in line and line.startswith("  ")
    ]
    assert offered_forms, (
        f"no offered invocation form names {guard._ALLOWED_BINARY_SUFFIX!r}"
    )
    for form in offered_forms:
        assert guard._REQUIRED_TYPE_ARG_END in form, (
            f"offered form omits the required argument: {form!r}"
        )
