"""Behavioral tests for coordinator_core.write_guards.block_consumed_handoff_edit
-- the consumed-handoff hard-deny guard (see the module's own docstring for
the reference-hook port this is a faithful engine-ification of).

Covers the 2026-07-28 scaffold-removal fix (memo
cross-repo/inbox/2026-07-28-example-retrieval-repo-em-claimed-handoff-guard-should-not-prescaffold-successor.md):
the guard denies a claimed-handoff edit and OFFERS the available routes by
naming them in the deny message, but writes NOTHING to disk -- the prior
``_write_scaffold`` side effect (a pickup-eligible successor file written
before the operator chose among the message's named exits) was deleted
outright, not narrowed. ``TestGuardWritesNothing`` is the regression pin;
``TestDenyReasonRoutes`` pins the message content (no dangling
"pre-scaffolded" phrasing, the right verb for each intent).

Also covers the 2026-07-21 closure-friction fix (memo
cross-repo/inbox/2026-07-21-claude-central-em-consumed-handoff-guard-closure-friction.md,
Finding 1): both deny-reason strings offer a third, terminal-close route
(``archive-stamp-cli ship-handoff`` — DoE's veneer over
``handoff.archive_transition`` / ``mode: stamp_only``) alongside the
continuation route and the recovery-only override.

Plus regression coverage that the guard's strictness is unchanged: it still
denies a ``status: consumed`` edit unconditionally (no allow path was
added), and still passes through non-consumed / override-set edits.

The git-root-containment gate (module ``_resolve_git_root``) is monkeypatched
rather than exercised via a real ``git init`` -- the guard's containment
check only needs a resolvable root string that matches the tmp_path repo
layout; a real git repo adds no coverage value here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.write_guards import block_consumed_handoff_edit as guard


_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_CONSUMED_HANDOFF_EDIT"

_CONSUMED_BODY = """---
status: claimed
claimed_by: some-prior-session
title: "Ship the thing"
branch: work/example/2026-07-21
---

# Ship the thing

Some prior progress notes.
"""

# Deliberately old vocabulary -- exercises the guard's documented tolerance
# for legacy `status: consumed` on read (module docstring: "old-vocabulary
# `status: consumed` still recognized on read").
_LEGACY_CONSUMED_BODY = """---
status: consumed
consumed_by: some-prior-session
title: "Ship the thing"
branch: work/example/2026-07-21
---

# Ship the thing

Some prior progress notes.
"""

_ACTIVE_BODY = """---
status: open
title: "Still going"
---

# Still going
"""


def _make_repo(tmp_path: Path, handoff_name: str = "2026-07-20_120000_abc.md", body: str = _CONSUMED_BODY) -> tuple[Path, Path]:
    """Build ``<tmp_path>/state/handoffs/<handoff_name>`` with ``body`` and
    return ``(repo_root, handoff_path)``.
    """
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    handoff_path = handoffs_dir / handoff_name
    handoff_path.write_text(body, encoding="utf-8")
    return tmp_path, handoff_path


def _payload(repo_root: Path, rel_file_path: str, tool_name: str = "Edit") -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": rel_file_path, "old_string": "x", "new_string": "y"},
        "cwd": str(repo_root),
    }


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(_OVERRIDE_ENV, raising=False)


def _resolve_root_for(repo_root: Path):
    def _fake(cwd):
        return str(repo_root)

    return _fake


# ---------------------------------------------------------------------------
# 1. The guard writes nothing to disk (2026-07-28 scaffold-removal memo).
#
# This is THE regression pin for
# cross-repo/inbox/2026-07-28-example-retrieval-repo-em-claimed-handoff-guard-should-not-prescaffold-successor.md
# -- a blocked edit against a claimed handoff must not create, modify, or
# delete anything in the handoffs directory. Snapshot the directory listing
# before and after a blocked call and assert byte-identical contents.
# ---------------------------------------------------------------------------


def _snapshot(handoffs_dir: Path) -> dict:
    return {
        p.name: p.read_bytes()
        for p in sorted(handoffs_dir.iterdir())
    }


class TestGuardWritesNothing:
    def test_guard_writes_nothing_on_edit(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        handoffs_dir = tmp_path / "state" / "handoffs"
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))

        before = _snapshot(handoffs_dir)
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")
        result = guard.check(payload)
        after = _snapshot(handoffs_dir)

        assert result is not None
        assert after == before, "guard must write nothing to the handoffs dir"

    def test_guard_writes_nothing_on_progress_append_edit(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        handoffs_dir = tmp_path / "state" / "handoffs"
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))

        before = _snapshot(handoffs_dir)
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "state/handoffs/2026-07-20_120000_abc.md",
                "old_string": "x",
                "new_string": "## Progress\n\nDid some more work.",
            },
            "cwd": str(repo_root),
        }
        result = guard.check(payload)
        after = _snapshot(handoffs_dir)

        assert result is not None
        assert after == before, "guard must write nothing to the handoffs dir"

    def test_guard_writes_nothing_on_write(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        handoffs_dir = tmp_path / "state" / "handoffs"
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))

        before = _snapshot(handoffs_dir)
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/handoffs/2026-07-20_120000_abc.md",
                "content": "---\ndeployment_state: shipped\n---\n",
            },
            "cwd": str(repo_root),
        }
        result = guard.check(payload)
        after = _snapshot(handoffs_dir)

        assert result is not None
        assert after == before, "guard must write nothing to the handoffs dir"


# ---------------------------------------------------------------------------
# 1b. Deny-matrix strictness pin (AC6's real evidence).
#
# Per the plan (docs/plans/2026-07-31-claimed-baton-body-correction-route.md
# C5 / AC6), ``TestGuardStillDenies`` below is decision-only -- it asserts
# ``permissionDecision == "deny"`` for exactly one claimed and one
# legacy-consumed payload, and by itself is NOT sufficient evidence the
# guard "still denies every body edit it denies today". This matrix is the
# strictness pin over these three named axes: exhaustive over
# Write/Edit/MultiEdit x claimed/legacy-consumed x
# frontmatter-only/body-prose/heading payloads -- NOT exhaustive over every
# possible new_string shape or payload type (e.g. other frontmatter-only
# variants, mixed prose+heading edits, NotebookEdit payloads).
# Every cell must deny.
# ---------------------------------------------------------------------------


_MATRIX_PAYLOAD_SHAPES = {
    "frontmatter_only": "deployment_state: shipped",
    "body_prose": "Some more progress notes were made today.",
    "heading": "## Progress\n\nDid some more work today.",
}


def _build_matrix_payload(repo_root: Path, tool_name: str, shape_text: str) -> dict:
    """Build a Write/Edit/MultiEdit payload carrying ``shape_text`` as the
    tool's own content signal (``content`` for Write, ``new_string`` for
    Edit/MultiEdit)."""
    rel = "state/handoffs/2026-07-20_120000_abc.md"
    if tool_name == "Write":
        return {
            "tool_name": "Write",
            "tool_input": {"file_path": rel, "content": shape_text},
            "cwd": str(repo_root),
        }
    if tool_name == "Edit":
        return {
            "tool_name": "Edit",
            "tool_input": {"file_path": rel, "old_string": "x", "new_string": shape_text},
            "cwd": str(repo_root),
        }
    # MultiEdit
    return {
        "tool_name": "MultiEdit",
        "tool_input": {
            "edits": [{"file_path": rel, "old_string": "x", "new_string": shape_text}]
        },
        "cwd": str(repo_root),
    }


class TestDenyMatrixStrictnessPin:
    @pytest.mark.parametrize("tool_name", ["Write", "Edit", "MultiEdit"])
    @pytest.mark.parametrize(
        "status_body",
        [_CONSUMED_BODY, _LEGACY_CONSUMED_BODY],
        ids=["claimed", "legacy-consumed"],
    )
    @pytest.mark.parametrize(
        "shape_name,shape_text", list(_MATRIX_PAYLOAD_SHAPES.items())
    )
    def test_denies_every_combination(
        self, tmp_path, monkeypatch, tool_name, status_body, shape_name, shape_text
    ):
        repo_root, _ = _make_repo(tmp_path, body=status_body)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = _build_matrix_payload(repo_root, tool_name, shape_text)

        result = guard.check(payload)

        assert result is not None, f"{tool_name}/{shape_name} unexpectedly passed through"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# 2. Deny reasons name the right routes and never claim a scaffold exists.
# ---------------------------------------------------------------------------


class TestDenyReasonRoutes:
    def test_continuation_deny_names_handoff_route(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]

        assert "/handoff" in reason
        assert "pre-scaffolded" not in reason
        assert "archive-stamp-cli ship-handoff" in reason
        assert "deployment_state: shipped" in reason
        assert "shipped_in" in reason
        # No unlock-exists statement (2026-08-13 guard-messages-stop-handing
        # -agents-the-keys AC-1/AC-2): the deny used to name the ship-handoff
        # override as "recovery"-only and point at
        # docs/wiki/pretooluse-write-guards.md unconditionally, for every
        # audience including a dispatched subagent -- itself a banned
        # unlock-exists-statement/doc-pointer shape. Removed; the route
        # (ship-handoff) stays named, only the override mention is gone.
        assert "recovery" not in reason.lower()
        assert "pretooluse-write-guards.md" not in reason

    def test_continuation_deny_names_correction_route(self, tmp_path, monkeypatch):
        # C4/AC7: the continuation deny additively names handoff.correct_body
        # alongside /handoff and ship-handoff.
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]

        assert "handoff.correct_body" in reason
        assert "coordinator_core.invoke" in reason
        assert "possession-gated" in reason
        assert "authorship-gated" not in reason
        # Regression guard (AC6 / plan C5): TestCloseIntentDiscrimination
        # below routes on `_is_close_route(reason) = "--sha" in reason`
        # (see this file, TestCloseIntentDiscrimination). If an unrelated
        # text edit to the continuation reason ever introduced the literal
        # substring `--sha`, it would flip six close-intent tests red in a
        # way indistinguishable from a real regression in `_is_close_intent`
        # itself -- this assertion makes that failure mode legible instead.
        assert "--sha" not in reason

    def test_close_intent_deny_unchanged_by_correction_route_addition(
        self, tmp_path, monkeypatch
    ):
        # The close-intent deny is untouched by the C4 correction-route
        # addition -- it still routes to ship-handoff only, and does not
        # mention the new author-correction route (that offer belongs to
        # the continuation branch only, per AC7).
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "state/handoffs/2026-07-20_120000_abc.md",
                "old_string": "deployment_state: in_flight",
                "new_string": "deployment_state: shipped\nshipped_in: deadbeef",
            },
            "cwd": str(repo_root),
        }

        result = guard.check(payload)
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]

        assert "archive-stamp-cli ship-handoff" in reason
        assert "--sha" in reason
        assert "handoff.correct_body" not in reason

    def test_close_intent_deny_routes_to_ship_op(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "state/handoffs/2026-07-20_120000_abc.md",
                "old_string": "deployment_state: in_flight",
                "new_string": "deployment_state: shipped\nshipped_in: deadbeef",
            },
            "cwd": str(repo_root),
        }

        result = guard.check(payload)
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]

        assert "archive-stamp-cli ship-handoff" in reason
        assert "--sha" in reason
        assert "pre-scaffolded" not in reason
        assert "_successor-of-" not in reason


# ---------------------------------------------------------------------------
# 2b. Session-identity resolution (AC7, chain-review slice D F1/F2/F3/F4).
#
# `is_holder` must resolve the calling session's id through the SAME
# three-variable precedence `handoff_correct_body` walks
# (`COORDINATOR_SESSION_ID` > `CLAUDE_SESSION_ID` > `CLAUDE_CODE_SESSION_ID`)
# -- not `COORDINATOR_SESSION_ID` alone, which is documented as the
# "explicit test override" tier and is unset in every real session.
# ---------------------------------------------------------------------------


class TestSessionIdentityResolution:
    @pytest.fixture(autouse=True)
    def _clear_session_env(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def test_holder_via_claude_code_session_id(self, tmp_path, monkeypatch):
        # The load-bearing case: a real session carries ONLY
        # CLAUDE_CODE_SESSION_ID (tier 3). Before the C18a reshape this was
        # classified a non-holder unconditionally (and, before that fix,
        # denied even when correctly classified a holder).
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "some-prior-session")
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)
        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "you hold this claim" in reason.lower()

    def test_holder_via_claude_session_id(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "some-prior-session")
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)
        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "you hold this claim" in reason.lower()

    def test_coordinator_session_id_takes_precedence(self, tmp_path, monkeypatch):
        # COORDINATOR_SESSION_ID (tier 1) wins over a mismatched lower tier.
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "some-prior-session")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "a-different-session")
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)
        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "you hold this claim" in reason.lower()

    def test_non_holder_when_no_session_env_set(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "Not your claim" in reason


# ---------------------------------------------------------------------------
# 2d. C18a reshape pin (2026-08-06 apply-guard-class-census, C18a): the guard
# splits on the holder predicate it already computed -- deny where the
# calling session is NOT the claim holder, advise (non-blocking) where it
# IS. Both directions pinned explicitly and side by side so a regression on
# either leg (e.g. accidentally denying the holder again, or accidentally
# advising a non-holder) fails loudly here rather than only via the
# `additionalContext`/`permissionDecision` assertions scattered above.
# ---------------------------------------------------------------------------


class TestHolderSplitBothDirections:
    @pytest.fixture(autouse=True)
    def _clear_session_env(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def test_holder_edit_is_advisory_not_blocking(self, tmp_path, monkeypatch):
        repo_root, _ = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "some-prior-session")
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)

        assert result is not None, "holder edit must still surface guidance"
        hso = result["hookSpecificOutput"]
        assert "permissionDecision" not in hso, (
            "holder's own claimed handoff must not be hard-denied"
        )
        assert "additionalContext" in hso
        assert "handoff.correct_body" in hso["additionalContext"]

    def test_non_holder_edit_is_still_hard_denied(self, tmp_path, monkeypatch):
        repo_root, _ = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "a-different-session")
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)

        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "permissionDecisionReason" in hso


# ---------------------------------------------------------------------------
# 2c. Non-holder remedy content (F2, F3, F4): pickup path, possession label,
# correctly-attributed override_reason.
# ---------------------------------------------------------------------------


class TestNonHolderRemedyContent:
    @pytest.fixture(autouse=True)
    def _repo(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        repo_root, _ = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        self.repo_root = repo_root

    def _non_holder_reason(self) -> str:
        payload = _payload(self.repo_root, "state/handoffs/2026-07-20_120000_abc.md")
        result = guard.check(payload)
        assert result is not None
        return result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_names_pickup_path_not_bare_claim_handoff(self):
        # F2: the lead remedy must not be the liveness-blind
        # `archive-stamp-cli claim-handoff` verb (stale-claim takeover path).
        reason = self._non_holder_reason()
        assert "archive-stamp-cli claim-handoff" not in reason
        assert "/pickup" in reason or "pickup_assemble" in reason

    def test_correct_body_labeled_possession_not_authorship(self):
        # F3
        reason = self._non_holder_reason()
        assert "handoff.correct_body" in reason
        assert "possession-gated" in reason
        assert "authorship-gated" not in reason

    def test_override_reason_attributed_to_correct_body_not_propagate(self):
        # F4: override_reason belongs to handoff.correct_body;
        # handoff.propagate has no such param and no gate.
        reason = self._non_holder_reason()
        correct_body_idx = reason.index("handoff.correct_body")
        propagate_idx = reason.index("handoff.propagate")
        override_idx = reason.index("override_reason")
        # override_reason must appear between the correct_body mention and
        # the propagate mention (i.e. attached to correct_body's clause).
        assert correct_body_idx < override_idx < propagate_idx
        # propagate's own clause must not re-claim override_reason.
        propagate_clause = reason[propagate_idx:]
        assert "override_reason" not in propagate_clause


# ---------------------------------------------------------------------------
# 2d. No unlock-exists statement, any branch, any audience (2026-08-13
# guard-messages-stop-handing-agents-the-keys, AC-1/AC-2). Every named route
# below (ship-handoff, correct_body, propagate, link_stubs) is a legitimate
# op any audience may invoke -- not this guard's own escape hatch -- so
# naming them is not a leak. What must never appear, for a dispatched
# subagent OR an unresolved/EM audience (this guard's deny text is
# audience-invariant, per `guard_settings_json_write`'s established
# pattern): the "recovery"-only override framing and its
# docs/wiki/pretooluse-write-guards.md pointer that used to sit on the
# ship-handoff mention in both the holder and non-holder branches.
# ---------------------------------------------------------------------------


class TestNoUnlockExistsStatement:
    @pytest.fixture(autouse=True)
    def _clear_session_env(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def _subagent_payload(self, repo_root: Path) -> dict:
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")
        payload["agent_id"] = "coordinatorexecutor-5a61a636"
        return payload

    def test_non_holder_deny_carries_no_mechanism_for_subagent(self, tmp_path, monkeypatch):
        repo_root, _ = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "a-different-session")
        result = guard.check(self._subagent_payload(repo_root))
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "recovery" not in reason.lower()
        assert "pretooluse-write-guards.md" not in reason

    def test_holder_advisory_carries_no_mechanism_for_subagent(self, tmp_path, monkeypatch):
        repo_root, _ = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "some-prior-session")
        result = guard.check(self._subagent_payload(repo_root))
        assert result is not None
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "recovery" not in reason.lower()
        assert "pretooluse-write-guards.md" not in reason


# ---------------------------------------------------------------------------
# 3. Close-intent discrimination (``_is_close_intent``).
#
# With the scaffold write gone, this predicate's only observable effect is
# WHICH of the two deny messages fires -- ship-op route vs `/handoff`
# continuation route. It stays deliberately conservative: every replacement
# must be frontmatter-shaped AND at least one must assert a terminal
# disposition, so a progress append that merely mentions `shipped_in` in
# prose is a continuation.
# ---------------------------------------------------------------------------


def _reason_for(repo_root: Path, new_strings: list[str]) -> str:
    """Deny reason for an Edit (one string) / MultiEdit (many) payload."""
    rel = "state/handoffs/2026-07-20_120000_abc.md"
    if len(new_strings) == 1:
        tool_input = {"file_path": rel, "old_string": "x", "new_string": new_strings[0]}
        tool_name = "Edit"
    else:
        tool_input = {
            "edits": [
                {"file_path": rel, "old_string": "x", "new_string": ns}
                for ns in new_strings
            ]
        }
        tool_name = "MultiEdit"
    result = guard.check(
        {"tool_name": tool_name, "tool_input": tool_input, "cwd": str(repo_root)}
    )
    assert result is not None
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _is_close_route(reason: str) -> bool:
    """Did the CLOSE branch fire? It is the only one carrying `--sha`."""
    return "--sha" in reason


class TestCloseIntentDiscrimination:
    @pytest.fixture(autouse=True)
    def _repo(self, tmp_path, monkeypatch):
        repo_root, _ = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        self.repo_root = repo_root

    def test_shipped_in_alone_is_close_intent(self):
        # A close-only key is terminal on its own, whatever else the edit carries.
        assert _is_close_route(_reason_for(self.repo_root, ["shipped_in: deadbeef"]))

    def test_abandoned_is_close_intent(self):
        # Abandoning is a close too -- the four-value terminal set, not three.
        assert _is_close_route(
            _reason_for(self.repo_root, ["deployment_state: abandoned"])
        )

    def test_multiedit_all_frontmatter_is_close_intent(self):
        assert _is_close_route(
            _reason_for(
                self.repo_root,
                ["deployment_state: shipped", "shipped_in_kind: commit"],
            )
        )

    def test_prose_mentioning_shipped_in_is_not_a_close(self):
        # Conservative by design: a body line disqualifies the whole edit.
        assert not _is_close_route(
            _reason_for(
                self.repo_root,
                ["We finally set shipped_in on the other baton today."],
            )
        )

    def test_non_terminal_deployment_state_is_not_a_close(self):
        assert not _is_close_route(
            _reason_for(self.repo_root, ["deployment_state: in_flight"])
        )

    def test_multiedit_with_one_body_edit_is_not_a_close(self):
        assert not _is_close_route(
            _reason_for(
                self.repo_root,
                ["deployment_state: shipped", "## Progress\n\nMore work."],
            )
        )


# ---------------------------------------------------------------------------
# 4. Guard still denies (regression -- strictness unchanged)
# ---------------------------------------------------------------------------


class TestGuardStillDenies:
    def test_claimed_handoff_edit_denied(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_legacy_consumed_handoff_edit_still_denied(self, tmp_path, monkeypatch):
        # Old-vocabulary tolerance: `status: consumed` must still deny.
        repo_root, handoff_path = _make_repo(tmp_path, body=_LEGACY_CONSUMED_BODY)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestDriveRootContainmentGate:
    """Discriminates the drive-root trailing-backslash defect directly:
    a `git_root` of a bare Windows drive root (`X:\\`) previously composed a
    double-slash `expected_prefix` (`rstrip("/")` does not strip a trailing
    backslash), which never matched the single-slash form `Path.resolve()`
    produces on the candidate side -- the gate went silently inert and
    `_normalize_and_gate` returned `None` for every candidate. Proven to
    fail against the pre-fix `rstrip("/")` spelling before this fix landed.
    """

    def test_drive_root_git_root_still_matches(self):
        result = guard._normalize_and_gate(
            "state/handoffs/2026-07-20_120000_abc.md", "X:\\"  # abs-path-ok: synthetic drive-root literal, not a repo path citation
        )

        assert result is not None


# ---------------------------------------------------------------------------
# 5. Pass-through behavior preserved
# ---------------------------------------------------------------------------


class TestPassThrough:
    def test_active_handoff_passes_through(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(
            tmp_path, handoff_name="2026-07-20_120000_active.md", body=_ACTIVE_BODY
        )
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_active.md")

        assert guard.check(payload) is None

    def test_override_env_set_passes_through(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv(_OVERRIDE_ENV, "1")
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        assert guard.check(payload) is None


def test_terminal_states_are_the_ssot_enum():
    """Guard against a hand-copied drift of the lifecycle enum."""
    from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

    assert guard._TERMINAL_DEPLOYMENT_STATES is HANDOFF_TERMINAL_DEPLOYMENT


# ---------------------------------------------------------------------------
# 6. AC8 citation-liveness pin, scoped to this repo's docs/wiki/ only.
#
# The deny text also cites `skills/pickup/SKILL.md` -- that lives in the
# coordinator-claude plane (this repo has no `skills/` tree) and is
# deliberately EXCLUDED from this assertion; it is not owned by this repo
# and this test must not be widened to cover it (per the plan's C5 body and
# C4's chunk report, which verifies that citation by hand against the
# coordinator-claude tree instead).
# ---------------------------------------------------------------------------

_DOCS_WIKI_CITATION_RE = re.compile(r"docs/wiki/[A-Za-z0-9_\-./]+\.md")


class TestDocsWikiCitationsLive:
    def test_docs_wiki_citations_resolve_on_disk(self, tmp_path, monkeypatch):
        """Any docs/wiki/*.md citation still present in the deny text must
        resolve on disk -- but the deny text is no longer GUARANTEED to
        carry one. 2026-08-13 (guard-messages-stop-handing-agents-the-keys,
        AC-1/AC-2): the ship-handoff mention's `docs/wiki/pretooluse-write-
        guards.md` pointer was itself the unlock-exists-statement/doc-
        pointer leak this guard's row was flagged for -- it named the
        "recovery"-only override and pointed at the doc describing it,
        unconditionally, for every audience including a dispatched
        subagent. Removed outright rather than gated, since this deny is
        audience-invariant (no payload-borne EM/subagent split threaded
        through these two branches). The prior `assert cited` (any docs/wiki
        citation must exist) pinned the very leak being fixed here; this
        test now only pins liveness for whatever citations remain, if any.
        """
        repo_root, _ = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))

        continuation_reason = _reason_for(repo_root, ["## Progress\n\nMore work."])
        close_reason = _reason_for(repo_root, ["deployment_state: shipped"])

        cited = set(
            _DOCS_WIKI_CITATION_RE.findall(continuation_reason)
            + _DOCS_WIKI_CITATION_RE.findall(close_reason)
        )

        claude_klabauter_root = Path(__file__).resolve().parents[3]
        for rel in cited:
            assert (claude_klabauter_root / rel).is_file(), f"dead citation: {rel}"
