"""Unit coverage for ``rollups._since_cutoff`` — pins the 30-day-back arithmetic, the
UTC attach, and the ``%Y-%m-%d`` output shape that ``ops.records_query._SINCE_ISO_RE``
expects, plus the malformed-``observed_at`` raise-loud contract.

Review: coordinatorcode-reviewer — locks in the fix that moved ``_since_cutoff(ctx)``
out of ``_query_completions``'s ``try`` so a malformed ``observed_at`` raises instead
of silently degrading to zero completions.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections.rollups import _query_completions, _since_cutoff


def _make_ctx(observed_at: str, tmp_path: Path) -> EmitContext:
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=tmp_path,
        git_branch="test-branch",
        git_sha="deadbeef" * 5,
        git_sha_short="deadbeef",
        observed_at=observed_at,
        hostname="test-host",
        repo_name="test/repo",
    )


def test_since_cutoff_is_30_days_before_observed_at_utc(tmp_path):
    ctx = _make_ctx("2026-07-31T12:00:00Z", tmp_path)
    result = _since_cutoff(ctx)

    expected = (
        datetime.datetime(2026, 7, 31, 12, 0, 0, tzinfo=datetime.timezone.utc)
        - datetime.timedelta(days=30)
    ).strftime("%Y-%m-%d")
    assert result == expected
    assert result == "2026-07-01"


def test_since_cutoff_output_matches_records_query_since_iso_shape(tmp_path):
    ctx = _make_ctx("2026-01-15T00:00:00Z", tmp_path)
    result = _since_cutoff(ctx)

    from coordinator_core.ops.records_query import _SINCE_ISO_RE

    assert _SINCE_ISO_RE.match(result)


def test_since_cutoff_raises_on_malformed_observed_at(tmp_path):
    ctx = _make_ctx("not-a-timestamp", tmp_path)

    with pytest.raises(ValueError):
        _since_cutoff(ctx)


def test_query_completions_raises_loudly_on_malformed_observed_at_not_degrades(tmp_path):
    """Locks in Finding 1's fix: the cutoff must be computed before the try/except so a
    malformed ``observed_at`` propagates a ``ValueError`` instead of being swallowed by
    the query-failure ``except (ValueError, SystemExit): return []`` — which would
    silently zero completions rather than surface the bad input."""
    ctx = _make_ctx("not-a-timestamp", tmp_path)

    with pytest.raises(ValueError):
        _query_completions(ctx)
