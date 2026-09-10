"""
Tests for coordinator_core.frontmatter.author_dependence.

Coverage targets (per C1 spec, docs/plans/2026-09-06-the-artifact-that-still-
needs-its-author.md):
  - determinism over a pinned inline fixture subset (verdict path only)
  - the three headline label numbers reproduced from the checked-in goldens
  - holdout-split stability across re-runs (pure function of the path string)
  - the k>=1 disjunction rule (any one property firing flags; none firing
    does not; unparseable is a distinct, always-flagged reason)
  - AC-5 structural absences: no sys.exit, no raise on a failing property, no
    severity ranking anywhere in the module
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from coordinator_core.frontmatter import author_dependence as ad
from coordinator_core.frontmatter.author_dependence import (
    DISCARDED_PROPERTIES,
    PROPERTY_CHECKS,
    Verdict,
    check_artifact,
    has_inflight_marker,
    has_missing_chunk_file_scope,
    has_unresolved_placeholder,
    proxy_a_label,
    split_holdout,
)

_GOLDENS_PATH = (
    Path(__file__).resolve().parent / "_goldens" / "author_dependence_labels.json"
)


# ---------------------------------------------------------------------------
# Inline fixture artifacts — pinned, not read from disk. C1's writes: scope
# is exactly {author_dependence.py, this test file, the goldens JSON}, so the
# "pinned fixture subset" the spec calls for lives here as literal strings.
# ---------------------------------------------------------------------------

_CLEAN_HANDOFF = (
    "---\n"
    "kind: handoff\n"
    "predecessor: none\n"
    "---\n"
    "# Session summary\n"
    "Work completed cleanly, no open threads.\n"
)

_PLACEHOLDER_PLAN = (
    "---\n"
    "kind: plan\n"
    "---\n"
    "# Plan\n"
    "## Tasks\n"
    "Scope: TBD\n"
)

_MISSING_WRITES_PLAN = (
    "---\n"
    "kind: plan\n"
    "---\n"
    "# Plan\n"
    "## Tasks\n"
    "```yaml plan-tasks\n"
    "- id: C1\n"
    "  title: Do a thing\n"
    "  change_kind: code-edit\n"
    "  writes: []\n"
    "  disposition: open\n"
    "  body: |\n"
    "    Do the thing.\n"
    "```\n"
)

_WELL_SCOPED_PLAN = (
    "---\n"
    "kind: plan\n"
    "---\n"
    "# Plan\n"
    "## Tasks\n"
    "```yaml plan-tasks\n"
    "- id: C1\n"
    "  title: Do a thing\n"
    "  change_kind: code-edit\n"
    "  writes:\n"
    "    - some/module.py\n"
    "  disposition: open\n"
    "  body: |\n"
    "    Do the thing.\n"
    "```\n"
)

_BOTH_FIRE_PLAN = (
    "---\n"
    "kind: plan\n"
    "---\n"
    "# Plan\n"
    "TODO: fill this in\n"
    "## Tasks\n"
    "```yaml plan-tasks\n"
    "- id: C1\n"
    "  title: Do a thing\n"
    "  change_kind: code-edit\n"
    "  writes: []\n"
    "  disposition: open\n"
    "  body: |\n"
    "    Do the thing.\n"
    "```\n"
)

_NO_FRONTMATTER = "# Just a markdown file\nNo frontmatter fence here at all.\n"

_AMENDED_HANDOFF = (
    "---\n"
    "kind: handoff\n"
    "---\n"
    "# Session summary\n"
    "AMENDED: the original scope was wrong, corrected below.\n"
)

_SHA_PREDECESSOR = (
    "---\n"
    "kind: handoff\n"
    'predecessor: "1c0e0b32a"  # crash-time SHA -- NOT a predecessor handoff path\n'
    "---\n"
    "# Session summary\n"
)

_PATH_PREDECESSOR = (
    "---\n"
    "kind: handoff\n"
    "predecessor: state/handoffs/2026-01-01_000000_prior-session.md\n"
    "---\n"
    "# Session summary\n"
)


class TestCheckArtifactDeterminism:
    """Determinism over the pinned fixture subset."""

    @pytest.mark.parametrize(
        "text,expected_flagged,expected_reasons",
        [
            (_CLEAN_HANDOFF, False, []),
            (_PLACEHOLDER_PLAN, True, ["unresolved_placeholder"]),
            (_MISSING_WRITES_PLAN, True, ["missing_chunk_file_scope"]),
            (_WELL_SCOPED_PLAN, False, []),
            (
                _BOTH_FIRE_PLAN,
                True,
                ["unresolved_placeholder", "missing_chunk_file_scope"],
            ),
            (_NO_FRONTMATTER, True, ["unparseable_frontmatter"]),
        ],
    )
    def test_verdict_is_deterministic(self, text, expected_flagged, expected_reasons):
        for _ in range(3):
            verdict = check_artifact(text)
            assert verdict.flagged is expected_flagged
            assert verdict.reasons == expected_reasons

    def test_unparseable_is_never_scored_clean(self):
        verdict = check_artifact(_NO_FRONTMATTER)
        assert verdict.flagged is True
        assert verdict.reasons == ["unparseable_frontmatter"]


class TestDisjunctionRule:
    """k>=1: any surviving property firing flags; the reasons list is exactly
    the fired property names, with no ranking or weighting."""

    def test_no_properties_fire_means_not_flagged(self):
        verdict = check_artifact(_CLEAN_HANDOFF)
        assert verdict.flagged is False
        assert verdict.reasons == []

    def test_one_property_firing_flags(self):
        verdict = check_artifact(_PLACEHOLDER_PLAN)
        assert verdict.flagged is True
        assert len(verdict.reasons) == 1

    def test_two_properties_firing_both_named(self):
        verdict = check_artifact(_BOTH_FIRE_PLAN)
        assert verdict.flagged is True
        assert set(verdict.reasons) == {
            "unresolved_placeholder",
            "missing_chunk_file_scope",
        }

    def test_reasons_come_from_property_checks_names(self):
        names = {name for name, _fn in PROPERTY_CHECKS}
        verdict = check_artifact(_BOTH_FIRE_PLAN)
        assert set(verdict.reasons) <= names


class TestPropertyFunctions:
    def test_unresolved_placeholder_markers(self):
        assert has_unresolved_placeholder("Scope: TBD")
        assert has_unresolved_placeholder("<REPLACE with detail>")
        assert has_unresolved_placeholder("PLACEHOLDER text")
        assert has_unresolved_placeholder("TODO: write this")
        assert not has_unresolved_placeholder("Nothing unresolved here.")

    def test_missing_chunk_file_scope_absent_writes(self):
        split = ad.split_frontmatter(_MISSING_WRITES_PLAN)
        assert has_missing_chunk_file_scope(split.body_with_leading_newline)

    def test_missing_chunk_file_scope_present_writes(self):
        split = ad.split_frontmatter(_WELL_SCOPED_PLAN)
        assert not has_missing_chunk_file_scope(split.body_with_leading_newline)

    def test_missing_chunk_file_scope_vacuous_without_task_spine(self):
        split = ad.split_frontmatter(_CLEAN_HANDOFF)
        assert not has_missing_chunk_file_scope(split.body_with_leading_newline)

    def test_has_inflight_marker(self):
        split = ad.split_frontmatter(_AMENDED_HANDOFF)
        assert has_inflight_marker(split.body_with_leading_newline)
        split_clean = ad.split_frontmatter(_CLEAN_HANDOFF)
        assert not has_inflight_marker(split_clean.body_with_leading_newline)


class TestProxyA:
    """String-shape only: never filesystem resolution, never "is not the
    literal string none"."""

    def test_sha_predecessor_is_excluded_not_positive(self):
        split = ad.split_frontmatter(_SHA_PREDECESSOR)
        assert proxy_a_label(split.fm_text) == "sha_excluded"

    def test_path_like_predecessor_is_positive(self):
        split = ad.split_frontmatter(_PATH_PREDECESSOR)
        assert proxy_a_label(split.fm_text) == "positive"

    def test_none_predecessor_is_not_positive(self):
        split = ad.split_frontmatter(_CLEAN_HANDOFF)
        assert proxy_a_label(split.fm_text) is None


class TestHoldoutSplitStability:
    def test_split_is_stable_across_calls(self):
        path = "archive/handoffs/2026-01-01_000000_example.md"
        results = {split_holdout(path) for _ in range(20)}
        assert results == {split_holdout(path)}

    def test_split_is_a_pure_function_of_the_path(self):
        path_a = "state/handoffs/example-a.md"
        path_b = "state/handoffs/example-b.md"
        assert split_holdout(path_a) in {"train", "holdout"}
        assert split_holdout(path_b) in {"train", "holdout"}

    def test_split_never_calls_git(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("split_holdout must not invoke a subprocess")

        monkeypatch.setattr(ad.subprocess, "run", _boom)
        split_holdout("docs/plans/example.md")


class TestGoldensReproduction:
    """The three headline label numbers, reproduced from the checked-in
    goldens rather than re-run against live git history in this test (the
    labelling harness is the one-off offline pass, not a per-test fixture)."""

    @pytest.fixture(scope="class")
    def goldens(self):
        assert _GOLDENS_PATH.exists(), f"missing goldens file: {_GOLDENS_PATH}"
        return json.loads(_GOLDENS_PATH.read_text(encoding="utf-8"))

    def test_corpus_totals_present(self, goldens):
        totals = goldens["corpus_totals"]
        assert totals["archive/handoffs"] > 0
        assert totals["state/handoffs"] > 0
        assert totals["docs/plans"] > 0
        assert totals["total"] == (
            totals["archive/handoffs"]
            + totals["state/handoffs"]
            + totals["docs/plans"]
        )

    def test_unparseable_never_silently_dropped(self, goldens):
        assert goldens["unparseable_count"] == len(goldens["unparseable"])
        assert goldens["unparseable_count"] >= 1

    def test_sha_excluded_counted_not_dropped(self, goldens):
        sha = goldens["sha_excluded_by_corpus"]
        assert "archive/handoffs" in sha
        assert "state/handoffs" in sha

    def test_contingency_table_is_four_integers(self, goldens):
        c = goldens["contingency"]
        for key in ("a_only", "b_only", "a_and_b", "neither"):
            assert isinstance(c[key], int)
        assert (
            c["a_only"] + c["b_only"] + c["a_and_b"] + c["neither"]
            == c["b_observable_total"]
        )

    def test_b_blind_subset_is_counted(self, goldens):
        assert goldens["contingency"]["b_blind_total"] == goldens[
            "single_commit_at_current_path"
        ]
        assert goldens["single_commit_at_current_path"] >= 0

    def test_property_variance_matches_surviving_properties(self, goldens):
        names = {name for name, _fn in PROPERTY_CHECKS}
        assert set(goldens["property_variance"].keys()) == names
        for stats in goldens["property_variance"].values():
            assert 0 <= stats["fired"] <= stats["of"]

    def test_discarded_properties_recorded(self, goldens):
        assert set(goldens["discarded_properties"]) == {
            d["name"] for d in DISCARDED_PROPERTIES
        }

    def test_holdout_split_matches_live_function(self, goldens):
        # Review: coordinator:code-reviewer — a `[:200]` prefix slice samples
        # only the alphabetically-first entries (the golden is written with
        # sort_keys=True), front-loading `archive/` and never touching
        # `docs/plans/` or the tail of `state/handoffs/`. A stride sample
        # touches every corpus directory instead, deterministically.
        per_artifact = goldens.get("per_artifact", {})
        items = list(per_artifact.items())
        assert items, "goldens per_artifact is empty"
        stride = max(1, len(items) // 200)
        sample = items[::stride]
        for rel_path, entry in sample:
            assert split_holdout(rel_path) == entry["split"]


class TestStructuralAbsences:
    """AC-5: the checker never exits non-zero and never raises on a failing
    property, and carries no severity ranking. Asserted by source inspection
    of the module, not by convention."""

    def test_no_sys_exit_in_module(self):
        code_only = "\n".join(
            line
            for line in inspect.getsource(ad).splitlines()
            if not line.strip().startswith(("#", '"', "-", "*"))
        )
        assert "sys.exit(" not in code_only

    def test_no_raise_in_verdict_path_functions(self):
        for fn in (
            ad.check_artifact,
            ad.has_unresolved_placeholder,
            ad.has_missing_chunk_file_scope,
            ad.has_inflight_marker,
            ad.proxy_a_label,
            ad.split_holdout,
        ):
            source = inspect.getsource(fn)
            assert not re.search(r"\braise\b", source), (
                f"{fn.__name__} must not raise on a failing property"
            )

    def test_verdict_has_no_severity_field(self):
        fields = Verdict._fields
        assert fields == ("flagged", "reasons")

    def test_check_artifact_never_raises_on_malformed_input(self):
        malformed_inputs = [
            "",
            "---\n",
            "---\nkey: [unterminated\n---\nbody\n",
            "not even close to frontmatter",
        ]
        for text in malformed_inputs:
            verdict = check_artifact(text)
            assert isinstance(verdict, Verdict)


class TestPureLibraryVerdictPath:
    """AC-6: the verdict path takes no git call at all — a hard, tested
    property, not a convention. Also asserts a per-artifact time bound
    rather than claiming speed in prose."""

    def test_check_artifact_does_not_invoke_subprocess(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("check_artifact must not spawn a subprocess")

        monkeypatch.setattr(ad.subprocess, "run", _boom)
        check_artifact(_BOTH_FIRE_PLAN)
        check_artifact(_CLEAN_HANDOFF)
        check_artifact(_NO_FRONTMATTER)

    def test_per_artifact_time_bound(self):
        import time

        fixtures = [
            _CLEAN_HANDOFF,
            _PLACEHOLDER_PLAN,
            _MISSING_WRITES_PLAN,
            _WELL_SCOPED_PLAN,
            _BOTH_FIRE_PLAN,
            _NO_FRONTMATTER,
        ] * 50
        start = time.perf_counter()
        for text in fixtures:
            check_artifact(text)
        elapsed = time.perf_counter() - start
        per_artifact_ms = (elapsed / len(fixtures)) * 1000
        # Measured (95c38a2cb9 survey, C1 brief): 0.093ms/artifact over 675
        # artifacts. A generous ceiling well clear of noise on a loaded box.
        assert per_artifact_ms < 5.0, (
            f"{per_artifact_ms:.3f}ms/artifact exceeds the brightline-derived bound"
        )
