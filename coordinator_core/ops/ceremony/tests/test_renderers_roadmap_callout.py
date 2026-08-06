"""
coordinator_core.ops.ceremony.tests.test_renderers_roadmap_callout — unit +
golden-fixture tests for C8c's ``refresh_roadmap_callout`` (pure-Python port of
``refresh-queries.js``'s BEGIN/END query-block regen).

Golden-fixture provenance (plan-mandated, load-bearing for AC12's "full-fidelity"
claim): the expected outputs below were captured by running the REAL
``refresh-queries.js`` (example-doctrine-repo ``coordinator/bin/refresh-queries.js``,
node v24.18.0) against the exact fixture tree built in ``_build_fixture_repo``,
BEFORE the node source is removed from the fleet. Capture transcript:

    node .../coordinator/bin/refresh-queries.js \\
        --files state/roadmap/testroadmap/STUB-INDEX.md --root <fixture-root>
    # => "[updated] state/roadmap/testroadmap/STUB-INDEX.md (1 callout(s))"
    # second run => "All query callouts are up to date." (idempotency proof)

    node .../coordinator/bin/refresh-queries.js \\
        --files state/roadmap/emptyroadmap/STUB-INDEX.md --root <fixture-root>
    # => "All query callouts are up to date." (empty result set — no matching
    #    handoffs for roadmap_id=emptyroadmap — renders as a no-op, matching
    #    formatRecords([]) === '' and replaceBlock leaving the block empty).

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C8c
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from coordinator_core.ops.ceremony.renderers import refresh_roadmap_callout

_GOLDEN_TESTROADMAP = dedent(
    """\
    # STUB-INDEX — testroadmap

    <!-- BEGIN query: handoff where=roadmap_id=testroadmap sort=sprint -->
    - [Beta work](../../handoffs/2026-07-02-beta.md) — ready_to_fire
    - [Gamma work](../../handoffs/2026-07-03-gamma.md) — shipped
    - [Alpha work](../../handoffs/2026-07-01-alpha.md) — in_flight
    <!-- END query -->
    """
)

_GOLDEN_EMPTYROADMAP = dedent(
    """\
    # STUB-INDEX — emptyroadmap

    <!-- BEGIN query: handoff where=roadmap_id=emptyroadmap sort=sprint -->
    <!-- END query -->
    """
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")
    return path


def _build_fixture_repo(root: Path) -> None:
    _write(
        root / "state/handoffs/2026-07-01-alpha.md",
        """\
        ---
        title: Alpha work
        status: open
        deployment_state: in_flight
        roadmap_id: testroadmap
        sprint: "3"
        created: 2026-07-01
        ---
        Body alpha.
        """,
    )
    _write(
        root / "state/handoffs/2026-07-02-beta.md",
        """\
        ---
        title: Beta work
        status: open
        deployment_state: ready_to_fire
        roadmap_id: testroadmap
        sprint: "1"
        created: 2026-07-02
        ---
        Body beta.
        """,
    )
    _write(
        root / "state/handoffs/2026-07-03-gamma.md",
        """\
        ---
        title: Gamma work
        status: claimed
        claimed_at: 2026-07-03
        claimed_by: gamma-consumer
        deployment_state: shipped
        roadmap_id: testroadmap
        sprint: "2"
        created: 2026-07-03
        ---
        Body gamma.
        """,
    )
    _write(
        root / "state/handoffs/2026-07-04-other.md",
        """\
        ---
        title: Other roadmap
        status: open
        deployment_state: in_flight
        roadmap_id: notthisone
        sprint: "1"
        created: 2026-07-04
        ---
        Body other.
        """,
    )
    _write(
        root / "state/roadmap/testroadmap/STUB-INDEX.md",
        """\
        # STUB-INDEX — testroadmap

        <!-- BEGIN query: handoff where=roadmap_id=testroadmap sort=sprint -->
        <!-- END query -->
        """,
    )
    _write(
        root / "state/roadmap/emptyroadmap/STUB-INDEX.md",
        """\
        # STUB-INDEX — emptyroadmap

        <!-- BEGIN query: handoff where=roadmap_id=emptyroadmap sort=sprint -->
        <!-- END query -->
        """,
    )


class TestGoldenFixtureParity:
    def test_matches_real_node_output(self, tmp_path: Path):
        _build_fixture_repo(tmp_path)
        result = refresh_roadmap_callout(tmp_path, "testroadmap")

        stub_index = tmp_path / "state/roadmap/testroadmap/STUB-INDEX.md"
        assert stub_index.read_text(encoding="utf-8") == _GOLDEN_TESTROADMAP
        assert result["acted"] == ["renderers:refresh_roadmap_callout:state/roadmap/testroadmap/STUB-INDEX.md"]
        assert result["skipped"] == []
        assert result["failed"] == []

    def test_idempotent_second_run_is_clean_skip(self, tmp_path: Path):
        _build_fixture_repo(tmp_path)
        refresh_roadmap_callout(tmp_path, "testroadmap")
        result = refresh_roadmap_callout(tmp_path, "testroadmap")

        stub_index = tmp_path / "state/roadmap/testroadmap/STUB-INDEX.md"
        assert stub_index.read_text(encoding="utf-8") == _GOLDEN_TESTROADMAP
        assert result["acted"] == []
        assert result["skipped"] == ["renderers:refresh_roadmap_callout:up-to-date"]
        assert result["failed"] == []

    def test_empty_result_set_renders_as_noop(self, tmp_path: Path):
        _build_fixture_repo(tmp_path)
        result = refresh_roadmap_callout(tmp_path, "emptyroadmap")

        stub_index = tmp_path / "state/roadmap/emptyroadmap/STUB-INDEX.md"
        assert stub_index.read_text(encoding="utf-8") == _GOLDEN_EMPTYROADMAP
        assert result["acted"] == []
        assert result["skipped"] == ["renderers:refresh_roadmap_callout:up-to-date"]
        assert result["failed"] == []


class TestSkipAndGuardBehavior:
    def test_no_roadmap_id_skips_cleanly(self, tmp_path: Path):
        _build_fixture_repo(tmp_path)
        result = refresh_roadmap_callout(tmp_path, "")
        assert result == {"acted": [], "skipped": ["renderers:refresh_roadmap_callout:no-roadmap-id"], "failed": []}

    def test_path_traversal_roadmap_id_rejected(self, tmp_path: Path):
        _build_fixture_repo(tmp_path)
        result = refresh_roadmap_callout(tmp_path, "../../etc")
        assert result["acted"] == []
        assert result["failed"] == []
        assert result["skipped"] == ["renderers:refresh_roadmap_callout:no-roadmap-id"]

    def test_shell_metachar_roadmap_id_rejected(self, tmp_path: Path):
        _build_fixture_repo(tmp_path)
        result = refresh_roadmap_callout(tmp_path, "x; rm -rf /")
        assert result["skipped"] == ["renderers:refresh_roadmap_callout:no-roadmap-id"]

    def test_quoted_roadmap_id_is_stripped(self, tmp_path: Path):
        _build_fixture_repo(tmp_path)
        result = refresh_roadmap_callout(tmp_path, '"testroadmap"')
        assert result["acted"] == ["renderers:refresh_roadmap_callout:state/roadmap/testroadmap/STUB-INDEX.md"]

    def test_missing_stub_index_skips_cleanly(self, tmp_path: Path):
        _build_fixture_repo(tmp_path)
        result = refresh_roadmap_callout(tmp_path, "no-such-roadmap")
        assert result == {
            "acted": [], "skipped": ["renderers:refresh_roadmap_callout:stub-index-not-found"], "failed": [],
        }

    def test_stub_index_without_callout_skips_cleanly(self, tmp_path: Path):
        _write(
            tmp_path / "state/roadmap/nocallout/STUB-INDEX.md",
            "# STUB-INDEX — nocallout\n\nNo query callout here.\n",
        )
        result = refresh_roadmap_callout(tmp_path, "nocallout")
        assert result == {
            "acted": [], "skipped": ["renderers:refresh_roadmap_callout:no-query-callout"], "failed": [],
        }
