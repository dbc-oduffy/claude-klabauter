"""guard_doctrine_surface_edits: live Group EM admission, the out-of-repo doctrine leg,
the R4/R5 deny branches and the edit_admitted export."""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core.group_em import nomination
from coordinator_core.write_guards import _gem_doctrine_carve_out as co
from coordinator_core.write_guards import block_derived_global_doctrine_write as derived
from coordinator_core.write_guards import guard_doctrine_surface_edits as guard

SID = "sess-gem-1"
HOLDER_NAME = "group-em-44"

# guard._deny_reason at HEAD for a class-1 target, subagent audience.
_HEAD_SUBAGENT = (
    "[doctrine-surface guard] BLOCKED: {target} is always-loaded doctrine — it "
    "reaches every session and every dispatched agent in the fleet, so a change "
    "here is not a unilateral edit the EM can make on its own. Describe the "
    "proposed change to the PM and ask them to approve it before editing this "
    "file. Consider whether the content belongs in a wiki page or a skill "
    "surface instead — those need no approval."
)


class Box:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.doe = tmp_path / "doe"
        self.repo = tmp_path / "repo"
        self.home = tmp_path / "home"
        self.cfg = self.home / ".claude"
        for d in (self.doe / ".git", self.repo / ".git", self.cfg / "rules"):
            d.mkdir(parents=True)
        self.anchor = str(self.repo)
        self.state = "live"
        self.holder_sid = SID
        self.audience_em = True

    def payload(self, rel_or_path, tool="Edit", sid=SID, **extra):
        path = rel_or_path if os.path.isabs(str(rel_or_path)) else str(self.doe / rel_or_path)
        return {
            "tool_name": tool,
            "session_id": sid,
            "cwd": self.anchor,
            "tool_input": {"file_path": path, "old_string": "x", "new_string": "y"},
            **extra,
        }

    def approve(self, root):
        (root / ".git" / guard._SENTINEL_NAME).write_text("", encoding="utf-8")


@pytest.fixture
def box(tmp_path, monkeypatch):
    b = Box(tmp_path)
    monkeypatch.setenv("HOME", str(b.home))
    monkeypatch.setenv("USERPROFILE", str(b.home))
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    guard._content_root_memo.clear()
    co._content_root_memo.clear()
    monkeypatch.setattr(guard, "read_content_root", lambda: str(b.doe))
    monkeypatch.setattr(co, "read_content_root", lambda: str(b.doe))
    monkeypatch.setattr(co, "claude_config_dir", lambda: b.cfg)
    monkeypatch.setattr(guard, "_git_root", lambda: str(b.repo))
    monkeypatch.setattr(guard, "_in_cloud_session", lambda: False)
    monkeypatch.setattr(guard, "resolves_em_audience", lambda p, r: b.audience_em)
    monkeypatch.setattr(
        guard, "compute_repo_identity_gate", lambda *a, **k: {"verdict": "MATCH", "message": ""}
    )
    monkeypatch.setattr(guard, "_write_repo_identity_advisory_log", lambda *a, **k: None)
    monkeypatch.setattr(
        guard._write_bump_applicability,
        "resolve_launch_anchor",
        lambda sid, cwd=None, env=None: b.anchor,
    )

    def identify(repo_root, *a, **k):
        if b.state == "vacant":
            return {"state": "vacant", "holder": None}
        return {"state": b.state, "holder": {"session_id": b.holder_sid, "name": HOLDER_NAME}}

    monkeypatch.setattr(nomination, "identify", identify)
    monkeypatch.setattr(derived, "registry_get", lambda key: str(b.doe))
    yield b
    guard._content_root_memo.clear()
    co._content_root_memo.clear()


def _denied(result):
    return result is not None and result["hookSpecificOutput"].get("permissionDecision") == "deny"


def _reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


ADMITTED = [
    "global-doctrine/CLAUDE.md",
    "coordinator/skills/group-em/SKILL.md",
    "coordinator/commands/x.md",
    "coordinator/agents/x.md",
    "coordinator/hooks/x.py",
    "coordinator/docs/wiki/coordinator-tripwires/x.md",
    "coordinator/docs/decisions/DR-x.md",
    "docs/decisions/DR-x.md",
]


@pytest.mark.parametrize("rel", ADMITTED)
def test_live_gem_is_admitted_without_a_sentinel(box, rel):
    payload = box.payload(rel)
    assert not _denied(guard.check(payload))
    assert guard.edit_admitted(payload, payload["tool_input"]["file_path"]) is True


def test_live_gem_is_admitted_on_claude_rules(box):
    target = str(box.cfg / "rules" / "x.md")
    payload = box.payload(target)
    assert not _denied(guard.check(payload))
    assert guard.edit_admitted(payload, target) is True


def test_live_gem_is_admitted_from_a_session_anchored_in_doe(box):
    box.anchor = str(box.doe)
    assert not _denied(guard.check(box.payload("global-doctrine/CLAUDE.md")))


def test_live_gem_write_tool_is_admitted(box):
    payload = box.payload("global-doctrine/CLAUDE.md", tool="Write")
    payload["tool_input"] = {"file_path": payload["tool_input"]["file_path"], "content": "z"}
    assert not _denied(guard.check(payload))


def test_gem_allow_carries_the_rot_advisory_and_no_pm_approval_sentence(box):
    payload = box.payload("global-doctrine/CLAUDE.md")
    payload["tool_input"]["new_string"] = "ratified 2026-10-24"
    out = guard.check(payload)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "2026-10-24" in context
    assert "PM approval" not in context


def test_derived_copy_is_refused_to_the_live_gem_by_both_guards(box):
    target = str(box.cfg / "CLAUDE.md")
    payload = box.payload(target)
    assert _denied(guard.check(payload))
    assert guard.edit_admitted(payload, target) is False
    assert _denied(derived.check(payload))


def test_em_operating_doctrine_stays_refused_to_the_live_gem(box):
    snippet = box.payload("coordinator/snippets/em-operating-doctrine.md")
    assert _denied(guard.check(snippet))
    assert guard.edit_admitted(snippet, snippet["tool_input"]["file_path"]) is False


def test_engine_plane_is_not_admitted_to_the_live_gem(box):
    engine = box.root / "claude-klabauter" / "coordinator" / "skills" / "x.md"
    payload = box.payload(str(engine))
    assert co.gem_admitted(payload, str(engine)) is False


@pytest.mark.parametrize("state", ["not_live", "pending", "unverified", "ended", "vacant"])
def test_unverified_or_stale_standing_is_refused(box, state):
    box.state = state
    payload = box.payload("global-doctrine/CLAUDE.md")
    assert _denied(guard.check(payload))
    assert guard.edit_admitted(payload, payload["tool_input"]["file_path"]) is False


def test_another_live_holder_is_refused(box):
    box.holder_sid = "someone-else"
    payload = box.payload("global-doctrine/CLAUDE.md")
    assert _denied(guard.check(payload))


def test_subagent_of_the_live_gem_is_refused(box):
    box.audience_em = False
    payload = box.payload("global-doctrine/CLAUDE.md", agent_id="agent-1")
    assert _denied(guard.check(payload))


def test_env_var_claim_admits_nobody(box, monkeypatch):
    box.state = "pending"
    monkeypatch.setenv("COORDINATOR_GROUP_EM", "1")
    assert _denied(guard.check(box.payload("global-doctrine/CLAUDE.md")))


def test_a_pending_nomination_does_not_read_identify_for_a_non_surface(box, monkeypatch):
    calls = []
    monkeypatch.setattr(nomination, "identify", lambda *a, **k: calls.append(a))
    assert guard.check(box.payload(str(box.repo / "notes.md"))) is None
    assert calls == []


# --- out-of-repo doctrine leg (R3c) -------------------------------------------------

SKILL = "coordinator/skills/group-em/SKILL.md"


def test_non_gem_out_of_repo_skill_edit_is_denied_here(box):
    box.state = "vacant"
    assert _denied(guard.check(box.payload(SKILL)))


def test_non_gem_in_repo_skill_edit_is_not_denied(box):
    box.state = "vacant"
    box.anchor = str(box.doe)
    assert guard.check(box.payload(SKILL)) is None


def test_fresh_pm_sentinel_at_doe_admits_the_out_of_repo_non_gem_edit(box):
    box.state = "vacant"
    box.approve(box.doe)
    payload = box.payload(SKILL)
    assert not _denied(guard.check(payload))
    assert guard.edit_admitted(payload, payload["tool_input"]["file_path"]) is True


def test_sentinel_in_the_session_repo_does_not_approve_doe_doctrine(box):
    box.state = "vacant"
    box.approve(box.repo)
    assert _denied(guard.check(box.payload(SKILL)))


def test_no_resolvable_anchor_leaves_the_leg_unmatched(box, monkeypatch):
    box.state = "vacant"
    monkeypatch.setattr(
        guard._write_bump_applicability, "resolve_launch_anchor", lambda *a, **k: None
    )
    assert guard.check(box.payload(SKILL)) is None


# --- deny messages (R4 / R5) --------------------------------------------------------


def test_r4_live_gem_on_the_box_is_named_for_a_non_holder(box):
    box.holder_sid = "someone-else"
    out = guard.check(box.payload("global-doctrine/CLAUDE.md", sid="caller-2"))
    assert _denied(out)
    reason = _reason(out)
    assert (
        f"A live Group EM is on the box: SendMessage {HOLDER_NAME} the proposed text and why. "
        "It edits or declines." in reason
    )
    assert "coordinator-queue-append" not in reason


def test_r4_does_not_fire_for_a_target_the_gem_could_not_edit(box):
    box.holder_sid = "someone-else"
    out = guard.check(box.payload(str(box.repo / "CLAUDE.md"), sid="caller-2"))
    reason = _reason(out)
    assert "SendMessage" not in reason
    assert "coordinator-queue-append" in reason


def test_r5_no_live_gem_files_a_lesson_and_keeps_the_pm_route(box):
    box.state = "vacant"
    out = guard.check(box.payload("global-doctrine/CLAUDE.md"))
    assert _denied(out)
    reason = _reason(out)
    assert "SendMessage" not in reason
    assert "coordinator-queue-append --schema lessons" in reason
    assert "doctrine-change-request" in reason
    assert "global-doctrine" in reason
    assert "<exact text>" in reason and "<reason>" in reason
    assert "next Group EM or /learn-lessons applies it" in reason
    assert "For an edit needed now" in reason
    assert "guard-override-keys.md" in reason
    assert guard._SENTINEL_NAME not in reason


def test_r5_cloud_em_keeps_the_pm_words_route(box, monkeypatch):
    box.state = "vacant"
    monkeypatch.setattr(guard, "_in_cloud_session", lambda: True)
    reason = _reason(guard.check(box.payload("global-doctrine/CLAUDE.md")))
    assert "coordinator-queue-append --schema lessons" in reason
    assert "in their own words" in reason


def test_class_2_local_config_keeps_todays_text(box):
    box.state = "live"
    box.holder_sid = "someone-else"
    target = str(box.repo / "coordinator.local.md")
    reason = _reason(guard.check(box.payload(target, sid="caller-2")))
    assert "coordinator-queue-append" not in reason
    assert "SendMessage" not in reason
    assert "Describe the proposed change to the PM" in reason
    assert "See " in reason and "for how approval works." in reason


def test_subagent_render_is_byte_identical_to_head(box):
    box.audience_em = False
    for state in ("live", "vacant"):
        box.state = state
        box.holder_sid = "someone-else"
        target = str(box.doe / "global-doctrine" / "CLAUDE.md")
        out = guard.check(box.payload(target, sid="caller-2", agent_id="a1"))
        assert _reason(out) == _HEAD_SUBAGENT.format(target=target)


def test_the_cli_step_lands_in_the_lesson_writer(box):
    import re
    from pathlib import Path

    cli = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "coordinator-queue-append.py"
    text = cli.read_text(encoding="utf-8")
    assert '"queue.append"' in text
    from coordinator_core.ops import queue_append

    src = Path(queue_append.__file__).read_text(encoding="utf-8")
    assert re.search(r'register_op\("queue\.append"\)\s*\ndef \w+[\s\S]*?append_queue_entry\(', src)


# --- edit_admitted ------------------------------------------------------------------


def test_edit_admitted_non_protected_path_is_true(box):
    payload = box.payload(str(box.repo / "notes.md"))
    assert guard.edit_admitted(payload, str(box.repo / "notes.md")) is True


def test_edit_admitted_protected_without_standing_or_sentinel_is_false(box):
    box.state = "vacant"
    target = str(box.repo / "CLAUDE.md")
    assert guard.edit_admitted(box.payload(target), target) is False


def test_edit_admitted_with_a_fresh_sentinel_is_true(box):
    box.state = "vacant"
    box.approve(box.repo)
    target = str(box.repo / "CLAUDE.md")
    assert guard.edit_admitted(box.payload(target), target) is True


def test_edit_admitted_never_unlinks_a_root_sentinel(box):
    box.state = "vacant"
    root_file = box.repo / guard._SENTINEL_NAME
    root_file.write_text("", encoding="utf-8")
    target = str(box.repo / "CLAUDE.md")
    assert guard.edit_admitted(box.payload(target), target) is False
    assert root_file.exists()


def test_edit_admitted_is_false_for_subagent_of_the_gem(box):
    payload = box.payload("global-doctrine/CLAUDE.md", agent_id="a1")
    assert guard.edit_admitted(payload, payload["tool_input"]["file_path"]) is False


def test_edit_admitted_fails_closed_on_an_exception(box, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(guard, "_match_protected", boom)
    monkeypatch.setattr(guard, "gem_admitted", lambda *a: False)
    target = str(box.repo / "notes.md")
    assert guard.edit_admitted(box.payload(target), target) is False


# --- process time -------------------------------------------------------------------


@pytest.mark.parametrize("gem", [True, False])
def test_check_process_time_stays_under_the_one_process_bar(box, gem):
    box.state = "live" if gem else "vacant"
    payload = box.payload("global-doctrine/CLAUDE.md")
    guard.check(payload)
    start = time.process_time()
    for _ in range(200):
        guard.check(payload)
    per_call = (time.process_time() - start) / 200
    assert per_call < 0.2
