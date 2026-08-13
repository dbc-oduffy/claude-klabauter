"""Behavioral tests for
coordinator_core.write_guards.block_em_hand_edit_pending_review_integration
-- the review-integrator-required guard (see the module's own docstring).

Spec backlink: coordinator-claude
  docs/plans/2026-07-27-claude-md-altitude-triage.md § C14
  (chunk id REVIEW-INTEGRATOR-REQUIRED-GUARD)
"""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import (
    block_em_hand_edit_pending_review_integration as guard,
)


_TARGET_FILE = "coordinator_core/write_guards/block_priority_ledger_edit.py"
_TARGET_BASENAME = "block_priority_ledger_edit.py"

_FINDINGS_FRONTMATTER = (
    "---\n"
    "status: open\n"
    "agent_type: coordinator:code-reviewer\n"
    "spawned_at: 2026-07-27T00:00:00Z\n"
    "lead_session_id: sess-abc\n"
    "divergence:\n"
    "  diverged: false\n"
    "commits: []\n"
    "dispatch_feed: null\n"
    "---\n\n"
)


def _findings_body(mentions_target: bool = True) -> str:
    citation = (
        f"- [P2] `{_TARGET_BASENAME}:42` unused import — disposition: accepted — "
        "rationale: dead import, safe to drop.\n\n"
        if mentions_target
        else "- [P2] `unrelated_module.py:10` unused import — disposition: accepted — "
        "rationale: dead import, safe to drop.\n\n"
    )
    return "## Findings\n\n" + citation


def _unfilled_findings_body() -> str:
    return (
        "## Findings\n\n"
        "<!-- One entry per finding: `- [severity] <finding> "
        "— disposition: accepted | rejected | deferred — rationale: ...` -->\n\n"
    )


def _write_sidecar(
    tmp_path,
    session_id: str,
    filename: str,
    *,
    agent_type: str = "coordinator:code-reviewer",
    body: str,
) -> None:
    sidecar_dir = tmp_path / "state" / "subagent-share" / session_id
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = _FINDINGS_FRONTMATTER.replace(
        "agent_type: coordinator:code-reviewer", f"agent_type: {agent_type}"
    )
    (sidecar_dir / filename).write_text(frontmatter + body, encoding="utf-8")


def _payload(
    tmp_path,
    file_path: str = _TARGET_FILE,
    *,
    agent_id: str = "",
    session_id: str = "sess-abc",
    tool_name: str = "Edit",
) -> dict:
    payload = {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path, "old_string": "x", "new_string": "y"},
        "cwd": str(tmp_path),
        "session_id": session_id,
    }
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)


def _advise(payload):
    result = guard.check(payload)
    assert result is not None, "expected ADVISORY"
    hook_output = result["hookSpecificOutput"]
    assert "permissionDecision" not in hook_output
    assert hook_output["additionalContext"]
    return result


def _allow(payload):
    result = guard.check(payload)
    assert result is None, f"expected ALLOW, got {result!r}"


# ---------------------------------------------------------------------------
# Core deny/allow behavior
# ---------------------------------------------------------------------------


class TestDeniesEmHandEditWithUnaddressedFinding:
    def test_em_edit_to_file_with_unintegrated_finding_denied(self, tmp_path):
        _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md", body=_findings_body()
        )
        _advise(_payload(tmp_path))

    def test_deny_reason_names_target_sidecar_and_override(self, tmp_path):
        """Negative-spec: the override KEY must never appear in the message
        (``docs/wiki/guard-messaging.md`` § Register, B6). The compliant shape
        is a pointer to the override-key doc, which is what
        ``operator_override_note`` has rendered since the 2026-08-11 reshape
        dropped the pasteable ``VAR=1`` literal."""
        _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md", body=_findings_body()
        )
        result = _advise(_payload(tmp_path))
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert _TARGET_FILE in reason
        assert "codereview-sliceA.md" in reason
        assert "review-integrator" in reason
        assert "guard-override-keys.md" in reason
        assert guard._OVERRIDE_ENV_VAR not in reason

    def test_deny_reason_names_the_integrator_unavailable_exit(self, tmp_path):
        """The advisory states no blind absolute: a genuinely dead integrator
        has a named exit, and the message points at it rather than leaving the
        EM to pick between a silent override and a shipped defect."""
        _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md", body=_findings_body()
        )
        result = _advise(_payload(tmp_path))
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "re-dispatch once" in reason
        assert "park with an owner" in reason
        assert "fresh-disk re-check" in reason

    def test_write_tool_also_denied(self, tmp_path):
        _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md", body=_findings_body()
        )
        payload = _payload(tmp_path, tool_name="Write")
        payload["tool_input"] = {"file_path": _TARGET_FILE, "content": "x"}
        _advise(payload)


class TestAllowsAfterIntegration:
    def test_em_edit_allowed_once_dispositions_block_present(self, tmp_path):
        body = _findings_body() + "## Integrator Dispositions\n\n- F1: Applied\n"
        _write_sidecar(tmp_path, "sess-abc", "codereview-sliceA.md", body=body)
        _allow(_payload(tmp_path))


class TestAllowsUnfilledScaffold:
    def test_em_edit_allowed_when_sidecar_still_unfilled(self, tmp_path):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_unfilled_findings_body(),
        )
        _allow(_payload(tmp_path))

    def test_em_edit_allowed_when_no_findings_heading_at_all(self, tmp_path):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "run-report.md",
            agent_type="coordinator:code-reviewer",
            body="## Run notes\n\nNothing here.\n",
        )
        _allow(_payload(tmp_path))


class TestScopeIsEmInlineOnly:
    def test_subagent_originated_edit_allowed_unconditionally(self, tmp_path):
        _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md", body=_findings_body()
        )
        _allow(_payload(tmp_path, agent_id="aexecutor-teammate-1234567890abcdef"))


class TestAgentTypeGate:
    def test_non_reviewer_sidecar_never_trips_guard(self, tmp_path):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "run-report.md",
            agent_type="coordinator:review-integrator",
            body=_findings_body(),
        )
        _allow(_payload(tmp_path))


class TestCoverageHeuristic:
    def test_sidecar_not_mentioning_target_file_allowed(self, tmp_path):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_findings_body(mentions_target=False),
        )
        _allow(_payload(tmp_path))

    def test_full_normalized_path_match_also_denies(self, tmp_path):
        body = (
            "## Findings\n\n"
            f"- [P1] `{_TARGET_FILE}:5` bug — disposition: accepted — rationale: x.\n\n"
        )
        _write_sidecar(tmp_path, "sess-abc", "codereview-sliceA.md", body=body)
        _advise(_payload(tmp_path))


class TestSessionScoping:
    def test_sidecar_in_different_session_directory_not_seen(self, tmp_path):
        _write_sidecar(
            tmp_path, "sess-OTHER", "codereview-sliceA.md", body=_findings_body()
        )
        _allow(_payload(tmp_path, session_id="sess-abc"))

    def test_missing_session_id_fails_open(self, tmp_path):
        _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md", body=_findings_body()
        )
        payload = _payload(tmp_path, session_id="")
        payload.pop("session_id", None)
        _allow(payload)

    def test_missing_sidecar_dir_fails_open(self, tmp_path):
        _allow(_payload(tmp_path))

    def test_unsafe_session_id_fails_open(self, tmp_path):
        _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md", body=_findings_body()
        )
        _allow(_payload(tmp_path, session_id="../sess-abc"))


class TestOverrideAndToolGating:
    def test_override_env_allows(self, tmp_path, monkeypatch):
        _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md", body=_findings_body()
        )
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")
        _allow(_payload(tmp_path))

    def test_non_write_tool_allowed(self, tmp_path):
        _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md", body=_findings_body()
        )
        _allow(_payload(tmp_path, tool_name="Read"))
