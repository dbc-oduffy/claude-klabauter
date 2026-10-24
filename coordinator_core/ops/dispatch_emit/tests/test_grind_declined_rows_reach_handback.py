"""
coordinator_core.ops.dispatch_emit.tests.test_grind_declined_rows_reach_handback

Purpose: pins that the emitted grind script returns the manifest's declined rows in its HANDBACK, so a
legitimately declined target is named at the run result instead of vanishing.
"""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.queue_select import DeclinedEntry, Manifest

from .test_grind_compose import _compose, _fixture_manifest


def _with_declined(declined):
    base = _fixture_manifest()
    return Manifest(
        entries=base.entries, batch_sizes=base.batch_sizes, source=None, digest=base.digest, declined=declined,
    )


def test_declined_row_is_returned_in_the_handback():
    declined = (DeclinedEntry(row_id="fleet-mode-row", path="state/bug-backlog/fleet.yaml", reason="route-to-plan"),)
    script = _compose(manifest=_with_declined(declined))
    handback = script[script.index("const HANDBACK = {"):]
    assert "declined: QUEUE_GRIND_MANIFEST.declined," in handback[: handback.index("};")]
    manifest_literal = script[script.index("const QUEUE_GRIND_MANIFEST"): script.index("const HANDBACK = {")]
    assert '"row_id": "fleet-mode-row"' in manifest_literal
    assert '"reason": "route-to-plan"' in manifest_literal


def test_manifest_without_declined_rows_returns_an_empty_list():
    script = _compose(manifest=_with_declined(()))
    manifest_literal = script[script.index("const QUEUE_GRIND_MANIFEST"): script.index("const HANDBACK = {")]
    assert '"declined": []' in manifest_literal
    assert "declined: QUEUE_GRIND_MANIFEST.declined," in script[script.index("const HANDBACK = {"):]
