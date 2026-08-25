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
    assertion), which resolves real per-machine settings_home/makima_root
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
