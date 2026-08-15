"""Port of: emit-cockpit-spine-graceful-absent.sh (DoE f703efad, 2026-07-21)
(AC4 realization) — pytest edition.

Asserts that when the spine source files that CAN be made absent for this test
(session-hierarchy.*.json files) are absent, ``emit(ctx, out=<tmp>)`` still:

  (a) succeeds (no exception — exit 0 equivalent).
  (b) emits ``session_hierarchies`` as ``[]``.
  (c) emits ``malformed_records.session_hierarchies`` as ``[]``.
  (d) emits ``file_attributions`` as a valid JSON array of any length (derived from
      transcripts independently; may be non-empty even when hierarchy source is absent).

Note on completion_rollups: computed dynamically from handoff YAML frontmatter
records via query-completions.sh — NOT from state/session-journal/ files. The
graceful-absent test cannot force it to [] by hiding files; it only asserts that
completion_rollups is a valid object with array-typed day/week members.

Spec backlink: docs/plans/2026-06-30-ccos-8-cockpit-read-contract-spine-entities.md § AC4
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.envelope import emit

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# Minimal EmitContext factory
# ---------------------------------------------------------------------------

def _make_ctx(central_state_root: Path, repo_root: Path) -> EmitContext:
    """Return a minimal EmitContext with the given state roots.

    Volatile fields are non-load-bearing for graceful-absent tests; only the path
    fields determine which source dirs the porters look at.
    """
    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=central_state_root,
        git_branch="test-branch",
        git_sha="0000000000000000000000000000000000000000",
        git_sha_short="00000000",
        observed_at="2026-07-04T00:00:00Z",
        hostname="test-host",
        repo_name="test/repo",
    )


# ---------------------------------------------------------------------------
# AC4 — graceful absent: sparse / empty source dirs still emit successfully
# ---------------------------------------------------------------------------

class TestGracefulAbsentEmission:
    """Verify that absent session-hierarchy source files do not break emission."""

    def test_emit_succeeds_with_empty_state_dirs(self, tmp_path: Path) -> None:
        """emit() with an empty central_state_root must not raise any exception."""
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)
        result = emit(ctx, out=tmp_path / "output.json")
        assert result["ok"] is True

    def test_output_file_is_created_and_non_empty(self, tmp_path: Path) -> None:
        """The output file must exist and have content when source dirs are absent."""
        out = tmp_path / "output.json"
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)

        emit(ctx, out=out)

        assert out.exists(), "output file must be created even when source dirs are absent"
        assert out.stat().st_size > 0, "output file must be non-empty"

    def test_output_is_valid_json(self, tmp_path: Path) -> None:
        """The emitted output must parse as valid JSON."""
        out = tmp_path / "output.json"
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)

        emit(ctx, out=out)

        data = json.loads(out.read_text())
        assert isinstance(data, dict)

    def test_session_hierarchies_is_empty_array(self, tmp_path: Path) -> None:
        """With no session-hierarchy source files, session_hierarchies must be []."""
        out = tmp_path / "output.json"
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)

        emit(ctx, out=out)

        data = json.loads(out.read_text())
        assert isinstance(data["session_hierarchies"], list), (
            "session_hierarchies must be an array"
        )
        assert data["session_hierarchies"] == [], (
            f"session_hierarchies must be [] when source is absent; "
            f"got {len(data['session_hierarchies'])} elements"
        )

    def test_malformed_session_hierarchies_is_empty_array(self, tmp_path: Path) -> None:
        """malformed_records.session_hierarchies must be [] when no sources are present."""
        out = tmp_path / "output.json"
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)

        emit(ctx, out=out)

        data = json.loads(out.read_text())
        assert isinstance(data["malformed_records"]["session_hierarchies"], list), (
            "malformed_records.session_hierarchies must be an array"
        )
        assert data["malformed_records"]["session_hierarchies"] == [], (
            "malformed_records.session_hierarchies must be [] when no source files are present"
        )

    def test_file_attributions_is_valid_array(self, tmp_path: Path) -> None:
        """file_attributions must be a valid JSON array (any length — derived from transcripts)."""
        out = tmp_path / "output.json"
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)

        emit(ctx, out=out)

        data = json.loads(out.read_text())
        assert isinstance(data["file_attributions"], list), (
            "file_attributions must be an array (any length — derived from transcripts)"
        )

    def test_malformed_file_attributions_is_array(self, tmp_path: Path) -> None:
        """malformed_records.file_attributions must be a valid array."""
        out = tmp_path / "output.json"
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)

        emit(ctx, out=out)

        data = json.loads(out.read_text())
        assert isinstance(data["malformed_records"]["file_attributions"], list), (
            "malformed_records.file_attributions must be an array"
        )

    def test_completion_rollups_is_object_with_array_members(self, tmp_path: Path) -> None:
        """completion_rollups must be a dict with array-typed .day and .week members.

        The rollups are computed from handoff YAML records (not from state/session-journal/
        files), so they cannot be forced to [] by hiding directories; we only assert structure.
        """
        out = tmp_path / "output.json"
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)

        emit(ctx, out=out)

        data = json.loads(out.read_text())
        rollups = data["completion_rollups"]
        assert isinstance(rollups, dict), "completion_rollups must be an object"
        assert isinstance(rollups["day"], list), "completion_rollups.day must be an array"
        assert isinstance(rollups["week"], list), "completion_rollups.week must be an array"

    def test_schema_version_present(self, tmp_path: Path) -> None:
        """The emitted envelope must carry a non-empty schema_version even with absent sources."""
        out = tmp_path / "output.json"
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)

        emit(ctx, out=out)

        data = json.loads(out.read_text())
        assert data.get("schema_version"), "schema_version must be present and non-empty"

    def test_all_malformed_buckets_are_arrays(self, tmp_path: Path) -> None:
        """Every key in malformed_records must be an array (structure invariant)."""
        out = tmp_path / "output.json"
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)

        emit(ctx, out=out)

        data = json.loads(out.read_text())
        malformed = data["malformed_records"]
        assert isinstance(malformed, dict), "malformed_records must be an object"
        for key, val in malformed.items():
            assert isinstance(val, list), (
                f"malformed_records.{key} must be an array; got {type(val).__name__}"
            )
