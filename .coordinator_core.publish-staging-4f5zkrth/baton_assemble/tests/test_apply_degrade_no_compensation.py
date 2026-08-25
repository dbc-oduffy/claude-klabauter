"""Regression coverage for the 2026-08-03 break-class defect: a `/handoff`
ceremony that minted its successor and then DELETED it, reporting the deletion
as a successful rollback.

Reproduced live in project-makima over a `status: open` predecessor. d6
(`handoff.supersede_predecessor`) raised on its DR-242 gate
(`claimed_or_shipped_at_path` -> False), its own `_cleanup_successor` unlinked
the successor d1 had just scaffolded, `_D1_COMPENSATORS` then ran, and the
report read `status: partial` with
`compensation: [{d5, True, True}, {d1, True, True}]`. The gate is DETERMINISTIC
on the same inputs, so the module's sanctioned resume path (re-run the
identical command) could never converge: every attempt minted and deleted the
same file. Under context pressure that is the exact save-state loss `/handoff`
exists to prevent.

The distinction under test is DEGRADED vs FAILED:

- **degraded** -- the directive declined for a known, anticipated, reported
  reason. Nothing was composed, nothing is half-applied. Compensation must NOT
  fire. d6's DR-242 gate and d4's `tracker-not-queue-backed` are the two
  instances.
- **failed** -- the directive did not achieve its effect. Compensation fires
  exactly as it always has.

Both halves are asserted here. A compensation path nobody has watched fire is
indistinguishable from one that was quietly disabled, so
`test_a_genuine_failure_still_compensates` is as load-bearing as the guard it
sits beside.

Spec backlink: the fix lives in `coordinator_core/baton_assemble/apply.py`'s
`_dispatch_handoff_supersede_predecessor` (DR-242 gate) and `_collect_degraded`.
"""

from __future__ import annotations

import pytest

from coordinator_core import baton_assemble as ba
from coordinator_core.baton_assemble import apply as ba_apply

# The whole-ceremony harness has ONE home -- `coordinator_core/
# test_baton_assemble.py`'s `_ReplayHarness`, which drives a real `apply()` run
# against a real git repo and the REAL `handoff.archive_transition` op, faking
# only the four subprocess-shaped directives (d1/d2/d4/d5). d6 is deliberately
# never faked: it is the directive under test here. Importing it rather than
# restating it is deliberate -- a second copy would drift from the first, and
# the fidelity of its d1 fake (it renders through the generator's own
# scaffolder) is exactly what makes the pristine-scaffold predicate and the
# adoption candidate-match real in these tests instead of asserted away.
from coordinator_core.test_baton_assemble import (  # noqa: E402
    _FAKE_OPERATOR_CONFIG,
    _PRED_REL,
    _PREDECESSOR_FM,
    _REPO_MAKIMA_BIN,
    _ReplayHarness,
)


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """The sibling module's autouse fixture of the same name, restated because
    an autouse fixture is module-scoped and does not cross into this one. The
    VALUES have one home -- both are imported above, never re-derived here.

    Without it this suite's HOME quarantine makes `resolve_operator_config()`
    fail loud by design and every `apply()` run aborts at d1. `_resolve_makima_
    bin` must point at the REAL in-repo `coordinator/bin`: the
    pristine-scaffold predicate decides whether to DELETE a file by re-rendering
    `coordinator-doc-new`'s own template, and a non-existent bin makes it
    decline for the wrong reason (generator unavailable), quietly asserting
    nothing in exactly the tests here that turn on it."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setattr(ba_apply, "_resolve_makima_bin", lambda: _REPO_MAKIMA_BIN)


def _never_claimed_predecessor_fm() -> list[str]:
    """`_PREDECESSOR_FM` with the claim stripped -- the shape
    `claimed_or_shipped_at_path` (DR-242) answers False for, and the frontmatter
    shape of the live 2026-08-03 `/handoff` input."""
    return [
        line
        for line in _PREDECESSOR_FM
        if not line.startswith(("claimed_at:", "claimed_by:"))
    ]


def _declining_harness(tmp_path, monkeypatch) -> _ReplayHarness:
    return _ReplayHarness(
        tmp_path, monkeypatch, predecessor_fm=_never_claimed_predecessor_fm()
    )


class TestADegradeCompensatesNothing:
    """Guard 1 of 3 -- THE regression guard. Fails against pre-fix `apply.py`
    on the first assertion of `test_the_minted_successor_survives_the_decline`:
    `live_handoffs()` came back `["predecessor.md"]`, the successor having been
    minted and then deleted."""

    def test_the_minted_successor_survives_the_decline(self, tmp_path, monkeypatch):
        harness = _declining_harness(tmp_path, monkeypatch)
        exit_code, report = harness.run()

        successors = [n for n in harness.live_handoffs() if n != "predecessor.md"]
        assert len(successors) == 1, (
            "the minted successor must survive an anticipated decline -- it is "
            f"the irreversible artifact of the ceremony; report={report}"
        )
        assert (harness.repo / "state" / "handoffs" / successors[0]).is_file()
        assert exit_code == ba_apply.APPLY_EXIT_OK, report
        # d7 (`handoff-carry-gate`) is a precondition gate that runs BEFORE d1
        # scaffolds the successor -- see `_build_directives`'s d7 block in
        # `coordinator_core/baton_assemble/__init__.py`, which explicitly
        # prepends it ahead of d1 for exactly this predecessor-with-carried-
        # items shape. It lands (not degrades) here: the never-claimed
        # predecessor still has readable `carried_items` frontmatter, so the
        # gate itself is satisfied -- only d6's DR-242 supersede gate declines.
        # d4 (render-project-tracker) was retired 2026-08-14 -- see
        # `_build_directives`'s own note in coordinator_core/baton_assemble/
        # __init__.py. The retirement landed without updating this list.
        assert report["landed"] == ["d7", "d1", "d2", "d5", "d6"]

    def test_the_decline_triggers_no_compensation_pass_at_all(
        self, tmp_path, monkeypatch
    ):
        harness = _declining_harness(tmp_path, monkeypatch)
        _, report = harness.run()

        assert "compensation" not in report, (
            "compensation is a reaction to FAILURE -- an anticipated decline "
            f"must not trigger it; report={report}"
        )

    def test_the_surviving_successor_is_committed_not_left_loose(
        self, tmp_path, monkeypatch
    ):
        """A run that ends `ok` reaches `_scoped_commit`, so the operator is not
        handed an uncommitted baton to finish by hand -- the "relocated
        transcription" the module's own resume model exists to end."""
        harness = _declining_harness(tmp_path, monkeypatch)
        _, report = harness.run()

        assert report["commit_sha"]

    def test_the_predecessor_is_left_untouched_by_the_decline(
        self, tmp_path, monkeypatch
    ):
        """DR-242's substance is unchanged: an unclaimed predecessor is still
        never superseded, and the op is never composed. Only the blast radius
        of that refusal changed."""
        harness = _declining_harness(tmp_path, monkeypatch)
        harness.run()

        assert harness.archived_predecessor() is None
        assert harness.continued_into() is None
        assert "deployment_state: continued" not in harness.predecessor_text()


class TestAGenuineFailureStillCompensates:
    """Guard 2 of 3 -- the safety half, proving the fix did not disable the
    compensation path it narrows. Same never-claimed predecessor, but a
    directive genuinely raises."""

    def test_a_genuine_failure_still_compensates(self, tmp_path, monkeypatch):
        harness = _declining_harness(tmp_path, monkeypatch)
        harness.fail_at = "d5"
        exit_code, report = harness.run()

        assert exit_code == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION, report
        assert report["failed_directive"] == "d5"
        assert report["compensation"] == [
            {"directive_id": "d1", "attempted": True, "succeeded": True}
        ]
        # d1's pristine scaffold is removed exactly as it was before this fix.
        assert harness.live_handoffs() == ["predecessor.md"]

    def test_compensation_still_declines_to_delete_operator_content(
        self, tmp_path, monkeypatch
    ):
        """The compensator's own pristine gate is untouched: a scaffold
        carrying real prose survives a genuine failure, as it did before."""
        harness = _declining_harness(tmp_path, monkeypatch)
        harness.d1_body = "## What Was Accomplished\n\nReal operator prose.\n"
        harness.fail_at = "d5"
        assert harness.run()[0] == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION

        stranded = [n for n in harness.live_handoffs() if n != "predecessor.md"]
        assert len(stranded) == 1
        assert "Real operator prose." in (
            harness.repo / "state" / "handoffs" / stranded[0]
        ).read_text(encoding="utf-8")


class TestTheReportDistinguishesTheTwoCases:
    """Guard 3 of 3. The live mis-read, twice over: `partial` read as "a
    directive declined as designed". `status` answers "did every directive that
    ran apply"; the top-level `degraded` list answers "did any directive
    decline". They are different questions and a caller can now separate them
    from either field."""

    def test_decline_reports_ok_and_failure_reports_partial(
        self, tmp_path, monkeypatch
    ):
        declined = _declining_harness(tmp_path, monkeypatch)
        _, decline_report = declined.run()

        failed = _declining_harness(tmp_path / "second", monkeypatch)
        failed.fail_at = "d5"
        _, failure_report = failed.run()

        assert decline_report["status"] == "ok"
        assert failure_report["status"] == "partial"
        assert "failed_directive" not in decline_report
        assert failure_report["failed_directive"] == "d5"
        assert [d["directive_id"] for d in decline_report["degraded"]] == ["d6"]

    def test_the_degrade_row_names_the_directive_the_cli_and_the_reason(
        self, tmp_path, monkeypatch
    ):
        harness = _declining_harness(tmp_path, monkeypatch)
        _, report = harness.run()

        assert report["degraded"] == [
            {
                "directive_id": "d6",
                "cli": "handoff.supersede_predecessor",
                "reason": "predecessor-not-claimed-or-shipped",
                "predecessor": _PRED_REL,
            }
        ]

    def test_a_clean_run_reports_an_empty_degraded_list(self, tmp_path, monkeypatch):
        """Present-as-[] -- "nothing declined" and "this report does not say"
        must not read the same.

        Uses a conforming predecessor id (not the shared `_PREDECESSOR_FM`'s
        `HID-PRED`, which does not match handoff.schema.json's `handoff_id`
        pattern) -- this is the only test in this module whose run reaches
        d6's real `handoff.archive_transition` compose, which validates the
        predecessor's frontmatter against the live schema. Every sibling test
        here declines before d6 composes, so the shared fixture's
        non-conforming id never gets validated there."""
        conforming_predecessor_fm = [
            line.replace("HID-PRED", "hnd-pred-a1b2c3") for line in _PREDECESSOR_FM
        ]
        harness = _ReplayHarness(
            tmp_path, monkeypatch, predecessor_fm=conforming_predecessor_fm
        )
        exit_code, report = harness.run()

        assert exit_code == ba_apply.APPLY_EXIT_OK, report
        assert report["degraded"] == []


class TestCollectDegradedNormalization:
    """`_collect_degraded` reads two in-tree degrade shapes: a single dict (d4,
    d6) and d3's LIST of per-field degrade rows. Both flatten to the same row
    shape, so a caller never branches on which handler produced the entry."""

    def test_a_list_valued_degrade_flattens_to_one_row_per_entry(self):
        report = {
            "results": [
                {
                    "id": "d3",
                    "detail": {
                        "cli": "handoff.author_fork",
                        "degraded": [
                            {"field": "origin_branch", "reason": "unresolvable"},
                            {"field": "origin_sha", "reason": "unresolvable"},
                        ],
                    },
                }
            ]
        }
        assert ba_apply._collect_degraded(report) == [
            {
                "directive_id": "d3",
                "cli": "handoff.author_fork",
                "field": "origin_branch",
                "reason": "unresolvable",
            },
            {
                "directive_id": "d3",
                "cli": "handoff.author_fork",
                "field": "origin_sha",
                "reason": "unresolvable",
            },
        ]

    def test_a_null_degrade_contributes_no_row(self):
        report = {
            "results": [
                {"id": "d4", "detail": {"cli": "render-project-tracker", "degraded": None}},
                {"id": "d5", "detail": {"cli": "session-claim-cli"}},
                {"id": "d1", "already_satisfied": True, "detail": None},
            ]
        }
        assert ba_apply._collect_degraded(report) == []

    def test_a_report_with_no_results_key_is_not_an_error(self):
        assert ba_apply._collect_degraded({"landed": []}) == []


class TestTheDeclineIsVisibleAtTheHandlerSeam:
    """The unit-level shape of the degrade the report rows above are built
    from, asserted without a whole `apply()` run."""

    def test_the_gate_returns_a_degrade_and_never_composes_the_op(
        self, tmp_path, monkeypatch
    ):
        from coordinator_core.test_baton_assemble import (
            _render_real_scaffold,
            _write_artifact,
        )

        successor_rel = "state/handoffs/2026-08-03-successor-declined.md"
        successor_abs = _render_real_scaffold(tmp_path / successor_rel)
        _write_artifact(tmp_path / _PRED_REL, ["title: never claimed"])

        calls: list[tuple] = []

        def _fake_invoke(op_name, params, repo_root):
            calls.append((op_name, params))
            return {"exit_code": 0, "superseded": True, "moved": True}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)

        result = ba_apply._dispatch_handoff_supersede_predecessor(
            [_PRED_REL, successor_rel, successor_rel], tmp_path
        )

        assert calls == [], "the op must never be composed behind a DR-242 decline"
        assert successor_abs.exists()
        assert result["result"] is None
        assert result["degraded"] == {
            "reason": "predecessor-not-claimed-or-shipped",
            "predecessor": _PRED_REL,
        }


def test_the_module_under_test_is_importable_without_a_repo():
    """Guards the import idiom above -- this package had no `tests/` directory
    before 2026-08-03, so the seam is new."""
    assert ba.KINDS
