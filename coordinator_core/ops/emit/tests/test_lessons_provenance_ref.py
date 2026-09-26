"""Regression test — lessons section provenance.ref MUST be null for local_fs.

The lessons porter (coordinator_core/ops/emit/sections/lessons.py) delegates entirely
to the frozen external producer bin/lib/emit-lesson-summaries.py, which hardcodes
provenance.source_kind="local_fs" but unconditionally populates provenance.ref with
{branch, sha}. That violates the D9/cockpit-contract invariant (vendored schema
lesson-summary.schema.json:135-151; context.py:_GIT_BACKED_SOURCE_KINDS) that ref MUST
be null for non-git-backed source kinds, and threw a ZodError (90 identical
provenance.ref violations) in DoE's SnapshotEnvelope.parse().

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P09
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import _GIT_BACKED_SOURCE_KINDS, EmitContext
from coordinator_core.ops.emit.resolvers import resolve_coordinator_root
from coordinator_core.ops.emit.sections.lessons import collect

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _make_ctx() -> EmitContext:
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


@pytest.mark.real_home
class TestLessonsProvenanceRefInvariant:

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
        ctx = _make_ctx()
        records, _malformed = collect(ctx)
        assert any(r["provenance"]["source_kind"] == "local_fs" for r in records), (
            "fixture must include a local_fs-sourced lesson record to exercise the ref-null guard"
        )
