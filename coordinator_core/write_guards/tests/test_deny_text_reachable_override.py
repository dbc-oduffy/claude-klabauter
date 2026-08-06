"""Runtime-render gate (write_guards sibling of
`bash_guards/tests/test_deny_text_reachable_override.py`): a write-guard's
deny/remediation TEXT must not name an override env var as `NAME=1` unless
the SAME rendered text also carries its reachability constraint (pre-launch
only) via `operator_override_note`.

Same approach as the bash_guards gate this mirrors: CALL the deny-text
builder with representative synthetic arguments and inspect the ACTUAL
RENDERED STRING, rather than statically folding the source — a runtime call
has no unfoldable operand, so it catches the identical defect shape a static
AST folder provably cannot see through (see the bash_guards sibling's module
docstring for the full "why a runtime gate, not a static one" argument; not
restated here).

SCOPE, NAMED NOT SILENT: covers every write_guards site converted by the
2026-07-30 write_guards override-note sweep (this dispatch) that exposes
either (a) a standalone deny-text-builder function callable with synthetic
arguments, or (b) a `check()` entry point whose deny branch needs only a
`tool_name`/`tool_input` payload (no disk read, no git-root resolution) to
reach the deny text.

KNOWN-UNCOVERED SITES (also converted by this sweep, deliberately NOT
render-tested here — asserting nothing would be worse than naming the gap):
  - `block_consumed_handoff_edit.check()` — deny text is built inline inside
    `check()`, reachable only after a real on-disk candidate file with
    `status: claimed`/`consumed` frontmatter is read via `_resolve_git_root`
    + `Path.is_file()`/`read_text()`. Synthesizing that needs a real repo
    tree, not just a payload dict.
  - `block_memo_status_hand_edit.check()` — same shape: inline deny text,
    gated on `_edit_touches_status()` + an on-disk memo file with a real
    `status:` frontmatter field.
  - `block_cutover_phase_hand_edit.check()` — same shape: inline deny text
    gated on an on-disk cutover record file with a real `phase:` field.
  - `block_dev_side_mirror_wiki.check()` — inline deny text gated on
    `_resolve_plugin_root()` (a trusted `CLAUDE_PLUGIN_ROOT` env var pointing
    at a real bundled `docs/wiki` tree) succeeding first; synthesizing a
    trusted plugin root is environment setup, not a payload shape.
  Each of the four above builds its deny text with an f-string containing
  the exact same `operator_override_note(...)` call as every covered site
  (see the source diff for this sweep) — the conversion is real, only the
  render-time verification is deferred pending a disk/environment-fixture
  investment a future dispatch can pick up.

NOT COVERED BY DESIGN (not converted, not override clauses in deny text —
see `docs/reference/guard-override-keys.md` "Not in this table, by design"
for the full reasoning per var):
  - `block_home_dir_memo_delivery.py` (`COORDINATOR_OVERRIDE_HOME_MEMO_GUARD`)
    — deliberately unadvertised, never named in deny text.
  - `validate_frontmatter_schema_deny.py`'s `COORDINATOR_OVERRIDE_MEMO_REDIRECT`
    — read in a code condition only, never interpolated into rendered text.
  - `COORDINATOR_SCHEMA_STRICT` — an internal mode flag, not a bypass route.
  Note: `nudge_improvement_queue_write.py` / `nudge_baton_body_bar.py`'s
  `COORDINATOR_QUEUE_PUNT` / `COORDINATOR_BATON_BODY_PUNT` used to be listed
  here too, on the reasoning that their `VAR="<reason>"` shape put them out
  of `_VIOLATION_RE`'s regex scope. That reasoning is now STALE: the
  bash_guards copy of `_VIOLATION_RE` was extended (2026-07-30) to also match
  the reason-shaped `NAME="..."` form these two vars use, specifically so it
  would not silently drift out of view (see that regex's own docstring in
  `bash_guards/tests/test_deny_text_reachable_override.py`). These two are
  NOT "not covered by design" — they ARE covered, just below, via bespoke
  assertions rather than `assert_render_carries_reachability_constraint`,
  because their tests need one more guarantee that helper doesn't check: that
  the text does not instruct the reader to "re-run the write" with an env var
  that cannot be set from inside a session (see the block comment above those
  tests for the full reasoning).

Spec backlink: 2026-07-30 write_guards override-note sweep (this dispatch),
following the bash_guards precedent
(`coordinator_core/bash_guards/tests/test_deny_text_reachable_override.py`)
and the PM ruling that added `operator_override_note` as the cross-package
SSOT (`coordinator_core/bash_guards/_helpers.py`).
"""

from __future__ import annotations

import os
import re

import pytest

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards.tests.test_deny_text_reachable_override import (
    assert_render_carries_reachability_constraint,
)
from coordinator_core.write_guards import (
    block_completion_monolith_write,
    block_em_hand_edit_pending_review_integration as block_review_pending,
    block_illegal_filename,
    block_priority_ledger_edit,
    block_subagent_archive_write,
    block_subagent_plan_body_write,
    block_tracker_edit,
    block_unauthorized_claude_md_write,
    nudge_baton_body_bar,
    nudge_improvement_queue_write,
    validate_frontmatter_schema_deny as schema_deny,
)


@pytest.fixture(autouse=True)
def _no_override_env_leaks(monkeypatch):
    """Every guard exercised here honors its own escape hatch FIRST -- make
    sure none of this test process's ambient environment accidentally
    disarms the guard before its deny text can be rendered."""
    for name in (
        block_completion_monolith_write._OVERRIDE_ENV_VAR,
        block_priority_ledger_edit._OVERRIDE_ENV_VAR,
        block_tracker_edit._OVERRIDE_ENV_VAR,
        block_subagent_archive_write._OVERRIDE_ENV_VAR,
        block_subagent_plan_body_write._OVERRIDE_ENV_VAR,
        block_unauthorized_claude_md_write._OVERRIDE_ENV_VAR,
        block_review_pending._OVERRIDE_ENV_VAR,
        block_illegal_filename._OVERRIDE_ENV,
        "COORDINATOR_OVERRIDE_OWN_INBOX",
        nudge_improvement_queue_write._ESCAPE_HATCH_ENV_VAR,
        nudge_baton_body_bar._ESCAPE_HATCH_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Standalone deny-text-builder functions -- called directly, no payload
# plumbing needed.
# ---------------------------------------------------------------------------


def test_block_em_hand_edit_pending_review_integration_deny_reason():
    rendered = block_review_pending._deny_reason(
        "docs/plans/2026-07-01-example.md", "state/subagent-share/x/y.md"
    )
    assert_render_carries_reachability_constraint(rendered, context="block_em_hand_edit_pending_review_integration._deny_reason")
    assert operator_override_note(block_review_pending._OVERRIDE_ENV_VAR) in rendered


def test_block_illegal_filename_make_deny_msg():
    rendered = block_illegal_filename._make_deny_msg("bad:name.md", ":")
    assert_render_carries_reachability_constraint(rendered, context="block_illegal_filename._make_deny_msg")
    assert operator_override_note(block_illegal_filename._OVERRIDE_ENV) in rendered


def test_block_subagent_archive_write_deny_reason():
    rendered = block_subagent_archive_write._deny_reason("agent-123", "archive/foo.md")
    assert_render_carries_reachability_constraint(rendered, context="block_subagent_archive_write._deny_reason")
    assert operator_override_note(block_subagent_archive_write._OVERRIDE_ENV_VAR) in rendered


def test_block_subagent_plan_body_write_deny_reason_ambiguous():
    rendered = block_subagent_plan_body_write._deny_reason_ambiguous("agent-123", "docs/plans/x.md")
    assert_render_carries_reachability_constraint(rendered, context="block_subagent_plan_body_write._deny_reason_ambiguous")
    assert operator_override_note(block_subagent_plan_body_write._OVERRIDE_ENV_VAR) in rendered


def test_block_subagent_plan_body_write_deny_reason_executor():
    rendered = block_subagent_plan_body_write._deny_reason_executor("agent-123", "docs/plans/x.md")
    assert_render_carries_reachability_constraint(rendered, context="block_subagent_plan_body_write._deny_reason_executor")
    assert operator_override_note(block_subagent_plan_body_write._OVERRIDE_ENV_VAR) in rendered


def test_block_unauthorized_claude_md_write_deny_reason():
    rendered = block_unauthorized_claude_md_write._deny_reason("agent-123", "CLAUDE.md")
    assert_render_carries_reachability_constraint(rendered, context="block_unauthorized_claude_md_write._deny_reason")
    assert operator_override_note(block_unauthorized_claude_md_write._OVERRIDE_ENV_VAR) in rendered


def test_validate_frontmatter_schema_deny_own_inbox_message():
    rendered = schema_deny._own_inbox_deny_message("this-em", "other-em")
    assert_render_carries_reachability_constraint(rendered, context="validate_frontmatter_schema_deny._own_inbox_deny_message")
    assert operator_override_note("COORDINATOR_OVERRIDE_OWN_INBOX") in rendered


# ---------------------------------------------------------------------------
# check() entry points whose deny branch needs only tool_name/tool_input --
# no disk read, no git-root resolution.
# ---------------------------------------------------------------------------


def test_block_completion_monolith_write_check_deny():
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "archive/completed/2026-01.md"},
    }
    result = block_completion_monolith_write.check(payload)
    assert result is not None
    rendered = result["hookSpecificOutput"]["additionalContext"]
    assert_render_carries_reachability_constraint(rendered, context="block_completion_monolith_write.check")
    assert operator_override_note(block_completion_monolith_write._OVERRIDE_ENV_VAR) in rendered


def test_block_priority_ledger_edit_check_deny():
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "state/priority-ledger/some-target.yaml"},
    }
    result = block_priority_ledger_edit.check(payload)
    assert result is not None
    rendered = result["hookSpecificOutput"]["additionalContext"]
    assert_render_carries_reachability_constraint(rendered, context="block_priority_ledger_edit.check")
    assert operator_override_note(block_priority_ledger_edit._OVERRIDE_ENV_VAR) in rendered


def test_block_tracker_edit_check_deny():
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "state/handoff-tracker.md"},
    }
    result = block_tracker_edit.check(payload)
    assert result is not None
    rendered = result["hookSpecificOutput"]["additionalContext"]
    assert_render_carries_reachability_constraint(rendered, context="block_tracker_edit.check")
    assert operator_override_note(block_tracker_edit._OVERRIDE_ENV_VAR) in rendered


# ---------------------------------------------------------------------------
# nudge_improvement_queue_write / nudge_baton_body_bar -- 2026-07-30
# escape-mechanism rework (COORDINATOR_QUEUE_PUNT is unreachable from inside
# a session; the deny/advisory text must not instruct the reader to take an
# action that cannot work from there). These two use the `VAR="<reason>"`
# shape (not `VAR=1`). `_VIOLATION_RE` in the bash_guards sibling this module
# mirrors was ITSELF extended (same 2026-07-30 dispatch) to also match that
# reason-shaped form, so it is no longer true that this shape is outside the
# regex's scope -- it is asserted directly here anyway, not because the regex
# can't see it, but because these two tests need one more guarantee the
# shared `assert_render_carries_reachability_constraint` helper doesn't check:
# the rendered text must carry `operator_override_note`'s output verbatim,
# AND must NOT instruct the reader to "re-run the write" with an env var (the
# exact dead-end shape this whole gate exists to catch, just spelled with a
# different var shape -- COORDINATOR_QUEUE_PUNT's write already landed by the
# time this text renders, so "re-run" is doubly wrong here).
# ---------------------------------------------------------------------------


def test_nudge_improvement_queue_write_deny_carries_override_note():
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "state/improvement-queue/2026-07-30-example.yaml",
            "content": "created: 2026-07-30\ntitle: example\n",
        },
    }
    result = nudge_improvement_queue_write.check(payload)
    assert result is not None
    rendered = result["hookSpecificOutput"]["additionalContext"]
    env_var = nudge_improvement_queue_write._ESCAPE_HATCH_ENV_VAR
    assert (
        operator_override_note(env_var, reason_placeholder="<one-sentence reason>")
        in rendered
    )
    assert "re-run the write" not in rendered
    assert "(pre-launch only)" in rendered
    # Shape must be reason-shaped (VAR="...") not flag-shaped (VAR=1) -- "1"
    # is denylisted by this guard's own _is_trivial_reason, so a VAR=1
    # remediation would be refused by the very guard printing it (the P1
    # bug this test exists to catch).
    assert "%s=1" % env_var not in rendered


def test_nudge_improvement_queue_write_override_note_round_trips_through_own_guard():
    """Not a string-match against the same buggy string (the tautology the
    reviewer flagged in the prior version of this gate) -- extracts the
    literal value this guard PRINTS as its remediation and feeds it back
    through the guard's OWN acceptance predicate (`_is_trivial_reason`),
    proving the printed remediation is one this guard would actually accept.
    This is exactly the check that would have caught the original bug
    (`COORDINATOR_QUEUE_PUNT=1` rejected by the guard's own trivial-value
    denylist)."""
    env_var = nudge_improvement_queue_write._ESCAPE_HATCH_ENV_VAR
    rendered = operator_override_note(env_var, reason_placeholder="<one-sentence reason>")
    match = re.search(r'%s="([^"]*)"' % re.escape(env_var), rendered)
    assert match is not None, "expected a reason-shaped VAR=\"...\" mention in the rendered note"
    printed_value = match.group(1)
    assert not nudge_improvement_queue_write._is_trivial_reason(printed_value), (
        "the printed remediation value %r is rejected by this guard's own "
        "_is_trivial_reason -- same defect class as the VAR=1 bug this "
        "round-trip test exists to catch" % printed_value
    )


def test_nudge_improvement_queue_write_justification_field_allows_write():
    """The content-based escape must actually WORK -- a deny path with an
    escape nobody proved works is how the original defect shipped."""
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "state/improvement-queue/2026-07-30-example.yaml",
            "content": (
                "created: 2026-07-30\n"
                "title: example\n"
                "justification: genuinely cross-cutting, needs its own plan\n"
            ),
        },
    }
    assert nudge_improvement_queue_write.check(payload) is None


def test_nudge_improvement_queue_write_trivial_justification_still_denies():
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "state/improvement-queue/2026-07-30-example.yaml",
            "content": "created: 2026-07-30\ntitle: example\njustification: ok\n",
        },
    }
    result = nudge_improvement_queue_write.check(payload)
    assert result is not None
    rendered = result["hookSpecificOutput"]["additionalContext"]
    assert "trivial" in rendered


def test_nudge_improvement_queue_write_legacy_prose_justification_line_allows_edit():
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "state/improvement-queue.md",
            "old_string": "existing content\n",
            "new_string": (
                "existing content\n"
                "- 2026-07-30 | new entry text\n"
                "  justification: cross-cutting pattern, own plan needed\n"
            ),
        },
    }
    assert nudge_improvement_queue_write.check(payload) is None


def test_nudge_baton_body_bar_advisory_does_not_instruct_rerun():
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "state/handoffs/example.md",
            "content": "- row one\n- row two\n- row three\n",
        },
    }
    result = nudge_baton_body_bar.check(payload)
    assert result is not None
    rendered = result["hookSpecificOutput"]["additionalContext"]
    env_var = nudge_baton_body_bar._ESCAPE_HATCH_ENV_VAR
    assert "re-run the write" not in rendered
    assert (
        operator_override_note(env_var, reason_placeholder="<one-sentence reason>")
        in rendered
    )
    assert "(pre-launch only)" in rendered
    assert "%s=1" % env_var not in rendered


def test_nudge_baton_body_bar_override_note_round_trips_through_own_guard():
    """Round-trip sibling of
    `test_nudge_improvement_queue_write_override_note_round_trips_through_own_guard`
    -- same P1 defect class, same guard-shaped acceptance-predicate check,
    for `COORDINATOR_BATON_BODY_PUNT`'s independently-owned trivial-reason
    predicate."""
    env_var = nudge_baton_body_bar._ESCAPE_HATCH_ENV_VAR
    rendered = operator_override_note(env_var, reason_placeholder="<one-sentence reason>")
    match = re.search(r'%s="([^"]*)"' % re.escape(env_var), rendered)
    assert match is not None, "expected a reason-shaped VAR=\"...\" mention in the rendered note"
    printed_value = match.group(1)
    assert not nudge_baton_body_bar._is_trivial_reason(printed_value), (
        "the printed remediation value %r is rejected by this guard's own "
        "_is_trivial_reason -- same defect class as the VAR=1 bug this "
        "round-trip test exists to catch" % printed_value
    )
