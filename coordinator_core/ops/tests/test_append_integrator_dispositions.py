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

_NO_FINDINGS_BODY = (
    "## Findings\n\n"
    "None.\n\n"
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
        # Before the disposition write, the guard fires. It is CLASS =
        # "advisory", so firing means an additionalContext advisory naming the
        # pending sidecar -- NOT a permissionDecision: "deny". This assertion
        # previously read the deny shape and had rotted silently: the KeyError
        # it raised was indistinguishable from the guard not firing at all,
        # which is the one thing this test exists to detect.
        result = guard.check(payload)
        assert result is not None
        hook_output = result["hookSpecificOutput"]
        assert hook_output["hookEventName"] == "PreToolUse"
        assert "permissionDecision" not in hook_output
        assert target_file in hook_output["additionalContext"]

        mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)

        # After, the guard goes quiet -- the loop is closed.
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

    def test_no_findings_records_an_empty_block(self, tmp_path):
        """A reviewer that filled its findings section and declared none is a
        legitimate outcome; without a recordable path the only way to close the
        loop is to invent a finding."""
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_NO_FINDINGS_BODY,
        )
        result = mod.append_dispositions(sidecar, {}, git_root=tmp_path, no_findings=True)
        assert result["already_dispositioned"] is False
        text = sidecar.read_text(encoding="utf-8")
        assert "## Integrator Dispositions" in text
        # The claim cannot be verified against reviewer prose at write time, so
        # the block records that it was made — a zero-finding close stays
        # greppable instead of looking identical to a lazy one.
        assert "no_findings: true" in text

    def test_ordinary_block_carries_no_no_findings_marker(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        assert "no_findings" not in sidecar.read_text(encoding="utf-8")

    def test_no_ids_without_the_flag_is_still_refused(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_NO_FINDINGS_BODY,
        )
        with pytest.raises(mod.DispositionsError, match="no finding ids supplied"):
            mod.append_dispositions(sidecar, {}, git_root=tmp_path)

    def test_no_findings_with_ids_is_refused(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        with pytest.raises(mod.DispositionsError, match="finding ids were supplied"):
            mod.append_dispositions(
                sidecar, {"applied": ["F1"]}, git_root=tmp_path, no_findings=True
            )

    def test_no_findings_does_not_bypass_the_unfilled_scaffold_check(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_UNFILLED_FINDINGS_BODY,
        )
        with pytest.raises(mod.DispositionsError, match="unfilled scaffold"):
            mod.append_dispositions(sidecar, {}, git_root=tmp_path, no_findings=True)

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


#: Agent types that are report_sidecar-eligible but whose deliverable is NOT a
#: finding set — the refusal side of the reviewer-set gate.
_NON_REVIEWER_AGENT_TYPES = (
    "coordinator:executor",
    "coordinator:review-integrator",
    "coordinator:enricher",
)


class TestReviewerAgentTypeSet:
    """The gate is a SET, and its membership is pinned.

    Rationale for pinning rather than asserting shape: the set mirrors the
    types DoE-claude's `report_type_map:` routes to a reviewer template, but
    this module deliberately does not read that peer policy at runtime (see the
    constant's own comment). A pinned membership test is what carries the drift
    risk that decision accepts — widening the set must be a deliberate edit
    against a failing assertion, never a silent slide.
    """

    def test_membership_is_pinned(self):
        assert mod._REVIEWER_AGENT_TYPES == frozenset(
            {
                "coordinator:code-reviewer",
                "coordinator:code-reviewer-weekly",
                "coordinator:staff-eng",
                "coordinator:staff-data-sci",
                "coordinator:senior-front-end",
                "coordinator:staff-ux",
                "coordinator:vp-product",
                "coordinator:eng-director",
                "coordinator:overengineering-reviewer",
                "coordinator:apm",
            }
        )

    def test_stays_wider_than_the_sibling_guards_literal(self):
        """The divergence from the guard's single literal is intentional.

        The guard decides which sidecars BLOCK an EM hand-edit; this set
        decides which can RECEIVE a disposition block. A future edit that
        collapses them into one constant should fail here and read both
        comments before proceeding.
        """
        opus_personas = {
            "coordinator:staff-eng",
            "coordinator:staff-data-sci",
            "coordinator:senior-front-end",
            "coordinator:staff-ux",
            "coordinator:vp-product",
            "coordinator:eng-director",
            "coordinator:overengineering-reviewer",
            "coordinator:apm",
        }
        # Strict superset of the guard's single literal, and the Opus
        # personas must all be present as a group — not just "bigger than 1",
        # which a narrowed-but-still-two-member set would also satisfy.
        assert mod._REVIEWER_AGENT_TYPES > {guard._REVIEWER_AGENT_TYPE}
        assert opus_personas <= mod._REVIEWER_AGENT_TYPES

    @pytest.mark.parametrize("agent_type", sorted(mod._REVIEWER_AGENT_TYPES))
    def test_every_member_can_be_dispositioned(self, tmp_path, agent_type):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "reviewer-slice.md",
            agent_type=agent_type, body=_FINDINGS_BODY,
        )
        result = mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is False
        assert mod._DISPOSITIONS_HEADING in sidecar.read_text(encoding="utf-8")

    @pytest.mark.parametrize("agent_type", _NON_REVIEWER_AGENT_TYPES)
    def test_non_members_are_still_refused(self, tmp_path, agent_type):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "not-a-reviewer.md",
            agent_type=agent_type, body=_FINDINGS_BODY,
        )
        with pytest.raises(mod.DispositionsError, match="agent_type"):
            mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)

    def test_refusal_message_names_the_accepted_set_not_one_value(self, tmp_path):
        """A caller who passed the wrong path needs to see what WOULD be right."""
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "not-a-reviewer.md",
            agent_type="coordinator:executor", body=_FINDINGS_BODY,
        )
        with pytest.raises(mod.DispositionsError) as excinfo:
            mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        message = str(excinfo.value)
        assert "coordinator:code-reviewer" in message
        assert "coordinator:staff-eng" in message


class TestStaffEngReviewSidecarRoundTrip:
    """The regression anchor: the exact case that is refused before this fix.

    A `staff-eng-review` sidecar as `provision_report` actually emits it,
    carrying a persona `agent_type`, must be dispositionable. Built through the
    real template builder rather than a local fixture on purpose — a fixture
    would keep passing if the template regressed, which is precisely the drift
    that produced the defect.
    """

    def test_staff_eng_review_template_is_dispositionable(self, tmp_path):
        from coordinator_core.subagent_sandbox import provision_report

        doc_text = provision_report._build_staff_eng_review_doc_text(
            "coordinator:staff-eng", "2026-08-10T00:00:00Z", "sess-abc"
        )
        filled = doc_text.replace(
            mod._FINDINGS_SENTINEL,
            "- [P1] `x.py:1` a real finding — disposition: accepted — rationale: fixed.",
        )
        sidecar_dir = tmp_path / "state" / "subagent-share" / "sess-abc"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        sidecar = sidecar_dir / "coordinatorstaff-eng-abc.md"
        sidecar.write_text(filled, encoding="utf-8")

        result = mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)

        assert result["already_dispositioned"] is False
        assert mod._DISPOSITIONS_HEADING in sidecar.read_text(encoding="utf-8")

    def test_unfilled_staff_eng_review_scaffold_is_still_refused(self, tmp_path):
        """The widened set must not weaken the empty-scaffold refusal."""
        from coordinator_core.subagent_sandbox import provision_report

        doc_text = provision_report._build_staff_eng_review_doc_text(
            "coordinator:staff-eng", "2026-08-10T00:00:00Z", "sess-abc"
        )
        sidecar_dir = tmp_path / "state" / "subagent-share" / "sess-abc"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        sidecar = sidecar_dir / "coordinatorstaff-eng-abc.md"
        sidecar.write_text(doc_text, encoding="utf-8")

        with pytest.raises(mod.DispositionsError, match="unfilled scaffold"):
            mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)

    def test_verdict_and_rationale_are_not_swallowed_into_the_findings_body(self):
        """Section ORDER is the load-bearing part of the template change.

        `_extract_findings_section` does not stop at an intervening `## `
        heading, so a template emitting Verdict/Rationale AFTER Findings would
        fold them into the findings body — making an unfilled scaffold look
        filled the moment a reviewer wrote a verdict.
        """
        from coordinator_core.subagent_sandbox import provision_report

        doc_text = provision_report._build_staff_eng_review_doc_text(
            "coordinator:staff-eng", "2026-08-10T00:00:00Z", "sess-abc"
        )
        section = mod._extract_findings_section(doc_text)

        assert section is not None
        assert "## Verdict" not in section
        assert "## Rationale" not in section
        assert mod._findings_section_is_empty(section)


#: A real persona sidecar body, trimmed to the load-bearing shape but
#: modeled directly on two live fixtures read during this fix:
#: state/subagent-share/931ad709-a702-4279-98b6-0b47ec3af140/
#: coordinatorstaff-eng-f7748ca8.md and state/subagent-share/
#: 352895b8-a3f9-4c20-bee5-95924bdbff99/coordinatorstaff-eng-28c30594.md.
#: Both real files: NO `## Findings` heading anywhere, an H1 whose wording
#: varies file to file ("# Staff review — ...", "# Staff-eng review — ...",
#: "# Review — ...", "# Re-review ..." all observed live), and a single
#: fenced ```json block (immediately after the H1 in both) whose top-level
#: object carries a `findings` array of finding dicts (file/line_start/
#: line_end/severity/category/finding/suggested_fix — no `id` field; the
#: integrator assigns bucket ids externally, same as the review-findings
#: shape). This fixture keeps that exact field set and section order
#: (H1 -> json fence -> ## Narrative -> ## Coverage -> ## Run notes).
_STAFF_ENG_REVIEW_BODY = (
    "# Staff review — widget throughput regression\n\n"
    "```json\n"
    "{\n"
    '  "reviewer": "staff-eng",\n'
    '  "verdict": "REQUIRES_CHANGES",\n'
    '  "summary": "One correctness defect in the retry path.",\n'
    '  "findings": [\n'
    "    {\n"
    '      "file": "widget.py",\n'
    '      "line_start": 10,\n'
    '      "line_end": 12,\n'
    '      "severity": "major",\n'
    '      "category": "correctness",\n'
    '      "finding": "retry loop never increments the backoff counter.",\n'
    '      "suggested_fix": "increment backoff inside the except clause."\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "```\n\n"
    "## Narrative\n\nRead the retry path end to end.\n\n"
    "## Coverage\n- **Reviewed:** widget.py\n\n"
    "## Run notes\n- No source modified.\n"
)

_STAFF_ENG_REVIEW_EMPTY_FINDINGS_BODY = (
    "# Staff review — clean pass\n\n"
    "```json\n"
    "{\n"
    '  "reviewer": "staff-eng",\n'
    '  "verdict": "SHIP",\n'
    '  "summary": "Nothing to flag.",\n'
    '  "findings": []\n'
    "}\n"
    "```\n\n"
    "## Narrative\n\nClean.\n"
)


#: A persona sidecar that carries BOTH shapes: the `## Findings` scaffold
#: `provision_report._build_staff_eng_review_doc_text` provisions at spawn
#: time, left UNFILLED, plus the JSON block the persona actually wrote. 19 of
#: 307 live persona sidecars carry both headings today (all with a filled
#: section); this is the unfilled variant the ordering must not refuse on.
_STAFF_ENG_REVIEW_WITH_UNFILLED_SCAFFOLD_BODY = (
    _STAFF_ENG_REVIEW_BODY
    + "\n## Findings\n\n"
    + mod._FINDINGS_SENTINEL
    + "\n\n## Exit interview\n\n- Nothing.\n"
)


class TestStaffEngReviewJsonShapeRoundTrip:
    """The regression anchor for the live defect: real persona sidecars carry
    NO `## Findings` heading at all -- only a fenced ```json block with a
    `findings` array. Fixture bodies above are modeled on two real files read
    during this fix (see their own docstring)."""

    def test_unfilled_scaffold_falls_through_to_the_json_block(self, tmp_path):
        """An empty `## Findings` scaffold attests nothing. Refusing on it
        while a populated JSON block sits in the same file is the original
        defect wearing a new label, so the dispatcher falls through instead of
        stopping at the first shape that merely MATCHES."""
        sidecar = _write_sidecar(
            tmp_path, "sess-both", "coordinatorstaff-eng-both01.md",
            agent_type="coordinator:staff-eng",
            body=_STAFF_ENG_REVIEW_WITH_UNFILLED_SCAFFOLD_BODY,
        )
        shape, is_empty = mod._detect_findings_shape(
            sidecar.read_text(encoding="utf-8")
        )
        assert shape == mod._SHAPE_STAFF_ENG_REVIEW
        assert is_empty is False

        result = mod.append_dispositions(sidecar, {"applied": ["0"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is False
        assert mod._DISPOSITIONS_HEADING in sidecar.read_text(encoding="utf-8")

    def test_filled_scaffold_still_wins_over_a_json_block(self, tmp_path):
        """The fall-through must not invert the order for the 19 live
        both-shape files whose section IS filled — those stay on the
        review-findings path they resolve to today."""
        filled = _STAFF_ENG_REVIEW_WITH_UNFILLED_SCAFFOLD_BODY.replace(
            mod._FINDINGS_SENTINEL, "- [major] a real hand-written finding"
        )
        sidecar = _write_sidecar(
            tmp_path, "sess-filled", "coordinatorstaff-eng-filled1.md",
            agent_type="coordinator:staff-eng", body=filled,
        )
        shape, is_empty = mod._detect_findings_shape(
            sidecar.read_text(encoding="utf-8")
        )
        assert shape == mod._SHAPE_REVIEW_FINDINGS
        assert is_empty is False

    def test_staff_eng_review_json_shape_appends_successfully(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "coordinatorstaff-eng-abc123.md",
            agent_type="coordinator:staff-eng", body=_STAFF_ENG_REVIEW_BODY,
        )
        result = mod.append_dispositions(sidecar, {"applied": ["0"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is False
        text = sidecar.read_text(encoding="utf-8")
        assert mod._DISPOSITIONS_HEADING in text
        assert "applied: [0]" in text

    def test_review_findings_heading_shape_still_works_unchanged(self, tmp_path):
        """Live-path non-regression: the pre-existing `## Findings` shape
        (code-reviewer's own output) must keep working exactly as before,
        routed through the same dispatcher that now also serves the
        staff-eng-review shape."""
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        result = mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is False
        assert "## Integrator Dispositions" in sidecar.read_text(encoding="utf-8")

    def test_empty_json_findings_array_is_refused(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "coordinatorstaff-eng-abc123.md",
            agent_type="coordinator:staff-eng", body=_STAFF_ENG_REVIEW_EMPTY_FINDINGS_BODY,
        )
        with pytest.raises(mod.DispositionsError, match="empty"):
            mod.append_dispositions(sidecar, {"applied": ["0"]}, git_root=tmp_path)

    def test_neither_shape_is_refused_naming_both_shapes(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "coordinatorstaff-eng-abc123.md",
            agent_type="coordinator:staff-eng", body="## Run notes\n\nNo findings section at all.\n",
        )
        with pytest.raises(mod.DispositionsError) as excinfo:
            mod.append_dispositions(sidecar, {"applied": ["0"]}, git_root=tmp_path)
        message = str(excinfo.value)
        assert "review-findings" in message
        assert "staff-eng-review" in message

    def test_json_fence_quoted_in_prose_does_not_false_positive(self, tmp_path):
        """A code fence that merely DISCUSSES the json-findings mechanism
        (not valid JSON, or valid JSON with no `findings` list) must not be
        mistaken for the real shape -- the staff-eng-review counterpart to
        `_find_heading`'s prose-quoting negative-spec."""
        body = (
            "# Staff review — mechanism note\n\n"
            "Explaining how the extractor works below, not an actual findings block:\n\n"
            "```json\n"
            "{\"reviewer\": \"staff-eng\", \"verdict\": \"SHIP\", \"summary\": \"no findings key here\"}\n"
            "```\n\n"
            "## Narrative\n\nSee above.\n"
        )
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "coordinatorstaff-eng-abc123.md",
            agent_type="coordinator:staff-eng", body=body,
        )
        with pytest.raises(mod.DispositionsError) as excinfo:
            mod.append_dispositions(sidecar, {"applied": ["0"]}, git_root=tmp_path)
        assert "review-findings" in str(excinfo.value)
        assert "staff-eng-review" in str(excinfo.value)

    def test_staff_eng_heading_quoted_in_prose_does_not_trigger_detection(self, tmp_path):
        """Quoting the literal H1 text `# Staff review` in running prose must
        not, by itself, make a document look like the staff-eng-review shape
        -- detection is content-addressed (the fenced JSON payload), not
        heading-anchored, so a prose mention has no path to a false
        positive. Pinned explicitly since the sibling `review-findings`
        shape has an analogous documented failure mode."""
        body = (
            "## Run notes\n\n"
            "This document is not itself a `# Staff review` sidecar, it just "
            "quotes that heading while describing one.\n\n"
            "```text\n"
            "# Staff review — not actually this shape, no json fence follows.\n"
            "```\n"
        )
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "coordinatorstaff-eng-abc123.md",
            agent_type="coordinator:staff-eng", body=body,
        )
        with pytest.raises(mod.DispositionsError) as excinfo:
            mod.append_dispositions(sidecar, {"applied": ["0"]}, git_root=tmp_path)
        message = str(excinfo.value)
        assert "review-findings" in message
        assert "staff-eng-review" in message

    def test_second_call_on_json_shape_is_a_safe_noop(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "coordinatorstaff-eng-abc123.md",
            agent_type="coordinator:staff-eng", body=_STAFF_ENG_REVIEW_BODY,
        )
        mod.append_dispositions(sidecar, {"applied": ["0"]}, git_root=tmp_path)
        first_text = sidecar.read_text(encoding="utf-8")
        result = mod.append_dispositions(sidecar, {"applied": ["0"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is True
        assert sidecar.read_text(encoding="utf-8") == first_text


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


#: Real code-reviewer self-scaffold frontmatter shape, modeled on the
#: committed sidecars under
#: state/subagent-share/41d66fda-8803-4abd-9454-a9e5e9b5ae24/2026-08-22-
#: codereview-sliceS1-*.md (read 2026-08-22 during this fix). Distinguishing
#: feature: `agent_type: review-findings` -- the doc-TYPE token
#: `coordinator-doc-new --type review-findings` stamps on its self-persist
#: fallback path, NOT a subagent_type name. This is the exact shape the op
#: refused before this fix (`agent_type` not a member of
#: `_REVIEWER_AGENT_TYPES`, which holds only agent-type names).
_SELF_SCAFFOLD_FRONTMATTER = (
    "---\n"
    "status: open\n"
    "agent_type: review-findings\n"
    "spawned_at: 2026-08-22T14:51:07Z\n"
    "lead_session_id: 41d66fda-8803-4abd-9454-a9e5e9b5ae24\n"
    "divergence:\n"
    "  diverged: false\n"
    "commits: []\n"
    "dispatch_feed:\n"
    "  gate_kind: none\n"
    "  write_files: []\n"
    "---\n\n"
)


class TestSelfScaffoldedReviewFindingsDocTypeToken:
    """Regression anchor for the live defect: `coordinator-doc-new --type
    review-findings`'s self-persist fallback stamps the doc-TYPE token
    `review-findings` into `agent_type:`, never an agent-type name — so the
    accepted-set gate must admit that literal too, alongside the real
    reviewer agent types. Fixture is the real frontmatter shape, not a
    synthetic one, per this fix's own regression discipline."""

    def test_self_scaffolded_review_findings_sidecar_is_dispositionable(self, tmp_path):
        sidecar_dir = tmp_path / "state" / "subagent-share" / "41d66fda-8803-4abd-9454-a9e5e9b5ae24"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        sidecar = sidecar_dir / "2026-08-22-codereview-sliceS1-primitive.md"
        sidecar.write_text(_SELF_SCAFFOLD_FRONTMATTER + _FINDINGS_BODY, encoding="utf-8")

        result = mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)

        assert result["already_dispositioned"] is False
        text = sidecar.read_text(encoding="utf-8")
        assert mod._DISPOSITIONS_HEADING in text
        assert "applied: [F1]" in text

    def test_doc_type_token_membership_is_pinned(self):
        assert mod._REVIEWER_DOC_TYPE_TOKENS == frozenset({"review-findings"})

    def test_arbitrary_doc_type_token_is_still_refused(self, tmp_path):
        """The gate is a pinned set, not "anything string-shaped" — a token
        that isn't the real self-scaffold literal must still be refused."""
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "not-a-real-token.md",
            agent_type="staff-eng-review", body=_FINDINGS_BODY,
        )
        with pytest.raises(mod.DispositionsError, match="agent_type"):
            mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)


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

    def test_cli_rationale_from_stdin_lands_in_the_block(self, tmp_path, monkeypatch):
        """The pathless channel: prose arrives on stdin, so there is no
        caller-chosen path for a concurrent session to clobber.

        Spec backlink: state/bug-backlog/2026-08-31-concurrent-sessions-collide-on-a-shared.yaml
        """
        import io as _io

        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        monkeypatch.setattr(
            mod.sys, "stdin", _io.StringIO("F1 applied verbatim; the precedence read was right.")
        )
        rc = mod.main([
            "--sidecar", str(sidecar),
            "--applied", "F1",
            "--rationale-stdin",
            "--root", str(tmp_path),
        ])
        assert rc == 0
        text = sidecar.read_text(encoding="utf-8")
        # Under the Rationale heading, not merely somewhere in the file -- a
        # substring check would pass on prose landing in the wrong section.
        assert "### Rationale" in text
        after = text.split("### Rationale", 1)[1]
        assert "the precedence read was right" in after

    def test_cli_dash_rationale_file_reads_stdin(self, tmp_path, monkeypatch):
        """`--rationale-file -` is the POSIX stdin convention, and a caller reached
        for it and was refused before this existed. Refusing it pushes the caller
        back onto a real temp path -- the hazard the stdin channel removes.
        """
        import io as _io

        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        monkeypatch.setattr(mod.sys, "stdin", _io.StringIO("piped in through the dash."))
        rc = mod.main([
            "--sidecar", str(sidecar),
            "--applied", "F1",
            "--rationale-file", "-",
            "--root", str(tmp_path),
        ])
        assert rc == 0
        text = sidecar.read_text(encoding="utf-8")
        assert "### Rationale" in text
        assert "piped in through the dash" in text.split("### Rationale", 1)[1]

    def test_cli_refuses_stdin_channel_when_there_is_no_stdin(self, tmp_path, capsys, monkeypatch):
        """A dispatched agent runs with no stdin, so `sys.stdin` is None and the
        read raised AttributeError -- a crash the caller could only read as the
        CLI being broken, with nothing naming the channel that does work. An
        integrator hit this mid-pass and had to invent the workaround itself."""
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        monkeypatch.setattr(mod.sys, "stdin", None)
        rc = mod.main([
            "--sidecar", str(sidecar),
            "--applied", "F1",
            "--rationale-stdin",
            "--root", str(tmp_path),
        ])
        assert rc == 2
        assert "--rationale-file" in capsys.readouterr().err
        assert "## Integrator Dispositions" not in sidecar.read_text(encoding="utf-8")

    def test_cli_refuses_both_rationale_channels_at_once(self, tmp_path, capsys):
        """Two sources for one field is a caller bug, not something to resolve by
        precedence -- silently picking one is how the wrong prose lands."""
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-sliceA.md",
            agent_type="coordinator:code-reviewer", body=_FINDINGS_BODY,
        )
        rationale = tmp_path / "rationale.txt"
        rationale.write_text("from the file", encoding="utf-8")
        rc = mod.main([
            "--sidecar", str(sidecar),
            "--applied", "F1",
            "--rationale-stdin",
            "--rationale-file", str(rationale),
            "--root", str(tmp_path),
        ])
        assert rc == 2
        assert "mutually exclusive" in capsys.readouterr().err
        assert "## Integrator Dispositions" not in sidecar.read_text(encoding="utf-8")

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


#: A findings body that QUOTES the disposition heading in prose while
#: explaining the mechanism — the shape a good reviewer actually writes, and
#: the shape that silently disarmed both consumers before the headings were
#: line-anchored. Taken from a real code-reviewer sidecar, 2026-08-10.
_FINDINGS_BODY_QUOTING_HEADINGS = (
    "## Findings\n\n"
    "Summary: verified the carve-out runs to whichever of `## Exit interview` /\n"
    "`## Integrator Dispositions` comes first, with no intervening-`## ` stop.\n\n"
    "- [P3] `some_file.py:42` unused import.\n\n"
)


class TestHeadingsAreLineAnchoredNotSubstrings:
    """Regression: a sidecar that MENTIONS a heading does not carry it.

    Both consumers keyed off `heading in text`, so a reviewer who quoted
    `## Integrator Dispositions` while explaining the extractor made
    `append_dispositions` no-op with the block never written, AND made the
    sibling guard read the loop as closed, silently unblocking EM hand-edits
    over undispositioned findings. The second is the dangerous direction: a
    guard that disarms itself when someone describes it reads as all-clear.
    """

    def test_quoted_disposition_heading_is_not_read_as_already_dispositioned(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-quoting.md",
            agent_type="coordinator:code-reviewer",
            body=_FINDINGS_BODY_QUOTING_HEADINGS,
        )
        result = mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is False
        assert "applied: [F1]" in sidecar.read_text(encoding="utf-8")

    def test_quoted_heading_does_not_disarm_the_sibling_guard(self, tmp_path):
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "codereview-quoting.md",
            agent_type="coordinator:code-reviewer",
            body=_FINDINGS_BODY_QUOTING_HEADINGS,
        )
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "some_file.py", "old_string": "x", "new_string": "y"},
            "cwd": str(tmp_path),
            "session_id": "sess-abc",
        }
        assert guard.check(payload) is not None

        mod.append_dispositions(sidecar, {"applied": ["F1"]}, git_root=tmp_path)
        assert guard.check(payload) is None

    def test_quoted_findings_heading_does_not_shift_the_section_carve(self):
        text = (
            "Preamble naming `## Findings` before the real one.\n\n"
            "## Findings\n\nreal body\n\n## Exit interview\n\nq\n"
        )
        section = mod._extract_findings_section(text)
        assert section is not None
        assert "real body" in section
        assert "Preamble" not in section

    def test_indented_heading_is_not_a_heading(self):
        assert mod._find_heading("    ## Findings\n", "## Findings") is None
        assert mod._find_heading("## Findings\n", "## Findings") == 0

    def test_trailing_whitespace_is_tolerated(self):
        assert mod._find_heading("## Findings   \n", "## Findings") == 0


class TestFencePairingSurvivesAnUnterminatedFence:
    """Regression for the 2026-08-19 review finding: `_FENCE_RE`'s prior
    whole-block DOTALL match had no pairing awareness, so an unterminated
    fence BEFORE the real ```json block greedily expanded to steal the real
    block's own closing delimiter -- the real block's opening line was then
    consumed as body text of the bogus match, and the genuinely populated
    findings block became invisible to detection (a false refusal on a
    sidecar that does carry real findings).
    """

    def test_unterminated_fence_before_the_real_block_does_not_swallow_it(self):
        real_block = (
            "```json\n"
            '{"reviewer": "staff-eng", "findings": [{"finding": "real one"}]}\n'
            "```\n"
        )
        doc = (
            "# Staff review\n\n"
            "## Notes\n\n"
            "```\n"
            "an unterminated snippet, no closing fence\n\n"
            + real_block
            + "\n## Narrative\n\nprose.\n"
        )
        findings = mod._find_json_findings_block(doc)
        assert findings == [{"finding": "real one"}]
        shape, is_empty = mod._detect_findings_shape(doc)
        assert shape == mod._SHAPE_STAFF_ENG_REVIEW
        assert is_empty is False

    def test_unterminated_fence_after_the_real_block_still_works(self):
        """Pin: this direction already worked before the fix (the real
        block is well-formed and self-contained before any malformed fence
        is reached) -- a fix must not regress it."""
        real_block = (
            "```json\n"
            '{"reviewer": "staff-eng", "findings": [{"finding": "real one"}]}\n'
            "```\n"
        )
        doc = (
            real_block
            + "\n## Narrative\n\n"
            + "```\n"
            + "an unterminated snippet after, no closing fence\n"
        )
        findings = mod._find_json_findings_block(doc)
        assert findings == [{"finding": "real one"}]

    def test_unterminated_fence_before_full_append_dispositions_round_trip(self, tmp_path):
        """End-to-end: the same shape via the real `append_dispositions`
        entrypoint, not just the extractor helper."""
        real_block = (
            "```json\n"
            '{"reviewer": "staff-eng", "findings": [{"finding": "real one"}]}\n'
            "```\n"
        )
        body = (
            "## Notes\n\n"
            "```\n"
            "an unterminated snippet, no closing fence\n\n"
            + real_block
            + "\n## Narrative\n\nprose.\n"
        )
        sidecar = _write_sidecar(
            tmp_path, "sess-abc", "coordinatorstaff-eng-abc123.md",
            agent_type="coordinator:staff-eng", body=body,
        )
        result = mod.append_dispositions(sidecar, {"applied": ["0"]}, git_root=tmp_path)
        assert result["already_dispositioned"] is False
        assert mod._DISPOSITIONS_HEADING in sidecar.read_text(encoding="utf-8")

def test_repeated_bucket_flag_accumulates_rather_than_overwriting():
    """A repeated `--applied` means BOTH sets, not the last one.

    Regression: every bucket flag was a plain `store`, so
    `--applied 1,2 --applied 3` silently kept only `3` and still exited 0 —
    a disposition record that read complete while half the ids were gone.
    Found by a review-integrator that hit it live, 2026-08-26.
    """
    from coordinator_core.ops.append_integrator_dispositions import (
        _build_arg_parser,
        _split_ids,
    )

    args = _build_arg_parser().parse_args(
        ["--sidecar", "x", "--applied", "1,2", "--applied", "3"]
    )
    assert _split_ids(args.applied) == ["1", "2", "3"]


def test_single_bucket_flag_use_is_unchanged_and_dedupes():
    from coordinator_core.ops.append_integrator_dispositions import (
        _build_arg_parser,
        _split_ids,
    )

    parser = _build_arg_parser()
    assert _split_ids(parser.parse_args(["--sidecar", "x", "--applied", "1,2"]).applied) == [
        "1",
        "2",
    ]
    assert _split_ids(parser.parse_args(["--sidecar", "x"]).applied) == []
    assert _split_ids(["1,2", "2,3"]) == ["1", "2", "3"]
