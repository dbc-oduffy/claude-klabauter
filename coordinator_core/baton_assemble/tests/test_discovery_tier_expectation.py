"""coordinator_core.baton_assemble.tests.test_discovery_tier_expectation

C2 (2026-09-02, the-loader-fires-the-assembly-not-the-em plan): the
`kind=handoff` parent-discovery cascade (`plan -> predecessor -> mint`) is
LEGAL to answer from the wrong tier -- passing the governing plan where the
predecessor baton was wanted resolves through the plan tier by design (the
claimed-plan rung is read from the session's OWN claim ledger via
`resolve_claimed_plan_path`, entirely independent of what `artifact_path`
names), and silently produces a brief with no predecessor and no supersede
directive. Only a warn-only stderr line used to distinguish the wrong
invocation from the right one.

This tests `resolve_lineage`'s new opt-in `expected_discovery_tier` kwarg:
the caller declares which cascade STEP it expects ("plan" / "predecessor" /
"mint"), and a mismatch raises `ValueError` naming both tiers.

`resolve_claimed_plan_path` is monkeypatched directly (module-level import
in `coordinator_core.baton_assemble`) rather than seeded through a real git
repo + session-claim ledger -- this module's own cascade call site never
consults `session_id`/git for that rung, only the return value of that one
function, so stubbing it is a faithful substitute for the real claim
machinery it wraps (mirrors `test_deliverable_collision_warn.py`'s own
`resolve_operator_config`/`_resolve_claude_klabauter_bin` stubbing pattern).

Spec backlink: `coordinator_core/baton_assemble/__init__.py :: resolve_lineage`,
`expected_discovery_tier` kwarg and its `_DISCOVERY_TIER_EXPECTATION_ALIASES`
module constant.

Negative-spec:
    - The expectation is OPT-IN -- an absent `expected_discovery_tier` must
      reproduce today's exact `resolve_lineage` return dict, unchanged. This
      is the regression test protecting every existing caller, which passes
      nothing.
    - This does NOT touch `_scan_deliverable_collision`'s own warn-only
      advisory (a competing live baton holding the same `deliverable_id`) --
      that stays a warning regardless of `expected_discovery_tier`.

Run: python3 -m pytest
coordinator_core/baton_assemble/tests/test_discovery_tier_expectation.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import _write_artifact


def _write_deliverable_carrier(root: Path, rel: str, deliverable_id: str) -> Path:
    """A `docs/plans/*.md`-shaped input carrying only `deliverable_id` --
    the SAME fixture shape `test_deliverable_collision_warn.py`'s own
    `_write_deliverable_carrier` uses for the predecessor-carry
    (`discovery == "artifact"`) tier, restated here since that helper is
    module-private to its own test file."""
    return _write_artifact(root / rel, [f'deliverable_id: "{deliverable_id}"'])


def _write_handoff_predecessor(root: Path, rel: str, deliverable_id: str, handoff_id: str) -> Path:
    """A real `state/handoffs/*.md` predecessor record -- carries its own
    `handoff_id`, so `resolve_lineage`'s own-handoff-id discriminator routes
    it through the PREDECESSOR-carry tier (`discovery == "artifact"`)."""
    return _write_artifact(
        root / rel,
        [
            f"deliverable_id: {deliverable_id}",
            f"handoff_id: {handoff_id}",
            "status: claimed",
            "deployment_state: in_flight",
            "claimed_by: some-session-id",
        ],
    )


def _stub_claimed_plan(monkeypatch, claimed_plan_rel: str | None) -> None:
    """Forces `resolve_lineage`'s claimed-plan rung -- the `plan` tier -- to
    resolve to `claimed_plan_rel` (or to nothing, when `None`), bypassing
    the real session-id/git-claim-ledger resolution entirely."""
    monkeypatch.setattr(ba, "resolve_claimed_plan_path", lambda cwd=None: claimed_plan_rel)


class TestWrongArtifactRefusesLoudly:
    """The wrong-artifact case actually hit in the field: the session holds
    a claimed plan, and the caller declares `predecessor` but supplies that
    SAME plan as `artifact_path` (mirroring the observed defect: the plan
    tier answers first, by design, regardless of what artifact_path names).
    Declaring `predecessor` here is a mismatch against the tier that
    actually answers -- refused rather than silently accepted."""

    def test_declaring_predecessor_but_the_claimed_plan_answers_first_refuses(
        self, tmp_path, monkeypatch
    ):
        plan_rel = "docs/plans/2026-09-02-wrong-artifact.md"
        plan = _write_deliverable_carrier(tmp_path, plan_rel, "DEL-WRONG-ARTIFACT")
        _stub_claimed_plan(monkeypatch, plan_rel)

        with pytest.raises(ValueError) as excinfo:
            ba.resolve_lineage(
                "handoff", str(plan), tmp_path, expected_discovery_tier="predecessor"
            )
        message = str(excinfo.value)
        assert "predecessor" in message, f"expected tier must be named: {message!r}"
        assert "plan" in message, f"actual tier must be named: {message!r}"


class TestEachTierAcceptsItsOwnArtifact:
    def test_plan_tier_accepts_its_own_claimed_plan(self, tmp_path, monkeypatch):
        plan_rel = "docs/plans/2026-09-02-correct-plan.md"
        _write_deliverable_carrier(tmp_path, plan_rel, "DEL-CORRECT-PLAN")
        _stub_claimed_plan(monkeypatch, plan_rel)

        lineage = ba.resolve_lineage(
            "handoff", "", tmp_path, expected_discovery_tier="plan"
        )
        assert lineage["discovery"] == "plan"

    def test_predecessor_tier_accepts_a_predecessor_handoff(self, tmp_path, monkeypatch):
        _stub_claimed_plan(monkeypatch, None)
        predecessor = _write_handoff_predecessor(
            tmp_path,
            "state/handoffs/2026-09-02-correct-predecessor.md",
            "DEL-CORRECT-PREDECESSOR",
            "hnd-correct-predecessor-1a2b3c",
        )
        lineage = ba.resolve_lineage(
            "handoff", str(predecessor), tmp_path, expected_discovery_tier="predecessor"
        )
        assert lineage["discovery"] == "artifact"

    def test_mint_tier_accepts_no_artifact_and_no_claimed_plan(self, tmp_path, monkeypatch):
        _stub_claimed_plan(monkeypatch, None)
        lineage = ba.resolve_lineage(
            "handoff", "", tmp_path, expected_discovery_tier="mint"
        )
        assert lineage["discovery"] == "mint"


class TestAbsentExpectationIsByteIdentical:
    """The regression test protecting every existing caller, which passes
    `expected_discovery_tier` as nothing (not even explicitly `None`)."""

    def test_absent_expectation_reproduces_todays_exact_result(self, tmp_path, monkeypatch):
        _stub_claimed_plan(monkeypatch, None)
        predecessor = _write_deliverable_carrier(
            tmp_path, "docs/plans/2026-09-02-absent-expectation.md", "DEL-ABSENT-EXPECTATION"
        )

        without_kwarg = ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        with_explicit_none = ba.resolve_lineage(
            "handoff", str(predecessor), tmp_path, expected_discovery_tier=None
        )

        assert without_kwarg == with_explicit_none
        assert without_kwarg["discovery"] == "artifact"

    def test_absent_expectation_never_raises_on_a_mismatched_artifact(self, tmp_path, monkeypatch):
        """The exact live shape the defect reproduced: the claimed plan
        answers the `plan` tier regardless of the (mismatched) artifact_path
        supplied -- legal, unchanged, and must not raise when no expectation
        was declared."""
        plan_rel = "docs/plans/2026-09-02-no-expectation-declared.md"
        _write_deliverable_carrier(tmp_path, plan_rel, "DEL-NO-EXPECTATION")
        _stub_claimed_plan(monkeypatch, plan_rel)

        predecessor = _write_handoff_predecessor(
            tmp_path,
            "state/handoffs/2026-09-02-no-expectation-predecessor.md",
            "DEL-NO-EXPECTATION",
            "hnd-no-expectation-declared-1a2b3c",
        )
        lineage = ba.resolve_lineage("handoff", str(predecessor), tmp_path)
        assert lineage["discovery"] == "plan"


class TestExpectDiscoveryTierCliFlag:
    """`baton-assemble brief --expect-discovery-tier` end-to-end through
    `ba.main()`, not just the `resolve_lineage`/`brief` kwarg -- the CLI is
    the only door DoE-claude's loader (this mechanism's actual caller,
    across the seam) reaches this repo through."""

    def test_absent_flag_is_byte_identical_to_todays_exit_code(self, tmp_path, monkeypatch):
        from coordinator_core.test_baton_assemble import _init_repo, _FAKE_OPERATOR_CONFIG
        import os

        monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
        predecessor = _write_deliverable_carrier(
            tmp_path, "docs/plans/2026-09-02-cli-absent-flag.md", "DEL-CLI-ABSENT-FLAG"
        )
        _stub_claimed_plan(monkeypatch, None)
        _init_repo(tmp_path)
        old_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            exit_code = ba.main(["brief", "handoff", str(predecessor)])
        finally:
            os.chdir(old_cwd)
        assert exit_code == ba.EXIT_OK

    def test_declared_tier_matching_actual_tier_succeeds(self, tmp_path, monkeypatch):
        from coordinator_core.test_baton_assemble import _init_repo, _FAKE_OPERATOR_CONFIG
        import os

        monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
        predecessor = _write_handoff_predecessor(
            tmp_path,
            "state/handoffs/2026-09-02-cli-matching-tier.md",
            "DEL-CLI-MATCHING-TIER",
            "hnd-cli-matching-tier-1a2b3c",
        )
        _stub_claimed_plan(monkeypatch, None)
        _init_repo(tmp_path)
        old_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            exit_code = ba.main(
                ["brief", "handoff", str(predecessor), "--expect-discovery-tier", "predecessor"]
            )
        finally:
            os.chdir(old_cwd)
        assert exit_code == ba.EXIT_OK

    def test_declared_tier_refuses_on_the_wrong_artifact_doe_hit(self, tmp_path, monkeypatch, capsys):
        """The exact live defect: a governing plan supplied where the
        predecessor baton was wanted -- legal at the cascade level, but now
        refused when the caller declares `predecessor` via the CLI flag."""
        from coordinator_core.test_baton_assemble import _init_repo, _FAKE_OPERATOR_CONFIG
        import os

        monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
        plan_rel = "docs/plans/2026-09-02-cli-wrong-artifact.md"
        _write_deliverable_carrier(tmp_path, plan_rel, "DEL-CLI-WRONG-ARTIFACT")
        _stub_claimed_plan(monkeypatch, plan_rel)
        _init_repo(tmp_path)
        old_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            exit_code = ba.main(
                [
                    "brief",
                    "handoff",
                    str(tmp_path / plan_rel),
                    "--expect-discovery-tier",
                    "predecessor",
                ]
            )
        finally:
            os.chdir(old_cwd)
        assert exit_code == ba.EXIT_USAGE
        err = capsys.readouterr().err
        assert "predecessor" in err
        assert "plan" in err

    def test_unrecognized_tier_value_refuses_at_parse_time(self, tmp_path, capsys):
        exit_code = ba.main(
            ["brief", "handoff", str(tmp_path / "irrelevant.md"), "--expect-discovery-tier", "bogus"]
        )
        assert exit_code == ba.EXIT_USAGE
        err = capsys.readouterr().err
        assert "bogus" in err
        assert "plan" in err
        assert "predecessor" in err
        assert "mint" in err

    def test_missing_value_after_flag_is_usage_error(self, tmp_path):
        exit_code = ba.main(
            ["brief", "handoff", str(tmp_path / "irrelevant.md"), "--expect-discovery-tier"]
        )
        assert exit_code == ba.EXIT_USAGE

    def test_help_still_lists_the_new_flag(self, capsys):
        exit_code = ba.main(["brief", "--help"])
        assert exit_code == ba.EXIT_OK
        out = capsys.readouterr().out
        assert "--expect-discovery-tier" in out
