"""guard_doctrine_surface_edits: live Group EM admission, the out-of-repo doctrine leg,
the advisory text and the edit_admitted export."""

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

_REVIEW_MARK = "Review check"


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

    def payload(self, rel_or_path, tool="Edit", sid=SID, **extra):
        path = rel_or_path if os.path.isabs(str(rel_or_path)) else str(self.doe / rel_or_path)
        return {
            "tool_name": tool,
            "session_id": sid,
            "cwd": self.anchor,
            "tool_input": {"file_path": path, "old_string": "x", "new_string": "y"},
            **extra,
        }


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


def _context(result):
    assert result is not None
    assert "permissionDecision" not in result["hookSpecificOutput"]
    return result["hookSpecificOutput"]["additionalContext"]


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
def test_live_gem_edit_lands_with_no_advisory_text(box, rel):
    payload = box.payload(rel)
    assert guard.check(payload) is None
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


def test_gem_allow_carries_only_the_rot_advisory(box):
    payload = box.payload("global-doctrine/CLAUDE.md")
    payload["tool_input"]["new_string"] = "ratified 2026-10-24"
    context = _context(guard.check(payload))
    assert "2026-10-24" in context
    assert _REVIEW_MARK not in context


def test_derived_copy_edit_is_not_offered_to_the_live_gem(box):
    target = str(box.cfg / "CLAUDE.md")
    payload = box.payload(target)
    assert not _denied(guard.check(payload))
    assert guard.edit_admitted(payload, target) is False
    # DR-475: the derived-copy guard is not registered to deny; it advises.
    routed = derived.check(payload)
    assert not _denied(routed)
    assert "derived global-doctrine" in routed["hookSpecificOutput"]["additionalContext"]


NON_GEM_STANDINGS = ["not_live", "pending", "unverified", "ended", "vacant"]


@pytest.mark.parametrize("state", NON_GEM_STANDINGS)
def test_unverified_or_stale_standing_lands_with_the_review_advisory(box, state):
    box.state = state
    payload = box.payload("global-doctrine/CLAUDE.md")
    assert _REVIEW_MARK in _context(guard.check(payload))
    assert guard.edit_admitted(payload, payload["tool_input"]["file_path"]) is True


def test_em_operating_doctrine_lands_with_the_review_advisory_for_the_live_gem(box):
    snippet = box.payload("coordinator/snippets/em-operating-doctrine.md")
    assert _REVIEW_MARK in _context(guard.check(snippet))
    assert guard.edit_admitted(snippet, snippet["tool_input"]["file_path"]) is True


def test_engine_plane_is_not_admitted_to_the_live_gem(box):
    engine = box.root / "claude-klabauter" / "coordinator" / "skills" / "x.md"
    payload = box.payload(str(engine))
    assert co.gem_admitted(payload, str(engine)) is False


def test_another_live_holder_lands_with_the_review_advisory(box):
    box.holder_sid = "someone-else"
    assert _REVIEW_MARK in _context(guard.check(box.payload("global-doctrine/CLAUDE.md")))


def test_subagent_of_the_live_gem_lands_with_the_review_advisory(box):
    payload = box.payload("global-doctrine/CLAUDE.md", agent_id="agent-1")
    assert _REVIEW_MARK in _context(guard.check(payload))


def test_env_var_claim_admits_nobody_to_the_quiet_path(box, monkeypatch):
    box.state = "pending"
    monkeypatch.setenv("COORDINATOR_GROUP_EM", "1")
    assert _REVIEW_MARK in _context(guard.check(box.payload("global-doctrine/CLAUDE.md")))


def test_a_pending_nomination_does_not_read_identify_for_a_non_surface(box, monkeypatch):
    calls = []
    monkeypatch.setattr(nomination, "identify", lambda *a, **k: calls.append(a))
    assert guard.check(box.payload(str(box.repo / "notes.md"))) is None
    assert calls == []


# --- out-of-repo doctrine leg (R3c) -------------------------------------------------

SKILL = "coordinator/skills/group-em/SKILL.md"


def test_non_gem_out_of_repo_skill_edit_lands_with_the_review_advisory(box):
    box.state = "vacant"
    assert _REVIEW_MARK in _context(guard.check(box.payload(SKILL)))


def test_non_gem_in_repo_skill_edit_gets_no_advisory(box):
    box.state = "vacant"
    box.anchor = str(box.doe)
    assert guard.check(box.payload(SKILL)) is None


def test_no_resolvable_anchor_leaves_the_leg_unmatched(box, monkeypatch):
    box.state = "vacant"
    monkeypatch.setattr(
        guard._write_bump_applicability, "resolve_launch_anchor", lambda *a, **k: None
    )
    assert guard.check(box.payload(SKILL)) is None


# --- advisory text ------------------------------------------------------------------


@pytest.mark.parametrize("state", ["live", "vacant"])
def test_doctrine_advisory_names_the_two_review_checks_and_no_human_route(box, state):
    box.state = state
    box.holder_sid = "someone-else"
    target = str(box.doe / "global-doctrine" / "CLAUDE.md")
    context = _context(guard.check(box.payload(target, sid="caller-2", agent_id="a1")))
    assert "byte budget" in context
    assert "no unprompted rewrite" in context
    for forbidden in ("PM", "sentinel", "SendMessage", "coordinator-queue-append"):
        assert forbidden not in context


def test_class_2_local_config_advisory_names_the_authority_check(box):
    target = str(box.repo / "coordinator.local.md")
    context = _context(guard.check(box.payload(target, sid="caller-2")))
    assert "Tier-U" in context
    assert "narrowed gate command" in context
    assert "always-loaded" not in context


# --- edit_admitted ------------------------------------------------------------------


def test_edit_admitted_non_protected_path_is_true(box):
    payload = box.payload(str(box.repo / "notes.md"))
    assert guard.edit_admitted(payload, str(box.repo / "notes.md")) is True


def test_edit_admitted_protected_path_needs_no_standing_or_sentinel(box):
    box.state = "vacant"
    target = str(box.repo / "CLAUDE.md")
    assert guard.edit_admitted(box.payload(target), target) is True


def test_edit_admitted_is_true_for_subagent_of_the_gem(box):
    payload = box.payload("global-doctrine/CLAUDE.md", agent_id="a1")
    assert guard.edit_admitted(payload, payload["tool_input"]["file_path"]) is True


def test_edit_admitted_fails_closed_on_an_exception(box, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(derived, "_is_derived_live_copy", boom)
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
