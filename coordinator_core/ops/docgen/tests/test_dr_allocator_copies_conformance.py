"""coordinator_core.ops.docgen.tests.test_dr_allocator_copies_conformance —
byte-identity compensating control for the deliberate two-copy vendoring of
``dr_allocator.py``.

Purpose: ``coordinator/bin/lib/dr_allocator.py`` (the live import target of
the in-repo ``coordinator-doc-new`` CLI) and
``coordinator_core/ops/docgen/dr_allocator.py`` (the DR-225 vendored copy
consumed by ``coordinator_core``) are two on-disk copies of the same
algorithm, kept deliberately separate rather than imported from one
location (DR-083, DR-225). Nothing else guards against the two copies
drifting apart silently — this test is that guard.

Spec backlink: docs/plans/2026-07-21-strang-12-doc-generation-strangle.md § C6 (AC5)
Negative-spec: this test does not exercise either copy's behavior (see
``test_dr_allocator.py`` for behavioral coverage) — it asserts byte-for-byte
textual identity only.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIVE_COPY = _REPO_ROOT / "coordinator" / "bin" / "lib" / "dr_allocator.py"
_VENDORED_COPY = _REPO_ROOT / "coordinator_core" / "ops" / "docgen" / "dr_allocator.py"


def test_dr_allocator_copies_byte_identical():
    assert _LIVE_COPY.is_file(), f"live copy not found at {_LIVE_COPY}"
    assert _VENDORED_COPY.is_file(), f"vendored copy not found at {_VENDORED_COPY}"

    live_bytes = _LIVE_COPY.read_bytes()
    vendored_bytes = _VENDORED_COPY.read_bytes()

    assert live_bytes == vendored_bytes, (
        f"dr_allocator.py copies have drifted apart:\n"
        f"  live (coordinator/bin/lib/dr_allocator.py): {_LIVE_COPY}\n"
        f"  vendored (coordinator_core/ops/docgen/dr_allocator.py): {_VENDORED_COPY}\n"
        "These two copies are deliberately vendored, not shared via import, "
        "per DR-083/DR-225 — they must stay byte-identical. Re-sync the "
        "vendored copy from the live SSOT (or vice versa, per whichever "
        "changed) and re-run this test."
    )
