"""Market-intel arrays (competitor_summaries / intelligence_signals) — empty-by-design.

Purpose: cockpit-contract v2.16.0 added two example-market-data-repo entities
(``competitor-summary``, ``intelligence-signal``) to ``SnapshotEnvelope``. Claude-klabauter is NOT the
data path for this data — example-market-data-repo routes to cockpit's ``ingestEmission`` plane
directly, never through ``artifact.emit`` — so these two arrays are wired present-but-empty
in ``envelope._empty_skeleton`` with NO section porter, and stay that way permanently. See
the negative-spec block above ``envelope._MALFORMED_KEYS`` for the full rationale + authority
citation (seam-confirm consult actioned by claude-central-em 2026-07-14).

This test asserts the wiring is present and validates against the vendored
``snapshot-envelope.schema.json`` — it is NOT a round-trip/collect() test (there is no
collect() for these two arrays, by design).

Latent-bug fix (2026-07-21, in-process-validation cutover): the vendored schema pin has since
moved past v2.17.0 (currently 2.20.0, per the re-vendor + ``assert_version_consistency``'s
2026-07-21 rework — see ``validate.py``). The two real-emission assertions below that read
``read_schema_version()`` / ``data["schema_version"]`` now compare against
``validate.read_schema_version()`` DYNAMICALLY rather than a hardcoded ``"2.17.0"`` literal,
so this test does not go stale again on the next re-vendor. The class/test names below keep
their historical ``2_17_0``/``v2170`` naming for continuity with the plan this file backlinks
to; only the LITERAL comparison values changed.

Spec backlink: cross-repo/inbox/2026-07-14-claude-klabauter-em-cockpit-v2160-source-seam.md
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.envelope import _empty_skeleton, emit
from coordinator_core.ops.emit.validate import _VENDOR_CONTRACT, read_schema_version

_SNAPSHOT_ENVELOPE_SCHEMA = _VENDOR_CONTRACT / "schema" / "snapshot-envelope.schema.json"


def _make_ctx(central_state_root: Path, repo_root: Path) -> EmitContext:
    """Minimal EmitContext factory (mirrors test_emit_graceful_absent.py's helper)."""
    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=central_state_root,
        git_branch="test-branch",
        git_sha="0000000000000000000000000000000000000000",
        git_sha_short="00000000",
        observed_at="2026-07-14T00:00:00Z",
        hostname="test-host",
        repo_name="test/repo",
    )


class TestEmptySkeletonCarriesMarketIntelArrays:
    """_empty_skeleton() present-but-empty wiring, independent of any live emit() run."""

    def test_competitor_summaries_present_and_empty(self) -> None:
        skeleton = _empty_skeleton(
            schema_version="2.17.0", emitted_at="2026-07-14T00:00:00Z",
            emitted_by_machine="test-host",
        )
        assert skeleton["competitor_summaries"] == []

    def test_intelligence_signals_present_and_empty(self) -> None:
        skeleton = _empty_skeleton(
            schema_version="2.17.0", emitted_at="2026-07-14T00:00:00Z",
            emitted_by_machine="test-host",
        )
        assert skeleton["intelligence_signals"] == []

    def test_malformed_buckets_present_and_empty(self) -> None:
        skeleton = _empty_skeleton(
            schema_version="2.17.0", emitted_at="2026-07-14T00:00:00Z",
            emitted_by_machine="test-host",
        )
        malformed = skeleton["malformed_records"]
        assert malformed["competitor_summaries"] == []
        assert malformed["intelligence_signals"] == []


class TestMarketIntelArraysInRealEmission:
    """A full emit() run (empty source dirs) still carries both arrays present-and-empty."""

    def test_full_emission_carries_both_empty_arrays(self, tmp_path: Path) -> None:
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)
        out = tmp_path / "output.json"
        emit(ctx, out=out)
        data = json.loads(out.read_text())
        assert data["competitor_summaries"] == []
        assert data["intelligence_signals"] == []
        assert data["malformed_records"]["competitor_summaries"] == []
        assert data["malformed_records"]["intelligence_signals"] == []

    def test_schema_version_is_2_17_0(self, tmp_path: Path) -> None:
        """Historical test name (v2.17.0 was the pin at authoring time); the assertion below
        is version-agnostic — it checks the emitted envelope's schema_version matches
        whatever the vendored pin currently resolves to, not a frozen literal (see module
        docstring's 2026-07-21 latent-bug-fix note)."""
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)
        out = tmp_path / "output.json"
        emit(ctx, out=out)
        data = json.loads(out.read_text())
        current_pin_version = read_schema_version()
        assert data["schema_version"] == current_pin_version
        assert read_schema_version() == current_pin_version


class TestValidatesAgainstVendoredV2170Schema:
    """The present-but-empty envelope validates cleanly against the vendored schema."""

    @pytest.mark.xfail(
        reason=(
            "KNOWN-PENDING bilateral gap, NOT a regression: the emitter emits the claude-klabauter-net-new "
            "top-level `commit_closures` array (since 477aceb7), but the vendored "
            "snapshot-envelope.schema.json has additionalProperties:false and no `commit_closures` "
            "declaration, so whole-envelope jsonschema.validate() fails with "
            "\"('commit_closures' was unexpected)\". `commit_closures`/`CommitClosure` is "
            "DELIBERATELY unregistered pending DoE-canonical-first promotion — registering it "
            "locally would force a cross-repo DoE-side commit-closure.schema.json and trip the "
            "D13/D21 bilateral version-bump gate this store-less emit leg exists to avoid. Posture + "
            "revisit-trigger: test_committed_emit_drift.py module docstring; "
            "state/improvement-queue/2026-07-17-commit-closure-entity-intentionally-excl-328d5281c0d2.yaml; "
            "docs/plans/2026-07-17-commit-closure-emission-fact.md. Confirmed 2026-07-21: after "
            "the producer-side removal of the separately-tracked top-level `coordinator_root_path` "
            "scalar, `commit_closures` is the ONLY key that fails this validation. strict=True re-arms this "
            "gate: the moment `commit_closures` is promoted into the vendored schema, this test "
            "XPASSes and the strict-xfail fails loud, forcing removal of this marker."
        ),
        strict=True,
    )
    def test_envelope_with_empty_market_intel_arrays_validates(self, tmp_path: Path) -> None:
        if not _SNAPSHOT_ENVELOPE_SCHEMA.exists():
            pytest.skip("vendored snapshot-envelope.schema.json not present")
        jsonschema = pytest.importorskip("jsonschema")

        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)
        out = tmp_path / "output.json"
        emit(ctx, out=out)
        envelope = json.loads(out.read_text())

        # The former top-level `coordinator_root_path` scalar (AC12) is no longer emitted —
        # removed at the producer 2026-07-21 (zero readers; forbidden by the SnapshotEnvelope
        # schema's additionalProperties:false). No in-test strip is needed anymore; only
        # `commit_closures` (the intentionally-unregistered claude-klabauter-net-new key) still fails
        # whole-envelope validation, which is what the strict-xfail below captures.

        schema = json.loads(_SNAPSHOT_ENVELOPE_SCHEMA.read_text(encoding="utf-8"))
        # No jsonschema.exceptions.ValidationError == passes; let any failure raise loud.
        jsonschema.validate(instance=envelope, schema=schema)
