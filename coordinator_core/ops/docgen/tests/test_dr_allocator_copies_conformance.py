
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
