"""Unit tests for the no-OVERVIEW malformed bucket (Item 58, P143-T58).

``collect()`` previously dropped a ``state/roadmap/<slug>/`` directory lacking
``OVERVIEW.md`` entirely: ``_query_roadmap_records``'s glob (``state/roadmap/**/OVERVIEW.md``)
only matches directories that HAVE the file, so a no-OVERVIEW directory never appeared in
``raw`` and therefore never appeared in either ``records`` or ``malformed``. These tests
exercise ``_no_overview_malformed`` and its wiring into ``collect()`` directly — no
subprocess, ``_query_roadmap_records`` is patched to control the raw-record set while
real ``tmp_path`` directories stand in for ``state/roadmap/*``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections.roadmaps import collect


def _make_ctx(worktree_root: Path) -> EmitContext:
    return EmitContext(
        repo_root=worktree_root,
        coordinator_root=worktree_root,
        central_state_root=worktree_root / "state",
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-07-06T00:00:00Z",
        hostname="test-host",
        repo_name="test-repo",
    )


def _valid_record(slug: str) -> dict:
    return {
        "frontmatter": {
            "title": "Test Roadmap",
            "created": "2026-07-01T00:00:00Z",
            "status": "active",
        },
        "path": f"state/roadmap/{slug}/OVERVIEW.md",
    }


class TestNoOverviewBucket:
    def test_directory_without_overview_is_quarantined(self, tmp_path: Path) -> None:
        """A roadmap directory with no OVERVIEW.md surfaces in malformed, not silently dropped."""
        (tmp_path / "state" / "roadmap" / "no-overview-here").mkdir(parents=True)

        ctx = _make_ctx(tmp_path)
        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=[],
        ):
            records, malformed = collect(ctx)

        assert records == []
        assert len(malformed) == 1, f"Expected 1 malformed entry, got {malformed}"
        entry = malformed[0]
        assert entry["path"] == "state/roadmap/no-overview-here/OVERVIEW.md"
        assert "OVERVIEW" in entry["reason"]

    def test_directory_with_overview_not_double_counted(self, tmp_path: Path) -> None:
        """A directory whose OVERVIEW.md the query seam already returned is not re-flagged."""
        roadmap_dir = tmp_path / "state" / "roadmap" / "has-overview"
        roadmap_dir.mkdir(parents=True)
        (roadmap_dir / "OVERVIEW.md").write_text("---\ntitle: x\n---\n", encoding="utf-8")

        ctx = _make_ctx(tmp_path)
        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=[_valid_record("has-overview")],
        ):
            records, malformed = collect(ctx)

        assert len(records) == 1
        assert malformed == [], f"Unexpected malformed: {malformed}"

    def test_mixed_directories(self, tmp_path: Path) -> None:
        """A mix of a valid roadmap and a no-OVERVIEW directory routes each correctly."""
        good = tmp_path / "state" / "roadmap" / "good-roadmap"
        good.mkdir(parents=True)
        (good / "OVERVIEW.md").write_text("---\ntitle: x\n---\n", encoding="utf-8")
        (tmp_path / "state" / "roadmap" / "bad-roadmap").mkdir(parents=True)

        ctx = _make_ctx(tmp_path)
        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=[_valid_record("good-roadmap")],
        ):
            records, malformed = collect(ctx)

        assert len(records) == 1
        assert records[0]["path"] == "state/roadmap/good-roadmap/OVERVIEW.md"
        assert len(malformed) == 1
        assert malformed[0]["path"] == "state/roadmap/bad-roadmap/OVERVIEW.md"

    def test_no_roadmap_dir_yields_empty_malformed(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=[],
        ):
            records, malformed = collect(ctx)

        assert records == []
        assert malformed == []

    def test_non_directory_entries_under_roadmap_root_are_ignored(self, tmp_path: Path) -> None:
        roadmap_root = tmp_path / "state" / "roadmap"
        roadmap_root.mkdir(parents=True)
        (roadmap_root / "README.md").write_text("stray file", encoding="utf-8")

        ctx = _make_ctx(tmp_path)
        with patch(
            "coordinator_core.ops.emit.sections.roadmaps._query_roadmap_records",
            return_value=[],
        ):
            records, malformed = collect(ctx)

        assert records == []
        assert malformed == []

    def test_this_repos_dogfood_roadmap_now_has_overview(self) -> None:
        """Regression pin for the specific directory this item's body names: authoring
        state/roadmap/dogfood-2026-05-08/OVERVIEW.md means it no longer falls into the
        no-OVERVIEW bucket for this real repo tree."""
        repo_root = Path(__file__).resolve().parents[4]
        overview = repo_root / "state" / "roadmap" / "dogfood-2026-05-08" / "OVERVIEW.md"
        assert overview.is_file(), f"expected {overview} to exist"
