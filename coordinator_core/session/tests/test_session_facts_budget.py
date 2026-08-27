"""
coordinator_core.session.tests.test_session_facts_budget — pins X against the
artifact it was armed from (`fl-core-04` C4).

Purpose: the constants here are a RECORDED MEASUREMENT. These tests pin them to
the artifact's stated figures so a future edit that changes the dial without
changing the derivation record fails loudly, and assert the declarative-only
posture the plan settled — that nothing reads X.

Spec backlink: docs/plans/2026-08-27-the-fact-layer-is-measured-on-the-one-hot-path.md § C4
               docs/research/2026-08-27-fact-layer-hot-path-measured.md
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.benchmarks import fact_layer_hot_path as flhp
from coordinator_core.session import session_facts_budget as budget

_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestArmedValuesMatchTheArtifact:
    def test_spawn_budget_is_the_measured_worst_case_times_the_headroom(self):
        assert budget.MEASURED_WORST_GIT_SPAWNS_PER_CEREMONY == 8
        assert budget.HEADROOM_MULTIPLIER == 2.0
        assert budget.FACT_LAYER_PER_CEREMONY_GIT_SPAWN_BUDGET == 16

    def test_file_read_budget_is_the_measured_worst_case_times_the_headroom(self):
        assert budget.MEASURED_WORST_FILE_READS_PER_CEREMONY == 3
        assert budget.FACT_LAYER_PER_CEREMONY_FILE_READ_BUDGET == 6

    def test_the_measured_worst_case_still_matches_the_structural_leg(self):
        """The dial was armed off the structural counts. If those counts move —
        a new conditional spawn, a retired one — this fails rather than letting
        the constant quietly describe a call graph that no longer exists."""
        counts = flhp.all_structural_counts()
        on_production_path = [
            counts[name]
            for name in flhp.FACT_NAMES
            if name != flhp.FACT_WITH_NO_PRODUCTION_CONSUMER
        ]

        worst_spawns = sum(c.git_spawns_max for c in on_production_path)
        worst_reads = sum(c.file_reads_max for c in on_production_path)

        assert worst_spawns == budget.MEASURED_WORST_GIT_SPAWNS_PER_CEREMONY
        assert worst_reads == budget.MEASURED_WORST_FILE_READS_PER_CEREMONY

    def test_the_armed_value_leaves_real_headroom_over_the_measurement(self):
        assert (
            budget.FACT_LAYER_PER_CEREMONY_GIT_SPAWN_BUDGET
            > budget.MEASURED_WORST_GIT_SPAWNS_PER_CEREMONY
        )


class TestDeclarativeOnly:
    """X is unread ON PURPOSE. These tests pin that posture so a later reader
    does not 'finish' it by wiring a consumer."""

    def test_the_module_docstring_says_it_is_declarative_only_and_unread(self):
        doc = budget.__doc__ or ""
        assert "DECLARATIVE-ONLY" in doc
        assert "unread" in doc.lower()

    def test_nothing_in_the_engine_reads_the_armed_constant(self):
        """A text sweep, not an import graph: the point is that no call site
        exists anywhere, including in code that never imports this module.

        Swept in-process rather than through `git grep` — a test for a plan
        whose whole subject is per-ceremony spawn cost should not itself spawn
        a process to run (DR-344: "git justifies itself per use")."""
        needle = "FACT_LAYER_PER_CEREMONY_GIT_SPAWN_BUDGET"
        allowed = {Path("coordinator_core/session/session_facts_budget.py")}

        hits = set()
        for path in (_REPO_ROOT / "coordinator_core").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            # A test naming the dial is asserting its shape, not consuming it.
            # Only a PRODUCTION reader would make X an enforcing ceiling.
            if "tests" in path.parts or path.name.startswith("test_"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle in text:
                hits.add(path.relative_to(_REPO_ROOT))

        assert hits <= allowed, f"X has acquired a reader: {sorted(map(str, hits))}"

    def test_it_does_not_import_the_composition_budget(self):
        source = Path(budget.__file__).read_text(encoding="utf-8")
        assert "composition_budget" not in source.split('"""')[2]


class TestRFourIsCarriedInTheModule:
    def test_per_fact_ceilings_do_not_discharge_the_aggregate(self):
        assert budget.PER_FACT_CEILINGS_DO_NOT_DISCHARGE_THIS is True
        assert "R-04" in (budget.__doc__ or "") or "R-04" in Path(
            budget.__file__
        ).read_text(encoding="utf-8")
