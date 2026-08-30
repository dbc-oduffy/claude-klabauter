"""
coordinator_core.install.tests.test_wsc_close_forwarder_stays_deleted

Purpose: `wsc-close.py` was renamed to `coordinator/bin/archive-session-scope.py`
and its temporary forwarder (`wsc-close.py` + `.cmd`) deleted once the
cross-repo caller (DoE-claude's `sessionend-archive-session.py::_archive`)
was repointed -- see `docs/install/relocation-ledger.json`'s `wsc-close.py`
entry (`disposition: "moved"`, `forwarder: "none"`). `bin_inventory_gate.py`
catches an UN-RECORDED disappearance but has no opinion about the reverse
direction: a `wsc-close.py`/`.cmd` file silently reappearing in
`coordinator/bin/` without a ledger update reopens the exact reviewless-
warm-service risk the ledger entry's own reason field names ("an allowlist
row naming a path with nothing on it is reviewless warm-service the
instant a .py lands there again"). This is a pure filesystem check -- no
subprocess, no interpreter start -- so it stays well under this repo's
500ms brightline.

Negative-spec:
    - Does NOT re-implement `bin_inventory_gate`'s disappearance check;
      this only pins the resurrection direction that gate does not cover.
    - Does NOT assert anything about the ledger's prose -- only that the
      two concrete forwarder paths stay absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.engine_root import coordinator_engine_root


@pytest.mark.real_home  # live-tree oracle: resolves the real CLAUDE_KLABAUTER_ROOT via the
# machine-local registry, which the suite-root `_quarantine_real_home` autouse
# fixture would otherwise route into an empty per-test HOME -- see
# `bin_inventory_gate.py`'s own `test_real_tracked_inventory_passes_gate` for the
# same pattern. This test's whole point is pinning the REAL tree's forwarder
# absence, so a tmp_path fixture would test nothing.
def test_wsc_close_forwarder_and_cmd_sibling_stay_deleted() -> None:
    bin_dir = Path(coordinator_engine_root()) / "coordinator" / "bin"
    resurrected = [
        str(p) for p in (bin_dir / "wsc-close.py", bin_dir / "wsc-close.cmd") if p.exists()
    ]
    assert not resurrected, (
        "wsc-close.py/.cmd resurrected in coordinator/bin/ without a "
        "relocation-ledger update -- see docs/install/relocation-ledger.json's "
        f"wsc-close.py entry. Found: {resurrected}"
    )
