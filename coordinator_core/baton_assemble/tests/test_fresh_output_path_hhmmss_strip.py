"""Regression for `_compute_fresh_output_path`'s date-prefix strip missing the
`_HHMMSS_`-disambiguated shape, which doubled the date prefix on any
multi-hop same-day chain.

Spec backlink: cross-repo/archive/2026-08-20-example-game-repo-em-continued-into-doubled-date-prefix.md
(sender-supplied fix shape + regression assertion).

Negative-spec: before the fix, feeding a stem already carrying
`YYYY-MM-DD_HHMMSS_` produced `YYYY-MM-DD-YYYY-MM-DD_HHMMSS_<slug>.md` (the
hyphen-delimited regex no-op'd against the underscore-delimited shape). This
test pins that exactly one date prefix survives, for both the plain
hyphen-delimited shape and the disambiguated shape.
"""
from __future__ import annotations

import datetime

from coordinator_core.baton_assemble import _compute_fresh_output_path


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def test_hhmmss_disambiguated_stem_gets_exactly_one_date_prefix() -> None:
    artifact_path = "state/handoffs/2026-08-20_155409_retire-host-resource-watchdog.md"
    result = _compute_fresh_output_path(artifact_path)
    today = _today()
    expected = f"state/handoffs/{today}-retire-host-resource-watchdog.md"
    assert result == expected, result
    # Exactly one date prefix -- not doubled.
    assert result.count(today) == 1, result


def test_plain_hyphen_dated_stem_still_strips_once() -> None:
    artifact_path = "docs/plans/2026-07-26-priority-ledger.md"
    result = _compute_fresh_output_path(artifact_path)
    today = _today()
    expected = f"state/handoffs/{today}-priority-ledger.md"
    assert result == expected, result
