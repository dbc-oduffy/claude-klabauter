"""Path and write-boundary guards return AlternativeRecords; the subagent render drops overrides."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import (
    block_drive_root_new_dir_bash_write as drive_root,
    block_external_workspace_vcs_write as external,
    block_hand_authored_handoff_creation as handoff,
    block_home_root_bash_write as home_root,
    block_push_pr_non_owned_remote as push_pr,
    block_topic_branch as topic,
    block_whole_filesystem_scan as scan,
    guard_move_from_inside_cwd as move,
)
from coordinator_core.guard_messages.alternative_record import (
    AlternativeRecord,
    Audience,
    EscalationRoute,
    Override,
)
from coordinator_core.guard_messages.render import render
from coordinator_core.write_guards import block_hand_authored_handoff_creation as handoff_twin


def _text(envelope: dict) -> str:
    return envelope["hookSpecificOutput"]["permissionDecisionReason"]


def _has_override(record: AlternativeRecord) -> bool:
    return any(isinstance(m, Override) for m in record.members)


def _has_route(record: AlternativeRecord) -> bool:
    return any(isinstance(m, EscalationRoute) for m in record.members)


def test_handoff_bash_twin_builds_a_record_from_the_write_twins_reason(tmp_path):
    out = handoff.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo x > state/handoffs/new.md"},
            "cwd": str(tmp_path),
        }
    )
    assert isinstance(out, AlternativeRecord)
    first_line = handoff_twin.DENY_REASON.splitlines()[0]
    assert out.summary == first_line
    em = _text(render(out, Audience.EM, override_note="NOTE"))
    assert "coordinator-doc-new --type handoff" in em
    assert em == _text(handoff_twin.deny_envelope({"tool_name": "Bash"}))


def test_handoff_subagent_render_drops_the_override_and_keeps_a_route(tmp_path):
    record = handoff._deny_record({"tool_name": "Bash"})
    assert _has_override(record) and _has_route(record)
    sub = _text(render(record, Audience.SUBAGENT, override_note="NOTE"))
    assert "NOTE" not in sub and handoff_twin.OVERRIDE_ENV_VAR not in sub
    assert "Report" in sub


def test_topic_branch_override_is_a_member_not_prose():
    record = topic._deny_reason("fix/foo")
    overrides = [m for m in record.members if isinstance(m, Override)]
    assert [o.env_var for o in overrides] == [topic.OVERRIDE_KEY]
    sub = _text(render(record, Audience.SUBAGENT))
    assert topic.OVERRIDE_KEY not in sub and "Group EM" in sub and "day branch" in sub
    em = _text(render(record, Audience.EM, override_note="NOTE"))
    assert em == record.em_text and topic.OVERRIDE_KEY in em and "NOTE" not in em


def test_topic_branch_push_hold_carries_no_override():
    record = topic._hold_reason("main", "frozen")
    assert not _has_override(record)
    assert "push-hold clear" in _text(render(record, Audience.EM))


def test_topic_branch_main_commit_refusal_is_an_override_record(monkeypatch):
    legacy = (
        "BLOCKED: x\n\nException: %s with a non-empty reason, or %s=1, prefixed on the command."
        % (topic.OVERRIDE_KEY, topic.MAIN_COMMIT_OVERRIDE_KEY)
    )
    record = topic._record("BLOCKED: x", legacy, topic._escalation("y"), topic._override())
    assert _has_override(record) and _has_route(record)
    assert topic.OVERRIDE_KEY not in _text(render(record, Audience.SUBAGENT))
    em = _text(render(record, Audience.EM))
    assert em == legacy and topic.MAIN_COMMIT_OVERRIDE_KEY in em


def test_home_root_and_drive_root_em_text_is_the_legacy_reason():
    assert home_root._deny().em_text == home_root.DENY_REASON
    assert _text(render(home_root._deny(), Audience.EM)) == home_root.DENY_REASON


@pytest.mark.parametrize(
    "record",
    [
        external._deny_record("git commit", "p4_x"),
        external._deny_record("git commit", None),
        push_pr._deny_record("git push", "github.com/theirs/repo"),
        move._deny("BLOCKED: the shell cwd `a` is inside `b`; Windows refuses to move a directory the shell holds."),
    ],
)
def test_overrideless_records_render_the_same_for_every_audience(record):
    assert not _has_override(record)
    em = _text(render(record, Audience.EM))
    assert em == _text(render(record, Audience.EM, override_note="NOTE"))
    assert "NOTE" not in em


def test_whole_filesystem_scan_returns_a_record_with_a_scoped_alternative():
    out = scan.check({"tool_name": "Bash", "tool_input": {"command": "find ~ -name x"}, "cwd": None})
    assert isinstance(out, AlternativeRecord) and not _has_override(out)
    assert "whole-filesystem scan" in _text(render(out, Audience.EM))


def test_whole_filesystem_scan_still_allows_a_scoped_search():
    assert scan.check({"tool_name": "Bash", "tool_input": {"command": "find src -name x"}, "cwd": None}) is None


@pytest.mark.parametrize("guard, call", [(drive_root, lambda g: g._deny("stray")), (home_root, lambda g: g._deny())])
def test_drive_and_home_root_return_a_record_at_strict_and_the_leveled_envelope_otherwise(
    guard, call, monkeypatch
):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    machine_profile.reset_cache()
    assert isinstance(call(guard), AlternativeRecord)
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "warn")
    machine_profile.reset_cache()
    leveled = call(guard)
    assert isinstance(leveled, dict)
    assert leveled["hookSpecificOutput"]["permissionDecision"] == "allow"
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "off")
    machine_profile.reset_cache()
    assert call(guard) is None
    machine_profile.reset_cache()
