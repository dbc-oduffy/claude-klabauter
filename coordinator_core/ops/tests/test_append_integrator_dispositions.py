"""Tests for coordinator_core.ops.append_integrator_dispositions.

Covers: the correct-usage append path unblocks the sibling guard's own
substring check; wrong-target-file misuse (own run-report, non-reviewer
sidecar, unfilled scaffold, non-state/subagent-share path) is refused loudly
rather than silently degrading; and calling twice is a safe no-op rather than
a duplicate/corrupted block.

Spec backlink: see coordinator_core/ops/append_integrator_dispositions.py
module docstring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import append_integrator_dispositions as mod
from coordinator_core.write_guards import (
    block_em_hand_edit_pending_review_integration as guard,
)

_FINDINGS_FRONTMATTER = (
    "---\n"
    "status: open\n"
    "agent_type: {agent_type}\n"
    "spawned_at: 2026-07-27T00:00:00Z\n"
    "lead_session_id: sess-abc\n"
    "divergence:\n"
    "  diverged: false\n"
    "commits: []\n"
    "dispatch_feed: null\n"
    "---\n\n"
)

_FINDINGS_BODY = (
    "## Findings\n\n"
    "- [P2] `some_file.py:42` unused import — disposition: accepted — "
    "rationale: dead import, safe to drop.\n\n"
)

_UNFILLED_FINDINGS_BODY = (
    "## Findings\n\n"
    "<!-- One entry per finding: `- [severity] <finding> "
    "— disposition: accepted | rejected | deferred — rationale: ...` -->\n\n"
)


def _write_sidecar(root: Path, session_id: str, filename: str, *, agent_type: str, body: str) -> Path:
    sidecar_dir = root / "state" / "subagent-share" / session_id
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    path = sidecar_dir / filename
    path.write_text(_FINDINGS_FRONTMATTER.format(agent_type=agent_type) + body, encoding="utf-8")
    return path


class TestAppendSucceedsAndUnblocksGuard:
    def test_valid_reviewer_sidecar_gets_block_appended(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        result = mod.append_dispositions(
            sidecar, {"applied": ["F1"]}, git_root=tmp_path,
        )
        assert result["already_dispositioned"] is False
        text = sidecar.read_text(encoding="utf-8")
        assert "## Integrator Dispositions" in text
        assert "applied: [F1]" in text

    def test_appended_block_unblocks_the_sibling_guard(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        target_file = "some_file.py"
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": target_file, "old_string": "x", "new_string": "y"},
            "cwd": str(tmp_path),
            "session_id": "sess-abc",
        }
        # Before the disposition write, the guard denies.
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

        mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)

        # After, the guard allows.
        assert guard.check(payload) is None

    def test_rationale_is_included_when_supplied(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        mod.append_dispositions(
            sidecar, {"escalated-disagree": ["F4"]},
            rationale="reviewer's fix would reintroduce a precedence bug.",
            git_root=tmp_path,
        )
        text = sidecar.read_text(encoding="utf-8")
        assert "### Rationale" in text
        assert "precedence bug" in text

    def test_all_five_buckets_render_in_canonical_order(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        mod.append_dispositions(
            sidecar,
            {
                "applied": ["F1", "F2"],
                "escalated-disagree": ["F4"],
                "escalated-ask": ["F6", "F9"],
                "escalated-p0": [],
                "deferred": ["F8"],
                "verified-no-action": ["F10"],
            },
            git_root=tmp_path,
        )
        text = sidecar.read_text(encoding="utf-8")
        order = [
            "applied:",
            "escalated-disagree:",
            "escalated-ask:",
            "escalated-p0:",
            "deferred:",
            "verified-no-action:",
        ]
        positions = [text.index(k) for k in order]
        assert positions == sorted(positions)

    def test_verified_no_action_bucket_round_trips(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        mod.append_dispositions(
            sidecar, {"verified-no-action": ["F7"]}, git_root=tmp_path,
        )
        text = sidecar.read_text(encoding="utf-8")
        assert "verified-no-action: [F7]" in text

    def test_five_bucket_only_block_is_byte_identical_to_pre_extension_shape(self, tmp_path):
        """Pins BUCKET_ORDER's append-at-end placement: a block using only the
        original five buckets must emit exactly what it emitted before
        `verified-no-action` existed — no new bucket line, no reordering of
        the existing five. Fails if BUCKET_ORDER is ever reordered so that
        `verified-no-action` lands anywhere but last."""
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        mod.append_dispositions(
            sidecar,
            {
                "applied": ["F1", "F2"],
                "escalated-disagree": ["F4"],
                "escalated-ask": ["F6", "F9"],
                "escalated-p0": [],
                "deferred": ["F8"],
            },
            git_root=tmp_path,
        )
        text = sidecar.read_text(encoding="utf-8")
        expected_block = (
            "\n"
            "---\n"
            "\n"
            "## Integrator Dispositions\n"
            "\n"
            "```yaml\n"
            "schema_version: 1\n"
            "applied: [F1, F2]\n"
            "escalated-disagree: [F4]\n"
            "escalated-ask: [F6, F9]\n"
            "escalated-p0: []\n"
            "deferred: [F8]\n"
            "```\n"
        )
        assert text.endswith(expected_block)
        assert "verified-no-action" not in text


class TestFindingsCheckIsScopedToTheFindingsSection:
    """Pins the false-positive fix: the guard must key off whether the
    `## Findings` section itself is filled, never a whole-file substring
    search for the scaffold sentinel."""

    def test_filled_findings_appendable_even_when_sentinel_text_survives_elsewhere(self, tmp_path):
        body = (
            "## Findings\n\n"
            "- [P2] `some_file.py:42` unused import — disposition: accepted — "
            "rationale: dead import, safe to drop.\n\n"
            "## Notes\n\n"
            "Template remnant left below by mistake: "
            + mod._FINDINGS_SENTINEL
            + "\n"
        )
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=body,
        )
        result = mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is False
        text = sidecar.read_text(encoding="utf-8")
        assert "## Integrator Dispositions" in text

    def test_unfilled_scaffold_still_refused_with_remedy_message(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_UNFILLED_FINDINGS_BODY,
        )
        with pytest.raises(mod.DispositionsError, match="unfilled scaffold") as excinfo:
            mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        message = str(excinfo.value)
        assert "have the reviewer replace" in message
        assert "## Findings" in message

    def test_real_reviewer_summary_and_finding_subheadings_appendable(self, tmp_path):
        """Pins the real producer layout: `## Findings` immediately followed
        by `## Summary`, prose, and `### Finding 1: <title>` with severity
        lines, before `## Exit interview` — must not be mistaken for an
        empty section just because sub-headings intervene."""
        body = (
            "## Findings\n\n"
            "## Summary\n\n"
            "Reviewed the diff for correctness and found one issue.\n\n"
            "### Finding 1: Off-by-one in loop bound\n"
            "- **Severity:** P1\n"
            "- **File:** some_file.py:42\n"
            "- **Detail:** loop bound is exclusive when it should be inclusive.\n\n"
            "## Exit interview\n\n"
            "Nothing further.\n"
        )
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=body,
        )
        result = mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is False
        text = sidecar.read_text(encoding="utf-8")
        assert "## Integrator Dispositions" in text

    def test_finding_body_with_horizontal_rule_still_appendable(self, tmp_path):
        """Regression pin for dropping the `---` boundary: a `---` rule
        inside the findings body (between findings) must not truncate the
        section and cause a false empty-scaffold refusal."""
        body = (
            "## Findings\n\n"
            "## Summary\n\n"
            "### Finding 1: Off-by-one\n"
            "- **Severity:** P1\n\n"
            "---\n\n"
            "### Finding 2: Missing null check\n"
            "- **Severity:** P2\n\n"
            "## Exit interview\n\n"
            "Nothing further.\n"
        )
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=body,
        )
        result = mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is False
        text = sidecar.read_text(encoding="utf-8")
        assert "## Integrator Dispositions" in text


class TestIdempotentSecondCall:
    def test_second_call_is_a_safe_noop(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        first_text = sidecar.read_text(encoding="utf-8")

        result = mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is True
        second_text = sidecar.read_text(encoding="utf-8")
        assert first_text == second_text
        assert second_text.count("## Integrator Dispositions") == 1

    def test_already_dispositioned_real_reviewer_layout_is_clean_noop(self, tmp_path):
        """Reorder pin: with the emptiness check running before the
        idempotency check, an already-dispositioned sidecar in the real
        `## Summary`/`### Finding N` layout would spuriously refuse instead
        of no-opping, since the whole document (findings body plus the
        already-appended block) no longer looks like the pristine scaffold
        but the boundary logic must still find a non-empty section. This
        must fail loudly on the old (pre-reorder) ordering."""
        body = (
            "## Findings\n\n"
            "## Summary\n\n"
            "### Finding 1: Off-by-one in loop bound\n"
            "- **Severity:** P1\n\n"
            "## Exit interview\n\n"
            "Nothing further.\n"
        )
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=body,
        )
        mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        result = mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is True


class TestFailsLoudOnMisdirection:
    def test_refuses_own_run_report_sidecar_wrong_agent_type(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "coordinatorreview-integrator-abc.md",
            agent_type="coordinator:review-integrator", body="## Run notes\n\nDid stuff.\n",
        )
        with pytest.raises(mod.DispositionsError, match="agent_type"):
            mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)

    def test_refuses_unfilled_scaffold(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_UNFILLED_FINDINGS_BODY,
        )
        with pytest.raises(mod.DispositionsError, match="unfilled scaffold"):
            mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)

    def test_refuses_missing_findings_heading(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body="## Run notes\n\nnothing\n",
        )
        with pytest.raises(mod.DispositionsError, match="Findings"):
            mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)

    def test_refuses_path_outside_subagent_share(self, tmp_path):
        outside = tmp_path / "docs" / "plans" / "some-plan.md"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text(
            _FINDINGS_FRONTMATTER.format(agent_type="coordinator:code-reviewer") + _FINDINGS_BODY,
            encoding="utf-8",
        )
        with pytest.raises(mod.DispositionsError, match="subagent-share"):
            mod.append_dispositions(outside, {"applied": ["F1"]}, git_root=tmp_path)

    def test_refuses_non_markdown_target(self, tmp_path):
        sidecar_dir = tmp_path / "state" / "subagent-share" / "sess-abc"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        json_path = sidecar_dir / "trail.json"
        json_path.write_text("{}", encoding="utf-8")
        with pytest.raises(mod.DispositionsError, match=r"\.md"):
            mod.append_dispositions(json_path, {"applied": ["F1"]}, git_root=tmp_path)

    def test_refuses_missing_file(self, tmp_path):
        missing = tmp_path / "state" / "subagent-share" / "sess-abc" / "nope.md"
        with pytest.raises(mod.DispositionsError, match="not found"):
            mod.append_dispositions(missing, {"applied": ["F1"]}, git_root=tmp_path)

    def test_refuses_empty_buckets(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        with pytest.raises(mod.DispositionsError, match="no finding ids"):
            mod.append_dispositions(sidecar, {}, git_root=tmp_path)

    def test_refuses_empty_buckets_including_verified_no_action(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        with pytest.raises(mod.DispositionsError, match="no finding ids"):
            mod.append_dispositions(
                sidecar,
                {
                    "applied": [],
                    "escalated-disagree": [],
                    "escalated-ask": [],
                    "escalated-p0": [],
                    "deferred": [],
                    "verified-no-action": [],
                },
                git_root=tmp_path,
            )


class TestCliEntrypoint:
    def test_cli_happy_path_exits_zero(self, tmp_path, capsys):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        rc = mod.main([
            "--sidecar", str(sidecar),
            "--applied", "F1",
            "--root", str(tmp_path),
        ])
        assert rc == 0
        assert "## Integrator Dispositions" in sidecar.read_text(encoding="utf-8")

    def test_cli_misuse_exits_one(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_UNFILLED_FINDINGS_BODY,
        )
        rc = mod.main([
            "--sidecar", str(sidecar),
            "--applied", "F1",
            "--root", str(tmp_path),
        ])
        assert rc == 1
