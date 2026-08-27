"""Behavioral tests for
coordinator_core.write_guards.nudge_sentinel_retained_review_sidecar
-- the sentinel-retained-but-filled advisory guard (see the module's own
docstring).

Spec backlink: cross-repo memo
  cross-repo/inbox/2026-08-06-example-market-data-repo-em-append-integrator-dispositions-refuses-every-reviewer-sidecar.md
  and commit 347a6a98f532.
"""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import (
    block_em_hand_edit_pending_review_integration as sibling_guard,
)
from coordinator_core.write_guards import (
    nudge_sentinel_retained_review_sidecar as guard,
)


_TARGET_FILE = "coordinator_core/write_guards/block_priority_ledger_edit.py"
_TARGET_BASENAME = "block_priority_ledger_edit.py"

_SENTINEL = (
    "<!-- One entry per finding: `- [severity] <finding> "
    "— disposition: accepted | rejected | deferred — rationale: ...` -->"
)

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


def _sentinel_retained_filled_body(mentions_target: bool = True) -> str:
    """Sentinel comment still present AND real findings content -- the
    coverage gap population this guard fires on."""
    citation = (
        f"- [P2] `{_TARGET_BASENAME}:42` unused import — disposition: accepted — "
        "rationale: dead import, safe to drop.\n\n"
        if mentions_target
        else "- [P2] `unrelated_module.py:10` unused import — disposition: accepted — "
        "rationale: dead import, safe to drop.\n\n"
    )
    return "## Findings\n\n" + _SENTINEL + "\n\n" + citation


def _pristine_scaffold_body() -> str:
    """Sentinel present, nothing else -- genuinely unfilled scaffold."""
    return "## Findings\n\n" + _SENTINEL + "\n\n"


def _sentinel_absent_filled_body() -> str:
    """Sentinel removed, real findings present -- sibling's own deny
    territory, not this guard's population."""
    return (
        "## Findings\n\n"
        f"- [P2] `{_TARGET_BASENAME}:42` unused import — disposition: accepted — "
        "rationale: dead import, safe to drop.\n\n"
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
    monkeypatch.delenv(sibling_guard._OVERRIDE_ENV_VAR, raising=False)


def _advise(payload):
    result = guard.check(payload)
    assert result is not None, "expected advisory"
    hso = result["hookSpecificOutput"]
    assert "additionalContext" in hso
    assert "permissionDecision" not in hso
    return result


def _silent(payload):
    result = guard.check(payload)
    assert result is None, f"expected silent (None), got {result!r}"


class TestFiresOnSentinelRetainedFilledFindings:
    def test_fires_when_sentinel_retained_but_findings_real_and_covers_target(
        self, tmp_path
    ):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        result = _advise(_payload(tmp_path))
        text = result["hookSpecificOutput"]["additionalContext"]
        assert "codereview-sliceA.md" in text
        assert "review-integrator" in text
        assert _TARGET_FILE in text

    def test_advisory_text_explains_warn_not_block(self, tmp_path):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        result = _advise(_payload(tmp_path))
        text = result["hookSpecificOutput"]["additionalContext"]
        assert "warning" in text.lower()
        assert "not a block" in text.lower()


class TestSilentOnPristineScaffold:
    def test_silent_when_sidecar_still_genuinely_unfilled(self, tmp_path):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_pristine_scaffold_body(),
        )
        _silent(_payload(tmp_path))


class TestSilentWhenSentinelAbsent:
    def test_silent_when_sentinel_already_removed(self, tmp_path):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_absent_filled_body(),
        )
        _silent(_payload(tmp_path))


class TestSilentWhenDispositionsPresent:
    def test_silent_once_integrator_dispositions_block_present(self, tmp_path):
        body = (
            _sentinel_retained_filled_body()
            + "## Integrator Dispositions\n\n- F1: Applied\n"
        )
        _write_sidecar(tmp_path, "sess-abc", "codereview-sliceA.md", body=body)
        _silent(_payload(tmp_path))


class TestScopeIsEmInlineOnly:
    def test_subagent_originated_edit_silent_unconditionally(self, tmp_path):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        _silent(_payload(tmp_path, agent_id="aexecutor-teammate-1234567890abcdef"))


class TestOverrideAndToolGating:
    def test_override_env_silences(self, tmp_path, monkeypatch):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        monkeypatch.setenv(sibling_guard._OVERRIDE_ENV_VAR, "1")
        _silent(_payload(tmp_path))

    def test_non_write_tool_silent(self, tmp_path):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        _silent(_payload(tmp_path, tool_name="Read"))


class TestCoverageHeuristic:
    def test_silent_when_sidecar_does_not_cover_target_file(self, tmp_path):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(mentions_target=False),
        )
        _silent(_payload(tmp_path))


class TestEnvelopeShape:
    def test_envelope_is_additional_context_only(self, tmp_path):
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        result = guard.check(_payload(tmp_path))
        assert result == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": result["hookSpecificOutput"]["additionalContext"],
            }
        }
