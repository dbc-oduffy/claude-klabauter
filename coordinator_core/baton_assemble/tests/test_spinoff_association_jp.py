"""Tests for `j-spinoff-plan-sizing` -- R6 (2026-08-21): a spinoff mint
cannot know which plan or sizing object it belongs to; that is genuine
EM/PM knowledge. `_build_judgment_points` asks via the existing
`judgment_points[]` mechanism rather than inferring or scanning for
candidates (a candidate list is a corpus walk wearing a helpful hat).

`origin_plan_id`/`governing_plan` (the ALREADY-resolved parent-provenance
rung read off `artifact_path`'s own frontmatter) are a distinct concern
from this judgment point, which is a forward-looking association the EM
supplies for the freshly-minted spinoff itself. Narrow by ruling (F10
rejected): this judgment point is scoped to spinoff mint only and is
never emitted for kind="handoff".

Spec backlink: coordinator_core/baton_assemble/__init__.py
`_build_judgment_points`'s `j-spinoff-plan-sizing` entry.
"""

from __future__ import annotations

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.test_baton_assemble import _FAKE_OPERATOR_CONFIG, _write_artifact

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """Restated per-module (autouse fixtures do not cross module boundaries)
    -- `brief()` calls `resolve_operator_config()` unconditionally (B0 seam
    assertion), which resolves real per-machine settings_home/claude_klabauter_root
    values absent this stub. Mirrors
    `test_j_continuation_vs_fork_excise.py`'s own fixture of the same name."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))


class TestJSpinoffPlanSizingEmittedOnlyForSpinoff:
    def test_emitted_on_a_bare_slug_spinoff_mint(self, tmp_path):
        decision = ba.brief(
            "spinoff", "a-fresh-mint-slug-c7", repo_root=tmp_path
        ).decision_object
        ids = {jp["id"] for jp in decision["judgment_points"]}
        assert "j-spinoff-plan-sizing" in ids, ids

    def test_absent_on_a_handoff_kind_brief(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ["deliverable_id: DEL-C7-HANDOFF", "initiative: init-c7"],
        )
        decision = ba.brief(
            "handoff", str(artifact), repo_root=tmp_path
        ).decision_object
        ids = {jp["id"] for jp in decision["judgment_points"]}
        assert "j-spinoff-plan-sizing" not in ids, ids


class TestJSpinoffPlanSizingDispositions:
    def test_advertises_associate_and_none_only(self, tmp_path):
        decision = ba.brief(
            "spinoff", "a-fresh-mint-slug-c7", repo_root=tmp_path
        ).decision_object
        jp = next(
            j for j in decision["judgment_points"] if j["id"] == "j-spinoff-plan-sizing"
        )
        values = {d["value"] for d in jp["dispositions"]}
        assert values == {"associate", "none"}

    def test_none_disposition_resolves_nothing_and_carries_guidance(self, tmp_path):
        """'none' is cheap to give and recorded as a TRUE absence -- it
        resolves no directive (nothing gates on the answer) and still
        carries guidance distinguishing it from an unanswered question."""
        decision = ba.brief(
            "spinoff", "a-fresh-mint-slug-c7", repo_root=tmp_path
        ).decision_object
        jp = next(
            j for j in decision["judgment_points"] if j["id"] == "j-spinoff-plan-sizing"
        )
        none_disposition = next(d for d in jp["dispositions"] if d["value"] == "none")
        assert none_disposition["resolves"] == []
        assert none_disposition.get("guidance")

    def test_associate_disposition_carries_guidance_and_resolves_nothing(self, tmp_path):
        decision = ba.brief(
            "spinoff", "a-fresh-mint-slug-c7", repo_root=tmp_path
        ).decision_object
        jp = next(
            j for j in decision["judgment_points"] if j["id"] == "j-spinoff-plan-sizing"
        )
        associate_disposition = next(
            d for d in jp["dispositions"] if d["value"] == "associate"
        )
        assert associate_disposition["resolves"] == []
        assert associate_disposition.get("guidance")

    def test_carries_no_recommendation(self, tmp_path):
        """Built via `build_untrusted_gate_judgment_point`, matching the
        other three spinoff/handoff-shared judgment points -- structurally
        impossible to attach a verdict here."""
        decision = ba.brief(
            "spinoff", "a-fresh-mint-slug-c7", repo_root=tmp_path
        ).decision_object
        jp = next(
            j for j in decision["judgment_points"] if j["id"] == "j-spinoff-plan-sizing"
        )
        assert jp["recommendation"] is None


class TestJSpinoffPlanSizingAnswerReachesTheMint:
    """The half that did not exist until 2026-09-04.

    `associate` collected the plan id and sizing slug as `decision_note` prose
    and nothing read it, so the minted spinoff carried the association
    nowhere. Reported from example-cockpit-repo via DoE-claude. The answer now
    travels as STRUCTURED keys into `governing_plan` / `sizing_object` -- not
    into `origin_plan_id`, which is the backward-looking progenitor rung this
    judgment point's own comment already declares itself distinct from.
    """

    def _d1c_args(self, decision):
        d1c = [d for d in decision["directives"] if d["id"] == "d1c"]
        return d1c[0]["args"] if d1c else None

    def test_associate_stamps_governing_plan_onto_the_mint(self, tmp_path):
        decision = ba.brief(
            "spinoff",
            "a-fresh-mint-slug-c7",
            repo_root=tmp_path,
            decisions={
                "j-spinoff-plan-sizing": {
                    "disposition": "associate",
                    "governing_plan": "docs/plans/2026-09-04-some-plan.md",
                    "decision_note": "the PM spun this chunk out of that plan",
                }
            },
        ).decision_object
        args = self._d1c_args(decision)
        assert args is not None, decision["directives"]
        assert "--governing-plan=docs/plans/2026-09-04-some-plan.md" in args

    def test_associate_stamps_sizing_object_onto_the_mint(self, tmp_path):
        decision = ba.brief(
            "spinoff",
            "a-fresh-mint-slug-c7",
            repo_root=tmp_path,
            decisions={
                "j-spinoff-plan-sizing": {
                    "disposition": "associate",
                    "sizing_object": "state/sizings/2026-09-04-some-ask.yaml",
                }
            },
        ).decision_object
        args = self._d1c_args(decision)
        assert args is not None, decision["directives"]
        assert "--sizing-object=state/sizings/2026-09-04-some-ask.yaml" in args

    def test_none_disposition_stamps_nothing(self, tmp_path):
        """The cheap true-absence answer must not mint a stamp directive."""
        decision = ba.brief(
            "spinoff",
            "a-fresh-mint-slug-c7",
            repo_root=tmp_path,
            decisions={"j-spinoff-plan-sizing": {"disposition": "none"}},
        ).decision_object
        assert self._d1c_args(decision) is None

    def test_associate_naming_nothing_fails_loud(self, tmp_path):
        """`associate` with neither key is the `none` answer wearing the wrong
        label -- refused rather than recorded as an association nobody made."""
        with pytest.raises(ValueError, match="at least one of governing_plan"):
            ba.brief(
                "spinoff",
                "a-fresh-mint-slug-c7",
                repo_root=tmp_path,
                decisions={
                    "j-spinoff-plan-sizing": {
                        "disposition": "associate",
                        "decision_note": "belongs to the thing we discussed",
                    }
                },
            )

    def test_associate_on_a_handoff_kind_fails_loud(self, tmp_path):
        """A continuation carries governing_plan/sizing forward from its
        predecessor; an association answered here would contradict that
        silently, so it is refused rather than ignored."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ["deliverable_id: DEL-C7-HANDOFF", "initiative: init-c7"],
        )
        with pytest.raises(ValueError, match="spinoff-mint"):
            ba.brief(
                "handoff",
                str(artifact),
                repo_root=tmp_path,
                decisions={
                    "j-spinoff-plan-sizing": {
                        "disposition": "associate",
                        "governing_plan": "docs/plans/2026-09-04-some-plan.md",
                    }
                },
            )
