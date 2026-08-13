"""Behavioral tests for
coordinator_core.write_guards.nudge_plan_sidecar_family_split -- the
plan-sidecar/subagent-share family-split advisory guard.

Spec: docs/plans/2026-08-03-plan-sidecar-write-seam-guard.md

Covers: non-write-tool/non-plan-sidecar-path passthrough, the four positive
cases from the real residue (staff-eng-review, staff-eng-review-delta,
memo-residue, cross-repo-amendment), silent passthrough for all five row-39
lenses (AC3), the archival timestamped rename form for both directions
(AC4), the imported (not re-listed) lens vocabulary (AC5), message shape
(leads with the offer, never a bare-boolean assertion), path-segment
anchoring, backslash-path normalization, and the module contract
(CLASS/PRIORITY/MATCHERS).
"""

from __future__ import annotations

import pytest

from coordinator_core.subagent_sandbox.provision_report import _PLAN_DERIVABLE_LENS
from coordinator_core.write_guards import nudge_plan_sidecar_family_split as guard


def _payload(tool_name, file_path, **extra):
    tool_input = {"file_path": file_path}
    tool_input.update(extra)
    return {"tool_name": tool_name, "tool_input": tool_input}


def _advisory_text(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso
    return hso["additionalContext"]


class TestGateOnToolAndPath:
    @pytest.mark.parametrize(
        "tool_name,file_path",
        [
            ("Read", "state/plan-sidecars/2026-08-01-foo.staff-eng-review.md"),
            ("Write", "state/subagent-share/abc12345/staff-eng-review.md"),
            ("Write", ""),
            ("Write", "state/plan-sidecars/2026-08-01-foo.prior-art-check.md"),
        ],
        ids=[
            "non_write_tool",
            "already_under_subagent_share",
            "file_path_empty",
            "row_39_legal_lens",
        ],
    )
    def test_passes_through(self, tool_name, file_path):
        assert guard.check(_payload(tool_name, file_path)) is None

    def test_tool_input_not_dict_passes_through(self):
        assert guard.check({"tool_name": "Write", "tool_input": "not-a-dict"}) is None

    def test_no_recognizable_lens_position_passes_through(self):
        """A bare filename with no dot-segment at all is not this guard's
        concern -- it only judges the lens value, not general shape."""
        assert guard.check(_payload("Write", "state/plan-sidecars/README.md")) is None


class TestRealResiduePositiveCases:
    """The four persona/report lens families actually found on disk in
    state/plan-sidecars/ today (spec Problem section) -- the engine never
    produced them; a dispatching EM named the path in a brief."""

    @pytest.mark.parametrize(
        "file_path",
        [
            "state/plan-sidecars/2026-07-31-publish-sync-single-source-portable-seam.staff-eng-review.md",
            "state/plan-sidecars/2026-08-01-percolate-root-rung-ordering.staff-eng-review-delta.md",
            "state/plan-sidecars/2026-08-01-percolate-root-rung-ordering.memo-residue.md",
            "state/plan-sidecars/2026-08-03-scope-guard-peer-claim-release.cross-repo-amendment.md",
        ],
        ids=[
            "staff-eng-review",
            "staff-eng-review-delta",
            "memo-residue",
            "cross-repo-amendment",
        ],
    )
    def test_non_row_39_lens_advises(self, file_path):
        result = guard.check(_payload("Write", file_path))
        text = _advisory_text(result)
        assert "state/subagent-share/<session>/<name>.md" in text
        assert "not a plan-derivable lens" in text

    def test_edit_tool_also_advises(self):
        result = guard.check(
            _payload(
                "Edit",
                "state/plan-sidecars/2026-08-01-foo.staff-eng-review.md",
                old_string="x",
                new_string="y",
            )
        )
        _advisory_text(result)

    def test_multi_edit_tool_also_advises(self):
        result = guard.check(
            _payload(
                "MultiEdit",
                "state/plan-sidecars/2026-08-01-foo.staff-eng-review.md",
                edits=[{"old_string": "x", "new_string": "y"}],
            )
        )
        _advisory_text(result)

    def test_notebook_edit_via_notebook_path_also_advises(self):
        payload = {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "notebook_path": "state/plan-sidecars/2026-08-01-foo.staff-eng-review.md",
                "new_source": "print('x')",
            },
        }
        result = guard.check(payload)
        _advisory_text(result)


class TestRow39LegalLensesSilent:
    """AC3: all five row-39 lenses write silently -- zero false fires."""

    @pytest.mark.parametrize("lens", sorted(_PLAN_DERIVABLE_LENS.values()))
    def test_row_39_lens_silent(self, lens):
        file_path = f"state/plan-sidecars/2026-07-28-sat-01-sovereign-tracker-substrate.{lens}.md"
        assert guard.check(_payload("Write", file_path)) is None

    def test_imports_lens_vocabulary_not_a_literal(self):
        """AC5: the guard's vocabulary is the imported map's values, not a
        second, hand-maintained list living in this test or the guard."""
        assert guard._ROW_39_LENSES == frozenset(_PLAN_DERIVABLE_LENS.values())
        # Bumped 4 -> 5 on 2026-08-06 when coordinator:plan-reviewer joined the
        # map (coordinator-claude DR-133). This literal exists to make a map change a
        # deliberate, reviewed act rather than a silent widening of the
        # guard's silent-passthrough vocabulary -- bump it only alongside a
        # membership ruling recorded in provision_report/CONTRACT.md.
        assert len(_PLAN_DERIVABLE_LENS) == 5


class TestArchivalTimestampedForm:
    """AC4: the <stem>.<lens>.<ISO8601>.md rename-on-existing form."""

    @pytest.mark.parametrize("lens", sorted(_PLAN_DERIVABLE_LENS.values()))
    def test_archival_row_39_lens_silent(self, lens):
        file_path = (
            f"state/plan-sidecars/2026-07-27-review-trail-scope-guard.{lens}."
            "2026-07-27T19-46-54Z.md"
        )
        assert guard.check(_payload("Write", file_path)) is None

    def test_archival_non_row_39_lens_advises(self):
        file_path = (
            "state/plan-sidecars/2026-08-01-percolate-root-rung-ordering."
            "staff-eng-review.2026-08-01T09-00-00Z.md"
        )
        result = guard.check(_payload("Write", file_path))
        text = _advisory_text(result)
        assert "not a plan-derivable lens" in text


class TestDoubledExtensionArchivalForm:
    """Regression fixtures for code-review Finding 1: real on-disk files with
    the doubled-extension archival shape ``<stem>.<lens>.md.<ISO8601>.md`` --
    an extra ``.md`` segment before the timestamp that the fixed-position
    second-to-last-segment rule mis-read as lens ``"md"``."""

    @pytest.mark.parametrize(
        "file_path",
        [
            "state/plan-sidecars/2026-08-03-check5-owner-attribution-liveness."
            "prior-art-check.md.2026-08-03T14-57-57Z.md",
            "state/plan-sidecars/2026-08-03-hooks-baked-interpreter-resolution."
            "prior-art-check.md.2026-08-03T13-53-12Z.md",
        ],
        ids=["check5-owner-attribution-liveness", "hooks-baked-interpreter-resolution"],
    )
    def test_real_doubled_extension_row_39_lens_silent(self, file_path):
        assert guard.check(_payload("Write", file_path)) is None

    def test_doubled_extension_non_row_39_lens_advises(self):
        file_path = (
            "state/plan-sidecars/2026-08-01-percolate-root-rung-ordering."
            "staff-eng-review.md.2026-08-01T09-00-00Z.md"
        )
        result = guard.check(_payload("Write", file_path))
        text = _advisory_text(result)
        assert "not a plan-derivable lens" in text


class TestMalformedNoStemEdgeCase:
    """Finding 5: a (hypothetical) archival filename with no stem must not
    mis-parse the raw ISO8601 timestamp itself as the lens."""

    def test_no_stem_archival_form_parses_lens_not_timestamp(self):
        file_path = "state/plan-sidecars/prior-art-check.2026-07-27T19-46-54Z.md"
        assert guard.check(_payload("Write", file_path)) is None


class TestPathSegmentAnchoring:
    def test_substring_coincidence_does_not_match(self):
        """A path containing the literal substring 'plan-sidecars/' where the
        parent is not actually 'state/plan-sidecars/' must not match."""
        assert (
            guard.check(_payload("Write", "vendor/state/plan-sidecars-old/x.staff-eng-review.md"))
            is None
        )

    def test_nested_absolute_prefix_still_anchors(self):
        result = guard.check(
            _payload("Write", "/repo/state/plan-sidecars/2026-08-01-foo.staff-eng-review.md")
        )
        _advisory_text(result)


class TestBackslashPathNormalization:
    def test_backslash_path_still_matched(self):
        result = guard.check(
            _payload("Write", "state\\plan-sidecars\\2026-08-01-foo.staff-eng-review.md")
        )
        _advisory_text(result)


class TestModuleContract:
    def test_class_is_advisory(self):
        assert guard.CLASS == "advisory"

    def test_priority_and_matchers(self):
        # Was 140; moved to 141 to resolve the 140/140 collision with
        # nudge_tasks_state_folder_split.py -- see
        # docs/wiki/write-guard-priority-bands.md § "The 140/140 collision (AC9)".
        assert guard.PRIORITY == 141
        assert guard.MATCHERS == ["Write", "Edit", "MultiEdit", "NotebookEdit"]
