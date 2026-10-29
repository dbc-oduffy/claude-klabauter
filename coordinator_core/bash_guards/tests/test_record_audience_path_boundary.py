"""Path and write-boundary guards return AlternativeRecords; the subagent render drops overrides."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import (
    block_drive_root_new_dir_bash_write as drive_root,
    block_external_workspace_vcs_write as external,
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


def _text(envelope: dict) -> str:
    return envelope["hookSpecificOutput"]["permissionDecisionReason"]


def _has_override(record: AlternativeRecord) -> bool:
    return any(isinstance(m, Override) for m in record.members)


def _has_route(record: AlternativeRecord) -> bool:
    return any(isinstance(m, EscalationRoute) for m in record.members)


@pytest.mark.parametrize("command", ["git checkout -b fix/foo", "git push -u origin fix/foo"])
def test_topic_branch_create_or_push_emits_nothing(tmp_path, command):
    # Contract retired by 900eac166a: block-topic-branch no longer judges a non-day branch.
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/fix/foo\n", encoding="utf-8")
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}
    assert topic.check(payload) is None


def test_topic_branch_push_hold_carries_no_override():
    record = topic._hold_reason("main", "frozen")
    assert not _has_override(record)
    assert "push-hold clear" in _text(render(record, Audience.EM))


def test_topic_branch_main_commit_refusal_is_an_escalation_record_without_override():
    # Contract retired by 900eac166a: the record carries no Override member; the inline
    # env prefix is read by the guard itself, never offered through the record.
    legacy = "BLOCKED: x Alternative: commit to the day branch."
    record = topic._record("BLOCKED: x", legacy, topic._escalation("y"))
    assert _has_route(record) and not _has_override(record)
    assert topic.OVERRIDE_KEY not in _text(render(record, Audience.SUBAGENT))
    assert _text(render(record, Audience.EM)) == legacy


def test_home_root_em_text_survives_as_the_advisory_context():
    # Advisory by ruling: the would-be deny reaches the model as an allow carrying the deny text.
    out = home_root._deny()
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert home_root.DENY_REASON.partition(" Instead: ")[0] in out["hookSpecificOutput"]["additionalContext"]


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
def test_drive_and_home_root_are_advisory_at_every_level(guard, call, monkeypatch):
    # Neither guard is a deny_registry floor guard, so no level yields a record or a refusal.
    for level in ("strict", "warn", "off"):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        machine_profile.reset_cache()
        out = call(guard)
        assert isinstance(out, dict) and not isinstance(out, AlternativeRecord)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert out["guardLevel"] == "log"
    machine_profile.reset_cache()
