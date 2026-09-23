"""
coordinator_core.workstream_complete.tests.test_group_em_cluster_stays_hand_invoked

Pin for baton `hnd-group-em-and-uhura-entry-stop-cce943` (census cluster B10), discharging AC4/AC5
of `docs/plans/2026-09-11-group-em-and-uhura-entry-stop-being-hand-run.md`.

The classification record
(`docs/reference/group-em-and-uhura-directive-classification.md`) decided four of the five B10
scripts `keep-as-entrypoint` and one (`plan-reversibility-eligibility`) `emit` — the latter is B0's
demonstration directive, not this plan's. This module makes that decision re-checkable: if a future
session wires one of the four `keep-as-entrypoint` barewords into a directive-dispatch surface
without reopening the classification, this test goes red and names the record to re-open.

NEGATIVE SPEC. This does NOT assert anything about `plan-reversibility-eligibility` — it is
deliberately excluded from both assertions below, because B0 adds it to `CONSUMES_MANIFEST` (and
potentially `_PLUGIN_LOCAL_CLIS`) by design. Asserting its absence here would make this guard red
the moment B0 lands, which is the opposite of what a pin is for.

This module resolves no DoE-claude clone and shells out to nothing — the guard reads two in-process
Python objects only, so it is green (or SKIPPED, see below) on a box with no DoE clone at all.
"""
from __future__ import annotations

import pathlib

from coordinator_core.workstream_complete import CONSUMES_MANIFEST, apply

#: The four B10 members this plan keeps hand-invoked. `plan-reversibility-eligibility` is the
#: fifth B10 member and is excluded deliberately (see module docstring).
_KEEP_AS_ENTRYPOINT = (
    "group-em-enter",
    "group-em-nomination",
    "group-em-watch-cli",
    "uhura-mode",
)

_CLASSIFICATION_RECORD = pathlib.Path(
    "docs/reference/group-em-and-uhura-directive-classification.md"
)


def test_classification_record_exists_and_names_all_five_scripts() -> None:
    root = pathlib.Path(__file__).resolve().parents[3]
    record_path = root / _CLASSIFICATION_RECORD
    assert record_path.is_file(), (
        f"{_CLASSIFICATION_RECORD} is missing -- the B10 per-script classification decision "
        "must live on disk, not only in a closed baton (see "
        "docs/plans/2026-09-11-group-em-and-uhura-entry-stop-being-hand-run.md AC1)."
    )
    text = record_path.read_text(encoding="utf-8")
    for name in (*_KEEP_AS_ENTRYPOINT, "plan-reversibility-eligibility"):
        assert name in text, (
            f"{_CLASSIFICATION_RECORD} no longer names {name!r} -- re-open the B10 "
            "classification in that record before touching this pin."
        )


def test_keep_as_entrypoint_members_absent_from_consumes_manifest() -> None:
    manifest = set(CONSUMES_MANIFEST)
    wired = manifest.intersection(_KEEP_AS_ENTRYPOINT)
    assert not wired, (
        f"{sorted(wired)} appear in workstream_complete.CONSUMES_MANIFEST, but "
        f"{_CLASSIFICATION_RECORD} classifies them keep-as-entrypoint. Re-open that "
        "classification (docs/plans/2026-09-11-group-em-and-uhura-entry-stop-being-hand-run.md) "
        "before wiring one of these as a directive."
    )


def test_keep_as_entrypoint_members_absent_from_plugin_local_clis() -> None:
    plugin_local_clis = getattr(apply, "_PLUGIN_LOCAL_CLIS", None)
    if plugin_local_clis is None:
        import pytest

        pytest.skip(
            "B10-PIN-INERT-UNTIL-B0: apply._PLUGIN_LOCAL_CLIS does not exist yet "
            "(docs/plans/2026-09-07-directive-resolution-reaches-a-plugin-local-cli.md, B0, "
            "unlanded). This assertion is inert until B0 lands the symbol it checks."
        )
    wired = set(plugin_local_clis).intersection(_KEEP_AS_ENTRYPOINT)
    assert not wired, (
        f"{sorted(wired)} appear in workstream_complete.apply._PLUGIN_LOCAL_CLIS, but "
        f"{_CLASSIFICATION_RECORD} classifies them keep-as-entrypoint. Re-open that "
        "classification (docs/plans/2026-09-11-group-em-and-uhura-entry-stop-being-hand-run.md) "
        "before wiring one of these as a plugin-local directive."
    )
