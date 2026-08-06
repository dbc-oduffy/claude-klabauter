"""
coordinator_core.ops.ceremony.tests.test_records_query — unit tests for the C8a
in-process records-query helper.

Coverage:
  (a) handoff enumerate + where-filter — equality-AND matches, non-matches excluded.
  (b) handoff-archived recursive enumeration under YYYY-MM/ subdirectories.
  (c) cross-repo-memo memo-shape guard — files lacking from/to are excluded.
  (d) unknown type raises ValueError.
  (e) limit truncation in readdir order.
  (f) consumed-marker normalization applies to handoff/handoff-archived before filtering.

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C8a
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from coordinator_core.ops.ceremony.records_query import (
    legacy_prose_signal,
    query_records,
    query_unattached_all,
)
from coordinator_core.ops.records_query import _legacy_prose_signal


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")
    return path


class TestHandoffEnumerateAndFilter:
    def test_matching_records_returned(self, tmp_path: Path):
        _write(
            tmp_path / "state/handoffs/hoff-a.md",
            """\
            ---
            status: open
            workstream: alpha
            ---
            Body A.
            """,
        )
        _write(
            tmp_path / "state/handoffs/hoff-b.md",
            """\
            ---
            status: open
            workstream: beta
            ---
            Body B.
            """,
        )
        results = query_records("handoff", tmp_path, where="workstream=alpha")
        assert len(results) == 1
        assert results[0]["path"] == "state/handoffs/hoff-a.md"
        assert results[0]["frontmatter"]["workstream"] == "alpha"

    def test_no_where_returns_all(self, tmp_path: Path):
        _write(tmp_path / "state/handoffs/h1.md", "---\nstatus: open\n---\nBody.\n")
        _write(tmp_path / "state/handoffs/h2.md", "---\nstatus: open\n---\nBody.\n")
        results = query_records("handoff", tmp_path)
        assert len(results) == 2

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        assert query_records("handoff", tmp_path) == []

    def test_no_frontmatter_skipped(self, tmp_path: Path):
        _write(tmp_path / "state/handoffs/h1.md", "No frontmatter here.\n")
        assert query_records("handoff", tmp_path) == []


class TestHandoffArchivedRecursive:
    def test_recursive_enumeration(self, tmp_path: Path):
        _write(
            tmp_path / "archive/handoffs/2026-07/h1.md",
            "---\nstatus: claimed\n---\nBody.\n",
        )
        _write(
            tmp_path / "archive/handoffs/2026-06/h2.md",
            "---\nstatus: claimed\n---\nBody.\n",
        )
        results = query_records("handoff-archived", tmp_path)
        paths = sorted(r["path"] for r in results)
        assert paths == [
            "archive/handoffs/2026-06/h2.md",
            "archive/handoffs/2026-07/h1.md",
        ]


class TestCrossRepoMemoShapeGuard:
    def test_memo_with_from_to_included(self, tmp_path: Path):
        _write(
            tmp_path / "cross-repo/inbox/memo-1.md",
            """\
            ---
            from: repo-a
            to: repo-b
            status: open
            ---
            Ask body.
            """,
        )
        results = query_records("cross-repo-memo", tmp_path)
        assert len(results) == 1
        assert results[0]["frontmatter"]["from"] == "repo-a"

    def test_readme_without_from_to_excluded(self, tmp_path: Path):
        _write(
            tmp_path / "cross-repo/inbox/README.md",
            """\
            ---
            title: inbox readme
            ---
            Not a memo.
            """,
        )
        assert query_records("cross-repo-memo", tmp_path) == []

    def test_where_filter_on_status(self, tmp_path: Path):
        _write(
            tmp_path / "cross-repo/inbox/memo-open.md",
            "---\nfrom: a\nto: b\nstatus: open\n---\nBody.\n",
        )
        _write(
            tmp_path / "cross-repo/inbox/memo-closed.md",
            "---\nfrom: a\nto: b\nstatus: actioned\n---\nBody.\n",
        )
        results = query_records("cross-repo-memo", tmp_path, where="status=open")
        assert len(results) == 1
        assert results[0]["path"] == "cross-repo/inbox/memo-open.md"


class TestLimitAndUnknownType:
    def test_limit_truncates_in_readdir_order(self, tmp_path: Path):
        for i in range(5):
            _write(
                tmp_path / f"state/handoffs/h{i}.md",
                "---\nstatus: open\n---\nBody.\n",
            )
        results = query_records("handoff", tmp_path, limit=2)
        assert len(results) == 2
        # readdir (alpha-sorted) order: h0, h1
        assert [r["path"] for r in results] == [
            "state/handoffs/h0.md",
            "state/handoffs/h1.md",
        ]

    def test_zero_limit_is_unlimited(self, tmp_path: Path):
        for i in range(3):
            _write(
                tmp_path / f"state/handoffs/h{i}.md",
                "---\nstatus: open\n---\nBody.\n",
            )
        results = query_records("handoff", tmp_path, limit=0)
        assert len(results) == 3

    def test_unknown_type_raises_value_error(self, tmp_path: Path):
        with pytest.raises(ValueError, match="unknown type"):
            query_records("not-a-real-type", tmp_path)


class TestConsumedMarkerNormalization:
    def test_consumed_marker_sets_shipped_state(self, tmp_path: Path):
        _write(
            tmp_path / "state/handoffs/h1.md",
            """\
            ---
            status: open
            deployment_state: in_flight
            ---
            <!-- consumed: 2026-07-15 -->
            Body.
            """,
        )
        results = query_records("handoff", tmp_path)
        assert len(results) == 1
        fm = results[0]["frontmatter"]
        assert fm["deployment_state"] == "shipped"
        assert fm["status"] == "claimed"
        assert fm["claimed_at"] == "2026-07-15"

    def test_consumed_marker_filterable_via_where(self, tmp_path: Path):
        _write(
            tmp_path / "state/handoffs/h1.md",
            """\
            ---
            status: open
            deployment_state: in_flight
            ---
            <!-- consumed: 2026-07-15 -->
            Body.
            """,
        )
        results = query_records("handoff", tmp_path, where="status=claimed")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# T4d-g1c EXTEND — widened type set, .yaml whole-file parsing, since= support,
# and plan-type sidecar filtering reused from coordinator_core.ops.records_query.
# ---------------------------------------------------------------------------


class TestWidenedTypeSet:
    def test_bug_yaml_type_supported(self, tmp_path: Path):
        d = tmp_path / "state/bug-backlog"
        d.mkdir(parents=True)
        (d / "2026-07-22-example.yaml").write_text(
            "title: Example bug\nstatus: open\n", encoding="utf-8",
        )
        results = query_records("bug", tmp_path)
        assert len(results) == 1
        assert results[0]["frontmatter"]["title"] == "Example bug"

    def test_roadmap_wildcard_dir_type_supported(self, tmp_path: Path):
        for name in ("aaa-roadmap", "zzz-roadmap"):
            d = tmp_path / "state/roadmap" / name
            d.mkdir(parents=True)
            (d / "OVERVIEW.md").write_text(
                "---\nstatus: draft\n---\nBody.\n", encoding="utf-8",
            )
        results = query_records("roadmap", tmp_path)
        assert [Path(r["path"]).parent.name for r in results] == [
            "aaa-roadmap", "zzz-roadmap",
        ]
        # normalizeRoadmapStatus applied: draft -> planning.
        assert all(r["frontmatter"]["status"] == "planning" for r in results)


class TestPlanTypeFilteringReused:
    def test_sidecar_excluded_anomaly_warned(self, tmp_path: Path):
        d = tmp_path / "docs/plans"
        d.mkdir(parents=True)
        _write(d / "2026-07-22-my-plan.md", "---\nstatus: draft\n---\nBody.\n")
        _write(d / "2026-07-22-my-plan.review.md", "---\nreviewer: x\n---\nBody.\n")
        results = query_records("plan", tmp_path)
        assert [Path(r["path"]).name for r in results] == ["2026-07-22-my-plan.md"]


class TestSinceSupport:
    def test_since_excludes_older_and_missing_created(self, tmp_path: Path):
        d = tmp_path / "state/handoffs"
        d.mkdir(parents=True)
        _write(d / "old.md", "---\nstatus: open\ncreated: 2020-01-01\n---\nBody.\n")
        _write(d / "recent.md", "---\nstatus: open\ncreated: 2026-07-01\n---\nBody.\n")
        _write(d / "no-created.md", "---\nstatus: open\n---\nBody.\n")
        results = query_records("handoff", tmp_path, since="2026-01-01")
        paths = {r["path"] for r in results}
        assert paths == {"state/handoffs/recent.md"}


class TestDecisionReviewLessonTypesSupported:
    """decision/review/lesson: static globs, no schema-registry port required
    (see records_query.py module Negative-spec — no sibling collision)."""

    def test_decision_type_supported(self, tmp_path: Path):
        _write(
            tmp_path / "docs/decisions/2026-07-01-example.md",
            "---\ntitle: Example Decision\nstatus: accepted\n---\nBody.\n",
        )
        results = query_records("decision", tmp_path)
        assert len(results) == 1
        assert results[0]["frontmatter"]["liveness"] == "DONE"

    def test_review_type_supported(self, tmp_path: Path):
        _write(
            tmp_path / "state/reviews/2026-07-01-example.md",
            "---\ntitle: Example Review\nreviewer: the Staff Engineer\n---\nBody.\n",
        )
        results = query_records("review", tmp_path)
        assert len(results) == 1
        assert results[0]["frontmatter"]["reviewer"] == "the Staff Engineer"

    def test_lesson_yaml_whole_file_type_supported(self, tmp_path: Path):
        d = tmp_path / "state/lessons"
        d.mkdir(parents=True)
        (d / "2026-07-01-example.yaml").write_text(
            "title: Example Lesson\ntier: universal\nstatus: deferred\n",
            encoding="utf-8",
        )
        results = query_records("lesson", tmp_path)
        assert len(results) == 1
        assert results[0]["frontmatter"]["liveness"] == "BLOCKED"

    def test_absent_directories_yield_empty_not_error(self, tmp_path: Path):
        assert query_records("decision", tmp_path) == []
        assert query_records("review", tmp_path) == []
        assert query_records("lesson", tmp_path) == []


class TestSyntheticTypesSupported:
    """handoff-ledger/research-claim: N-records-per-file synthetic collection,
    routed around the one-file-one-record loop entirely."""

    def test_handoff_ledger_where_and_limit_compose(self, tmp_path: Path):
        _write(
            tmp_path / "state/handoffs/h1.md",
            "## Session Ledger\n\n| Field | Value |\n|---|---|\n"
            "| tshirt | L |\n| session_id | sid-1 |\n",
        )
        _write(
            tmp_path / "state/handoffs/h2.md",
            "## Session Ledger\n\n| Field | Value |\n|---|---|\n"
            "| tshirt | S |\n| session_id | sid-2 |\n",
        )
        results = query_records("handoff-ledger", tmp_path, where="tshirt=L")
        assert [r["path"] for r in results] == ["state/handoffs/h1.md#ledger-0"]

    def test_research_claim_limit_applies(self, tmp_path: Path):
        import json

        d = tmp_path / "docs/research"
        d.mkdir(parents=True)
        (d / "example.claims.json").write_text(
            json.dumps([{"claim_text": f"claim {i}"} for i in range(5)]),
            encoding="utf-8",
        )
        results = query_records("research-claim", tmp_path, limit=2)
        assert len(results) == 2
        assert results[0]["path"] == "docs/research/example.claims.json#claim-0"

    def test_absent_directories_yield_empty_not_error(self, tmp_path: Path):
        assert query_records("handoff-ledger", tmp_path) == []
        assert query_records("research-claim", tmp_path) == []


class TestQueryUnattachedAll:
    """``query_unattached_all`` — the in-process ceremony wrapper over
    ``coordinator_core.ops.records_query._query_unattached_all``."""

    def test_union_across_member_types_excludes_attached_and_non_member(self, tmp_path: Path):
        _write(
            tmp_path / "state/bug-backlog/2026-07-01-attached.yaml",
            "title: Attached bug\nstatus: open\ninitiative: init-x\n",
        )
        _write(
            tmp_path / "state/bug-backlog/2026-07-02-unattached.yaml",
            "title: Unattached bug\nstatus: open\n",
        )
        _write(
            tmp_path / "docs/plans/2026-07-02-unattached-plan.md",
            "---\nstatus: implemented\n---\nBody.\n",
        )
        # Non-member type — decision has no `initiative` field either, but
        # must never surface since it's not in UNATTACHED_TYPES.
        _write(
            tmp_path / "docs/decisions/2026-07-01-example.md",
            "---\nstatus: accepted\n---\nBody.\n",
        )

        results = query_unattached_all(tmp_path)
        types = {r["_type"] for r in results}
        titles = {r["frontmatter"].get("title") for r in results}

        assert types == {"bug", "plan"}
        assert "Attached bug" not in titles
        assert "Unattached bug" in titles

    def test_where_and_since_apply_per_type_before_union(self, tmp_path: Path):
        _write(
            tmp_path / "state/bug-backlog/old.yaml",
            "title: Old bug\nstatus: open\ncreated: 2020-01-01\n",
        )
        _write(
            tmp_path / "state/bug-backlog/recent.yaml",
            "title: Recent bug\nstatus: open\ncreated: 2026-07-01\n",
        )
        _write(
            tmp_path / "state/debt-backlog/recent-closed.yaml",
            "title: Recent closed debt\nstatus: closed\ncreated: 2026-07-01\n",
        )
        results = query_unattached_all(tmp_path, since="2026-01-01", where="status=open")
        titles = {r["frontmatter"].get("title") for r in results}
        assert titles == {"Recent bug"}

    def test_sort_and_limit_apply_once_to_the_union(self, tmp_path: Path):
        """A per-type limit would truncate before the union is assembled —
        limit must cap the ASSEMBLED union, sorted, not each type's own
        result set."""
        for i in range(3):
            _write(
                tmp_path / f"state/bug-backlog/bug-{i}.yaml",
                f"title: Bug {i}\nstatus: open\npriority: {i}\n",
            )
        for i in range(3):
            _write(
                tmp_path / f"state/debt-backlog/debt-{i}.yaml",
                f"title: Debt {i}\nstatus: open\npriority: {10 + i}\n",
            )
        results = query_unattached_all(tmp_path, sort="-priority", limit=2)
        assert len(results) == 2
        # Highest priority values live in debt (10-12); a per-type limit=2
        # would instead have kept 2 bug + 2 debt = 4 records (or truncated
        # bug's own top-2 before debt was ever unioned in).
        assert [r["frontmatter"]["title"] for r in results] == ["Debt 2", "Debt 1"]

    def test_unknown_directory_for_one_type_is_skipped_not_fatal(self, tmp_path: Path):
        # No state/roadmap, state/handoffs, docs/plans, debt/improvement dirs at
        # all — every member type is legitimately absent, union is just empty.
        _write(
            tmp_path / "state/bug-backlog/2026-07-01-only.yaml",
            "title: Only bug\nstatus: open\n",
        )
        results = query_unattached_all(tmp_path)
        assert [r["frontmatter"]["title"] for r in results] == ["Only bug"]

    def test_empty_repo_yields_empty_union(self, tmp_path: Path):
        assert query_unattached_all(tmp_path) == []


# ---------------------------------------------------------------------------
# DR-115 legacy-prose-queue invisibility signal — ceremony-seam parity with
# coordinator_core.ops.records_query's records.query op. This seam's callers
# (coordinator_core/ops/emit/sections/backlogs.py, coordinator_core/roadmap/
# audit.py, coordinator_core/goals/reassess_krs.py,
# coordinator_core/reconcile/gate_eval.py) previously had no way to learn
# that a repo's improvement/bug queue depth answer was silently missing
# unmigrated legacy prose entries.
# ---------------------------------------------------------------------------


class TestLegacyProseSignal:
    def test_yaml_only_no_legacy_signal(self, tmp_path: Path):
        """Regression guard: a pure-YAML repo must not gain the signal."""
        _write(
            tmp_path / "state/improvement-queue/2026-07-22-example.yaml",
            "title: Example improvement\nstatus: open\n",
        )
        assert legacy_prose_signal("improvement", tmp_path) is None

    def test_legacy_only_signals_count_and_path(self, tmp_path: Path):
        _write(
            tmp_path / "state/improvement-queue.md",
            "# Improvement Queue\n"
            "- 2026-07-01 | idea one | notes\n"
            "- 2026-07-02 | idea two | notes\n"
            "- 2026-07-03 | idea three | notes\n",
        )
        signal = legacy_prose_signal("improvement", tmp_path)
        assert signal == {"count": 3, "path": "state/improvement-queue.md"}

    def test_both_present_neither_masks_the_other(self, tmp_path: Path):
        """A repo mid-migration (YAML dir populated AND legacy prose file
        populated) must still surface the legacy signal — the query_records()
        result for the YAML side and this signal are independent reads."""
        _write(
            tmp_path / "state/improvement-queue/2026-07-22-example.yaml",
            "title: Example improvement\nstatus: open\n",
        )
        _write(
            tmp_path / "state/improvement-queue.md",
            "- 2026-07-01 | idea one | notes\n"
            "- 2026-07-02 | idea two | notes\n",
        )
        results = query_records("improvement", tmp_path)
        assert len(results) == 1, "the YAML-side record must still be returned"
        signal = legacy_prose_signal("improvement", tmp_path)
        assert signal == {"count": 2, "path": "state/improvement-queue.md"}

    def test_bug_type_has_its_own_legacy_path(self, tmp_path: Path):
        _write(
            tmp_path / "state/bug-backlog.md",
            "- 2026-07-01 | some bug | notes\n",
        )
        signal = legacy_prose_signal("bug", tmp_path)
        assert signal == {"count": 1, "path": "state/bug-backlog.md"}

    def test_debt_type_has_its_own_legacy_path(self, tmp_path: Path):
        _write(
            tmp_path / "state/debt-backlog.md",
            "- 2026-07-01 | some debt | notes\n",
        )
        signal = legacy_prose_signal("debt", tmp_path)
        assert signal == {"count": 1, "path": "state/debt-backlog.md"}

    def test_type_without_legacy_path_never_signals(self, tmp_path: Path):
        """A type with no known legacy prose path (e.g. `lesson`) never signals,
        even when a same-shaped file happens to exist at an unrelated path.
        `debt` no longer qualifies as the neutral example now that it has a
        legacy prose path (see Finding 1, DR-115)."""
        _write(
            tmp_path / "state/lesson-backlog.md",
            "- 2026-07-01 | some lesson | notes\n",
        )
        assert legacy_prose_signal("lesson", tmp_path) is None


class TestLegacyProseSignalParity:
    """The ceremony seam and the JSON-RPC records.query op must agree on the
    legacy-prose signal for identical input — this is what keeps the two
    entry points from silently drifting apart again (see this module's own
    Negative-spec docstring)."""

    @pytest.mark.parametrize("record_type", ["improvement", "bug"])
    def test_agrees_with_ops_module_signal(self, tmp_path: Path, record_type: str):
        rel_path = {
            "improvement": "state/improvement-queue.md",
            "bug": "state/bug-backlog.md",
        }[record_type]
        _write(
            tmp_path / rel_path,
            "- 2026-07-01 | entry one | notes\n"
            "- 2026-07-02 | entry two | notes\n",
        )

        assert legacy_prose_signal(record_type, tmp_path) == _legacy_prose_signal(
            tmp_path, record_type,
        )

    @pytest.mark.parametrize("record_type", ["improvement", "bug", "debt"])
    def test_agrees_with_ops_module_signal_when_absent(self, tmp_path: Path, record_type: str):
        assert legacy_prose_signal(record_type, tmp_path) == _legacy_prose_signal(
            tmp_path, record_type,
        )
