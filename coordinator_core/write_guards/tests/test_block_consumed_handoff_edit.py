
from __future__ import annotations

import re
import sys
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


_MATRIX_PAYLOAD_SHAPES = {
    "frontmatter_only": "deployment_state: shipped",
    "body_prose": "Some more progress notes were made today.",
    "heading": "## Progress\n\nDid some more work today.",
}


def _build_matrix_payload(repo_root: Path, tool_name: str, shape_text: str) -> dict:
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
        assert "recovery" not in reason.lower()
        assert "pretooluse-write-guards.md" not in reason

    def test_continuation_deny_names_correction_route(self, tmp_path, monkeypatch):
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
        assert "--sha" not in reason

    def test_close_intent_deny_unchanged_by_correction_route_addition(
        self, tmp_path, monkeypatch
    ):
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


# (`COORDINATOR_SESSION_ID` > `CLAUDE_SESSION_ID` > `CLAUDE_CODE_SESSION_ID`)
# -- not `COORDINATOR_SESSION_ID` alone, which is documented as the


class TestSessionIdentityResolution:
    @pytest.fixture(autouse=True)
    def _clear_session_env(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def test_holder_via_claude_code_session_id(self, tmp_path, monkeypatch):
        # CLAUDE_CODE_SESSION_ID (tier 3). Before the C18a reshape this was
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "some-prior-session")
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)
        assert result is None, "the holder leg is silent (2026-08-20 PM ruling)"

    def test_holder_via_claude_session_id(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "some-prior-session")
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)
        assert result is None, "the holder leg is silent (2026-08-20 PM ruling)"

    def test_coordinator_session_id_takes_precedence(self, tmp_path, monkeypatch):
        # COORDINATOR_SESSION_ID (tier 1) wins over a mismatched lower tier.
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "some-prior-session")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "a-different-session")
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)
        assert result is None, "the holder leg is silent (2026-08-20 PM ruling)"

    def test_non_holder_when_no_session_env_set(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "Not your claim" in reason


class TestHolderSplitBothDirections:
    @pytest.fixture(autouse=True)
    def _clear_session_env(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def test_holder_edit_is_silent(self, tmp_path, monkeypatch):
        repo_root, _ = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "some-prior-session")
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)

        assert result is None, (
            "holder's own claimed handoff must produce no envelope at all"
        )

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
        reason = self._non_holder_reason()
        assert "archive-stamp-cli claim-handoff" not in reason
        assert "/pickup" in reason or "pickup_assemble" in reason

    def test_correct_body_labeled_possession_not_authorship(self):
        reason = self._non_holder_reason()
        assert "handoff.correct_body" in reason
        assert "possession-gated" in reason
        assert "authorship-gated" not in reason

    def test_override_reason_attributed_to_correct_body_not_propagate(self):
        reason = self._non_holder_reason()
        correct_body_idx = reason.index("handoff.correct_body")
        propagate_idx = reason.index("handoff.propagate")
        override_idx = reason.index("override_reason")
        assert correct_body_idx < override_idx < propagate_idx
        propagate_clause = reason[propagate_idx:]
        assert "override_reason" not in propagate_clause


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

    def test_holder_edit_is_silent_for_subagent_too(self, tmp_path, monkeypatch):
        repo_root, _ = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "some-prior-session")
        result = guard.check(self._subagent_payload(repo_root))
        assert result is None


def _reason_for(repo_root: Path, new_strings: list[str]) -> str:
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
    return "--sha" in reason


class TestCloseIntentDiscrimination:
    @pytest.fixture(autouse=True)
    def _repo(self, tmp_path, monkeypatch):
        repo_root, _ = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        self.repo_root = repo_root

    def test_shipped_in_alone_is_close_intent(self):
        assert _is_close_route(_reason_for(self.repo_root, ["shipped_in: deadbeef"]))

    def test_abandoned_is_close_intent(self):
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


class TestGuardStillDenies:
    def test_claimed_handoff_edit_denied(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_legacy_consumed_handoff_edit_still_denied(self, tmp_path, monkeypatch):
        repo_root, handoff_path = _make_repo(tmp_path, body=_LEGACY_CONSUMED_BODY)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        payload = _payload(repo_root, "state/handoffs/2026-07-20_120000_abc.md")

        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestDriveRootContainmentGate:

    @pytest.mark.skipif(
        not sys.platform.startswith("win"),
        reason=(
            "hardware-gated: `_normalize_and_gate`'s containment leg calls "
            "`pathlib.Path(...).resolve()` on the candidate, not `os.path.*` -- "
            "on a POSIX interpreter this treats a Windows drive-root string "
            "('X:/...') as a relative path segment and resolves it against cwd "
            "instead of as an absolute drive path, so the assertion cannot hold "
            "regardless of the guard's correctness. Named as hardware-gated "
            "(not simulable via the ntpath swap used elsewhere in this suite) "
            "in test_windows_platform_simulation.py::"
            "test_windows_path_resolve_is_hardware_gated. Runs for real on a "
            "Windows host, where Path.resolve() natively treats 'X:/...' as "
            "drive-rooted."
        ),
    )
    def test_drive_root_git_root_still_matches(self):
        result = guard._normalize_and_gate(
            "state/handoffs/2026-07-20_120000_abc.md", "X:\\"
        )

        assert result is not None


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
    from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

    assert guard._TERMINAL_DEPLOYMENT_STATES is HANDOFF_TERMINAL_DEPLOYMENT


# deliberately EXCLUDED from this assertion; it is not owned by this repo

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


def _ac_body(criteria: str, kind: str | None = None) -> str:
    kind_line = f"kind: {kind}\n" if kind else ""
    return (
        "---\n"
        "status: claimed\n"
        "claimed_by: some-prior-session\n"
        f"{kind_line}"
        'title: "Ship the thing"\n'
        "---\n\n"
        "# Ship the thing\n\n"
        "## Acceptance criteria\n\n"
        f"{criteria}"
    )


_CLOSE_EDIT = "deployment_state: shipped"


class TestHolderCloseIsSilent:
    @pytest.fixture(autouse=True)
    def _holder_session(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "some-prior-session")

    def _check(self, tmp_path, monkeypatch, body: str):
        repo_root, _ = _make_repo(tmp_path, body=body)
        monkeypatch.setattr(guard, "_resolve_git_root", _resolve_root_for(repo_root))
        return guard.check(
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "state/handoffs/2026-07-20_120000_abc.md",
                    "old_string": "x",
                    "new_string": _CLOSE_EDIT,
                },
                "cwd": str(repo_root),
            }
        )

    def test_unticked_criteria_do_not_block_the_close(self, tmp_path, monkeypatch):
        result = self._check(
            tmp_path, monkeypatch, _ac_body("- [x] Landed\n- [ ] Not landed\n")
        )

        assert result is None, "unticked criteria are normal at handoff close"

    def test_all_ticked_closes_in_silence(self, tmp_path, monkeypatch):
        result = self._check(tmp_path, monkeypatch, _ac_body("- [x] One\n- [x] Two\n"))

        assert result is None

    def test_no_acceptance_criteria_heading_closes_in_silence(self, tmp_path, monkeypatch):
        result = self._check(tmp_path, monkeypatch, _CONSUMED_BODY)

        assert result is None

    def test_session_handoff_kind_closes_in_silence(self, tmp_path, monkeypatch):
        result = self._check(
            tmp_path,
            monkeypatch,
            _ac_body("- [ ] Not landed\n", kind="session-handoff"),
        )

        assert result is None

    def test_non_holder_close_is_still_denied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "a-different-session")
        result = self._check(tmp_path, monkeypatch, _ac_body("- [x] One\n"))

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "corrupts the audit trail" in (
            result["hookSpecificOutput"]["permissionDecisionReason"]
        )
