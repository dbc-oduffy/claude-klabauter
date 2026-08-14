"""Regression test — lessons section provenance.ref MUST be null for local_fs.

The lessons porter (coordinator_core/ops/emit/sections/lessons.py) delegates entirely
to the frozen external producer bin/lib/emit-lesson-summaries.py, which hardcodes
provenance.source_kind="local_fs" but unconditionally populates provenance.ref with
{branch, sha}. That violates the D9/cockpit-contract invariant (vendored schema
lesson-summary.schema.json:135-151; context.py:_GIT_BACKED_SOURCE_KINDS) that ref MUST
be null for non-git-backed source kinds, and threw a ZodError (90 identical
provenance.ref violations) in DoE's SnapshotEnvelope.parse().

Sibling regression: test_backlog_history.py::test_provenance_uses_canonical_contract_enums
(landed 48d3c29) — same failure shape (non-null-only coverage let a canonical-value drift
ship undetected), different section porter.

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P09
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import _GIT_BACKED_SOURCE_KINDS, EmitContext
from coordinator_core.ops.emit.envelope import resolve_coordinator_root
from coordinator_core.ops.emit.sections.lessons import collect

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _make_ctx() -> EmitContext:
    """Build an EmitContext pointed at the frozen fixture tree (frozen-fixture doctrine).

    Mirrors test_emit_parity.build_emit_context() — subprocess_root redirects the lessons
    producer's DATA reads to fixtures/root so collect() output is reproducible, while
    coordinator_root stays the real coordinator plugin dir (the producer script itself must
    be real; only the data root is frozen).
    """
    fixture_root = _FIXTURES / "root"
    return EmitContext(
        repo_root=fixture_root,
        coordinator_root=resolve_coordinator_root(),
        central_state_root=fixture_root / "state",
        git_branch="work/fixture/2026-07-01",
        git_sha="aaaaaaaabbbbbbbbccccccccddddddddeeeeeeee",
        git_sha_short="aaaaaaaa",
        observed_at="2026-07-06T15:54:47Z",
        hostname="fixture.local",
        repo_name="dbc-oduffy/.example-doctrine-mirror-repo",
        subprocess_root=fixture_root,
    )


@pytest.mark.real_home  # live-tree parity oracle: resolves the real coordinator root
class TestLessonsProvenanceRefInvariant:
    """D9 bidirectional invariant: ref is null for every non-git-backed source_kind."""

    def test_local_fs_records_have_null_ref(self) -> None:
        """Every record with source_kind not in _GIT_BACKED_SOURCE_KINDS must have ref=None.

        Regression guard: prior coverage (test_emit_parity's golden-fixture comparison) only
        asserted parity with a golden that itself carried the bug — it did not independently
        assert the invariant, so the drift shipped undetected. This test pins the invariant
        directly against the real producer output, not against a golden that could itself drift.
        """
        ctx = _make_ctx()
        records, _malformed = collect(ctx)
        assert records, "fixture must produce at least one lesson record to exercise the guard"
        for record in records:
            provenance = record["provenance"]
            if provenance["source_kind"] not in _GIT_BACKED_SOURCE_KINDS:
                assert provenance["ref"] is None, (
                    f"record {record.get('lesson_key')!r}: source_kind="
                    f"{provenance['source_kind']!r} is not git-backed but ref="
                    f"{provenance['ref']!r} (D9 invariant, context.py:_GIT_BACKED_SOURCE_KINDS)"
                )

    def test_source_kind_is_local_fs_in_fixture(self) -> None:
        """Sanity anchor: the fixture's single lesson record is local_fs (the exact drift shape).

        Pins the scenario this regression targets — if the fixture ever moves to a git-backed
        source_kind, this test's failure signals the primary assertion above no longer exercises
        the local_fs path and needs a fixture with a local_fs record re-added.
        """
        ctx = _make_ctx()
        records, _malformed = collect(ctx)
        assert any(r["provenance"]["source_kind"] == "local_fs" for r in records), (
            "fixture must include a local_fs-sourced lesson record to exercise the ref-null guard"
        )
