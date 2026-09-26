"""Tests for `_write_bump_message._classification_defect_notice`'s
containment leg -- in-repo build output (`.next/standalone` and the like)
classified as a foreign repo (Item 48,
state/handoffs/2026-09-11-inbox-blitz-small-fixes.md,
`2026-09-02-example-cockpit-repo-em-write-guard-blocks-the-repos-own-build-
output-directory.md`).

`_classification_defect_notice` previously only caught the exact-equal case
(`target_repo == session_repo`); build output resolving to its own nested
"repo" path (`<session_repo>/.next/standalone`) reached the ordinary
FOREIGN-class contrast form instead, naming a path that never actually left
the session's own tree. This suite pins the widened containment check
(`_target_is_contained_in_session`) and its effect on both FOREIGN-class
renderers, plus the genuinely-foreign case staying unaffected.
"""

from __future__ import annotations

from coordinator_core.bash_guards import _write_bump_message as message
from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES, measure_envelope

_SESSION_REPO = "claude-klabauter"
_BUILD_OUTPUT_TARGET = "claude-klabauter/.next/standalone"
_SESSION_ID = "751ab9de-9319-4d63-b174-36145a4a3045"
_SANDBOX_ROOT = "state/subagent-share/751ab9de-9319-4d63-b174-36145a4a3045"
_BACKLOG_ENTRY = (
    "state/bug-backlog/2026-08-21-foreign-write-deny-names-the-same-repo-on-both-sides.yaml"
)


def _measure(text: str):
    envelope = {"hookSpecificOutput": {"permissionDecisionReason": text}}
    return measure_envelope(envelope)


def test_containment_true_when_target_equals_session():
    assert message._target_is_contained_in_session(_SESSION_REPO, _SESSION_REPO) is True


def test_containment_true_when_target_nested_under_session():
    assert message._target_is_contained_in_session(_BUILD_OUTPUT_TARGET, _SESSION_REPO) is True


def test_containment_false_when_target_is_a_sibling_repo():
    assert message._target_is_contained_in_session("DoE-claude", _SESSION_REPO) is False


def test_containment_false_when_target_or_session_empty():
    assert message._target_is_contained_in_session("", _SESSION_REPO) is False
    assert message._target_is_contained_in_session(_BUILD_OUTPUT_TARGET, "") is False


# `render_em_message` -- build output reaching the FOREIGN-class EM renderer


def test_em_message_names_defect_for_in_repo_build_output():
    text = message.render_em_message(_BUILD_OUTPUT_TARGET, _SESSION_REPO, None, _SESSION_ID)
    assert "classification defect" in text
    assert _BUILD_OUTPUT_TARGET in text
    assert _SESSION_REPO in text
    assert _BACKLOG_ENTRY in text
    # Not the ordinary FOREIGN-class contrast form -- the containment case
    assert f"(not `{_SESSION_REPO}`)" not in text


def test_em_message_build_output_defect_still_self_attributes():
    text = message.render_em_message(_BUILD_OUTPUT_TARGET, _SESSION_REPO, None, _SESSION_ID)
    assert text.startswith("Coordinator guard")


def test_em_message_build_output_defect_fits_the_prose_cap():
    text = message.render_em_message(_BUILD_OUTPUT_TARGET, _SESSION_REPO, None, _SESSION_ID)
    measurement = _measure(text)
    assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES
    assert measurement.over_cap is False


# `render_subagent_message` -- same for the FOREIGN-class subagent renderer


def test_subagent_message_names_defect_for_in_repo_build_output():
    text = message.render_subagent_message(
        _BUILD_OUTPUT_TARGET, _SESSION_REPO, None, _SESSION_ID, _SANDBOX_ROOT
    )
    assert "classification defect" in text
    assert _BUILD_OUTPUT_TARGET in text
    assert _SESSION_REPO in text
    assert _BACKLOG_ENTRY in text
    assert "report to the EM that dispatched you" in text
    assert f"(not `{_SESSION_REPO}`)" not in text


def test_subagent_message_build_output_defect_fits_the_prose_cap():
    text = message.render_subagent_message(
        _BUILD_OUTPUT_TARGET, _SESSION_REPO, None, _SESSION_ID, _SANDBOX_ROOT
    )
    measurement = _measure(text)
    assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES
    assert measurement.over_cap is False


def test_em_message_genuinely_foreign_target_unaffected():
    text = message.render_em_message("DoE-claude", _SESSION_REPO, None, _SESSION_ID)
    assert "classification defect" not in text
    assert f"(not `{_SESSION_REPO}`)" in text


def test_subagent_message_genuinely_foreign_target_unaffected():
    text = message.render_subagent_message(
        "DoE-claude", _SESSION_REPO, None, _SESSION_ID, _SANDBOX_ROOT
    )
    assert "classification defect" not in text
    assert f"(not `{_SESSION_REPO}`)" in text
