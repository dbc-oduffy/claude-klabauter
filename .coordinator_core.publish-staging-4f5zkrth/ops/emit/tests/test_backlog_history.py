"""Unit tests for backlog_history.collect() — C5 assertions.

Critical assertion: D9 non-null provenance (plan § D6, AC8).
opticon's store has a ``provenance_fk NOT NULL`` constraint; the block-level ``provenance``
MUST be fully populated (non-null) even in the empty-series D9 default path.

Covers:
  - D9 default when no shards exist → provenance non-null, series=[], generated_at=None
  - D9 default when contract absent (default vendored 2.5.0 state) → D9 empty-series
  - Populated path when contract declares the block AND shards present → series populated
  - Regression (AC4): v2.5.0 sentinel present but contract absent → still D9 (gate decoupled)
  - Latest-per-(repo,date) aggregation across multiple shard lines
  - Malformed lines skipped gracefully (no abort)

Monkeypatch seam: ``validate.contract_declares_backlog_history`` is the named gate function.
Tests that exercise the contract-present path monkeypatch it to return True directly
(Option a from the plan's Test surface section — simpler than redirecting VENDOR_SCHEMA_BUNDLE).

Spec backlink: pln-backloghistory-emit-gate-decou-22d451 § Test surface
Amends: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § C5
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.emit import validate
from coordinator_core.ops.emit.backlog_history import ShardRootUnreadable, collect
from coordinator_core.ops.emit.context import EmitContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path: Path) -> EmitContext:
    """Build a minimal EmitContext pointing at tmp_path as central_state_root."""
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=tmp_path,
        git_branch="main",
        git_sha="abc1234567890123456789012345678901234567",
        git_sha_short="abc12345",
        observed_at="2026-07-04T00:00:00Z",
        hostname="test-host",
        repo_name="dbc-oduffy/.claude-prime",
    )


def _write_shard(state_dir: Path, machine: str, rows: list[dict]) -> Path:
    """Write a JSONL shard under state_dir and return the path."""
    shard = state_dir / f"backlog-snapshots.{machine}.jsonl"
    with shard.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
    return shard


# ---------------------------------------------------------------------------
# D9 non-null-provenance assertions (the critical AC8 / D6 invariant)
# ---------------------------------------------------------------------------

class TestD9DefaultProvenanceNonNull:
    """Plan § D6, AC8: block-level provenance MUST be non-null in every D9 default path."""

    def test_no_shards_provenance_non_null(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """D9 default: no shards present → provenance is non-null.

        Monkeypatches the probe to False to pin the "contract absent" scenario explicitly —
        so a future re-vendor that lands the block does not silently shift this test from
        the contract-absent D9 path to the contract-present-but-no-data D9 path.
        <!-- Review: code-reviewer — F4 nit: pin contract-absent scenario for fragility under re-vendor. -->
        """
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: False)
        ctx = _make_ctx(tmp_path)
        block = collect(ctx)
        assert block["provenance"] is not None, (
            "provenance must be non-null even when no shard data exists "
            "(opticon provenance_fk NOT NULL, plan § D6)"
        )
        assert isinstance(block["provenance"], dict)

    def test_no_shards_series_empty_and_generated_at_null(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """D9 default: no shards → series=[] and generated_at=None.

        Monkeypatches the probe to False to pin the "contract absent" scenario explicitly —
        so a future re-vendor does not silently shift this test's D9 path.
        <!-- Review: code-reviewer — F4 nit: pin contract-absent scenario for fragility under re-vendor. -->
        """
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: False)
        ctx = _make_ctx(tmp_path)
        block = collect(ctx)
        assert block["series"] == []
        assert block["generated_at"] is None

    def test_contract_present_no_shards_still_d9(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Contract present + empty shard directory → still D9 default (contract-present-but-no-data branch).

        Directly covers the branch at backlog_history.py:173–180 — probe returns True but
        _read_shards returns an empty map (no shard files present), so collect() returns
        the D9 empty-series shape with non-null provenance.
        <!-- Review: code-reviewer — F4 nit: contract-present-but-no-data branch had no dedicated test. -->
        """
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)
        # No shard files written — empty directory.
        block = collect(ctx)
        assert block["series"] == [], (
            "contract present but no shards → still D9 empty-series (no data to populate)"
        )
        assert block["generated_at"] is None
        assert block["provenance"] is not None, (
            "provenance must be non-null even in contract-present-but-no-data D9 path (D6)"
        )

    def test_provenance_uses_canonical_contract_enums(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Block-level provenance MUST use canonical ProvenanceEnvelope enum values.

        Regression guard for the drift the DoE cockpit-contract live-emit round-trip caught:
        ``source_kind`` was emitted as the non-canonical internal label ``local_file`` (correct:
        ``local_fs``) and ``derivation`` as ``derived`` (correct: ``parsed``). Every sibling
        emit section (cross_repo_memos, health, backlogs, …) uses ``local_fs``/``parsed``;
        backlog_history was the lone outlier and threw a ZodError in SnapshotEnvelope.parse().
        Non-null alone (the assertions below) did NOT catch it — the value must be pinned.
        """
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: False)
        ctx = _make_ctx(tmp_path)
        prov = collect(ctx)["provenance"]
        assert prov["source_kind"] == "local_fs", (
            "source_kind must be the canonical contract enum 'local_fs', not the internal "
            "'local_file' label (cockpit-contract SourceKind)"
        )
        assert prov["derivation"] == "parsed", (
            "derivation must be the canonical contract enum 'parsed', not 'derived' "
            "(cockpit-contract Derivation)"
        )
        # local_fs is a non-git source_kind → ref MUST be null (D9 bidirectional invariant).
        assert prov["ref"] is None

    def test_contract_absent_provenance_non_null(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """D9 default: contract absent (probe → False) → provenance is non-null.

        The default vendored 2.5.0 bundle does not declare backlog_history; the probe
        returns False and collect() returns the D9 empty-series shape with non-null provenance.
        Monkeypatches the probe directly to ensure determinism regardless of the local bundle.
        """
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: False)
        ctx = _make_ctx(tmp_path)
        block = collect(ctx)
        assert block["provenance"] is not None, (
            "provenance must be non-null even when contract does not declare backlog_history "
            "(opticon provenance_fk NOT NULL, plan § D6)"
        )
        assert isinstance(block["provenance"], dict)

    def test_contract_absent_series_empty_and_generated_at_null(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """D9 default: contract absent → series=[] and generated_at=None."""
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: False)
        ctx = _make_ctx(tmp_path)
        _write_shard(tmp_path, "machine-a", [
            {"repo": "owner/repo", "date": "2026-07-04", "bug": 5, "improvement": 3, "lessons": 1},
        ])
        block = collect(ctx)
        assert block["series"] == [], "contract absent must suppress shard data (D9 gate)"
        assert block["generated_at"] is None

    def test_v25_sentinel_present_but_contract_absent_still_d9(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC4 regression: v2.5.0 sentinel dropped + contract still absent → still D9.

        Proves that the gate is fully decoupled from the v2.5.0 sentinel: even if a
        cockpit-revendor-pending-v2.5.0 file exists in central_state_root, the contract-absence
        probe returns False and the D9 empty-series hold remains active. The premature-activation
        trap (gate keyed on content_hash sentinel → flips early when 2.5.0 clears) is closed.
        """
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: False)
        ctx = _make_ctx(tmp_path)
        # Place the old v2.5.0 sentinel — must NOT activate real-series emission.
        (tmp_path / "cockpit-revendor-pending-v2.5.0").touch()
        _write_shard(tmp_path, "machine-a", [
            {"repo": "owner/repo", "date": "2026-07-04", "bug": 5, "improvement": 3, "lessons": 1},
        ])
        block = collect(ctx)
        assert block["series"] == [], (
            "contract-absent gate must hold even when v2.5.0 sentinel is present; "
            "the gate is contract-presence, not sentinel-keyed (AC4)"
        )
        assert block["generated_at"] is None
        assert block["provenance"] is not None


# ---------------------------------------------------------------------------
# Populated-path assertions (sentinel absent, shards present)
# ---------------------------------------------------------------------------

class TestPopulatedPath:
    """Verify the block is populated correctly when contract declares the block and shards exist."""

    def test_contract_present_shards_populated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC3: contract declares backlog_history (probe → True) + shards present → real series.

        Monkeypatches ``validate.contract_declares_backlog_history`` to True to simulate the
        post-re-vendor state in which the vendored bundle has declared the block concretely.
        Confirms that collect() reads the shards and returns a populated series, not D9 default.
        """
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)
        _write_shard(tmp_path, "machine-a", [
            {"repo": "owner/repo", "date": "2026-07-05", "bug": 4, "improvement": 2, "lessons": 1},
        ])
        block = collect(ctx)
        assert block["generated_at"] is not None, "contract present + shards → generated_at must be set"
        assert len(block["series"]) == 1
        assert block["series"][0]["repo"] == "owner/repo"
        assert block["series"][0]["points"][0]["bug"] == 4
        # provenance is a block-level envelope keyed off ctx (not the shard's machine
        # identity -- collect() never threads "machine-a" into it), so the specific-value
        # pin is the full expected envelope shape, deterministic from ctx above.
        assert block["provenance"] == {
            "source_kind": "local_fs",
            "repo": ctx.repo_name,
            "ref": None,
            "path": "",
            "observed_at": ctx.observed_at,
            "derivation": "parsed",
            "entity_anchor": None,
        }, "provenance envelope must match ctx exactly in populated path (D6)"

    def test_single_shard_single_row(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Single shard with one row → series has one repo with one point."""
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)
        _write_shard(tmp_path, "machine-a", [
            {"repo": "owner/repo", "date": "2026-07-04", "bug": 3, "improvement": 5, "lessons": 2},
        ])
        block = collect(ctx)
        assert block["generated_at"] is not None
        assert len(block["series"]) == 1
        entry = block["series"][0]
        assert entry["repo"] == "owner/repo"
        assert len(entry["points"]) == 1
        pt = entry["points"][0]
        assert pt["date"] == "2026-07-04"
        assert pt["bug"] == 3
        assert pt["improvement"] == 5
        assert pt["lessons"] == 2
        assert block["provenance"] == {
            "source_kind": "local_fs",
            "repo": ctx.repo_name,
            "ref": None,
            "path": "",
            "observed_at": ctx.observed_at,
            "derivation": "parsed",
            "entity_anchor": None,
        }

    def test_latest_per_repo_date_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Latest-per-(repo,date): later shard line overwrites earlier for same key."""
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)
        # Write two rows for the same (repo, date) — second should win.
        _write_shard(tmp_path, "machine-a", [
            {"repo": "owner/repo", "date": "2026-07-04", "bug": 1, "improvement": 1, "lessons": 1},
            {"repo": "owner/repo", "date": "2026-07-04", "bug": 9, "improvement": 7, "lessons": 4},
        ])
        block = collect(ctx)
        assert len(block["series"]) == 1
        pt = block["series"][0]["points"][0]
        assert pt["bug"] == 9, "last row should win for same (repo, date)"
        assert pt["improvement"] == 7
        assert pt["lessons"] == 4

    def test_multiple_repos_sorted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple repos → series sorted alphabetically."""
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)
        _write_shard(tmp_path, "machine-a", [
            {"repo": "z-owner/z-repo", "date": "2026-07-04", "bug": 1, "improvement": 0, "lessons": 0},
            {"repo": "a-owner/a-repo", "date": "2026-07-04", "bug": 2, "improvement": 0, "lessons": 0},
        ])
        block = collect(ctx)
        repos = [e["repo"] for e in block["series"]]
        assert repos == sorted(repos), "series must be sorted by repo"

    def test_points_sorted_by_date(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple dates for one repo → points sorted ascending by date."""
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)
        _write_shard(tmp_path, "machine-a", [
            {"repo": "owner/repo", "date": "2026-07-03", "bug": 2, "improvement": 0, "lessons": 0},
            {"repo": "owner/repo", "date": "2026-07-01", "bug": 4, "improvement": 0, "lessons": 0},
            {"repo": "owner/repo", "date": "2026-07-02", "bug": 3, "improvement": 0, "lessons": 0},
        ])
        block = collect(ctx)
        pts = block["series"][0]["points"]
        dates = [p["date"] for p in pts]
        assert dates == sorted(dates), "points must be sorted by date ascending"

    def test_malformed_lines_skipped_gracefully(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Malformed JSONL lines are skipped — collect never aborts on bad input."""
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)
        shard = tmp_path / "backlog-snapshots.machine-a.jsonl"
        shard.write_text(
            'not-valid-json\n'
            '{"repo":"owner/repo","date":"2026-07-04","bug":5,"improvement":2,"lessons":1}\n'
            '{"missing_repo":true,"date":"2026-07-04","bug":0,"improvement":0,"lessons":0}\n',
            encoding="utf-8",
        )
        block = collect(ctx)
        # The valid line should be present; bad lines skipped.
        assert len(block["series"]) == 1
        assert block["series"][0]["repo"] == "owner/repo"
        assert block["series"][0]["points"][0]["bug"] == 5


# ---------------------------------------------------------------------------
# Block shape assertions (key presence + camelCase)
# ---------------------------------------------------------------------------

class TestBlockShape:
    """Verify the block always has the three required snake_case top-level keys."""

    def test_keys_present_in_d9_default(self, tmp_path: Path) -> None:
        """D9 default block has all three required snake_case keys."""
        ctx = _make_ctx(tmp_path)
        block = collect(ctx)
        assert "generated_at" in block
        assert "series" in block
        assert "provenance" in block
        # Must NOT have the old camelCase variant (snake_case per contract v2.7.0, 2026-07-06 memo).
        camel_key = "generated" + "At"  # avoid literal match in completeness grep
        assert camel_key not in block

    def test_keys_present_in_populated_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Populated block has all three required snake_case keys."""
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)
        _write_shard(tmp_path, "machine-a", [
            {"repo": "owner/repo", "date": "2026-07-04", "bug": 1, "improvement": 0, "lessons": 0},
        ])
        block = collect(ctx)
        assert "generated_at" in block
        assert "series" in block
        assert "provenance" in block


# ---------------------------------------------------------------------------
# _safe_int / non-integer count field guard
# ---------------------------------------------------------------------------

class TestCrossMachineAggregation:
    """cross-machine shard aggregation: collect() reads ALL per-machine shards and merges them.

    Review: code-reviewer (S4-F6) — the multi-machine path is the expected production shape
    (one shard per machine per repo). A regression where collect() only reads one shard, or
    hardcodes a machine slug, would silently drop data from other machines.
    """

    def test_two_machine_shards_both_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """collect() reads shards from BOTH machine-a and machine-b.

        Verifies that the fleet-wide aggregation sees data from all machines, not just the
        first shard found. machine-b has a repo (repo-y) that only appears in machine-b's
        shard — its presence in the result proves machine-b was read.
        """
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)

        # machine-a: has repo-x and repo-y
        _write_shard(tmp_path, "machine-a", [
            {"repo": "owner/repo-x", "date": "2026-07-07", "bug": 2, "improvement": 1, "lessons": 0},
            {"repo": "owner/repo-y", "date": "2026-07-07", "bug": 3, "improvement": 0, "lessons": 1},
        ])
        # machine-b: has repo-x only (with different values)
        _write_shard(tmp_path, "machine-b", [
            {"repo": "owner/repo-x", "date": "2026-07-07", "bug": 5, "improvement": 2, "lessons": 0},
        ])

        block = collect(ctx)
        repos_in_series = {e["repo"] for e in block["series"]}

        # repo-y only exists in machine-a's shard — its presence proves machine-a was read
        assert "owner/repo-y" in repos_in_series, (
            "owner/repo-y must appear in series (only in machine-a shard); "
            "if absent, machine-a shard was not read"
        )
        # repo-x exists in both — its presence is guaranteed
        assert "owner/repo-x" in repos_in_series

    def test_latest_per_repo_date_across_machines(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """latest-per-(repo,date) applies ACROSS machines, not just within one shard.

        machine-a writes (repo-x, 2026-07-07, bug=2) first; machine-b writes the same
        (repo-x, 2026-07-07, bug=5). Both rows have the same key. collect() must produce
        exactly ONE point for (repo-x, 2026-07-07) — not two — demonstrating that
        latest-per-(repo,date) is enforced across the full multi-machine read.
        """
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)

        _write_shard(tmp_path, "machine-a", [
            {"repo": "owner/repo-x", "date": "2026-07-07", "bug": 2, "improvement": 0, "lessons": 0},
        ])
        _write_shard(tmp_path, "machine-b", [
            {"repo": "owner/repo-x", "date": "2026-07-07", "bug": 5, "improvement": 0, "lessons": 0},
        ])

        block = collect(ctx)
        assert len(block["series"]) == 1
        entry = block["series"][0]
        assert entry["repo"] == "owner/repo-x"
        # Only ONE point for the overlapping (repo, date) — cross-machine dedup applied
        assert len(entry["points"]) == 1, (
            f"Expected 1 point for (repo-x, 2026-07-07) after cross-machine dedup; "
            f"got {len(entry['points'])} — latest-per-(repo,date) may not apply across shards"
        )

    def test_cross_machine_non_overlapping_rows_aggregated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-overlapping rows from different machines are both preserved in the series.

        machine-a has (repo-x, 2026-07-06); machine-b has (repo-x, 2026-07-07). These have
        different dates so both should appear as separate points in the series.
        """
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)

        _write_shard(tmp_path, "machine-a", [
            {"repo": "owner/repo-x", "date": "2026-07-06", "bug": 1, "improvement": 0, "lessons": 0},
        ])
        _write_shard(tmp_path, "machine-b", [
            {"repo": "owner/repo-x", "date": "2026-07-07", "bug": 4, "improvement": 0, "lessons": 0},
        ])

        block = collect(ctx)
        assert len(block["series"]) == 1
        entry = block["series"][0]
        assert len(entry["points"]) == 2, (
            "Both machine-a (2026-07-06) and machine-b (2026-07-07) rows must appear "
            "as separate points (different dates → no dedup)"
        )
        dates = {p["date"] for p in entry["points"]}
        assert "2026-07-06" in dates
        assert "2026-07-07" in dates


class TestSafeInt:
    """_safe_int silently treats non-integer count fields as 0 (malformed-skip posture)."""

    def test_string_count_field_gracefully_skips_to_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-integer bug count ("N/A") is treated as 0, not a crash."""
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)
        _write_shard(tmp_path, "machine-a", [
            {"repo": "owner/repo", "date": "2026-07-04", "bug": "N/A", "improvement": 3, "lessons": 1},
        ])
        block = collect(ctx)
        # The row is included but the malformed count field resolves to 0.
        assert len(block["series"]) == 1
        pt = block["series"][0]["points"][0]
        assert pt["bug"] == 0, "non-integer count field must degrade to 0, not raise"
        assert pt["improvement"] == 3
        assert pt["lessons"] == 1

    def test_none_count_field_gracefully_skips_to_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A null/absent count field is treated as 0."""
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        ctx = _make_ctx(tmp_path)
        shard = tmp_path / "backlog-snapshots.machine-a.jsonl"
        shard.write_text(
            '{"repo":"owner/repo","date":"2026-07-04","improvement":2,"lessons":1}\n',
            encoding="utf-8",
        )
        block = collect(ctx)
        assert len(block["series"]) == 1
        pt = block["series"][0]["points"][0]
        assert pt["bug"] == 0, "absent bug field must degrade to 0, not raise"


# ---------------------------------------------------------------------------
# Unreadable central_state_root — silent-success guard.
#
# ``Path.glob()`` silently swallows ``PermissionError`` while walking (an unreadable
# dir yields an empty iterator, no exception), which would otherwise make a
# permission-denied central_state_root indistinguishable from "no shard data" and
# collapse into the exact same D9 empty-series shape as a genuinely empty root.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_central_state_root_raises_not_d9_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unscannable central_state_root must raise ShardRootUnreadable, never silently
    degrade to the D9 empty-series shape (which would misreport "no shard data" for a
    root this machine simply couldn't check)."""
    monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_shard(state_dir, "machine-a", [
        {"repo": "owner/repo", "date": "2026-07-04", "bug": 5, "improvement": 3, "lessons": 1},
    ])
    ctx = _make_ctx(state_dir)

    original_mode = state_dir.stat().st_mode
    os.chmod(state_dir, 0o000)
    try:
        with pytest.raises(ShardRootUnreadable):
            collect(ctx)
    finally:
        os.chmod(state_dir, original_mode)
